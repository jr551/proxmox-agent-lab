"""Minimal stdlib-only PNG writer.

The controller must run under the system interpreter, so image output cannot
depend on Pillow or numpy.
"""

from __future__ import annotations

import struct
import zlib


_GLYPHS = {
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"),
    "3": ("111", "001", "111", "001", "111"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"),
    "7": ("111", "001", "010", "010", "010"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "111"),
    "X": ("101", "101", "010", "101", "101"),
    "Y": ("101", "101", "010", "010", "010"),
}


def _chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def encode_png(width: int, height: int, rgb: bytes) -> bytes:
    """Encode a packed RGB buffer (3 bytes per pixel, top-down) as a PNG."""
    expected = width * height * 3
    if len(rgb) != expected:
        raise ValueError(f"expected {expected} RGB bytes, got {len(rgb)}")
    stride = width * 3
    raw = bytearray()
    for row in range(height):
        raw.append(0)  # filter type: none
        raw += rgb[row * stride : (row + 1) * stride]
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + _chunk(b"IEND", b"")
    )


def overlay_coordinate_grid(width: int, height: int, rgb: bytes,
                            step: int = 100) -> bytes:
    """Return RGB with a labelled coordinate grid over the original pixels.

    The canvas is never resized: labels describe the original framebuffer,
    with (0,0) at top-left, X increasing right and Y increasing down. This is
    model guidance only; the untouched capture remains the audit checkpoint.
    """
    expected = width * height * 3
    if len(rgb) != expected:
        raise ValueError(f"expected {expected} RGB bytes, got {len(rgb)}")
    if step < 20:
        raise ValueError("grid step must be at least 20 pixels")
    out = bytearray(rgb)

    def pixel(x: int, y: int, colour: tuple[int, int, int],
              alpha: int = 255) -> None:
        if not (0 <= x < width and 0 <= y < height):
            return
        offset = (y * width + x) * 3
        inverse = 255 - alpha
        for channel, target in enumerate(colour):
            out[offset + channel] = (
                out[offset + channel] * inverse + target * alpha
            ) // 255

    grid = (0, 255, 255)
    axis = (255, 224, 0)
    for x in range(0, width, step):
        for y in range(height):
            pixel(x, y, grid, 96)
    for y in range(0, height, step):
        for x in range(width):
            pixel(x, y, grid, 96)
    for x in range(width):
        pixel(x, 0, axis)
        pixel(x, 1, axis)
    for y in range(height):
        pixel(0, y, axis)
        pixel(1, y, axis)

    def text(value: str, x: int, y: int, scale: int = 2) -> None:
        cursor = x
        for character in value:
            glyph = _GLYPHS.get(character)
            if glyph is None:
                cursor += 4 * scale
                continue
            # Dark backing keeps coordinates legible on bright installers.
            for py in range(y - 1, y + 5 * scale + 1):
                for px in range(cursor - 1, cursor + 3 * scale + 1):
                    pixel(px, py, (0, 0, 0), 190)
            for row, bits in enumerate(glyph):
                for column, bit in enumerate(bits):
                    if bit == "1":
                        for dy in range(scale):
                            for dx in range(scale):
                                pixel(cursor + column * scale + dx,
                                      y + row * scale + dy, (255, 255, 255))
            cursor += 4 * scale

    for x in range(0, width, step):
        text(str(x), min(x + 3, max(3, width - len(str(x)) * 8)), 4)
    for y in range(step, height, step):
        text(str(y), 4, min(y + 3, height - 12))
    text("X", max(3, width - 10), 18)
    text("Y", 4, max(4, height - 12))
    return bytes(out)
