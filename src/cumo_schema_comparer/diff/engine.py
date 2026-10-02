"""Comparing two inventories.

A pure function: no IO, no clock, no configuration objects. That is what makes the engine
testable against hand-built inventories, and what lets a saved inventory be re-compared later
and produce exactly the same result.

The order of the steps matters, and each one exists to prevent a specific kind of noise:

1. **Remap** the target into the master's schema namespace, so a per-environment
   ``defaultSchemaName`` does not read as "every table is missing".
2. **Short-circuit an empty target**, so a brand-new database produces one finding instead of
   one per object the master has.
3. **Compare** per kind: set difference for existence, then attribute-by-attribute.
4. **Reconcile renames**, so an auto-named index that differs only in name is one "name differs"
   finding rather than a missing/extra pair.
5. **Downgrade definition bodies** when the servers are different PostgreSQL majors, because the
   server re-prints them differently and every view and function would otherwise look changed.
6. **Apply ignores** last, so a rule sees the finding in its final form.
"""

from __future__ import annotations

import logging
from dataclasses import replace

from ..model.inventory import Inventory
from ..model.keys import ObjectKey
from ..model.kinds import KIND_ORDER
from .attributes import AttributeSpec, specs_for
from .changelog import ChangelogOptions, diff_changelog
from .ignores import Action, IgnoreRuleSet
from .model import (
    AttributeDelta,
    DiffOptions,
    Note,
    NoteKind,
    ObjectFinding,
    ObjectStatus,
    TargetDiff,
)
from .rename import reconcile_renames
from .severity import Severity

log = logging.getLogger(__name__)


def diff_inventories(
    master: Inventory,
    target: Inventory,
    *,
    options: DiffOptions | None = None,
    schema_map: dict[str, str] | None = None,
    ignores: IgnoreRuleSet | None = None,
    changelog_options: ChangelogOptions | None = None,
) -> TargetDiff:
    """Compare ``target`` against ``master``.

    ``schema_map`` maps the *target's* schema names to the master's, so the comparison happens
    in one namespace. ``ignores`` suppresses or downgrades findings; it is applied last, so a rule
    always sees a finding in its final form.
    """
    options = options or DiffOptions()
    rules = ignores or IgnoreRuleSet.empty()
    notes: list[Note] = []

    if schema_map:
        target = target.remap_schemas(schema_map)

    options = _adjust_for_sources(master, target, options, notes)

    changelog = diff_changelog(master.changelog, target.changelog, options=changelog_options)

    if target.is_empty and not master.is_empty:
        # One finding, not thousands. A fresh database is a deployment that has not happened,
        # and listing every object it lacks buries that single fact.
        notes.append(
            Note(
                kind=NoteKind.TARGET_EMPTY,
                message=(
                    f"target {target.source.label!r} contains no comparable objects, while "
                    f"{master.source.label!r} has {len(master)}; it looks like nothing has been "
                    "deployed to it"
                ),
                severity=Severity.ERROR,
            )
        )
        return TargetDiff(
            master_label=master.source.label,
            target_label=target.source.label,
            master_source=master.source,
            target_source=target.source,
            changelog=changelog,
            notes=tuple(notes),
        )

    label = target.source.label
    findings: list[ObjectFinding] = []
    for kind in KIND_ORDER:
        master_objects = master.by_kind(kind)
        target_objects = target.by_kind(kind)
        if not master_objects and not target_objects:
            continue
        findings.extend(_diff_kind(master_objects, target_objects, options, rules, label))

    findings = reconcile_renames(findings, master.objects, target.objects)
    reported, ignored = _apply_object_rules(findings, rules, label)

    reported.sort(key=lambda f: (f.key.sort_key, f.status.value))
    ignored.sort(key=lambda f: (f.key.sort_key, f.status.value))

    return TargetDiff(
        master_label=master.source.label,
        target_label=label,
        master_source=master.source,
        target_source=target.source,
        findings=tuple(reported),
        ignored=tuple(ignored),
        changelog=changelog,
        notes=tuple(notes),
    )


def _apply_object_rules(
    findings: list[ObjectFinding], rules: IgnoreRuleSet, target: str
) -> tuple[list[ObjectFinding], list[ObjectFinding]]:
    """Suppress or downgrade whole findings.

    Runs after rename reconciliation, so a rule sees the finding a reader would see rather than the
    missing/extra pair it started as.
    """
    reported: list[ObjectFinding] = []
    ignored: list[ObjectFinding] = []

    for finding in findings:
        decision = rules.for_object(target=target, key=finding.key, status=finding.status)
        if decision.action is Action.IGNORE:
            # Recorded rather than dropped: a suppression nobody can see is indistinguishable
            # from a bug in the comparer.
            ignored.append(replace(finding, ignored_by=decision.rule_id))
            continue
        ceiling = decision.action.clamps_to
        if ceiling is not None and finding.severity > ceiling:
            reported.append(replace(finding, severity=ceiling, ignored_by=decision.rule_id))
            continue
        reported.append(finding)

    return reported, ignored


