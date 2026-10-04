"""Object identity.

An :class:`ObjectKey` is what makes diffing a set operation. Two objects are "the same
object" when their keys are equal, and only then are their attributes compared.

Two identity decisions are load-bearing:

* A routine's ``name`` includes its identity argument list, because overloads are distinct
  objects that happen to share a name.
* An index's, constraint's or trigger's name *is* part of its key, but a name-only
  difference is later reconciled into a single "name differs" finding rather than a
  missing/extra pair, because PostgreSQL generates those names.
"""

from __future__ import annotations

from dataclasses import dataclass

from .kinds import ObjectKind, kind_sort_key


@dataclass(frozen=True, slots=True)
class ObjectKey:
    """Fully qualified identity of one catalog object."""

    kind: ObjectKind
    schema: str
    name: str
    subname: str | None = None
    """Second-level name: a column's name, or a trigger's name on its table."""

    def __post_init__(self) -> None:
        if not self.schema or not self.name:
            raise ValueError("an ObjectKey needs both a schema and a name")

    @property
    def qualified(self) -> str:
        """``schema.name``, without quoting. For display and glob matching."""
        return f"{self.schema}.{self.name}"

    @property
    def path(self) -> str:
        """``schema.name`` or ``schema.name.subname``. The string ignore globs match."""
        return self.qualified if self.subname is None else f"{self.qualified}.{self.subname}"

    def display(self) -> str:
        """Report form, e.g. ``column public.invoice.amount``."""
        return f"{self.kind.label} {self.path}"

    @property
    def sort_key(self) -> tuple[int, str, str, str]:
        """Deterministic ordering: by kind, then schema, then name, then subname.

        Case-sensitive on purpose — PostgreSQL identifiers are, and ``"acme-invoicing"`` and
        ``QRTZ_LOCKS`` are both real names in this platform.
        """
        return (kind_sort_key(self.kind), self.schema, self.name, self.subname or "")

    def parent(self) -> ObjectKey | None:
        """The owning table for a column-like key, otherwise ``None``."""
        if self.subname is None:
            return None
        return ObjectKey(ObjectKind.TABLE, self.schema, self.name)

    def with_schema(self, schema: str) -> ObjectKey:
        """The same object under a different schema name. Used by ``schema_map`` remapping."""
        return ObjectKey(self.kind, schema, self.name, self.subname)


def table_key(schema: str, name: str) -> ObjectKey:
    """Convenience constructor for a table key."""
    return ObjectKey(ObjectKind.TABLE, schema, name)


def column_key(schema: str, table: str, column: str) -> ObjectKey:
    """Convenience constructor for a column key."""
    return ObjectKey(ObjectKind.COLUMN, schema, table, column)
