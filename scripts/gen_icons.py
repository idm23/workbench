#!/usr/bin/env python3
"""Regenerates the Workbench site icon: favicons, the iOS home-screen icon,
and the PWA manifest icons, from a hand-specified anvil design.

Run as a one-off dev-time tool (`uv run scripts/gen_icons.py`) rather than at
deploy time — the outputs are committed to `src/workbench/static/` like any
other static asset. Re-run this and commit the results if the design
changes; nothing regenerates it automatically, and nothing at runtime
depends on this script existing.

The design is a handful of flat convex shapes (two rectangles, a trapezoid,
a triangle) in a 100x100 coordinate space, shared between the SVG — drawn
directly as vector `<polygon>`s — and the raster PNGs, which are rendered at
high resolution with hard edges and then box-downsampled to each target
size. That downsample is what gives the small favicon sizes clean
anti-aliased edges instead of jagged ones, without reaching for an imaging
library — this server's Python is new enough (3.14) that a wheel for one
is not a given, and the shapes here are simple enough not to need one.
"""

from __future__ import annotations

import logging
import math
import struct
import zlib
from pathlib import Path

from workbench.logs import configure_console_logging

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent.parent / "src" / "workbench" / "static"
ICONS_DIR = STATIC_DIR / "icons"

# Colors lifted straight from base.html's --accent / --fg (light theme), so
# the icon reads as the same palette as the app rather than an unrelated
# color picked just for this one asset.
BG = (0xB4, 0x53, 0x09)  # --accent
FG = (0x1A, 0x1A, 0x18)  # --fg

# The anvil, as a handful of flat convex shapes in a 100x100 design space:
# a triangular horn, a rectangular face, a trapezoid waist that flares
# downward, and a wider rectangular base — rather than one hand-drawn
# polygon, so each piece is easy to reason about and re-tune independently.
HORN: list[tuple[float, float]] = [(10, 40), (34, 30), (34, 44)]
FACE = (34, 30, 90, 44)  # x0, y0, x1, y1
WAIST: list[tuple[float, float]] = [(46, 44), (78, 44), (86, 64), (38, 64)]
BASE = (30, 64, 94, 76)

RGB = tuple[int, int, int]
Point = tuple[float, float]


def _rect_to_poly(rect: tuple[float, float, float, float]) -> list[Point]:
    x0, y0, x1, y1 = rect
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def _polygons() -> list[list[Point]]:
    return [HORN, _rect_to_poly(FACE), WAIST, _rect_to_poly(BASE)]


def _point_in_convex_polygon(px: float, py: float, poly: list[Point]) -> bool:
    """True if (px, py) is inside the convex polygon `poly` (either winding)."""
    signs = set()
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        cross = (x2 - x1) * (py - y1) - (y2 - y1) * (px - x1)
        if cross != 0:
            signs.add(cross > 0)
    return len(signs) <= 1


def render_master(size: int) -> list[list[RGB]]:
    """Renders the design at `size`x`size` with hard edges (no AA yet).

    Only scans each polygon's own bounding box rather than every pixel
    against every shape — the shapes are a small fraction of the canvas, and
    at the 1024x1024 master size used below, scanning the whole canvas per
    shape would be needlessly slow in pure Python.
    """
    scale = size / 100
    pixels: list[list[RGB]] = [[BG] * size for _ in range(size)]
    for poly in _polygons():
        xs = [p[0] for p in poly]
        ys = [p[1] for p in poly]
        x0 = max(0, int(min(xs) * scale))
        x1 = min(size, math.ceil(max(xs) * scale))
        y0 = max(0, int(min(ys) * scale))
        y1 = min(size, math.ceil(max(ys) * scale))
        for y in range(y0, y1):
            cy = (y + 0.5) / scale
            row = pixels[y]
            for x in range(x0, x1):
                cx = (x + 0.5) / scale
                if _point_in_convex_polygon(cx, cy, poly):
                    row[x] = FG
    return pixels


def downsample(pixels: list[list[RGB]], src_size: int, dst_size: int) -> list[list[RGB]]:
    """Box-filter downsample: for a flat-color source this is what produces
    anti-aliased edges without any imaging library."""
    if src_size == dst_size:
        return pixels
    out: list[list[RGB]] = [[BG] * dst_size for _ in range(dst_size)]
    ratio = src_size / dst_size
    for dy in range(dst_size):
        y0 = int(dy * ratio)
        y1 = max(y0 + 1, int((dy + 1) * ratio))
        for dx in range(dst_size):
            x0 = int(dx * ratio)
            x1 = max(x0 + 1, int((dx + 1) * ratio))
            r = g = b = count = 0
            for sy in range(y0, y1):
                row = pixels[sy]
                for sx in range(x0, x1):
                    pr, pg, pb = row[sx]
                    r += pr
                    g += pg
                    b += pb
                    count += 1
            out[dy][dx] = (r // count, g // count, b // count)
    return out


def write_png(path: Path, pixels: list[list[RGB]], size: int) -> None:
    """Minimal, dependency-free 8-bit truecolor PNG encoder — just enough to
    write a flat-shaded icon, not a general-purpose one."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    raw = bytearray()
    for row in pixels:
        raw.append(0)  # filter type 0 (none), required before each scanline
        for r, g, b in row:
            raw += bytes((r, g, b))

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8-bit, color type 2 = RGB
    idat = zlib.compress(bytes(raw), 9)
    png = sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")
    path.write_bytes(png)


def write_svg(path: Path) -> None:
    def hexc(c: RGB) -> str:
        r, g, b = c
        return f"#{r:02x}{g:02x}{b:02x}"

    def pts(poly: list[Point]) -> str:
        return " ".join(f"{x},{y}" for x, y in poly)

    shapes = "\n  ".join(
        f'<polygon points="{pts(poly)}" fill="{hexc(FG)}"/>' for poly in _polygons()
    )
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <rect width="100" height="100" fill="{hexc(BG)}"/>
  {shapes}
</svg>
"""
    path.write_text(svg)


def main() -> None:
    configure_console_logging()
    ICONS_DIR.mkdir(parents=True, exist_ok=True)

    # Rendered well above the largest output and box-downsampled to each
    # target, rather than rendered fresh at each size.
    master_size = 1024
    master = render_master(master_size)

    targets = {
        "favicon-16.png": 16,
        "favicon-32.png": 32,
        # iOS's actual home-screen icon. Safari does not reliably honor the
        # manifest's icons for "Add to Home Screen" — this is the tag that
        # fixes the reported gray-box-with-a-W problem.
        "apple-touch-icon.png": 180,
        "icon-192.png": 192,
        "icon-512.png": 512,
    }
    for name, size in targets.items():
        pixels = downsample(master, master_size, size)
        write_png(ICONS_DIR / name, pixels, size)
        logger.info("wrote %s", ICONS_DIR / name)

    write_svg(ICONS_DIR / "icon.svg")
    logger.info("wrote %s", ICONS_DIR / "icon.svg")

    manifest_path = STATIC_DIR / "manifest.webmanifest"
    manifest_path.write_text(
        """{
  "name": "Workbench",
  "short_name": "Workbench",
  "icons": [
    { "src": "icons/icon-192.png", "sizes": "192x192", "type": "image/png" },
    { "src": "icons/icon-512.png", "sizes": "512x512", "type": "image/png" }
  ],
  "theme_color": "#b45309",
  "background_color": "#b45309",
  "display": "standalone"
}
"""
    )
    logger.info("wrote %s", manifest_path)


if __name__ == "__main__":
    main()
