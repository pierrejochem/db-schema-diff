"""Several targets at once, and the ignore ruleset, end to end.

The shape this tool is actually used in: one authoritative master and a handful of environments,
where one of them is down for reasons that have nothing to do with schema drift. The run has to
report what it could determine and be unambiguous about what it could not.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from cumo_schema_comparer.exit_codes import ExitCode
from tests.integration.conftest import MASTER_DB, TARGET_DB, apply_sql, dsn_for

pytestmark = pytest.mark.integration

UNREACHABLE = "postgresql://nobody@127.0.0.1:1/absent?connect_timeout=2"

THREE_TARGETS = textwrap.dedent(
    """
    version: 1
    name: invoicing
    master:
      label: prod
      dsn_env: MT_PROD_DSN
    targets:
      - label: qa
        dsn_env: MT_QA_DSN
      - label: dev
        dsn_env: MT_DEV_DSN
      - label: dead
        dsn_env: MT_DEAD_DSN
    """
).lstrip()

THIRD_DB = "third_db"


@pytest.fixture
def three(tmp_path, server, databases):
    """A master and three targets: qa in sync, dev drifted, dead unreachable."""
    from tests.integration.conftest import _recreate_database, reset

    _recreate_database(server, THIRD_DB)
    dev_dsn = dsn_for(server, THIRD_DB)

    config = tmp_path / "invoicing.yaml"
    config.write_text(THREE_TARGETS)

    class Fixture:
        master_dsn = dsn_for(server, MASTER_DB)
        qa_dsn = dsn_for(server, TARGET_DB)
        dev_dsn_ = dev_dsn
        path = config
        tmp = tmp_path

        def setup(self, *, dev_drift: str | None = None, qa_drift: str | None = None) -> None:
            reset(self.master_dsn)
            reset(self.qa_dsn)
            apply_sql(self.master_dsn, "base")
            apply_sql(self.qa_dsn, "base")
            apply_sql(dev_dsn, "base")
            if qa_drift:
                apply_sql(self.qa_dsn, qa_drift)
            if dev_drift:
                apply_sql(dev_dsn, dev_drift)

        def run(self, *args: str, ignores: Path | None = None) -> subprocess.CompletedProcess[str]:
            executable = Path(sys.executable).parent / "cumo-schema-diff"
            argv = [str(executable), "compare", "-c", str(config), *args]
            if ignores is not None:
                argv += ["--ignores", str(ignores)]
            return subprocess.run(  # noqa: S603 - fixed argv, no shell
                argv,
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "MT_PROD_DSN": self.master_dsn,
                    "MT_QA_DSN": self.qa_dsn,
                    "MT_DEV_DSN": dev_dsn,
                    "MT_DEAD_DSN": UNREACHABLE,
                    "NO_COLOR": "1",
                },
                check=False,
                timeout=180,
            )

    return Fixture()


class TestThreeTargetsOneUnreachable:
    """The phase's gate."""

    def test_an_unreachable_target_makes_the_run_incomplete(self, three):
        three.setup(dev_drift="drift_column_type")
        result = three.run()
        # Exit 3, not 1: a comparison that could not inspect everything must be distinguishable
        # from one that inspected everything and found drift, so CI can retry the first.
        assert result.returncode == ExitCode.PROBE_ERROR, result.stdout
        assert "incomplete" in result.stdout

    def test_allow_unreachable_lets_the_reachable_targets_decide(self, three):
        three.setup(dev_drift="drift_column_type")
        result = three.run("--allow-unreachable")
        assert result.returncode == ExitCode.DRIFT
        assert "cumo-invoicing.invoice_line.position" in result.stdout

    def test_every_target_appears_in_the_report(self, three):
        three.setup(dev_drift="drift_column_type")
        out = three.tmp / "report.json"
        three.run("--allow-unreachable", "--json", str(out))
        payload = json.loads(out.read_text())
        assert [t["target"] for t in payload["targets"]] == ["qa", "dev", "dead"]

    def test_each_target_gets_its_own_verdict(self, three):
        three.setup(dev_drift="drift_column_type")
        out = three.tmp / "report.json"
        three.run("--allow-unreachable", "--json", str(out))
        by_label = {t["target"]: t for t in json.loads(out.read_text())["targets"]}
        assert by_label["qa"]["worst_severity"] is None
        assert by_label["dev"]["worst_severity"] == "error"
        assert by_label["dead"]["failed"]

    def test_drift_on_two_targets_is_reported_separately(self, three):
        three.setup(dev_drift="drift_column_type", qa_drift="drift_nullable")
        out = three.tmp / "report.json"
        three.run("--allow-unreachable", "--json", str(out))
        by_label = {t["target"]: t for t in json.loads(out.read_text())["targets"]}
        assert {f["path"] for f in by_label["qa"]["findings"]} == {"cumo-invoicing.invoice.number"}
        assert {f["path"] for f in by_label["dev"]["findings"]} == {
            "cumo-invoicing.invoice_line.position"
        }

    def test_the_unreachable_target_is_an_error_case_in_junit(self, three):
        three.setup()
        junit = three.tmp / "junit.xml"
        three.run("--allow-unreachable", "--junit", str(junit))
        root = ET.parse(junit).getroot()
        suites = {s.get("name"): s for s in root.findall("testsuite")}
        assert int(suites["schema-drift/dead"].get("errors")) == 1
        assert int(suites["schema-drift/qa"].get("failures")) == 0

    def test_selecting_only_the_healthy_targets_needs_no_flag(self, three):
        three.setup()
        result = three.run("--target", "qa", "--target", "dev")
        assert result.returncode == ExitCode.OK

    def test_sequential_capture_gives_the_same_verdict(self, three):
        three.setup(dev_drift="drift_column_type")
        parallel = three.run("--allow-unreachable")
        sequential = three.run("--allow-unreachable", "--sequential")
        assert parallel.returncode == sequential.returncode == ExitCode.DRIFT


