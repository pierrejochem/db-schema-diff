"""Unified diffs of definition text.

Pure, like every other module under ``report``: no IO, no clock, no config. The caller decides
how to paint the result, so the GUI and the HTML report render the same lines.

Line endings are normalised before comparing. Windows-authored migrations are common in this
platform, and without that a CRLF body against an LF one reports every line as changed — which is
the loudest possible way to say nothing.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from enum import StrEnum

__all__ = ["DiffKind", "DiffLine", "unified"]


class DiffKind(StrEnum):
    """What a rendered line is, so a caller can style it without parsing the text."""

    HUNK = "hunk"
    CONTEXT = "context"
    ADDED = "added"
    REMOVED = "removed"
    ELIDED = "elided"
    """Stands in for the lines a ``max_lines`` cap dropped."""


@dataclass(frozen=True, slots=True)
class DiffLine:
    kind: DiffKind
    text: str


def _lines(text: str | None) -> list[str]:
    """Split into comparable lines, ignoring line-ending and trailing-newline differences."""
    if text is None:
        return []
    return text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n").split("\n")


_PREFIXES = {
    "@": DiffKind.HUNK,
    "+": DiffKind.ADDED,
    "-": DiffKind.REMOVED,
    " ": DiffKind.CONTEXT,
}


def unified(
    master: str | None,
    target: str | None,
    *,
    context: int = 3,
    max_lines: int | None = None,
) -> tuple[DiffLine, ...]:
    """The difference between two definitions, as styled lines.

    Empty when the two are equivalent — including when both are absent — so a caller can tell
    "nothing to show" from "a change I should render".
    """
    if max_lines is not None and max_lines < 0:
        max_lines = 0

    before, after = _lines(master), _lines(target)
    if before == after:
        return ()

    raw = list(difflib.unified_diff(before, after, n=context, lineterm=""))
    # The first two lines are difflib's file headers. Filtering them by prefix instead would
    # discard a removed line that starts with "--" — a SQL comment — because difflib prefixes it
    # to "---". Dropping them by position cannot confuse content with a header.
    out: list[DiffLine] = []
    for line in raw[2:]:
        kind = _PREFIXES.get(line[:1], DiffKind.CONTEXT)
        out.append(DiffLine(kind=kind, text=line[1:] if kind is not DiffKind.HUNK else line))

    if max_lines is not None and len(out) > max_lines:
        dropped = len(out) - max_lines
        out = out[:max_lines]
        plural = "line" if dropped == 1 else "lines"
        out.append(DiffLine(kind=DiffKind.ELIDED, text=f"… {dropped} more {plural}"))

    return tuple(out)
