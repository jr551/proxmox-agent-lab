"""Minimal stdlib-only PNG writer.

The controller must run under the system interpreter, so image output cannot
depend on Pillow or numpy.
"""

from __future__ import annotations

import struct
import zlib


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
