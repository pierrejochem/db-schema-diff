#!/usr/bin/env python3
"""The brand mark, drawn once, in one place.

The application has three things that have to look like the same product: the macOS `.icns`, the
PNGs the README and any launcher use, and the SVGs a document or a slide drops in. A mark redrawn
per output is a mark that drifts, so the geometry lives here as numbers on a 1024-unit grid and
every raster output is rendered from it. `media/*.svg` is the same grid written out by hand, which
is the one duplication: the suite pins the SVGs against these constants so the two cannot part.

What the mark says: two columns of rows are two environments' schemas side by side, the lime rule
between them is the comparison, and the row that is lime and short is the drift the tool found.
Rows one and three are the same width in both columns -- they match; row two is not -- it does not.
That is the whole product in one glyph, and it survives being 16px wide because it is four shapes.

Stdlib only. Pillow is not a dependency of this project and should not become one for a logo, so
PNGs are written by hand (zlib plus a few struct packs) and anti-aliased from a signed distance
rather than supersampled: one evaluation per pixel, which keeps a 1024px render to about a second.
Every size is rendered from the grid rather than resampled from the big one, so a 16px icon gets
its own anti-aliasing instead of a blur of the 1024.

Colours are straight from `gui/ui/tokens.slint` and nowhere else.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

#: The mark is authored on this grid; `render` scales every number by `size / GRID`.
GRID = 1024

# Straight from gui/ui/tokens.slint.
NAVY = (0x13, 0x2C, 0x5C)
LIME = (0xAA, 0xDC, 0x23)
WHITE = (0xFF, 0xFF, 0xFF)

#: macOS leaves a margin around the art: a 1024px icon is drawn at about 824px, so the ground
#: insets by 100 on every side. The SVGs keep the same inset so a mark pasted into a slide and the
#: mark in the dock are the same drawing.
MARGIN = 100
GROUND = (MARGIN, MARGIN, GRID - MARGIN, GRID - MARGIN)
GROUND_RADIUS = 180

#: The lime rule between the two columns: the comparison itself. It overshoots the rows block top
#: and bottom so it reads as an axis the rows hang off, not as a fourth row.
DIVIDER = (503, 300, 521, 724)
DIVIDER_RADIUS = 9

#: Rows. `(box, colour)`; the radius is half the height, so every row is a pill.
ROW_HEIGHT = 96
ROW_RADIUS = ROW_HEIGHT // 2
_LEFT = 192
_RIGHT = 540
ROW_WIDTH = 292
#: The one row that differs. Short *and* lime: the width carries at 16px where the hue has gone,
#: and the hue carries on a greyscale print where the width is all that is left.
DRIFT_WIDTH = 190
ROWS = (
    ((_LEFT, 312, _LEFT + ROW_WIDTH, 312 + ROW_HEIGHT), WHITE),
    ((_LEFT, 464, _LEFT + ROW_WIDTH, 464 + ROW_HEIGHT), WHITE),
    ((_LEFT, 616, _LEFT + ROW_WIDTH, 616 + ROW_HEIGHT), WHITE),
    ((_RIGHT, 312, _RIGHT + ROW_WIDTH, 312 + ROW_HEIGHT), WHITE),
    ((_RIGHT, 464, _RIGHT + DRIFT_WIDTH, 464 + ROW_HEIGHT), LIME),
    ((_RIGHT, 616, _RIGHT + ROW_WIDTH, 616 + ROW_HEIGHT), WHITE),
)


def rounded_rect_coverage(
    x: float, y: float, box: tuple[float, float, float, float], r: float
) -> float:
    """How much of the pixel at (x, y) lies inside the rounded rectangle, from 0 to 1.

    Anti-aliasing from the signed distance to the shape: one evaluation per pixel instead of the
    nine or sixteen a supersampled render would need.

    The distance has to be properly signed -- negative inside, positive outside. Clamping the point
    to the shape and measuring to it gives zero everywhere inside, which reads as half coverage, so
    a square-cornered bar came out a 50/50 blend with whatever was under it instead of its own
    colour. That is wrong only for r = 0, which is exactly what a hairline rule would be.
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
    """RGBA rows for the mark at `size` pixels square, as raw PNG scanlines.

    One pass, back to front: the navy ground decides the alpha, then the lime rule and the rows
    composite onto it. Outside the ground nothing is drawn at all -- the corners stay transparent,
    which is what lets the same render serve a dock icon and a mark on a white page.
    """
    scale = size / GRID

    def scaled(box: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
        return (box[0] * scale, box[1] * scale, box[2] * scale, box[3] * scale)

    ground = scaled(GROUND)
    corner = GROUND_RADIUS * scale
    divider = scaled(DIVIDER)
    divider_radius = DIVIDER_RADIUS * scale
    rows = [(scaled(box), colour) for box, colour in ROWS]
    row_radius = ROW_RADIUS * scale

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
            rule = rounded_rect_coverage(x, y, divider, divider_radius)
            if rule > 0.0:
                colour = mix(colour, LIME, rule)
            for box, row_colour in rows:
                cover = rounded_rect_coverage(x, y, box, row_radius)
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
