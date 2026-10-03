#!/usr/bin/env python3
"""Build the .icns macOS wants, from the brand mark in `logo.py`.

A committed binary with no provenance is a thing nobody can correct, so the icon is generated
rather than drawn: the geometry and the palette come from `packaging/logo.py`, which takes its
colours from `gui/ui/tokens.slint`. The icon and `media/logo-mark.svg` are therefore the same mark,
and a brand change is one edit in one file.

`sips` resizes and `iconutil` assembles, both of which ship with macOS; this script does nothing
anywhere else, which is correct, because nothing but macOS consumes an `.icns`. The portable
outputs are `media/`, written by `make_logo.py`.

Run it when the brand changes:

    python3 packaging/make_icon.py

It rewrites packaging/cumo-schema-diff-gui.icns, which `make exe` passes to --macos-app-icon.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from logo import GRID, render, write_png

HERE = Path(__file__).resolve().parent
ICNS = HERE / "cumo-schema-diff-gui.icns"

#: Absolute, because these ship with macOS at fixed locations and a bare name would be
#: resolved through PATH.
SIPS = "/usr/bin/sips"
ICONUTIL = "/usr/bin/iconutil"


def main() -> int:
    if sys.platform != "darwin":
        print("iconutil and sips are macOS tools; nothing to do here.", file=sys.stderr)
        return 0

    iconset = HERE / "cumo-schema-diff-gui.iconset"
    iconset.mkdir(exist_ok=True)
    master = iconset / "icon_512x512@2x.png"
    write_png(master, GRID, render(GRID))

    # Every size the iconset format asks for, resampled by sips from the one render. Resampling is
    # acceptable here and not in media/: the Finder and the dock never show these at 16px without
    # also having the 32 and 64 to pick from, whereas a favicon or a README image is shown at
    # exactly the size it was written at.
    for name, pixels in (
        ("icon_16x16", 16),
        ("icon_16x16@2x", 32),
        ("icon_32x32", 32),
        ("icon_32x32@2x", 64),
        ("icon_128x128", 128),
        ("icon_128x128@2x", 256),
        ("icon_256x256", 256),
        ("icon_256x256@2x", 512),
        ("icon_512x512", 512),
    ):
        subprocess.run(  # noqa: S603 - fixed argv, no shell
            [
                SIPS,
                "-z",
                str(pixels),
                str(pixels),
                str(master),
                "--out",
                str(iconset / f"{name}.png"),
            ],
            check=True,
            capture_output=True,
        )

    subprocess.run(  # noqa: S603 - fixed argv, no shell
        [ICONUTIL, "--convert", "icns", str(iconset), "--output", str(ICNS)],
        check=True,
    )
    for leftover in iconset.glob("*.png"):
        leftover.unlink()
    iconset.rmdir()
    print(f"wrote {ICNS.relative_to(HERE.parent)} ({ICNS.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
