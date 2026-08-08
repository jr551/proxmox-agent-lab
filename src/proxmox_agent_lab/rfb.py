"""Stdlib-only RFB (VNC) client for Proxmox QEMU consoles.

Only what a headless agent needs: connect, read a framebuffer, send keyboard
and pointer events. Encodings are limited to Raw, CopyRect and Zlib, which
QEMU always offers and which zlib in the standard library can decode.
"""

from __future__ import annotations

import struct
import time
import zlib

from . import des

ENC_RAW = 0
ENC_COPYRECT = 1
ENC_ZLIB = 6
ENC_DESKTOP_SIZE = -223
ENC_LAST_RECT = -224

MSG_FRAMEBUFFER_UPDATE = 0
MSG_SET_COLOUR_MAP = 1
MSG_BELL = 2
MSG_SERVER_CUT_TEXT = 3


class RFBError(RuntimeError):
    pass


# X11 keysyms for the keys an installer or boot menu actually needs.
KEYSYMS = {
    "backspace": 0xFF08,
    "tab": 0xFF09,
    "enter": 0xFF0D,
    "return": 0xFF0D,
    "esc": 0xFF1B,
    "escape": 0xFF1B,
    "insert": 0xFF63,
    "delete": 0xFFFF,
    "home": 0xFF50,
    "end": 0xFF57,
    "pageup": 0xFF55,
    "pagedown": 0xFF56,
    "left": 0xFF51,
    "up": 0xFF52,
    "right": 0xFF53,
    "down": 0xFF54,
    "space": 0x0020,
    "shift": 0xFFE1,
    "ctrl": 0xFFE3,
    "control": 0xFFE3,
    "alt": 0xFFE9,
    "meta": 0xFFEB,
    "super": 0xFFEB,
    "win": 0xFFEB,
    "capslock": 0xFFE5,
    "printscreen": 0xFF61,
    "pause": 0xFF13,
    "menu": 0xFF67,
}
for _index in range(1, 25):
    KEYSYMS[f"f{_index}"] = 0xFFBD + _index

_SHIFTED = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7",
    "*": "8", "(": "9", ")": "0", "_": "-", "+": "=", "{": "[", "}": "]",
    "|": "\\", ":": ";", '"': "'", "<": ",", ">": ".", "?": "/", "~": "`",
}


def char_keysym(char: str) -> tuple[int, bool]:
    """Return (keysym, needs_shift) for a printable character."""
    if char == "\n":
        return 0xFF0D, False
    if char == "\t":
        return 0xFF09, False
    if char.isupper():
        return ord(char), True
    if char in _SHIFTED:
        return ord(char), True
    return ord(char), False


def parse_key_combo(combo: str) -> tuple[list[int], int]:
    """Split "ctrl-alt-delete" into modifier keysyms and the final keysym."""
    parts = [part for part in combo.strip().lower().split("-") if part]
    if not parts:
        raise RFBError("empty key combination")
    if len(parts) > 1 and parts[-1] == "":
        raise RFBError(f"invalid key combination: {combo}")
    modifiers: list[int] = []
    for part in parts[:-1]:
        if part not in KEYSYMS:
            raise RFBError(f"unknown modifier: {part}")
        modifiers.append(KEYSYMS[part])
    final = parts[-1]
    if final in KEYSYMS:
        return modifiers, KEYSYMS[final]
    if len(final) == 1:
        keysym, shifted = char_keysym(final)
        if shifted and KEYSYMS["shift"] not in modifiers:
            modifiers.append(KEYSYMS["shift"])
        return modifiers, keysym
    raise RFBError(f"unknown key: {final}")


