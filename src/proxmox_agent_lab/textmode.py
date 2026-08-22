"""Terminal-text cleanup and a cheap 'is this a character-cell screen?' check.

This module used to carry a glyph-matching OCR decoder for VGA text screens.
It was removed: matching pixels against a font table only works when the guest
uses a font the controller happens to have, and a guest is free to ship its
own -- which is exactly the failure that made the decoder useless in practice.
Reading a screen is now a vision job. `console screenshot --for-model` hands
the pixels back to the caller, `console inspect` sends them to a vision
provider, and `console text` returns the guest's real character stream when
the guest is genuinely a terminal.

What is left here is the part that was never OCR: stripping escape sequences
out of a terminal transcript, and a colour/grid heuristic that says whether a
framebuffer looks like a character-cell screen at all.
"""

from __future__ import annotations

import re
from typing import Any

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
# Character-cell sizes a PC framebuffer is plausibly divided into. Only used
# now to answer "does a text grid fit this resolution?", never to decode one.
CELL_CANDIDATES = ((8, 16), (9, 16), (8, 8), (8, 14), (16, 16))
TEXT_MODE_MAX_COLOURS = 24


def strip_ansi(text: str) -> str:
    """Remove escape sequences so terminal output is readable as plain text."""
    cleaned = ANSI.sub("", text)
    return cleaned.replace("\r\n", "\n").replace("\r", "\n")


# --- screen analysis -----------------------------------------------------


def _distinct_colours(rgb: bytes, limit: int = 64) -> int:
    seen: set[bytes] = set()
    # Sample roughly 100k pixels. The old 4k-pixel stride resonated with wide
    # framebuffers (for 1280px it repeatedly hit the same five columns), so a
    # mostly flat desktop with colourful icons along the top looked like a
    # one-colour text console. An odd pixel stride walks across rows and edges.
    pixels = len(rgb) // 3
    pixel_step = max(1, pixels // 100_000)
    if pixel_step % 2 == 0:
        pixel_step += 1
    step = pixel_step * 3
    for offset in range(0, len(rgb) - 2, step):
        seen.add(rgb[offset : offset + 3])
        if len(seen) > limit:
            return len(seen)
    return len(seen)


def analyse(rgb: bytes, width: int, height: int) -> dict[str, Any]:
    """Cheap check for 'is this a character-cell screen?'."""
    colours = _distinct_colours(rgb)
    grid_fits = any(
        width % cell_width == 0 and height % cell_height == 0
        for cell_width, cell_height in CELL_CANDIDATES
    )
    return {
        "distinct_colours": colours,
        "grid_fits": grid_fits,
        "looks_like_text_console": bool(
            grid_fits and colours <= TEXT_MODE_MAX_COLOURS
        ),
    }
