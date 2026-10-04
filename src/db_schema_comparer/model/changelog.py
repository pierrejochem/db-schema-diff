"""Liquibase ``DATABASECHANGELOG`` state.

Comparing migration history catches a class of drift the DDL diff cannot explain: *why* the
schemas differ, and which environment is behind.

Two facts about this platform shape the model:

* The table is **not** in ``public``. ``cumo-invoicing`` sets
  ``spring.liquibase.liquibase-schema=cumo-invoicing``, so it lives in a quoted, hyphenated
  schema of its own. The table is located by scanning the catalog, never assumed.
* A changeset's identity is ``(id, author)`` and deliberately **excludes** ``filename``.
  ``cumo-invoicing``'s ``master.xml`` mixes relative and non-relative includes, and some
  changelogs declare their own ``logicalFilePath``, so the same changeset legitimately
  records a different filename in different deployments.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

#: ``(id, author)`` — a changeset's identity.
ChangeSetRef = tuple[str, str]

#: Columns Liquibase has always written, so they are safe to select unconditionally.
CORE_COLUMNS = ("ID", "AUTHOR", "FILENAME", "DATEEXECUTED", "ORDEREXECUTED", "EXECTYPE")

#: Columns added in later Liquibase releases. Selecting one that does not exist fails the
#: whole query, so the real column list is introspected first and intersected with this.
OPTIONAL_COLUMNS = (
    "MD5SUM",
    "DESCRIPTION",
    "COMMENTS",
    "TAG",
    "LIQUIBASE",
    "CONTEXTS",
    "LABELS",
    "DEPLOYMENT_ID",
)


class ExecType(StrEnum):
    """``EXECTYPE`` values Liquibase writes."""

    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    RERAN = "RERAN"
    MARK_RAN = "MARK_RAN"


@dataclass(frozen=True, slots=True)
class ChangelogLocation:
    """Where a ``DATABASECHANGELOG`` table was found.

    ``table`` preserves the catalog's own spelling: Liquibase writes the name uppercase on
    some databases and lowercase on others, and the name is later quoted, so it must not be
    case-folded.
    """

    schema: str
    table: str

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.table}"

    def to_json_dict(self) -> dict[str, str]:
        return {"schema": self.schema, "table": self.table}

    @classmethod
    def from_json_dict(cls, data: Mapping[str, Any]) -> ChangelogLocation:
        return cls(schema=data["schema"], table=data["table"])


@dataclass(frozen=True, slots=True)
class ChangeSetRow:
    """One applied changeset."""

    id: str
    author: str
    filename: str
    order_executed: int
    exec_type: str
    md5sum: str | None = None
    date_executed: datetime | None = None
    tag: str | None = None
    description: str | None = None
    liquibase_version: str | None = None
    deployment_id: str | None = None

    @property
    def ref(self) -> ChangeSetRef:
        """The ``(id, author)`` identity."""
        return (self.id, self.author)

    @property
    def checksum_algorithm(self) -> str | None:
        """The algorithm-version prefix of ``MD5SUM``, e.g. ``"8"`` or ``"9"``.

        Liquibase prefixes every checksum with the version of the algorithm that produced it
        and rewrites all of them on upgrade. Comparing digests across an upgrade would report
        every changeset as mismatched, so the prefix is separated out and a prefix difference
        is reported once, as a Liquibase version skew, rather than per changeset.
        """
        if not self.md5sum or ":" not in self.md5sum:
            return None
        return self.md5sum.split(":", 1)[0]

    @property
    def checksum_digest(self) -> str | None:
        """``MD5SUM`` without its algorithm-version prefix."""
        if not self.md5sum:
            return None
        return self.md5sum.split(":", 1)[1] if ":" in self.md5sum else self.md5sum

    @property
    def basename(self) -> str:
        """Final path segment of ``FILENAME``.

        Compared instead of the full path, because the path varies with how a changelog was
        included while the basename identifies the same file.
        """
        return self.filename.rsplit("/", 1)[-1]

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "author": self.author,
            "filename": self.filename,
            "order_executed": self.order_executed,
            "exec_type": self.exec_type,
            "md5sum": self.md5sum,
            "date_executed": self.date_executed.isoformat() if self.date_executed else None,
            "tag": self.tag,
            "description": self.description,
            "liquibase_version": self.liquibase_version,
            "deployment_id": self.deployment_id,
        }

    @classmethod
    def from_json_dict(cls, data: Mapping[str, Any]) -> ChangeSetRow:
        executed = data.get("date_executed")
        return cls(
            id=data["id"],
            author=data["author"],
            filename=data["filename"],
            order_executed=int(data["order_executed"]),
            exec_type=data["exec_type"],
            md5sum=data.get("md5sum"),
            date_executed=datetime.fromisoformat(executed) if executed else None,
            tag=data.get("tag"),
            description=data.get("description"),
            liquibase_version=data.get("liquibase_version"),
            deployment_id=data.get("deployment_id"),
        )


@dataclass(frozen=True, slots=True)
class ChangelogState:
    """One database's migration history.

    ``location is None`` means no ``DATABASECHANGELOG`` table was found, which is reported
    rather than treated as an error: plenty of databases are not Liquibase-managed.
    """

    location: ChangelogLocation | None = None
    columns_present: frozenset[str] = frozenset()
    rows: tuple[ChangeSetRow, ...] = ()
    lock_held: bool | None = None
    """``DATABASECHANGELOGLOCK.LOCKED``.

    True means a deployment may be in progress, so the DDL snapshot could have been taken
    mid-migration — worth a warning before anyone trusts the diff.
    """
    candidates: tuple[ChangelogLocation, ...] = ()
    """Every candidate found, when there was more than one and the choice was ambiguous."""

    @property
    def present(self) -> bool:
        return self.location is not None

    @property
    def ambiguous(self) -> bool:
        """Whether several changelog tables were found and none was configured."""
        return self.location is None and len(self.candidates) > 1

    @property
    def count(self) -> int:
        return len(self.rows)

    @property
    def last_tag(self) -> str | None:
        """Most recently applied tag — the most human-readable line in any report."""
        for row in reversed(self.rows):
            if row.tag:
                return row.tag
        return None

    def by_ref(self) -> dict[ChangeSetRef, ChangeSetRow]:
        """Rows keyed by ``(id, author)``.

        A duplicate ref means the changeset ran more than once, e.g. ``runOnChange``; the
        last occurrence wins, matching what Liquibase itself considers current.
        """
        return {row.ref: row for row in self.rows}

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "location": self.location.to_json_dict() if self.location else None,
            "columns_present": sorted(self.columns_present),
            "rows": [row.to_json_dict() for row in self.rows],
            "lock_held": self.lock_held,
            "candidates": [c.to_json_dict() for c in self.candidates],
        }

    @classmethod
    def from_json_dict(cls, data: Mapping[str, Any]) -> ChangelogState:
        location = data.get("location")
        return cls(
            location=ChangelogLocation.from_json_dict(location) if location else None,
            columns_present=frozenset(data.get("columns_present", ())),
            rows=tuple(ChangeSetRow.from_json_dict(r) for r in data.get("rows", ())),
            lock_held=data.get("lock_held"),
            candidates=tuple(
                ChangelogLocation.from_json_dict(c) for c in data.get("candidates", ())
            ),
        )
