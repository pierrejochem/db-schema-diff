"""``validate-config``, which never opens a connection.

That is the point of it: a pipeline can check its configuration in a stage that runs long before any
database is reachable, and a hand-edited file can be checked in a second.
"""

from __future__ import annotations

import contextlib
import io
import textwrap
from dataclasses import dataclass

import pytest

from db_schema_comparer import cli as cli_module
from db_schema_comparer.exit_codes import ExitCode

CONFIG = textwrap.dedent(
    """
    version: 1
    name: invoicing
    master:
      label: prod
      dsn_env: VC_PROD_DSN
    targets:
      - label: qa
        dsn_env: VC_QA_DSN
      - label: dev
        dsn_env: VC_DEV_DSN
        schema_map: { "acme-invoicing": invoicing_dev }
    """
).lstrip()


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "invoicing.yaml"
    path.write_text(CONFIG)
    return path


@dataclass(frozen=True)
class Run:
    """What a command printed and what it exited with."""

    exit_code: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        return self.stdout + self.stderr


def run(*args, env=None) -> Run:
    """Invoke the real entry point.

    Through ``main()`` rather than click's test runner, because translating an exception into an
    exit code happens there. Going straight to the group tests the parsing and misses the contract.
    """
    import os
    from unittest import mock

    argv = ["db-schema-diff", "validate-config", *args]
    stdout, stderr = io.StringIO(), io.StringIO()
    environment = {k: v for k, v in os.environ.items() if not k.startswith("VC_")}
    environment.update(env or {})

    with (
        mock.patch.object(cli_module.sys, "argv", argv),
        mock.patch.dict(os.environ, environment, clear=True),
        contextlib.redirect_stdout(stdout),
        contextlib.redirect_stderr(stderr),
    ):
        code = cli_module.main()
    return Run(exit_code=code, stdout=stdout.getvalue(), stderr=stderr.getvalue())


class TestValidConfiguration:
    def test_it_passes_with_no_credentials_present(self, config):
        result = run("-c", str(config))
        assert result.exit_code == ExitCode.OK, result.output
        assert "1 config(s) valid" in result.output

    def test_it_lists_every_source_and_its_variable(self, config):
        output = run("-c", str(config)).output
        assert "master  prod  ($VC_PROD_DSN)" in output
        assert "target  qa  ($VC_QA_DSN)" in output
        assert "target  dev  ($VC_DEV_DSN)" in output

    def test_it_shows_a_schema_map(self, config):
        # Easy to get backwards, so worth printing back.
        assert "schema_map" in run("-c", str(config)).output

    def test_it_reports_how_many_ignore_rules_are_in_force(self, config):
        # Including the bundled defaults, which a reader may not know are there.
        assert "ignore rules in force: 3" in run("-c", str(config)).output

    def test_a_directory_validates_every_config_in_it(self, tmp_path):
        directory = tmp_path / "configs"
        directory.mkdir()
        (directory / "a.yaml").write_text(CONFIG)
        (directory / "b.yaml").write_text(CONFIG.replace("invoicing", "payment"))
        result = run("-c", str(directory))
        assert result.exit_code == ExitCode.OK
        assert "2 config(s) valid" in result.output


class TestInvalidConfiguration:
    def test_a_typo_in_a_key_is_reported(self, tmp_path):
        path = tmp_path / "broken.yaml"
        path.write_text(CONFIG.replace("dsn_env: VC_QA_DSN", "dsn_ev: VC_QA_DSN"))
        result = run("-c", str(path))
        assert result.exit_code == ExitCode.CONFIG_ERROR
        assert "dsn_ev" in result.output

    def test_a_duplicate_label_is_reported(self, tmp_path):
        path = tmp_path / "dup.yaml"
        path.write_text(CONFIG.replace("label: dev", "label: qa"))
        result = run("-c", str(path))
        assert result.exit_code == ExitCode.CONFIG_ERROR
        assert "duplicate" in result.output

    def test_a_missing_file_is_reported(self, tmp_path):
        result = run("-c", str(tmp_path / "absent.yaml"))
        assert result.exit_code == ExitCode.CONFIG_ERROR

    def test_an_invalid_ignore_ruleset_is_reported(self, tmp_path):
        (tmp_path / "ignores.yaml").write_text("version: 1\nrules:\n  - id: unbounded\n")
        path = tmp_path / "invoicing.yaml"
        path.write_text(CONFIG + "ignores_file: ignores.yaml\n")
        result = run("-c", str(path))
        assert result.exit_code == ExitCode.CONFIG_ERROR
        assert "every finding" in result.output


class TestCheckEnv:
    def test_it_passes_when_every_variable_is_set(self, config):
        env = {
            "VC_PROD_DSN": "postgresql://u:p@h/db",
            "VC_QA_DSN": "postgresql://u:p@h/db",
            "VC_DEV_DSN": "postgresql://u:p@h/db",
        }
        result = run("-c", str(config), "--check-env", env=env)
        assert result.exit_code == ExitCode.OK
        assert "every credential is set" in result.output

    def test_it_names_every_missing_variable_at_once(self, config):
        # One run should fix the whole environment, not one variable per run.
        result = run("-c", str(config), "--check-env", env={"VC_PROD_DSN": ""})
        assert result.exit_code == ExitCode.CONFIG_ERROR
        for name in ("VC_PROD_DSN", "VC_QA_DSN", "VC_DEV_DSN"):
            assert name in result.output

    def test_it_never_prints_a_credential(self, config):
        env = {
            "VC_PROD_DSN": "postgresql://u:hunter2@h/db",
            "VC_QA_DSN": "postgresql://u:hunter2@h/db",
            "VC_DEV_DSN": "postgresql://u:hunter2@h/db",
        }
        assert "hunter2" not in run("-c", str(config), "--check-env", env=env).output


class TestWarnings:
    def test_a_target_sharing_the_masters_credential_warns(self, tmp_path):
        path = tmp_path / "shared.yaml"
        path.write_text(CONFIG.replace("dsn_env: VC_QA_DSN", "dsn_env: VC_PROD_DSN"))
        result = run("-c", str(path))
        # Not an error: it is legal, just almost certainly a mistake.
        assert result.exit_code == ExitCode.OK
        assert "compared against itself" in result.output
