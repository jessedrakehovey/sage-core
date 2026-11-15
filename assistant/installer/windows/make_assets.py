"""make_assets.py - the app's icon and the installer's corner image, drawn by code (sage-core, 2026-11-14).

  python assistant/installer/windows/make_assets.py <out folder>

Writes sage.ico (16 to 256 px: the installer's icon, the Start-menu and desktop entries, Add or remove programs) and
wizard-small-*.bmp (the mark in the installer's top corner, one per screen scale). The mark is the one in the app's
window (assistant/app_ui.html): a deep green rounded tile with a white leaf. It is drawn from its geometry, so the
repository carries no picture files and the build needs nothing but Python's standard library.
"""
from __future__ import annotations

import math
import struct
import sys
import zlib
from pathlib import Path

GREEN = (0x17, 0x78, 0x5F)
WHITE = (255, 255, 255)
# the leaf, in the mark's own 32-unit square: two arcs of radius R between the tips (0, -A) and (0, A), turned 45 degrees
A, R = 10.5, 12.6
D = math.sqrt(R * R - A * A)
VEIN_TOP, VEIN_W = -2.5, 1.7
COS = SIN = math.sqrt(0.5)


def _clamp(v: float) -> float:
    return 0.0 if v < 0 else 1.0 if v > 1 else v


def mark(n: int, pad: float = 0.0, back: tuple | None = None) -> list:
    """n x n pixels of the mark as rows of (r, g, b, a). pad = empty border as a share of n; back = a solid colour
    behind it (then every pixel is opaque). Edges are smoothed from the exact distance to each shape."""
    inner = n * (1 - 2 * pad)
    scale = inner / 32.0                       # pixels per unit of the mark's square
    off = n * pad
    rad = 9.0                                  # the tile's corner radius, in units
    rows = []
    for y in range(n):
        row = []
        for x in range(n):
            ux, uy = (x + 0.5 - off) / scale, (y + 0.5 - off) / scale      # this pixel's centre, in units
            qx, qy = abs(ux - 16) - (16 - rad), abs(uy - 16) - (16 - rad)
            d_tile = math.hypot(max(qx, 0), max(qy, 0)) + min(max(qx, qy), 0) - rad
            a_tile = _clamp(0.5 - d_tile * scale)
            dx, dy = ux - 16, uy - 16
            lx, ly = dx * COS + dy * SIN, -dx * SIN + dy * COS             # into the leaf's own upright frame
            d_leaf = max(math.hypot(lx + D, ly) - R, math.hypot(lx - D, ly) - R)
            a_leaf = _clamp(0.5 - d_leaf * scale)
            if n >= 24:                                                     # the vein: too fine to read on the smallest sizes
                cy = min(max(ly, VEIN_TOP), A)
                a_leaf *= 1 - _clamp(0.5 - (math.hypot(lx, ly - cy) - VEIN_W / 2) * scale)
            rgb = tuple(GREEN[i] + (WHITE[i] - GREEN[i]) * a_leaf for i in range(3))
            if back is not None:
                row.append(tuple(int(round(back[i] + (rgb[i] - back[i]) * a_tile)) for i in range(3)) + (255,))
            else:
                row.append(tuple(int(round(c)) for c in rgb) + (int(round(255 * a_tile)),))
        rows.append(row)
    return rows


def png(rows: list) -> bytes:
    n_w, n_h = len(rows[0]), len(rows)
    raw = b"".join(b"\x00" + bytes(c for px in row for c in px) for row in rows)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", n_w, n_h, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def dib32(rows: list) -> bytes:
    """An icon's picture the classic way: a 32-bit bitmap (bottom row first, blue-green-red-alpha) and an empty mask."""
    n = len(rows)
    head = struct.pack("<IiiHHIIiiII", 40, n, n * 2, 1, 32, 0, n * n * 4, 0, 0, 0, 0)
    pix = b"".join(bytes((b, g, r, a)) for row in reversed(rows) for (r, g, b, a) in row)
    mask = b"\x00" * (((n + 31) // 32) * 4 * n)
    return head + pix + mask


def ico(sizes=(16, 20, 24, 32, 40, 48, 64, 128, 256)) -> bytes:
    pics = [(n, png(mark(n)) if n >= 256 else dib32(mark(n))) for n in sizes]
    out = struct.pack("<HHH", 0, 1, len(pics))
    at = 6 + 16 * len(pics)
    for n, data in pics:
        out += struct.pack("<BBBBHHII", n % 256, n % 256, 0, 0, 1, 32, len(data), at)
        at += len(data)
    return out + b"".join(d for _, d in pics)


def bmp24(w: int, h: int, back: tuple = WHITE) -> bytes:
    """The mark centred on a w x h field of one colour, as a 24-bit bitmap (what the installer's corner image is)."""
    n = min(w, h)
    m = mark(n, pad=0.1, back=back)
    ox, oy = (w - n) // 2, (h - n) // 2
    stride = (w * 3 + 3) & ~3
    body = bytearray()
    for y in range(h - 1, -1, -1):
        line = bytearray()
        for x in range(w):
            r, g, b = m[y - oy][x - ox][:3] if 0 <= y - oy < n and 0 <= x - ox < n else back
            line += bytes((b, g, r))
        body += line + b"\x00" * (stride - w * 3)
    return b"BM" + struct.pack("<IHHI", 54 + len(body), 0, 0, 54) + struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, len(body), 2835, 2835, 0, 0) + bytes(body)


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    out.mkdir(parents=True, exist_ok=True)
    (out / "sage.ico").write_bytes(ico())
    for w, h in ((55, 55), (83, 80), (110, 106), (138, 140)):
        (out / f"wizard-small-{w}.bmp").write_bytes(bmp24(w, h))
    (out / "sage-256.png").write_bytes(png(mark(256)))
    print(f"assets in {out}: sage.ico, wizard-small-*.bmp, sage-256.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
