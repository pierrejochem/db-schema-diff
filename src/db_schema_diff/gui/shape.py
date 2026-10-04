"""Credential-shape detection for the GUI.

The implementation lives in :mod:`db_schema_diff.literals` because build-time code
needs it too, and no library module may import from ``gui``. This module stays so GUI callers and
their tests keep one import path.
"""

from ..literals import looks_like_connection_string

__all__ = ["looks_like_connection_string"]
