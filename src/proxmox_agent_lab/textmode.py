"""Opt-in text-mode screen decoding.

Policy: never OCR automatically. A multimodal model reads the PNG better than
any decoder here, and where a guest is genuinely a terminal, `console text`
returns the exact character stream from Proxmox instead of guessing at pixels.

This module exists for the one case those two miss: a VGA text-mode screen
reachable only over VNC -- a boot menu, a BIOS setup screen, the Windows setup
text phase, a kernel panic. Such screens are a strict grid of fixed-size glyphs
in a tiny palette, so decoding is exact glyph lookup, not fuzzy recognition,
using a font table (an embedded public-domain VGA font is installed on first
use if none has been imported).
"""

from __future__ import annotations

import base64
import gzip
import json
from pathlib import Path
import re
import struct
from typing import Any

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
CELL_CANDIDATES = ((8, 16), (9, 16), (8, 8), (8, 14), (16, 16))
TEXT_MODE_MAX_COLOURS = 24


def font_table_path() -> Path:
    return Path(__file__).resolve().parents[1] / "assets" / "console-font.json"


# Embedded default console font, so `console screenshot --ocr` works out of
# the box on legacy guests (Windows 2000 setup, DOS, BIOS screens) that have
# no Linux install to pull a PSF from via `import-font --from-vmid`.
#
# Source: SeaBIOS vgasrc/vgafonts.c, `vgafont16` array (the classic IBM VGA
# ROM 8x16 font in CP437 order, exactly what QEMU/Proxmox guests render):
#   https://github.com/coreboot/seabios/blob/master/vgasrc/vgafonts.c
# License: the file header states the fonts are public domain (originally
# from Joseph Gil's fntcol16.zip, ftp://ftp.simtel.net/pub/simtelnet/
# msdos/screen/fntcol16.zip); MIT-compatible.
#
# Stored as a base64-encoded PSF1 payload (magic 0x36 0x04, mode 0 = 256
# glyphs without a unicode table, charsize 16, then 256 * 16 glyph bytes)
# so the existing parse_psf / build_font_table path is reused unchanged.
DEFAULT_FONT_B64 = (
    "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"
    "GDBgAHgMfMzMzHYAAAAAAAwYMAA4GBgYGBg8AAAAAAAYMGAAfMbGxsbGfAAAAAAAGDBgAMzMzMzM"
    "zHYAAAAAAAB23ADcZmZmZmZmAAAAAHbcAMbm9v7ezsbGxgAAAAAAPGxsPgB+AAAAAAAAAAAAADhs"
    "bDgAfAAAAAAAAAAAAAAAMDAAMDBgwMbGfAAAAAAAAAAAAAD+wMDAwAAAAAAAAAAAAAAA/gYGBgYA"
    "AAAAAADAwMLGzBgwYM6bBgwfAAAAwMDCxswYMGbOlj4GBgAAAAAYGAAYGBg8PDwYAAAAAAAAAAAA"
    "NmzYbDYAAAAAAAAAAAAAANhsNmzYAAAAAAAAEUQRRBFEEUQRRBFEEUQRRFWqVapVqlWqVapVqlWq"
    "Vardd9133Xfdd9133Xfdd913GBgYGBgYGBgYGBgYGBgYGBgYGBgYGBj4GBgYGBgYGBgYGBgYGPgY"
    "+BgYGBgYGBgYNjY2NjY2NvY2NjY2NjY2NgAAAAAAAAD+NjY2NjY2NjYAAAAAAPgY+BgYGBgYGBgY"
    "NjY2Njb2BvY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjYAAAAAAP4G9jY2NjY2NjY2NjY2Njb2Bv4A"
    "AAAAAAAAADY2NjY2Njb+AAAAAAAAAAAYGBgYGPgY+AAAAAAAAAAAAAAAAAAAAPgYGBgYGBgYGBgY"
    "GBgYGBgfAAAAAAAAAAAYGBgYGBgY/wAAAAAAAAAAAAAAAAAAAP8YGBgYGBgYGBgYGBgYGBgfGBgY"
    "GBgYGBgAAAAAAAAA/wAAAAAAAAAAGBgYGBgYGP8YGBgYGBgYGBgYGBgYHxgfGBgYGBgYGBg2NjY2"
    "NjY2NzY2NjY2NjY2NjY2NjY3MD8AAAAAAAAAAAAAAAAAPzA3NjY2NjY2NjY2NjY2NvcA/wAAAAAA"
    "AAAAAAAAAAD/APc2NjY2NjY2NjY2NjY2NzA3NjY2NjY2NjYAAAAAAP8A/wAAAAAAAAAANjY2Njb3"
    "APc2NjY2NjY2NhgYGBgY/wD/AAAAAAAAAAA2NjY2NjY2/wAAAAAAAAAAAAAAAAD/AP8YGBgYGBgY"
    "GAAAAAAAAAD/NjY2NjY2NjY2NjY2NjY2PwAAAAAAAAAAGBgYGBgfGB8AAAAAAAAAAAAAAAAAHxgf"
    "GBgYGBgYGBgAAAAAAAAAPzY2NjY2NjY2NjY2NjY2Nv82NjY2NjY2NhgYGBgY/xj/GBgYGBgYGBgY"
    "GBgYGBgY+AAAAAAAAAAAAAAAAAAAAB8YGBgYGBgYGP////////////////////8AAAAAAAAA////"
    "////////8PDw8PDw8PDw8PDw8PDw8A8PDw8PDw8PDw8PDw8PDw//////////AAAAAAAAAAAAAAAA"
    "AAB23NjY2Nx2AAAAAAAAeMzMzNjMxsbGzAAAAAAAAP7GxsDAwMDAwMAAAAAAAAAAAP5sbGxsbGxs"
    "AAAAAAAAAP7GYDAYMGDG/gAAAAAAAAAAAH7Y2NjY2HAAAAAAAAAAAGZmZmZmfGBgwAAAAAAAAAB2"
    "3BgYGBgYGAAAAAAAAAB+GDxmZmY8GH4AAAAAAAAAOGzGxv7Gxmw4AAAAAAAAOGzGxsZsbGxs7gAA"
    "AAAAAB4wGAw+ZmZmZjwAAAAAAAAAAAB+29vbfgAAAAAAAAAAAAMGftvb835gwAAAAAAAABwwYGB8"
    "YGBgMBwAAAAAAAAAfMbGxsbGxsbGAAAAAAAAAAD+AAD+AAD+AAAAAAAAAAAAGBh+GBgAAP8AAAAA"
    "AAAAMBgMBgwYMAB+AAAAAAAAAAwYMGAwGAwAfgAAAAAAAA4bGxgYGBgYGBgYGBgYGBgYGBgYGBjY"
    "2NhwAAAAAAAAAAAYGAB+ABgYAAAAAAAAAAAAAHbcAHbcAAAAAAAAADhsbDgAAAAAAAAAAAAAAAAA"
    "AAAAAAAYGAAAAAAAAAAAAAAAAAAAABgAAAAAAAAAAA8MDAwMDOxsbDwcAAAAAADYbGxsbGwAAAAA"
    "AAAAAAAAcNgwYMj4AAAAAAAAAAAAAAAAAHx8fHx8fHwAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
)


