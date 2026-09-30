#!/usr/bin/env python3
"""Run the real CLI against two deliberately-divergent databases.

The most honest smoke test available: it exercises the installed console script, a real
PostgreSQL, the full capture-diff-report path and the exit-code contract, and it leaves a report
behind for a reviewer to read.

Used by the `dogfood` CI job. Runs locally too, whenever Docker is available.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BUILD = ROOT / "build"
CONFIG = """\
version: 1
name: dogfood
master:
  label: prod
  dsn_env: DOGFOOD_PROD_DSN
targets:
  - label: qa
    dsn_env: DOGFOOD_QA_DSN
"""

#: Applied to the target only. Each produces a finding of a known severity.
DRIFT = ("drift_column_type", "drift_nullable", "drift_extra_table")


def main() -> int:
    from tests.integration.conftest import docker_available

    if not docker_available():
        print("::notice::Docker is unavailable; skipping the dogfood run")
        return 0

    from testcontainers.community.postgres import PostgresContainer

    image = os.environ.get("CUMO_SCHEMA_DIFF_TEST_IMAGE", "postgres:15")
    container = PostgresContainer(image, driver=None)
    container.start()
    try:
        base = container.get_connection_url()
        master_dsn, target_dsn = _prepare(base)
        return _compare(master_dsn, target_dsn)
    finally:
        container.stop()


def _prepare(base: str) -> tuple[str, str]:
    import psycopg
    from psycopg import sql
    from tests.integration.conftest import apply_sql, dsn_for

    with psycopg.connect(base, autocommit=True) as connection, connection.cursor() as cursor:
        for database in ("dogfood_master", "dogfood_target"):
            cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))

    master_dsn = dsn_for(base, "dogfood_master")
    target_dsn = dsn_for(base, "dogfood_target")
    apply_sql(master_dsn, "base")
    apply_sql(target_dsn, "base", *DRIFT)
    return master_dsn, target_dsn


def _compare(master_dsn: str, target_dsn: str) -> int:
    BUILD.mkdir(exist_ok=True)
    config = BUILD / "dogfood.yaml"
    config.write_text(CONFIG)

    executable = Path(sys.executable).parent / "cumo-schema-diff"
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            str(executable),
            "compare",
            "-c",
            str(config),
            "--json",
            str(BUILD / "report.json"),
            "--junit",
            str(BUILD / "junit.xml"),
            "--html",
            str(BUILD / "report.html"),
        ],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "DOGFOOD_PROD_DSN": master_dsn,
            "DOGFOOD_QA_DSN": target_dsn,
            "NO_COLOR": "1",
        },
        check=False,
    )

    (BUILD / "console.txt").write_text(result.stdout + result.stderr)
    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)

    if result.returncode != 1:
        print(
            f"::error::expected exit 1 (drift found), got {result.returncode}",
            file=sys.stderr,
        )
        return 1

    for secret in _secrets(master_dsn, target_dsn):
        if secret and secret in result.stdout + result.stderr:
            print("::error::a credential appeared in the tool's output", file=sys.stderr)
            return 1

    # The reports are uploaded as CI artifacts, so they must exist and be well-formed.
    from xml.etree import ElementTree

    report = json.loads((BUILD / "report.json").read_text())
    if report["worst_severity"] != "error":
        print(f"::error::expected an error-level report, got {report['worst_severity']!r}")
        return 1
    failures = ElementTree.parse(BUILD / "junit.xml").getroot().findall(".//failure")  # noqa: S314
    if not failures:
        print("::error::the JUnit report recorded no failures")
        return 1

    # The HTML report is the artifact a reviewer actually opens, often with no network access.
    html = (BUILD / "report.html").read_text(encoding="utf-8")
    for pattern in ("http://", "https://", "//cdn"):
        if pattern in html:
            print(f"::error::the HTML report references something external ({pattern})")
            return 1

    print(
        f"dogfood run reported {len(failures)} JUnit failure(s), leaked no credential, and wrote a "
        f"self-contained {len(html) // 1024} KiB HTML report"
    )
    return 0


def _secrets(*dsns: str) -> list[str]:
    from psycopg.conninfo import conninfo_to_dict

    out = []
    for dsn in dsns:
        password = conninfo_to_dict(dsn).get("password")
        if isinstance(password, str) and password:
            out.append(password)
    return out


if __name__ == "__main__":
    sys.exit(main())
