#!/usr/bin/env python3
"""Build the .icns macOS wants, from the brand mark in `logo.py`.

A committed binary with no provenance is a thing nobody can correct, so the icon is generated
rather than drawn: the geometry and the palette come from `packaging/logo.py`, which takes its
colours from `gui/ui/tokens.slint`. The icon and `media/logo-mark.svg` are therefore the same mark,
and a brand change is one edit in one file.

An .icns is a small container -- the bytes `icns`, a total length, then one entry per size made of
a four-letter type, its own length and a PNG -- so this writes it directly and needs neither `sips`
nor `iconutil`. That keeps it runnable on any machine, and renders every size from the grid rather
than shrinking one large image, so the 16 and 32 pixel entries are drawn for the size they are shown
at.

Run it when the brand changes:

    python3 packaging/make_icon.py

It rewrites packaging/db-schema-diff-gui.icns, which `make exe` passes to --macos-app-icon.
"""

from __future__ import annotations

import struct
import tempfile
from pathlib import Path

from logo import render, write_png

HERE = Path(__file__).resolve().parent
ICNS = HERE / "db-schema-diff-gui.icns"

#: Entry type -> pixel size. The `@2x` entries are the same picture at twice the pixels, which is
#: why a size can appear under two types; each type is a distinct slot the system looks up.
ENTRIES = (
    ("icp4", 16),
    ("icp5", 32),
    ("ic11", 32),  # 16@2x
    ("ic12", 64),  # 32@2x
    ("ic07", 128),
    ("ic13", 256),  # 128@2x
    ("ic08", 256),
    ("ic14", 512),  # 256@2x
    ("ic09", 512),
    ("ic10", 1024),  # 512@2x
)


def png_bytes(size: int) -> bytes:
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / f"{size}.png"
        write_png(path, size, render(size))
        return path.read_bytes()


def build() -> bytes:
    rendered = {size: png_bytes(size) for size in sorted({size for _, size in ENTRIES})}
    body = b"".join(
        kind.encode("ascii") + struct.pack(">I", 8 + len(rendered[size])) + rendered[size]
        for kind, size in ENTRIES
    )
    return b"icns" + struct.pack(">I", 8 + len(body)) + body


def main() -> int:
    ICNS.write_bytes(build())
    print(f"wrote {ICNS.relative_to(HERE.parent)} ({ICNS.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