class RFBClient:
    """RFB 3.8 client speaking over an already-connected transport."""

    def __init__(self, transport: object, password: str) -> None:
        self._transport = transport
        self._password = password
        self._inflate = zlib.decompressobj()
        self.width = 0
        self.height = 0
        self.name = ""
        self.framebuffer = bytearray()
        self._handshake()

    # -- transport helpers -------------------------------------------------

    def _read(self, count: int) -> bytes:
        return self._transport.read_exact(count)  # type: ignore[attr-defined]

    def _write(self, data: bytes) -> None:
        self._transport.send(data)  # type: ignore[attr-defined]

    # -- handshake ---------------------------------------------------------

    def _handshake(self) -> None:
        version = self._read(12)
        if not version.startswith(b"RFB "):
            raise RFBError(f"not an RFB stream: {version!r}")
        self._write(b"RFB 003.008\n")
        count = self._read(1)[0]
        if count == 0:
            reason_length = struct.unpack(">I", self._read(4))[0]
            raise RFBError(
                "server refused the connection: "
                + self._read(reason_length).decode("latin-1")
            )
        types = set(self._read(count))
        if 2 in types:
            # RFB 3.8: the client must announce the security type it chose
            # before the server will send the authentication challenge.
            self._write(bytes([2]))
            challenge = self._read(16)
            if not self._password:
                raise RFBError("server requires VNC authentication but no ticket")
            self._write(des.vnc_response(self._password, challenge))
        elif 1 in types:
            self._write(bytes([1]))
        else:
            raise RFBError(f"no supported RFB security type in {sorted(types)}")
        if 2 in types or 1 in types:
            result = struct.unpack(">I", self._read(4))[0]
            if result != 0:
                reason = ""
                try:
                    length = struct.unpack(">I", self._read(4))[0]
                    reason = self._read(length).decode("latin-1")
                except (RFBError, OSError, struct.error):
                    reason = "no reason given"
                raise RFBError(f"RFB authentication failed: {reason}")
        self._write(bytes([1]))  # ClientInit, shared
        header = self._read(24)
        self.width, self.height = struct.unpack(">HH", header[:4])
        name_length = struct.unpack(">I", header[20:24])[0]
        self.name = self._read(name_length).decode("latin-1", "replace")
        self._reset_framebuffer(self.width, self.height)
        self._set_pixel_format()
        self._set_encodings()

    def _reset_framebuffer(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.framebuffer = bytearray(width * height * 3)

    def _set_pixel_format(self) -> None:
        # 32bpp little-endian truecolour, red<<16 green<<8 blue<<0 (BGRX bytes)
        self._write(
            struct.pack(
                ">BBBBBBBBHHHBBBBBB",
                0, 0, 0, 0,
                32, 24, 0, 1,
                255, 255, 255,
                16, 8, 0,
                0, 0, 0,
            )
        )

    def _set_encodings(self) -> None:
        encodings = (
            ENC_ZLIB,
            ENC_COPYRECT,
            ENC_RAW,
            ENC_DESKTOP_SIZE,
            ENC_LAST_RECT,
        )
        payload = struct.pack(">BBH", 2, 0, len(encodings))
        payload += b"".join(struct.pack(">i", value) for value in encodings)
        self._write(payload)

    # -- framebuffer -------------------------------------------------------

    def request_update(self, incremental: bool = False) -> None:
        self._write(
            struct.pack(
                ">BBHHHH",
                3,
                1 if incremental else 0,
                0,
                0,
                self.width,
                self.height,
            )
        )

    def _blit(self, x: int, y: int, width: int, height: int, pixels: bytes) -> None:
        expected = width * height * 4
        if len(pixels) != expected:
            raise RFBError(f"rectangle expected {expected} bytes, got {len(pixels)}")
        stride = self.width * 3
        for row in range(height):
            source = row * width * 4
            target = (y + row) * stride + x * 3
            line = bytearray(width * 3)
            for column in range(width):
                offset = source + column * 4
                line[column * 3] = pixels[offset + 2]
                line[column * 3 + 1] = pixels[offset + 1]
                line[column * 3 + 2] = pixels[offset]
            self.framebuffer[target : target + width * 3] = line

    def _copy_rect(self, x: int, y: int, width: int, height: int) -> None:
        source_x, source_y = struct.unpack(">HH", self._read(4))
        stride = self.width * 3
        rows = range(height) if source_y >= y else reversed(range(height))
        for row in rows:
            source = (source_y + row) * stride + source_x * 3
            target = (y + row) * stride + x * 3
            self.framebuffer[target : target + width * 3] = self.framebuffer[
                source : source + width * 3
            ]

    def _read_rectangle(self) -> bool:
        """Read one rectangle; return False when the update is finished."""
        x, y, width, height, encoding = struct.unpack(">HHHHi", self._read(12))
        if encoding == ENC_LAST_RECT:
            return False
        if encoding == ENC_DESKTOP_SIZE:
            self._reset_framebuffer(width, height)
            return True
        if encoding == ENC_COPYRECT:
            self._copy_rect(x, y, width, height)
            return True
        if encoding == ENC_RAW:
            self._blit(x, y, width, height, self._read(width * height * 4))
            return True
        if encoding == ENC_ZLIB:
            length = struct.unpack(">I", self._read(4))[0]
            data = self._inflate.decompress(self._read(length))
            self._blit(x, y, width, height, data)
            return True
        raise RFBError(f"unsupported RFB encoding {encoding}")

    def pump(self, deadline: float) -> bool:
        """Process one server message; return True if a full frame arrived."""
        if time.monotonic() > deadline:
            raise RFBError("timed out waiting for a framebuffer update")
        message = self._read(1)[0]
        if message == MSG_FRAMEBUFFER_UPDATE:
            count = struct.unpack(">xH", self._read(3))[0]
            if count == 0xFFFF:  # streaming update terminated by LastRect
                while self._read_rectangle():
                    pass
            else:
                for _ in range(count):
                    if not self._read_rectangle():
                        break
            return True
        if message == MSG_SET_COLOUR_MAP:
            count = struct.unpack(">xHH", self._read(5))[1]
            self._read(count * 6)
            return False
        if message == MSG_BELL:
            return False
        if message == MSG_SERVER_CUT_TEXT:
            length = struct.unpack(">3xI", self._read(7))[0]
            self._read(length)
            return False
        raise RFBError(f"unexpected RFB server message type {message}")

    def capture(self, timeout: float = 20.0, settle: float = 0.0) -> bytes:
        """Return the current screen as packed RGB bytes."""
        if settle > 0:
            time.sleep(settle)
        deadline = time.monotonic() + timeout
        self.request_update(incremental=False)
        while not self.pump(deadline):
            pass
        return bytes(self.framebuffer)

    # -- input -------------------------------------------------------------

    def key(self, keysym: int, down: bool) -> None:
        self._write(struct.pack(">BBHI", 4, 1 if down else 0, 0, keysym))

    def tap(self, keysym: int, modifiers: list[int] | None = None) -> None:
        modifiers = modifiers or []
        for modifier in modifiers:
            self.key(modifier, True)
        self.key(keysym, True)
        self.key(keysym, False)
        for modifier in reversed(modifiers):
            self.key(modifier, False)

    def type_text(self, text: str, delay: float = 0.012) -> int:
        sent = 0
        for char in text:
            keysym, shifted = char_keysym(char)
            self.tap(keysym, [KEYSYMS["shift"]] if shifted else [])
            sent += 1
            if delay:
                time.sleep(delay)
        return sent

    def pointer(self, x: int, y: int, button_mask: int = 0) -> None:
        self._write(struct.pack(">BBHH", 5, button_mask, x, y))

    def click(self, x: int, y: int, button: int = 1, double: bool = False) -> None:
        mask = 1 << (button - 1)
        self.pointer(x, y, 0)
        for _ in range(2 if double else 1):
            self.pointer(x, y, mask)
            time.sleep(0.05)
            self.pointer(x, y, 0)
            time.sleep(0.05)
