"""Offline tests for the console, image, transfer and Windows helpers.

Nothing here touches PC2 or the network: the RFB client is driven against a
scripted in-memory server, and S3 signing is checked against a fixed clock.
"""

from __future__ import annotations

import os
from pathlib import Path

# Point every module at a fixture config *before* importing the package:
# site values are read at import time.
os.environ["PROXMOX_AGENT_LAB_CONFIG"] = str(
    Path(__file__).parent / "fixtures" / "config.toml"
)

import shutil
import tempfile
# ...and at a disposable state directory: a test must never write into the
# developer's real controller state. Cleared here so a previous run cannot
# leak into this one; imports all happen before any test runs.
_TEST_STATE = Path(tempfile.gettempdir()) / "proxmox-agent-lab-test-state"
shutil.rmtree(_TEST_STATE, ignore_errors=True)
_TEST_STATE.mkdir(parents=True, exist_ok=True)
os.environ["PROXMOX_AGENT_LAB_STATE"] = str(_TEST_STATE)

import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock
import zlib

SRC = Path(__file__).parents[1] / "src"
sys.path.insert(0, str(SRC))
SCRIPTS = SRC / "proxmox_agent_lab"

from proxmox_agent_lab import des as lab_des  # noqa: E402
from proxmox_agent_lab import console as lab_console  # noqa: E402
from proxmox_agent_lab import netgw as lab_netgw  # noqa: E402
from proxmox_agent_lab import png as lab_png  # noqa: E402
from proxmox_agent_lab import rfb as lab_rfb  # noqa: E402
from proxmox_agent_lab import s3 as lab_s3  # noqa: E402
from proxmox_agent_lab import storage as lab_storage  # noqa: E402
from proxmox_agent_lab import textmode as lab_textmode  # noqa: E402
from proxmox_agent_lab import windows as lab_windows  # noqa: E402


class FakeTransport:
    """Scripted RFB server side, plus a record of what the client sent.

    A passive fake cannot catch a missing client message, so `writes` records
    each send in order and `test_handshake_message_order` asserts the exact
    sequence the protocol requires.
    """

    def __init__(self, script: bytes) -> None:
        self.inbound = io.BytesIO(script)
        self.sent = bytearray()
        self.writes: list[bytes] = []

    def read_exact(self, count: int) -> bytes:
        data = self.inbound.read(count)
        if len(data) != count:
            raise AssertionError(f"script exhausted: wanted {count}, got {len(data)}")
        return data

    def send(self, data: bytes) -> None:
        self.sent += data
        self.writes.append(data)


def build_server_script(width: int, height: int, pixels: bytes,
                        encoding: int = lab_rfb.ENC_RAW) -> bytes:
    out = bytearray()
    out += b"RFB 003.008\n"
    out += bytes([1, 2])                      # one security type: VNC auth
    out += bytes(range(16))                   # challenge
    out += struct.pack(">I", 0)               # auth OK
    name = b"lab"
    out += struct.pack(">HH", width, height)
    out += bytes(16)                          # server pixel format (ignored)
    out += struct.pack(">I", len(name)) + name
    payload = pixels if encoding == lab_rfb.ENC_RAW else (
        struct.pack(">I", len(zlib.compress(pixels))) + zlib.compress(pixels)
    )
    out += struct.pack(">BxH", 0, 1)          # FramebufferUpdate, 1 rectangle
    out += struct.pack(">HHHHi", 0, 0, width, height, encoding)
    out += payload
    return bytes(out)


def bgrx(rgb_pixels: list[tuple[int, int, int]]) -> bytes:
    return b"".join(bytes([b, g, r, 0]) for r, g, b in rgb_pixels)


class DesTests(unittest.TestCase):
    def test_known_answer_vectors(self) -> None:
        self.assertEqual(
            lab_des.encrypt_block(
                bytes.fromhex("0123456789ABCDEF"), bytes.fromhex("4E6F772069732074")
            ).hex().upper(),
            "3FA40E8A984D4815",
        )
        self.assertEqual(
            lab_des.encrypt_block(bytes(8), bytes(8)).hex().upper(),
            "8CA64DE9C1B123A7",
        )

    def test_vnc_response_length_and_determinism(self) -> None:
        challenge = bytes(range(16))
        first = lab_des.vnc_response("ticket12", challenge)
        self.assertEqual(len(first), 16)
        self.assertEqual(first, lab_des.vnc_response("ticket12", challenge))
        self.assertNotEqual(first, lab_des.vnc_response("ticket13", challenge))

    def test_password_is_truncated_to_eight_bytes(self) -> None:
        challenge = bytes(range(16))
        self.assertEqual(
            lab_des.vnc_response("abcdefgh", challenge),
            lab_des.vnc_response("abcdefghIGNORED", challenge),
        )


class PngTests(unittest.TestCase):
    def test_round_trip_header_and_size(self) -> None:
        png = lab_png.encode_png(2, 1, bytes([255, 0, 0, 0, 255, 0]))
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        width, height = struct.unpack(">II", png[16:24])
        self.assertEqual((width, height), (2, 1))
        self.assertTrue(png.endswith(b"IEND\xae\x42\x60\x82"))

    def test_rejects_wrong_buffer_length(self) -> None:
        with self.assertRaises(ValueError):
            lab_png.encode_png(2, 2, b"\x00" * 3)

    def test_coordinate_grid_preserves_canvas_and_labels_original_axes(self) -> None:
        width, height = 220, 120
        original = bytes((10, 20, 30)) * (width * height)
        gridded = lab_png.overlay_coordinate_grid(width, height, original, 100)
        self.assertEqual(len(gridded), len(original))
        untouched = (50 * width + 50) * 3
        grid_line = (50 * width + 100) * 3
        self.assertEqual(gridded[untouched:untouched + 3], original[untouched:untouched + 3])
        self.assertNotEqual(gridded[grid_line:grid_line + 3],
                            original[grid_line:grid_line + 3])
        encoded = lab_png.encode_png(width, height, gridded)
        self.assertEqual(struct.unpack(">II", encoded[16:24]), (width, height))

    def test_change_highlight_dims_stable_pixels_and_outlines_delta(self) -> None:
        width, height = 3, 1
        previous = bytes((100, 100, 100)) * 3
        current = bytearray(previous)
        current[3:6] = bytes((220, 100, 100))
        highlighted, changed = lab_png.highlight_changes(
            width, height, bytes(current), previous
        )
        self.assertEqual(changed, 1)
        self.assertEqual(highlighted[3:6], current[3:6])
        self.assertEqual(highlighted[0:3], b"\xff\x00\xff")
        self.assertEqual(highlighted[6:9], b"\xff\x00\xff")

    def test_change_highlight_rejects_mismatched_frames(self) -> None:
        with self.assertRaises(ValueError):
            lab_png.highlight_changes(2, 1, b"\x00" * 6, b"\x00" * 3)

    def test_stitch_horizontal_places_frames_left_to_right(self) -> None:
        red = bytes((255, 0, 0)) * (2 * 2)
        blue = bytes((0, 0, 255)) * (3 * 2)
        width, height, stitched = lab_png.stitch_horizontal(
            [(2, 2, red, ""), (3, 2, blue, "")], gap=1
        )
        self.assertEqual((width, height), (2 + 1 + 3, 2))
        # row 0: red pixel, red pixel, gap (black), blue, blue, blue
        self.assertEqual(stitched[0:3], b"\xff\x00\x00")
        self.assertEqual(stitched[6:9], b"\x00\x00\x00")
        self.assertEqual(stitched[9:12], b"\x00\x00\xff")

    def test_stitch_horizontal_handles_frames_of_different_heights(self) -> None:
        short = bytes((1, 2, 3)) * (2 * 1)
        tall = bytes((4, 5, 6)) * (2 * 3)
        width, height, stitched = lab_png.stitch_horizontal(
            [(2, 1, short, ""), (2, 3, tall, "")], gap=0
        )
        self.assertEqual((width, height), (4, 3))
        self.assertEqual(len(stitched), width * height * 3)
        # the short frame's second row (row 1 of the canvas) is unfilled/black
        second_row_short_side = (1 * width + 0) * 3
        self.assertEqual(
            stitched[second_row_short_side:second_row_short_side + 3],
            b"\x00\x00\x00",
        )

    def test_stitch_horizontal_rejects_empty_input(self) -> None:
        with self.assertRaises(ValueError):
            lab_png.stitch_horizontal([])

    def test_stitch_horizontal_rejects_mismatched_frame_buffer(self) -> None:
        with self.assertRaises(ValueError):
            lab_png.stitch_horizontal([(2, 2, b"\x00" * 3, "")])


class RfbTests(unittest.TestCase):
    def test_capture_decodes_raw_rectangle(self) -> None:
        expected = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (10, 20, 30)]
        transport = FakeTransport(build_server_script(2, 2, bgrx(expected)))
        client = lab_rfb.RFBClient(transport, "ticket")
        self.assertEqual((client.width, client.height), (2, 2))
        self.assertEqual(
            client.capture(), b"".join(bytes(pixel) for pixel in expected)
        )

    def test_capture_decodes_zlib_rectangle(self) -> None:
        expected = [(1, 2, 3), (4, 5, 6)]
        transport = FakeTransport(
            build_server_script(2, 1, bgrx(expected), lab_rfb.ENC_ZLIB)
        )
        client = lab_rfb.RFBClient(transport, "ticket")
        self.assertEqual(client.capture(), bytes([1, 2, 3, 4, 5, 6]))

    def test_client_answers_the_auth_challenge(self) -> None:
        transport = FakeTransport(build_server_script(1, 1, bgrx([(0, 0, 0)])))
        lab_rfb.RFBClient(transport, "sekrit")
        self.assertIn(b"RFB 003.008\n", bytes(transport.sent))
        self.assertIn(
            lab_des.vnc_response("sekrit", bytes(range(16))), bytes(transport.sent)
        )

    def test_handshake_message_order(self) -> None:
        """The server stalls unless every handshake message is sent, in order.

        Regression guard: the chosen-security-type byte was originally
        missing, which a passive fake server did not notice but a real one
        did -- it simply never sent the challenge.
        """
        transport = FakeTransport(build_server_script(1, 1, bgrx([(0, 0, 0)])))
        lab_rfb.RFBClient(transport, "sekrit")
        self.assertEqual(transport.writes[0], b"RFB 003.008\n")
        self.assertEqual(transport.writes[1], b"\x02", "chosen security type")
        self.assertEqual(
            transport.writes[2], lab_des.vnc_response("sekrit", bytes(range(16)))
        )
        self.assertEqual(transport.writes[3], b"\x01", "ClientInit, shared")
        self.assertEqual(transport.writes[4][0], 0, "SetPixelFormat")
        self.assertEqual(len(transport.writes[4]), 20)
        self.assertEqual(transport.writes[5][0], 2, "SetEncodings")

    def test_key_combo_parsing(self) -> None:
        modifiers, keysym = lab_rfb.parse_key_combo("ctrl-alt-delete")
        self.assertEqual(
            modifiers, [lab_rfb.KEYSYMS["ctrl"], lab_rfb.KEYSYMS["alt"]]
        )
        self.assertEqual(keysym, lab_rfb.KEYSYMS["delete"])
        self.assertEqual(lab_rfb.parse_key_combo("f2"), ([], lab_rfb.KEYSYMS["f2"]))
        with self.assertRaises(lab_rfb.RFBError):
            lab_rfb.parse_key_combo("ctrl-nosuchkey")

    def test_uppercase_and_symbols_take_shift(self) -> None:
        self.assertEqual(lab_rfb.char_keysym("a"), (ord("a"), False))
        self.assertEqual(lab_rfb.char_keysym("A"), (ord("A"), True))
        self.assertEqual(lab_rfb.char_keysym("!"), (ord("!"), True))

    def test_pointer_and_key_wire_format(self) -> None:
        transport = FakeTransport(build_server_script(4, 4, bgrx([(0, 0, 0)] * 16)))
        client = lab_rfb.RFBClient(transport, "t")
        transport.sent.clear()
        client.pointer(3, 2, 1)
        self.assertEqual(bytes(transport.sent), struct.pack(">BBHH", 5, 1, 3, 2))
        transport.sent.clear()
        client.key(0xFF0D, True)
        self.assertEqual(bytes(transport.sent), struct.pack(">BBHI", 4, 1, 0, 0xFF0D))


