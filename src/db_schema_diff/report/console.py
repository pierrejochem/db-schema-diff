"""The terminal report.

Built on ``click.style`` rather than a rich-text library: it keeps the runtime dependency set
at five packages, and click already suppresses colour when the output is not a terminal or
``NO_COLOR`` is set, which is what CI logs need.

Ordering is fully deterministic — kind, then schema, then name — so two runs against unchanged
databases produce identical text and can be diffed.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar, TextIO

import click

from ..diff.changelog import ChangelogStatus
from ..diff.model import ComparisonReport, ObjectFinding, ObjectStatus, TargetDiff
from ..diff.severity import Severity, gate
from ..model.kinds import KIND_ORDER, ObjectKind

DEFAULT_MAX_PER_KIND = 20

#: Shorthand, used where the line would otherwise wrap awkwardly.
IN_SYNC = ChangelogStatus.IN_SYNC

#: Emits one line of the report.
_Write = Callable[[str], None]

_SEVERITY_COLOUR = {
    Severity.ERROR: "red",
    Severity.WARNING: "yellow",
    Severity.INFO: "cyan",
}

_STATUS_MARK = {
    ObjectStatus.MISSING_IN_TARGET: "-",
    ObjectStatus.EXTRA_IN_TARGET: "+",
    ObjectStatus.DIFFERS: "~",
    ObjectStatus.MATCH: " ",
}


class ConsoleReporter:
    """Human-readable report for a terminal or a CI log."""

    name: ClassVar[str] = "console"

    def __init__(
        self, *, max_per_kind: int = DEFAULT_MAX_PER_KIND, show_ignored: bool = False
    ) -> None:
        self._max_per_kind = max_per_kind
        self._show_ignored = show_ignored

    def render(self, report: ComparisonReport, out: TextIO) -> None:
        write = _writer(out)

        write(_bold(f"Schema comparison: {report.name}"))
        write(f"master: {report.master_label}")
        write("")

        for note in report.notes:
            write(_note_line(note.severity, note.message))
        if report.notes:
            write("")

        for target in report.targets:
            self._render_target(target, write)

        self._render_summary(report, write)

    # -- Per target ----------------------------------------------------------------------

    def _render_target(self, target: TargetDiff, write: _Write) -> None:
        write(_bold(f"── {target.target_label} " + "─" * max(0, 60 - len(target.target_label))))

        if target.skipped:
            write(_colour("yellow", f"   SKIPPED: {target.failed}"))
            write("")
            return

        write(f"   {_versions(target)}")

        for note in target.notes:
            write("   " + _note_line(note.severity, note.message))

        if target.findings:
            for kind in KIND_ORDER:
                findings = [f for f in target.findings if f.kind is kind]
                if findings:
                    self._render_kind(kind, findings, write)
        elif not _explained_by_a_note(target):
            # Scoped to the DDL on purpose: the Liquibase section below may still be reporting a
            # problem, and a bare "in sync" beside it reads as a contradiction.
            #
            # Suppressed entirely when a note already explains why there are no findings. An empty
            # target produces one note and zero findings by design, and "no schema differences"
            # printed underneath that reads as reassurance about a database that holds nothing.
            write(_colour("green", f"   no schema differences{_suppressed_suffix(target)}"))

        self._render_changelog(target, write)

        # Always after the findings, never skipped when there are none: a run whose every finding
        # was suppressed is precisely when a reader needs to see what the rules set aside.
        if self._show_ignored and target.ignored:
            write(f"   {len(target.ignored)} finding(s) suppressed by ignore rules:")
            for finding in target.ignored:
                write(
                    f"     {_STATUS_MARK[finding.status]} {finding.key.path}  "
                    f"[{finding.ignored_by}]"
                )

        write("")

    def _render_changelog(self, target: TargetDiff, write: _Write) -> None:
        """The Liquibase section.

        Deliberately short. The headline and the first divergence are what somebody acts on; the
        full list of missing changesets is in the JSON report for when they need it.
        """
        changelog = target.changelog
        if changelog is None or not changelog.applicable:
            return

        write(f"   {_bold('liquibase')}")
        colour = _SEVERITY_COLOUR[changelog.severity]
        write(
            "     " + _colour(colour, changelog.headline(target.master_label, target.target_label))
        )

        tags = changelog.tag_line(target.master_label, target.target_label)
        if tags:
            write(f"     {tags}")

        if changelog.first_divergence is not None:
            changeset, author = changelog.first_divergence
            # The single most actionable line: where the two histories split, rather than every
            # consequence downstream of the split.
            write(f"     histories diverge at: {changeset} by {author}")

        for ref in changelog.missing_in_target[: self._max_per_kind]:
            write(_colour(colour, f"     - {ref[0]} by {ref[1]}"))
        hidden = len(changelog.missing_in_target) - self._max_per_kind
        if hidden > 0:
            write(
                _colour(
                    "bright_black",
                    f"     … and {hidden} more missing changeset(s) (see --json)",
                )
            )

        for ref in changelog.extra_in_target[: self._max_per_kind]:
            write(_colour(colour, f"     + {ref[0]} by {ref[1]}"))

        for mismatch in changelog.checksum_mismatches[: self._max_per_kind]:
            write(
                _colour(
                    "red",
                    f"     ~ {mismatch.ref[0]} by {mismatch.ref[1]}: checksum differs "
                    "(a deployed changeset was edited)",
                )
            )

        for difference in changelog.exectype_differences[: self._max_per_kind]:
            write(
                _colour(
                    _SEVERITY_COLOUR[difference.severity],
                    f"     ~ {difference.ref[0]} by {difference.ref[1]}: "
                    f"{difference.master_exec_type} vs {difference.target_exec_type}",
                )
            )

        for failed in changelog.failed_changesets:
            where = {"master": target.master_label, "target": target.target_label}.get(failed.side)
            suffix = f" on {where}" if where else ""
            write(
                _colour("red", f"     ! {failed.id} by {failed.author}: recorded as FAILED{suffix}")
            )

        for note in changelog.notes:
            write(_colour("bright_black", f"     note: {note}"))

    def _render_kind(self, kind: ObjectKind, findings: list[ObjectFinding], write: _Write) -> None:
        # Worst first: the reader should hit the broken things before the cosmetic ones.
        ordered = sorted(findings, key=lambda f: (-f.severity, f.key.sort_key))
        counts = _summarize(ordered)
        write(f"   {_bold(kind.plural)}  {counts}")

        for finding in ordered[: self._max_per_kind]:
            write("     " + _finding_line(finding))
            for delta in finding.deltas:
                write(f"       {delta.attribute}: {delta.master_value} → {delta.target_value}")
                if delta.note:
                    write(_colour("bright_black", f"         {delta.note}"))

        hidden = len(ordered) - self._max_per_kind
        if hidden > 0:
            write(
                _colour(
                    "bright_black",
                    f"     … and {hidden} more (use --json for the complete list)",
                )
            )

    # -- Summary -------------------------------------------------------------------------

    def _render_summary(self, report: ComparisonReport, write: _Write) -> None:
        write(_bold("Summary"))
        for target in report.targets:
            write(f"   {target.target_label:<16} {_target_summary(target)}")

        threshold = gate(report.fail_on)
        write("")
        if report.probe_failed:
            # Stated explicitly: a partial comparison must never read as a clean gate.
            write(
                _colour(
                    "red",
                    "At least one source could not be inspected, so this comparison is incomplete.",
                )
            )
        elif report.has_drift(threshold):
            # has_drift() is only true when a worst severity exists.
            worst = report.worst_severity() or Severity.ERROR
            write(
                _colour(
                    _SEVERITY_COLOUR[worst],
                    f"Drift found at {worst.label} (gate: --fail-on {report.fail_on}).",
                )
            )
        else:
            write(_colour("green", f"No drift at or above {report.fail_on}."))


# -- Formatting helpers ------------------------------------------------------------------


def _writer(out: TextIO) -> _Write:
    def write(line: str) -> None:
        click.echo(line, file=out)

    return write


def _bold(text: str) -> str:
    return click.style(text, bold=True)


def _colour(colour: str, text: str) -> str:
    return click.style(text, fg=colour)


def _note_line(severity: Severity, message: str) -> str:
    return _colour(_SEVERITY_COLOUR[severity], f"note ({severity.label}): {message}")


def _finding_line(finding: ObjectFinding) -> str:
    mark = _STATUS_MARK[finding.status]
    text = f"{mark} {finding.key.path}"
    if finding.paired_with is not None:
        text += f"  (matches {finding.paired_with.name})"
    if finding.ignored_by is not None:
        text += f"  [downgraded by {finding.ignored_by}]"
    return _colour(_SEVERITY_COLOUR[finding.severity], text)


def _summarize(findings: list[ObjectFinding]) -> str:
    parts = []
    for status in (
        ObjectStatus.MISSING_IN_TARGET,
        ObjectStatus.EXTRA_IN_TARGET,
        ObjectStatus.DIFFERS,
    ):
        count = sum(1 for f in findings if f.status is status)
        if count:
            parts.append(f"{count} {status.label}")
    return ", ".join(parts)


def _explained_by_a_note(target: TargetDiff) -> bool:
    """Whether a note already accounts for the absence of findings."""
    return any(note.severity >= Severity.WARNING for note in target.notes)


def _suppressed_suffix(target: TargetDiff) -> str:
    """Say so when "in sync" only holds because rules suppressed something.

    Reporting a bare "in sync" would overstate the result, and the whole point of an auditable
    suppression is that the reader knows it happened.
    """
    if not target.ignored:
        return ""
    count = len(target.ignored)
    noun = "finding" if count == 1 else "findings"
    return f" ({count} {noun} suppressed; --show-ignored to list them)"


def _target_summary(target: TargetDiff) -> str:
    """One line per target: counts by severity, because severity is what gates the build.

    Grouping by status instead would read "3 extra in target, 2 differs" next to the word
    "error", which invites the reader to think all five are errors.
    """
    if target.skipped:
        return _colour("yellow", "skipped")
    worst = target.worst_severity()
    if worst is None:
        return _colour("green", f"in sync{_suppressed_suffix(target)}")

    parts = []
    for severity in (Severity.ERROR, Severity.WARNING, Severity.INFO):
        count = sum(1 for f in target.findings if f.severity is severity)
        if count:
            parts.append(f"{count} {severity.label}")

    # A note can be the whole story — an empty target produces one note and no findings at all — so
    # the summary has to name it rather than fall through to something less important.
    for note in target.notes:
        if note.severity >= Severity.WARNING:
            parts.append(note.kind.value.replace("_", " "))

    # The changelog is not a finding, but it can be the only reason the run fails. Naming it here
    # stops the summary reading "notes only" next to an exit code of 1.
    changelog = target.changelog
    if changelog is not None and changelog.applicable and changelog.status is not IN_SYNC:
        parts.append(f"liquibase: {changelog.status.label}")

    detail = ", ".join(parts) or "notes only"
    return _colour(_SEVERITY_COLOUR[worst], detail)


def _versions(target: TargetDiff) -> str:
    master = target.master_source
    replica = target.target_source
    if master is None or replica is None:
        return ""
    return (
        f"{master.label} PostgreSQL {_short_version(master.server_version)} · "
        f"{replica.label} PostgreSQL {_short_version(replica.server_version)}"
    )


def _short_version(server_version: str) -> str:
    """Just the release number.

    ``server_version`` carries the packager's full description — "15.19 (Debian
    15.19-1.pgdg13+2)" — which is worth keeping in the JSON report and too long for a heading.
    """
    return server_version.split(" ", 1)[0]
