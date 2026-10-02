"""Entry point for the standalone executable.

Nuitka compiles a script rather than a package, so this is the single file the build points at.

It forwards to the GUI's own start-up instead of reimplementing it, because that module installs
the bundled typefaces *before* anything imports slint, and that ordering is load-bearing — setting
`SLINT_FONT_PATH` after the renderer starts is measurably a no-op (see `gui/fonts.py`). Importing
this module imports no GUI toolkit: everything `gui/launcher` touches lives inside its `main`.
"""

from __future__ import annotations

from cumo_schema_comparer.gui.launcher import main

if __name__ == "__main__":
    raise SystemExit(main())
