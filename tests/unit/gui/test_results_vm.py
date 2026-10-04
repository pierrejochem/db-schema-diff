# tests/unit/gui/test_results_vm.py
"""Presenting a finished comparison.

Filtering must behave the same way the HTML report's does, and the verdict must never present a
partial comparison as a clean one.
"""

from __future__ import annotations

import json

import pytest

from db_schema_comparer.gui.results_vm import ResultsModel
from tests.support.reports import clean_report, full_report


@pytest.fixture
def model() -> ResultsModel:
    return ResultsModel(full_report())


class TestVerdict:
    def test_an_incomplete_comparison_is_never_reported_as_clean(self, model):
        level, sentence = model.verdict
        assert level == "error"
        assert "could not be inspected" in sentence

    def test_a_clean_report_says_so(self):
        level, sentence = ResultsModel(clean_report()).verdict
        assert level == "ok"
        assert "No differences" in sentence


class TestRows:
    def test_one_row_per_target_including_the_skipped_one(self, model):
        rows = model.target_rows()
        assert [row["label"] for row in rows] == ["qa", "dev", "local"]
        assert rows[2]["skipped"] is True

    def test_findings_are_grouped_in_report_order(self, model):
        kinds = [row.kind for row in model.finding_rows()]
        # Tables before columns, as every other reporter orders them.
        assert kinds.index("table") < kinds.index("column")

    def test_a_finding_carries_its_delta_detail(self, model):
        row = next(r for r in model.finding_rows() if r.path == "cumo-invoicing.invoice.number")
        assert "varchar(40)" in row.detail
        assert "text" in row.detail

    def test_suppressed_findings_name_the_rule(self, model):
        suppressed = [row for row in model.finding_rows() if row.suppressed_by]
        assert suppressed
        assert suppressed[0].suppressed_by == "quartz-runtime"


class TestFiltering:
    def test_the_needle_matches_a_path_case_insensitively(self, model):
        rows = model.finding_rows(needle="DUNNING")
        assert rows
        assert all("dunning" in row.path.lower() for row in rows)

    def test_severity_toggles_narrow_the_rows(self, model):
        only_errors = model.finding_rows(severities=frozenset({"error"}))
        assert only_errors
        assert all(row.severity == "error" for row in only_errors)

    def test_an_empty_severity_set_shows_nothing(self, model):
        assert model.finding_rows(severities=frozenset()) == []

    def test_a_needle_matching_nothing_shows_nothing(self, model):
        assert model.finding_rows(needle="zzz-no-such-object") == []


class TestChangelog:
    def test_the_changelog_lines_lead_with_the_headline(self, model):
        from db_schema_comparer.diff.changelog import ChangelogDiff, ChangelogStatus
        from db_schema_comparer.diff.model import ComparisonReport, TargetDiff
        from db_schema_comparer.diff.severity import Severity
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
                    changelog=ChangelogDiff(
                        status=ChangelogStatus.TARGET_BEHIND,
                        severity=Severity.ERROR,
                        missing_in_target=(("add-index", "kolowae"),),
                        first_divergence=("add-index", "kolowae"),
                        master_tag="R7.6.2",
                    ),
                ),
            ),
        )
        lines = ResultsModel(report).changelog_lines("qa")
        assert "1 changeset(s) behind" in lines[0]
        assert any("diverge at" in line for line in lines)

    def test_a_target_without_a_changelog_has_no_lines(self, model):
        assert model.changelog_lines("qa") == []


class TestWritingReports:
    def test_all_three_reports_are_written(self, model, tmp_path):
        written = model.write_reports(tmp_path)
        names = {path.name for path in written}
        assert names == {"report.json", "junit.xml", "report.html"}
        assert all(path.exists() for path in written)

    def test_the_json_report_matches_the_reporter(self, model, tmp_path):
        model.write_reports(tmp_path)
        payload = json.loads((tmp_path / "report.json").read_text())
        assert payload["name"] == "invoicing"

    def test_the_html_report_is_self_contained(self, model, tmp_path):
        model.write_reports(tmp_path)
        html = (tmp_path / "report.html").read_text()
        for pattern in ("http://", "https://", "//cdn"):
            assert pattern not in html

    def test_no_report_contains_a_credential(self, tmp_path, monkeypatch):
        from dataclasses import replace

        from tests.support.builders import source
        from tests.support.reports import drifted_target

        # Distinctive values, so the assertions can fail: the labels prove the fixture reached the
        # files, and the DSN in the environment proves nothing is read from there.
        monkeypatch.setenv("CUMO_SECRET_DSN", "postgresql://svc:hunter2-7731@db.internal/inv")
        target = replace(
            drifted_target(),
            target_label="qa-marker-4471",
            target_source=source("qa-marker-4471"),
        )
        report = full_report()
        report = replace(report, targets=(target, *report.targets[1:]))
        for path in ResultsModel(report).write_reports(tmp_path):
            text = path.read_text()
            assert "qa-marker-4471" in text
            for secret in ("hunter2-7731", "CUMO_SECRET_DSN", "db.internal"):
                assert secret not in text


