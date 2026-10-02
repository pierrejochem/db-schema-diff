"""Against a real database: a secret in definition text reaches no output.

This is the case the flag exists for. ``report.html`` and ``junit.xml`` are uploaded as CI
artifacts by this repo's own dogfood job, so a hardcoded connection string in a default or a
function body would travel with them.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cumo_schema_comparer.config.model import SourceRef
from cumo_schema_comparer.config.secrets import Dsn
from cumo_schema_comparer.db.connect import open_connection
from tests.integration.conftest import apply_sql
from tests.integration.test_cli_end_to_end import CONFIG

pytestmark = pytest.mark.integration

SECRET = "s3cret"
OUTPUTS = ("report.json", "junit.xml", "report.html")


def invoke(tmp_path: Path, databases, *args: str, command: str = "compare") -> tuple[str, Path]:
    config = tmp_path / "invoicing.yaml"
    config.write_text(CONFIG)
    out_dir = tmp_path / "out"
    extra = ["--out-dir", str(out_dir), "--fail-on", "never"] if command == "compare" else []
    executable = Path(sys.executable).parent / "cumo-schema-diff"
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [str(executable), command, "-c", str(config), *extra, *args],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "IT_PROD_DSN": databases.master_dsn,
            "IT_QA_DSN": databases.target_dsn,
            "NO_COLOR": "1",
        },
        check=False,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return completed.stdout + completed.stderr, out_dir


@pytest.fixture
def secret_databases(databases):
    databases.setup("base", drift="drift_secret_default")
    apply_sql(databases.master_dsn, "secret_default_master")
    return databases


def test_a_secret_is_masked_in_every_output(tmp_path, secret_databases):
    console, out_dir = invoke(tmp_path, secret_databases)

    assert SECRET not in console, "console leaked the secret"
    for name in OUTPUTS:
        text = (out_dir / name).read_text(encoding="utf-8")
        assert SECRET not in text, f"{name} leaked the secret"
    assert "***:" in (out_dir / "report.json").read_text(encoding="utf-8")

    # The fifth artefact: the captured inventory, which also holds the expression index key and
    # the function's SET value that the report may not print.
    inventory_path = tmp_path / "inventory.json"
    invoke(
        tmp_path,
        secret_databases,
        "--source",
        "qa",
        "-o",
        str(inventory_path),
        command="inventory",
    )
    captured = inventory_path.read_text(encoding="utf-8")
    assert SECRET not in captured, "inventory leaked the secret"
    assert "idx_invoice_secret" in captured and "uses_setting" in captured


def test_the_masked_text_still_reports_as_drift(tmp_path, secret_databases):
    """Masking must not hide the change: the default and the body differ, and that is drift."""
    _, out_dir = invoke(tmp_path, secret_databases)
    report = json.loads((out_dir / "report.json").read_text(encoding="utf-8"))
    findings = {
        f["path"]: {d["attribute"] for d in f.get("deltas", [])}
        for target in report["targets"]
        for f in target["findings"]
    }
    assert "column.default" in findings["cumo-invoicing.invoice.sync_target"]
    assert "routine.body_hash" in findings["cumo-invoicing.invoice_gross(p_invoice_id integer)"]


def test_no_redact_literals_shows_the_real_text(tmp_path, secret_databases):
    _, out_dir = invoke(tmp_path, secret_databases, "--no-redact-literals")
    assert SECRET in (out_dir / "report.json").read_text(encoding="utf-8")


def test_the_inventory_command_masks_by_default_and_not_when_asked(tmp_path, secret_databases):
    for flag, expect_secret in ((None, False), ("--no-redact-literals", True)):
        out = tmp_path / f"inv{expect_secret}.json"
        args = ["--source", "qa", "-o", str(out), *([flag] if flag else [])]
        invoke(tmp_path, secret_databases, *args, command="inventory")
        text = out.read_text(encoding="utf-8")
        assert (SECRET in text) is expect_secret


def test_a_routine_body_secret_is_in_body_and_raw_only_without_redaction(secret_databases):
    from cumo_schema_comparer.build import build_inventory

    source = SourceRef(label="qa", dsn_env="QA_DSN")

    def routine(redact: bool):
        with open_connection(Dsn(secret_databases.target_dsn, env_name="QA_DSN"), label="qa") as c:
            inv = build_inventory(c, source, redact_literals=redact)
        return next(
            o for k, o in inv.objects.items() if k.path.startswith("cumo-invoicing.invoice_gross")
        )

    masked = routine(True)
    assert SECRET not in masked.body and SECRET not in masked.raw.values["body"]
    real = routine(False)
    assert SECRET in real.body and SECRET in real.raw.values["body"]
