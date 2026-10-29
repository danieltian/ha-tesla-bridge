"""Generate the brand and app icons (stdlib only; no Tesla trademarks).

A rounded square in deep red with a white lightning bolt over a "bridge" arc.
Run from the repo root: ``python3 tools/gen_icons.py``.
"""

from __future__ import annotations

from pathlib import Path
import struct
import zlib

BG = (0xC8, 0x1E, 0x2B)
FG = (0xFF, 0xFF, 0xFF)

# Bolt polygon in unit coordinates (0..1).
BOLT = [
    (0.58, 0.12),
    (0.30, 0.54),
    (0.48, 0.54),
    (0.40, 0.88),
    (0.72, 0.42),
    (0.53, 0.42),
]


def _inside_polygon(x: float, y: float, poly: list[tuple[float, float]]) -> bool:
    inside = False
    j = len(poly) - 1
    for i, (xi, yi) in enumerate(poly):
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _inside_rounded(x: float, y: float, r: float) -> bool:
    cx = min(max(x, r), 1 - r)
    cy = min(max(y, r), 1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def _on_arc(x: float, y: float) -> bool:
    # A thin arch under the bolt: the "bridge".
    d = ((x - 0.5) ** 2 + (y - 1.05) ** 2) ** 0.5
    return 0.40 <= d <= 0.46 and y < 0.86


def render(width: int, height: int, ss: int = 4) -> bytes:
    rows = []
    for py in range(height):
        row = bytearray([0])  # filter type
        for px in range(width):
            acc = [0, 0, 0, 0]
            for sy in range(ss):
                for sx in range(ss):
                    # Square content area centered for non-square (logo) sizes.
                    side = min(width, height)
                    ox = (width - side) / 2
                    x = ((px + (sx + 0.5) / ss) - ox) / side
                    y = (py + (sy + 0.5) / ss) / side
                    if not (0 <= x <= 1 and 0 <= y <= 1) or not _inside_rounded(
                        x, y, 0.2
                    ):
                        continue
                    color = FG if _inside_polygon(x, y, BOLT) or _on_arc(x, y) else BG
                    acc[0] += color[0]
                    acc[1] += color[1]
                    acc[2] += color[2]
                    acc[3] += 255
            n = ss * ss
            alpha = acc[3] // n
            if alpha:
                row += bytes(
                    (
                        acc[0] * 255 // acc[3],
                        acc[1] * 255 // acc[3],
                        acc[2] * 255 // acc[3],
                        alpha,
                    )
                )
            else:
                row += b"\x00\x00\x00\x00"
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data))
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    targets = {
        root / "custom_components/tesla_bridge/brand/icon.png": (256, 256),
        root / "custom_components/tesla_bridge/brand/icon@2x.png": (512, 512),
        root / "apps/tesla_bridge/icon.png": (128, 128),
        root / "apps/tesla_bridge/logo.png": (250, 100),
    }
    for path, (w, h) in targets.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(render(w, h))
        print(f"wrote {path.relative_to(root)} ({w}x{h})")


if __name__ == "__main__":
    main()
