"""Credential handling.

The whole design rests on one idea: a credential is never a ``str``. It is a :class:`Dsn`
whose *default* string behaviour is redaction, so the usual accidents — an f-string in a log
call, a ``repr`` in a traceback, a dict dumped into a report — cannot leak it. A logging
filter alone is not enough, because any f-string bypasses it before the filter ever runs.
"""

from __future__ import annotations

import ipaddress
import os
import re
from typing import Any
from urllib.parse import unquote

from psycopg.conninfo import conninfo_to_dict

from ..errors import MissingCredentialsError

#: Conninfo keys that must never appear in a summary, a log line or a report.
_SENSITIVE_KEYS = frozenset({"password", "passfile", "sslpassword", "sslkey"})

#: Conninfo keys worth showing when a connection fails. ``options`` is deliberately absent: libpq
#: passes it to the server as ``-c name=value`` and it can carry ``-c password=...``.
_SUMMARY_KEYS = ("host", "hostaddr", "port", "dbname", "user", "sslmode")

_KEYWORD_PASSWORD = re.compile(r"(?:^|\s)password\s*=\s*('(?:\\.|[^'\\])*'|\S+)")


def _is_ipv6(text: str) -> bool:
    try:
        ipaddress.IPv6Address(text)
    except ValueError:
        return False
    return True


def _looks_misparsed(parsed: dict[str, Any]) -> bool:
    """Whether libpq split the string somewhere impossible in a well-formed one.

    An unencoded ``@``, ``/`` or similar in a password relocates password material into any
    field (host, port, dbname), so a field-name allowlist cannot be trusted. Each test below is a
    condition a correct connection string cannot produce; the summary then fails closed.

    A host/hostaddr part that starts with ``/`` is a Unix-socket directory; such parts may
    contain interior ``/``. This exemption prevents misparsed URIs (where a ``/`` in the password
    appears in the host field) from leaking through.
    """

    def text(key: str) -> str:
        return str(parsed.get(key) or "")

    port = text("port")
    if port:
        # libpq accepts comma-separated ports to match comma-separated hosts
        for port_part in port.split(","):
            if port_part and not (port_part.isascii() and port_part.isdigit()):
                return True
    for key in ("host", "hostaddr"):
        for part in text(key).split(","):  # libpq accepts a comma-separated host list
            # Unix-socket directory (starts with /) may contain interior /
            if part.startswith("/"):
                if any(ch in part for ch in "@") or any(ch.isspace() for ch in part):
                    return True
            else:
                # Non-socket hosts must not contain @, /, or whitespace
                if any(ch in part for ch in "@/") or any(ch.isspace() for ch in part):
                    return True
            # IPv6 literals are the only valid use of : in a host
            if ":" in part and not _is_ipv6(part):
                return True
    return any(ch in text("dbname") for ch in "@/:")


