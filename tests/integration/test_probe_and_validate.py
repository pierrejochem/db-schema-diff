"""``probe``, ``validate-config`` and ``--baseline`` against real databases.

``probe`` is the first thing to run against a new environment: it answers the questions that would
otherwise turn into a confusing comparison. ``validate-config`` answers them without connecting at
all.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from db_schema_comparer.exit_codes import ExitCode

pytestmark = pytest.mark.integration

CONFIG = textwrap.dedent(
    """
    version: 1
    name: invoicing
    master:
      label: prod
      dsn_env: PV_PROD_DSN
    targets:
      - label: qa
        dsn_env: PV_QA_DSN
    """
).lstrip()


@pytest.fixture
def cli(tmp_path, databases):
    config = tmp_path / "invoicing.yaml"
    config.write_text(CONFIG)

    def invoke(*args: str, **env_overrides: str) -> subprocess.CompletedProcess[str]:
        executable = Path(sys.executable).parent / "cumo-schema-diff"
        env = {
            **os.environ,
            "PV_PROD_DSN": databases.master_dsn,
            "PV_QA_DSN": databases.target_dsn,
            "NO_COLOR": "1",
        }
        env.update(env_overrides)
        return subprocess.run(  # noqa: S603 - fixed argv, no shell
            [str(executable), *args],
            capture_output=True,
            text=True,
            env=env,
            check=False,
            timeout=180,
        )

    invoke.config = config  # type: ignore[attr-defined]
    invoke.tmp = tmp_path  # type: ignore[attr-defined]
    invoke.databases = databases  # type: ignore[attr-defined]
    return invoke


class TestValidateConfig:
    def test_a_valid_config_passes_without_connecting(self, cli):
        # No credentials in the environment at all: this stage runs before anything is reachable.
        result = cli(
            "validate-config",
            "-c",
            str(cli.config),
            PV_PROD_DSN="",
            PV_QA_DSN="",
        )
        assert result.returncode == ExitCode.OK, result.stdout + result.stderr
        assert "1 config(s) valid" in result.stdout

    def test_it_lists_the_sources_and_the_rules_in_force(self, cli):
        result = cli("validate-config", "-c", str(cli.config), PV_PROD_DSN="", PV_QA_DSN="")
        assert "master  prod  ($PV_PROD_DSN)" in result.stdout
        assert "target  qa  ($PV_QA_DSN)" in result.stdout
        assert "ignore rules in force: 3" in result.stdout

    def test_check_env_passes_when_everything_is_set(self, cli):
        result = cli("validate-config", "-c", str(cli.config), "--check-env")
        assert result.returncode == ExitCode.OK
        assert "every credential is set" in result.stdout

    def test_check_env_names_every_missing_variable_at_once(self, cli):
        result = cli(
            "validate-config", "-c", str(cli.config), "--check-env", PV_PROD_DSN="", PV_QA_DSN=""
        )
        assert result.returncode == ExitCode.CONFIG_ERROR
        # One run should fix the whole environment, not one variable per run.
        assert "PV_PROD_DSN" in result.stderr
        assert "PV_QA_DSN" in result.stderr

    def test_an_invalid_config_is_reported_without_connecting(self, cli, tmp_path):
        broken = tmp_path / "broken.yaml"
        broken.write_text(CONFIG.replace("dsn_env: PV_QA_DSN", "dsn_ev: PV_QA_DSN"))
        result = cli("validate-config", "-c", str(broken))
        assert result.returncode == ExitCode.CONFIG_ERROR
        assert "dsn_ev" in result.stdout + result.stderr


class TestProbe:
    def test_it_reports_what_each_source_holds(self, cli):
        cli.databases.setup("base")
        result = cli("probe", "-c", str(cli.config))
        assert result.returncode == ExitCode.OK, result.stdout + result.stderr
        assert "prod: PostgreSQL" in result.stdout
        assert "qa: PostgreSQL" in result.stdout
        assert "cumo-invoicing" in result.stdout
        assert "schemas" in result.stdout

    def test_probe_does_not_capture_an_inventory(self, cli):
        """It answers in about a second, which is the point of a connection check.

        Capturing would make it as slow as a comparison and defeat its purpose.
        """
        cli.databases.setup("base")
        result = cli("probe", "-c", str(cli.config))
        assert result.returncode == ExitCode.OK
        assert "cumo-invoicing" in result.stdout

    def test_it_says_where_the_changelog_lives(self, cli):
        """The question that otherwise turns into a confusing comparison.

        The table is not in ``public`` here, and knowing that up front saves working it out from a
        "changelog missing in target" finding later.
        """
        cli.databases.setup("base")
        result = cli("probe", "-c", str(cli.config))
        assert "cumo-invoicing.DATABASECHANGELOG: 3 changeset(s)" in result.stdout
        assert "last tag R7.6.2" in result.stdout

    def test_it_reports_the_collation(self, cli):
        # Differing collations make every text column drift, so it is worth seeing before comparing.
        cli.databases.setup("base")
        assert "collation" in cli("probe", "-c", str(cli.config)).stdout

    def test_an_unreachable_source_exits_three(self, cli):
        cli.databases.setup("base")
        result = cli(
            "probe",
            "-c",
            str(cli.config),
            PV_QA_DSN="postgresql://nobody@127.0.0.1:1/absent?connect_timeout=2",
        )
        assert result.returncode == ExitCode.PROBE_ERROR
        assert "qa: UNREACHABLE" in result.stdout

    def test_it_warns_about_an_ambiguous_changelog(self, cli):
        cli.databases.setup("base", drift="drift_changelog_ambiguous")
        result = cli("probe", "-c", str(cli.config))
        assert "AMBIGUOUS" in result.stdout
        assert "set liquibase.schema" in result.stdout

    def test_it_warns_about_a_held_deployment_lock(self, cli):
        cli.databases.setup("base", drift="drift_changelog_locked")
        assert "lock is HELD" in cli("probe", "-c", str(cli.config)).stdout

    def test_it_leaks_no_credential(self, cli):
        cli.databases.setup("base")
        result = cli("probe", "-c", str(cli.config))
        assert "postgresql://" not in result.stdout + result.stderr


class TestBaseline:
    def test_an_approved_report_stops_its_findings_gating(self, cli):
        cli.databases.setup("base", drift="drift_column_type")
        baseline = cli.tmp / "baseline.json"

        first = cli("compare", "-c", str(cli.config), "--json", str(baseline))
        assert first.returncode == ExitCode.DRIFT

        second = cli("compare", "-c", str(cli.config), "--baseline", str(baseline))
        assert second.returncode == ExitCode.OK
        assert "1 accepted, 0 new" in second.stderr

    def test_a_new_finding_still_fails(self, cli):
        cli.databases.setup("base", drift="drift_column_type")
        baseline = cli.tmp / "baseline.json"
        cli("compare", "-c", str(cli.config), "--json", str(baseline))

        # Something else drifts after the baseline was approved.
        from tests.integration.conftest import apply_sql

        apply_sql(cli.databases.target_dsn, "drift_nullable")

        result = cli("compare", "-c", str(cli.config), "--baseline", str(baseline))
        assert result.returncode == ExitCode.DRIFT
        assert "cumo-invoicing.invoice.number" in result.stdout
        # The accepted one is no longer listed among the findings.
        assert "1 accepted, 1 new" in result.stderr

    def test_an_accepted_finding_stays_in_the_json_report(self, cli):
        import json

        cli.databases.setup("base", drift="drift_column_type")
        baseline = cli.tmp / "baseline.json"
        out = cli.tmp / "after.json"
        cli("compare", "-c", str(cli.config), "--json", str(baseline))
        cli("compare", "-c", str(cli.config), "--baseline", str(baseline), "--json", str(out))

        target = json.loads(out.read_text())["targets"][0]
        assert target["findings"] == []
        assert [f["ignored_by"] for f in target["ignored"]] == ["baseline"]

    def test_a_malformed_baseline_is_a_config_error(self, cli):
        cli.databases.setup("base")
        broken = cli.tmp / "broken.json"
        broken.write_text("{}")
        result = cli("compare", "-c", str(cli.config), "--baseline", str(broken))
        assert result.returncode == ExitCode.CONFIG_ERROR
        assert "not a readable baseline" in result.stdout + result.stderr

    def test_a_fixed_finding_is_reported_as_no_longer_occurring(self, cli):
        cli.databases.setup("base", drift="drift_column_type")
        baseline = cli.tmp / "baseline.json"
        cli("compare", "-c", str(cli.config), "--json", str(baseline))

        # Somebody fixed it, so the baseline can be regenerated smaller. Rebuild the target from
        # scratch rather than re-applying base.sql over itself.
        from tests.integration.conftest import apply_sql, reset

        reset(cli.databases.target_dsn)
        apply_sql(cli.databases.target_dsn, "base")

        result = cli("compare", "-c", str(cli.config), "--baseline", str(baseline))
        assert result.returncode == ExitCode.OK
        assert "no longer occur" in result.stderr
