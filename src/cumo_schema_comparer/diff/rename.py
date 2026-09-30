"""Reconciling renamed objects.

PostgreSQL generates names for indexes, constraints and triggers. Create the same unique
constraint through two slightly different migration paths and you get ``t_col_key`` in one
database and ``t_col_key1`` in the other — the same structure under two names.

Set-difference diffing reports that as two findings: one missing, one extra. Both are wrong. So
before ignores are applied, structurally identical pairs are matched up and reported once, as a
name difference.

Only the auto-named kinds take part. A renamed *table* is genuinely a missing table plus an
extra one, because nothing generated that name.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from ..model.keys import ObjectKey
from ..model.kinds import AUTO_NAMED_KINDS
from .model import AttributeDelta, ObjectFinding, ObjectStatus
from .severity import Severity


def reconcile_renames(
    findings: list[ObjectFinding],
    master_objects: Mapping[ObjectKey, Any],
    target_objects: Mapping[ObjectKey, Any],
) -> list[ObjectFinding]:
    """Replace missing/extra pairs of identically-structured objects with one name difference."""
    missing: dict[ObjectKey, ObjectFinding] = {}
    extra: dict[ObjectKey, ObjectFinding] = {}
    others: list[ObjectFinding] = []

    for finding in findings:
        if finding.key.kind not in AUTO_NAMED_KINDS:
            others.append(finding)
        elif finding.status is ObjectStatus.MISSING_IN_TARGET:
            missing[finding.key] = finding
        elif finding.status is ObjectStatus.EXTRA_IN_TARGET:
            extra[finding.key] = finding
        else:
            others.append(finding)

    if not missing or not extra:
        return others + list(missing.values()) + list(extra.values())

    # Fingerprint the candidates on the target side, then look each master-only object up.
    # Sorted iteration keeps the pairing deterministic when two objects share a fingerprint.
    by_fingerprint: dict[tuple[Any, ...], list[ObjectKey]] = {}
    for key in sorted(extra, key=lambda k: k.sort_key):
        by_fingerprint.setdefault(_fingerprint(key, target_objects[key]), []).append(key)

    paired: list[ObjectFinding] = []
    for key in sorted(missing, key=lambda k: k.sort_key):
        fingerprint = _fingerprint(key, master_objects[key])
        candidates = by_fingerprint.get(fingerprint)
        if not candidates:
            paired.append(missing[key])
            continue
        partner = candidates.pop(0)
        del extra[partner]
        paired.append(
            replace(
                missing[key],
                status=ObjectStatus.DIFFERS,
                severity=Severity.WARNING,
                paired_with=partner,
                deltas=(
                    AttributeDelta(
                        attribute=f"{key.kind.value}.name",
                        master_value=key.name,
                        target_value=partner.name,
                        severity=Severity.WARNING,
                        note="same structure under a different name; PostgreSQL generates these "
                        "names, so this is usually harmless",
                    ),
                ),
            )
        )

    return others + paired + list(extra.values())


def _fingerprint(key: ObjectKey, obj: Any) -> tuple[Any, ...]:
    """Everything about an object except the name PostgreSQL generated for it.

    Two objects with the same fingerprint are the same object under two names.

    Which part of the key is the generated name depends on the kind, and getting it wrong is
    silently wrong rather than loudly wrong:

    * an index's key is ``(schema, index_name)``, so its ``name`` is the generated part and its
      table is a compared field;
    * a constraint's and a trigger's key is ``(schema, table, own_name)``, so ``name`` is the
      table — structural, and it must stay in the fingerprint — while ``subname`` is the generated
      part. Dropping the table here would let a constraint on one table pair with an identically
      shaped constraint on another.
    """
    from dataclasses import fields as dataclass_fields

    parent = key.name if key.subname is not None else None
    values: list[Any] = [key.kind.value, key.schema, parent]
    for f in sorted(dataclass_fields(obj), key=lambda f: f.name):
        if f.name in ("key", "raw") or not f.compare:
            continue
        values.append(getattr(obj, f.name))
    return tuple(values)
