"""Comparing a report against an approved one.

The single most useful thing for adopting this tool. A first run against environments that have
drifted for years produces hundreds of findings, all true and none of them actionable today.
Rejecting that report is the natural response, and then nobody runs it again.

``--baseline`` accepts a previously-approved report and fails only on findings that were not in it.
The backlog stays visible and stops blocking, and anything *new* is caught from day one.

A finding is matched to the baseline by target, object, status and delta values — but never by
severity. So a column that was already the wrong type stays accepted, while that same column
changing type *again* is new; and re-grading an attribute later cannot silently un-accept a backlog.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .diff.model import ComparisonReport, Note, NoteKind, ObjectFinding, TargetDiff
from .diff.severity import Severity

#: What makes two findings "the same finding" across runs.
Signature = tuple[str, str, str, str, str, tuple[tuple[str, str | None, str | None], ...]]


def signature(report_name: str, target_label: str, finding: ObjectFinding) -> Signature:
    """A finding's identity for baseline purposes.

    Scoped by report name as well as target, because ``--config`` can name a directory and each
    service database produces its own report. Two services can both have a target called ``qa``
    and a table called ``public.audit``; without the report name, one service's approved backlog
    would silently accept the other's findings.

    Includes the delta values, so a column that was already ``int4`` where the master has ``int8``
    stays accepted, while that same column changing to ``text`` is a new finding. Excludes severity,
    so re-grading an attribute later does not silently un-accept a whole backlog.
    """
    deltas = tuple(
        (delta.attribute, delta.master_value, delta.target_value) for delta in finding.deltas
    )
    return (
        report_name,
        target_label,
        finding.key.kind.value,
        finding.key.path,
        finding.status.value,
        deltas,
    )


@dataclass(frozen=True, slots=True)
class BaselineResult:
    """A report with its known findings set aside."""

    report: ComparisonReport
    accepted: int
    """Findings that were in the baseline and are no longer gating."""
    new: int
    """Findings that were not in the baseline."""
    resolved: int
    """Findings in the baseline that no longer occur. Worth knowing: the baseline can shrink."""


def apply_baseline(report: ComparisonReport, baseline: ComparisonReport) -> BaselineResult:
    """Suppress every finding the baseline already contains.

    Accepted findings move to ``TargetDiff.ignored`` — the same place ignore rules put theirs — so
    they stay in the JSON report and appear as skipped cases in JUnit. A baseline that made findings
    vanish would be indistinguishable from a broken comparer.
    """
    known = {
        signature(baseline.name, target.target_label, finding)
        for target in baseline.targets
        for finding in (*target.findings, *target.ignored)
    }
    seen: set[Signature] = set()

    targets: list[TargetDiff] = []
    accepted = 0
    new = 0

    for target in report.targets:
        kept: list[ObjectFinding] = []
        ignored = list(target.ignored)
        for finding in target.findings:
            key = signature(report.name, target.target_label, finding)
            seen.add(key)
            if key in known:
                accepted += 1
                ignored.append(replace(finding, ignored_by="baseline"))
                continue
            new += 1
            kept.append(finding)
        targets.append(replace(target, findings=tuple(kept), ignored=tuple(ignored)))

    resolved = len(known - seen)
    notes = list(report.notes)
    notes.append(
        Note(
            kind=NoteKind.BASELINE_APPLIED,
            message=_summary(accepted, new, resolved),
            severity=Severity.INFO,
        )
    )

    return BaselineResult(
        report=replace(report, targets=tuple(targets), notes=tuple(notes)),
        accepted=accepted,
        new=new,
        resolved=resolved,
    )


def _summary(accepted: int, new: int, resolved: int) -> str:
    parts = [f"{accepted} finding(s) accepted by the baseline"]
    if new:
        parts.append(f"{new} new")
    if resolved:
        # Worth saying: the baseline can be regenerated smaller, and should be.
        parts.append(f"{resolved} baseline finding(s) no longer occur and can be dropped from it")
    return "; ".join(parts)
