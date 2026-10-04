"""Running the catalog queries.

This module has one job and one rule. The job is to run SQL and hand back raw rows. The rule is
that it **never normalises anything** — no type aliasing, no expression canonicalisation, no
sentinel substitution. All of that lives in :mod:`db_schema_comparer.normalize`, and keeping
the two apart is what makes normalisation testable without a database and introspection
testable without caring how values are canonicalised.

Every query is set-based. Per-object round trips are forbidden: a schema with 200 tables would
otherwise need thousands of queries, and on a production server the latency and the lock
exposure both matter.
"""

from __future__ import annotations

import logging
from functools import cache
from importlib import resources
from typing import Any

import psycopg
from psycopg import sql

from ..model.changelog import (
    CORE_COLUMNS,
    OPTIONAL_COLUMNS,
    ChangelogLocation,
    ChangelogState,
    ChangeSetRow,
)
from .features import ServerFeatures

log = logging.getLogger(__name__)

#: Schemas never inventoried. pg_% covers pg_catalog, pg_toast and every numbered temporary
#: schema in one condition.
SYSTEM_SCHEMAS = ("information_schema",)


@cache
def load_query(name: str) -> str:
    """Read one ``.sql`` file from the installed package.

    Via ``importlib.resources`` rather than a path relative to ``__file__``, so the queries are
    found the same way whether the package is a source checkout, a wheel, or a zipimport.
    """
    return (resources.files(__package__).joinpath("queries", f"{name}.sql")).read_text(
        encoding="utf-8"
    )