class TextModeTests(unittest.TestCase):
    def test_strip_ansi(self) -> None:
        self.assertEqual(
            lab_textmode.strip_ansi("\x1b[32mok\x1b[0m\r\ndone"), "ok\ndone"
        )

    def test_graphical_screen_is_not_reported_as_text(self) -> None:
        rgb = bytes(
            value % 251 for value in range(640 * 480 * 3)
        )  # many distinct colours
        self.assertFalse(lab_textmode.analyse(rgb, 640, 480)["looks_like_text_console"])

    def test_flat_desktop_with_icons_is_not_reported_as_text(self) -> None:
        width, height = 1280, 800
        rgb = bytearray(b"\x35\x70\xa0" * (width * height))
        for index in range(10_000):
            offset = index * 3
            rgb[offset:offset + 3] = bytes(
                (index % 251, (index * 3) % 251, (index * 7) % 251)
            )
        analysis = lab_textmode.analyse(bytes(rgb), width, height)
        self.assertGreater(analysis["distinct_colours"], 24)
        self.assertFalse(analysis["looks_like_text_console"])

    def test_two_colour_grid_is_reported_as_text(self) -> None:
        rgb = b"\x00\x00\x00" * (640 * 400)
        analysis = lab_textmode.analyse(rgb, 640, 400)
        self.assertTrue(analysis["looks_like_text_console"])

    def test_psf2_round_trip_and_screen_decode(self) -> None:
        # A two-glyph 8x16 PSF2 font: 'A' is a solid block, 'B' a single row.
        glyph_a = bytes([0xFF] * 16)
        glyph_b = bytes([0x00] * 8 + [0xFF] + [0x00] * 7)
        header = struct.pack("<I7I", 0x864AB572, 0, 32, 0, 2, 16, 16, 8)
        font = lab_textmode.parse_psf(header + glyph_a + glyph_b)
        self.assertEqual((font["width"], font["height"]), (8, 16))
        table = lab_textmode.build_font_table(
            {**font, "characters": ["A", "B"]}
        )
        self.assertEqual(table["glyphs"][glyph_a.hex()], "A")

        # Paint one 8x16 cell of 'A' (all foreground) beside two blank cells,
        # so the screen background is unambiguously black.
        width, height = 24, 16
        screen = bytearray(b"\x00\x00\x00" * width * height)
        for row in range(16):
            for column in range(8):
                offset = (row * width + column) * 3
                screen[offset : offset + 3] = b"\xff\xff\xff"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "console-font.json"
            path.write_text(json.dumps(table))
            with mock.patch.object(lab_textmode, "font_table_path", return_value=path):
                decoded = lab_textmode.decode_screen(bytes(screen), width, height)
        self.assertEqual(decoded["text"], "A")
        self.assertEqual(decoded["confidence"], 1.0)
        self.assertEqual(decoded["ocr_font"], "imported")

    def test_decode_without_a_table_installs_the_builtin_font(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "absent.json"
            with mock.patch.object(
                lab_textmode, "font_table_path", return_value=missing
            ):
                result = lab_textmode.decode_screen(
                    b"\x00" * (8 * 16 * 3), 8, 16
                )
            table = json.loads(missing.read_text())
        self.assertNotIn("error", result)
        self.assertEqual(result["ocr_font"], "builtin")
        self.assertEqual((table["width"], table["height"]), (8, 16))

    def test_builtin_font_round_trip_decodes_ascii(self) -> None:
        # Paint one 8x16 cell of 'A' from the embedded font and check the
        # exact glyph is recovered when no user table exists yet.
        table = lab_textmode.builtin_font_table()
        glyph = bytes.fromhex(
            next(key for key, value in table["glyphs"].items() if value == "A")
        )
        width, height = 8, 16
        screen = bytearray(b"\x00\x00\x00" * width * height)
        for row in range(height):
            for column in range(width):
                if glyph[row] & (1 << (7 - column)):
                    offset = (row * width + column) * 3
                    screen[offset : offset + 3] = b"\xff\xff\xff"
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "absent.json"
            with mock.patch.object(
                lab_textmode, "font_table_path", return_value=missing
            ):
                decoded = lab_textmode.decode_screen(
                    bytes(screen), width, height
                )
        self.assertEqual(decoded["text"], "A")
        self.assertEqual(decoded["confidence"], 1.0)
        self.assertEqual(decoded["ocr_font"], "builtin")

    def test_decode_reports_when_the_builtin_font_cannot_be_saved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "absent.json"
            with mock.patch.object(
                lab_textmode, "font_table_path", return_value=missing
            ), mock.patch.object(
                lab_textmode, "_write_font_table",
                side_effect=OSError("read-only filesystem"),
            ):
                result = lab_textmode.decode_screen(
                    b"\x00" * (8 * 16 * 3), 8, 16
                )
        self.assertIn("builtin", result["error"])
        self.assertIn("import-font", result["error"])


class S3Tests(unittest.TestCase):
    def test_presign_is_deterministic_for_a_fixed_clock(self) -> None:
        with mock.patch.object(
            lab_s3, "credentials", return_value=("AKID", "SECRET")
        ), mock.patch.object(
            lab_s3, "_now", return_value=("20260807T120000Z", "20260807")
        ):
            first = lab_s3.presign("dir/file.bin", expires=900)
            second = lab_s3.presign("dir/file.bin", expires=900)
        self.assertEqual(first, second)
        self.assertIn("X-Amz-Signature=", first)
        self.assertIn("X-Amz-Expires=900", first)
        self.assertTrue(first.startswith(f"{lab_s3.ENDPOINT}/{lab_s3.BUCKET}/"))

    def test_presign_rejects_absurd_expiry(self) -> None:
        with self.assertRaises(lab_s3.S3Error):
            lab_s3.presign("k", expires=0)
        with self.assertRaises(lab_s3.S3Error):
            lab_s3.presign("k", expires=99_999_999)

    def test_no_credential_is_embedded_in_the_repository(self) -> None:
        source = (SCRIPTS / "s3.py").read_text()
        self.assertNotIn("GK", source.replace("BUCKET", ""))
        self.assertIn("Keychain", source)


class WindowsTests(unittest.TestCase):
    def test_generated_password_meets_complexity_rules(self) -> None:
        for _ in range(20):
            password = lab_windows.generate_password()
            self.assertTrue(any(c.islower() for c in password))
            self.assertTrue(any(c.isupper() for c in password))
            self.assertTrue(any(c.isdigit() for c in password))
            self.assertTrue(any(not c.isalnum() for c in password))

    def test_unattend_is_well_formed_and_escapes_values(self) -> None:
        from xml.etree import ElementTree

        xml = lab_windows.render_unattend(
            locale="en-GB",
            timezone="GMT Standard Time",
            hostname="win-lab",
            owner="a & b",
            image_index=2,
            driver_branch="2k25",
            admin_password="p<a>ss&1",
        )
        root = ElementTree.fromstring(xml)
        self.assertTrue(root.tag.endswith("unattend"))
        self.assertIn("a &amp; b", xml)
        self.assertIn("p&lt;a&gt;ss&amp;1", xml)

    def test_answer_iso_contains_the_answer_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "unattend.iso"
            xml = lab_windows.render_unattend(
                locale="en-GB", timezone="GMT Standard Time", hostname="h",
                owner="o", image_index=1, driver_branch="2k25",
                admin_password="Pw1!",
            )
            lab_windows.build_answer_iso(xml, target)
            produced = target if target.exists() else target.with_suffix(".iso.cdr")
            self.assertTrue(produced.exists(), "no ISO was produced")
            blob = produced.read_bytes()
            self.assertIn(b"AUTOUNATTEND.XML", blob.upper())
            self.assertIn(b"<unattend", blob)


class StorageGuardTests(unittest.TestCase):
    """Formatting a disk is irreversible; every guard gets a test."""

    def _lab(self, disks: list) -> Any:
        lab = mock.Mock()
        lab.LabError = RuntimeError
        lab.NODE = "testnode"
        api = mock.Mock()
        api.call.return_value = disks
        lab.ProxmoxAPI.return_value = api
        return lab, api

    def _args(self, **overrides: Any) -> Any:
        base = dict(
            lease="L", device="/dev/sdb", filesystem="ext4",
            content=lab_storage.DEFAULT_CONTENT, expect_serial=None,
            expect_size_gb=None, wipe_confirmed=False,
            host_change_authorized=True, timeout=60,
        )
        base.update(overrides)
        args = mock.Mock(**base)
        # Mock(name=...) names the mock rather than setting an attribute.
        args.name = overrides.get("name", "test-bulk")
        return args

    USB = {"devpath": "/dev/sdb", "size": 1_000_204_886_016, "serial": "TESTSERIAL",
           "model": "Portable", "type": "hdd", "used": None, "osdisk": 0}

    def test_refuses_without_host_change_authorization(self) -> None:
        lab, _ = self._lab([self.USB])
        with self.assertRaises(RuntimeError) as caught:
            lab_storage.cmd_add_disk(lab, self._args(host_change_authorized=False))
        self.assertIn("host-level", str(caught.exception))

    def test_refuses_the_os_disk(self) -> None:
        lab, _ = self._lab([{**self.USB, "osdisk": 1}])
        with self.assertRaises(RuntimeError) as caught:
            lab_storage.cmd_add_disk(lab, self._args())
        self.assertIn("OS disk", str(caught.exception))

    def test_refuses_a_disk_already_in_use(self) -> None:
        lab, _ = self._lab([{**self.USB, "used": "LVM"}])
        with self.assertRaises(RuntimeError) as caught:
            lab_storage.cmd_add_disk(lab, self._args())
        self.assertIn("already in use", str(caught.exception))

    def test_refuses_on_serial_mismatch(self) -> None:
        lab, _ = self._lab([self.USB])
        with self.assertRaises(RuntimeError) as caught:
            lab_storage.cmd_add_disk(lab, self._args(expect_serial="OTHER"))
        self.assertIn("serial mismatch", str(caught.exception))

    def test_refuses_on_size_mismatch(self) -> None:
        lab, _ = self._lab([self.USB])
        with self.assertRaises(RuntimeError) as caught:
            lab_storage.cmd_add_disk(lab, self._args(expect_size_gb=250))
        self.assertIn("size mismatch", str(caught.exception))

    def test_refuses_an_unknown_device(self) -> None:
        lab, _ = self._lab([self.USB])
        with self.assertRaises(RuntimeError) as caught:
            lab_storage.cmd_add_disk(lab, self._args(device="/dev/sdz"))
        self.assertIn("not a disk", str(caught.exception))

    def test_matching_serial_and_size_proceed(self) -> None:
        lab, api = self._lab([self.USB])

        def call(method: str, path: str, data: Any = None) -> Any:
            if path.endswith("/disks/list"):
                return [self.USB]
            return "UPID:testnode:task"

        api.call.side_effect = call
        lab.wait_task.return_value = {"exitstatus": "OK"}
        lab_storage.cmd_add_disk(
            lab, self._args(expect_serial="TESTSERIAL", expect_size_gb=1000)
        )
        formatted = [
            c for c in api.call.call_args_list if "disks/directory" in c[0][1]
        ]
        self.assertEqual(len(formatted), 1, "the format call should fire once")
        self.assertEqual(formatted[0][0][2]["device"], "/dev/sdb")

    def test_failing_to_set_content_types_is_not_a_success(self) -> None:
        """Found live: the disk was formatted and registered, setting content
        types failed, and the command still exited 0. A caller then uploaded
        to a storage that would not accept the content."""
        import contextlib
        import io

        lab, api = self._lab([self.USB])

        def call(method: str, path: str, data: Any = None) -> Any:
            if path.endswith("/disks/list"):
                return [self.USB]
            if method == "PUT" and path.startswith("/storage/"):
                raise RuntimeError("HTTP 500: storage 'test-bulk' is busy")
            return "UPID:testnode:task"

        api.call.side_effect = call
        lab.wait_task.return_value = {"exitstatus": "OK"}
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            with self.assertRaises(RuntimeError) as caught:
                lab_storage.cmd_add_disk(lab, self._args())
        self.assertIn("set-content", str(caught.exception))
        # The partial state is still reported, so recovery does not need the
        # command to be rerun (which would reformat the disk).
        result = json.loads(printed.getvalue())
        self.assertFalse(result["content_configured"])
        self.assertIn("content_warning", result)
        self.assertEqual(result["storage"], "test-bulk")
        self.assertFalse(lab.audit.call_args.kwargs["content_configured"])


class SerialSessionTests(unittest.TestCase):
    """A serial console echoes what you type and wraps long lines, so these
    cover the parsing that separates a command's output from its own echo."""

    def _session(self, transcript_for):
        from proxmox_agent_lab import console as lab_console

        session = lab_console.TermSession.__new__(lab_console.TermSession)
        sent: list[str] = []
        session.send_line = sent.append  # type: ignore[method-assign]

        def fake_expect(patterns, timeout=60.0, poke=False):
            return patterns[0], transcript_for(sent[-1], patterns[0])

        session.expect = fake_expect  # type: ignore[method-assign]
        return session, sent

    def test_markers_are_not_matched_by_the_command_echo(self) -> None:
        seen: dict[str, str] = {}

        def transcript(typed: str, end_marker: str) -> str:
            seen["typed"] = typed
            seen["end"] = end_marker
            begin = end_marker.replace("__e", "__b", 1)
            return f"{typed}\n{begin}\nhello\n{end_marker}0\n"

        session, sent = self._session(transcript)
        session.run("echo hello")
        # The echoed command must not contain either marker verbatim,
        # otherwise expect() returns before the command has even run.
        self.assertNotIn(seen["end"], seen["typed"])
        self.assertNotIn(seen["end"].replace("__e", "__b", 1), seen["typed"])
        # But the shell must still reconstruct them.
        self.assertIn(seen["end"], seen["typed"].replace('""', ""))

    def test_run_returns_output_without_the_echo_or_markers(self) -> None:
        def transcript(typed: str, end_marker: str) -> str:
            begin = end_marker.replace("__e", "__b", 1)
            return f"{typed}\r\n{begin}\r\nnameserver 10.66.0.1\r\n{end_marker}0\r\n"

        session, _ = self._session(transcript)
        output, code = session.run_status("cat /etc/resolv.conf")
        self.assertEqual(output, "nameserver 10.66.0.1")
        self.assertEqual(code, 0)

    def test_wrapped_echo_does_not_leak_into_the_output(self) -> None:
        """A console hard-wraps long commands mid-token; output must survive."""
        def transcript(typed: str, end_marker: str) -> str:
            begin = end_marker.replace("__e", "__b", 1)
            wrapped = typed[:20] + "\r\n" + typed[20:]
            return f"{wrapped}\r\n{begin}\r\nREACHABLE\r\n{end_marker}0\r\n"

        session, _ = self._session(transcript)
        self.assertEqual(session.run("ping -c2 -W3 1.1.1.1 && echo yes"),
                         "REACHABLE")

    def test_nonzero_exit_code_is_reported(self) -> None:
        def transcript(typed: str, end_marker: str) -> str:
            begin = end_marker.replace("__e", "__b", 1)
            return f"{typed}\n{begin}\nboom\n{end_marker}7\n"

        session, _ = self._session(transcript)
        self.assertEqual(session.run_status("false"), ("boom", 7))

    def test_guest_output_is_not_double_base64_decoded(self) -> None:
        """Proxmox pre-decodes guest output; decoding again corrupts it.

        Only output that is *coincidentally* valid base64 was affected -- a
        bare timestamp, a hex digest -- so the bug hid behind an exception
        fallback that made everything else look correct.
        """
        from proxmox_agent_lab import console as lab_console

        lab = mock.Mock()
        lab.NODE = "testnode"
        api = mock.Mock()
        api.call.side_effect = [
            {"pid": 1},
            {"exited": 1, "exitcode": 0, "out-data": "1786125185\n",
             "err-data": ""},
        ]
        result = lab_console.agent_exec(lab, api, 9000, ["true"])
        self.assertEqual(result["stdout"], "1786125185\n")
        self.assertEqual(result["exitcode"], 0)

    def test_a_signal_killed_process_is_never_reported_as_exitcode_none(self) -> None:
        """qemu-guest-agent reports either exitcode or signal, never both.

        Every caller decides success with `exitcode not in (0, None)`. If a
        signal-killed process (OOM, crash, an external kill) came back as
        exitcode None, it would look exactly like the "no code available"
        case the serial channel legitimately has, instead of the failure it
        actually is.
        """
        from proxmox_agent_lab import console as lab_console

        lab = mock.Mock()
        lab.NODE = "testnode"
        api = mock.Mock()
        api.call.side_effect = [
            {"pid": 1},
            {"exited": 1, "signal": 9, "out-data": "", "err-data": ""},
        ]
        result = lab_console.agent_exec(lab, api, 9000, ["sleep", "300"])
        self.assertEqual(result["signal"], 9)
        self.assertNotIn(result["exitcode"], (0, None))
        self.assertEqual(result["exitcode"], 137)

    def test_send_line_declares_byte_length_not_character_length(self) -> None:
        from proxmox_agent_lab import console as lab_console

        session = lab_console.TermSession.__new__(lab_console.TermSession)
        frames: list[bytes] = []
        session.socket = type("S", (), {"send": lambda _s, d: frames.append(d)})()
        session.send_line("café")
        header, _, payload = frames[0].partition(b":")[2].partition(b":")
        self.assertEqual(int(header), len(payload),
                         "length prefix must count bytes, not characters")


class VpnGatewayTests(unittest.TestCase):
    def test_wg_config_pulls_every_key_from_the_keychain(self) -> None:
        with mock.patch.object(
            lab_netgw, "_keychain", side_effect=lambda account: f"<{account}>"
        ), mock.patch.object(lab_netgw, "VPN_ENABLED", True):
            config = lab_netgw.render_wg_config()
        self.assertIn("PrivateKey = <wg-private-key>", config)
        self.assertIn("PresharedKey = <wg-preshared-key>", config)
        self.assertIn("PublicKey = <wg-peer-public-key>", config)
        self.assertIn(f"Endpoint = {lab_netgw.WG_ENDPOINT}", config)
        self.assertIn("AllowedIPs = 0.0.0.0/0, ::/0", config)

    def test_forwarding_fails_closed_without_the_tunnel(self) -> None:
        rules = lab_netgw.render_nftables()
        self.assertIn("type filter hook forward priority 0; policy drop;", rules)
        self.assertIn('iifname "__LAB_IF__" oifname "wg0" accept', rules)
        self.assertIn('oifname "wg0" masquerade', rules)
        # The leak that matters: every accept must egress via the tunnel.
        accepts = [
            line.strip() for line in rules.splitlines()
            if line.strip().endswith("accept") and "policy" not in line
        ]
        self.assertTrue(accepts)
        for line in accepts:
            self.assertIn('"wg0"', line, f"accept rule bypasses the tunnel: {line}")

    def test_interface_names_are_resolved_at_provision_time(self) -> None:
        """Hardcoded names fail silently: rules that never match look fine."""
        script = lab_netgw.provision_script()
        self.assertIn(lab_netgw.LAB_GATEWAY_IP, script)
        self.assertNotIn("__LAB_GATEWAY_IP__", script)
        self.assertIn("s/__LAB_IF__/$LAB_IF/g", script)
        # And it must refuse to proceed rather than install rules that
        # silently match nothing.
        self.assertIn("cannot build the ruleset", script)
        self.assertIn("exit 1", script)
        for rendered in (lab_netgw.render_nftables(), lab_netgw.render_dnsmasq()):
            self.assertIn("__LAB_IF__", rendered)
            self.assertNotIn('"eth1"', rendered)

    def test_bootstrap_password_is_cleared_after_use(self) -> None:
        source = (SCRIPTS / "netgw.py").read_text()
        self.assertIn('{"delete": "cipassword"}', source)

    def test_verify_requires_a_lease_and_checks_handshake_age(self) -> None:
        source = (SCRIPTS / "netgw.py").read_text()
        self.assertIn("lab.load_lease(args.lease)", source)
        self.assertIn("max_handshake_age", source)
        # A handshake that merely exists is not proof of a live tunnel.
        self.assertNotIn('"ok": last > 0,', source)

    def test_dnsmasq_points_guests_at_the_tunnel_resolver(self) -> None:
        config = lab_netgw.render_dnsmasq()
        self.assertIn(f"server={lab_netgw.WG_DNS}", config)
        self.assertIn("no-resolv", config)
        self.assertIn(
            f"dhcp-option=option:router,{lab_netgw.LAB_GATEWAY_IP}", config
        )

    def test_probes_are_wrapped_in_command_substitution(self) -> None:
        """`echo X={cmd}` echoes the command text instead of running it."""
        source = (SCRIPTS / "netgw.py").read_text()
        for bad in ("echo PING={_ping()}", "echo DOWN={_ping()}",
                    "echo BACK={_ping()}"):
            self.assertNotIn(bad, source, f"{bad} must be wrapped in $( … )")

    def test_inconclusive_kill_switch_is_not_reported_as_a_leak(self) -> None:
        """A probe that returns nothing is unproven, never a detected leak.

        The first live run emitted "KILL SWITCH LEAK" purely because the probe
        was broken. A false alarm here is as damaging as a missed leak.
        """
        source = (SCRIPTS / "netgw.py").read_text()
        self.assertIn('checks.get("fails_closed") is False', source)
        self.assertIn('checks.get("fails_closed") is None', source)
        self.assertIn("not a detected leak", source)

    def test_command_substitution_is_not_arithmetic_expansion(self) -> None:
        """`$(` immediately followed by `(` is arithmetic, and hangs the shell.

        The leak-test fetch helper is wrapped in parentheses, so composing it
        as `$({fetch})` produced `$((curl ...)` -- the shell waited forever at
        a continuation prompt instead of running anything.
        """
        source = (SCRIPTS / "netgw.py").read_text()
        self.assertNotIn(
            "$({_fetch", source,
            "wrap _fetch as `$( {…} )`; `$({…})` becomes `$((` and hangs",
        )
        fetch = lab_netgw._fetch("https://example.invalid")
        composed = f"echo IP=$( {fetch} )"
        self.assertNotIn("$((", composed)

    def test_no_key_material_in_the_repository(self) -> None:
        source = (SCRIPTS / "netgw.py").read_text()
        import re as _re

        self.assertIsNone(
            _re.search(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{43}=", source),
            "a WireGuard-shaped key is embedded in lab_netgw.py",
        )

    def test_host_bridge_refuses_without_authorization(self) -> None:
        lab = mock.Mock()
        lab.LabError = RuntimeError
        args = mock.Mock(host_change_authorized=False)
        with self.assertRaises(RuntimeError) as caught:
            lab_netgw.cmd_host_bridge(lab, args)
        self.assertIn("host networking", str(caught.exception))


if __name__ == "__main__":
    unittest.main()


class ScreenshotCommandTests(unittest.TestCase):
    def test_temporal_baselines_are_isolated_by_lease(self) -> None:
        from proxmox_agent_lab import console as lab_console

        lab = mock.Mock()
        with tempfile.TemporaryDirectory() as tmp:
            lab.STATE_ROOT = Path(tmp)
            first = b"\x00\x00\x00" * 4
            changed = b"\xff\xff\xff" * 4
            _, initial = lab_console._model_frame(
                lab, "lease-one", 7, first, 2, 2
            )
            _, other_lease = lab_console._model_frame(
                lab, "lease-two", 7, changed, 2, 2
            )
            _, repeated = lab_console._model_frame(
                lab, "lease-one", 7, changed, 2, 2
            )

        self.assertFalse(initial["baseline"])
        self.assertFalse(other_lease["baseline"])
        self.assertTrue(repeated["baseline"])

    def test_inspect_refuses_to_transmit_an_unowned_guest(self) -> None:
        from proxmox_agent_lab import console as lab_console

        lab = mock.Mock()
        lab.LabError = RuntimeError
        lab.load_lease.return_value = {
            "resources": [{"kind": "qemu", "vmid": 8}]
        }
        lab.require_lease_resource.side_effect = RuntimeError(
            "VMID 7 is not a qemu guest registered to this lease"
        )
        args = mock.Mock(lease="lease-12345678", vmid=7)
        with mock.patch.object(lab_console, "VncSession") as vnc, \
             mock.patch.object(lab_console.vision, "analyze_png") as analyze:
            with self.assertRaises(RuntimeError) as caught:
                lab_console.cmd_inspect(lab, args)
        self.assertIn("not a qemu guest registered", str(caught.exception))
        vnc.assert_not_called()
        analyze.assert_not_called()

    def test_inspect_requires_lease_ownership_and_audits_provider(self) -> None:
        from proxmox_agent_lab import console as lab_console

        lab = mock.Mock()
        lab.LabError = RuntimeError
        lab.load_lease.return_value = {
            "resources": [{"kind": "qemu", "vmid": 7}]
        }
        session = mock.MagicMock()
        session.__enter__.return_value = session
        session.client.width, session.client.height = 2, 2
        session.client.capture.return_value = b"\x00\x00\x00" * 4

        with tempfile.TemporaryDirectory() as tmp:
            lab.STATE_ROOT = Path(tmp)
            out = Path(tmp) / "inspect.png"
            args = mock.Mock(
                lease="lease-12345678", vmid=7, settle=2.0, out=str(out),
                prompt=None, timeout=120, max_tokens=1024, provider="auto",
            )
            with mock.patch.object(lab_console, "VncSession",
                                   return_value=session), \
                 mock.patch.object(lab, "ProxmoxAPI"), \
                 mock.patch.object(lab_console.vision, "analyze_png",
                                   return_value={
                                       "provider": "nvidia",
                                       "model": lab_console.vision.MODEL,
                                       "analysis": {"screen": "gui"},
                                   }) as analyze, \
                 mock.patch("builtins.print") as printed:
                lab_console.cmd_inspect(lab, args)
                payload = json.loads(printed.call_args.args[0])
                original_png = out.read_bytes()
                model_png = analyze.call_args.args[1]
                grid_existed = Path(payload["model_input"]["path"]).exists()

        self.assertEqual(payload["transmitted_to"], "integrate.api.nvidia.com")
        self.assertEqual(payload["vision"]["analysis"]["screen"], "gui")
        self.assertEqual(payload["model_input"]["grid_step"], 100)
        self.assertEqual(payload["model_input"]["origin"], "top-left")
        self.assertTrue(payload["model_input"]["path"].endswith("-grid.png"))
        self.assertTrue(grid_existed)
        self.assertNotEqual(model_png, original_png)
        self.assertIn("X increases right", analyze.call_args.kwargs["prompt"])
        lab.audit.assert_called_once_with(
            "console-vision-inspect", lease=args.lease, vmid=7,
            provider="nvidia", model=lab_console.vision.MODEL, sync=False,
        )

    def test_cmd_screenshot_writes_a_file(self) -> None:
        """Regression: the module `png` was shadowed by a local of the same
        name, so this command raised UnboundLocalError. Testing the encoder
        alone did not catch it -- only calling the command does."""
        from proxmox_agent_lab import console as lab_console

        lab = mock.Mock()
        lab.LabError = RuntimeError
        session = mock.MagicMock()
        session.__enter__.return_value = session
        session.client.capture.return_value = b"\x00\x00\x00" * 4
        session.client.width, session.client.height = 2, 2

        with tempfile.TemporaryDirectory() as tmp:
            lab.STATE_ROOT = Path(tmp)
            out = Path(tmp) / "shot.png"
            args = mock.Mock(vmid=1, out=str(out), settle=0, timeout=5,
                             upload=False, url_expiry=60, ocr=False)
            with mock.patch.object(lab_console, "VncSession",
                                   return_value=session), \
                 mock.patch.object(lab, "ProxmoxAPI"), \
                 mock.patch("builtins.print"):
                lab_console.cmd_screenshot(lab, args)
            self.assertTrue(out.exists())
            self.assertTrue(out.read_bytes().startswith(b"\x89PNG"))

    def test_cmd_screenshot_burst_stitches_captures_over_time(self) -> None:
        from proxmox_agent_lab import console as lab_console

        lab = mock.Mock()
        lab.LabError = RuntimeError
        session = mock.MagicMock()
        session.__enter__.return_value = session
        session.client.capture.return_value = b"\x00\x00\x00" * 4
        session.client.width, session.client.height = 2, 2

        with tempfile.TemporaryDirectory() as tmp:
            lab.STATE_ROOT = Path(tmp)
            out = Path(tmp) / "burst.png"
            args = mock.Mock(vmid=1, out=str(out), count=3, interval=10.0,
                             timeout=5, upload=False, url_expiry=60)
            with mock.patch.object(lab_console, "VncSession",
                                   return_value=session), \
                 mock.patch.object(lab, "ProxmoxAPI"), \
                 mock.patch.object(lab_console.time, "sleep") as sleep, \
                 mock.patch("builtins.print") as printed:
                lab_console.cmd_screenshot_burst(lab, args)
            payload = json.loads(printed.call_args.args[0])

            self.assertEqual(session.client.capture.call_count, 3)
            self.assertEqual(sleep.call_count, 2)  # never sleeps after the last
            sleep.assert_called_with(10.0)
            self.assertEqual(payload["frame_count"], 3)
            self.assertEqual(payload["width"], 2 * 3 + 4 * 2)  # 3 frames + 2 gaps
            self.assertTrue(out.exists())
            self.assertTrue(out.read_bytes().startswith(b"\x89PNG"))

    def test_cmd_screenshot_burst_rejects_a_bad_count(self) -> None:
        from proxmox_agent_lab import console as lab_console

        lab = mock.Mock()
        lab.LabError = RuntimeError
        args = mock.Mock(vmid=1, count=0, interval=10.0)
        with self.assertRaises(RuntimeError) as caught:
            lab_console.cmd_screenshot_burst(lab, args)
        self.assertIn("--count", str(caught.exception))

    def test_save_screenshot_flags_identical_repeat_frames(self) -> None:
        """A pixel-identical repeat capture is reported as possibly stale."""
        from proxmox_agent_lab import console as lab_console

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            first = lab_console._save_screenshot(
                7, b"\x00\x00\x00" * 4, 2, 2,
                override=str(Path(tmp) / "a.png"), state_root=state,
            )
            repeat = lab_console._save_screenshot(
                7, b"\x00\x00\x00" * 4, 2, 2,
                override=str(Path(tmp) / "b.png"), state_root=state,
            )
            changed = lab_console._save_screenshot(
                7, b"\xff\xff\xff" * 4, 2, 2,
                override=str(Path(tmp) / "c.png"), state_root=state,
            )

        self.assertFalse(first["identical_to_previous_capture"])
        self.assertNotIn("stale_possible", first)
        self.assertTrue(repeat["identical_to_previous_capture"])
        self.assertIn("stale_possible", repeat)
        self.assertIn("recapture before acting", repeat["stale_possible"])
        self.assertFalse(changed["identical_to_previous_capture"])
        self.assertNotIn("stale_possible", changed)

    def test_click_requires_independent_target_verification(self) -> None:
        from proxmox_agent_lab import console as lab_console

        lab = mock.Mock()
        lab.LabError = RuntimeError
        session = mock.MagicMock()
        session.__enter__.return_value = session
        session.client.width, session.client.height = 2, 2
        session.client.capture.return_value = b"\x00\x00\x00" * 4

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "after.png"
            args = mock.Mock(
                lease="lease-12345678", vmid=1, x=0, y=0, button=1,
                double=False, screenshot_after=2.5, screenshot_out=str(out),
                target="OK", empty_space=False, calibration_settle=1.0,
                vision_timeout=10, provider="auto",
            )
            with mock.patch.object(lab, "STATE_ROOT", Path(tmp)), \
                 mock.patch.object(lab_console, "VncSession",
                                   return_value=session), \
                 mock.patch.object(lab, "ProxmoxAPI"), \
                 mock.patch.object(lab_console.vision, "analyze_png",
                                   return_value={
                                       "provider": "nvidia",
                                       "analysis": {
                                           "controls": [
                                               {"label": "OK",
                                                "bbox": [0, 0, 2, 2]},
                                           ],
                                           "recommended_action": {
                                               "kind": "click", "value": "0,0",
                                           },
                                       },
                                   }), \
                 mock.patch.object(lab_console.vision, "verifies_target",
                                   side_effect=[(False, "wrong target"),
                                                (True, "matched")]), \
                 mock.patch("builtins.print") as printed:
                # A rejected checkpoint moves the cursor but cannot click.
                lab_console.cmd_click(lab, args)
                first = json.loads(printed.call_args.args[0])
                self.assertFalse(first["clicked"])
                self.assertFalse(first["verification"]["accepted"])
                session.client.click.assert_not_called()

                # Only an independent positive verdict performs the click.
                lab_console.cmd_click(lab, args)
                payload = json.loads(printed.call_args.args[0])

            self.assertEqual(session.client.click.call_count, 1)
            session.client.click.assert_called_with(0, 0, button=1, double=False)
            self.assertTrue(out.read_bytes().startswith(b"\x89PNG"))
            self.assertEqual(payload["screenshot_after"]["path"], str(out))
            self.assertTrue(payload["verification"]["accepted"])
            self.assertEqual(payload["control_bbox"], [0, 0, 2, 2])

    def test_click_empty_space_bypasses_target_verification(self) -> None:
        import argparse

        lab = mock.Mock()
        lab.LabError = RuntimeError
        parser = argparse.ArgumentParser()
        lab_console.register(parser.add_subparsers(), lab)
        args = parser.parse_args([
            "console", "click", "--lease", "lease-12345678", "--vmid", "1",
            "--x", "1", "--y", "1", "--button", "3", "--empty-space",
        ])
        session = mock.MagicMock()
        session.__enter__.return_value = session
        session.client.width, session.client.height = 2, 2

        with mock.patch.object(lab_console, "VncSession",
                               return_value=session), \
             mock.patch.object(lab, "ProxmoxAPI"), \
             mock.patch.object(lab_console.vision, "analyze_png") as analyze, \
             mock.patch("builtins.print") as printed:
            lab_console.cmd_click(lab, args)

        session.client.click.assert_called_once_with(1, 1, button=3, double=False)
        session.client.capture.assert_not_called()
        analyze.assert_not_called()
        lab.audit.assert_called_once_with(
            "console-click-unverified", lease="lease-12345678", vmid=1,
            x=1, y=1, button=3, sync=False,
        )
        payload = json.loads(printed.call_args.args[0])
        self.assertEqual(payload["clicked"], [1, 1])
        self.assertTrue(payload["empty_space"])
        self.assertIn("unverified", payload["verification"]["reason"])

    def test_has_gui_locked_up_true_when_nothing_changes(self) -> None:
        from proxmox_agent_lab import console as lab_console

        lab = mock.Mock()
        lab.LabError = RuntimeError
        session = mock.MagicMock()
        session.__enter__.return_value = session
        session.client.width, session.client.height = 4, 4
        session.client.capture.return_value = b"\x00\x00\x00" * 16
        args = mock.Mock(lease="lease-1", vmid=1, settle=0.0,
                         timeout=5, threshold=24)
        with mock.patch.object(lab_console, "VncSession",
                               return_value=session), \
             mock.patch.object(lab, "ProxmoxAPI"), \
             mock.patch("builtins.print") as printed:
            lab_console.cmd_has_gui_locked_up(lab, args)
        payload = json.loads(printed.call_args.args[0])

        self.assertEqual(session.client.pointer.call_count, 2)
        self.assertTrue(payload["locked_up"])
        self.assertEqual(payload["changed_pixels_per_probe"], [0, 0])
        self.assertIn("caveat", payload)
        lab.audit.assert_called_once_with(
            "console-has-gui-locked-up", lease="lease-1", vmid=1,
            locked_up=True, sync=False,
        )

    def test_has_gui_locked_up_false_when_a_probe_sees_change(self) -> None:
        from proxmox_agent_lab import console as lab_console

        lab = mock.Mock()
        lab.LabError = RuntimeError
        session = mock.MagicMock()
        session.__enter__.return_value = session
        session.client.width, session.client.height = 4, 4
        blank = b"\x00\x00\x00" * 16
        changed = b"\xff\xff\xff" * 16
        session.client.capture.side_effect = [blank, changed, changed]
        args = mock.Mock(lease="lease-1", vmid=1, settle=0.0,
                         timeout=5, threshold=24)
        with mock.patch.object(lab_console, "VncSession",
                               return_value=session), \
             mock.patch.object(lab, "ProxmoxAPI"), \
             mock.patch("builtins.print") as printed:
            lab_console.cmd_has_gui_locked_up(lab, args)
        payload = json.loads(printed.call_args.args[0])

        self.assertFalse(payload["locked_up"])
        self.assertNotIn("caveat", payload)

    def test_has_terminal_locked_up_refuses_a_graphical_screen(self) -> None:
        from proxmox_agent_lab import console as lab_console

        lab = mock.Mock()
        lab.LabError = RuntimeError
        session = mock.MagicMock()
        session.__enter__.return_value = session
        session.client.width, session.client.height = 8, 8
        # Many distinct colours: textmode.analyse should call this graphical.
        session.client.capture.return_value = bytes(range(192))
        args = mock.Mock(vmid=1, samples=2, interval=0.0, timeout=5, threshold=24)
        with mock.patch.object(lab_console, "VncSession",
                               return_value=session), \
             mock.patch.object(lab, "ProxmoxAPI"):
            with self.assertRaises(RuntimeError) as caught:
                lab_console.cmd_has_terminal_locked_up(lab, args)
        self.assertIn("not a text console", str(caught.exception))

    def test_has_terminal_locked_up_true_when_static(self) -> None:
        from proxmox_agent_lab import console as lab_console

        lab = mock.Mock()
        lab.LabError = RuntimeError
        session = mock.MagicMock()
        session.__enter__.return_value = session
        session.client.width, session.client.height = 8, 8
        # A handful of colours reads as a text console to textmode.analyse.
        frame = (b"\x00\x00\x00" * 60) + (b"\xff\xff\xff" * 4)
        session.client.capture.return_value = frame
        args = mock.Mock(vmid=1, samples=3, interval=0.0, timeout=5, threshold=24)
        with mock.patch.object(lab_console, "VncSession",
                               return_value=session), \
             mock.patch.object(lab, "ProxmoxAPI"), \
             mock.patch("builtins.print") as printed:
            lab_console.cmd_has_terminal_locked_up(lab, args)
        payload = json.loads(printed.call_args.args[0])

        self.assertTrue(payload["locked_up"])
        self.assertEqual(payload["changed_pixels_per_sample"], [0, 0])
        self.assertIn("caveat", payload)

    def test_has_terminal_locked_up_rejects_too_few_samples(self) -> None:
        from proxmox_agent_lab import console as lab_console

        lab = mock.Mock()
        lab.LabError = RuntimeError
        args = mock.Mock(vmid=1, samples=1, interval=0.0)
        with self.assertRaises(RuntimeError) as caught:
            lab_console.cmd_has_terminal_locked_up(lab, args)
        self.assertIn("--samples", str(caught.exception))

    def test_inspect_audits_vision_failure(self) -> None:
        """A rejected vision analysis must leave a journal trail."""
        from proxmox_agent_lab import console as lab_console

        lab = mock.Mock()
        lab.LabError = RuntimeError
        lab.load_lease.return_value = {
            "resources": [{"kind": "qemu", "vmid": 7}]
        }
        session = mock.MagicMock()
        session.__enter__.return_value = session
        session.client.width, session.client.height = 2, 2
        session.client.capture.return_value = b"\x00\x00\x00" * 4
        message = (
            "no vision provider returned a valid analysis: "
            "nvidia: rejected (some/vision-model: screen is not a non-empty string)"
        )

        with tempfile.TemporaryDirectory() as tmp:
            lab.STATE_ROOT = Path(tmp)
            args = mock.Mock(
                lease="lease-12345678", vmid=7, settle=2.0,
                out=str(Path(tmp) / "inspect.png"), prompt=None, timeout=120,
                max_tokens=1024, provider="nvidia",
            )
            with mock.patch.object(lab_console, "VncSession",
                                   return_value=session), \
                 mock.patch.object(lab, "ProxmoxAPI"), \
                 mock.patch.object(lab_console.vision, "analyze_png",
                                   side_effect=lab_console.vision.VisionError(
                                       message
                                   )):
                with self.assertRaises(RuntimeError) as caught:
                    lab_console.cmd_inspect(lab, args)

        self.assertEqual(str(caught.exception), message)
        lab.audit.assert_called_once_with(
            "console-vision-inspect-failed", lease=args.lease, vmid=7,
            error=message[:200], provider="nvidia", sync=False,
        )


class ChunkedTransferTests(unittest.TestCase):
    """Chunked push/pull: part keys, reassembly, and hash verification."""

    def _lab(self) -> mock.Mock:
        lab = mock.Mock()
        lab.LabError = RuntimeError
        lab.NODE = "aipve"
        lab.STATE_ROOT = "/tmp/pb-state"
        lab.load_lease.return_value = {"resources": []}
        lab.iso_now = lambda: "2026-08-11T00:00:00Z"
        return lab

    def _args(self, lab: mock.Mock, *argv: str) -> object:
        import argparse

        parser = argparse.ArgumentParser()
        lab_console.register(parser.add_subparsers(), lab)
        return parser.parse_args(list(argv))

    def test_fetch_parts_command_has_all_urls_and_hash(self) -> None:
        command = lab_console._fetch_parts_command(
            ["https://s3/part-0", "https://s3/part-1"], "/tmp/out.bin"
        )
        self.assertEqual(command[0], "/bin/sh")
        script = command[2]
        self.assertIn("https://s3/part-0", script)
        self.assertIn("https://s3/part-1", script)
        self.assertIn("cat /tmp/pp-* > /tmp/out.bin", script)
        self.assertIn("sha256sum /tmp/out.bin", script)

    def test_push_chunked_uploads_parts_and_verifies(self) -> None:
        import hashlib

        with tempfile.TemporaryDirectory() as tmp:
            lab = self._lab()
            source = Path(tmp) / "payload.bin"
            payload = b"a" * (2 * 1024 * 1024 + 13)  # 2 parts at 1 MiB
            source.write_bytes(payload)
            api = mock.Mock()
            lab.ProxmoxAPI.return_value = api
            fake_s3 = mock.Mock()
            fake_s3.put_bytes.return_value = "push/abc/payload.bin"
            fake_s3.presign.return_value = "https://s3/part"
            with mock.patch.object(lab_console, "SINGLE_OBJECT_MAX_MB", 0), \
                 mock.patch.object(lab_console, "s3", fake_s3), \
                 mock.patch.object(
                     lab_console, "agent_exec",
                     return_value={
                         "exitcode": 0,
                         "stdout": hashlib.sha256(payload).hexdigest(),
                         "stderr": "",
                     },
                 ):
                args = self._args(
                    lab, "push", "--lease", "L1", "--vmid", "7",
                    "--file", str(source), "--chunk-size", "1",
                )
                lab_console.cmd_push(lab, args)
            keys = [c.args[0] for c in fake_s3.put_bytes.call_args_list]
            self.assertEqual(len(keys), 3)
            self.assertTrue(all(
                k.endswith(("/part-0000", "/part-0001", "/part-0002"))
                for k in keys
            ))

    def test_push_chunked_raises_on_guest_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lab = self._lab()
            source = Path(tmp) / "payload.bin"
            source.write_bytes(b"a" * (2 * 1024 * 1024 + 1))
            api = mock.Mock()
            lab.ProxmoxAPI.return_value = api
            fake_s3 = mock.Mock()
            fake_s3.put_bytes.return_value = "push/abc/payload.bin"
            fake_s3.presign.return_value = "https://s3/part"
            with mock.patch.object(lab_console, "SINGLE_OBJECT_MAX_MB", 0), \
                 mock.patch.object(lab_console, "s3", fake_s3), \
                 mock.patch.object(
                     lab_console, "agent_exec",
                     return_value={"exitcode": 0, "stdout": "deadbeef",
                                   "stderr": ""},
                 ):
                args = self._args(
                    lab, "push", "--lease", "L1", "--vmid", "7",
                    "--file", str(source), "--chunk-size", "1",
                    "--sha256", "0" * 64,
                )
                with self.assertRaises(RuntimeError) as caught:
                    lab_console.cmd_push(lab, args)
            self.assertIn("sha256 mismatch", str(caught.exception))

    def test_pull_skips_when_local_file_already_matches(self) -> None:
        import hashlib

        with tempfile.TemporaryDirectory() as tmp:
            lab = self._lab()
            out = Path(tmp) / "artifact.iso"
            payload = b"x" * (2 * 1024 * 1024 + 7)
            out.write_bytes(payload)
            expected = hashlib.sha256(payload).hexdigest()
            api = mock.Mock()
            lab.ProxmoxAPI.return_value = api
            with mock.patch.object(lab_console, "s3", mock.Mock()), \
                 mock.patch.object(lab_console, "agent_exec") as execute:
                args = self._args(
                    lab, "pull", "--lease", "L1", "--vmid", "7",
                    "--remote", "/tmp/artifact.iso", "--out", str(out),
                    "--sha256", expected,
                )
                lab_console.cmd_pull(lab, args)
            execute.assert_not_called()

    def test_pull_assembles_parts_and_checks_guest_hash(self) -> None:
        import hashlib

        with tempfile.TemporaryDirectory() as tmp:
            lab = self._lab()
            out = Path(tmp) / "artifact.iso"
            payload = b"y" * (1024 * 1024 + 1)  # two 1 MiB parts
            expected = hashlib.sha256(payload).hexdigest()
            api = mock.Mock()
            lab.ProxmoxAPI.return_value = api
            fake_s3 = mock.Mock()
            fake_s3.list_objects.return_value = []
            half = len(payload) // 2
            fake_s3.get_bytes.side_effect = [payload[:half],
                                             payload[half:]]
            fake_s3.presign.return_value = "https://s3/put"
            with mock.patch.object(lab_console, "SINGLE_OBJECT_MAX_MB", 0), \
                 mock.patch.object(lab_console, "s3", fake_s3), \
                 mock.patch.object(
                     lab_console, "agent_exec",
                     side_effect=[
                         {"exitcode": 0, "stdout": str(len(payload)),
                          "stderr": ""},
                         {"exitcode": 0, "stdout": str(len(payload)),
                          "stderr": ""},
                         {"exitcode": 0, "stdout": expected, "stderr": ""},
                     ],
                 ):
                args = self._args(
                    lab, "pull", "--lease", "L1", "--vmid", "7",
                    "--remote", "/tmp/artifact.iso", "--out", str(out),
                    "--chunk-size", "1",
                )
                lab_console.cmd_pull(lab, args)
            self.assertEqual(out.read_bytes(), payload)
            self.assertEqual(fake_s3.get_bytes.call_count, 2)
            self.assertEqual(fake_s3.delete_object.call_count, 2)


class SerialDebugTests(unittest.TestCase):
    """--send-raw framing and --from-reset attach-before-reset ordering."""

    def _lab(self) -> mock.Mock:
        lab = mock.Mock()
        lab.LabError = RuntimeError
        lab.NODE = "aipve"
        lab.load_lease.return_value = {"resources": []}
        # A reset keeps the QEMU process alive, so --from-reset only applies to
        # a guest that is already running.
        lab.guest_status.return_value = "running"
        return lab

    def _text_args(self, **overrides: object) -> object:
        import argparse

        defaults = dict(
            vmid=9001, kind="qemu", seconds=0.0, timeout=0.01, follow=False,
            send=None, send_raw=None, nudge=False, from_reset=False,
            lease=None,
        )
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def test_send_raw_frames_bytes_without_trailing_newline(self) -> None:
        session = lab_console.TermSession.__new__(lab_console.TermSession)
        sent: list[bytes] = []
        session.socket = mock.Mock(send=sent.append)
        session.send_raw("cont")
        self.assertEqual(sent, [b"0:4:cont"])

    def test_from_reset_requires_follow(self) -> None:
        lab = self._lab()
        args = self._text_args(from_reset=True, lease="L1")
        with self.assertRaisesRegex(RuntimeError, "requires --follow"):
            lab_console.cmd_text(lab, args)

    def test_from_reset_rejects_lxc(self) -> None:
        lab = self._lab()
        args = self._text_args(
            from_reset=True, follow=True, lease="L1", kind="lxc"
        )
        with self.assertRaisesRegex(RuntimeError, "QEMU"):
            lab_console.cmd_text(lab, args)

    def test_from_reset_requires_lease(self) -> None:
        lab = self._lab()
        args = self._text_args(from_reset=True, follow=True)
        with self.assertRaisesRegex(RuntimeError, "requires --lease"):
            lab_console.cmd_text(lab, args)

    def test_from_reset_attaches_serial_before_resetting(self) -> None:
        lab = self._lab()
        order: list[str] = []
        api = mock.Mock()
        lab.ProxmoxAPI.return_value = api

        def api_call(method: str, path: str, *a: object, **k: object) -> None:
            if path.endswith("/status/reset"):
                order.append("reset")

        api.call.side_effect = api_call

        class FakeSession:
            """Filters like the real session does, so the fake cannot pass a
            stream the production path would have rejected."""

            def __init__(self, *a: object, **k: object) -> None:
                order.append("attach")
                self.socket = mock.Mock()
                self.socket.read_available.return_value = b""
                self.filter = lab_console.TermFilter()

            def read_bytes(self, timeout: float) -> bytes:
                return self.filter.feed(self.socket.read_available(timeout))

            def flush_bytes(self) -> bytes:
                return self.filter.flush()

            def __enter__(self) -> "FakeSession":
                return self

            def __exit__(self, *a: object) -> None:
                return None

        args = self._text_args(from_reset=True, follow=True, lease="L1")
        with mock.patch.object(lab_console, "TermSession", FakeSession):
            lab_console.cmd_text(lab, args)
        self.assertEqual(order[:2], ["attach", "reset"])
        api.call.assert_any_call(
            "POST", "/nodes/aipve/qemu/9001/status/reset"
        )
        lab.require_lease_resource.assert_called_once()


class TermTransportFilterTests(unittest.TestCase):
    """Found live: a termproxy status line was returned as guest serial output.

    It contaminated saved boot logs and could be sent on to a debugger as if
    the guest had printed it. These assert that transport records are removed
    whether or not they arrive whole, and that real guest text is not.
    """

    BANNER = b"starting serial terminal on interface serial0 (press Ctrl+O to exit)\n"
    # What a Proxmox 9.2 node actually sends: no suffix, CRLF terminated.
    LIVE_BANNER = b"starting serial terminal on interface serial0\r\n"

    def test_handshake_and_banner_in_one_read_are_both_removed(self) -> None:
        term = lab_console.TermFilter()
        out = term.feed(b"OK\n" + self.BANNER + b"Booting ReactOS\n")
        self.assertEqual(out, b"Booting ReactOS\n")

    def test_handshake_without_a_newline_is_removed(self) -> None:
        term = lab_console.TermFilter()
        self.assertEqual(term.feed(b"OK" + self.BANNER), b"")
        self.assertEqual(term.feed(b"guest\n"), b"guest\n")

    def test_records_split_across_reads_are_still_removed(self) -> None:
        """A websocket read is not a record boundary."""
        term = lab_console.TermFilter()
        chunks = [b"O", b"K\nstarting serial ter",
                  b"minal on interface serial0 (press Ctrl+O to exit)",
                  b"\nBooting ReactOS\n"]
        out = b"".join(term.feed(chunk) for chunk in chunks)
        self.assertEqual(out, b"Booting ReactOS\n")
        self.assertEqual(term.flush(), b"")

    def test_the_framing_a_real_node_sends(self) -> None:
        """Captured from a Proxmox 9.2 node: the ack arrives alone with no
        newline, a blank CRLF can precede the record, the record is CRLF
        terminated, and guest bytes then arrive a few at a time. The first fix
        for this issue stripped nothing here, because the blank line looked
        like guest output and ended the search."""
        term = lab_console.TermFilter()
        frames = [
            b"OK",
            b"\r\n",
            b"starting serial terminal on interface serial0\r\n",
            b"\r\n",
            b"\x1b[?2", b"004", b"l\r", b"\x1b[", b"?2004h",
            b"de", b"bian", b"@", b"host", b":~", b"$ ",
        ]
        out = b"".join(term.feed(frame) for frame in frames) + term.flush()
        self.assertNotIn(b"starting serial terminal", out)
        self.assertIn(b"debian@host:~$ ", out)

    def test_an_lxc_style_pair_of_records_is_removed_before_the_prompt(self) -> None:
        term = lab_console.TermFilter()
        out = term.feed(b"OK") + term.feed(
            b"Connected to tty 1\r\n"
            b"Type <Ctrl+a q> to exit the console, "
            b"<Ctrl+a Ctrl+a> to enter Ctrl+a itself\r\n"
        ) + term.feed(b"root@ct:~# ")
        self.assertEqual(out, b"root@ct:~# ")

    def test_the_record_is_still_removed_after_guest_echo(self) -> None:
        """It is not always the first thing on the stream."""
        term = lab_console.TermFilter()
        out = term.feed(b"OK\r\n\r\n") + term.feed(
            b"starting serial terminal on interface serial0\r\n[    0.00] boot\n"
        )
        self.assertNotIn(b"starting serial", out)
        self.assertIn(b"[    0.00] boot\n", out)

    def test_the_stopped_guest_refusal_is_not_guest_output(self) -> None:
        """Captured live from a stopped guest: the ticket is issued, the socket
        opens, and this is all that ever arrives. Saved into a boot log it
        reads as something the guest printed."""
        term = lab_console.TermFilter()
        out = term.feed(b"OK") + term.feed(b"VM 9231 not running\r\n")
        self.assertEqual(out + term.flush(), b"")

    def test_a_guest_line_about_something_not_running_survives(self) -> None:
        term = lab_console.TermFilter()
        term.feed(b"OK")
        line = b"systemd: nginx.service is not running, restarting\n"
        self.assertEqual(term.feed(line), line)

    def test_a_short_ambiguous_tail_is_not_held_back(self) -> None:
        """An interactive prompt has no newline; holding it would hang a
        debugger waiting for a byte that has already arrived."""
        term = lab_console.TermFilter()
        term.feed(b"OK")
        self.assertEqual(term.feed(b"C"), b"C")
        self.assertEqual(term.feed(b"on"), b"on")

    def test_a_guest_line_beginning_with_ok_is_preserved(self) -> None:
        """The old prefix test truncated any guest line starting 'OK'."""
        term = lab_console.TermFilter()
        term.feed(b"OK\n")
        self.assertEqual(term.feed(b"OKAY device ready\n"),
                         b"OKAY device ready\n")

    def test_guest_output_is_never_held_waiting_for_a_record(self) -> None:
        """Held bytes must be limited to something that could still be one."""
        term = lab_console.TermFilter()
        term.feed(b"OK\n")
        self.assertEqual(term.feed(b"kdb:> "), b"kdb:> ")

    def test_a_truncated_record_is_not_leaked_when_the_session_ends(self) -> None:
        term = lab_console.TermFilter()
        self.assertEqual(term.feed(b"OK\nstarting serial terminal on inter"), b"")
        self.assertEqual(term.flush(), b"")

    def test_a_bare_handshake_reads_as_no_guest_output(self) -> None:
        term = lab_console.TermFilter()
        self.assertEqual(term.feed(b"OK"), b"")
        self.assertEqual(term.flush(), b"")

    def test_the_lxc_console_banner_is_removed_too(self) -> None:
        term = lab_console.TermFilter()
        out = term.feed(
            b"OK\nConnected to tty 1\n"
            b"Type <Ctrl+a q> to exit the console, "
            b"<Ctrl+a Ctrl+a> to enter Ctrl+a itself\n"
            b"root@ct:~# "
        )
        self.assertEqual(out, b"root@ct:~# ")

    def test_session_read_returns_guest_text_only(self) -> None:
        session = lab_console.TermSession.__new__(lab_console.TermSession)
        session.filter = lab_console.TermFilter()
        reads = [b"OK\n" + self.BANNER, b"Booting ReactOS\n", b""]
        session.socket = mock.Mock()
        session.socket.read_available.side_effect = \
            lambda _t: reads.pop(0) if reads else b""
        self.assertEqual(session.read(0.6), "Booting ReactOS\n")

    def test_a_transport_only_read_does_not_end_the_capture(self) -> None:
        """Found live: the record filtered down to no bytes, read() read that
        as a gap in guest output and stopped, and the prompt arriving right
        after it was lost. Removing noise must not remove signal."""
        session = lab_console.TermSession.__new__(lab_console.TermSession)
        session.filter = lab_console.TermFilter()
        session.last_read_was_empty = True
        reads = [b"OK", b"\r\n", self.LIVE_BANNER, b"debian@host:~$ "]
        session.socket = mock.Mock()
        session.socket.read_available.side_effect = \
            lambda _t: reads.pop(0) if reads else b""
        text = session.read(2.0)
        self.assertIn("debian@host:~$ ", text)
        self.assertNotIn("starting serial terminal", text)

    def test_the_bridge_client_sees_the_same_filtered_stream(self) -> None:
        """The JSON wrapper was not the only leaking path; the bridge was too."""
        reads = [b"OK\n" + self.BANNER, b"Booting ReactOS\n"]

        class FakeTerm:
            def __init__(self, *a: object, **k: object) -> None:
                self.filter = lab_console.TermFilter()
                self.socket = mock.Mock()
                self.socket.read_available.side_effect = \
                    lambda _t: reads.pop(0) if reads else b""

            def read_bytes(self, timeout: float) -> bytes:
                return self.filter.feed(self.socket.read_available(timeout))

            def __enter__(self) -> "FakeTerm":
                return self

            def __exit__(self, *a: object) -> None:
                return None

        import select as select_module

        sent: list[bytes] = []
        client = mock.Mock()
        client.recv.return_value = b""          # third pass: client disconnects
        idle = [([], [], []), ([], [], [])]     # let both guest reads through

        def fake_select(readable, _w, _x, _timeout):
            return idle.pop(0) if idle else (readable, [], [])

        with mock.patch.object(lab_console, "TermSession", FakeTerm), \
             mock.patch.object(select_module, "select", fake_select), \
             mock.patch.object(
                 lab_console, "_bridge_send_all",
                 side_effect=lambda _c, data: bool(sent.append(data)) or True):
            lab_console._bridge_serve(
                mock.Mock(), mock.Mock(), "qemu", 9001, client
            )
        self.assertEqual(b"".join(sent), b"Booting ReactOS\n")
        self.assertNotIn(b"starting serial terminal", b"".join(sent))


class SerialAttachTests(unittest.TestCase):
    """Found live on a Proxmox 9.2 node: `termproxy` issues a ticket for a
    *stopped* guest and the websocket opens, then 'qm terminal' writes
    "VM <id> not running" into the stream and exits. So the documented
    attach-before-power-on capture produced a log with one transport sentence
    in it, and the attach itself could not tell that anything was wrong."""

    def _lab(self, *statuses: str) -> mock.Mock:
        lab = mock.Mock()
        lab.LabError = RuntimeError
        lab.NODE = "aipve"
        remaining = list(statuses)
        lab.guest_status.side_effect = \
            lambda *_a: remaining.pop(0) if remaining else statuses[-1]
        return lab

    def test_attach_waits_for_a_stopped_guest_then_attaches(self) -> None:
        lab = self._lab("stopped", "stopped", "running")
        with mock.patch.object(lab_console, "TermSession",
                               return_value="session") as term, \
             mock.patch.object(lab_console.time, "sleep") as slept:
            session = lab_console._attach_term(
                lab, mock.Mock(), "qemu", 9001, wait=30, poll=0.01
            )
        self.assertEqual(session, "session")
        self.assertEqual(lab.guest_status.call_count, 3)
        self.assertEqual(term.call_count, 1, "attach only once the guest is up")
        self.assertEqual(slept.call_count, 2)

    def test_without_waiting_a_stopped_guest_is_a_clear_error(self) -> None:
        """It used to return a session that streamed 'VM 9001 not running' as
        if the guest had printed it, and exit 0."""
        lab = self._lab("stopped")
        with mock.patch.object(lab_console, "TermSession") as term:
            with self.assertRaisesRegex(RuntimeError, "is not running"):
                lab_console._attach_term(lab, mock.Mock(), "qemu", 9001)
        term.assert_not_called()

    def test_a_running_guest_is_attached_without_waiting(self) -> None:
        lab = self._lab("running")
        with mock.patch.object(lab_console, "TermSession",
                               return_value="session"), \
             mock.patch.object(lab_console.time, "sleep") as slept:
            self.assertEqual(
                lab_console._attach_term(lab, mock.Mock(), "qemu", 9001,
                                         wait=30),
                "session",
            )
        slept.assert_not_called()

    def test_the_wait_is_bounded(self) -> None:
        lab = self._lab("stopped")
        with mock.patch.object(lab_console, "TermSession"), \
             mock.patch.object(lab_console.time, "sleep"):
            with self.assertRaisesRegex(
                RuntimeError, "did not become available within"
            ):
                lab_console._attach_term(
                    lab, mock.Mock(), "qemu", 9001, wait=0.02, poll=0.01
                )

    def test_a_real_configuration_error_is_not_retried(self) -> None:
        """Waiting is for 'not yet', not for a guest with no serial device."""
        lab = self._lab("running")
        with mock.patch.object(
            lab_console, "TermSession",
            side_effect=RuntimeError(
                "termproxy did not return a ticket for qemu/9001"
            ),
        ) as term:
            with self.assertRaisesRegex(RuntimeError, "termproxy"):
                lab_console._attach_term(
                    lab, mock.Mock(), "qemu", 9001, wait=5, poll=0.01
                )
        self.assertEqual(term.call_count, 1)

    def test_an_unreadable_status_counts_as_not_running(self) -> None:
        lab = mock.Mock()
        lab.LabError = RuntimeError
        lab.guest_status.side_effect = RuntimeError("HTTP 500")
        with mock.patch.object(lab_console, "TermSession") as term:
            with self.assertRaisesRegex(RuntimeError, "is not running"):
                lab_console._attach_term(lab, mock.Mock(), "qemu", 9001)
        term.assert_not_called()


class ConsoleTlsTests(unittest.TestCase):
    """Found live: the console websocket disabled certificate checks even with
    [proxmox] verify_tls = true, so only the REST path was protected."""

    def _open(self, verify: bool) -> tuple[object, dict]:
        import ssl as ssl_module

        context = ssl_module.create_default_context()
        wrapped = mock.Mock()
        recorded: dict = {}

        def wrap_socket(_raw: object, **kwargs: object) -> object:
            recorded.update(kwargs)
            return wrapped

        context.wrap_socket = wrap_socket        # type: ignore[method-assign]
        wrapped.recv.side_effect = [
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Sec-WebSocket-Protocol: binary\r\n\r\n"
        ]
        from proxmox_agent_lab import ws as lab_ws

        with mock.patch.object(lab_ws.ssl, "create_default_context",
                               return_value=context), \
             mock.patch.object(lab_ws.socket, "create_connection",
                               return_value=mock.Mock()):
            lab_ws.WebSocket(
                "pve.example", 8006, "/api2/json/x", {}, {},
                verify_tls=verify,
            )
        return context, recorded

    def test_verified_mode_checks_the_certificate_and_hostname(self) -> None:
        import ssl as ssl_module

        context, recorded = self._open(True)
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl_module.CERT_REQUIRED)
        self.assertEqual(recorded.get("server_hostname"), "pve.example")

    def test_the_self_signed_opt_out_is_still_available(self) -> None:
        import ssl as ssl_module

        context, recorded = self._open(False)
        self.assertFalse(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl_module.CERT_NONE)
        self.assertIsNone(recorded.get("server_hostname"))

    def test_the_console_passes_the_configured_policy_through(self) -> None:
        lab = mock.Mock()
        lab.HOST, lab.PORT, lab.NODE = "pve.example", 8006, "aipve"
        lab.TOKEN_USER, lab.TOKEN_NAME = "agent@pve", "lab"
        lab.keychain_secret.return_value = "secret"
        for verify in (True, False):
            lab.VERIFY_TLS = verify
            with mock.patch.object(lab_console.ws, "WebSocket") as socket_class:
                lab_console._open_websocket(
                    lab, "qemu", 9001, {"port": "5900", "ticket": "t"}, 20.0
                )
            self.assertEqual(
                socket_class.call_args.kwargs["verify_tls"], verify
            )


class MonitorScreenshotTests(unittest.TestCase):
    """'console screenshot --via monitor' writes a file on the *host*, so the
    path, the format and the cleanup are all fixed by the code, not the
    caller."""

    def _args(self, **overrides: object) -> object:
        import argparse

        defaults = dict(vmid=9001, lease="20260821120000-abc0", via="monitor",
                        out=None, ocr=False, upload=False, timeout=25.0,
                        url_expiry=3600, settle=0.0)
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def test_the_only_monitor_command_is_a_png_screendump(self) -> None:
        path = lab_console._monitor_remote_path("20260821120000-abc0", 9001)
        command = lab_console._screendump_command(path)
        self.assertTrue(command.startswith("screendump "))
        self.assertTrue(command.endswith(" -f png"))
        self.assertIn(lab_console.MONITOR_SCREENSHOT_ROOT, command)

    def test_the_host_path_is_lease_scoped(self) -> None:
        first = lab_console._monitor_remote_path("lease-one", 9001)
        second = lab_console._monitor_remote_path("lease-two", 9001)
        self.assertIn("/lease-one/", first)
        self.assertIn("/lease-two/", second)
        self.assertNotEqual(first, second)

    def test_a_lease_id_cannot_escape_the_screenshot_root(self) -> None:
        path = lab_console._monitor_remote_path("../../etc/x", 9001)
        self.assertTrue(
            path.startswith(lab_console.MONITOR_SCREENSHOT_ROOT + "/")
        )
        self.assertNotIn("..", path)

    def test_paths_and_formats_outside_the_contract_are_refused(self) -> None:
        for candidate in (
            "/etc/shadow.png",
            f"{lab_console.MONITOR_SCREENSHOT_ROOT}/x/shot.ppm",
            f"{lab_console.MONITOR_SCREENSHOT_ROOT}/../shot.png",
            f"{lab_console.MONITOR_SCREENSHOT_ROOT}/x/two words.png",
        ):
            with self.assertRaises(ValueError):
                lab_console._screendump_command(candidate)

    def test_non_png_bytes_from_the_host_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            lab_console._png_dimensions(b"not a png at all........")

    def _run(self, lab: mock.Mock, api: mock.Mock, memflow: mock.Mock,
             **overrides: object) -> dict:
        # Importing it first guarantees the package attribute exists, so the
        # lazy 'from . import memflow' inside the command sees the double.
        from proxmox_agent_lab import memflow as _real   # noqa: F401

        with mock.patch("proxmox_agent_lab.memflow", memflow):
            return lab_console._screenshot_via_monitor(
                lab, api, self._args(**overrides)
            )

    def _memflow(self, png: bytes) -> mock.Mock:
        memflow = mock.Mock()
        memflow.host_read_bytes.return_value = png
        memflow.host_remove_file.return_value = True
        return memflow

    def _png(self) -> bytes:
        return lab_png.encode_png(2, 1, bytes([1, 2, 3, 4, 5, 6]))

    def test_the_capture_is_fetched_and_the_host_copy_deleted(self) -> None:
        lab = mock.Mock()
        lab.LabError = RuntimeError
        lab.NODE = "aipve"
        api = mock.Mock()
        api.call.return_value = ""
        memflow = self._memflow(self._png())
        with tempfile.TemporaryDirectory() as tmp:
            result = self._run(lab, api, memflow,
                               out=str(Path(tmp) / "shot.png"))
        self.assertEqual(result["source"], "monitor")
        self.assertEqual((result["width"], result["height"]), (2, 1))
        self.assertTrue(result["host_file_removed"])
        sent = api.call.call_args.args[2]["command"]
        self.assertTrue(sent.endswith(" -f png"))
        memflow.host_remove_file.assert_called_once()
        self.assertEqual(
            memflow.host_remove_file.call_args.args[1],
            memflow.host_read_bytes.call_args.args[1],
        )
        # Nothing of ours is left on the host, not even the directory.
        memflow.host_remove_empty_dir.assert_called_once()
        self.assertEqual(
            memflow.host_remove_empty_dir.call_args.args[1],
            memflow.host_mkdir.call_args.args[1],
        )
        audited = lab.audit.call_args.kwargs
        self.assertEqual(audited["source"], "monitor")
        self.assertNotIn("image", audited)

    def test_the_host_file_is_deleted_even_when_the_fetch_fails(self) -> None:
        lab = mock.Mock()
        lab.LabError = RuntimeError
        lab.NODE = "aipve"
        api = mock.Mock()
        api.call.return_value = ""
        memflow = self._memflow(b"")
        memflow.host_read_bytes.side_effect = RuntimeError("no such file")
        with self.assertRaisesRegex(RuntimeError, "no such file"):
            self._run(lab, api, memflow)
        memflow.host_remove_file.assert_called_once()

    def test_a_monitor_refusal_in_the_body_is_an_error(self) -> None:
        lab = mock.Mock()
        lab.LabError = RuntimeError
        lab.NODE = "aipve"
        api = mock.Mock()
        api.call.return_value = (
            "Currently only 'png' and 'ppm' formats are supported."
        )
        memflow = self._memflow(self._png())
        with self.assertRaisesRegex(RuntimeError, "screendump refused"):
            self._run(lab, api, memflow)
        memflow.host_remove_file.assert_called_once()

    def test_it_requires_a_lease_and_ownership_before_touching_the_host(self) -> None:
        lab = mock.Mock()
        lab.LabError = RuntimeError
        memflow = self._memflow(self._png())
        with self.assertRaisesRegex(RuntimeError, "requires --lease"):
            self._run(lab, mock.Mock(), memflow, lease=None)
        memflow.require_host_ssh.assert_not_called()

        lab = mock.Mock()
        lab.LabError = RuntimeError
        lab.require_lease_resource.side_effect = RuntimeError("not registered")
        with self.assertRaisesRegex(RuntimeError, "not registered"):
            self._run(lab, mock.Mock(), memflow)
        memflow.require_host_ssh.assert_not_called()

    def test_ocr_is_refused_because_there_is_no_framebuffer(self) -> None:
        lab = mock.Mock()
        lab.LabError = RuntimeError
        with self.assertRaisesRegex(RuntimeError, "--ocr"):
            self._run(lab, mock.Mock(), self._memflow(self._png()), ocr=True)


class StorageGarbageCollectionTests(unittest.TestCase):
    """Deleting a disk image is irreversible, so the only safe default is a
    report, and a false 'orphan' must be impossible rather than unlikely."""

    STORES = [
        {"storage": "local-lvm", "content": "images,rootdir"},
        {"storage": "usb-bulk", "content": "images,iso,backup"},
        {"storage": "local", "content": "iso,vztmpl"},
    ]
    VOLUMES = {
        "local-lvm": [
            {"volid": "local-lvm:vm-9001-disk-0", "vmid": 9001,
             "size": 34_359_738_368, "format": "raw"},
            {"volid": "local-lvm:vm-9001-state-before-update", "vmid": 9001,
             "size": 4_294_967_296, "format": "raw"},
        ],
        "usb-bulk": [
            {"volid": "usb-bulk:9002/vm-9002-disk-0.raw", "vmid": 9002,
             "size": 107_374_182_400, "format": "raw"},
            {"volid": "usb-bulk:9003/vm-9003-disk-0.raw", "vmid": 9003,
             "size": 10_737_418_240, "format": "raw"},
        ],
    }
    # 9001 is in use; 9002's config mentions its volume with options appended;
    # 9003 no longer exists at all.
    CONFIGS = {
        9001: {"scsi0": "local-lvm:vm-9001-disk-0,discard=on,size=32G"},
        9002: {"virtio0": "usb-bulk:9002/vm-9002-disk-0.raw,iothread=1,size=100G"},
    }
    # A snapshot's vmstate volume is listed as ordinary images content but
    # appears only in the snapshot's own config.
    SNAPSHOTS = {
        9001: [{"name": "before-update"}, {"name": "current"}],
        9002: [],
    }
    SNAPSHOT_CONFIGS = {
        (9001, "before-update"): {
            "vmstate": "local-lvm:vm-9001-state-before-update",
            "scsi0": "local-lvm:vm-9001-disk-0,discard=on,size=32G",
        },
    }

    def _lab(self) -> tuple:
        import re

        lab = mock.Mock()
        lab.LabError = RuntimeError
        lab.NODE = "aipve"
        api = mock.Mock()

        def call(method: str, path: str, data: Any = None) -> Any:
            if path == "/nodes/aipve/storage":
                return self.STORES
            if path == "/cluster/resources":
                return [{"vmid": vmid, "type": "qemu"} for vmid in self.CONFIGS]
            matched = re.fullmatch(r"/nodes/aipve/qemu/(\d+)/config", path)
            if matched:
                return self.CONFIGS[int(matched.group(1))]
            matched = re.fullmatch(r"/nodes/aipve/qemu/(\d+)/snapshot", path)
            if matched:
                return self.SNAPSHOTS[int(matched.group(1))]
            matched = re.fullmatch(
                r"/nodes/aipve/qemu/(\d+)/snapshot/([^/]+)/config", path
            )
            if matched:
                return self.SNAPSHOT_CONFIGS[
                    (int(matched.group(1)), matched.group(2))
                ]
            matched = re.fullmatch(
                r"/nodes/aipve/storage/([^/]+)/content", path
            )
            if matched:
                return self.VOLUMES.get(matched.group(1), [])
            if method == "DELETE":
                return None
            raise AssertionError(f"unexpected call {method} {path}")

        api.call.side_effect = call
        lab.ProxmoxAPI.return_value = api
        return lab, api

    def _args(self, **overrides: Any) -> Any:
        import argparse

        base = dict(storage=None, vmid=None, dry_run=False, delete=False,
                    host_change_authorized=False, lease=None)
        base.update(overrides)
        return argparse.Namespace(**base)

    def _run(self, **overrides: Any) -> tuple:
        import contextlib
        import io

        lab, api = self._lab()
        out = io.StringIO()
        error = None
        with contextlib.redirect_stdout(out):
            try:
                lab_storage.cmd_gc(lab, self._args(**overrides))
            except RuntimeError as exc:
                error = str(exc)
        return json.loads(out.getvalue()), error, lab, api

    def test_only_unreferenced_volumes_are_reported(self) -> None:
        result, error, _, _ = self._run()
        self.assertIsNone(error)
        self.assertEqual(
            [x["volid"] for x in result["orphaned_volumes"]],
            ["usb-bulk:9003/vm-9003-disk-0.raw"],
        )
        self.assertEqual(result["referenced_volumes"], 3)
        self.assertEqual(result["orphaned_gb"], 10.74)

    def test_a_volume_named_in_a_config_with_options_is_referenced(self) -> None:
        """The config value is 'volid,iothread=1,size=100G', never a bare
        volid, so an exact-match check would have called it an orphan."""
        result, _, _, _ = self._run()
        self.assertNotIn(
            "usb-bulk:9002/vm-9002-disk-0.raw",
            [x["volid"] for x in result["orphaned_volumes"]],
        )

    def test_a_snapshot_state_volume_is_never_an_orphan(self) -> None:
        """It is listed as ordinary images content but appears only in the
        snapshot's own config, so a live-config-only scan would have offered
        to delete the thing a rollback needs."""
        result, _, _, _ = self._run()
        self.assertNotIn(
            "local-lvm:vm-9001-state-before-update",
            [x["volid"] for x in result["orphaned_volumes"]],
        )

    def test_an_unreadable_snapshot_list_also_refuses_to_classify(self) -> None:
        lab, api = self._lab()
        original = api.call.side_effect

        def call(method: str, path: str, data: Any = None) -> Any:
            if path.endswith("/9001/snapshot"):
                raise RuntimeError("HTTP 500")
            return original(method, path, data)

        api.call.side_effect = call
        with self.assertRaisesRegex(RuntimeError, "Refusing to classify"):
            lab_storage.cmd_gc(lab, self._args())

    def test_reporting_is_the_default_and_deletes_nothing(self) -> None:
        result, error, _, api = self._run()
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["deleted"], [])
        self.assertIsNone(error)
        self.assertFalse(
            [c for c in api.call.call_args_list if c.args[0] == "DELETE"]
        )

    def test_deleting_needs_host_change_authorization(self) -> None:
        result, error, _, api = self._run(delete=True)
        self.assertIn("host-change-authorized", error or "")
        self.assertEqual(result["deleted"], [])
        self.assertFalse(
            [c for c in api.call.call_args_list if c.args[0] == "DELETE"]
        )

    def test_authorized_deletion_removes_only_this_run_s_orphans(self) -> None:
        result, error, lab, api = self._run(
            delete=True, host_change_authorized=True
        )
        self.assertIsNone(error)
        self.assertEqual(result["deleted"], ["usb-bulk:9003/vm-9003-disk-0.raw"])
        deletes = [c.args[1] for c in api.call.call_args_list
                   if c.args[0] == "DELETE"]
        self.assertEqual(
            deletes,
            ["/nodes/aipve/storage/usb-bulk/content/"
             "usb-bulk:9003/vm-9003-disk-0.raw"],
        )
        audited = lab.audit.call_args
        self.assertEqual(audited.args[0], "storage-volume-deleted")
        self.assertEqual(audited.kwargs["volid"],
                         "usb-bulk:9003/vm-9003-disk-0.raw")
        self.assertEqual(audited.kwargs["size_gb"], 10.74)

    def test_an_unreadable_guest_config_refuses_to_classify_anything(self) -> None:
        """If one config cannot be read, a volume it references would look
        unreferenced. Nothing may be called an orphan in that case."""
        lab, api = self._lab()
        original = api.call.side_effect

        def call(method: str, path: str, data: Any = None) -> Any:
            if path.endswith("/9002/config"):
                raise RuntimeError("HTTP 500: permission denied")
            return original(method, path, data)

        api.call.side_effect = call
        with self.assertRaisesRegex(RuntimeError, "Refusing to classify"):
            lab_storage.cmd_gc(lab, self._args())

    def test_only_images_capable_stores_are_scanned(self) -> None:
        _, _, _, api = self._run()
        scanned = [
            c.args[1] for c in api.call.call_args_list
            if "/content" in c.args[1]
        ]
        self.assertNotIn("/nodes/aipve/storage/local/content", scanned)

    def test_the_vmid_filter_narrows_the_report(self) -> None:
        result, _, _, _ = self._run(vmid=9001)
        self.assertEqual(result["orphaned_volumes"], [])


class StorageClassTests(unittest.TestCase):
    def test_the_bulk_store_is_labelled_bulk(self) -> None:
        with mock.patch.object(lab_storage, "_DEFAULT_BULK", "usb-bulk"):
            self.assertEqual(lab_storage.storage_class("usb-bulk"), "bulk")
            self.assertEqual(lab_storage.storage_class("local-lvm"), "fast")
            self.assertEqual(lab_storage.storage_class(""), "fast")
