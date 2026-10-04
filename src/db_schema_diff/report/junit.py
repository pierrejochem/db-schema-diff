"""The JUnit XML report.

Written so GitHub Actions renders schema drift in the Checks tab as ordinary test failures, which
is the difference between a pipeline that says "step failed" and one that says which column in
which schema is wrong.

Uses the standard library's ElementTree — no dependency needed for a format this simple.

Two details here are bug sources rather than choices:

* **Characters illegal in XML 1.0.** PostgreSQL definitions and comments can contain control
  characters, and an XML file containing one is not merely ugly, it is unparseable. They are
  stripped.
* **A cap on the number of cases.** GitHub's renderer degrades badly on thousands of test cases,
  and a first run against a drifted environment can easily produce that many. The overflow is
  summarised in one extra case instead.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar, TextIO
from xml.etree import ElementTree as ET

from ..diff.model import ComparisonReport, ObjectFinding, TargetDiff
from ..diff.severity import Severity, gate

DEFAULT_MAX_CASES = 500

#: Characters XML 1.0 does not allow, at all, even escaped.
_ILLEGAL_XML = re.compile(r"[^\u0009\u000A\u000D -퟿-�\U00010000-\U0010FFFF]")


class JUnitReporter:
    """Writes the report as a JUnit XML suite per target."""

    name: ClassVar[str] = "junit"

    def __init__(self, *, max_cases: int = DEFAULT_MAX_CASES) -> None:
        self._max_cases = max_cases

    def render(self, report: ComparisonReport, out: TextIO) -> None:
        threshold = gate(report.fail_on)
        suites = ET.Element("testsuites", name=f"schema-drift/{report.name}")

        for target in report.targets:
            suites.append(self._suite(target, threshold))

        ET.indent(suites, space="  ")
        out.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        out.write(ET.tostring(suites, encoding="unicode"))
        out.write("\n")

    def _suite(self, target: TargetDiff, threshold: Severity | None) -> ET.Element:
        suite = ET.Element("testsuite", name=f"schema-drift/{target.target_label}")

        if target.skipped:
            # A target that could not be inspected is an error, not a pass: a pipeline must not
            # read an unreachable environment as a clean one.
            case = ET.SubElement(suite, "testcase", classname=target.target_label, name="probe")
            error = ET.SubElement(case, "error", message="could not be inspected")
            error.text = _clean(target.failed or "")
            _counts(suite, tests=1, failures=0, errors=1, skipped=0)
            return suite

        failures = 0
        shown = target.findings[: self._max_cases]
        for finding in shown:
            case = self._case(suite, target, finding)
            if threshold is not None and finding.severity >= threshold:
                failures += 1
                failure = ET.SubElement(
                    case, "failure", message=_message(finding), type=finding.severity.label
                )
                failure.text = _clean(_detail(finding))

        hidden = len(target.findings) - len(shown)
        if hidden > 0:
            case = ET.SubElement(
                suite, "testcase", classname=target.target_label, name="additional-findings"
            )
            failure = ET.SubElement(
                case, "failure", message=f"{hidden} further finding(s) not listed individually"
            )
            failure.text = _clean(
                f"{hidden} findings were omitted to keep this file renderable. "
                "The JSON report contains all of them."
            )
            failures += 1

        for finding in target.ignored:
            case = ET.SubElement(
                suite,
                "testcase",
                classname=f"{target.target_label}.{finding.kind.value}",
                name=_clean(finding.key.path),
            )
            ET.SubElement(
                case, "skipped", message=f"suppressed by ignore rule {finding.ignored_by}"
            )

        if target.changelog is not None and target.changelog.applicable:
            failures += self._changelog_case(suite, target, threshold)

        for note in target.notes:
            case = ET.SubElement(
                suite, "testcase", classname=target.target_label, name=note.kind.value
            )
            if threshold is not None and note.severity >= threshold:
                failure = ET.SubElement(case, "failure", message=_clean(note.message))
                failure.text = _clean(note.message)
                failures += 1
            else:
                system_out = ET.SubElement(case, "system-out")
                system_out.text = _clean(note.message)

        tests = len(suite.findall("testcase"))
        _counts(suite, tests=tests, failures=failures, errors=0, skipped=len(target.ignored))
        return suite

    def _changelog_case(
        self, suite: ET.Element, target: TargetDiff, threshold: Severity | None
    ) -> int:
        """One case for the migration history.

        A separate case rather than one per changeset: "qa is 7 changesets behind" is one fact, and
        seven failures in a CI summary would misrepresent it as seven problems.
        """
        changelog = target.changelog
        if changelog is None:
            return 0

        case = ET.SubElement(
            suite,
            "testcase",
            classname=f"{target.target_label}.liquibase",
            name="changelog",
        )
        detail = _clean(_changelog_detail(changelog, target))
        in_sync = changelog.status.value == "in_sync"
        if not in_sync and threshold is not None and changelog.severity >= threshold:
            failure = ET.SubElement(
                case,
                "failure",
                message=_clean(changelog.headline(target.master_label, target.target_label)),
                type=changelog.severity.label,
            )
            failure.text = detail
            return 1
        system_out = ET.SubElement(case, "system-out")
        system_out.text = detail
        return 0

    def _case(self, suite: ET.Element, target: TargetDiff, finding: ObjectFinding) -> ET.Element:
        return ET.SubElement(
            suite,
            "testcase",
            classname=f"{target.target_label}.{finding.kind.value}",
            name=_clean(finding.key.path),
        )


def _counts(suite: ET.Element, *, tests: int, failures: int, errors: int, skipped: int) -> None:
    suite.set("tests", str(tests))
    suite.set("failures", str(failures))
    suite.set("errors", str(errors))
    suite.set("skipped", str(skipped))


def _message(finding: ObjectFinding) -> str:
    if finding.deltas:
        attributes = ", ".join(d.attribute for d in finding.deltas)
        return _clean(f"{finding.status.label}: {attributes}")
    return _clean(finding.status.label)


def _detail(finding: ObjectFinding) -> str:
    lines = [f"{finding.key.display()}: {finding.status.label} ({finding.severity.label})"]
    for delta in finding.deltas:
        lines.append(f"  {delta.attribute}: {delta.master_value} -> {delta.target_value}")
        if delta.note:
            lines.append(f"    {delta.note}")
    if finding.paired_with is not None:
        lines.append(f"  matched to {finding.paired_with.path}")
    return "\n".join(lines)


def _changelog_detail(changelog: Any, target: TargetDiff) -> str:
    """The changelog verdict as text, for a JUnit failure body."""
    lines = [changelog.headline(target.master_label, target.target_label)]
    tags = changelog.tag_line(target.master_label, target.target_label)
    if tags:
        lines.append(tags)
    if changelog.first_divergence is not None:
        lines.append(
            f"histories diverge at: {changelog.first_divergence[0]} "
            f"by {changelog.first_divergence[1]}"
        )
    for ref in changelog.missing_in_target:
        lines.append(f"  missing in target: {ref[0]} by {ref[1]}")
    for ref in changelog.extra_in_target:
        lines.append(f"  extra in target: {ref[0]} by {ref[1]}")
    for mismatch in changelog.checksum_mismatches:
        lines.append(f"  checksum differs: {mismatch.ref[0]} by {mismatch.ref[1]}")
    for difference in changelog.exectype_differences:
        lines.append(
            f"  {difference.ref[0]} by {difference.ref[1]}: "
            f"{difference.master_exec_type} vs {difference.target_exec_type}"
        )
    for note in changelog.notes:
        lines.append(f"  note: {note}")
    return "\n".join(lines)


def _clean(text: str) -> str:
    """Remove characters XML 1.0 forbids.

    A single control character in a column comment or a check expression makes the whole file
    unparseable, and the failure surfaces in the CI renderer rather than here.
    """
    return _ILLEGAL_XML.sub("", text)
