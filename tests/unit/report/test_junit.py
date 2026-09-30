"""The JUnit XML report.

Written so GitHub renders drift as ordinary test failures. The two things that actually break in
practice are unparseable characters and case counts in the thousands, so both are pinned here.
"""

from __future__ import annotations

import io
from xml.etree import ElementTree as ET

from cumo_schema_comparer.report.junit import JUnitReporter
from tests.support.reports import HOSTILE_TEXT, clean_report, full_report


def render(report=None, **kwargs) -> str:
    out = io.StringIO()
    JUnitReporter(**kwargs).render(report or full_report(), out)
    return out.getvalue()


def parse(report=None, **kwargs) -> ET.Element:
    return ET.fromstring(render(report, **kwargs))


def suite(root: ET.Element, name: str) -> ET.Element:
    return next(s for s in root.findall("testsuite") if s.get("name", "").endswith(name))


class TestStructure:
    def test_the_document_is_well_formed_xml_with_a_declaration(self):
        text = render()
        assert text.startswith('<?xml version="1.0" encoding="UTF-8"?>')
        ET.fromstring(text)

    def test_one_suite_per_target(self):
        root = parse()
        names = [s.get("name") for s in root.findall("testsuite")]
        assert names == [
            "schema-drift/qa",
            "schema-drift/dev",
            "schema-drift/local",
        ]

    def test_each_finding_becomes_a_case_named_by_its_path(self):
        qa = suite(parse(), "/qa")
        names = [c.get("name") for c in qa.findall("testcase")]
        assert "cumo-invoicing.invoice.number" in names
        assert "cumo-invoicing.DunningLevel" in names

    def test_the_classname_carries_the_target_and_kind(self):
        qa = suite(parse(), "/qa")
        case = next(c for c in qa.findall("testcase") if c.get("name") == "public.tmp_debug")
        assert case.get("classname") == "qa.table"


class TestFailureGating:
    def test_only_findings_at_or_above_the_gate_fail(self):
        qa = suite(parse(), "/qa")
        failed = {c.get("name") for c in qa.findall("testcase") if c.find("failure") is not None}
        # fail_on=error, so the two errors fail and the warnings and info do not.
        assert "cumo-invoicing.DunningLevel" in failed
        assert "cumo-invoicing.invoice.number" in failed
        assert "public.tmp_debug" not in failed
        assert "public.audit_entry" not in failed

    def test_lowering_the_gate_fails_the_warnings_too(self):
        qa = suite(parse(full_report(fail_on="warning")), "/qa")
        failed = {c.get("name") for c in qa.findall("testcase") if c.find("failure") is not None}
        assert "public.tmp_debug" in failed
        assert "public.audit_entry" not in failed

    def test_fail_on_never_produces_no_failures(self):
        root = parse(full_report(fail_on="never"))
        assert root.findall(".//failure") == []

    def test_the_failure_carries_the_severity_as_its_type(self):
        qa = suite(parse(), "/qa")
        case = next(
            c for c in qa.findall("testcase") if c.get("name") == "cumo-invoicing.invoice.number"
        )
        assert case.find("failure").get("type") == "error"

    def test_the_failure_body_names_the_attribute_and_both_values(self):
        qa = suite(parse(), "/qa")
        case = next(
            c for c in qa.findall("testcase") if c.get("name") == "cumo-invoicing.invoice.number"
        )
        body = case.find("failure").text or ""
        assert "column.data_type" in body
        assert "varchar(40)" in body
        assert "text" in body


class TestCounts:
    def test_the_suite_counts_match_its_contents(self):
        qa = suite(parse(), "/qa")
        assert int(qa.get("tests")) == len(qa.findall("testcase"))
        assert int(qa.get("failures")) == len(qa.findall("testcase/failure"))
        assert int(qa.get("skipped")) == len(qa.findall("testcase/skipped"))

    def test_an_in_sync_target_has_no_failures(self):
        dev = suite(parse(), "/dev")
        assert int(dev.get("failures")) == 0


