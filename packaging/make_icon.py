#!/usr/bin/env python3
"""Draw the application icon from the design tokens, and build the .icns macOS wants.

A committed binary with no provenance is a thing nobody can correct, so the icon is generated from
the same palette the application uses: navy ground, the lime accent edge that marks the master
source and the selected view, and three rows standing for a schema with one of them different.

Stdlib only. Pillow is not a dependency of this project and should not become one for an icon, so
the PNG is written by hand (zlib plus a few struct packs) and anti-aliased from a signed distance
rather than supersampled, which keeps a 1024px render to about a second. `sips` resizes and
`iconutil` assembles, both of which ship with macOS.

Run it when the brand changes:

    python3 packaging/make_icon.py

It rewrites packaging/cumo-schema-diff-gui.icns, which `make exe` passes to --macos-app-icon.
"""

from __future__ import annotations

import struct
import subprocess
import sys
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
SIZE = 1024
ICNS = HERE / "cumo-schema-diff-gui.icns"

#: Absolute, because these ship with macOS at fixed locations and a bare name would be
#: resolved through PATH.
SIPS = "/usr/bin/sips"
ICONUTIL = "/usr/bin/iconutil"

# Straight from gui/ui/tokens.slint.
NAVY = (0x13, 0x2C, 0x5C)
LIME = (0xAA, 0xDC, 0x23)
WHITE = (0xFF, 0xFF, 0xFF)


def rounded_rect_coverage(
    x: float, y: float, box: tuple[float, float, float, float], r: float
) -> float:
    """How much of the pixel at (x, y) lies inside the rounded rectangle, from 0 to 1.

    Anti-aliasing from the signed distance to the shape: one evaluation per pixel instead of the
    nine or sixteen a supersampled render would need.

    The distance has to be properly signed — negative inside, positive outside. Clamping the point
    to the shape and measuring to it gives zero everywhere inside, which reads as half coverage, so
    a square-cornered bar came out a 50/50 blend with whatever was under it instead of its own
    colour. That is wrong only for r = 0, which is exactly the accent edge.
    """
    left, top, right, bottom = box
    half_w = (right - left) / 2 - r
    half_h = (bottom - top) / 2 - r
    qx = abs(x - (left + right) / 2) - half_w
    qy = abs(y - (top + bottom) / 2) - half_h
    outside = (max(qx, 0.0) ** 2 + max(qy, 0.0) ** 2) ** 0.5
    distance = outside + min(max(qx, qy), 0.0) - r
    return min(max(0.5 - distance, 0.0), 1.0)


def mix(
    under: tuple[int, int, int], over: tuple[int, int, int], alpha: float
) -> tuple[int, int, int]:
    return (
        round(under[0] + (over[0] - under[0]) * alpha),
        round(under[1] + (over[1] - under[1]) * alpha),
        round(under[2] + (over[2] - under[2]) * alpha),
    )


def render(size: int) -> bytes:
    """RGBA rows for the icon, as raw scanlines."""
    scale = size / 1024
    # macOS leaves a margin around the art; 1024px icons are drawn at about 824px.
    margin = 100 * scale
    ground = (margin, margin, size - margin, size - margin)
    corner = 180 * scale

    # The accent edge, in the same proportion the application draws it.
    edge = (margin, margin, margin + 86 * scale, size - margin)

    # Three rows: the middle one is the drift, so it is lime and a different width.
    row_left = margin + 190 * scale
    rows = [
        ((row_left, 330 * scale, row_left + 480 * scale, 330 * scale + 74 * scale), WHITE),
        ((row_left, 475 * scale, row_left + 300 * scale, 475 * scale + 74 * scale), LIME),
        ((row_left, 620 * scale, row_left + 430 * scale, 620 * scale + 74 * scale), WHITE),
    ]
    row_corner = 37 * scale

    raw = bytearray()
    for py in range(size):
        raw.append(0)  # PNG filter type 0 for this scanline
        y = py + 0.5
        for px in range(size):
            x = px + 0.5
            alpha = rounded_rect_coverage(x, y, ground, corner)
            if alpha <= 0.0:
                raw += b"\x00\x00\x00\x00"
                continue
            colour = NAVY
            edge_cover = rounded_rect_coverage(x, y, edge, 0.0)
            if edge_cover > 0.0:
                colour = mix(colour, LIME, edge_cover)
            for box, row_colour in rows:
                cover = rounded_rect_coverage(x, y, box, row_corner)
                if cover > 0.0:
                    colour = mix(colour, row_colour, cover)
            raw += bytes((colour[0], colour[1], colour[2], round(alpha * 255)))
    return bytes(raw)


def chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def write_png(path: Path, size: int, raw: bytes) -> None:
    header = struct.pack(">2I5B", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA, no interlace
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def main() -> int:
    if sys.platform != "darwin":
        print("iconutil and sips are macOS tools; nothing to do here.", file=sys.stderr)
        return 0

    iconset = HERE / "cumo-schema-diff-gui.iconset"
    iconset.mkdir(exist_ok=True)
    master = iconset / "icon_512x512@2x.png"
    write_png(master, SIZE, render(SIZE))

    # Every size the iconset format asks for, resampled by sips from the one render.
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
