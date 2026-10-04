"""Proof that the bundled faces are actually in use, not merely named.

Slint resolves an unknown ``font-family`` to the default face without raising, so every other test
in this suite would pass with the fonts absent, mis-named, or installed too late. Text advance width
is the only observable that differs, so these tests measure it.

Each case runs in a subprocess, because ``SLINT_FONT_PATH`` is read once when the renderer starts
and cannot be changed within a live process.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("slint", reason="the GUI extra is not installed")

PROBE = Path(__file__).parent / "harness" / "font_metrics.slint"

MEASURE = """
import json, os, sys
install = os.environ.pop("MEASURE_INSTALL", "") == "1"
if install:
    from db_schema_comparer.gui import fonts
    fonts.install()
import slint
ui = slint.load_file(sys.argv[1])
window = ui.FontMetrics()
window.show()
print(json.dumps({
    "body": float(window.body_width),
    "display": float(window.display_width),
    "mono": float(window.mono_width),
    "fallback": float(window.fallback_width),
}))
"""


def measure(*, install: bool, font_path: str | None) -> dict[str, float]:
    """Width of the same string under each named family, in a fresh interpreter."""
    environment = {
        **{k: v for k, v in __import__("os").environ.items() if k != "SLINT_FONT_PATH"},
        "MEASURE_INSTALL": "1" if install else "0",
    }
    if font_path is not None:
        environment["SLINT_FONT_PATH"] = font_path
    finished = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", MEASURE, str(PROBE)],
        capture_output=True,
        text=True,
        env=environment,
        timeout=120,
        check=True,
    )
    return json.loads(finished.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def with_fonts() -> dict[str, float]:
    return measure(install=True, font_path=None)


@pytest.fixture(scope="module")
def without_fonts(tmp_path_factory) -> dict[str, float]:
    # An empty directory rather than an unset variable: install() leaves an existing value alone,
    # so this is how the baseline is obtained without editing the module under test.
    empty = tmp_path_factory.mktemp("no-fonts")
    return measure(install=True, font_path=str(empty))


def test_without_the_bundled_faces_every_family_collapses_to_one(without_fonts):
    """The failure this whole module exists to detect: three families, one actual face."""
    widths = set(without_fonts.values())
    assert len(widths) == 1, f"expected a single fallback face, measured {without_fonts}"


def test_installing_the_fonts_changes_what_is_rendered(with_fonts, without_fonts):
    assert with_fonts["body"] != without_fonts["body"]
    assert with_fonts["display"] != without_fonts["display"]
    assert with_fonts["mono"] != without_fonts["mono"]


def test_the_three_families_render_as_three_different_faces(with_fonts):
    measured = (with_fonts["body"], with_fonts["display"], with_fonts["mono"])
    assert len(set(measured)) == 3, f"families are not distinct: {with_fonts}"


def test_the_monospace_face_is_the_widest(with_fonts):
    # A sanity check on identity rather than mere difference: a monospaced face advances every
    # glyph equally, so this string is wider in it than in either proportional face.
    assert with_fonts["mono"] > with_fonts["body"]
    assert with_fonts["mono"] > with_fonts["display"]
