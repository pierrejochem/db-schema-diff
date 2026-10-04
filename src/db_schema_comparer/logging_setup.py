"""Logging configuration and the second layer of credential defence.

:class:`Dsn` redaction handles everything this tool formats itself. This filter catches what
it does not control: libpq and psycopg embed the full conninfo, password included, in some of
their own messages.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterable

from .config.secrets import Dsn, secret_strings

REDACTION = "***"


class RedactingFilter(logging.Filter):
    """Replace every known secret with ``***`` in a record before it is emitted.

    Interpolation happens here rather than at emit time, because a secret passed as a
    ``%s`` argument is not present in ``record.msg`` and would otherwise survive.
    """

    def __init__(self, dsns: Iterable[Dsn] = ()) -> None:
        super().__init__()
        self._dsns: list[Dsn] = list(dsns)
        self._secrets: list[str] = secret_strings(self._dsns)

    def add(self, *dsns: Dsn) -> None:
        """Register further credentials resolved after logging was configured."""
        self._dsns.extend(dsns)
        self._secrets = secret_strings(self._dsns)

    def _scrub(self, text: str) -> str:
        for secret in self._secrets:
            if secret and secret in text:
                text = text.replace(secret, REDACTION)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        message = record.getMessage()
        scrubbed = self._scrub(message)
        if scrubbed != message:
            record.msg = scrubbed
            record.args = ()
        return True


def configure(
    verbosity: int = 0, *, quiet: bool = False, dsns: Iterable[Dsn] = ()
) -> RedactingFilter:
    """Configure root logging and install the redacting filter.

    Returns the filter so later-resolved credentials can be registered with it.
    """
    levels = (logging.WARNING, logging.INFO, logging.DEBUG)
    level = logging.ERROR if quiet else levels[min(verbosity, 2)]
    redactor = RedactingFilter(dsns)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    handler.addFilter(redactor)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    return redactor
