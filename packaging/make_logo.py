#!/usr/bin/env python3
"""Write the raster logo files in `media/`, from the brand mark in `logo.py`.

`media/*.svg` is the mark for anything that can scale -- a slide, a document, a web page. These
PNGs are for the places that cannot take an SVG: a Windows or Linux launcher entry, a favicon, a
README image, a store listing, a Slack or Confluence avatar.

Every size is rendered from the grid rather than resampled from the largest, so each one is
anti-aliased at the size it will actually be shown at. That matters most at 16 and 32, where a
downsample of the 1024 turns the lime rule into grey mush.

Stdlib only, and no macOS tools, so this runs on every platform CI uses.

Run it when the brand changes:

    python3 packaging/make_logo.py
"""

from __future__ import annotations

from pathlib import Path

from logo import render, write_png

MEDIA = Path(__file__).resolve().parent.parent / "media"

#: 1024 for a store listing or a retina README image; 512 and 256 for launchers and avatars; 128
#: down to 16 for a favicon and a window icon. Nothing else is needed, and a size nobody uses is a
#: file nobody updates.
SIZES = (1024, 512, 256, 128, 64, 32, 16)


def main() -> int:
    MEDIA.mkdir(exist_ok=True)
    for size in SIZES:
        path = MEDIA / f"logo-mark-{size}.png"
        write_png(path, size, render(size))
        print(f"wrote {path.relative_to(MEDIA.parent)} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
