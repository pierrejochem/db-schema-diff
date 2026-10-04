#!/usr/bin/env python3
"""Write the raster logo files in `media/` and the Windows icon, from the brand mark in `logo.py`.

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

import struct
from pathlib import Path

from logo import render, write_png

MEDIA = Path(__file__).resolve().parent.parent / "media"

#: 1024 for a store listing or a retina README image; 512 and 256 for launchers and avatars; 128
#: down to 16 for a favicon and a window icon. Nothing else is needed, and a size nobody uses is a
#: file nobody updates.
SIZES = (1024, 512, 256, 128, 64, 32, 16)

#: The Windows icon, which `make exe` hands to Nuitka. Windows picks the nearest of these for the
#: taskbar, the title bar and Explorer; 256 is the one it scales for large views.
ICO = Path(__file__).resolve().parent / "db-schema-diff-gui.ico"
ICO_SIZES = (256, 64, 48, 32, 24, 16)


def write_ico(path: Path, sizes: tuple[int, ...]) -> None:
    """An .ico holding one PNG per size. Windows has read PNG-compressed entries since Vista.

    Stdlib only, like the rest of this script: the file is a 6-byte header, a 16-byte directory
    entry per image, then the images, and an entry's width and height of 0 means 256.
    """
    images = []
    for size in sizes:
        scratch = path.with_suffix(f".{size}.png")
        write_png(scratch, size, render(size))
        images.append((size, scratch.read_bytes()))
        scratch.unlink()
    offset = 6 + 16 * len(images)
    directory = b""
    for size, data in images:
        dimension = 0 if size >= 256 else size
        directory += struct.pack("<BBBBHHII", dimension, dimension, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    path.write_bytes(
        struct.pack("<HHH", 0, 1, len(images)) + directory + b"".join(d for _, d in images)
    )


def main() -> int:
    MEDIA.mkdir(exist_ok=True)
    for size in SIZES:
        path = MEDIA / f"logo-mark-{size}.png"
        write_png(path, size, render(size))
        print(f"wrote {path.relative_to(MEDIA.parent)} ({path.stat().st_size} bytes)")
    write_ico(ICO, ICO_SIZES)
    print(f"wrote {ICO.relative_to(MEDIA.parent)} ({ICO.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