class Dsn:
    """A libpq connection string that redacts itself in every string context.

    :attr:`value` is the single accessor that returns the secret, and it is called once, at
    connect time. Everything else — logging, reporting, tracebacks — sees the redacted form.
    """

    __slots__ = ("_env_name", "_value")

    def __init__(self, value: str, *, env_name: str) -> None:
        self._value = value
        self._env_name = env_name

    @property
    def value(self) -> str:
        """The raw connection string. The only way to obtain the secret."""
        return self._value

    @property
    def env_name(self) -> str:
        """Name of the environment variable this came from."""
        return self._env_name

    @property
    def redacted(self) -> str:
        """The form safe to print anywhere."""
        return f"<Dsn from ${self._env_name}>"

    def __repr__(self) -> str:
        return self.redacted

    def __str__(self) -> str:
        return self.redacted

    def __format__(self, spec: str) -> str:
        # Ignore the spec deliberately: honouring it would let `f"{dsn:s}"` produce the raw
        # value on some future refactor.
        return self.redacted

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Dsn):
            return self._value == other._value
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._value)

    def password(self) -> str | None:
        """The password component, for the logging filter to scrub. Never for display."""
        try:
            parsed = conninfo_to_dict(self._value)
        except Exception:
            return None
        password = parsed.get("password")
        return password if isinstance(password, str) and password else None

    def secret_fragments(self) -> tuple[str, ...]:
        """Every substring that might be part of a credential, for scrubbing output.

        Derived from the raw string rather than the parsed password, because the case that leaks is
        exactly the case libpq parses wrongly: an unencoded `@` in a password makes libpq read the
        tail as the host, so the parsed password is only the part before the `@` and the rest
        travels inside the host, where libpq then echoes it back in its own error text.
        """
        found: list[str] = []
        parsed_password = self.password()
        if parsed_password:
            found.append(parsed_password)
        raw = self._value
        if "://" in raw:
            # The last "@" is the userinfo delimiter whatever the password contains: a real host
            # never has one. Do not stop at the first "/" - a password may hold it.
            head = raw.split("://", 1)[1]
            if "@" in head:
                userinfo = head.rsplit("@", 1)[0]
                if ":" in userinfo:  # no colon means a user name only, no password
                    after = userinfo.split(":", 1)[1]
                    found.extend([userinfo, after, unquote(after)])
                    found.extend(userinfo.split("@"))
                    found.extend(after.split("@"))
                    found.extend(re.split(r"[@/?#:]", after))
        else:
            for match in _KEYWORD_PASSWORD.finditer(raw):
                token = match.group(1)
                if token.startswith("'") and token.endswith("'") and len(token) >= 2:
                    found.append(re.sub(r"\\(.)", r"\1", token[1:-1]))
                found.append(token)
        unique = {fragment for fragment in found if fragment}
        return tuple(sorted(unique, key=lambda fragment: (-len(fragment), fragment)))

    def safe_summary(self) -> dict[str, str]:
        """Host, port, database and user, with every secret component removed.

        Used in connection-failure messages: enough to diagnose, nothing to leak. Never
        raises — an unparseable value still has to produce a usable error message.
        """
        try:
            parsed = conninfo_to_dict(self._value)
        except Exception:
            return {"source": f"${self._env_name}", "parsed": "unparseable"}
        unparseable = {"source": f"${self._env_name}", "parsed": "unparseable"}
        if _looks_misparsed(parsed):
            return unparseable
        summary = {
            key: str(parsed[key])
            for key in _SUMMARY_KEYS
            if key not in _SENSITIVE_KEYS and parsed.get(key) not in (None, "")
        }
        if not summary:
            return {"source": f"${self._env_name}", "parsed": "unparseable"}
        summary["source"] = f"${self._env_name}"
        return summary


def resolve_dsn(env_name: str, *, role: str) -> Dsn:
    """Read one credential from the environment.

    ``role`` describes who wants it (``"master 'prod'"``) so the failure message points at
    the config entry rather than only at the variable name.
    """
    raw = os.environ.get(env_name)
    if raw is None or not raw.strip():
        raise MissingCredentialsError(format_missing([(env_name, role)]))
    return Dsn(raw.strip(), env_name=env_name)


def resolve_all(requests: list[tuple[str, str]]) -> dict[str, Dsn]:
    """Resolve every credential at once, reporting *all* that are missing.

    Failing one variable per run turns a three-variable mistake into three runs, so the
    misses are collected and raised together. ``requests`` is a list of
    ``(env_var_name, role)`` pairs; the result is keyed by env var name.
    """
    resolved: dict[str, Dsn] = {}
    missing: list[tuple[str, str]] = []
    for env_name, role in requests:
        raw = os.environ.get(env_name)
        if raw is None or not raw.strip():
            missing.append((env_name, role))
            continue
        resolved[env_name] = Dsn(raw.strip(), env_name=env_name)
    if missing:
        raise MissingCredentialsError(format_missing(missing))
    return resolved


def format_missing(missing: list[tuple[str, str]]) -> str:
    """Render the missing-credentials message."""
    width = max(len(name) for name, _ in missing)
    lines = [f"  {name:<{width}}  ({role})" for name, role in missing]
    return (
        "Configuration error: missing credentials\n"
        + "\n".join(lines)
        + "\nSet each to a libpq URI or keyword string. "
        + "Values are read from the environment only."
    )


def secret_strings(dsns: Any) -> list[str]:
    """Every literal string that must never appear in output, for the logging filter."""
    out: list[str] = []
    for dsn in dsns:
        if not isinstance(dsn, Dsn):
            continue
        out.append(dsn.value)
        password = dsn.password()
        if password:
            out.append(password)
    # Longest first, so scrubbing the full DSN wins over scrubbing its password substring.
    return sorted(set(out), key=len, reverse=True)
