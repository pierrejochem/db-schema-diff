"""The JSON report.

It has to be lossless: the HTML and JUnit output must be reproducible from this file alone, so a
comparison can be captured in CI and rendered anywhere later.
"""

from __future__ import annotations

import io
import json

import pytest

from cumo_schema_comparer.diff.model import REPORT_SCHEMA_VERSION, ComparisonReport
from cumo_schema_comparer.report.json_report import JsonReporter, load_report
from tests.support.reports import HOSTILE_TEXT, clean_report, full_report


def render(report=None) -> str:
    out = io.StringIO()
    JsonReporter().render(report or full_report(), out)
    return out.getvalue()


class TestShape:
    def test_the_output_is_valid_json_ending_in_a_newline(self):
        text = render()
        assert text.endswith("\n")
        assert json.loads(text)["name"] == "invoicing"

    def test_the_schema_version_is_declared(self):
        assert json.loads(render())["schema_version"] == REPORT_SCHEMA_VERSION

    def test_every_target_appears_including_the_unreachable_one(self):
        targets = json.loads(render())["targets"]
        assert [t["target"] for t in targets] == ["qa", "dev", "local"]
        assert targets[2]["failed"]

    def test_the_incomplete_flag_survives(self):
        assert json.loads(render())["probe_failed"] is True


class TestRoundTrip:
    def test_a_report_survives_a_round_trip(self):
        original = full_report()
        restored = load_report(render(original))
        assert restored == original

    def test_a_clean_report_survives_a_round_trip(self):
        original = clean_report()
        assert load_report(render(original)) == original

    def test_the_round_trip_is_idempotent(self):
        # Re-rendering what was loaded must produce the same bytes, or `render --from` would
        # silently drift from the original.
        once = render()
        assert render(load_report(once)) == once

    def test_an_unknown_schema_version_is_refused(self):
        payload = json.loads(render())
        payload["schema_version"] = 99
        with pytest.raises(ValueError, match="schema_version"):
            ComparisonReport.from_json_dict(payload)

    def test_a_column_path_containing_a_dot_still_round_trips(self):
        # Table and column names may contain dots; the path has to be split by position.
        from cumo_schema_comparer.diff.model import ObjectFinding, ObjectStatus, TargetDiff
        from cumo_schema_comparer.diff.severity import Severity
        from cumo_schema_comparer.model.keys import column_key

        key = column_key("cumo-invoicing", "odd.name", "col")
        report = ComparisonReport(
            name="x",
            master_label="prod",
            targets=(
                TargetDiff(
                    master_label="prod",
                    target_label="qa",
                    master_source=None,
                    target_source=None,
                    findings=(ObjectFinding(key, ObjectStatus.MISSING_IN_TARGET, Severity.ERROR),),
                ),
            ),
        )
        restored = load_report(render(report))
        assert restored.targets[0].findings[0].key == key


class TestCompleteness:
    def test_ignored_findings_are_present_not_dropped(self):
        # Suppressed is not the same as invisible: an ignore rule has to stay auditable.
        target = json.loads(render())["targets"][0]
        assert target["ignored"][0]["ignored_by"] == "quartz-runtime"

    def test_notes_are_present_at_both_levels(self):
        payload = json.loads(render())
        assert payload["notes"][0]["kind"] == "probe_failed"
        assert payload["targets"][0]["notes"][0]["kind"] == "version_skew"

    def test_delta_notes_and_severities_survive(self):
        finding = json.loads(render())["targets"][0]["findings"][1]
        delta = finding["deltas"][0]
        assert delta["severity"] == "error"
        assert "length" in delta["note"]

    def test_hostile_text_is_preserved_verbatim(self):
        # JSON can carry a control character; only the XML reporter has to strip it.
        payload = json.loads(render())
        statuses = payload["targets"][0]["findings"][2]["deltas"][0]
        assert statuses["target"] == HOSTILE_TEXT


class TestDeterminism:
    def test_two_renders_are_byte_identical(self):
        assert render() == render()

    def test_no_credential_appears_in_the_output(self):
        text = render()
        for forbidden in ("password", "dsn_env", "PROD_DSN"):
            assert forbidden not in text


class TestSuppressionIsVisible:
    """A suppressed finding must be discoverable, in every output format.

    An ignore rule people cannot audit is indistinguishable from a bug in the comparer.
    """

    def _report_with_only_suppressed_findings(self):
        from cumo_schema_comparer.diff.model import (
            ComparisonReport,
            ObjectFinding,
            ObjectStatus,
            TargetDiff,
        )
        from cumo_schema_comparer.diff.severity import Severity
        from cumo_schema_comparer.model.keys import table_key
        from tests.support.builders import source

        return ComparisonReport(
            name="invoicing",
            master_label="prod",
            targets=(
                TargetDiff(
                    master_label="prod",
                    target_label="qa",
                    master_source=source("prod"),
                    target_source=source("qa"),
                    ignored=(
                        ObjectFinding(
                            key=table_key("public", "QRTZ_LOCKS"),
                            status=ObjectStatus.MISSING_IN_TARGET,
                            severity=Severity.ERROR,
                            ignored_by="quartz-runtime",
                        ),
                    ),
                ),
            ),
        )

    def test_the_console_says_a_finding_was_suppressed_even_when_in_sync(self):
        import io

        from cumo_schema_comparer.report.console import ConsoleReporter

        out = io.StringIO()
        ConsoleReporter().render(self._report_with_only_suppressed_findings(), out)
        text = out.getvalue()
        # A bare "in sync" would overstate the result.
        assert "1 finding suppressed" in text

    def test_show_ignored_lists_them_even_when_nothing_else_is_reported(self):
        import io

        from cumo_schema_comparer.report.console import ConsoleReporter

        out = io.StringIO()
        ConsoleReporter(show_ignored=True).render(self._report_with_only_suppressed_findings(), out)
        text = out.getvalue()
        assert "public.QRTZ_LOCKS" in text
        assert "quartz-runtime" in text

    def test_the_json_report_carries_them(self):
        payload = self._report_with_only_suppressed_findings().to_json_dict()
        ignored = payload["targets"][0]["ignored"]
        assert [f["ignored_by"] for f in ignored] == ["quartz-runtime"]


