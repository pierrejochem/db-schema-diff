"""Opening a hardened, read-only connection to one source.

Three things happen on every connection, and each one prevents a specific failure:

``SET search_path = ''``
    The single most important line in this tool. ``pg_get_expr``, ``pg_get_indexdef``,
    ``pg_get_viewdef``, ``pg_get_constraintdef``, ``pg_get_triggerdef`` and ``regtype`` all
    print *search_path-dependent* text. With an empty search_path every user-schema reference
    comes back fully qualified, so the same object produces identical text regardless of which
    role connected to which server. Without it, two environments whose roles have different
    default search paths disagree about every definition. (``format_type`` still prints
    built-ins unqualified — ``integer``, not ``pg_catalog.int4`` — which is what we want.)

A read-only ``REPEATABLE READ`` transaction
    Every query in one capture sees one snapshot, so a migration running concurrently cannot
    produce a half-applied inventory. Read-only is set twice, as the session default and as the
    transaction mode, because pointing this at production must be unambiguously safe.

Timeouts
    ``statement_timeout`` bounds a slow catalog query, and ``lock_timeout`` stops concurrent
    DDL from parking the probe behind a lock it will never get.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from ..config.model import SshRef
from ..config.secrets import Dsn, Secret
from ..errors import ConfigError, ConnectionFailed, UnsupportedServerError
from .features import MINIMUM_VERSION_LABEL, MINIMUM_VERSION_NUM, ServerFeatures

log = logging.getLogger(__name__)

APPLICATION_NAME = "db-schema-diff"

#: Stand-in when an exception carries no usable text.
NO_DETAIL = "no detail reported"


@dataclass(frozen=True, slots=True)
class ConnectionOptions:
    """Timeouts, all in seconds."""

    connect_timeout: int = 10
    statement_timeout: int = 60
    lock_timeout: int = 3
    idle_in_transaction_timeout: int = 30


@contextmanager
def open_connection(
    dsn: Dsn,
    *,
    label: str,
    options: ConnectionOptions | None = None,
    version: str = "",
    ssh: SshRef | None = None,
    ssh_passphrase: Secret | None = None,
) -> Iterator[psycopg.Connection[dict[str, object]]]:
    """Open a hardened read-only connection, yield it, and always close it.

    Any failure is re-raised as :class:`ConnectionFailed` carrying only a redacted summary:
    psycopg embeds the full conninfo, password included, in some of its own messages, so the
    original exception is never allowed to propagate.

    With ``ssh``, a tunnel to the database named in the DSN is opened first and closed with the
    connection. This is the one place anything in this project connects, so wiring it here covers
    the CLI and the desktop application together.
    """
    options = options or ConnectionOptions()
    if ssh is None:
        with _direct(dsn, options, version, label) as connection:
            yield connection
        return

    from .tunnel import open_tunnel  # imported lazily: paramiko is an optional extra

    to_host, to_port = _tunnel_target(dsn)
    with (
        open_tunnel(ssh, to_host=to_host, to_port=to_port, passphrase=ssh_passphrase) as local,
        _direct(dsn, options, version, label, through=local) as connection,
    ):
        yield connection


def _tunnel_target(dsn: Dsn) -> tuple[str, int]:
    """The database address the tunnel must reach, read from the DSN.

    A DSN with no TCP host cannot be tunnelled: a Unix socket is a path on the machine libpq runs
    on, and forwarding a local port to it means nothing.
    """
    parsed = conninfo_to_dict(dsn.value)
    host = str(parsed.get("host") or "")
    if not host or host.startswith("/"):
        raise ConfigError(
            f"{dsn} names no TCP host, so it cannot be reached through an SSH tunnel. "
            "Give the DSN the host and port as the gateway sees them."
        )
    return host, int(str(parsed.get("port") or 5432))


@contextmanager
def _direct(
    dsn: Dsn,
    options: ConnectionOptions,
    version: str,
    label: str,
    *,
    through: tuple[str, int] | None = None,
) -> Iterator[psycopg.Connection[dict[str, object]]]:
    """The connection itself, optionally pointed at a local tunnel endpoint."""
    conninfo = _build_conninfo(dsn, options, version, through)

    try:
        connection = psycopg.connect(conninfo, autocommit=True, row_factory=dict_row)
    except Exception as exc:
        raise ConnectionFailed(_failure_message(label, dsn, exc)) from None

    try:
        _harden(connection, options)
        _require_supported_version(connection, label)
        # Switching out of autocommit makes psycopg open a transaction on the next statement,
        # using the isolation level and read-only mode set below.
        connection.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        connection.read_only = True
        connection.autocommit = False
        yield connection
    except ConnectionFailed:
        connection.close()
        raise
    except UnsupportedServerError:
        connection.close()
        raise
    except Exception as exc:
        connection.close()
        raise ConnectionFailed(_failure_message(label, dsn, exc)) from None
    else:
        # Read-only, so there is nothing to commit; rollback releases the snapshot cleanly.
        connection.rollback()
        connection.close()


def _build_conninfo(
    dsn: Dsn,
    options: ConnectionOptions,
    version: str,
    through: tuple[str, int] | None = None,
) -> str:
    """Merge our own settings into the user's connection string.

    A ``connect_timeout`` or ``application_name`` the user set explicitly wins: their DSN is
    more specific than our default.

    ``through`` points the connection at a tunnel endpoint by setting ``hostaddr`` and ``port`` and
    **leaving ``host`` alone**. libpq connects to ``hostaddr`` but verifies the server certificate
    against ``host``, so rewriting ``host`` to 127.0.0.1 — the obvious way to do this — would turn
    ``sslmode=verify-full`` into a connection no certificate can satisfy, and the natural fix for
    that is to weaken sslmode. Keeping ``host`` means TLS verification still checks the name the
    database actually answers to.
    """
    parsed = {
        key: str(value) for key, value in conninfo_to_dict(dsn.value).items() if value is not None
    }
    if through is not None:
        parsed["hostaddr"], parsed["port"] = through[0], str(through[1])
    parsed.setdefault("connect_timeout", str(options.connect_timeout))
    suffix = f"/{version}" if version else ""
    parsed.setdefault("application_name", f"{APPLICATION_NAME}{suffix}")
    return make_conninfo(**parsed)


def _harden(connection: psycopg.Connection[dict[str, object]], options: ConnectionOptions) -> None:
    """Apply the session settings every query then depends on."""
    statements: list[sql.SQL | sql.Composed] = [
        # Deterministic qualification in every pg_get_*def() result. See the module docstring.
        sql.SQL("SET search_path = ''"),
        sql.SQL("SET statement_timeout = {}").format(options.statement_timeout * 1000),
        sql.SQL("SET lock_timeout = {}").format(options.lock_timeout * 1000),
        sql.SQL("SET idle_in_transaction_session_timeout = {}").format(
            options.idle_in_transaction_timeout * 1000
        ),
        sql.SQL("SET default_transaction_read_only = on"),
        # JIT compilation is pure overhead on catalog queries and adds latency variance.
        sql.SQL("SET jit = off"),
    ]
    with connection.cursor() as cursor:
        for statement in statements:
            cursor.execute(statement)


def _require_supported_version(
    connection: psycopg.Connection[dict[str, object]], label: str
) -> None:
    """Refuse a server too old for the catalog queries, with a message that says so."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_setting('server_version_num')::int AS num, version() AS full"
        )
        row = cursor.fetchone()
    if row is None:  # pragma: no cover - a server always answers this
        raise ConnectionFailed(f"{label}: server did not report its version")
    version_num = int(row["num"])  # type: ignore[call-overload]
    if version_num < MINIMUM_VERSION_NUM:
        raise UnsupportedServerError(
            f"{label}: PostgreSQL {version_num // 10000} is too old; "
            f"this tool needs {MINIMUM_VERSION_LABEL} or newer"
        )