# -- The verdict must never call a partial comparison clean ---------------------------------


def _report(*targets, probe_failed=False, notes=(), fail_on="error"):
    from db_schema_comparer.diff.model import ComparisonReport

    return ComparisonReport(
        name="invoicing",
        master_label="prod",
        targets=tuple(targets),
        notes=tuple(notes),
        fail_on=fail_on,
        probe_failed=probe_failed,
    )


def _html_text(report) -> str:
    import io

    from db_schema_comparer.report.html import HtmlReporter

    out = io.StringIO()
    HtmlReporter().render(report, out)
    return out.getvalue()


def _console_text(report) -> str:
    import io

    from db_schema_comparer.report.console import ConsoleReporter

    out = io.StringIO()
    ConsoleReporter().render(report, out)
    return out.getvalue()


class TestVerdictPrecedence:
    def test_probe_failure_outranks_a_clean_looking_report(self):
        from tests.support.reports import in_sync_target

        level, sentence = ResultsModel(_report(in_sync_target(), probe_failed=True)).verdict
        assert level == "error"
        assert "incomplete" in sentence

    def test_a_skipped_target_alone_is_never_clean(self):
        from tests.support.reports import in_sync_target, skipped_target

        # probe_failed deliberately left False: the targets say it even if the flag does not.
        level, sentence = ResultsModel(_report(in_sync_target(), skipped_target())).verdict
        assert level == "error"
        assert "No differences" not in sentence
        assert "local" in sentence

    def test_a_skipped_target_outranks_info_only_drift(self):
        from tests.support.reports import skipped_target

        level, _ = ResultsModel(_report(skipped_target(), probe_failed=True)).verdict
        assert level == "error"

    def test_drift_reports_its_worst_severity(self):
        from tests.support.reports import drifted_target

        level, sentence = ResultsModel(_report(drifted_target())).verdict
        assert level == "error"
        assert "Drift found at error" in sentence
        assert "No differences" not in sentence

    def test_a_warning_note_alone_is_not_clean(self):
        from db_schema_comparer.diff.model import Note, NoteKind
        from db_schema_comparer.diff.severity import Severity
        from tests.support.reports import in_sync_target

        note = Note(NoteKind.TARGET_EMPTY, "target is empty", Severity.WARNING)
        level, sentence = ResultsModel(_report(in_sync_target(), notes=[note])).verdict
        assert level == "warning"
        assert "No differences" not in sentence

    def test_a_liquibase_problem_alone_is_not_clean(self):
        from dataclasses import replace

        from db_schema_comparer.diff.changelog import ChangelogDiff, ChangelogStatus
        from db_schema_comparer.diff.severity import Severity
        from tests.support.reports import in_sync_target

        target = replace(
            in_sync_target(),
            changelog=ChangelogDiff(
                status=ChangelogStatus.TARGET_BEHIND,
                severity=Severity.ERROR,
                missing_in_target=(("a", "b"),),
            ),
        )
        level, _ = ResultsModel(_report(target)).verdict
        assert level == "error"
        assert "liquibase" in ResultsModel(_report(target)).target_rows()[0]["summary"]

    @pytest.mark.parametrize("which", ["full", "skipped_only", "drift", "warning_note"])
    def test_the_sentence_is_the_one_the_html_report_shows(self, which):
        from db_schema_comparer.diff.model import Note, NoteKind
        from db_schema_comparer.diff.severity import Severity
        from tests.support.reports import drifted_target, in_sync_target, skipped_target

        report = {
            "full": full_report(),
            "skipped_only": _report(in_sync_target(), skipped_target(), probe_failed=True),
            "drift": _report(drifted_target()),
            "warning_note": _report(
                in_sync_target(), notes=[Note(NoteKind.TARGET_EMPTY, "empty", Severity.WARNING)]
            ),
        }[which]
        _, sentence = ResultsModel(report).verdict
        # The appended "Not inspected" list is ours alone; the leading sentence is shared.
        assert sentence.split(" Not inspected")[0] in _html_text(report)

    def test_the_console_agrees_on_partial_and_drifted_reports(self):
        from tests.support.reports import drifted_target

        for report in (full_report(), _report(drifted_target())):
            _, sentence = ResultsModel(report).verdict
            assert sentence.split(" Not inspected")[0] in _console_text(report)

    def test_the_clean_report_is_clean_in_the_html_too(self):
        _, sentence = ResultsModel(clean_report()).verdict
        assert sentence in _html_text(clean_report())