def strip_ansi(text: str) -> str:
    """Remove escape sequences so terminal output is readable as plain text."""
    cleaned = ANSI.sub("", text)
    return cleaned.replace("\r\n", "\n").replace("\r", "\n")


# --- PSF font import -----------------------------------------------------


def parse_psf(data: bytes) -> dict[str, Any]:
    """Parse a PSF1 or PSF2 console font into glyph bitmaps and characters."""
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    if data[:2] == b"\x36\x04":
        mode, charsize = data[2], data[3]
        count = 512 if mode & 0x01 else 256
        width, height = 8, charsize
        offset = 4
        stride = charsize
        has_unicode = bool(mode & 0x02)
    elif data[:4] == b"\x72\xb5\x4a\x86":
        (_, headersize, flags, count, charsize, height, width) = struct.unpack(
            "<7I", data[4:32]
        )
        offset = headersize
        stride = charsize
        has_unicode = bool(flags & 0x01)
    else:
        raise ValueError("not a PSF1 or PSF2 console font")
    glyphs = [
        data[offset + index * stride : offset + (index + 1) * stride]
        for index in range(count)
    ]
    characters: list[str] = []
    if has_unicode:
        table = data[offset + count * stride :]
        if data[:2] == b"\x36\x04":
            entries = table.split(b"\xff\xff")
            for entry in entries[:count]:
                codes = struct.unpack(f"<{len(entry) // 2}H", entry[: len(entry) // 2 * 2])
                characters.append(chr(codes[0]) if codes else "")
        else:
            for entry in table.split(b"\xff")[:count]:
                text = entry.split(b"\xfe")[0].decode("utf-8", "ignore")
                characters.append(text[:1])
    while len(characters) < count:
        # No unicode table: glyph index is its code-page position, which
        # matches ASCII over the printable range we care about.
        index = len(characters)
        characters.append(chr(index) if 32 <= index < 127 else "")
    return {
        "width": width,
        "height": height,
        "glyphs": glyphs,
        "characters": characters,
    }


def build_font_table(font: dict[str, Any]) -> dict[str, Any]:
    mapping: dict[str, str] = {}
    for glyph, character in zip(font["glyphs"], font["characters"]):
        if not character or character == "\x00":
            continue
        key = glyph.hex()
        # First writer wins: the low code page holds the canonical ASCII forms.
        mapping.setdefault(key, character)
    return {
        "width": font["width"],
        "height": font["height"],
        "glyph_count": len(mapping),
        "glyphs": mapping,
    }


def load_font_table() -> dict[str, Any] | None:
    path = font_table_path()
    if not path.exists():
        return None
    return json.loads(path.read_text())


def builtin_font_table() -> dict[str, Any]:
    """Build the embedded public-domain VGA font table without installing it."""
    return build_font_table(parse_psf(base64.b64decode(DEFAULT_FONT_B64)))


def _write_font_table(table: dict[str, Any]) -> Path:
    """Persist a font table to the standard location."""
    path = font_table_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(table, indent=2, sort_keys=True) + "\n")
    return path


def install_builtin_font() -> dict[str, Any]:
    """Build and save the embedded default font, like ``cmd_import_font``.

    Lets ``console screenshot --ocr`` work on legacy guests with no Linux
    install to import a PSF from.
    """
    table = builtin_font_table()
    _write_font_table(table)
    return table


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


def screen_background(rgb: bytes, sample_step: int = 3) -> bytes:
    """The most common colour on the screen, i.e. the console background."""
    counts: dict[bytes, int] = {}
    step = max(3, sample_step - sample_step % 3)
    for offset in range(0, len(rgb) - 2, step):
        pixel = rgb[offset : offset + 3]
        counts[pixel] = counts.get(pixel, 0) + 1
    return max(counts, key=lambda key: counts[key]) if counts else b"\x00\x00\x00"


def _cell_bitmap(
    rgb: bytes,
    width: int,
    x: int,
    y: int,
    cell_width: int,
    cell_height: int,
    glyph_width: int,
    background_hint: bytes,
) -> tuple[bytes, bool]:
    """Binarise one character cell against its background colour."""
    counts: dict[bytes, int] = {}
    pixels: list[list[bytes]] = []
    for row in range(cell_height):
        line: list[bytes] = []
        base = ((y + row) * width + x) * 3
        for column in range(cell_width):
            pixel = rgb[base + column * 3 : base + column * 3 + 3]
            line.append(pixel)
            counts[pixel] = counts.get(pixel, 0) + 1
        pixels.append(line)
    if len(counts) == 1:
        # A uniform cell is either empty space or a solid block glyph; only
        # the screen background distinguishes the two.
        only = next(iter(counts))
        if only == background_hint:
            return bytes(cell_height), True
        solid = (0xFF << (8 - min(glyph_width, 8))) & 0xFF
        return bytes([solid] * cell_height), False
    background = max(counts, key=lambda key: counts[key])
    blank = False
    out = bytearray(cell_height)
    for row in range(cell_height):
        value = 0
        for column in range(min(glyph_width, cell_width)):
            if pixels[row][column] != background:
                value |= 1 << (7 - column)
        out[row] = value
    return bytes(out), blank


def decode_screen(rgb: bytes, width: int, height: int) -> dict[str, Any]:
    """Decode a text-mode screen into lines using a font table.

    A missing font table is not an error: the embedded public-domain VGA
    font is installed automatically, so ``--ocr`` works out of the box on
    legacy guests with no Linux install to import a PSF from. The result
    records which font was used under ``ocr_font``.
    """
    table = load_font_table()
    font_source = "imported"
    if not table:
        try:
            table = install_builtin_font()
        except OSError as exc:
            return {
                "error": (
                    f"no console font table installed and the builtin VGA "
                    f"font could not be installed ({exc}); run 'proxmox-lab "
                    "console import-font --file <psf>' or prefer 'proxmox-lab "
                    "console text' for exact terminal output"
                )
            }
        font_source = "builtin"
    glyphs: dict[str, str] = table["glyphs"]
    glyph_width = int(table["width"])
    glyph_height = int(table["height"])
    best: dict[str, Any] | None = None
    # One scan of the screen, not one per candidate cell size.
    background = screen_background(rgb)
    for cell_width, cell_height in CELL_CANDIDATES:
        if cell_height != glyph_height:
            continue
        if width % cell_width or height % cell_height:
            continue
        columns = width // cell_width
        rows = height // cell_height
        background = screen_background(rgb)
        lines: list[str] = []
        matched = 0
        unknown = 0
        for row in range(rows):
            line: list[str] = []
            for column in range(columns):
                bitmap, blank = _cell_bitmap(
                    rgb,
                    width,
                    column * cell_width,
                    row * cell_height,
                    cell_width,
                    cell_height,
                    glyph_width,
                    background,
                )
                if blank:
                    line.append(" ")
                    continue
                character = glyphs.get(bitmap.hex())
                if character is None:
                    unknown += 1
                    line.append("�")
                else:
                    matched += 1
                    line.append(character)
            lines.append("".join(line).rstrip())
        total = matched + unknown
        confidence = matched / total if total else 0.0
        candidate = {
            "cell": [cell_width, cell_height],
            "columns": columns,
            "rows": rows,
            "matched_cells": matched,
            "unknown_cells": unknown,
            "confidence": round(confidence, 3),
            "text": "\n".join(lines).rstrip("\n"),
        }
        if best is None or candidate["confidence"] > best["confidence"]:
            best = candidate
    if best is None:
        return {
            "error": f"no {glyph_height}px cell grid fits {width}x{height}",
            "ocr_font": font_source,
        }
    if best["confidence"] < 0.5:
        best["warning"] = (
            "low glyph match rate; the guest is probably not using the installed "
            "font, or the screen is not text mode"
        )
    best["ocr_font"] = font_source
    return best


# --- commands ------------------------------------------------------------


def cmd_import_font(lab: Any, args: Any) -> None:
    if args.file:
        data = Path(args.file).expanduser().read_bytes()
        source = str(Path(args.file).expanduser())
    elif args.from_vmid:
        from . import console

        api = lab.ProxmoxAPI()
        lab.load_lease(args.lease)
        result = console.agent_exec(
            lab,
            api,
            args.from_vmid,
            ["/bin/sh", "-c", f"base64 < {args.guest_path}"],
            timeout=120,
        )
        if result["exitcode"] not in (0, None):
            raise lab.LabError(f"could not read {args.guest_path}: {result['stderr'][:300]}")
        import base64 as _b64

        data = _b64.b64decode(result["stdout"])
        source = f"vmid {args.from_vmid}:{args.guest_path}"
    else:
        raise lab.LabError("provide --file or --from-vmid with --guest-path")
    table = build_font_table(parse_psf(data))
    path = _write_font_table(table)
    lab.audit(
        "console-font-imported",
        source=source,
        width=table["width"],
        height=table["height"],
        glyph_count=table["glyph_count"],
        sync=False,
    )
    print(json.dumps(
        {
            "path": str(path),
            "width": table["width"],
            "height": table["height"],
            "glyph_count": table["glyph_count"],
        },
        indent=2,
    ))


def register(console_sub: Any, lab: Any) -> None:
    font = console_sub.add_parser(
        "import-font", help="install a PSF console font for optional OCR"
    )
    font.add_argument("--file", help="local .psf or .psf.gz")
    font.add_argument("--from-vmid", type=int, help="read the font out of a guest")
    font.add_argument("--guest-path",
                      default="/usr/share/consolefonts/Lat15-VGA16.psf.gz")
    font.add_argument("--lease")
    font.set_defaults(func=lambda args: cmd_import_font(lab, args))