class TestDefaultIgnores:
    """The bundled ruleset, against the real Quartz and Liquibase objects in the fixture."""

    def test_liquibase_tables_are_suppressed_by_default(self, three):
        # base.sql creates DATABASECHANGELOG in the hyphenated schema on all three databases, so
        # this proves the default rule matches a real object rather than a contrived one.
        three.setup()
        out = three.tmp / "report.json"
        three.run("--allow-unreachable", "--json", str(out))
        payload = json.loads(out.read_text())
        qa = next(t for t in payload["targets"] if t["target"] == "qa")
        assert qa["findings"] == []

    def test_a_dropped_quartz_table_is_suppressed(self, three):
        three.setup(dev_drift="drift_quartz_dropped")
        result = three.run("--allow-unreachable")
        assert result.returncode == ExitCode.OK

    def test_the_same_drop_is_reported_without_the_defaults(self, three):
        # Proves the suppression came from the ruleset rather than from the object being invisible.
        three.setup(dev_drift="drift_quartz_dropped")
        result = three.run("--allow-unreachable", "--no-default-ignores")
        assert result.returncode == ExitCode.DRIFT
        assert "QRTZ_LOCKS" in result.stdout

    def test_show_ignored_names_the_rule_that_suppressed_each_finding(self, three):
        three.setup(dev_drift="drift_quartz_dropped")
        result = three.run("--allow-unreachable", "--show-ignored")
        assert "quartz-runtime" in result.stdout

    def test_a_suppressed_finding_is_still_in_the_json_report(self, three):
        # Suppressed is not invisible: an ignore rule stays auditable.
        three.setup(dev_drift="drift_quartz_dropped")
        out = three.tmp / "report.json"
        three.run("--allow-unreachable", "--json", str(out))
        dev = next(t for t in json.loads(out.read_text())["targets"] if t["target"] == "dev")
        assert [f["ignored_by"] for f in dev["ignored"]] == ["quartz-runtime"] * len(dev["ignored"])
        assert dev["ignored"]

    def test_a_suppressed_finding_is_a_skipped_case_in_junit(self, three):
        three.setup(dev_drift="drift_quartz_dropped")
        junit = three.tmp / "junit.xml"
        three.run("--allow-unreachable", "--junit", str(junit))
        suite = next(
            s
            for s in ET.parse(junit).getroot().findall("testsuite")
            if s.get("name") == "schema-drift/dev"
        )
        assert int(suite.get("skipped")) >= 1

    def test_a_real_object_is_not_suppressed(self, three):
        three.setup(dev_drift="drift_column_type")
        assert three.run("--allow-unreachable").returncode == ExitCode.DRIFT


class TestProjectIgnores:
    def _ignores(self, tmp_path, body: str) -> Path:
        path = tmp_path / "ignores.yaml"
        path.write_text(textwrap.dedent(body).lstrip())
        return path

    def test_a_project_rule_suppresses_a_real_finding(self, three):
        three.setup(dev_drift="drift_column_type")
        ignores = self._ignores(
            three.tmp,
            """
            version: 1
            rules:
              - id: known-type-change
                reason: dev is deliberately ahead
                names: ["cumo-invoicing.invoice_line.position"]
            """,
        )
        assert three.run("--allow-unreachable", ignores=ignores).returncode == ExitCode.OK

    def test_a_target_scoped_rule_applies_only_to_that_target(self, three):
        three.setup(dev_drift="drift_column_type", qa_drift="drift_column_type")
        ignores = self._ignores(
            three.tmp,
            """
            version: 1
            rules:
              - id: dev-only
                reason: dev is a playground
                targets: [dev]
                names: ["cumo-invoicing.*"]
            """,
        )
        result = three.run("--allow-unreachable", ignores=ignores)
        # qa still fails, dev does not.
        assert result.returncode == ExitCode.DRIFT
        out = three.tmp / "report.json"
        three.run("--allow-unreachable", "--json", str(out), ignores=ignores)
        by_label = {t["target"]: t for t in json.loads(out.read_text())["targets"]}
        assert by_label["qa"]["findings"]
        assert by_label["dev"]["findings"] == []

    def test_a_warn_action_keeps_the_finding_but_passes_the_error_gate(self, three):
        three.setup(dev_drift="drift_column_type")
        ignores = self._ignores(
            three.tmp,
            """
            version: 1
            rules:
              - id: soften
                reason: tracked separately
                names: ["cumo-invoicing.invoice_line.*"]
                action: warn
            """,
        )
        result = three.run("--allow-unreachable", ignores=ignores)
        assert result.returncode == ExitCode.OK
        # Still visible, which is the difference between warn and ignore.
        assert "cumo-invoicing.invoice_line.position" in result.stdout

    def test_the_same_warn_rule_fails_a_warning_gate(self, three):
        three.setup(dev_drift="drift_column_type")
        ignores = self._ignores(
            three.tmp,
            """
            version: 1
            rules:
              - id: soften
                names: ["cumo-invoicing.invoice_line.*"]
                action: warn
            """,
        )
        result = three.run("--allow-unreachable", "--fail-on", "warning", ignores=ignores)
        assert result.returncode == ExitCode.DRIFT

    def test_an_invalid_ruleset_is_a_config_error(self, three):
        three.setup()
        ignores = self._ignores(
            three.tmp,
            """
            version: 1
            rules:
              - id: unbounded
                reason: matches everything
            """,
        )
        result = three.run("--allow-unreachable", ignores=ignores)
        assert result.returncode == ExitCode.CONFIG_ERROR
        assert "every finding" in result.stdout + result.stderr
