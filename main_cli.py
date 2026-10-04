"""Entry point for the standalone command-line executable.

A second script because Nuitka compiles one script into one binary, and the two entry points are
genuinely different programs: this one has no GUI toolkit behind it and still runs on Python 3.11,
which is the floor the command-line tool keeps.
"""

from __future__ import annotations

from db_schema_diff.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
