"""The standalone HTML report.

One file, no network. A drift report is usually read from a CI artifact, often on a machine with no
outbound access, so a stylesheet or font that failed to load would make it unreadable. Everything is
inline: CSS, SVG, and the ~40 lines of JavaScript that filter it.

It also works with scripting off. Expanding and collapsing uses ``<details>``, and every finding is
rendered into the DOM; the JavaScript only hides what the reader is not looking at.

The same payload the JSON reporter writes is embedded in the page. That is not for the filtering —
the DOM already holds everything — but so the file is self-describing, and so the two outputs cannot
drift apart without a test noticing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import ClassVar, TextIO

from jinja2 import Environment, PackageLoader, select_autoescape
from markupsafe import Markup

from ..diff.changelog import ChangelogStatus
from ..diff.model import ComparisonReport, ObjectFinding, ObjectStatus, TargetDiff
from ..diff.severity import Severity
from ..model.kinds import KIND_ORDER, ObjectKind

TEMPLATE = "report.html.j2"

#: Severity to CSS class. ``ok`` has no severity of its own — it is the absence of one.
_CSS = {Severity.ERROR: "error", Severity.WARNING: "warning", Severity.INFO: "info"}

_MARKS = {
    ObjectStatus.MISSING_IN_TARGET: "-",
    ObjectStatus.EXTRA_IN_TARGET: "+",
    ObjectStatus.DIFFERS: "~",
    ObjectStatus.MATCH: " ",
}

#: Inline SVG, because an icon font or an external file would break the no-network guarantee.
_ICONS = {
    "error": (
        '<svg width="18" height="18" viewBox="0 0 20 20" fill="none" aria-hidden="true">'
        '<circle cx="10" cy="10" r="8" stroke="currentColor" stroke-width="2"/>'
        '<path d="M10 6v5M10 14h.01" stroke="currentColor" stroke-width="2"'
        ' stroke-linecap="round"/></svg>'
    ),
    "warning": (
        '<svg width="18" height="18" viewBox="0 0 20 20" fill="none" aria-hidden="true">'
        '<path d="M10 3l7.5 13H2.5L10 3z" stroke="currentColor" stroke-width="2"'
        ' stroke-linejoin="round"/>'
        '<path d="M10 8v3M10 13.5h.01" stroke="currentColor" stroke-width="2"'
        ' stroke-linecap="round"/></svg>'
    ),
    "info": (
        '<svg width="18" height="18" viewBox="0 0 20 20" fill="none" aria-hidden="true">'
        '<circle cx="10" cy="10" r="8" stroke="currentColor" stroke-width="2"/>'
        '<path d="M10 9v5M10 6h.01" stroke="currentColor" stroke-width="2"'
        ' stroke-linecap="round"/></svg>'
    ),
    "ok": (
        '<svg width="18" height="18" viewBox="0 0 20 20" fill="none" aria-hidden="true">'
        '<circle cx="10" cy="10" r="8" stroke="currentColor" stroke-width="2"/>'
        '<path d="M6.5 10.5l2.5 2.5 4.5-5" stroke="currentColor" stroke-width="2"'
        ' stroke-linecap="round" stroke-linejoin="round"/></svg>'
    ),
}


def _trusted(html: str) -> Markup:
    """Mark a string as safe to emit unescaped.

    Used for exactly two things, both of which are safe for a stated reason rather than by
    assumption: the inline SVG icons, which are literal constants in this module, and the embedded
    JSON payload, whose ``</`` sequences are escaped before it gets here. Nothing derived from a
    database ever passes through this.
    """
    return Markup(html)  # noqa: S704 - callers are the two cases documented above


@dataclass(frozen=True, slots=True)
class Verdict:
    """The banner at the top: the one thing a reader needs before anything else."""

    css: str
    text: str
    icon: Markup


@dataclass(frozen=True, slots=True)
class Cell:
    """One cell of the overview matrix."""

    text: str
    css: str
    title: str | None = None


@dataclass(frozen=True, slots=True)
class Row:
    """One row of the overview matrix: an object kind across every target."""

    label: str
    cells: tuple[Cell, ...]


@dataclass(frozen=True, slots=True)
class Group:
    """Findings of one kind within one target."""

    label: str
    summary: str
    findings: tuple[ObjectFinding, ...]


class HtmlReporter:
    """Renders the report as a single self-contained HTML file."""

    name: ClassVar[str] = "html"

    def __init__(self) -> None:
        self._environment = Environment(
            loader=PackageLoader("cumo_schema_comparer.report", "templates"),
            # Autoescaping is not optional here: schema identifiers, default expressions and
            # function bodies all reach the page, and any of them can contain angle brackets.
            autoescape=select_autoescape(default_for_string=True, default=True),
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def render(self, report: ComparisonReport, out: TextIO) -> None:
        template = self._environment.get_template(TEMPLATE)
        worst = report.worst_severity()
        out.write(
            template.render(
                report=report,
                worst=_CSS.get(worst) if worst else None,
                verdict=_verdict(report),
                matrix=_matrix(report),
                payload=_payload(report),
                grouped=_grouped,
                target_summary=_target_summary,
                short_version=_short_version,
                mark=lambda status: _MARKS.get(status, " "),
            )
        )
        out.write("\n")


def _payload(report: ComparisonReport) -> Markup:
    """The JSON reporter's payload, safe to embed in a ``<script>`` element.

    Every ``<`` becomes ``\\u003c``, a legal JSON escape, so no HTML-sensitive sequence can form
    inside the element at all.

    Escaping only ``</`` would stop the element closing early but is not enough in general: per the
    HTML tokenizer, a ``<!--<script`` sequence in script data switches to an escaped state in
    which a later ``</script>`` does *not* close the element, and the rest of the document is
    swallowed into it. Column defaults, check expressions and function bodies all reach this
    payload, so none of those sequences may survive.
    """
    text = json.dumps(report.to_json_dict(), indent=2, ensure_ascii=False, sort_keys=False)
    return _trusted(text.replace("<", "\\u003c"))


def _verdict(report: ComparisonReport) -> Verdict:
    """The headline. A partial comparison is never presented as a clean one."""
    if report.probe_failed:
        return Verdict(
            css="error",
            text=("At least one source could not be inspected, so this comparison is incomplete."),
            icon=_trusted(_ICONS["error"]),
        )
    worst = report.worst_severity()
    if worst is None:
        return Verdict(
            css="ok",
            text=f"No differences found across {len(report.targets)} target(s).",
            icon=_trusted(_ICONS["ok"]),
        )
    css = _CSS[worst]
    return Verdict(
        css=css,
        text=f"Drift found at {worst.label} (gate: --fail-on {report.fail_on}).",
        icon=_trusted(_ICONS[css]),
    )


def _matrix(report: ComparisonReport) -> tuple[Row, ...]:
    """The overview grid: object kinds down, targets across.

    Only kinds that actually produced a finding somewhere get a row. Showing every kind would pad
    the table with fifteen rows of zeros and bury the two that matter.
    """
    rows: list[Row] = []

    for kind in KIND_ORDER:
        if not any(f.kind is kind for target in report.targets for f in target.findings):
            continue
        rows.append(
            Row(
                label=kind.plural,
                cells=tuple(_kind_cell(target, kind) for target in report.targets),
            )
        )

    if any(t.changelog is not None and t.changelog.applicable for t in report.targets):
        rows.append(
            Row(
                label="liquibase",
                cells=tuple(_changelog_cell(target) for target in report.targets),
            )
        )

    return tuple(rows)


def _kind_cell(target: TargetDiff, kind: ObjectKind) -> Cell:
    if target.skipped:
        return Cell(text="—", css="skipped", title="not inspected")
    findings = [f for f in target.findings if f.kind is kind]
    if not findings:
        return Cell(text="0", css="ok")
    worst = max(f.severity for f in findings)
    breakdown = ", ".join(
        f"{sum(1 for f in findings if f.status is status)} {status.label}"
        for status in (
            ObjectStatus.MISSING_IN_TARGET,
            ObjectStatus.EXTRA_IN_TARGET,
            ObjectStatus.DIFFERS,
        )
        if any(f.status is status for f in findings)
    )
    return Cell(text=str(len(findings)), css=_CSS[worst], title=breakdown)


def _changelog_cell(target: TargetDiff) -> Cell:
    if target.skipped or target.changelog is None:
        return Cell(text="—", css="skipped", title="not inspected")
    changelog = target.changelog
    if changelog.status is ChangelogStatus.IN_SYNC:
        return Cell(text="in sync", css="ok")
    if changelog.behind_count:
        return Cell(
            text=f"-{changelog.behind_count}",
            css=_CSS[changelog.severity],
            title=f"{changelog.behind_count} changeset(s) behind",
        )
    return Cell(
        text=changelog.status.label, css=_CSS[changelog.severity], title=changelog.status.label
    )


def _grouped(target: TargetDiff) -> tuple[Group, ...]:
    """A target's findings by kind, in report order, worst first within each kind."""
    groups: list[Group] = []
    for kind in KIND_ORDER:
        findings = [f for f in target.findings if f.kind is kind]
        if not findings:
            continue
        findings.sort(key=lambda f: (-f.severity, f.key.sort_key))
        groups.append(
            Group(
                label=kind.plural,
                summary=_status_summary(findings),
                findings=tuple(findings),
            )
        )
    return tuple(groups)


