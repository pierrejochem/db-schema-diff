"""The CLI against real databases, through a real process.

Run as a subprocess rather than through click's test runner, because the exit code *is* the
product here: CI pipelines branch on it, and only a real process proves what they will see.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from db_schema_diff.exit_codes import ExitCode
from tests.integration.conftest import apply_sql

pytestmark = pytest.mark.integration

CONFIG = textwrap.dedent(
    """
    version: 1
    name: invoicing
    master:
      label: prod
      dsn_env: IT_PROD_DSN
    targets:
      - label: qa
        dsn_env: IT_QA_DSN
    """
).lstrip()


def run(config_path: Path, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    executable = Path(sys.executable).parent / "db-schema-diff"
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [str(executable), "compare", "-c", str(config_path), *args],
        capture_output=True,
        text=True,
        env={**os.environ, **env, "NO_COLOR": "1"},
        check=False,
        timeout=120,
    )


@pytest.fixture
def cli(tmp_path, databases):
    config = tmp_path / "invoicing.yaml"
    config.write_text(CONFIG)

    def invoke(*args: str, **overrides: str) -> subprocess.CompletedProcess[str]:
        env = {"IT_PROD_DSN": databases.master_dsn, "IT_QA_DSN": databases.target_dsn}
        env.update(overrides)
        return run(config, *args, env=env)

    invoke.databases = databases  # type: ignore[attr-defined]
    return invoke


class TestExitCodes:
    def test_in_sync_exits_zero(self, cli):
        cli.databases.setup("base")
        result = cli()
        assert result.returncode == ExitCode.OK, result.stdout + result.stderr
        assert "No drift at or above error" in result.stdout

    def test_drift_exits_one(self, cli):
        cli.databases.setup("base", drift="drift_column_type")
        result = cli()
        assert result.returncode == ExitCode.DRIFT
        assert "acme-invoicing.invoice_line.position" in result.stdout

    def test_a_warning_alone_does_not_fail_the_default_gate(self, cli):
        cli.databases.setup("base", drift="drift_extra_table")
        result = cli()
        assert result.returncode == ExitCode.OK
        assert "public.tmp_debug" in result.stdout

    def test_fail_on_warning_promotes_it_to_a_failure(self, cli):
        cli.databases.setup("base", drift="drift_extra_table")
        assert cli("--fail-on", "warning").returncode == ExitCode.DRIFT

    def test_fail_on_never_always_exits_zero(self, cli):
        cli.databases.setup("base", drift="drift_column_type")
        assert cli("--fail-on", "never").returncode == ExitCode.OK

    def test_an_unreachable_target_exits_three_not_one(self, cli):
        # A probe failure must never be reported as a clean gate, and must be distinguishable
        # from drift so CI can retry it.
        cli.databases.setup("base", drift="drift_column_type")
        result = cli(IT_QA_DSN="postgresql://nobody@127.0.0.1:1/absent?connect_timeout=2")
        assert result.returncode == ExitCode.PROBE_ERROR
        assert "SKIPPED" in result.stdout
        assert "incomplete" in result.stdout

    def test_allow_unreachable_still_exits_three_when_nothing_was_compared(self, cli):
        # Nothing was inspected, so there is no "in sync" verdict to give. Exiting 0 here would
        # report success about a comparison that never happened.
        cli.databases.setup("base", drift="drift_column_type")
        result = cli(
            "--allow-unreachable",
            IT_QA_DSN="postgresql://nobody@127.0.0.1:1/absent?connect_timeout=2",
        )
        assert result.returncode == ExitCode.PROBE_ERROR

    def test_a_missing_credential_exits_two(self, cli):
        cli.databases.setup("base")
        result = cli(IT_QA_DSN="")
        assert result.returncode == ExitCode.CONFIG_ERROR
        assert "IT_QA_DSN" in result.stderr


class TestOutputSafety:
    def test_no_credential_appears_anywhere_in_the_output(self, cli):
        cli.databases.setup("base", drift="drift_column_type")
        result = cli()
        combined = result.stdout + result.stderr
        # The container's password, whatever it is, must not be echoed back.
        for dsn in (cli.databases.master_dsn, cli.databases.target_dsn):
            if "password=" in dsn:
                secret = dsn.split("password=")[1].split("&")[0].split(" ")[0]
                assert secret not in combined
            if "://" in dsn and "@" in dsn:
                credentials = dsn.split("://")[1].split("@")[0]
                if ":" in credentials:
                    assert credentials.split(":", 1)[1] not in combined

    def test_a_failed_connection_reports_host_but_not_the_dsn(self, cli):
        cli.databases.setup("base")
        result = cli(IT_QA_DSN="postgresql://u:hunter2@127.0.0.1:1/absent?connect_timeout=2")
        combined = result.stdout + result.stderr
        assert "hunter2" not in combined
        assert "127.0.0.1" in combined


class TestReadOnly:
    def test_the_target_is_untouched_by_a_comparison(self, cli):
        cli.databases.setup("base")
        before = cli.databases.query(
            cli.databases.target_dsn,
            "SELECT count(*) AS n FROM information_schema.tables WHERE table_schema = 'public'",
        )
        cli()
        after = cli.databases.query(
            cli.databases.target_dsn,
            "SELECT count(*) AS n FROM information_schema.tables WHERE table_schema = 'public'",
        )
        assert before == after


class TestTargetSelection:
    def test_an_unknown_target_label_is_a_config_error(self, cli):
        cli.databases.setup("base")
        result = cli("--target", "staging")
        assert result.returncode != ExitCode.OK
        assert "staging" in result.stdout + result.stderr


class TestSequential:
    def test_sequential_capture_produces_the_same_verdict(self, cli):
        cli.databases.setup("base", drift="drift_nullable")
        parallel = cli()
        sequential = cli("--sequential")
        assert parallel.returncode == sequential.returncode == ExitCode.DRIFT


TWO_TARGETS = textwrap.dedent(
    """
    version: 1
    name: invoicing
    master:
      label: prod
      dsn_env: IT_PROD_DSN
    targets:
      - label: qa
        dsn_env: IT_QA_DSN
      - label: dead
        dsn_env: IT_DEAD_DSN
    """
).lstrip()

UNREACHABLE = "postgresql://nobody@127.0.0.1:1/absent?connect_timeout=2"


class TestPartialReachability:
    """With several targets, one unreachable environment must not hide the others."""

    @pytest.fixture
    def cli_two(self, tmp_path, databases):
        config = tmp_path / "two.yaml"
        config.write_text(TWO_TARGETS)

        def invoke(*args: str) -> subprocess.CompletedProcess[str]:
            return run(
                config,
                *args,
                env={
                    "IT_PROD_DSN": databases.master_dsn,
                    "IT_QA_DSN": databases.target_dsn,
                    "IT_DEAD_DSN": UNREACHABLE,
                },
            )

        invoke.databases = databases  # type: ignore[attr-defined]
        return invoke

    def test_without_the_flag_one_dead_target_makes_the_run_incomplete(self, cli_two):
        cli_two.databases.setup("base")
        assert cli_two().returncode == ExitCode.PROBE_ERROR

    def test_with_the_flag_the_reachable_targets_decide_the_verdict(self, cli_two):
        cli_two.databases.setup("base", drift="drift_column_type")
        result = cli_two("--allow-unreachable")
        assert result.returncode == ExitCode.DRIFT
        assert "SKIPPED" in result.stdout
        assert "acme-invoicing.invoice_line.position" in result.stdout

    def test_with_the_flag_reachable_targets_in_sync_exit_zero(self, cli_two):
        cli_two.databases.setup("base")
        assert cli_two("--allow-unreachable").returncode == ExitCode.OK

    def test_selecting_only_the_healthy_target_needs_no_flag(self, cli_two):
        cli_two.databases.setup("base")
        assert cli_two("--target", "qa").returncode == ExitCode.OK


def test_excluding_a_schema_removes_its_findings(cli):
    apply_sql(cli.databases.master_dsn, "base")
    apply_sql(cli.databases.target_dsn, "base")
    apply_sql(cli.databases.target_dsn, "drift_column_type")
    assert cli().returncode == ExitCode.DRIFT
    assert cli("--exclude-schema", "acme-invoicing").returncode == ExitCode.OK
