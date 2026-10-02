"""Terse constructors for hand-built inventories.

The diff engine is a pure function of two inventories, so most of its tests need no database
at all — they need two small inventories that differ in exactly one way. These helpers keep
that intent readable:

    master = inventory(table("public", "invoice", cols=[col("id", "int4", nullable=False)]))
    target = inventory(table("public", "invoice", cols=[col("id", "int8", nullable=False)]))
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from cumo_schema_comparer.model.changelog import (
    ChangelogLocation,
    ChangelogState,
    ChangeSetRow,
)
from cumo_schema_comparer.model.inventory import Inventory, SourceInfo
from cumo_schema_comparer.model.keys import ObjectKey, column_key, table_key
from cumo_schema_comparer.model.kinds import ObjectKind
from cumo_schema_comparer.model.objects import Column, RawValues, Routine, Table
from cumo_schema_comparer.normalize.routines import body_hash as hash_body
from cumo_schema_comparer.normalize.routines import canonical_body

CAPTURED_AT = datetime(2026, 1, 15, 9, 30, tzinfo=UTC)


def source(
    label: str = "prod",
    *,
    version: int = 150004,
    collate: str = "de_DE.utf8",
    database: str = "invoicing",
    notes: tuple[str, ...] = (),
) -> SourceInfo:
    major = version // 10000
    minor = version % 10000
    return SourceInfo(
        label=label,
        host=f"db-{label}",
        database=database,
        user="cumo",
        server_version_num=version,
        server_version=f"{major}.{minor}",
        encoding="UTF8",
        datcollate=collate,
        datctype=collate,
        captured_at=CAPTURED_AT,
        notes=notes,
    )


def col(
    name: str,
    data_type: str = "text",
    *,
    ordinal: int | None = None,
    nullable: bool = True,
    default: str | None = None,
    identity: str | None = None,
    generated: str | None = None,
    collation: str | None = None,
    sequence_name: str | None = None,
    raw: dict[str, str] | None = None,
) -> dict[str, Any]:
    """A column spec. Bound to its table by :func:`table`.

    ``ordinal`` is normally left unset and assigned from declaration order; pass it
    explicitly only to simulate a column-order difference.
    """
    return {
        "name": name,
        "data_type": data_type,
        "ordinal": ordinal,
        "is_nullable": nullable,
        "default": default,
        "identity": identity,
        "generated": generated,
        "collation": collation,
        "sequence_name": sequence_name,
        "raw": RawValues(dict(raw or {})),
    }


def table(
    schema: str,
    name: str,
    *,
    cols: list[dict[str, Any]] | None = None,
    persistence: str = "p",
    reloptions: tuple[str, ...] = (),
) -> dict[ObjectKey, Any]:
    """A table and its columns, with dense ordinals assigned in declaration order."""
    key = table_key(schema, name)
    objects: dict[ObjectKey, Any] = {
        key: Table(key=key, persistence=persistence, reloptions=reloptions)
    }
    for position, spec in enumerate(cols or [], start=1):
        spec = dict(spec)
        ckey = column_key(schema, name, spec.pop("name"))
        if spec.get("ordinal") is None:
            spec["ordinal"] = position
        objects[ckey] = Column(key=ckey, **spec)
    return objects


_UNSET: Any = object()


def routine(
    schema: str,
    name: str,
    source_text: str | None = None,
    *,
    body: Any = _UNSET,
    body_hash: Any = _UNSET,
    **overrides: Any,
) -> dict[ObjectKey, Any]:
    """A routine built from its source text, the way the inventory builder does it.

    ``body`` and ``body_hash`` default to the canonical form of ``source_text`` and its hash. Pass
    either explicitly (including ``None``) to simulate a version 1 capture, which has no body.
    """
    key = ObjectKey(ObjectKind.ROUTINE, schema, name)
    return {
        key: Routine(
            key=key,
            body=canonical_body(source_text) if body is _UNSET else body,
            body_hash=hash_body(source_text) if body_hash is _UNSET else body_hash,
            **overrides,
        )
    }


def changeset(
    id_: str,
    author: str = "kolowae",
    *,
    order: int = 1,
    filename: str = "liquibase/update.xml",
    md5sum: str | None = "9:abcdef",
    exec_type: str = "EXECUTED",
    tag: str | None = None,
) -> ChangeSetRow:
    return ChangeSetRow(
        id=id_,
        author=author,
        filename=filename,
        order_executed=order,
        exec_type=exec_type,
        md5sum=md5sum,
        tag=tag,
    )


def changelog(
    *rows: ChangeSetRow,
    schema: str = "cumo-invoicing",
    table_name: str = "DATABASECHANGELOG",
    lock_held: bool | None = False,
) -> ChangelogState:
    return ChangelogState(
        location=ChangelogLocation(schema=schema, table=table_name),
        columns_present=frozenset(
            {"ID", "AUTHOR", "FILENAME", "MD5SUM", "ORDEREXECUTED", "EXECTYPE"}
        ),
        rows=rows,
        lock_held=lock_held,
    )


def inventory(
    *object_groups: dict[ObjectKey, Any],
    label: str = "prod",
    schemas: tuple[str, ...] = ("public",),
    version: int = 150004,
    collate: str = "de_DE.utf8",
    changelog_state: ChangelogState | None = None,
) -> Inventory:
    """Merge object groups into one inventory."""
    merged: dict[ObjectKey, Any] = {}
    for group in object_groups:
        merged.update(group)
    derived = tuple(dict.fromkeys(key.schema for key in merged)) or schemas
    return Inventory(
        source=source(label, version=version, collate=collate),
        schemas=derived,
        objects=merged,
        changelog=changelog_state,
    )