def _status_summary(findings: list[ObjectFinding]) -> str:
    parts = [
        f"{count} {status.label}"
        for status in (
            ObjectStatus.MISSING_IN_TARGET,
            ObjectStatus.EXTRA_IN_TARGET,
            ObjectStatus.DIFFERS,
        )
        if (count := sum(1 for f in findings if f.status is status))
    ]
    return ", ".join(parts)


def _target_summary(target: TargetDiff) -> str:
    """One line per target, counted by severity because severity is what gates the build."""
    worst = target.worst_severity()
    if worst is None:
        suffix = f" ({len(target.ignored)} suppressed)" if target.ignored else ""
        return f"in sync{suffix}"

    parts = [
        f"{count} {severity.label}"
        for severity in (Severity.ERROR, Severity.WARNING, Severity.INFO)
        if (count := sum(1 for f in target.findings if f.severity is severity))
    ]
    changelog = target.changelog
    if (
        changelog is not None
        and changelog.applicable
        and changelog.status is not ChangelogStatus.IN_SYNC
    ):
        parts.append(f"liquibase: {changelog.status.label}")
    return ", ".join(parts) or "notes only"


def _short_version(server_version: str) -> str:
    """Just the release number; the packager's full description is too long for a heading."""
    return server_version.split(" ", 1)[0]


def load_template_source() -> str:
    """The template's text, for the test that checks it references nothing external."""
    from importlib import resources

    return (
        resources.files("cumo_schema_comparer.report")
        .joinpath("templates", TEMPLATE)
        .read_text(encoding="utf-8")
    )
