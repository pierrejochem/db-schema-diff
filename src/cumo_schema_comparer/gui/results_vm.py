"""What the Results tab shows once a comparison has finished.

Everything here is derived from a :class:`ComparisonReport` and nothing else, so the window shows
exactly what the JSON, JUnit and HTML reports contain. The verdict follows the same precedence as
the HTML reporter and the console summary: a source that could not be inspected outranks drift,
and drift outranks a clean result. A partial comparison must never read as a clean one.

Clean is never inferred from how many sources were captured. Only the report decides, and it
decides against ``probe_failed`` and against every skipped target.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..diff.changelog import ChangelogDiff, ChangelogStatus
from ..diff.model import ComparisonReport, ObjectFinding, TargetDiff
from ..diff.severity import Severity
from ..model.kinds import KIND_ORDER
from ..report.base import Reporter, render_to_path
from ..report.html import HtmlReporter
from ..report.html import _diff_for as diff_for  # one decision, shared with the HTML report
from ..report.json_report import JsonReporter
from ..report.junit import JUnitReporter
from .errors import GuiError

JSON_NAME = "report.json"
JUNIT_NAME = "junit.xml"
HTML_NAME = "report.html"

#: Same sentence the console and the HTML report use for a partial comparison.
INCOMPLETE = "At least one source could not be inspected, so this comparison is incomplete."

#: Fields of the ``DeltaRow`` and ``DiffLine`` structs in ``ui/results_tab.slint``. A test compares
#: them with the structs, so they cannot drift from the markup.
DELTA_ROW_FIELDS = ("attribute", "master", "target", "severity", "note", "is_body")
DIFF_ROW_FIELDS = ("attribute", "kind", "text")

_SEVERITIES = (Severity.ERROR, Severity.WARNING, Severity.INFO)


@dataclass(frozen=True, slots=True)
class FindingRow:
    """One finding on one target, flattened for a table."""

    target: str
    kind: str
    path: str
    status: str
    severity: str
    detail: str
    suppressed_by: str | None


class ResultsModel:
    """A finished comparison, ready to display and to write out."""

    def __init__(self, report: ComparisonReport) -> None:
        self._report = report

    @property
    def verdict(self) -> tuple[str, str]:
        """``(level, sentence)``; level is ``"ok"`` or a severity label.

        An incomplete comparison is ``"error"`` whatever else it found, whether the report says so
        through ``probe_failed`` or only through a skipped target.
        """
        report = self._report
        skipped = [t.target_label for t in report.targets if t.skipped]
        if report.probe_failed or skipped:
            sentence = INCOMPLETE
            if skipped:
                sentence += f" Not inspected: {', '.join(skipped)}."
            return ("error", sentence)
        worst = report.worst_severity()
        if worst is None:
            return ("ok", f"No differences found across {len(report.targets)} target(s).")
        return (worst.label, f"Drift found at {worst.label} (gate: --fail-on {report.fail_on}).")

    def target_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for target in self._report.targets:
            worst = target.worst_severity()
            rows.append(
                {
                    "label": target.target_label,
                    "skipped": target.skipped,
                    "failed": target.failed or "",
                    "severity": "error" if target.skipped else (worst.label if worst else "ok"),
                    "summary": _target_summary(target),
                    "findings": len(target.findings),
                    "suppressed": len(target.ignored),
                    "has_changelog": _changelog(target) is not None,
                }
            )
        return rows

    def finding_rows(
        self, *, needle: str = "", severities: frozenset[str] | None = None
    ) -> list[FindingRow]:
        """Findings for every target, filtered the way the HTML report filters.

        The needle is a case-insensitive substring of the object path; ``severities`` of ``None``
        means no severity filter, and an empty set means nothing is shown. Severity names are
        matched case-insensitively.
        """
        return [
            _row(target, finding)
            for target, finding in self._filtered(needle=needle, severities=severities)
        ]

    def delta_rows(
        self,
        target: str,
        kind: str,
        path: str,
        *,
        needle: str = "",
        severities: frozenset[str] | None = None,
    ) -> list[dict[str, Any]]:
        """The deltas of the ``kind`` finding at ``path`` on ``target``, as dicts for Slint.

        The finding is named by identity, not position, so a selection cannot go stale: it is
        named by ``(target, kind, path)``, which is unique (a column, a constraint and a trigger can
        share a path) and is exactly the three fields of a :class:`FindingRow`. It is looked up in
        the same filtered list :meth:`finding_rows` builds. A finding that moved still
        resolves to itself; one the filter now hides, or that never existed, yields nothing.
        ``master`` and ``target`` are the compared values; body text belongs in :meth:`diff_rows`.
        """
        finding = self._selected(target, kind, path, needle=needle, severities=severities)
        if finding is None:
            return []
        return [
            {
                "attribute": delta.attribute,
                "master": delta.master_value or "",
                "target": delta.target_value or "",
                "severity": delta.severity.label,
                "note": delta.note or "",
                "is_body": delta.body,
            }
            for delta in finding.deltas
        ]

    def diff_rows(
        self,
        target: str,
        kind: str,
        path: str,
        *,
        needle: str = "",
        severities: frozenset[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Diff lines for the finding's body deltas, each labelled with its delta's attribute.

        Rows are grouped in delta order. Resolution is as in :meth:`delta_rows`.
        """
        finding = self._selected(target, kind, path, needle=needle, severities=severities)
        if finding is None:
            return []
        return [
            {"attribute": delta.attribute, "kind": line.kind.value, "text": line.text}
            for delta in finding.deltas
            for line in diff_for(delta)
        ]

    def _filtered(
        self, *, needle: str, severities: frozenset[str] | None
    ) -> list[tuple[TargetDiff, ObjectFinding]]:
        text = needle.strip().lower()
        wanted = None if severities is None else {name.lower() for name in severities}
        rows: list[tuple[TargetDiff, ObjectFinding]] = []
        for target in self._report.targets:
            for finding in (*_ordered(target.findings), *_ordered(target.ignored)):
                if text and text not in finding.key.path.lower():
                    continue
                if wanted is not None and finding.severity.label not in wanted:
                    continue
                rows.append((target, finding))
        return rows

    def _selected(
        self, target: str, kind: str, path: str, *, needle: str, severities: frozenset[str] | None
    ) -> ObjectFinding | None:
        for candidate, finding in self._filtered(needle=needle, severities=severities):
            if (
                candidate.target_label == target
                and finding.kind.value == kind
                and finding.key.path == path
            ):
                return finding
        return None

    def changelog_lines(self, target: str) -> list[str]:
        """The Liquibase section for one target, headline first."""
        for candidate in self._report.targets:
            if candidate.target_label == target:
                changelog = _changelog(candidate)
                if changelog is None:
                    return []
                return _changelog_lines(candidate, changelog)
        return []

    def write_reports(self, directory: Path) -> list[Path]:
        """Write the JSON, JUnit and HTML reports into ``directory``, all or none.

        Every report is rendered to a ``.partial`` file first and they are moved into place only
        once all three exist, so a failure while rendering never leaves new files beside old ones
        that someone would read as one consistent set.
        """
        plan: list[tuple[Reporter, Path]] = [
            (JsonReporter(), directory / JSON_NAME),
            (JUnitReporter(), directory / JUNIT_NAME),
            (HtmlReporter(), directory / HTML_NAME),
        ]
        partials = [(path, path.with_name(path.name + ".partial")) for _, path in plan]
        try:
            for (reporter, path), (_, partial) in zip(plan, partials, strict=True):
                _render(reporter, self._report, path, partial)
            for path, partial in partials:
                _publish(partial, path)
        finally:
            for _, partial in partials:
                with contextlib.suppress(OSError):
                    partial.unlink()
        return [path for path, _ in partials]

    def html_path(self, directory: Path) -> Path:
        return directory / HTML_NAME


