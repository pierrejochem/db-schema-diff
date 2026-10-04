"""The bundled brand typefaces, and the one way to make Slint use them.

The design system names three families — Archivo for display, Mulish for body and UI, IBM Plex
Mono for machine text. None of them is installed on a typical machine, and a ``font-family`` Slint
cannot resolve falls back silently rather than failing, so naming them alone would render the
default face and nobody would notice.

Slint's Python binding exposes no font-registration call. What the runtime does honour is
``SLINT_FONT_PATH``, a directory it scans for faces — and it must be set **before** ``import
slint``. Setting it afterwards is measurably a no-op: the same string rendered under a named family
measures 167px either way, where a loaded face gives 177px. That is why this module is stdlib-only
and why :func:`install` is called from ``__main__`` before the application module is imported.

A zipimported install has no real directory to point at. That case falls back to the system faces,
which is why the type scale and the colour tokens carry the design on their own.
"""

from __future__ import annotations

import os
from importlib import resources
from pathlib import Path

#: The environment variable Slint scans for additional faces. Read once, at renderer start-up.
FONT_PATH_VARIABLE = "SLINT_FONT_PATH"

#: The families the bundled files provide, for the error message and for tests.
BUNDLED_FAMILIES = ("Archivo", "Mulish", "IBM Plex Mono")


def font_directory() -> Path | None:
    """The directory holding the bundled faces, or ``None`` when there is no real one.

    Returns a filesystem path rather than a ``Traversable`` because Slint needs a directory it can
    scan itself. An install that keeps the package zipped therefore has no usable directory, and
    the caller falls back to system faces.
    """
    candidate = resources.files(__package__).joinpath("fonts")
    try:
        path = Path(str(candidate))
    except (TypeError, ValueError):  # pragma: no cover - a non-filesystem loader
        return None
    return path if path.is_dir() else None


def install() -> Path | None:
    """Point Slint at the bundled faces, unless the environment already chose a directory.

    Returns the directory that will be used, or ``None`` when the variable was left alone. An
    existing value always wins: someone pointing the variable at their own licensed cut of the
    brand faces is doing the right thing, and this should not overwrite it.

    Must be called before ``import slint``. Later is silently ineffective.
    """
    if os.environ.get(FONT_PATH_VARIABLE):
        return None
    directory = font_directory()
    if directory is None:
        return None
    os.environ[FONT_PATH_VARIABLE] = str(directory)
    return directory
