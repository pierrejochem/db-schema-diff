"""Integration fixtures: a real PostgreSQL, two databases, deliberate drift.

Why testcontainers rather than a compose file: the container is ephemeral, its port is allocated
automatically so parallel runs do not collide, and there is no second file to keep in step with
this one. It behaves identically on a developer's Mac and on GitHub's ubuntu-latest.

Why two databases inside **one** container: that is the real topology. The Acme server hosts ~18
databases, one per service, so "master" and "target" are two databases on one server far more
often than two servers. It is also faster.

Everything here skips cleanly when Docker is absent, so ``pytest`` with no arguments never needs
it. ``DB_SCHEMA_DIFF_TEST_DSN`` points the same tests at an existing server instead.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("psycopg", reason="psycopg is required for integration tests")

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

FIXTURE_SQL = Path(__file__).parent.parent / "fixtures" / "sql"
MASTER_DB = "master_db"
TARGET_DB = "target_db"
POSTGRES_IMAGE = os.environ.get("DB_SCHEMA_DIFF_TEST_IMAGE", "postgres:15")
"""Mirrors local-qa-env/RMV, which runs postgres:15."""


def docker_available() -> bool:
    """Whether a usable Docker daemon is reachable.

    Checks the daemon, not just the binary: a Mac with Docker Desktop installed but stopped has
    the binary and no daemon, and the resulting testcontainers failure is slow and confusing.
    """
    docker = shutil.which("docker")
    if not docker:
        return False
    try:
        # The absolute path from shutil.which, so the call does not depend on PATH twice.
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [docker, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


@pytest.fixture(scope="session")
def base_dsn() -> Iterator[str]:
    """A libpq URI for a server we may create databases on.

    Either an external server named by ``DB_SCHEMA_DIFF_TEST_DSN`` or a throwaway container.
    """
    external = os.environ.get("DB_SCHEMA_DIFF_TEST_DSN")
    if external:
        yield external
        return

    if not docker_available():
        pytest.skip("needs Docker, or DB_SCHEMA_DIFF_TEST_DSN pointing at a PostgreSQL server")

    postgres = pytest.importorskip(
        "testcontainers.community.postgres", reason="testcontainers is required"
    )
    container = postgres.PostgresContainer(POSTGRES_IMAGE, driver=None)
    container.start()
    try:
        yield container.get_connection_url()
    finally:
        container.stop()


@pytest.fixture(scope="session")
def server(base_dsn: str) -> str:
    """The base DSN, with both test databases created and empty."""
    for database in (MASTER_DB, TARGET_DB):
        _recreate_database(base_dsn, database)
    return base_dsn


def dsn_for(base_dsn: str, database: str) -> str:
    """The base DSN pointed at a different database."""
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    parsed = {k: str(v) for k, v in conninfo_to_dict(base_dsn).items() if v is not None}
    parsed["dbname"] = database
    return make_conninfo(**parsed)


def _recreate_database(base_dsn: str, database: str) -> None:
    """Drop and recreate one database, so every session starts from nothing.

    ``WITH (FORCE)`` arrived in PostgreSQL 13, which is why the CI matrix starts there and why
    ``test_pg11.py`` starts its own containers instead of using this harness.
    """
    with psycopg.connect(base_dsn, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database))
        )
        cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))


def apply_sql(dsn: str, *names: str) -> None:
    """Run one or more fixture SQL files against a database."""
    with psycopg.connect(dsn, autocommit=True) as connection, connection.cursor() as cursor:
        for name in names:
            cursor.execute((FIXTURE_SQL / f"{name}.sql").read_text(encoding="utf-8"))


def reset(dsn: str) -> None:
    """Return a database to an empty state without recreating it.

    Faster than DROP DATABASE, and it keeps the fixture usable while connections are open.
    """
    with psycopg.connect(dsn, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute('DROP SCHEMA IF EXISTS "acme-invoicing" CASCADE')
        cursor.execute("DROP SCHEMA public CASCADE")
        cursor.execute("CREATE SCHEMA public AUTHORIZATION CURRENT_USER")


@pytest.fixture(scope="module")
def read_only_master(server: str) -> str:
    """One database with ``base.sql`` applied, built once per module.

    For the many assertions that only *read* a captured inventory. Rebuilding the schema for each
    of them costs seconds and buys nothing, because nothing writes to it.
    """
    name = "readonly_db"
    _recreate_database(server, name)
    dsn = dsn_for(server, name)
    apply_sql(dsn, "base")
    return dsn


@pytest.fixture
def databases(server: str):
    """A pair of empty databases, and a helper to load fixtures into them.

    Yields a small object rather than a tuple so the tests read as
    ``dbs.setup("base", drift="drift_nullable")``.
    """

    class Databases:
        master_dsn = dsn_for(server, MASTER_DB)
        target_dsn = dsn_for(server, TARGET_DB)

        def setup(self, *base: str, drift: str | None = None) -> None:
            """Apply ``base`` to both databases, then ``drift`` to the target only."""
            apply_sql(self.master_dsn, *base)
            apply_sql(self.target_dsn, *base)
            if drift:
                apply_sql(self.target_dsn, drift)

        def query(self, dsn: str, statement: str) -> list[dict[str, object]]:
            with (
                psycopg.connect(dsn, row_factory=dict_row) as connection,
                connection.cursor() as cursor,
            ):
                cursor.execute(statement)
                return list(cursor.fetchall())

    reset(Databases.master_dsn)
    reset(Databases.target_dsn)
    yield Databases()