class Introspector:
    """Reads one database's catalog.

    Holds a connection and the server's feature set; the feature set supplies the SQL fragments
    that stand in for columns an older release does not have.
    """

    def __init__(
        self, connection: psycopg.Connection[dict[str, Any]], features: ServerFeatures
    ) -> None:
        self._connection = connection
        self._features = features
        self._placeholders = features.placeholders()

    @property
    def features(self) -> ServerFeatures:
        return self._features

    # -- Queries -------------------------------------------------------------------------

    def server_info(self) -> dict[str, Any]:
        """Database identity, server version, encoding and collation."""
        rows = self._run("server")
        if not rows:  # pragma: no cover - current_database() always has a pg_database row
            raise RuntimeError("could not read the current database's catalog entry")
        return rows[0]

    def schemas(
        self, *, exclude: tuple[str, ...] = (), only: tuple[str, ...] | None = None
    ) -> list[str]:
        """Schema names to inventory, in sorted order."""
        rows = self._run(
            "schemas",
            {"exclude": list(exclude) + list(SYSTEM_SCHEMAS), "only": list(only) if only else None},
        )
        return [str(row["schema"]) for row in rows]

    def relations(self, schemas: list[str]) -> list[dict[str, Any]]:
        """Tables, partitioned parents and partition leaves."""
        return self._run("relations", {"schemas": schemas})

    def columns(self, schemas: list[str]) -> list[dict[str, Any]]:
        """Columns of tables and views, with dense ordinals."""
        return self._run("columns", {"schemas": schemas})

    def constraints(self, schemas: list[str]) -> list[dict[str, Any]]:
        """Primary key, unique, foreign key, check and exclusion constraints."""
        return self._run("constraints", {"schemas": schemas})

    def indexes(self, schemas: list[str]) -> list[dict[str, Any]]:
        """Index keys, one row each, for indexes that do not merely back a constraint.

        The caller groups the rows by index. Returning one row per key keeps this a single query;
        a round trip per index would be thousands of queries on a real schema.
        """
        return self._run("indexes", {"schemas": schemas})

    def views(self, schemas: list[str]) -> list[dict[str, Any]]:
        """Views and materialized views, with their resolved column lists."""
        return self._run("views", {"schemas": schemas})

    def sequences(self, schemas: list[str]) -> list[dict[str, Any]]:
        """Sequence definitions. Never their positions, which are runtime state."""
        return self._run("sequences", {"schemas": schemas})

    def routines(self, schemas: list[str]) -> list[dict[str, Any]]:
        """Functions and procedures, with bodies from ``prosrc``."""
        return self._run("routines", {"schemas": schemas})

    def triggers(self, schemas: list[str]) -> list[dict[str, Any]]:
        """User triggers only; the internal ones enforcing foreign keys are excluded."""
        return self._run("triggers", {"schemas": schemas})

    def types(self, schemas: list[str]) -> list[dict[str, Any]]:
        """Enum, domain, composite and range types."""
        return self._run("types", {"schemas": schemas})

    def extensions(self) -> list[dict[str, Any]]:
        """Installed extensions, compared as a unit rather than by their contents."""
        return self._run("extensions")

    def changelog_state(self, location: ChangelogLocation) -> ChangelogState:
        """Read one ``DATABASECHANGELOG`` table.

        The column list is introspected first and intersected with the columns this build knows
        about, so a database running an older Liquibase is read successfully rather than failing the
        run. Both identifiers are composed through :class:`psycopg.sql.Identifier`: the schema is
        genuinely hyphenated in this platform, and the table name's case varies.
        """
        spelling = self._column_spelling(location)
        present = set(spelling)
        wanted = [c for c in (*CORE_COLUMNS, *OPTIONAL_COLUMNS) if c in present]
        if not all(c in present for c in ("ID", "AUTHOR")):
            log.warning(
                "%s has no ID/AUTHOR columns; not treating it as a Liquibase changelog",
                location.qualified,
            )
            return ChangelogState(location=None)

        order_by = "ORDEREXECUTED" if "ORDEREXECUTED" in present else "ID"
        statement = sql.SQL("SELECT {columns} FROM {table} ORDER BY {order}").format(
            # Each identifier uses the catalog's own spelling, because these names are quoted and
            # Liquibase writes them uppercase on some databases and lowercase on others.
            columns=sql.SQL(", ").join(
                sql.SQL("{} AS {}").format(sql.Identifier(spelling[c]), sql.Identifier(c))
                for c in wanted
            ),
            table=sql.Identifier(location.schema, location.table),
            # ORDEREXECUTED is a per-deployment counter, never comparable between servers, but it
            # is the right order to *read* in: it is this deployment's own sequence.
            order=sql.Identifier(spelling[order_by]),
        )
        with self._connection.cursor() as cursor:
            cursor.execute(statement)
            rows = list(cursor.fetchall())

        return ChangelogState(
            location=location,
            columns_present=frozenset(present),
            rows=tuple(_changeset_row(row) for row in rows),
            lock_held=self._changelog_lock(location),
        )

    def _column_spelling(self, location: ChangelogLocation) -> dict[str, str]:
        """Canonical upper-case column name to the catalog's own spelling of it.

        Both halves are needed: the upper-case name is what this code reasons about, and the
        catalog's spelling is what a quoted identifier in a query has to use. Liquibase writes
        these names uppercase on some databases and lowercase on others.
        """
        rows = self._run("changelog_columns", {"schema": location.schema, "table": location.table})
        return {str(row["name"]).upper(): str(row["name"]) for row in rows}

    def _changelog_lock(self, location: ChangelogLocation) -> bool | None:
        """Whether a Liquibase deployment lock is held.

        A held lock means the snapshot may have been taken mid-migration, which a reader needs to
        know before trusting any of the findings. ``None`` when there is no lock table to ask.
        """
        lock = next(
            (
                c
                for c in self.locate_changelog(include_lock=True)
                if c.schema == location.schema and c.table.lower() == "databasechangeloglock"
            ),
            None,
        )
        if lock is None:
            return None
        spelling = self._column_spelling(lock)
        if "LOCKED" not in spelling:
            return None
        statement = sql.SQL("SELECT bool_or({locked}) AS held FROM {table}").format(
            locked=sql.Identifier(spelling["LOCKED"]),
            table=sql.Identifier(lock.schema, lock.table),
        )
        with self._connection.cursor() as cursor:
            cursor.execute(statement)
            row = cursor.fetchone()
        return bool(row["held"]) if row and row["held"] is not None else False

    def locate_changelog(self, *, include_lock: bool = False) -> list[ChangelogLocation]:
        """Find every Liquibase ``DATABASECHANGELOG`` table in the database.

        Scans **all** schemas, including any the user excluded from the DDL comparison: the
        table is not reliably in ``public`` — ``cumo-invoicing`` keeps it in a hyphenated schema
        of its own — and excluding its schema from the structural diff should not hide the
        migration history.
        """
        rows = self._run("changelog_locate")
        return [
            ChangelogLocation(schema=str(row["schema"]), table=str(row["name"]))
            for row in rows
            if include_lock or not row["is_lock"]
        ]

    # -- Internals -----------------------------------------------------------------------

    def _run(self, name: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Execute one named query.

        ``format`` fills only the version-gating placeholders, which are fixed text chosen by
        server version. Everything that came from a user or a catalog travels as a query
        parameter, so nothing here can be injected into.

        A mapping is always passed, even an empty one. psycopg only parses ``%`` placeholders
        when parameters are present, so passing ``None`` sometimes would mean a literal percent
        had to be escaped in some query files and not others — exactly the kind of rule that
        gets a file wrong later. One rule instead: every file doubles its literal percents.
        """
        statement = load_query(name).format(**self._placeholders)
        with self._connection.cursor() as cursor:
            cursor.execute(statement, params or {})
            rows = cursor.fetchall()
        log.debug("%s returned %d rows", name, len(rows))
        return list(rows)


def _changeset_row(row: dict[str, Any]) -> ChangeSetRow:
    """One DATABASECHANGELOG row, with the columns this build knows about.

    Keys are upper-cased by the query's column list, and anything absent from an older Liquibase
    schema simply comes back as ``None``.
    """
    return ChangeSetRow(
        id=str(row["ID"]),
        author=str(row["AUTHOR"]),
        filename=str(row.get("FILENAME") or ""),
        order_executed=int(row.get("ORDEREXECUTED") or 0),
        exec_type=str(row.get("EXECTYPE") or ""),
        md5sum=_optional(row.get("MD5SUM")),
        date_executed=row.get("DATEEXECUTED"),
        tag=_optional(row.get("TAG")),
        description=_optional(row.get("DESCRIPTION")),
        liquibase_version=_optional(row.get("LIQUIBASE")),
        deployment_id=_optional(row.get("DEPLOYMENT_ID")),
    )


def _optional(value: Any) -> str | None:
    return None if value is None else str(value)
