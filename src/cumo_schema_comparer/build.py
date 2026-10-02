"""Turning raw catalog rows into a canonical :class:`Inventory`.

This is the seam between the two halves of the tool. Introspection produces raw rows and knows
nothing about canonical form; normalisation produces canonical values and knows nothing about
SQL. This module is the only place that sees both, which is why it stays small.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import fields as dataclass_fields
from dataclasses import is_dataclass, replace
from datetime import UTC, datetime
from typing import Any

import psycopg

from .config.model import SourceRef
from .db.connect import server_features
from .db.introspect import Introspector
from .diff.attributes import SPECS
from .model.changelog import ChangelogLocation, ChangelogState
from .model.inventory import Inventory, SourceInfo
from .model.keys import ObjectKey, column_key, table_key
from .model.kinds import ObjectKind
from .model.objects import (
    TRIGGER_BEFORE,
    TRIGGER_DELETE,
    TRIGGER_INSERT,
    TRIGGER_INSTEAD,
    TRIGGER_ROW,
    TRIGGER_TRUNCATE,
    TRIGGER_UPDATE,
    Column,
    Constraint,
    Extension,
    Index,
    IndexKey,
    RawValues,
    Routine,
    Sequence,
    Table,
    Trigger,
    UserType,
    View,
)
from .normalize.defaults import canonical_default
from .normalize.expressions import canonical_expr
from .normalize.redact import mask_literals
from .normalize.routines import body_hash, canonical_body
from .normalize.types import canonical_type

log = logging.getLogger(__name__)


def build_inventory(
    connection: psycopg.Connection[dict[str, Any]],
    source: SourceRef,
    *,
    exclude_schemas: tuple[str, ...] = (),
    skip_liquibase: bool = False,
    redact_literals: bool = True,
) -> Inventory:
    """Capture one database as a canonical inventory.

    With ``redact_literals`` (the default) credential-shaped literals in definition text are masked
    here, as the inventory is built, so they never enter the in-memory model and every consumer
    inherits that. See :func:`redact_inventory`.
    """
    features = server_features(connection)
    introspector = Introspector(connection, features)

    info = _source_info(introspector.server_info(), source)
    schemas = introspector.schemas(exclude=exclude_schemas, only=source.schemas)
    log.info("%s: inventorying %d schema(s)", source.label, len(schemas))

    objects: dict[ObjectKey, Any] = {}
    if schemas:
        objects.update(_tables(introspector.relations(schemas)))
        objects.update(_columns(introspector.columns(schemas)))
        objects.update(_constraints(introspector.constraints(schemas)))
        objects.update(_indexes(introspector.indexes(schemas)))
        objects.update(_views(introspector.views(schemas)))
        objects.update(_sequences(introspector.sequences(schemas)))
        objects.update(_routines(introspector.routines(schemas)))
        objects.update(_triggers(introspector.triggers(schemas)))
        objects.update(_types(introspector.types(schemas)))
        objects.update(_extensions(introspector.extensions()))

    changelog = None if skip_liquibase else _changelog(introspector, source)

    inventory = Inventory(source=info, schemas=tuple(schemas), objects=objects, changelog=changelog)
    return redact_inventory(inventory) if redact_literals else inventory


#: Specs whose compared attribute is not itself the text that travels. ``routine.body_hash`` is a
#: hash; the body text that reaches a report is carried in :attr:`Routine.body` (and
#: ``raw["body"]``), so that is the field to mask. Every other body spec names its own field.
_BODY_TEXT_FIELDS: Mapping[str, tuple[str, ...]] = {"routine.body_hash": ("body",)}

#: Definition-bearing text that no ``body`` spec covers but that can still hold a literal: the
#: argument list shown for a routine (with its defaults) and the literal arguments of a trigger.
#: Kept explicit and pinned by a test, like the derived set.
_EXTRA_TEXT_FIELDS: Mapping[ObjectKind, tuple[str, ...]] = {
    ObjectKind.ROUTINE: ("arguments", "config"),
    ObjectKind.TRIGGER: ("arguments",),
    # ``keys`` holds IndexKey objects whose ``expression`` is parsed from the same text as
    # ``predicate``; masking one and not the other would print the secret one slot later.
    ObjectKind.INDEX: ("keys",),
}


def masked_fields() -> dict[ObjectKind, tuple[str, ...]]:
    """Which model fields carry definition text, per object kind.

    Derived from the ``body=True`` attribute specs rather than listed by hand, so a new body
    attribute is covered automatically.
    """
    out: dict[ObjectKind, list[str]] = {}
    for kind, specs in SPECS.items():
        for spec in specs:
            if not spec.body:
                continue
            attribute = spec.name.split(".", 1)[1]
            for name in _BODY_TEXT_FIELDS.get(spec.name, (attribute,)):
                out.setdefault(kind, []).append(name)
    for kind, names in _EXTRA_TEXT_FIELDS.items():
        out.setdefault(kind, []).extend(names)
    return {kind: tuple(names) for kind, names in out.items()}


def _mask_value(value: Any) -> Any:
    if isinstance(value, str):
        return mask_literals(value)
    if isinstance(value, tuple):
        return tuple(_mask_value(item) for item in value)
    if is_dataclass(value) and not isinstance(value, type):
        # Generic rather than a per-class case (IndexKey today): any structured value that holds
        # text is masked field by field, so a new one cannot silently pass through.
        changes = {f.name: _mask_value(getattr(value, f.name)) for f in dataclass_fields(value)}
        return replace(value, **{k: v for k, v in changes.items() if v != getattr(value, k)})
    return value


#: Fields holding ``name=value`` settings (``proconfig``). The value is stored bare, not as a SQL
#: literal, so ``mask_literals`` would see no literal in it at all.
_SETTING_FIELDS = frozenset({"config"})


def _mask_setting(entry: str) -> str:
    """Mask the value of one ``name=value`` setting by judging it as the literal it was set as."""
    name, sep, value = entry.partition("=")
    if not sep:
        return entry
    masked = mask_literals(f"{name} = '{value.replace(chr(39), chr(39) * 2)}'") or ""
    prefix = f"{name} = '"
    if not (masked.startswith(prefix) and masked.endswith("'")):
        return entry
    return f"{name}={masked[len(prefix) : -1].replace(chr(39) * 2, chr(39))}"


def _redact_object(obj: Any, fields: tuple[str, ...]) -> Any:
    changes: dict[str, Any] = {}
    for name in fields:
        current = getattr(obj, name)
        if name in _SETTING_FIELDS:
            masked = tuple(_mask_setting(e) for e in current)
        else:
            masked = _mask_value(current)
        if masked != current:
            changes[name] = masked
    # Every raw value is pre-normalisation server text kept for display; whichever definition it
    # holds, it is the same secret in a second place.
    raw = obj.raw.values
    masked_raw = {name: _mask_value(text) for name, text in raw.items()}
    if masked_raw != raw:
        changes["raw"] = RawValues(masked_raw)
    return replace(obj, **changes) if changes else obj


def redact_inventory(inventory: Inventory) -> Inventory:
    """A copy of ``inventory`` with credential-shaped literals in definition text masked.

    Touches only the fields from :func:`masked_fields` and each object's ``raw`` display text;
    identifiers, types and flags are never rewritten. Idempotent. ``Routine.body_hash`` is left as
    computed from the real body: it is a digest, not text, and keeping it means redaction never
    changes which routines compare as different.
    """
    fields = masked_fields()
    objects = {
        key: _redact_object(obj, fields.get(key.kind, ())) for key, obj in inventory.objects.items()
    }
    return replace(inventory, objects=objects)


def _changelog(introspector: Introspector, source: SourceRef) -> ChangelogState:
    """Locate and read this database's Liquibase changelog.

    Located rather than assumed: ``cumo-invoicing`` sets
    ``spring.liquibase.liquibase-schema=cumo-invoicing``, so its table lives in a hyphenated schema
    of its own and is not in ``public`` at all.

    Several candidates with no configured override is reported as ambiguous rather than guessed at.
    Picking one arbitrarily would make the answer depend on catalog ordering, which is exactly the
    kind of non-determinism that makes a report untrustworthy.
    """
    if source.liquibase is not None:
        configured = ChangelogLocation(
            schema=source.liquibase.schema_name, table=source.liquibase.table
        )
        log.debug("%s: using configured changelog %s", source.label, configured.qualified)
        return introspector.changelog_state(configured)

    candidates = introspector.locate_changelog()
    if not candidates:
        return ChangelogState(location=None)
    if len(candidates) > 1:
        log.warning(
            "%s: %d changelog tables found; set liquibase.schema to choose one",
            source.label,
            len(candidates),
        )
        return ChangelogState(location=None, candidates=tuple(candidates))
    return introspector.changelog_state(candidates[0])


def _source_info(row: dict[str, Any], source: SourceRef) -> SourceInfo:
    """Identity of what was captured. Never contains a credential."""
    return SourceInfo(
        label=source.label,
        host=source.host or _text(row.get("server_addr")),
        database=str(row["database"]),
        user=str(row["user"]),
        server_version_num=int(row["server_version_num"]),
        server_version=str(row["server_version"]),
        encoding=str(row["encoding"]),
        datcollate=str(row["datcollate"]),
        datctype=str(row["datctype"]),
        captured_at=datetime.now(UTC),
    )


def _tables(rows: list[dict[str, Any]]) -> dict[ObjectKey, Table]:
    """Tables, with leaf partitions folded into a count on their parent.

    A leaf is not inventoried in its own right. Partitions are created by a maintenance job on a
    schedule rather than by a migration, so production holding sixty monthly partitions where
    development holds two is expected — and listing each one would put fifty-eight findings in the
    report and bury whatever is actually wrong. The parent carries the count instead.
    """
    counts: dict[ObjectKey, int] = {}
    for row in rows:
        parent = _text(row.get("partition_parent"))
        if parent:
            parent_key = table_key(str(row["schema"]), parent)
            counts[parent_key] = counts.get(parent_key, 0) + 1

    out: dict[ObjectKey, Table] = {}
    for row in rows:
        if row.get("partition_parent"):
            continue
        key = table_key(str(row["schema"]), str(row["name"]))
        partitioned = bool(row["is_partitioned"])
        out[key] = Table(
            key=key,
            persistence=str(row["persistence"]),
            is_partitioned=partitioned,
            is_partition=bool(row["is_partition"]),
            partition_key=canonical_expr(_text(row.get("partition_key"))),
            partition_bound=canonical_expr(_text(row.get("partition_bound"))),
            partition_count=counts.get(key, 0) if partitioned else None,
            reloptions=tuple(row.get("reloptions") or ()),
        )
    return out


def _columns(rows: list[dict[str, Any]]) -> dict[ObjectKey, Column]:
    """Canonicalise each column, keeping the server's own text for display.

    Only table and view columns become :class:`Column` objects here; a view's columns are
    captured because a view whose column list changed is a real, and breaking, difference.
    """
    out: dict[ObjectKey, Column] = {}
    for row in rows:
        schema = str(row["schema"])
        relation = str(row["name"])
        name = str(row["subname"])
        key = column_key(schema, relation, name)

        raw_type = str(row["data_type"])
        data_type = canonical_type(raw_type) or raw_type

        raw_default = _text(row.get("default_expr"))
        owned_sequence = _text(row.get("owned_sequence"))
        generated_kind = _text(row.get("generated_kind"))

        # A stored generated column's expression lives in the same catalog slot as a default,
        # but the two are not the same thing and must not be compared as one.
        if generated_kind == "s":
            default = None
            generated = canonical_expr(_text(row.get("generated_expr")), column_type=data_type)
        else:
            default = canonical_default(
                raw_default, column_type=data_type, owned_sequence=owned_sequence
            )
            generated = None

        raw_values = {"data_type": raw_type}
        if raw_default is not None:
            raw_values["default"] = raw_default

        out[key] = Column(
            key=key,
            ordinal=int(row["ordinal"]),
            data_type=data_type,
            is_nullable=bool(row["is_nullable"]),
            default=default,
            identity=_identity(_text(row.get("identity"))),
            generated=generated,
            collation=_text(row.get("collation")),
            sequence_name=owned_sequence,
            raw=RawValues(raw_values),
        )
    return out


def _identity(value: str | None) -> Any:
    """``attidentity`` as a comparable value: ``'a'``, ``'d'`` or ``None``."""
    return value if value in ("a", "d") else None


def _text(value: Any) -> str | None:
    """A catalog value as text, with SQL NULL and the empty string both meaning "absent"."""
    if value is None:
        return None
    text = str(value)
    return text or None


def _constraints(rows: list[dict[str, Any]]) -> dict[ObjectKey, Constraint]:
    """Canonicalise each constraint.

    A CHECK or EXCLUDE expression goes through the same canonicalisation as a column default: the
    server re-prints it with added parentheses and casts, so comparing the printed text reports
    drift between two databases built from the same migration.
    """
    out: dict[ObjectKey, Constraint] = {}
    for row in rows:
        schema = str(row["schema"])
        table = str(row["name"])
        name = str(row["subname"])
        key = ObjectKey(ObjectKind.CONSTRAINT, schema, table, name)

        definition = _text(row.get("definition"))
        contype = str(row["contype"])
        out[key] = Constraint(
            key=key,
            contype=contype,
            columns=tuple(row.get("columns") or ()),
            references_schema=_text(row.get("references_schema")),
            references_name=_text(row.get("references_name")),
            references_columns=tuple(row.get("references_columns") or ()),
            on_update=_text(row.get("on_update")),
            on_delete=_text(row.get("on_delete")),
            match_type=_text(row.get("match_type")),
            deferrable=bool(row.get("deferrable")),
            deferred=bool(row.get("deferred")),
            validated=bool(row.get("validated", True)),
            expression=_constraint_expression(contype, definition),
            backing_index=_text(row.get("backing_index")),
            raw=RawValues({"definition": definition} if definition else {}),
        )
    return out


def _constraint_expression(contype: str, definition: str | None) -> str | None:
    """The canonical expression of a CHECK or EXCLUDE constraint.

    Extracted from ``pg_get_constraintdef`` rather than queried separately: ``conbin`` is an
    internal node tree, and the printed form is what the server itself considers the expression.
    Only the part inside the outermost ``CHECK (...)`` matters; the keyword carries no information.
    """
    if contype not in ("c", "x") or definition is None:
        return None
    body = definition
    prefix = "CHECK "
    if body.startswith(prefix):
        body = body[len(prefix) :]
        if body.endswith(" NOT VALID"):
            body = body[: -len(" NOT VALID")]
    return canonical_expr(body)


def _indexes(rows: list[dict[str, Any]]) -> dict[ObjectKey, Index]:
    """Group the per-key index rows into one object each.

    The query returns one row per index key so it stays a single round trip; reassembling them here
    is the price of that, and a cheap one.
    """
    grouped: dict[ObjectKey, list[dict[str, Any]]] = {}
    for row in rows:
        key = ObjectKey(ObjectKind.INDEX, str(row["schema"]), str(row["name"]))
        grouped.setdefault(key, []).append(row)

    out: dict[ObjectKey, Index] = {}
    for key, key_rows in grouped.items():
        ordered = sorted(key_rows, key=lambda r: int(r["key_ordinal"]))
        head = ordered[0]
        table = str(head["table_name"])
        definition = _text(head.get("definition"))
        out[key] = Index(
            key=key,
            table=table,
            access_method=str(head["access_method"]),
            is_unique=bool(head["is_unique"]),
            keys=tuple(_index_key(row) for row in ordered),
            predicate=canonical_expr(_text(head.get("predicate"))),
            nulls_not_distinct=bool(head.get("nulls_not_distinct")),
            is_valid=bool(head.get("is_valid", True)) and bool(head.get("is_ready", True)),
            reloptions=tuple(head.get("reloptions") or ()),
            raw=RawValues({"definition": definition} if definition else {}),
        )
    return out


def _index_key(row: dict[str, Any]) -> IndexKey:
    """One index key, with its expression canonicalised.

    A plain column key is left alone; an expression key goes through the canonicaliser, because
    ``pg_get_indexdef`` prints it with the parentheses and casts the parse tree implies rather than
    the ones the author wrote.
    """
    expression = str(row["key_expression"])
    is_expression = bool(row["key_is_expression"])
    if is_expression:
        expression = canonical_expr(expression) or expression

    # An operator class is only worth comparing when it is not the type's default: a non-default
    # one changes which queries the index can serve.
    opclass = None if row.get("opclass_is_default") else _text(row.get("opclass"))

    return IndexKey(
        expression=expression,
        is_expression=is_expression,
        opclass=opclass,
        collation=_text(row.get("key_collation")),
        descending=bool(row.get("descending")),
        nulls_first=bool(row.get("nulls_first")),
        included=not bool(row.get("is_key_column", True)),
    )


def _views(rows: list[dict[str, Any]]) -> dict[ObjectKey, View]:
    """Views and materialized views, as separate kinds.

    Separate because they behave differently: a matview holds data and has to be refreshed, so one
    turning into the other is a real change rather than a rename.
    """
    out: dict[ObjectKey, View] = {}
    for row in rows:
        materialized = bool(row["is_materialized"])
        kind = ObjectKind.MATVIEW if materialized else ObjectKind.VIEW
        key = ObjectKey(kind, str(row["schema"]), str(row["name"]))
        definition = _text(row.get("definition"))
        out[key] = View(
            key=key,
            is_materialized=materialized,
            columns=tuple(row.get("columns") or ()),
            definition=canonical_body(definition),
            reloptions=tuple(row.get("reloptions") or ()),
            raw=RawValues({"definition": definition} if definition else {}),
        )
    return out


def _sequences(rows: list[dict[str, Any]]) -> dict[ObjectKey, Sequence]:
    out: dict[ObjectKey, Sequence] = {}
    for row in rows:
        key = ObjectKey(ObjectKind.SEQUENCE, str(row["schema"]), str(row["name"]))
        raw_type = str(row["data_type"])
        out[key] = Sequence(
            key=key,
            data_type=canonical_type(raw_type) or raw_type,
            start_value=int(row["start_value"]),
            increment=int(row["increment"]),
            min_value=_int_or_none(row.get("min_value")),
            max_value=_int_or_none(row.get("max_value")),
            cache_size=int(row["cache_size"]),
            cycles=bool(row["cycles"]),
            owned_by=_text(row.get("owned_by")),
            raw=RawValues({"data_type": raw_type}),
        )
    return out


def _routines(rows: list[dict[str, Any]]) -> dict[ObjectKey, Routine]:
    """Functions and procedures, keyed by name *and* argument list.

    The identity arguments are part of the key because overloads are distinct objects that share a
    name; without them two overloads collapse into one and each reports the other's body as drift.
    """
    out: dict[ObjectKey, Routine] = {}
    for row in rows:
        identity_args = _canonical_identity_arguments(_text(row.get("identity_arguments")))
        name = f"{row['name']}({identity_args})"
        key = ObjectKey(ObjectKind.ROUTINE, str(row["schema"]), name)

        # A standard-body SQL function (PostgreSQL 14+) leaves prosrc empty and keeps its body in
        # prosqlbody instead, so both have to be considered.
        body = _text(row.get("body")) or _text(row.get("sql_body"))
        return_type = _text(row.get("return_type"))

        out[key] = Routine(
            key=key,
            prokind=str(row.get("prokind") or "f"),
            language=str(row["language"]),
            return_type=canonical_type(return_type) if return_type else None,
            returns_set=bool(row.get("returns_set")),
            arguments=_text(row.get("arguments")),
            body=canonical_body(body),
            body_hash=body_hash(body),
            volatility=str(row.get("volatility") or "v"),
            strict=bool(row.get("strict")),
            security_definer=bool(row.get("security_definer")),
            parallel_safety=str(row.get("parallel_safety") or "u"),
            argument_defaults=canonical_expr(_text(row.get("argument_defaults"))),
            config=tuple(row.get("config") or ()),
            raw=RawValues({"body": body} if body else {}),
        )
    return out


#: A leading ``IN`` on an argument, which is the default mode and carries no information.
_EXPLICIT_IN = re.compile(r"(^|,\s*)IN\s+")


def _canonical_identity_arguments(raw: str | None) -> str:
    """The argument list that identifies a routine, spelled the same on every server version.

    PostgreSQL 13 prints a procedure's arguments as ``p_id integer`` and 15 prints
    ``IN p_id integer`` — the same procedure, two spellings. Since the argument list is part of the
    routine's key, leaving that alone makes a 13-against-15 comparison report *every* procedure as
    both missing and extra. Found by running the suite across the version matrix, not by reading the
    release notes.

    Only a bare ``IN`` is dropped: it is the default. ``INOUT`` and ``VARIADIC`` change what a
    caller must pass, so they stay — and the pattern requires whitespace after ``IN``, so ``INOUT``
    is untouched.
    """
    if not raw:
        return ""
    return _EXPLICIT_IN.sub(r"\1", raw)


def _triggers(rows: list[dict[str, Any]]) -> dict[ObjectKey, Trigger]:
    out: dict[ObjectKey, Trigger] = {}
    for row in rows:
        key = ObjectKey(
            ObjectKind.TRIGGER, str(row["schema"]), str(row["name"]), str(row["subname"])
        )
        tgtype = int(row["tgtype"])
        definition = _text(row.get("definition"))
        out[key] = Trigger(
            key=key,
            function=_text(row.get("function_name")),
            timing=_trigger_timing(tgtype),
            events=_trigger_events(tgtype),
            level="ROW" if tgtype & TRIGGER_ROW else "STATEMENT",
            enabled=str(row.get("enabled") or "O"),
            condition=(
                canonical_expr(_trigger_condition(definition)) if row.get("has_condition") else None
            ),
            arguments=_trigger_arguments(definition),
            deferrable=bool(row.get("deferrable")),
            deferred=bool(row.get("deferred")),
            raw=RawValues({"definition": definition} if definition else {}),
        )
    return out


def _trigger_timing(tgtype: int) -> str:
    if tgtype & TRIGGER_INSTEAD:
        return "INSTEAD OF"
    return "BEFORE" if tgtype & TRIGGER_BEFORE else "AFTER"


def _trigger_events(tgtype: int) -> tuple[str, ...]:
    """The events in a fixed order.

    Fixed so that ``AFTER INSERT OR UPDATE`` and ``AFTER UPDATE OR INSERT`` — the same trigger,
    written two ways — compare equal.
    """
    events = []
    for bit, name in (
        (TRIGGER_INSERT, "INSERT"),
        (TRIGGER_UPDATE, "UPDATE"),
        (TRIGGER_DELETE, "DELETE"),
        (TRIGGER_TRUNCATE, "TRUNCATE"),
    ):
        if tgtype & bit:
            events.append(name)
    return tuple(events)


def _trigger_condition(definition: str | None) -> str | None:
    """The ``WHEN`` clause, read out of the printed trigger definition.

    Read from there because ``pg_get_expr(tgqual, tgrelid)`` cannot produce it: a trigger condition
    references both ``NEW`` and ``OLD``, and that function raises "expression contains variables of
    more than one relation" when given a single relation oid.
    """
    if not definition:
        return None
    marker = " WHEN ("
    start = definition.find(marker)
    if start == -1:
        return None
    # Walk to the parenthesis that closes the clause, so a condition containing its own parentheses
    # survives intact.
    cursor = start + len(marker) - 1
    depth = 0
    for index in range(cursor, len(definition)):
        char = definition[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return definition[cursor : index + 1]
    return None


def _trigger_arguments(definition: str | None) -> tuple[str, ...]:
    """Arguments passed to the trigger function, read out of the printed definition.

    Taken from there rather than decoded from ``tgargs``, which is a null-separated bytea whose
    encoding is fiddly and version-sensitive for no benefit.
    """
    if not definition:
        return ()
    marker = "EXECUTE FUNCTION "
    index = definition.find(marker)
    if index == -1:
        marker = "EXECUTE PROCEDURE "
        index = definition.find(marker)
    if index == -1:
        return ()
    call = definition[index + len(marker) :]
    if "(" not in call or not call.rstrip().endswith(")"):
        return ()
    inner = call[call.index("(") + 1 : call.rstrip().rindex(")")].strip()
    if not inner:
        return ()
    return tuple(part.strip() for part in inner.split(","))


#: ``typtype`` to the kind it is reported as.
_TYPE_KINDS: dict[str, ObjectKind] = {
    "e": ObjectKind.ENUM_TYPE,
    "d": ObjectKind.DOMAIN_TYPE,
    "c": ObjectKind.COMPOSITE_TYPE,
    "r": ObjectKind.RANGE_TYPE,
    "m": ObjectKind.RANGE_TYPE,
}


def _types(rows: list[dict[str, Any]]) -> dict[ObjectKey, UserType]:
    out: dict[ObjectKey, UserType] = {}
    for row in rows:
        typtype = str(row["typtype"])
        kind = _TYPE_KINDS.get(typtype)
        if kind is None:  # pragma: no cover - the query restricts typtype
            continue
        key = ObjectKey(kind, str(row["schema"]), str(row["name"]))
        base_type = _text(row.get("base_type"))
        out[key] = UserType(
            key=key,
            typtype=typtype,
            labels=tuple(row.get("labels") or ()),
            base_type=canonical_type(base_type) if base_type else None,
            not_null=bool(row.get("not_null")),
            default=canonical_expr(_text(row.get("default_value"))),
            constraints=tuple(canonical_expr(c) or c for c in (row.get("constraints") or ())),
            attributes=tuple(row.get("attributes") or ()),
        )
    return out


def _extensions(rows: list[dict[str, Any]]) -> dict[ObjectKey, Extension]:
    out: dict[ObjectKey, Extension] = {}
    for row in rows:
        key = ObjectKey(ObjectKind.EXTENSION, str(row["schema"]), str(row["name"]))
        out[key] = Extension(key=key, version=_text(row.get("version")))
    return out


def _int_or_none(value: Any) -> int | None:
    return None if value is None else int(value)
