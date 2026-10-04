"""Makes `python -m db_schema_comparer.gui` work.

A shim only: the start-up itself is in `launcher.py`, because a compiled build cannot import a
module called `__main__` without colliding with its own. See that module for the details.
"""

from __future__ import annotations

from .launcher import main

if __name__ == "__main__":
    raise SystemExit(main())
