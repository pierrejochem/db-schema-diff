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

from ..config.secrets import Dsn
from ..errors import ConnectionFailed, UnsupportedServerError
from .features import MINIMUM_VERSION_LABEL, MINIMUM_VERSION_NUM, ServerFeatures

log = logging.getLogger(__name__)

APPLICATION_NAME = "cumo-schema-comparer"

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
    dsn: Dsn, *, label: str, options: ConnectionOptions | None = None, version: str = ""
) -> Iterator[psycopg.Connection[dict[str, object]]]:
    """Open a hardened read-only connection, yield it, and always close it.

    Any failure is re-raised as :class:`ConnectionFailed` carrying only a redacted summary:
    psycopg embeds the full conninfo, password included, in some of its own messages, so the
    original exception is never allowed to propagate.
    """
    options = options or ConnectionOptions()
    conninfo = _build_conninfo(dsn, options, version)

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


def _build_conninfo(dsn: Dsn, options: ConnectionOptions, version: str) -> str:
    """Merge our own settings into the user's connection string.

    A ``connect_timeout`` or ``application_name`` the user set explicitly wins: their DSN is
    more specific than our default.
    """
    parsed = {
        key: str(value) for key, value in conninfo_to_dict(dsn.value).items() if value is not None
    }
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
