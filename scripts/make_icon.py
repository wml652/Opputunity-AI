"""Generate the Mission Control .ico (stdlib only): graphite rounded square, radar ring, sweep
line, and an emerald signal dot. PNG-compressed frames at 16-256 px, 4x supersampled."""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

SIZES = (16, 24, 32, 48, 64, 128, 256)
SS = 4  # supersampling factor

BG = (23, 23, 26)
BORDER = (58, 58, 66)
RING = (160, 160, 170)
SWEEP = (110, 110, 120)
DOT = (52, 211, 153)


def _render(size: int) -> bytes:
    n = size * SS
    radius = n * 0.2
    cx = cy = n / 2
    ring_r = n * 0.30
    ring_w = max(n * 0.035, SS * 1.2)
    dot_angle = math.radians(-40)
    dot_x = cx + ring_r * math.cos(dot_angle)
    dot_y = cy + ring_r * math.sin(dot_angle)
    dot_r = n * 0.085
    border_w = max(n * 0.02, SS * 0.8)

    def sample(x: float, y: float) -> tuple[int, int, int, int]:
        # rounded-square signed distance
        qx = abs(x - cx) - (n / 2 - radius)
        qy = abs(y - cy) - (n / 2 - radius)
        outside = math.hypot(max(qx, 0), max(qy, 0)) + min(max(qx, qy), 0) - radius
        if outside > 0:
            return (0, 0, 0, 0)
        color = BORDER if outside > -border_w else BG
        d = math.hypot(x - cx, y - cy)
        # sweep line from center toward the dot
        t = ((x - cx) * math.cos(dot_angle) + (y - cy) * math.sin(dot_angle)) / ring_r
        perp = abs(-(x - cx) * math.sin(dot_angle) + (y - cy) * math.cos(dot_angle))
        if 0 <= t <= 1 and perp < ring_w * 0.45:
            color = SWEEP
        if abs(d - ring_r) < ring_w / 2:
            color = RING
        if d < ring_w * 0.9:
            color = RING
        if math.hypot(x - dot_x, y - dot_y) < dot_r:
            color = DOT
        return (*color, 255)

    rows = bytearray()
    for py in range(size):
        rows.append(0)  # PNG filter: none
        for px in range(size):
            acc = [0, 0, 0, 0]
            for sy in range(SS):
                for sx in range(SS):
                    r, g, b, a = sample(px * SS + sx + 0.5, py * SS + sy + 0.5)
                    acc[0] += r * a
                    acc[1] += g * a
                    acc[2] += b * a
                    acc[3] += a
            alpha = acc[3] / (SS * SS)
            if acc[3]:
                rows += bytes(
                    (acc[0] // acc[3], acc[1] // acc[3], acc[2] // acc[3], round(alpha))
                )
            else:
                rows += b"\x00\x00\x00\x00"
    return _png(size, bytes(rows))


def _png(size: int, raw: bytes) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def write_icon(path: Path) -> Path:
    frames = [(_s, _render(_s)) for _s in SIZES]
    header = struct.pack("<HHH", 0, 1, len(frames))
    offset = 6 + 16 * len(frames)
    directory = b""
    payload = b""
    for size, png in frames:
        dim = 0 if size >= 256 else size
        directory += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
        payload += png
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + directory + payload)
    return path


if __name__ == "__main__":
    out = write_icon(Path(__file__).parent / "assets" / "mission-control.ico")
    print(out)  # noqa: T201
