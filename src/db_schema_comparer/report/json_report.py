"""The machine-readable report.

Deliberately lossless: the HTML and JUnit reporters must be reproducible from this file alone, so
that a comparison can be captured once in CI and rendered anywhere afterwards. Ignored findings
and notes are included for the same reason — a suppressed finding is auditable, not invisible.

Key order is the insertion order of :meth:`ComparisonReport.to_json_dict`, and that order is
stable, so two runs over unchanged databases produce byte-identical files that can be diffed.
"""

from __future__ import annotations

import json
from typing import ClassVar, TextIO

from ..diff.model import ComparisonReport


class JsonReporter:
    """Writes the report as JSON."""

    name: ClassVar[str] = "json"

    def __init__(self, *, indent: int | None = 2) -> None:
        self._indent = indent

    def render(self, report: ComparisonReport, out: TextIO) -> None:
        json.dump(
            report.to_json_dict(),
            out,
            indent=self._indent,
            # Schema identifiers are not ASCII-only in general, and escaping them would make the
            # file harder to read for no gain.
            ensure_ascii=False,
            sort_keys=False,
        )
        out.write("\n")


def load_report(text: str) -> ComparisonReport:
    """Rebuild a report from JSON text. The inverse of :class:`JsonReporter`."""
    return ComparisonReport.from_json_dict(json.loads(text))
