"""Process exit codes.

The contract is part of the public interface: CI pipelines branch on these values.
``DRIFT`` is the gate; ``PROBE_ERROR`` is the retryable one.
"""

from enum import IntEnum


class ExitCode(IntEnum):
    """Exit status returned by every command."""

    OK = 0
    """No drift at or above the configured ``--fail-on`` severity."""

    DRIFT = 1
    """Drift found at or above ``--fail-on``. The CI gate."""

    CONFIG_ERROR = 2
    """Unusable input: bad YAML, missing env var, unknown label, PostgreSQL below the floor."""

    PROBE_ERROR = 3
    """A source could not be inspected: connect, auth, permission or timeout failure.

    Takes precedence over :attr:`DRIFT`, because a partial comparison must never be
    presented as a complete gate.
    """

    INTERNAL_ERROR = 4
    """Unexpected failure. A bug in this tool."""

    INTERRUPTED = 130
    """SIGINT."""
