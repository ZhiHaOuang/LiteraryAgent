"""Render the actual terminal pixels to GIF/PNG, using only the standard library."""

from __future__ import annotations

import argparse
import struct
import zlib
from pathlib import Path

from lg_cli.slime_animation import (
    BODY,
    FRAME_COUNT,
    FRAME_INTERVAL,
    HEIGHT,
    WIDTH,
    frame_pixels,
)

PALETTE = ((24, 25, 25), (220, 121, 95), (232, 184, 102), (255, 255, 255))


def indexed_frame(index: int, scale: int = 6) -> bytes:
    data = bytearray()
    for row in frame_pixels(index):
        line = b"".join(
            bytes([0 if pixel is None else 1 if pixel == BODY else 2]) * scale
            for pixel in row
        )
        data.extend(line * scale)
    return bytes(data)


def gif_data() -> bytes:
    width, height = WIDTH * 6, HEIGHT * 6
    data = bytearray(b"GIF89a" + struct.pack("<HHBBB", width, height, 0xF1, 0, 0))
    data.extend(bytes(channel for color in PALETTE for channel in color))
    data.extend(b"\x21\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00")
    for index in range(FRAME_COUNT):
        delay = round((index + 1) * FRAME_INTERVAL * 100) - round(
            index * FRAME_INTERVAL * 100
        )
        data.extend(b"\x21\xf9\x04\x04" + struct.pack("<H", delay) + b"\x00\x00")
        data.extend(b"," + struct.pack("<HHHHB", 0, 0, width, height, 0))
        # Frequent clear codes keep LZW at three bits; preview size is bounded.
        encoded = bytearray()
        bits = count = 0
        for pixel in indexed_frame(index):
            bits |= (4 | (pixel << 3)) << count
            count += 6
            while count >= 8:
                encoded.append(bits & 255)
                bits >>= 8
                count -= 8
        bits |= 5 << count
        count += 3
        while count > 0:
            encoded.append(bits & 255)
            bits >>= 8
            count -= 8
        data.append(2)
        for offset in range(0, len(encoded), 255):
            block = encoded[offset : offset + 255]
            data.append(len(block))
            data.extend(block)
        data.append(0)
    data.append(0x3B)
    return bytes(data)


def png_sheet() -> bytes:
    # Left to right: anticipation, stretch, landing, then the quick return.
    indices = tuple(
        round(t / FRAME_INTERVAL)
        for t in (0, 0.4, 0.65, 1.1, 1.45, 1.7, 1.8, 1.95, 2.2, 2.4, 2.6, 2.8)
    )
    tile_w, tile_h = WIDTH * 6, HEIGHT * 6
    width, height = tile_w * 4, tile_h * 3
    rows = bytearray()
    frames = [indexed_frame(index) for index in indices]
    for y in range(height):
        rows.append(0)
        for x in range(width):
            frame = frames[(y // tile_h) * 4 + x // tile_w]
            color = frame[(y % tile_h) * tile_w + x % tile_w]
            rows.extend(PALETTE[color])

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack("!I", len(payload))
            + kind
            + payload
            + struct.pack("!I", zlib.crc32(kind + payload))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    for name, content in (
        ("slime-preview.gif", gif_data()),
        ("slime-frames.png", png_sheet()),
    ):
        target = args.output / name
        target.write_bytes(content)
        print(target)


if __name__ == "__main__":
    main()
