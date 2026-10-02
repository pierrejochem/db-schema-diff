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

    # Before the next line, and only before it: Slint reads SLINT_FONT_PATH when the renderer
    # starts, and `.app` imports slint at module level. Installing the fonts after that import is
    # measurably a no-op, so this call cannot move below it.
    from . import fonts

    fonts.install()

    from .app import run

    return run()


if __name__ == "__main__":
    raise SystemExit(main())
