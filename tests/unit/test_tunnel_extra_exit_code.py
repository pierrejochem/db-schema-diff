"""The exit code a config gets when it needs the ssh extra and the extra is not installed.

CI branches on these codes. Reported from the connection, a missing package arrives as one probe
failure per tunnelled source and exits 3 — the code that means "a source could not be inspected,
try again". Installing a package is not a try-again, and three sources behind one bastion is still
one thing to fix, so it is checked once, before anything connects, and exits 2.
"""

from __future__ import annotations

import contextlib
import io
import os
import textwrap
from unittest import mock

import pytest

from db_schema_diff import cli as cli_module
from db_schema_diff.exit_codes import ExitCode

CONFIG = textwrap.dedent(
    """
    version: 1
    name: invoicing
    master:
      label: prod
      dsn_env: TX_PROD_DSN
    targets:
      - label: qa
        dsn_env: TX_QA_DSN
        ssh:
          host: bastion.internal
    """
).strip()


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "invoicing.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    return path


def run(*args, env=None):
    argv = ["db-schema-diff", *args]
    stdout, stderr = io.StringIO(), io.StringIO()
    environment = {k: v for k, v in os.environ.items() if not k.startswith("TX_")}
    environment.update(env or {})
    with (
        mock.patch.object(cli_module.sys, "argv", argv),
        mock.patch.dict(os.environ, environment, clear=True),
        contextlib.redirect_stdout(stdout),
        contextlib.redirect_stderr(stderr),
    ):
        code = cli_module.main()
    return code, stdout.getvalue() + stderr.getvalue()


@pytest.fixture
def without_the_extra(monkeypatch):
    from db_schema_diff.db import tunnel

    monkeypatch.setattr(tunnel, "available", lambda: False)


DSNS = {
    "TX_PROD_DSN": "postgresql://u@h:5432/invoicing",
    "TX_QA_DSN": "postgresql://u@h:5432/invoicing",
}


@pytest.mark.parametrize("command", ["compare", "probe"])
def test_a_config_needing_the_extra_is_a_config_error(command, config, without_the_extra):
    code, output = run(command, "-c", str(config), env=DSNS)
    assert code == ExitCode.CONFIG_ERROR, output
    assert "db-schema-diff[ssh]" in output
    assert "'qa'" in output


def test_it_fails_before_it_tries_to_connect(config, without_the_extra):
    """Nothing should have been dialled: the answer is the same for every source."""
    with mock.patch("db_schema_diff.runner.open_connection") as connect:
        code, _ = run("compare", "-c", str(config), env=DSNS)
    assert code == ExitCode.CONFIG_ERROR
    connect.assert_not_called()


def test_a_config_without_a_tunnel_is_unaffected(tmp_path, without_the_extra):
    """The check must not stand between an ordinary config and its database.

    Pointed at a port nothing is listening on, so it gets as far as failing to connect — which is
    exit 3, and is the proof that the extra was never its business.
    """
    plain = tmp_path / "plain.yaml"
    plain.write_text(CONFIG[: CONFIG.index("ssh:")].rstrip() + "\n")
    refused = "postgresql://u@127.0.0.1:1/invoicing?connect_timeout=1"
    code, output = run(
        "compare", "-c", str(plain), env={"TX_PROD_DSN": refused, "TX_QA_DSN": refused}
    )
    assert code == ExitCode.PROBE_ERROR, output
    assert "ssh" not in output
