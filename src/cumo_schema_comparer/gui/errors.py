"""Errors the user sees, rather than tracebacks."""

from __future__ import annotations


class GuiError(Exception):
    """Something the user needs to know, optionally tied to a form field.

    ``field`` is a pydantic-style path such as ``targets.1.dsn_env``, so the window can show the
    message against the offending input instead of in a dialog.
    """

    def __init__(self, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field
