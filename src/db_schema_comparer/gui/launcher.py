"""Start-up for `cumo-schema-diff-gui`, in an ordinary importable module.

This lives here rather than in `__main__.py` because a compiled build cannot import it from there.
Nuitka names each module's generated C file after the module, so a program whose own entry point is
`__main__` and which imports a package's `__main__` submodule produces two files called
`module.__main__.c` and aborts:

    AssertionError: build/main.build/module.__main__.c

`__main__.py` is now a shim over this, so `python -m db_schema_comparer.gui` still works and the
compiled binary imports a module with a name of its own.
"""

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