def server_features(connection: psycopg.Connection[dict[str, object]]) -> ServerFeatures:
    """Capabilities of the connected server."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('server_version_num')::int AS num")
        row = cursor.fetchone()
    if row is None:  # pragma: no cover - a server always answers this
        raise ConnectionFailed("server did not report its version")
    return ServerFeatures.from_version_num(int(row["num"]))  # type: ignore[call-overload]


def _failure_message(label: str, dsn: Dsn, exc: Exception) -> str:
    """Connection-failure text: enough to diagnose, with no credential in it."""
    summary = ", ".join(f"{key}={value}" for key, value in sorted(dsn.safe_summary().items()))
    reason = _redact_address_tokens(_scrub(str(exc), dsn))
    return f"{label}: cannot connect ({summary})\n  {reason}"


def _scrub(text: str, dsn: Dsn) -> str:
    """Remove anything secret that libpq may have put in its own message."""
    cleaned = text.strip().replace(dsn.value, "***")
    for fragment in dsn.secret_fragments():
        cleaned = cleaned.replace(fragment, "***")
    return cleaned or NO_DETAIL


def _redact_address_tokens(text: str) -> str:
    """Second layer: libpq never needs to echo a token containing ``@``.

    Catches a password fragment that travelled inside a mis-parsed host and that no fragment
    list anticipated. Whitespace is preserved.
    """
    return re.sub(r"\S*@\S*", "***", text)
