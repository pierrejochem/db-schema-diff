"""Which attributes are compared, and how much each one matters.

Declarative on purpose. Adding an object kind means adding a list here, not extending the diff
engine, so the engine stays one readable function and every severity decision is visible in one
place next to the attribute it applies to.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..model.kinds import ObjectKind
from .severity import Severity


@dataclass(frozen=True, slots=True)
class AttributeSpec:
    """One comparable attribute of one object kind."""

    name: str
    getter: Callable[[Any], Any]
    severity: Severity
    note: str | None = None
    """Why this matters, when the attribute name is not self-explanatory."""
    body: bool = False
    """Whether this is a printed *definition*.

    Those are re-printed by the server from its parse tree, and the formatting changes between
    major releases, so they are downgraded to INFO when the two servers are different majors.
    """
    display_getter: Callable[[Any], Any] | None = None
    """An alternative value to *show* when this attribute differs.

    The comparison always uses :attr:`getter`. A routine is compared by its body hash — stable,
    cheap, and independent of how the server prints the body — but a reader needs the text, so the
    text is carried alongside for display when both sides have one. Nothing about equality or
    identity changes.

    Every ``body`` spec has one, because none of their compared values is readable text: the
    canonicaliser joins the tokens of a definition with single spaces, so a 400-line view body
    compares as one line and a diff of it could only ever show one removed and one added line.
    See :func:`_raw_display`.
    """

    @property
    def qualified(self) -> str:
        """``kind.attribute``, the string an ignore rule matches."""
        return self.name

    def render(self, value: Any) -> str | None:
        """Display form of one value."""
        if value is None:
            return None
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, tuple):
            return ", ".join(str(v) for v in value) if value else None
        return str(value)


def _attrgetter(attribute: str) -> Callable[[Any], Any]:
    """Read one attribute by name.

    A named function rather than an inline lambda so the closure binds ``attribute`` explicitly
    and the type is inferable.
    """

    def get(obj: Any) -> Any:
        return getattr(obj, attribute)

    return get


def _raw_display(field: str, fallback: Callable[[Any], Any]) -> Callable[[Any], Any]:
    """Read the server's own text for ``field``, falling back to the compared value.

    This is what makes a rendered diff of a definition worth reading. The compared value is
    canonical — the tokenizer's output joined by single spaces — which is exactly right for
    deciding equality and useless to show: it holds no newline, so every multi-line body would
    render as one removed and one added line however long it is.

    ``raw`` is the text the server printed (``pg_get_viewdef``, ``prosrc``), line breaks and all.
    It is masked by :func:`db_schema_comparer.build.redact_inventory` alongside the canonical
    value — every ``raw`` entry is, unconditionally — so showing it reopens no credential path.

    An inventory captured before a given raw value was recorded has none; then the canonical value
    is shown, which is what every reader saw before this existed.
    """

    def display(obj: Any) -> Any:
        raw = getattr(obj, "raw", None)
        text = raw.get(field) if raw is not None else None
        return fallback(obj) if text is None else text

    return display


def _spec(
    kind: ObjectKind,
    attribute: str,
    severity: Severity,
    *,
    note: str | None = None,
    body: bool = False,
    getter: Callable[[Any], Any] | None = None,
    display_field: str | None = None,
) -> AttributeSpec:
    """One spec. ``display_field`` names the model field holding the text, when it is not
    ``attribute`` itself — a routine is compared by ``body_hash`` and read as ``body``."""
    compare_with = getter or _attrgetter(attribute)
    display: Callable[[Any], Any] | None = None
    if body:
        field = display_field or attribute
        display = _raw_display(field, _attrgetter(field) if display_field else compare_with)
    return AttributeSpec(
        name=f"{kind.value}.{attribute}",
        getter=compare_with,
        severity=severity,
        note=note,
        body=body,
        display_getter=display,
    )


TABLE_SPECS: tuple[AttributeSpec, ...] = (
    _spec(
        ObjectKind.TABLE,
        "persistence",
        Severity.ERROR,
        note="an unlogged table is not crash-safe; a permanent one is",
    ),
    _spec(ObjectKind.TABLE, "is_partitioned", Severity.ERROR),
    _spec(ObjectKind.TABLE, "partition_key", Severity.ERROR, body=True),
    _spec(ObjectKind.TABLE, "partition_bound", Severity.WARNING, body=True),
    _spec(
        ObjectKind.TABLE,
        "partition_count",
        Severity.INFO,
        note="partitions are created on a schedule, not by a migration, so the count differing "
        "between environments is expected",
    ),
    _spec(ObjectKind.TABLE, "reloptions", Severity.INFO),
)

COLUMN_SPECS: tuple[AttributeSpec, ...] = (
    _spec(ObjectKind.COLUMN, "data_type", Severity.ERROR),
    _spec(ObjectKind.COLUMN, "is_nullable", Severity.ERROR),
    _spec(
        ObjectKind.COLUMN,
        "generated",
        Severity.ERROR,
        note="a stored generated column is not the same as a defaulted one",
        body=True,
    ),
    _spec(
        ObjectKind.COLUMN,
        "autoincrement",
        Severity.WARNING,
        note="serial and GENERATED AS IDENTITY both autoincrement, but they are different objects",
    ),
    _spec(ObjectKind.COLUMN, "default", Severity.WARNING, body=True),
    _spec(
        ObjectKind.COLUMN,
        "ordinal",
        Severity.WARNING,
        note="column order differs; harmless unless something does SELECT * or INSERT without "
        "a column list",
    ),
    _spec(
        ObjectKind.COLUMN,
        "collation",
        Severity.WARNING,
        note="a different collation changes sort order and comparison results",
    ),
)

CONSTRAINT_SPECS: tuple[AttributeSpec, ...] = (
    _spec(
        ObjectKind.CONSTRAINT,
        "contype",
        Severity.ERROR,
        note="a unique constraint and a primary key are not interchangeable",
    ),
    _spec(
        ObjectKind.CONSTRAINT,
        "columns",
        Severity.ERROR,
        note="a constraint over different columns enforces a different rule",
    ),
    _spec(
        ObjectKind.CONSTRAINT,
        "references",
        Severity.ERROR,
        note="the foreign key points somewhere else",
        getter=lambda obj: obj.references,
    ),
    _spec(
        ObjectKind.CONSTRAINT,
        "referential_actions",
        Severity.ERROR,
        note="ON DELETE CASCADE and ON DELETE RESTRICT behave differently the first time a "
        "referenced row is deleted",
        getter=lambda obj: obj.referential_actions,
    ),
    _spec(ObjectKind.CONSTRAINT, "match_type", Severity.WARNING),
    _spec(
        ObjectKind.CONSTRAINT,
        "validated",
        Severity.ERROR,
        note="a NOT VALID constraint is not enforced for existing rows",
    ),
    _spec(
        ObjectKind.CONSTRAINT,
        "expression",
        Severity.ERROR,
        note="the check accepts a different set of values",
        body=True,
    ),
    _spec(
        ObjectKind.CONSTRAINT,
        "deferrable",
        Severity.WARNING,
        note="a deferrable constraint is checked at commit rather than per statement",
    ),
    _spec(ObjectKind.CONSTRAINT, "deferred", Severity.WARNING),
)

INDEX_SPECS: tuple[AttributeSpec, ...] = (
    _spec(
        ObjectKind.INDEX,
        "is_unique",
        Severity.ERROR,
        note="a unique index enforces a constraint; a plain one only speeds queries up",
    ),
    _spec(
        ObjectKind.INDEX,
        "key_columns",
        Severity.ERROR,
        note="a different key, or a different key order, serves different queries",
    ),
    _spec(
        ObjectKind.INDEX,
        "predicate",
        Severity.ERROR,
        note="a partial index only covers the rows matching its condition",
        body=True,
    ),
    _spec(
        ObjectKind.INDEX,
        "nulls_not_distinct",
        Severity.ERROR,
        note="NULLS NOT DISTINCT changes how many NULLs a unique index permits",
    ),
    _spec(
        ObjectKind.INDEX,
        "is_valid",
        Severity.ERROR,
        note="an invalid index is ignored by the planner; a CREATE INDEX CONCURRENTLY failed",
    ),
    _spec(
        ObjectKind.INDEX,
        "access_method",
        Severity.WARNING,
        note="btree and gin answer different kinds of query",
    ),
    _spec(
        ObjectKind.INDEX,
        "table",
        Severity.WARNING,
        note="the index is on a different table",
    ),
    _spec(
        ObjectKind.INDEX,
        "included_columns",
        Severity.INFO,
        note="INCLUDE columns allow an index-only scan but do not change what the index matches",
        getter=lambda obj: obj.included_columns,
    ),
    _spec(ObjectKind.INDEX, "reloptions", Severity.INFO),
)


def _view_specs(kind: ObjectKind) -> tuple[AttributeSpec, ...]:
    return (
        _spec(
            kind,
            "columns",
            Severity.ERROR,
            note="a consumer breaks when a column disappears, however the query was rewritten",
        ),
        _spec(
            kind,
            "definition",
            Severity.WARNING,
            note="the query behind the view differs",
            body=True,
        ),
        _spec(kind, "is_materialized", Severity.ERROR),
        _spec(kind, "reloptions", Severity.INFO),
    )


SEQUENCE_SPECS: tuple[AttributeSpec, ...] = (
    _spec(ObjectKind.SEQUENCE, "data_type", Severity.ERROR),
    _spec(
        ObjectKind.SEQUENCE,
        "increment",
        Severity.ERROR,
        note="a different increment produces a different series of values",
    ),
    _spec(
        ObjectKind.SEQUENCE,
        "max_value",
        Severity.WARNING,
        note="a lower ceiling exhausts sooner",
    ),
    _spec(ObjectKind.SEQUENCE, "min_value", Severity.WARNING),
    _spec(
        ObjectKind.SEQUENCE,
        "cycles",
        Severity.ERROR,
        note="a cycling sequence reissues values that already exist",
    ),
    _spec(ObjectKind.SEQUENCE, "start_value", Severity.INFO),
    _spec(
        ObjectKind.SEQUENCE,
        "cache_size",
        Severity.INFO,
        note="cache size affects gaps after a crash, not correctness",
    ),
)

ROUTINE_SPECS: tuple[AttributeSpec, ...] = (
    _spec(
        ObjectKind.ROUTINE,
        "return_type",
        Severity.ERROR,
        note="callers depend on the return type",
    ),
    _spec(ObjectKind.ROUTINE, "returns_set", Severity.ERROR),
    _spec(ObjectKind.ROUTINE, "language", Severity.ERROR),
    _spec(ObjectKind.ROUTINE, "prokind", Severity.ERROR),
    _spec(
        ObjectKind.ROUTINE,
        "security_definer",
        Severity.ERROR,
        note="SECURITY DEFINER runs with the owner's privileges; a difference here is a privilege "
        "difference",
    ),
    _spec(
        ObjectKind.ROUTINE,
        "config",
        Severity.ERROR,
        note="a routine with a pinned search_path in one environment and not the other resolves "
        "names differently, which is a security difference as well as a behavioural one",
    ),
    _spec(
        ObjectKind.ROUTINE,
        "body_hash",
        Severity.WARNING,
        note="the routine does something different",
        body=True,
        display_field="body",
    ),
    _spec(
        ObjectKind.ROUTINE,
        "strict",
        Severity.WARNING,
        note="a STRICT routine returns NULL instead of running when any argument is NULL",
    ),
    _spec(
        ObjectKind.ROUTINE,
        "volatility",
        Severity.WARNING,
        note="volatility controls how freely the planner may cache the result",
    ),
    _spec(ObjectKind.ROUTINE, "argument_defaults", Severity.WARNING, body=True),
    _spec(ObjectKind.ROUTINE, "parallel_safety", Severity.INFO),
)

TRIGGER_SPECS: tuple[AttributeSpec, ...] = (
    _spec(
        ObjectKind.TRIGGER,
        "enabled",
        Severity.ERROR,
        note="a disabled trigger does nothing, and nothing about its definition says so",
    ),
    _spec(
        ObjectKind.TRIGGER,
        "function",
        Severity.ERROR,
        note="the trigger calls something else",
    ),
    _spec(ObjectKind.TRIGGER, "timing", Severity.ERROR),
    _spec(
        ObjectKind.TRIGGER,
        "events",
        Severity.ERROR,
        note="the trigger fires on a different set of statements",
    ),
    _spec(ObjectKind.TRIGGER, "level", Severity.ERROR),
    _spec(
        ObjectKind.TRIGGER,
        "condition",
        Severity.ERROR,
        note="the WHEN clause selects different rows",
        body=True,
    ),
    _spec(ObjectKind.TRIGGER, "arguments", Severity.WARNING),
    _spec(ObjectKind.TRIGGER, "deferrable", Severity.WARNING),
    _spec(ObjectKind.TRIGGER, "deferred", Severity.WARNING),
)


def _type_specs(kind: ObjectKind) -> tuple[AttributeSpec, ...]:
    return (
        _spec(
            kind,
            "labels",
            Severity.ERROR,
            note="an enum's label order defines its comparison operators, so a different order is "
            "a different type",
        ),
        _spec(kind, "base_type", Severity.ERROR),
        _spec(kind, "attributes", Severity.ERROR),
        _spec(
            kind,
            "constraints",
            Severity.ERROR,
            note="the domain accepts a different set of values",
            body=True,
        ),
        _spec(kind, "not_null", Severity.ERROR),
        _spec(kind, "default", Severity.WARNING, body=True),
    )


EXTENSION_SPECS: tuple[AttributeSpec, ...] = (
    _spec(
        ObjectKind.EXTENSION,
        "version",
        Severity.WARNING,
        note="a different extension version can change the behaviour of everything it provides",
    ),
)

#: Every kind's attribute list. A kind absent from here is compared on existence alone.
SPECS: dict[ObjectKind, tuple[AttributeSpec, ...]] = {
    ObjectKind.TABLE: TABLE_SPECS,
    ObjectKind.COLUMN: COLUMN_SPECS,
    ObjectKind.CONSTRAINT: CONSTRAINT_SPECS,
    ObjectKind.INDEX: INDEX_SPECS,
    ObjectKind.VIEW: _view_specs(ObjectKind.VIEW),
    ObjectKind.MATVIEW: _view_specs(ObjectKind.MATVIEW),
    ObjectKind.SEQUENCE: SEQUENCE_SPECS,
    ObjectKind.ROUTINE: ROUTINE_SPECS,
    ObjectKind.TRIGGER: TRIGGER_SPECS,
    ObjectKind.ENUM_TYPE: _type_specs(ObjectKind.ENUM_TYPE),
    ObjectKind.DOMAIN_TYPE: _type_specs(ObjectKind.DOMAIN_TYPE),
    ObjectKind.COMPOSITE_TYPE: _type_specs(ObjectKind.COMPOSITE_TYPE),
    ObjectKind.RANGE_TYPE: _type_specs(ObjectKind.RANGE_TYPE),
    ObjectKind.EXTENSION: EXTENSION_SPECS,
}

#: Attributes not compared unless explicitly enabled, because they differ per environment by
#: design: an environment's databases are owned by that environment's roles.
OPT_IN_ATTRIBUTES: dict[str, str] = {
    "owner": "include_owners",
    "comment": "include_comments",
    "grants": "include_grants",
}


def specs_for(kind: ObjectKind) -> tuple[AttributeSpec, ...]:
    """Attribute specs for one kind, empty when only existence is compared."""
    return SPECS.get(kind, ())