class TestTargetSummary:
    def test_a_skipped_target_never_reads_as_in_sync(self, model):
        summary = model.target_rows()[2]["summary"]
        assert "in sync" not in summary
        assert "skipped" in summary

    def test_a_target_with_only_a_warning_note_is_not_in_sync(self):
        from db_schema_comparer.diff.model import Note, NoteKind, TargetDiff
        from db_schema_comparer.diff.severity import Severity
        from tests.support.builders import source

        target = TargetDiff(
            master_label="prod",
            target_label="qa",
            master_source=source("prod"),
            target_source=source("qa"),
            notes=(Note(NoteKind.TARGET_EMPTY, "empty", Severity.WARNING),),
        )
        summary = ResultsModel(_report(target)).target_rows()[0]["summary"]
        assert "in sync" not in summary
        assert "target empty" in summary

    def test_a_clean_target_says_in_sync(self):
        assert ResultsModel(clean_report()).target_rows()[0]["summary"] == "in sync"


class TestWriteFailures:
    def test_a_failed_write_names_the_file_and_leaves_nothing_behind(self, model, tmp_path):
        from db_schema_comparer.gui.errors import GuiError

        blocker = tmp_path / "file"
        blocker.write_text("x")
        with pytest.raises(GuiError, match=r"report\.json"):
            model.write_reports(blocker / "sub")
        assert blocker.read_text() == "x"

    def test_no_partial_files_remain_after_success(self, model, tmp_path):
        model.write_reports(tmp_path)
        assert not list(tmp_path.glob("*.partial"))

    def test_html_path_is_where_the_html_report_goes(self, model, tmp_path):
        assert model.html_path(tmp_path) == tmp_path / "report.html"

    def _failing_html(self, monkeypatch, exc):
        from db_schema_comparer.report.html import HtmlReporter

        def boom(self, report, out):
            out.write("half a report")
            raise exc

        monkeypatch.setattr(HtmlReporter, "render", boom)

    def test_a_late_failure_keeps_the_previous_set_intact(self, model, tmp_path, monkeypatch):
        from db_schema_comparer.gui.errors import GuiError

        for name in ("report.json", "junit.xml", "report.html"):
            (tmp_path / name).write_text("OLD")
        self._failing_html(monkeypatch, OSError(28, "No space left on device"))

        with pytest.raises(GuiError, match="No space left"):
            model.write_reports(tmp_path)

        # All or nothing: JSON and JUnit rendered fine, yet nothing replaced the old generation.
        assert {p.read_text() for p in tmp_path.iterdir()} == {"OLD"}
        assert not list(tmp_path.glob("*.partial"))

    def test_a_non_os_failure_leaks_no_text_and_no_partial(self, model, tmp_path, monkeypatch):
        from db_schema_comparer.gui.errors import GuiError

        self._failing_html(monkeypatch, ValueError("secret-dsn-xyz"))
        with pytest.raises(GuiError) as caught:
            model.write_reports(tmp_path)
        assert "secret-dsn-xyz" not in str(caught.value)
        assert str(tmp_path) not in str(caught.value)
        assert list(tmp_path.iterdir()) == []

    def test_a_lone_surrogate_becomes_a_gui_error(self, tmp_path):
        from dataclasses import replace

        from db_schema_comparer.gui.errors import GuiError

        report = full_report()
        drifted = report.targets[0]
        finding = drifted.findings[1]
        delta = replace(finding.deltas[0], target_value="x\udc80y")
        bad = replace(finding, deltas=(delta,))
        drifted = replace(drifted, findings=(drifted.findings[0], bad, *drifted.findings[2:]))
        report = replace(report, targets=(drifted, *report.targets[1:]))

        with pytest.raises(GuiError) as caught:
            ResultsModel(report).write_reports(tmp_path)
        assert "udc80" not in str(caught.value)
        assert str(tmp_path) not in str(caught.value)
        assert list(tmp_path.iterdir()) == []


