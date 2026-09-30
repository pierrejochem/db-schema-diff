"""Entry point for `cumo-schema-diff-gui`."""

from __future__ import annotations

import sys

from . import MINIMUM_PYTHON


def main() -> int:
    """Start the application, or explain why it cannot start.

    The version check runs before any Slint import: on 3.11 the dependency is not installable at
    all, and an ImportError traceback would be a poor way to learn that.
    """
    if tuple(sys.version_info[:2]) < MINIMUM_PYTHON:
        required = ".".join(str(part) for part in MINIMUM_PYTHON)
        running = ".".join(str(part) for part in sys.version_info[:2])
        print(
            f"cumo-schema-diff-gui needs Python {required} or newer; this is {running}.\n"
            "The command-line tool still supports 3.11 — only the GUI needs the newer runtime.",
            file=sys.stderr,
        )
        return 2

    from .app import run  # type: ignore[import-untyped]

    return run()  # type: ignore[no-any-return]


if __name__ == "__main__":
    raise SystemExit(main())
