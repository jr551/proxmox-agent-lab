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
                target="OK", calibration_settle=1.0,
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
