"""Generate installer/assets/zaza.ico — a simple, ZaZa-owned placeholder icon
(white "Z" on a blue rounded square). Pure Python (zlib + struct), no image
library. Replace assets/zaza.ico with final artwork at any time.

Usage: python installer/make_icon.py
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

BLUE = (31, 95, 168, 255)
WHITE = (255, 255, 255, 255)
CLEAR = (0, 0, 0, 0)


def _pixel(x: float, y: float, n: int) -> tuple[int, int, int, int]:
    r = n * 0.18  # corner radius
    cx = min(max(x, r), n - r)
    cy = min(max(y, r), n - r)
    if (x - cx) ** 2 + (y - cy) ** 2 > r * r:
        return CLEAR
    m, t = n * 0.24, n * 0.13  # margin and stroke thickness of the "Z"
    if m <= x <= n - m and (m <= y <= m + t or n - m - t <= y <= n - m):
        return WHITE
    if m <= y <= n - m:
        # diagonal from top-right to bottom-left
        u = (y - m) / (n - 2 * m)
        diag_x = (n - m) - u * (n - 2 * m)
        if abs(x - diag_x) <= t * 0.75:
            return WHITE
    return BLUE


def png(n: int) -> bytes:
    raw = b"".join(b"\x00" + b"".join(bytes(_pixel(x + 0.5, y + 0.5, n)) for x in range(n)) for y in range(n))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", n, n, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def ico(sizes: tuple[int, ...] = (16, 24, 32, 48, 64, 256)) -> bytes:
    images = [png(n) for n in sizes]
    out = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    for n, data in zip(sizes, images, strict=True):
        out += struct.pack("<BBBBHHII", n % 256, n % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return out + b"".join(images)


if __name__ == "__main__":
    target = Path(__file__).resolve().parent / "assets" / "zaza.ico"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(ico())
    print(f"wrote {target} ({target.stat().st_size} bytes)")