def _publish(partial: Path, final: Path) -> None:
    try:
        os.replace(partial, final)
    except OSError as exc:
        raise GuiError(f"could not write {final.name}: {exc.strerror or 'write failed'}") from None


def _render(reporter: Reporter, report: ComparisonReport, final: Path, partial: Path) -> None:
    """Render to the partial file. Any failure becomes a GuiError that quotes nothing.

    The exception text can hold report content (a column default, a connection detail), so only
    the operating system's reason is ever passed on.
    """
    try:
        render_to_path(reporter, report, partial)
    except OSError as exc:
        raise GuiError(f"could not write {final.name}: {exc.strerror or 'write failed'}") from None
    except Exception:
        raise GuiError(f"could not write {final.name}: the report could not be rendered") from None


def _changelog(target: TargetDiff) -> ChangelogDiff | None:
    changelog = target.changelog
    if changelog is None or not changelog.applicable:
        return None
    return changelog


def _ordered(findings: tuple[ObjectFinding, ...]) -> list[ObjectFinding]:
    """Report order: by kind, worst first within a kind, then by name."""
    ordered: list[ObjectFinding] = []
    for kind in KIND_ORDER:
        group = [f for f in findings if f.kind is kind]
        ordered.extend(sorted(group, key=lambda f: (-f.severity, f.key.sort_key)))
    return ordered