class TestRowDetails:
    def test_a_skipped_target_row_is_an_error(self, model):
        rows = model.target_rows()
        assert [r["severity"] for r in rows] == ["error", "ok", "error"]

    def test_the_finding_count_is_reported(self, model):
        assert model.target_rows()[0]["findings"] == 5
        assert model.target_rows()[0]["suppressed"] == 1

    def test_suppressed_findings_are_counted_in_the_summary(self):
        from dataclasses import replace

        from tests.support.reports import drifted_target, in_sync_target

        target = replace(in_sync_target(), ignored=drifted_target().ignored)
        summary = ResultsModel(_report(target)).target_rows()[0]["summary"]
        assert summary == "in sync (1 suppressed)"

    def test_a_delta_note_is_part_of_the_detail(self, model):
        row = next(r for r in model.finding_rows() if r.path.endswith("invoice.number"))
        assert "a type change breaks anything relying on the length" in row.detail

    def test_a_renamed_object_says_what_it_matches(self):
        from dataclasses import replace

        from db_schema_comparer.model.keys import table_key
        from tests.support.reports import drifted_target

        drifted = drifted_target()
        paired = replace(drifted.findings[0], paired_with=table_key("cumo-invoicing", "Dunning2"))
        drifted = replace(drifted, findings=(paired,))
        rows = ResultsModel(_report(drifted)).finding_rows()
        assert "matches Dunning2" in rows[0].detail


class TestSeverityNames:
    def test_severity_names_are_case_insensitive(self, model):
        assert model.finding_rows(severities=frozenset({"ERROR"})) == model.finding_rows(
            severities=frozenset({"error"})
        )
        assert model.finding_rows(severities=frozenset({"ERROR"}))


class TestChangelogLinesCompleteness:
    def test_a_filename_difference_is_shown_at_its_severity(self):
        from db_schema_comparer.diff.changelog import diff_changelog
        from tests.support.builders import changelog, changeset

        diff = diff_changelog(
            changelog(changeset("a", filename="db/a.xml")),
            changelog(changeset("a", filename="other/a.xml")),
        )
        from dataclasses import replace

        from tests.support.reports import in_sync_target

        report = _report(replace(in_sync_target(), changelog=diff))
        lines = ResultsModel(report).changelog_lines("dev")
        assert any("db/a.xml vs other/a.xml" in line and "(info)" in line for line in lines)

    def test_a_failure_names_its_side(self):
        from dataclasses import replace

        from db_schema_comparer.diff.changelog import diff_changelog
        from tests.support.builders import changelog, changeset
        from tests.support.reports import in_sync_target

        diff = diff_changelog(
            changelog(changeset("a", exec_type="FAILED")), changelog(changeset("a"))
        )
        report = _report(replace(in_sync_target(), changelog=diff))
        lines = ResultsModel(report).changelog_lines("dev")
        assert "FAILED on prod" in lines[0]
        assert any(line.endswith("recorded as FAILED on prod") for line in lines)


class TestFindingStatus:
    """Telling "this finding has no deltas" from "there is no such finding".

    ``delta_rows`` returns an empty list for both, and the detail pane treating them alike made
    clicking a missing or extra object — the commonest finding there is — read as a no-op.
    """

    MISSING = ("qa", "table", "cumo-invoicing.DunningLevel")
    EXTRA = ("qa", "table", "public.tmp_debug")
    DIFFERS = ("qa", "column", "cumo-invoicing.invoice.number")

    def test_a_missing_object_resolves_with_no_deltas(self, model):
        assert model.finding_status(*self.MISSING) == "missing in target"
        assert model.delta_rows(*self.MISSING) == []

    def test_an_extra_object_resolves_with_no_deltas(self, model):
        assert model.finding_status(*self.EXTRA) == "extra in target"
        assert model.delta_rows(*self.EXTRA) == []

    def test_a_differing_object_resolves_and_has_deltas(self, model):
        assert model.finding_status(*self.DIFFERS) == "differs"
        assert model.delta_rows(*self.DIFFERS) != []

    def test_an_unknown_identity_does_not_resolve(self, model):
        assert model.finding_status("qa", "table", "public.nope") is None
        assert model.finding_status("nope", "table", "public.tmp_debug") is None
        assert model.finding_status("qa", "view", "public.tmp_debug") is None

    def test_an_identity_the_filter_hides_does_not_resolve(self, model):
        assert model.finding_status(*self.MISSING, needle="tmp_debug") is None
        assert model.finding_status(*self.MISSING, severities=frozenset({"warning"})) is None
        assert model.finding_status(*self.MISSING, severities=frozenset({"error"})) is not None
