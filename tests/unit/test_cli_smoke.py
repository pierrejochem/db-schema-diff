"""Phase 0 smoke tests: the package imports, the entry point runs, exit codes are stable."""

import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from db_schema_diff import __version__
from db_schema_diff.cli import cli
from db_schema_diff.diff.model import ComparisonReport
from db_schema_diff.diff.severity import Severity
from db_schema_diff.errors import ComparerError, ConfigError, ProbeError
from db_schema_diff.exit_codes import ExitCode
from db_schema_diff.report.base import render_to_path
from db_schema_diff.report.json_report import JsonReporter
from tests.support.reports import drifted_target


def test_version_flag_reports_the_package_version():
    result = CliRunner().invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_help_lists_the_group_description():
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "Compare a master PostgreSQL schema" in result.output


def test_exit_code_contract_is_stable():
    # CI pipelines branch on these numbers; changing one is a breaking change.
    assert (ExitCode.OK, ExitCode.DRIFT, ExitCode.CONFIG_ERROR) == (0, 1, 2)
    assert (ExitCode.PROBE_ERROR, ExitCode.INTERNAL_ERROR, ExitCode.INTERRUPTED) == (3, 4, 130)


def test_errors_carry_their_exit_code():
    assert ConfigError().exit_code is ExitCode.CONFIG_ERROR
    assert ProbeError().exit_code is ExitCode.PROBE_ERROR
    assert ComparerError().exit_code is ExitCode.INTERNAL_ERROR


def test_no_credential_flags_exist():
    # Credentials come only from the environment, so argv can never carry one.
    help_text = CliRunner().invoke(cli, ["--help"]).output
    assert "--dsn" not in help_text
    assert "--password" not in help_text


class TestExitCodePropagation:
    """click is run with standalone_mode off, so it *returns* the code instead of exiting.

    Discarding that return value made every command exit 0 while printing that it had found
    drift — the worst possible failure for a CI gate, because it fails open.
    """

    def test_main_returns_the_code_a_command_asked_for(self, monkeypatch):
        import click

        from db_schema_diff import cli as cli_module

        @click.command()
        @click.pass_context
        def boom(ctx):
            ctx.exit(1)

        monkeypatch.setattr(cli_module, "cli", boom)
        monkeypatch.setattr("sys.argv", ["db-schema-diff"])
        assert cli_module.main() == 1

    def test_the_module_and_the_console_script_agree_on_a_drifting_comparison(self, tmp_path):
        """Both entry points, run for real, because the exit code *is* the contract.

        ``python -m db_schema_diff`` discarded main()'s return value and exited 0 on drift
        while the console script exited 1. Nothing in-process could see it: it is the ``if
        __name__`` block itself that is wrong, so it only shows in a subprocess.
        """
        report = ComparisonReport(
            name="invoicing",
            master_label="prod",
            targets=(drifted_target(),),
            generated_at="2026-01-15T09:30:00+00:00",
            tool_version=__version__,
            fail_on="error",
        )
        path = tmp_path / "report.json"
        render_to_path(JsonReporter(), report, path)
        assert report.has_drift(Severity.ERROR), "the fixture must drift for this to mean anything"

        arguments = ["render", "--from", str(path), "--no-console"]
        script = Path(sys.executable).parent / "db-schema-diff"
        if not script.exists():  # pragma: no cover - an editable install always has it
            pytest.skip("the console script is not installed in this environment")

        def exit_code(*argv: str) -> int:
            return subprocess.run(  # noqa: S603 - fixed argv, no shell
                [*argv, *arguments], capture_output=True, text=True, timeout=120, check=False
            ).returncode

        module = exit_code(sys.executable, "-m", "db_schema_diff")
        console = exit_code(str(script))
        assert (module, console) == (ExitCode.DRIFT, ExitCode.DRIFT)

    def test_main_returns_zero_when_a_command_succeeds(self, monkeypatch):
        import click

        from db_schema_diff import cli as cli_module

        @click.command()
        def fine():
            click.echo("ok")

        monkeypatch.setattr(cli_module, "cli", fine)
        monkeypatch.setattr("sys.argv", ["db-schema-diff"])
        assert cli_module.main() == 0


class TestConsoleSummary:
    """The summary line is what a reader checks first, so it must not mislead."""

    def _report(self):
        from db_schema_diff.diff.model import (
            ComparisonReport,
            ObjectFinding,
            ObjectStatus,
            TargetDiff,
        )
        from db_schema_diff.diff.severity import Severity
        from db_schema_diff.model.keys import table_key
        from tests.support.builders import source

        findings = (
            ObjectFinding(
                table_key("public", "gone"), ObjectStatus.MISSING_IN_TARGET, Severity.ERROR
            ),
            ObjectFinding(
                table_key("public", "tmp_a"), ObjectStatus.EXTRA_IN_TARGET, Severity.WARNING
            ),
            ObjectFinding(
                table_key("public", "tmp_b"), ObjectStatus.EXTRA_IN_TARGET, Severity.WARNING
            ),
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
                    findings=findings,
                ),
            ),
        )

    def _render(self):
        import io

        from db_schema_diff.report.console import ConsoleReporter

        out = io.StringIO()
        ConsoleReporter().render(self._report(), out)
        return out.getvalue()

    def test_the_summary_counts_by_severity_not_by_status(self):
        # "3 extra in target" beside the word "error" reads as three errors. It is not.
        assert "1 error, 2 warning" in self._render()

    def test_the_version_heading_omits_the_packager_string(self):
        from tests.support.builders import source

        rendered = self._render()
        assert source("prod").server_version.split(" ")[0] in rendered
        assert "Debian" not in rendered


class TestEveryCommandIsWiredUp:
    """Each command's options must match its signature.

    click passes options by keyword, so adding a parameter without its decorator produces a
    ``missing required positional arguments`` TypeError at *invocation* time. Nothing catches that
    until somebody runs the command, which previously meant the integration suite rather than here.
    Rendering every command's help exercises the wiring without needing a database.
    """

    def _commands(self):
        return sorted(cli.commands)

    COMMANDS = (
        "compare",
        "diff-inventories",
        "inventory",
        "probe",
        "render",
        "validate-config",
    )

    def test_the_expected_commands_are_registered(self):
        assert self._commands() == list(self.COMMANDS)

    @pytest.mark.parametrize("command", ["compare", "diff-inventories", "inventory", "render"])
    def test_help_renders_for_every_command(self, command):
        result = CliRunner().invoke(cli, [command, "--help"])
        assert result.exit_code == 0, result.output
        assert "Usage:" in result.output

    @pytest.mark.parametrize("command", ["compare", "diff-inventories", "inventory", "render"])
    def test_every_declared_parameter_has_an_option_or_argument(self, command):
        import inspect

        callback = cli.commands[command].callback
        signature = inspect.signature(callback)
        declared = {
            name
            for name, parameter in signature.parameters.items()
            if parameter.kind is not parameter.VAR_KEYWORD and name != "ctx"
        }
        wired = {p.name for p in cli.commands[command].params}
        assert declared == wired, f"{command}: signature and options disagree"

    @pytest.mark.parametrize("command", ["compare", "diff-inventories", "inventory", "render"])
    def test_no_command_accepts_a_credential_on_the_command_line(self, command):
        names = {p.name for p in cli.commands[command].params}
        assert "dsn" not in names
        assert "password" not in names
