"""Capture now, compare later, render somewhere else.

The point of this path is that no single process ever needs credentials for two environments at
once: production is captured where production is reachable, QA where QA is, and the comparison
happens anywhere. For that to be trustworthy the offline result has to be *identical* to the
online one, so these tests compare the two byte for byte.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from db_schema_diff.exit_codes import ExitCode

pytestmark = pytest.mark.integration

CONFIG = textwrap.dedent(
    """
    version: 1
    name: invoicing
    master:
      label: prod
      dsn_env: RT_PROD_DSN
    targets:
      - label: qa
        dsn_env: RT_QA_DSN
    """
).lstrip()


@pytest.fixture
def run(tmp_path, databases):
    config = tmp_path / "invoicing.yaml"
    config.write_text(CONFIG)

    def invoke(*args: str) -> subprocess.CompletedProcess[str]:
        executable = Path(sys.executable).parent / "db-schema-diff"
        return subprocess.run(  # noqa: S603 - fixed argv, no shell
            [str(executable), *args],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "RT_PROD_DSN": databases.master_dsn,
                "RT_QA_DSN": databases.target_dsn,
                "NO_COLOR": "1",
            },
            check=False,
            timeout=120,
        )

    invoke.config = config  # type: ignore[attr-defined]
    invoke.tmp = tmp_path  # type: ignore[attr-defined]
    invoke.databases = databases  # type: ignore[attr-defined]
    return invoke


class TestInventoryCommand:
    def test_capturing_one_source_writes_a_readable_inventory(self, run):
        run.databases.setup("base")
        out = run.tmp / "prod.json"
        result = run("inventory", "-c", str(run.config), "--source", "prod", "-o", str(out))
        assert result.returncode == ExitCode.OK, result.stdout + result.stderr
        payload = json.loads(out.read_text())
        assert payload["schema_version"] == 2
        assert payload["source"]["label"] == "prod"
        assert payload["objects"]

    def test_the_inventory_carries_no_credential(self, run):
        run.databases.setup("base")
        out = run.tmp / "prod.json"
        run("inventory", "-c", str(run.config), "--source", "prod", "-o", str(out))
        text = out.read_text()
        assert "password" not in text
        assert "RT_PROD_DSN" not in text

    def test_capturing_an_unreachable_source_exits_three(self, run, monkeypatch):
        run.databases.setup("base")
        bad = run.tmp / "bad.yaml"
        bad.write_text(CONFIG.replace("RT_QA_DSN", "RT_MISSING_DSN"))
        executable = Path(sys.executable).parent / "db-schema-diff"
        result = subprocess.run(  # noqa: S603
            [
                str(executable),
                "inventory",
                "-c",
                str(bad),
                "--source",
                "qa",
                "-o",
                str(run.tmp / "x.json"),
            ],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "RT_PROD_DSN": run.databases.master_dsn,
                "RT_MISSING_DSN": "postgresql://nobody@127.0.0.1:1/absent?connect_timeout=2",
                "NO_COLOR": "1",
            },
            check=False,
            timeout=60,
        )
        assert result.returncode == ExitCode.PROBE_ERROR

    def test_an_unknown_source_label_is_a_config_error(self, run):
        run.databases.setup("base")
        result = run(
            "inventory", "-c", str(run.config), "--source", "nope", "-o", str(run.tmp / "x.json")
        )
        assert result.returncode == ExitCode.CONFIG_ERROR
        assert "nope" in result.stdout + result.stderr


class TestOfflineEqualsOnline:
    """The whole justification for the offline path: it must agree with the online one."""

    def _capture_both(self, run):
        master = run.tmp / "prod.json"
        target = run.tmp / "qa.json"
        assert (
            run("inventory", "-c", str(run.config), "--source", "prod", "-o", str(master))
        ).returncode == 0
        assert (
            run("inventory", "-c", str(run.config), "--source", "qa", "-o", str(target))
        ).returncode == 0
        return master, target

    def test_the_offline_diff_finds_the_same_drift(self, run):
        run.databases.setup("base", drift="drift_column_type")
        master, target = self._capture_both(run)

        online = run("compare", "-c", str(run.config), "--json", str(run.tmp / "online.json"))
        offline = run(
            "diff-inventories",
            str(master),
            str(target),
            "--json",
            str(run.tmp / "offline.json"),
        )

        assert online.returncode == offline.returncode == ExitCode.DRIFT
        assert _findings(run.tmp / "online.json") == _findings(run.tmp / "offline.json")

    def test_the_offline_diff_agrees_when_in_sync(self, run):
        run.databases.setup("base")
        master, target = self._capture_both(run)
        result = run("diff-inventories", str(master), str(target))
        assert result.returncode == ExitCode.OK
        assert "in sync" in result.stdout

    def test_diff_inventories_needs_no_credentials_at_all(self, run):
        run.databases.setup("base", drift="drift_nullable")
        master, target = self._capture_both(run)

        executable = Path(sys.executable).parent / "db-schema-diff"
        # A deliberately credential-free environment: this is the property that lets production
        # and QA be captured by different jobs with different secrets.
        clean_env = {k: v for k, v in os.environ.items() if not k.endswith("_DSN")} | {
            "NO_COLOR": "1"
        }
        result = subprocess.run(  # noqa: S603
            [str(executable), "diff-inventories", str(master), str(target)],
            capture_output=True,
            text=True,
            env=clean_env,
            check=False,
            timeout=60,
        )
        assert result.returncode == ExitCode.DRIFT
        assert "acme-invoicing.invoice.number" in result.stdout


class TestRenderCommand:
    def test_render_reproduces_the_exit_code_without_a_database(self, run):
        run.databases.setup("base", drift="drift_column_type")
        report = run.tmp / "report.json"
        assert run("compare", "-c", str(run.config), "--json", str(report)).returncode == 1

        clean_env_result = run("render", "--from", str(report))
        assert clean_env_result.returncode == ExitCode.DRIFT
        assert "acme-invoicing.invoice_line.position" in clean_env_result.stdout

    def test_render_produces_byte_identical_junit(self, run):
        run.databases.setup("base", drift="drift_column_type")
        report = run.tmp / "report.json"
        direct = run.tmp / "direct.xml"
        rendered = run.tmp / "rendered.xml"

        run("compare", "-c", str(run.config), "--json", str(report), "--junit", str(direct))
        run("render", "--from", str(report), "--junit", str(rendered), "--no-console")

        assert rendered.read_text() == direct.read_text()

    def test_rendering_a_report_json_reproduces_it_exactly(self, run):
        run.databases.setup("base", drift="drift_nullable")
        first = run.tmp / "first.json"
        second = run.tmp / "second.json"
        run("compare", "-c", str(run.config), "--json", str(first))
        run("render", "--from", str(first), "--json", str(second), "--no-console")
        assert second.read_text() == first.read_text()

    def test_a_malformed_report_is_a_config_error(self, run):
        broken = run.tmp / "broken.json"
        broken.write_text('{"schema_version": 1}')
        result = run("render", "--from", str(broken))
        assert result.returncode == ExitCode.CONFIG_ERROR
        assert "not a readable report" in result.stdout + result.stderr


class TestCompareOutputs:
    def test_out_dir_writes_both_machine_readable_reports(self, run):
        run.databases.setup("base", drift="drift_column_type")
        out = run.tmp / "reports"
        run("compare", "-c", str(run.config), "--out-dir", str(out))
        assert (out / "report.json").exists()
        assert (out / "junit.xml").exists()

    def test_the_junit_report_marks_the_drift_as_a_failure(self, run):
        from xml.etree import ElementTree as ET

        run.databases.setup("base", drift="drift_column_type")
        junit = run.tmp / "junit.xml"
        run("compare", "-c", str(run.config), "--junit", str(junit))
        root = ET.parse(junit).getroot()
        failures = root.findall(".//failure")
        assert len(failures) == 1
        assert "column.data_type" in failures[0].get("message")

    def test_the_config_can_name_the_output_directory(self, run):
        """So a service's reports go to the same place on every machine, with no flag to remember.

        The path is relative to the config file, which is what keeps a committed config from
        naming one person's home directory.
        """
        run.databases.setup("base")
        run.config.write_text(CONFIG + "output_dir: reports\n")
        run("compare", "-c", str(run.config))
        assert (run.tmp / "reports" / "report.json").exists()
        assert (run.tmp / "reports" / "junit.xml").exists()
        assert (run.tmp / "reports" / "report.html").exists()

    def test_the_flag_wins_over_the_config(self, run):
        """A flag is what the person asked for now; the file is what the project asked for."""
        run.databases.setup("base")
        run.config.write_text(CONFIG + "output_dir: from_config\n")
        chosen = run.tmp / "from_flag"
        run("compare", "-c", str(run.config), "--out-dir", str(chosen))
        assert (chosen / "report.json").exists()
        assert not (run.tmp / "from_config").exists()

    def test_an_absolute_path_in_the_config_is_used_as_written(self, run):
        run.databases.setup("base")
        target = run.tmp / "absolute"
        run.config.write_text(CONFIG + f"output_dir: {target}\n")
        run("compare", "-c", str(run.config))
        assert (target / "report.json").exists()

    def test_a_config_without_one_writes_nothing(self, run):
        """The behaviour every existing config must keep: no flag, no files."""
        run.databases.setup("base")
        run("compare", "-c", str(run.config))
        assert list(run.tmp.glob("**/report.json")) == []

    def test_reports_are_written_even_when_in_sync(self, run):
        run.databases.setup("base")
        out = run.tmp / "reports"
        run("compare", "-c", str(run.config), "--out-dir", str(out))
        payload = json.loads((out / "report.json").read_text())
        assert payload["worst_severity"] is None


def _findings(path: Path) -> set[tuple[str, str, str]]:
    payload = json.loads(path.read_text())
    return {
        (f["kind"], f["path"], f["status"])
        for target in payload["targets"]
        for f in target["findings"]
    }


class TestHtmlReport:
    """The standalone report, produced from a real comparison."""

    def test_compare_writes_a_self_contained_html_file(self, run):
        run.databases.setup("base", drift="drift_column_type")
        html_path = run.tmp / "report.html"
        assert run("compare", "-c", str(run.config), "--html", str(html_path)).returncode == 1

        html = html_path.read_text()
        # Readable with no network access at all, which is how a CI artifact is usually opened.
        for pattern in ("http://", "https://", "//cdn"):
            assert pattern not in html
        assert html.lstrip().startswith("<!doctype html>")
        assert "acme-invoicing.invoice_line.position" in html

    def test_out_dir_includes_the_html_report(self, run):
        run.databases.setup("base", drift="drift_column_type")
        out = run.tmp / "reports"
        run("compare", "-c", str(run.config), "--out-dir", str(out))
        assert (out / "report.html").exists()
        assert (out / "report.json").exists()
        assert (out / "junit.xml").exists()

    def test_render_reproduces_a_byte_identical_html_report(self, run):
        run.databases.setup("base", drift="drift_nullable")
        report = run.tmp / "report.json"
        direct = run.tmp / "direct.html"
        rendered = run.tmp / "rendered.html"

        run("compare", "-c", str(run.config), "--json", str(report), "--html", str(direct))
        run("render", "--from", str(report), "--html", str(rendered), "--no-console")

        # The report can be produced in one job and rendered in another, offline.
        assert rendered.read_text() == direct.read_text()

    def test_the_embedded_payload_matches_the_json_report(self, run):
        import json as json_module

        run.databases.setup("base", drift="drift_column_type")
        json_path = run.tmp / "report.json"
        html_path = run.tmp / "report.html"
        run("compare", "-c", str(run.config), "--json", str(json_path), "--html", str(html_path))

        html = html_path.read_text()
        marker = '<script type="application/json" id="report-data">'
        start = html.index(marker) + len(marker)
        embedded = json_module.loads(html[start : html.index("</script>", start)])
        assert embedded == json_module.loads(json_path.read_text())

    def test_the_html_report_leaks_no_credential(self, run):
        run.databases.setup("base", drift="drift_column_type")
        html_path = run.tmp / "report.html"
        run("compare", "-c", str(run.config), "--html", str(html_path))
        html = html_path.read_text()
        assert "postgresql://" not in html
        assert "password" not in html

    def test_a_clean_comparison_still_produces_a_report(self, run):
        run.databases.setup("base")
        html_path = run.tmp / "report.html"
        assert run("compare", "-c", str(run.config), "--html", str(html_path)).returncode == 0
        assert "No differences found" in html_path.read_text()
