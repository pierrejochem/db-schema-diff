"""Exception hierarchy. Every error carries the exit code it should produce."""

from __future__ import annotations

from .exit_codes import ExitCode


class ComparerError(Exception):
    """Base class for every expected failure.

    ``cli`` catches these, prints ``str(self)`` without a traceback, and exits with
    :attr:`exit_code`. Anything that is not a ``ComparerError`` is a bug and surfaces as
    :attr:`ExitCode.INTERNAL_ERROR` with a traceback.
    """

    exit_code: ExitCode = ExitCode.INTERNAL_ERROR


class ConfigError(ComparerError):
    """Unusable configuration: malformed YAML, failed validation, unknown target label."""

    exit_code = ExitCode.CONFIG_ERROR


class MissingCredentialsError(ConfigError):
    """One or more ``dsn_env`` variables are unset or blank.

    Raised once, listing every missing variable, so a user fixes the whole environment in
    one pass instead of one variable per run.
    """


class UnsupportedServerError(ConfigError):
    """A source runs a PostgreSQL release below the supported floor."""


class ProbeError(ComparerError):
    """A source could not be inspected."""

    exit_code = ExitCode.PROBE_ERROR


class ConnectionFailed(ProbeError):
    """Connecting to a source failed.

    Carries a redacted connection summary only: psycopg embeds the full conninfo, password
    included, in some of its failure messages, so the original exception is never
    propagated verbatim.
    """
