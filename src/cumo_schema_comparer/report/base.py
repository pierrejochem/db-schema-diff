"""The reporter contract.

Every reporter takes a :class:`ComparisonReport` and nothing else: no connection, no config
object, no clock. That restriction is what keeps four output formats consistent with each
other, lets them all be tested from a canned report, and makes ``render --from report.json``
possible — the same report re-rendered later produces byte-identical output.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar, Protocol, TextIO, runtime_checkable

from ..diff.model import ComparisonReport


@runtime_checkable
class Reporter(Protocol):
    """Renders a comparison in one output format."""

    name: ClassVar[str]

    def render(self, report: ComparisonReport, out: TextIO) -> None:
        """Write the report to an open stream."""
        ...


def render_to_path(reporter: Reporter, report: ComparisonReport, path: Path) -> None:
    """Render one report to a file, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        reporter.render(report, handle)