def _adjust_for_sources(
    master: Inventory, target: Inventory, options: DiffOptions, notes: list[Note]
) -> DiffOptions:
    """Suppress comparisons the two servers cannot meaningfully agree on.

    Both adjustments here turn a guaranteed flood of findings into one sentence.
    """
    adjusted = options

    if master.source.major_version != target.source.major_version:
        # The server re-prints view bodies, check expressions and index expressions from its
        # parse tree, and that printing changes between major releases. Comparing a 15 against a
        # 17 would report every one of them as changed.
        notes.append(
            Note(
                kind=NoteKind.VERSION_SKEW,
                message=(
                    f"{master.source.label} runs PostgreSQL {master.source.major_version} and "
                    f"{target.source.label} runs {target.source.major_version}; printed "
                    "definitions differ by formatting alone, so definition differences are "
                    "reported at INFO only"
                ),
                severity=Severity.WARNING,
            )
        )
        adjusted = replace(adjusted, downgrade_bodies=True)

    if master.source.datcollate != target.source.datcollate:
        # Different database collations mean every text column's collation differs by
        # definition. That is one fact about the databases, not thousands about their columns.
        notes.append(
            Note(
                kind=NoteKind.COLLATION_SKEW,
                message=(
                    f"{master.source.label} uses collation {master.source.datcollate} and "
                    f"{target.source.label} uses {target.source.datcollate}; per-column "
                    "collation is not compared"
                ),
                severity=Severity.WARNING,
            )
        )
        adjusted = replace(
            adjusted, ignored_attributes=adjusted.ignored_attributes | {"column.collation"}
        )

    return adjusted


def _diff_kind(
    master_objects: dict[ObjectKey, object],
    target_objects: dict[ObjectKey, object],
    options: DiffOptions,
    rules: IgnoreRuleSet,
    target: str,
) -> list[ObjectFinding]:
    """Compare every object of one kind."""
    findings: list[ObjectFinding] = []

    for key in master_objects.keys() - target_objects.keys():
        findings.append(
            ObjectFinding(key=key, status=ObjectStatus.MISSING_IN_TARGET, severity=Severity.ERROR)
        )

    for key in target_objects.keys() - master_objects.keys():
        findings.append(
            ObjectFinding(key=key, status=ObjectStatus.EXTRA_IN_TARGET, severity=Severity.WARNING)
        )

    for key in master_objects.keys() & target_objects.keys():
        deltas = _compare_attributes(master_objects[key], target_objects[key], key, options)
        deltas = _apply_attribute_rules(deltas, rules, key, target)
        reportable = deltas if options.show_cosmetic else [d for d in deltas if not d.cosmetic]
        if not reportable:
            # Every difference was either cosmetic or suppressed, so there is nothing to report:
            # the object matches as far as anybody cares.
            continue
        findings.append(
            ObjectFinding(
                key=key,
                status=ObjectStatus.DIFFERS,
                severity=max(d.severity for d in reportable),
                deltas=tuple(reportable),
            )
        )

    return findings


def _apply_attribute_rules(
    deltas: list[AttributeDelta], rules: IgnoreRuleSet, key: ObjectKey, target: str
) -> list[AttributeDelta]:
    """Drop or clamp individual attribute differences.

    Attribute-scoped rules run before object-scoped ones so that a rule can say "a different
    collation is acceptable" without also saying "a missing column is acceptable". If every
    difference on an object is dropped, the object stops being a finding at all.
    """
    if not deltas:
        return deltas

    kept: list[AttributeDelta] = []
    for delta in deltas:
        decision = rules.for_attribute(
            target=target, key=key, status=ObjectStatus.DIFFERS, attribute=delta.attribute
        )
        if decision.action is Action.IGNORE:
            continue
        ceiling = decision.action.clamps_to
        if ceiling is not None and delta.severity > ceiling:
            kept.append(replace(delta, severity=ceiling))
            continue
        kept.append(delta)
    return kept


def _compare_attributes(
    master_object: object, target_object: object, key: ObjectKey, options: DiffOptions
) -> list[AttributeDelta]:
    """Walk one kind's attribute specs."""
    deltas: list[AttributeDelta] = []
    for spec in specs_for(key.kind):
        if spec.name in options.ignored_attributes:
            continue
        if spec.name.endswith(".ordinal") and options.ignore_column_order:
            continue
        delta = _compare_one(spec, master_object, target_object, options)
        if delta is not None:
            deltas.append(delta)
    return deltas


def _compare_one(
    spec: AttributeSpec, master_object: object, target_object: object, options: DiffOptions
) -> AttributeDelta | None:
    """Compare one attribute, or return ``None`` when it matches."""
    master_value = spec.getter(master_object)
    target_value = spec.getter(target_object)

    if master_value == target_value:
        return _cosmetic_delta(spec, master_object, target_object)

    severity = spec.severity
    if spec.body and options.downgrade_bodies:
        severity = Severity.INFO

    shown_master, shown_target = master_value, target_value
    if spec.display_getter is not None:
        display_master = spec.display_getter(master_object)
        display_target = spec.display_getter(target_object)
        # Both or neither: showing text against a hash would read as a difference in kind.
        if display_master is not None and display_target is not None:
            shown_master, shown_target = display_master, display_target

    return AttributeDelta(
        attribute=spec.name,
        master_value=spec.render(shown_master),
        target_value=spec.render(shown_target),
        severity=severity,
        note=spec.note,
    )


def _cosmetic_delta(
    spec: AttributeSpec, master_object: object, target_object: object
) -> AttributeDelta | None:
    """Record that the canonical values matched although the raw text did not.

    Hidden unless ``--show-cosmetic``. Its purpose is to make the normaliser's work visible:
    when someone doubts that two databases really are in sync, this shows exactly what was
    normalised away.
    """
    attribute = spec.name.split(".", 1)[1]
    master_raw = _raw(master_object, attribute)
    target_raw = _raw(target_object, attribute)
    if master_raw is None or target_raw is None or master_raw == target_raw:
        return None
    return AttributeDelta(
        attribute=spec.name,
        master_value=master_raw,
        target_value=target_raw,
        severity=Severity.INFO,
        cosmetic=True,
        note="the server prints these differently; they mean the same thing",
    )


def _raw(obj: object, attribute: str) -> str | None:
    raw = getattr(obj, "raw", None)
    return raw.get(attribute) if raw is not None else None
