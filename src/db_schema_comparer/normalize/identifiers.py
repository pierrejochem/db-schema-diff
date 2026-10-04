"""Identifier quoting and schema renaming.

PostgreSQL identifiers are case-sensitive and may contain almost anything. This platform
proves it: ``"acme-invoicing"`` is a real schema name and ``QRTZ_LOCKS`` a real table name. So
nothing here ever case-folds a name, and every identifier that reaches SQL is quoted through
``psycopg.sql.Identifier`` rather than interpolated.
"""

from __future__ import annotations

import re

#: A name matching this needs no quoting when printed back into SQL.
_BARE = re.compile(r"^[a-z_][a-z0-9_$]*$")

#: Reserved words that must be quoted even though they look bare.
_RESERVED = frozenset(
    {
        "all",
        "analyse",
        "analyze",
        "and",
        "any",
        "array",
        "as",
        "asc",
        "authorization",
        "between",
        "binary",
        "both",
        "case",
        "cast",
        "check",
        "collate",
        "column",
        "constraint",
        "create",
        "cross",
        "current_catalog",
        "current_date",
        "current_role",
        "current_schema",
        "current_time",
        "current_timestamp",
        "current_user",
        "default",
        "deferrable",
        "desc",
        "distinct",
        "do",
        "else",
        "end",
        "except",
        "false",
        "fetch",
        "for",
        "foreign",
        "freeze",
        "from",
        "full",
        "grant",
        "group",
        "having",
        "ilike",
        "in",
        "initially",
        "inner",
        "intersect",
        "into",
        "is",
        "isnull",
        "join",
        "lateral",
        "leading",
        "left",
        "like",
        "limit",
        "localtime",
        "localtimestamp",
        "natural",
        "not",
        "notnull",
        "null",
        "offset",
        "on",
        "only",
        "or",
        "order",
        "outer",
        "overlaps",
        "placing",
        "primary",
        "references",
        "returning",
        "right",
        "select",
        "session_user",
        "similar",
        "some",
        "symmetric",
        "table",
        "tablesample",
        "then",
        "to",
        "trailing",
        "true",
        "union",
        "unique",
        "user",
        "using",
        "variadic",
        "verbose",
        "when",
        "where",
        "window",
        "with",
    }
)


def needs_quoting(name: str) -> bool:
    """Whether ``name`` must be double-quoted to survive a round trip through SQL."""
    return not _BARE.match(name) or name in _RESERVED


def quote(name: str) -> str:
    """Double-quote ``name`` if it needs it, escaping any embedded quote.

    For printing canonical expressions only. Identifiers sent to the server go through
    ``psycopg.sql.Identifier``, which is the only safe way to build dynamic SQL.
    """
    if not needs_quoting(name):
        return name
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def unquote(token: str) -> str:
    """The identifier a token names, with quoting removed.

    A bare word is lower-cased because PostgreSQL folds unquoted identifiers; a quoted one is
    returned exactly as written.
    """
    if len(token) >= 2 and token.startswith('"') and token.endswith('"'):
        return token[1:-1].replace('""', '"')
    return token.lower()


class SchemaMap:
    """A source's ``schema_map``, and its inverse.

    Some services deploy with a per-environment ``defaultSchemaName``, so the same table lives
    under a different schema name in each environment. Comparison happens in the master's
    namespace, so a target's names are translated into it before diffing.
    """

    __slots__ = ("_forward", "_inverse")

    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self._forward = dict(mapping or {})
        self._inverse = {target: master for master, target in self._forward.items()}

    def __bool__(self) -> bool:
        return bool(self._forward)

    @property
    def forward(self) -> dict[str, str]:
        """Master schema name to target schema name."""
        return dict(self._forward)

    @property
    def inverse(self) -> dict[str, str]:
        """Target schema name to master schema name. What remapping actually uses."""
        return dict(self._inverse)

    def to_master(self, schema: str) -> str:
        """Translate one target schema name into the master's namespace."""
        return self._inverse.get(schema, schema)

    def to_target(self, schema: str) -> str:
        """Translate one master schema name into the target's namespace."""
        return self._forward.get(schema, schema)
