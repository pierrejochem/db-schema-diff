"""Building a connection string from the parts a person typed.

The desktop application asks for a host, a port, a database, a user and a password rather than for
the name of an environment variable holding a DSN. It assembles the connection string itself and
puts it in the OS keychain; the parts that are not secret are saved in the configuration file, and
the password is saved nowhere else at all.

Two rules this module exists to keep:

**The password is never a config value.** Everything here returns either a non-secret summary or a
``Dsn``, which redacts itself in every string context. Nothing returns the assembled string as a
plain ``str``, because a plain string is one f-string away from a log line.

**Every part is escaped.** A password is the most likely place to find ``@``, ``/``, ``:`` or ``#``,
and a connection string built by concatenation turns any of them into a different connection — or a
parse error that quotes the password back. Everything is percent-encoded through ``urllib``.
"""

from __future__ import annotations

import string
from dataclasses import dataclass
from urllib.parse import quote, urlencode

from ..config.model import SourceRef
from ..config.secrets import Dsn

#: The default when a configuration does not say. libpq's own default too, so saying it changes
#: nothing — it is here so the assembled string is explicit about what it connected to.
DEFAULT_PORT = 5432

#: What may appear in a generated variable name. POSIX says letters, digits and underscores, and
#: means ASCII ones.
_NAME_CHARS = frozenset(string.ascii_letters + string.digits)


@dataclass(frozen=True, slots=True)
class Missing:
    """What a source still needs before it can be connected to."""

    fields: tuple[str, ...]

    def __bool__(self) -> bool:
        return bool(self.fields)

    def __str__(self) -> str:
        return ", ".join(self.fields)


def missing_parts(source: SourceRef, *, has_password: bool) -> Missing:
    """Which of the parts a connection needs are not filled in yet.

    The password is counted but never looked at: whether one is stored is a question for the
    keychain, and its value has no business here.
    """
    absent = [
        name
        for name, value in (
            ("host", source.host),
            ("database", source.database),
            ("user", source.user),
        )
        if not (value or "").strip()
    ]
    if not has_password:
        absent.append("password")
    return Missing(tuple(absent))


def build(source: SourceRef, password: str, *, env_name: str) -> Dsn:
    """The connection string for ``source``, as a ``Dsn``.

    ``env_name`` is the name the result is stored and reported under. It is generated rather than
    typed — see :func:`variable_name` — so that a configuration written here still means something
    to the command-line tool, which reads the environment and nothing else.
    """
    user = quote(source.user or "", safe="")
    secret = quote(password, safe="")
    host = source.host or ""
    port = source.port or DEFAULT_PORT
    database = quote(source.database or "", safe="")
    credentials = f"{user}:{secret}@" if secret else f"{user}@"
    query = f"?{urlencode({'sslmode': source.sslmode})}" if source.sslmode else ""
    return Dsn(f"postgresql://{credentials}{host}:{port}/{database}{query}", env_name=env_name)


def variable_name(label: str) -> str:
    """The environment variable a source's connection string is stored under.

    Generated from the label rather than asked for: the application has no reason to make anybody
    invent a name, but the name still has to exist, because it is how the command-line tool and the
    keychain both find the credential.
    """
    # ASCII only, not ``isalnum``: ``"ü".isalnum()`` is true, and a label written in German —
    # which these are — would then generate ``DB_ÜMLAUT_DSN``, which is not an environment
    # variable name. The configuration would refuse to save, naming a field nobody typed.
    cleaned = "".join(c if c in _NAME_CHARS else "_" for c in label).strip("_").upper()
    return f"DB_{cleaned or 'SOURCE'}_DSN"