class TestUnreachableTarget:
    def test_an_unreachable_target_is_an_error_not_a_pass(self):
        # A pipeline must never read an environment it could not inspect as a clean one.
        local = suite(parse(), "/local")
        error = local.find("testcase/error")
        assert error is not None
        assert int(local.get("errors")) == 1

    def test_the_error_says_why(self):
        local = suite(parse(), "/local")
        assert "cannot connect" in (local.find("testcase/error").text or "")


class TestIgnoredFindings:
    def test_an_ignored_finding_becomes_a_skipped_case(self):
        qa = suite(parse(), "/qa")
        skipped = qa.find("testcase/skipped")
        assert skipped is not None
        assert "quartz-runtime" in skipped.get("message")


class TestNotes:
    def test_a_note_below_the_gate_is_reported_as_output_not_a_failure(self):
        qa = suite(parse(full_report(fail_on="error")), "/qa")
        case = next(c for c in qa.findall("testcase") if c.get("name") == "version_skew")
        assert case.find("failure") is None
        assert "PostgreSQL 15" in (case.find("system-out").text or "")

    def test_a_note_at_or_above_the_gate_fails(self):
        qa = suite(parse(full_report(fail_on="warning")), "/qa")
        case = next(c for c in qa.findall("testcase") if c.get("name") == "version_skew")
        assert case.find("failure") is not None


class TestHostileCharacters:
    def test_a_control_character_is_stripped_rather_than_emitted(self):
        """XML 1.0 cannot represent a control character at all, escaped or not.

        A single one in a column comment or a check expression makes the whole file unparseable,
        and the failure shows up in the CI renderer rather than here.
        """
        text = render(full_report(fail_on="warning"))
        assert "\x01" not in text
        ET.fromstring(text)  # would raise if the stripping had missed one

    def test_the_surrounding_text_survives_the_stripping(self):
        qa = suite(parse(full_report(fail_on="warning")), "/qa")
        case = next(
            c for c in qa.findall("testcase") if c.get("name") == "cumo-invoicing.invoice.status"
        )
        body = case.find("failure").text or ""
        assert HOSTILE_TEXT.replace("\x01", "") in body


class TestCaseCap:
    def _many(self, count):
        from cumo_schema_comparer.diff.model import (
            ComparisonReport,
            ObjectFinding,
            ObjectStatus,
            TargetDiff,
        )
        from cumo_schema_comparer.diff.severity import Severity
        from cumo_schema_comparer.model.keys import table_key

        findings = tuple(
            ObjectFinding(
                key=table_key("public", f"t{n:04d}"),
                status=ObjectStatus.MISSING_IN_TARGET,
                severity=Severity.ERROR,
            )
            for n in range(count)
        )
        return ComparisonReport(
            name="big",
            master_label="prod",
            targets=(
                TargetDiff(
                    master_label="prod",
                    target_label="qa",
                    master_source=None,
                    target_source=None,
                    findings=findings,
                ),
            ),
        )

    def test_the_number_of_cases_is_capped(self):
        # GitHub's renderer degrades badly on thousands of cases, and a first run against a
        # drifted environment easily produces that many.
        qa = suite(parse(self._many(50), max_cases=10), "/qa")
        assert len(qa.findall("testcase")) == 11  # 10 findings plus the overflow summary

    def test_the_overflow_is_summarized_as_one_failure(self):
        qa = suite(parse(self._many(50), max_cases=10), "/qa")
        case = next(c for c in qa.findall("testcase") if c.get("name") == "additional-findings")
        assert "40 further finding" in case.find("failure").get("message")

    def test_nothing_is_capped_when_it_fits(self):
        qa = suite(parse(self._many(5), max_cases=10), "/qa")
        names = [c.get("name") for c in qa.findall("testcase")]
        assert "additional-findings" not in names


class TestDeterminism:
    def test_two_renders_are_byte_identical(self):
        assert render() == render()

    def test_a_clean_report_renders_without_failures(self):
        root = parse(clean_report())
        assert root.findall(".//failure") == []
        assert root.findall(".//error") == []
