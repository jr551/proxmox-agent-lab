"""Minimal stdlib-only PNG writer, reader and resampler.

The controller must run under the system interpreter, so image handling cannot
depend on Pillow or numpy. `zlib` plus a few loops is enough for the shapes
this project produces and consumes: 8-bit, non-interlaced PNGs.
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


def highlight_changes(width: int, height: int, current: bytes, previous: bytes,
                      threshold: int = 24) -> tuple[bytes, int]:
    """Dim stable pixels and outline regions changed since the prior frame.

    This image is model guidance only.  The untouched current frame remains
    the audit checkpoint.  A small per-channel threshold ignores compression
    noise and cursor antialiasing; changed pixels stay full-bright while their
    one-pixel boundary is coloured magenta.
    """
    expected = width * height * 3
    if len(current) != expected or len(previous) != expected:
        raise ValueError("current and previous RGB buffers must match the canvas")
    if not 0 <= threshold <= 255:
        raise ValueError("threshold must be between 0 and 255")
    mask = bytearray(width * height)
    changed = 0
    for pixel_index in range(width * height):
        offset = pixel_index * 3
        if max(abs(current[offset + channel] - previous[offset + channel])
               for channel in range(3)) >= threshold:
            mask[pixel_index] = 1
            changed += 1
    out = bytearray(len(current))
    for pixel_index, is_changed in enumerate(mask):
        offset = pixel_index * 3
        if is_changed:
            out[offset:offset + 3] = current[offset:offset + 3]
        else:
            for channel in range(3):
                out[offset + channel] = current[offset + channel] * 35 // 100
    # Outline stable pixels immediately adjacent to a changed region.  This
    # makes tiny controls and progress deltas visible without filling them in.
    for y in range(height):
        for x in range(width):
            index = y * width + x
            if mask[index]:
                continue
            neighbours = (
                (x > 0 and mask[index - 1])
                or (x + 1 < width and mask[index + 1])
                or (y > 0 and mask[index - width])
                or (y + 1 < height and mask[index + width])
            )
            if neighbours:
                offset = index * 3
                out[offset:offset + 3] = b"\xff\x00\xff"
    return bytes(out), changed


def _chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def encode_png(width: int, height: int, rgb: bytes, level: int = 6) -> bytes:
    """Encode a packed RGB buffer (3 bytes per pixel, top-down) as a PNG.

    `level` is the zlib compression level. The default keeps ordinary captures
    fast to write; callers that must fit an image inside a byte budget pass 9.
    """
    expected = width * height * 3
    if len(rgb) != expected:
        raise ValueError(f"expected {expected} RGB bytes, got {len(rgb)}")
    if not 0 <= level <= 9:
        raise ValueError("zlib compression level must be between 0 and 9")
    stride = width * 3
    raw = bytearray()
    for row in range(height):
        raw.append(0)  # filter type: none
        raw += rgb[row * stride : (row + 1) * stride]
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(bytes(raw), level))
        + _chunk(b"IEND", b"")
    )


def downscale_rgb(width: int, height: int, rgb: bytes,
                  max_edge: int) -> tuple[int, int, bytes]:
    """Shrink a packed RGB buffer so its longest edge fits `max_edge`.

    Every source pixel contributes to exactly one destination pixel and each
    destination pixel is the average of its box, so small text stays legible.
    Nearest-neighbour sampling would drop whole scanlines and columns, which
    is precisely what destroys 8-pixel-wide glyphs. An image already within
    `max_edge` is returned untouched, buffer and all.
    """
    expected = width * height * 3
    if len(rgb) != expected:
        raise ValueError(f"expected {expected} RGB bytes, got {len(rgb)}")
    if max_edge < 1:
        raise ValueError("max_edge must be at least 1 pixel")
    longest = max(width, height)
    if longest <= max_edge:
        return width, height, rgb
    # Round to nearest so the aspect ratio survives; the longest edge lands
    # exactly on max_edge because a half-pixel can never carry it past.
    new_width = max(1, (width * max_edge + longest // 2) // longest)
    new_height = max(1, (height * max_edge + longest // 2) // longest)
    columns = [x * new_width // width for x in range(width)]
    totals = [0] * (new_width * new_height * 3)
    counts = [0] * (new_width * new_height)
    for y in range(height):
        row_base = (y * new_height // height) * new_width
        offset = y * width * 3
        for x in range(width):
            cell = row_base + columns[x]
            counts[cell] += 1
            target = cell * 3
            totals[target] += rgb[offset]
            totals[target + 1] += rgb[offset + 1]
            totals[target + 2] += rgb[offset + 2]
            offset += 3
    out = bytearray(new_width * new_height * 3)
    for cell, count in enumerate(counts):
        if not count:
            continue
        target = cell * 3
        out[target] = totals[target] // count
        out[target + 1] = totals[target + 1] // count
        out[target + 2] = totals[target + 2] // count
    return new_width, new_height, bytes(out)


def _unfilter_row(line: bytearray, previous: bytes, filter_type: int,
                  bpp: int) -> None:
    """Reverse one PNG scanline filter in place (RFC 2083 section 6)."""
    if filter_type == 0:
        return
    if filter_type == 1:  # Sub
        for index in range(bpp, len(line)):
            line[index] = (line[index] + line[index - bpp]) & 0xFF
    elif filter_type == 2:  # Up
        for index in range(len(line)):
            line[index] = (line[index] + previous[index]) & 0xFF
    elif filter_type == 3:  # Average
        for index in range(len(line)):
            left = line[index - bpp] if index >= bpp else 0
            line[index] = (
                line[index] + ((left + previous[index]) >> 1)
            ) & 0xFF
    elif filter_type == 4:  # Paeth
        for index in range(len(line)):
            left = line[index - bpp] if index >= bpp else 0
            up = previous[index]
            up_left = previous[index - bpp] if index >= bpp else 0
            estimate = left + up - up_left
            da, db, dc = (
                abs(estimate - left), abs(estimate - up), abs(estimate - up_left)
            )
            if da <= db and da <= dc:
                predictor = left
            elif db <= dc:
                predictor = up
            else:
                predictor = up_left
            line[index] = (line[index] + predictor) & 0xFF
    else:
        raise ValueError(f"unknown PNG filter type {filter_type}")


def decode_png(data: bytes) -> tuple[int, int, bytes]:
    """Decode an 8-bit non-interlaced PNG into a packed RGB buffer.

    Enough of the format to read back what this project writes and what QEMU's
    `screendump -f png` produces: greyscale, RGB, and either of those with an
    alpha channel, which is discarded. Palette and 16-bit images are refused
    rather than guessed at.
    """
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    header: tuple[int, ...] | None = None
    compressed = bytearray()
    position = 8
    while position + 8 <= len(data):
        length = struct.unpack(">I", data[position:position + 4])[0]
        tag = data[position + 4:position + 8]
        payload = data[position + 8:position + 8 + length]
        if len(payload) != length:
            raise ValueError("truncated PNG chunk")
        position += 12 + length
        if tag == b"IHDR":
            header = struct.unpack(">IIBBBBB", payload)
        elif tag == b"IDAT":
            compressed += payload
        elif tag == b"IEND":
            break
    if header is None:
        raise ValueError("PNG has no IHDR chunk")
    width, height, depth, colour, compression, filtering, interlace = header
    if depth != 8:
        raise ValueError(f"only 8-bit PNGs are supported, got {depth}-bit")
    if compression or filtering or interlace:
        raise ValueError("only non-interlaced deflate PNGs are supported")
    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(colour)
    if channels is None:
        raise ValueError(f"unsupported PNG colour type {colour}")
    if not width or not height:
        raise ValueError("PNG has no pixels")
    try:
        raw = zlib.decompress(bytes(compressed))
    except zlib.error as exc:
        raise ValueError(f"PNG pixel data will not inflate: {exc}") from None
    stride = width * channels
    if len(raw) != (stride + 1) * height:
        raise ValueError("PNG pixel data has the wrong length")
    out = bytearray(width * height * 3)
    previous = bytes(stride)
    position = 0
    for row in range(height):
        filter_type = raw[position]
        line = bytearray(raw[position + 1:position + 1 + stride])
        position += stride + 1
        _unfilter_row(line, previous, filter_type, channels)
        base = row * width * 3
        if channels == 3:
            out[base:base + stride] = line
        elif channels == 4:
            for x in range(width):
                out[base + x * 3:base + x * 3 + 3] = line[x * 4:x * 4 + 3]
        else:  # 1 or 2 channels: greyscale, alpha discarded
            for x in range(width):
                grey = line[x * channels]
                target = base + x * 3
                out[target] = out[target + 1] = out[target + 2] = grey
        previous = bytes(line)
    return width, height, bytes(out)


def _draw_label(out: bytearray, width: int, height: int, value: str,
                x: int, y: int, scale: int = 2) -> None:
    """Stamp a short digit label into a mutable RGB buffer, in place."""

    def pixel(px: int, py: int, colour: tuple[int, int, int],
              alpha: int = 255) -> None:
        if not (0 <= px < width and 0 <= py < height):
            return
        offset = (py * width + px) * 3
        inverse = 255 - alpha
        for channel, target in enumerate(colour):
            out[offset + channel] = (
                out[offset + channel] * inverse + target * alpha
            ) // 255

    cursor = x
    for character in value:
        glyph = _GLYPHS.get(character)
        if glyph is None:
            cursor += 4 * scale
            continue
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


def stitch_horizontal(
    frames: list[tuple[int, int, bytes, str]], gap: int = 4,
) -> tuple[int, int, bytes]:
    """Combine same-session captures side by side into one wider canvas.

    Built for watching something slow -- a progress bar, an installer copy
    step, a boot animation -- as one image instead of a manual sleep-then-
    screenshot loop. A VM's resolution can change between captures (a boot
    menu switching to the desktop, for instance), so frames are never scaled
    or cropped to match: each is placed top-left on a shared black canvas
    sized to the widest sum and the tallest single frame, separated by `gap`
    pixels, with its label (typically elapsed seconds) stamped in its
    top-left corner.
    """
    if not frames:
        raise ValueError("stitch_horizontal needs at least one frame")
    max_height = max(height for _, height, _, _ in frames)
    total_width = (
        sum(width for width, _, _, _ in frames) + gap * (len(frames) - 1)
    )
    out = bytearray(total_width * max_height * 3)
    x_offset = 0
    for width, height, rgb, label in frames:
        expected = width * height * 3
        if len(rgb) != expected:
            raise ValueError(f"expected {expected} RGB bytes, got {len(rgb)}")
        stride = width * 3
        for row in range(height):
            src = row * stride
            dst = (row * total_width + x_offset) * 3
            out[dst:dst + stride] = rgb[src:src + stride]
        if label:
            _draw_label(out, total_width, max_height, label, x_offset + 4, 4)
        x_offset += width + gap
    return total_width, max_height, bytes(out)


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