def _row(target: TargetDiff, finding: ObjectFinding) -> FindingRow:
    lines = []
    for delta in finding.deltas:
        before = "(none)" if delta.master_value is None else delta.master_value
        after = "(none)" if delta.target_value is None else delta.target_value
        lines.append(f"{delta.attribute}: {before} -> {after}")
        if delta.note:
            lines.append(f"  {delta.note}")
    if finding.paired_with is not None:
        lines.append(f"matches {finding.paired_with.name}")
    return FindingRow(
        target=target.target_label,
        kind=finding.kind.value,
        path=finding.key.path,
        status=finding.status.label,
        severity=finding.severity.label,
        detail="\n".join(lines),
        suppressed_by=finding.ignored_by,
    )


def _target_summary(target: TargetDiff) -> str:
    """Counts by severity, because severity is what gates. Never "in sync" for a skipped target."""
    if target.skipped:
        return "skipped: not inspected"
    worst = target.worst_severity()
    if worst is None:
        suffix = f" ({len(target.ignored)} suppressed)" if target.ignored else ""
        return f"in sync{suffix}"

    parts = [
        f"{count} {severity.label}"
        for severity in _SEVERITIES
        if (count := sum(1 for f in target.findings if f.severity is severity))
    ]
    # A note can be the whole story: an empty target has one note and no findings at all.
    for note in target.notes:
        if note.severity >= Severity.WARNING:
            parts.append(note.kind.value.replace("_", " "))
    changelog = _changelog(target)
    if changelog is not None and changelog.status is not ChangelogStatus.IN_SYNC:
        parts.append(f"liquibase: {changelog.status.label}")
    return ", ".join(parts) or "notes only"


def _changelog_lines(target: TargetDiff, changelog: ChangelogDiff) -> list[str]:
    master, replica = target.master_label, target.target_label
    lines = [changelog.headline(master, replica)]
    tags = changelog.tag_line(master, replica)
    if tags:
        lines.append(tags)
    if changelog.first_divergence is not None:
        changeset, author = changelog.first_divergence
        lines.append(f"histories diverge at: {changeset} by {author}")
    lines.extend(f"- {ref[0]} by {ref[1]}" for ref in changelog.missing_in_target)
    lines.extend(f"+ {ref[0]} by {ref[1]}" for ref in changelog.extra_in_target)
    lines.extend(
        f"~ {m.ref[0]} by {m.ref[1]}: checksum differs (a deployed changeset was edited)"
        for m in changelog.checksum_mismatches
    )
    lines.extend(
        f"~ {d.ref[0]} by {d.ref[1]}: {d.master_exec_type} vs {d.target_exec_type}"
        for d in changelog.exectype_differences
    )
    lines.extend(
        f"~ {d.ref[0]} by {d.ref[1]}: filename {d.master_filename} vs {d.target_filename} "
        f"({d.severity.label})"
        for d in changelog.filename_differences
    )
    for failed in changelog.failed_changesets:
        where = {"master": master, "target": replica}.get(failed.side)
        suffix = f" on {where}" if where else ""
        lines.append(f"! {failed.id} by {failed.author}: recorded as FAILED{suffix}")
    lines.extend(f"note: {note}" for note in changelog.notes)
    return lines


def checked_rows(
    rows: Sequence[Mapping[str, Any]], fields: tuple[str, ...], what: str
) -> list[Mapping[str, Any]]:
    """Return ``rows`` unchanged after proving every one carries every field of its struct.

    Assign a Slint model only from rows that went through here. Slint accepts a row dict with a
    key missing and then dies later, at ``show()`` or the next repaint, with no Python exception:
    ``panicked at internal/core/rtti.rs:260: binding was of the wrong type: ()``, process exit
    code 134. The window simply vanishes, far from its cause. A missing ``DiffLine.kind`` is
    quieter and no better: accepted, and drawn as context. Do not remove this as paranoid.
    Wrong types and extra keys already raise ``ValueError`` in the binding, so only presence is
    checked.
    """
    for index, row in enumerate(rows):
        for name in fields:
            if name not in row:
                raise GuiError(f"{what} row {index} is missing the field '{name}'")
    return list(rows)


def checked_delta_rows(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return checked_rows(rows, DELTA_ROW_FIELDS, "delta")


def checked_diff_rows(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return checked_rows(rows, DIFF_ROW_FIELDS, "diff")
