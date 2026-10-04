"""Severity, and what it means.

The scale is opinionated because the master is authoritative:

*Target lacks what the master has* is an **error** — a migration did not land, and something
will fail at runtime.

*Target has something extra* is a **warning** — usually someone experimenting in a lower
environment, which is worth knowing but is not a broken deployment.

That asymmetry is the whole reason this tool has a designated master rather than just diffing
two databases.
"""

from __future__ import annotations

from enum import IntEnum


class Severity(IntEnum):
    """How much a difference matters. Ordered, so ``max()`` finds the worst."""

    INFO = 10
    """Worth recording, never worth failing a build over."""

    WARNING = 20
    """A real difference that is usually intentional, or whose impact is performance not
    correctness."""

    ERROR = 30
    """A difference that will break something, or means a migration is missing."""

    @property
    def label(self) -> str:
        return self.name.lower()


#: Which severities a given ``--fail-on`` setting treats as a failure.
FAIL_ON_THRESHOLDS: dict[str, Severity | None] = {
    "error": Severity.ERROR,
    "warning": Severity.WARNING,
    "any": Severity.INFO,
    "never": None,
}


def gate(fail_on: str) -> Severity | None:
    """Lowest severity that should fail the run, or ``None`` when nothing should."""
    try:
        return FAIL_ON_THRESHOLDS[fail_on]
    except KeyError:  # pragma: no cover - the config schema restricts this
        raise ValueError(f"unknown fail_on value {fail_on!r}") from None


def worst(severities: object) -> Severity | None:
    """Highest severity in an iterable, or ``None`` when it is empty."""
    values = list(severities)  # type: ignore[call-overload]
    return max(values) if values else None