class TestChangelogInTheConsoleReport:
    """The Liquibase section must not contradict the schema section."""

    def _report(self, *, behind: int):
        from cumo_schema_comparer.diff.changelog import ChangelogDiff, ChangelogStatus
        from cumo_schema_comparer.diff.model import ComparisonReport, TargetDiff
        from cumo_schema_comparer.diff.severity import Severity
        from tests.support.builders import source

        changelog = ChangelogDiff(
            status=(ChangelogStatus.TARGET_BEHIND if behind else ChangelogStatus.IN_SYNC),
            severity=Severity.ERROR if behind else Severity.INFO,
            master_count=3,
            target_count=3 - behind,
            missing_in_target=tuple((f"cs{n}", "kolowae") for n in range(behind)),
            first_divergence=("cs0", "kolowae") if behind else None,
            master_tag="R7.6.2",
            target_tag=None,
        )
        return ComparisonReport(
            name="invoicing",
            master_label="prod",
            targets=(
                TargetDiff(
                    master_label="prod",
                    target_label="qa",
                    master_source=source("prod"),
                    target_source=source("qa"),
                    changelog=changelog,
                ),
            ),
        )

    def _render(self, *, behind: int) -> str:
        import io

        from cumo_schema_comparer.report.console import ConsoleReporter

        out = io.StringIO()
        ConsoleReporter().render(self._report(behind=behind), out)
        return out.getvalue()

    def test_a_clean_ddl_beside_a_failing_changelog_does_not_claim_in_sync(self):
        # "in sync" next to a Liquibase error reads as a contradiction.
        text = self._render(behind=2)
        assert "no schema differences" in text
        assert "in sync" not in text

    def test_the_summary_names_liquibase_when_that_is_what_failed(self):
        # Otherwise the summary reads "notes only" beside an exit code of 1.
        text = self._render(behind=2)
        assert "liquibase: target behind" in text

    def test_the_headline_and_divergence_point_are_shown(self):
        text = self._render(behind=2)
        assert "qa is 2 changeset(s) behind prod" in text
        assert "histories diverge at: cs0 by kolowae" in text

    def test_the_tag_line_is_shown(self):
        assert "prod @ R7.6.2 · qa @ untagged" in self._render(behind=2)

    def test_an_in_sync_changelog_leaves_the_summary_clean(self):
        text = self._render(behind=0)
        assert "in sync" in text
        assert "liquibase:" not in text


class TestEmptyTargetIsNotReportedAsClean:
    """An empty target produces one note and zero findings, by design.

    Printing "no schema differences" underneath that note reads as reassurance about a database that
    holds nothing at all. Seen in a real run against the RMV stack.
    """

    def _report(self):
        from cumo_schema_comparer.diff.model import (
            ComparisonReport,
            Note,
            NoteKind,
            TargetDiff,
        )
        from cumo_schema_comparer.diff.severity import Severity
        from tests.support.builders import source

        return ComparisonReport(
            name="invoicing",
            master_label="prod",
            targets=(
                TargetDiff(
                    master_label="prod",
                    target_label="stale",
                    master_source=source("prod"),
                    target_source=source("stale"),
                    notes=(
                        Note(
                            kind=NoteKind.TARGET_EMPTY,
                            message="target 'stale' contains no comparable objects",
                            severity=Severity.ERROR,
                        ),
                    ),
                ),
            ),
        )

    def _render(self) -> str:
        import io

        from cumo_schema_comparer.report.console import ConsoleReporter

        out = io.StringIO()
        ConsoleReporter().render(self._report(), out)
        return out.getvalue()

    def test_it_does_not_claim_there_are_no_schema_differences(self):
        text = self._render()
        assert "no comparable objects" in text
        assert "no schema differences" not in text

    def test_the_summary_names_the_note(self):
        # Otherwise it reads "notes only" beside an exit code of 1.
        assert "target empty" in self._render()

    def test_a_genuinely_clean_target_still_says_so(self):
        import io

        from cumo_schema_comparer.diff.model import ComparisonReport, TargetDiff
        from cumo_schema_comparer.report.console import ConsoleReporter
        from tests.support.builders import source

        report = ComparisonReport(
            name="invoicing",
            master_label="prod",
            targets=(
                TargetDiff(
                    master_label="prod",
                    target_label="qa",
                    master_source=source("prod"),
                    target_source=source("qa"),
                ),
            ),
        )
        out = io.StringIO()
        ConsoleReporter().render(report, out)
        assert "no schema differences" in out.getvalue()
