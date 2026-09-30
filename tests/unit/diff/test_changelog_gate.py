"""A failed migration or a held lock must reach the exit code.

The changelog verdict once reported IN_SYNC while carrying an ERROR, and the model dropped the
severity because it only looked at the status. A CI gate therefore passed a failed deployment.
These tests go through the CLI, because the exit code is the surface that failed.
"""

from __future__ import annotations

import io
from dataclasses import replace

import pytest
from click.testing import CliRunner

from cumo_schema_comparer.cli import cli
from cumo_schema_comparer.diff.changelog import ChangelogDiff, ChangelogStatus, diff_changelog
from cumo_schema_comparer.diff.model import ComparisonReport
from cumo_schema_comparer.diff.severity import Severity, gate
from cumo_schema_comparer.exit_codes import ExitCode
from cumo_schema_comparer.gui.results_vm import ResultsModel
from cumo_schema_comparer.report.console import ConsoleReporter
from cumo_schema_comparer.report.html import HtmlReporter
from cumo_schema_comparer.report.json_report import JsonReporter
from tests.support.builders import changelog, changeset
from tests.support.reports import in_sync_target


def _failed() -> ChangelogDiff:
    return diff_changelog(changelog(changeset("a")), changelog(changeset("a", exec_type="FAILED")))


def _locked() -> ChangelogDiff:
    return diff_changelog(changelog(changeset("a")), changelog(changeset("a"), lock_held=True))


def _report(diff: ChangelogDiff, fail_on: str) -> ComparisonReport:
    target = replace(in_sync_target(), changelog=diff)
    return ComparisonReport(
        name="invoicing", master_label="prod", targets=(target,), fail_on=fail_on
    )


CASES = [
    pytest.param(_failed, "error", ChangelogStatus.FAILED_CHANGESETS, id="failed-changeset"),
    pytest.param(_locked, "warning", ChangelogStatus.LOCK_HELD, id="held-lock"),
]


class TestVerdictNeverSyncWithSeverity:
    @pytest.mark.parametrize(("make", "fail_on", "status"), CASES)
    def test_the_status_tells_the_truth(self, make, fail_on, status):
        diff = make()
        assert diff.status is status
        assert diff.severity > Severity.INFO

    def test_a_clean_history_is_still_in_sync(self):
        diff = diff_changelog(changelog(changeset("a")), changelog(changeset("a")))
        assert diff.status is ChangelogStatus.IN_SYNC

    @pytest.mark.parametrize(
        "status", [s for s in ChangelogStatus if s is not ChangelogStatus.MISSING_IN_BOTH]
    )
    def test_the_model_counts_any_severity_above_info_whatever_the_status(self, status):
        # The structural guard: even a wrongly-labelled IN_SYNC cannot drop its severity.
        diff = ChangelogDiff(status=status, severity=Severity.ERROR)
        target = replace(in_sync_target(), changelog=diff)
        assert target.worst_severity() is Severity.ERROR

    def test_an_in_sync_verdict_at_info_is_not_drift(self):
        diff = ChangelogDiff(status=ChangelogStatus.IN_SYNC, severity=Severity.INFO)
        assert replace(in_sync_target(), changelog=diff).worst_severity() is None


class TestGate:
    @pytest.mark.parametrize(("make", "fail_on", "status"), CASES)
    def test_has_drift_at_the_matching_threshold(self, make, fail_on, status):
        assert _report(make(), fail_on).has_drift(gate(fail_on))

    @pytest.mark.parametrize(("make", "fail_on", "status"), CASES)
    def test_the_cli_exits_non_zero(self, make, fail_on, status, tmp_path):
        path = tmp_path / "report.json"
        out = io.StringIO()
        JsonReporter().render(_report(make(), fail_on), out)
        path.write_text(out.getvalue(), encoding="utf-8")

        result = CliRunner().invoke(cli, ["render", "--from", str(path), "--no-console"])
        assert result.exit_code == ExitCode.DRIFT


class TestNeverReadsAsClean:
    @pytest.mark.parametrize(("make", "fail_on", "status"), CASES)
    def test_every_rendering_agrees(self, make, fail_on, status):
        report = _report(make(), fail_on)
        model = ResultsModel(report)

        level, sentence = model.verdict
        assert level != "ok"
        assert "No differences" not in sentence
        assert model.target_rows()[0]["summary"] != "in sync"
        assert model.target_rows()[0]["severity"] != "ok"

        html = io.StringIO()
        HtmlReporter().render(report, html)
        assert "No differences found" not in html.getvalue()

        console = io.StringIO()
        ConsoleReporter().render(report, console)
        assert "No drift at or above" not in console.getvalue()
        assert "in sync" not in console.getvalue().split("Summary")[1]
