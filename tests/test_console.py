"""Tests for the slim console surface: screendump PPM->PNG and key input.

Everything runs against the scripted ``FakeSSH`` seam via the ``_make_ssh``
patch and a real store registry under a temporary state root -- no network,
no real ssh, no host. Pinned here: the key table (shift glyphs and named
keys), `type` pacing and its never-audited text, the screenshot pipeline
(golden 2x2 P6 PPM -> PNG, fixed /tmp/pxl-* host temp), the LXC
"unsupported" path, and the ownership gate firing before any seam call.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path

import sys  # noqa: E402

# Shared bootstrap: fixture configuration plus a per-process state directory,
# applied before any proxmox_agent_lab import. `support` sits beside this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import bootstrap  # noqa: E402,F401
import tempfile  # noqa: E402
import time  # noqa: E402
import unittest  # noqa: E402
from unittest import mock  # noqa: E402

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from proxmox_agent_lab import config as config_module  # noqa: E402
from proxmox_agent_lab import console as console_module  # noqa: E402
from proxmox_agent_lab import png as png_module  # noqa: E402
from proxmox_agent_lab import store as store_module  # noqa: E402
from proxmox_agent_lab.errors import LabError  # noqa: E402
from support.fakessh import FakeSSH  # noqa: E402

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
GOLDEN_RGB = bytes(range(12))
#: QEMU screendump's format: binary P6. 2x2 pixels, exactly 12 raster bytes.
GOLDEN_PPM = b"P6\n2 2\n255\n" + GOLDEN_RGB


class _Lab:
    """The lab facade surface console handlers touch.

    CONFIG/STATE_ROOT as the real facade carries them; ``audit`` records
    instead of writing, so a test can assert exactly what was (not) logged.
    """

    def __init__(self, state_root: Path) -> None:
        self.STATE_ROOT = Path(state_root)
        self.CONFIG = config_module.get()
        self.audits: list[dict] = []

    def audit(self, event: str, **fields: object) -> None:
        self.audits.append({"event": event, **fields})


def _register_lease_guest(lab: _Lab, lease_id: str, kind: str,
                          vmid: int) -> None:
    """Seed the registry: one active lease owning one live guest."""
    with store_module.Store(Path(lab.STATE_ROOT) / "lab.db") as db:
        db.create_lease(lease_id, expires_at=int(time.time()) + 3600)
        db.register_resource(lease_id, kind, vmid)


class KeyTableTests(unittest.TestCase):
    """key_for: the pinned character table and key-name pass-through."""

    def test_single_characters_translate_through_the_table(self) -> None:
        cases = [
            ("a", "a"), ("z", "z"), ("A", "shift-a"), ("Z", "shift-z"),
            ("5", "5"), ("0", "0"),
            ("!", "shift-1"), ("@", "shift-2"), ("#", "shift-3"),
            ("$", "shift-4"), ("%", "shift-5"), ("^", "shift-6"),
            ("&", "shift-7"), ("*", "shift-8"), ("(", "shift-9"),
            (")", "shift-0"), ("_", "shift-minus"), ("+", "shift-equal"),
            (" ", "spc"), (".", "dot"), (",", "comma"), ("-", "minus"),
            ("=", "equal"), ("/", "slash"), (";", "semicolon"),
            ("'", "quote"), ("\\", "backslash"), ("\t", "tab"),
            ("\n", "ret"),
        ]
        for char, expected in cases:
            with self.subTest(char=char):
                self.assertEqual(console_module.key_for(char), [expected])

    def test_named_keys_and_combinations_pass_through(self) -> None:
        for token in ("ret", "f2", "spc", "tab", "ctrl-alt-delete",
                      "shift-a", "shift-minus"):
            with self.subTest(token=token):
                self.assertEqual(console_module.key_for(token), [token])

    def test_unknown_key_lists_the_accepted_names(self) -> None:
        for token in ("", "é", "banana", "ctrl-nope", "shift-é"):
            with self.subTest(token=token):
                with self.assertRaises(LabError) as caught:
                    console_module.key_for(token)
                message = str(caught.exception)
                self.assertIn("ret", message)
                self.assertIn("f2", message)
                self.assertIn("ctrl-alt-delete", message)


class PpmTests(unittest.TestCase):
    """ppm_to_png semantics: golden conversion, loud failure on junk."""

    def test_golden_ppm_round_trips_and_junk_raises(self) -> None:
        png = png_module.ppm_to_png(GOLDEN_PPM)
        self.assertTrue(png.startswith(PNG_MAGIC))
        self.assertEqual(png_module.decode_png(png), (2, 2, GOLDEN_RGB))
        with self.assertRaises(ValueError):
            png_module.ppm_to_png(b"not a ppm at all")


class _ConsoleCase(unittest.TestCase):
    """A temp state root, a scripted FakeSSH seam, recorded sleeps."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.lab = _Lab(self.root)
        self.fake = FakeSSH()
        self.sleeps: list[float] = []
        seam = mock.patch.object(
            console_module, "_make_ssh", lambda config: self.fake
        )
        seam.start()
        self.addCleanup(seam.stop)
        clock = mock.patch.object(
            console_module, "_sleep", self.sleeps.append
        )
        clock.start()
        self.addCleanup(clock.stop)

    def capture(self, function: object, args: argparse.Namespace) -> dict:
        """Run one handler; its JSON stdout must mirror what it returns."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            payload = function(self.lab, args)  # type: ignore[operator]
        printed = json.loads(out.getvalue())
        self.assertEqual(printed, payload)
        return printed


class ScreenshotTests(_ConsoleCase):
    def test_qemu_screenshot_writes_png_over_a_read_only_host_temp(self) -> None:
        self.fake.add(r"^qm status 101$", stdout=b"status: running\n")
        self.fake.add(r"^qm monitor 101$")
        self.fake.add(r"^cat /tmp/pxl-shot-101\.ppm$", stdout=GOLDEN_PPM)
        out_path = self.root / "shot.png"
        result = self.capture(
            console_module.cmd_screenshot,
            argparse.Namespace(vmid=101, out=str(out_path)),
        )
        png = out_path.read_bytes()
        self.assertTrue(png.startswith(PNG_MAGIC))
        self.assertEqual(
            result,
            {
                "vmid": 101, "path": str(out_path), "bytes": len(png),
                "width": 2, "height": 2,
            },
        )
        monitor = [
            call for call in self.fake.calls
            if call["argv"][:2] == ["qm", "monitor"]
        ]
        self.assertEqual(len(monitor), 1)
        self.assertEqual(
            monitor[0]["stdin"], b"screendump /tmp/pxl-shot-101.ppm\nquit\n"
        )
        cats = [
            call for call in self.fake.calls if call["argv"][0] == "cat"
        ]
        self.assertEqual(len(cats), 1)
        self.assertTrue(
            cats[0]["argv"][1].startswith("/tmp/pxl-"),
            f"host temp must live in /tmp/pxl-*, got {cats[0]['argv'][1]!r}",
        )
        # The fact and the size only, never the pixels.
        self.assertEqual(
            self.lab.audits,
            [{"event": "console-screenshot", "vmid": 101, "bytes": len(png)}],
        )

    def test_default_output_lands_in_the_state_screens_directory(self) -> None:
        self.fake.add(r"^qm status 101$", stdout=b"status: running\n")
        self.fake.add(r"^qm monitor 101$")
        self.fake.add(r"^cat /tmp/pxl-shot-101\.ppm$", stdout=GOLDEN_PPM)
        result = self.capture(
            console_module.cmd_screenshot,
            argparse.Namespace(vmid=101, out=None),
        )
        path = Path(result["path"])
        self.assertEqual(path.parent.name, "screens")
        self.assertEqual(path.parent.parent, self.root)
        self.assertTrue(path.name.startswith("pxl-shot-101-"))
        self.assertTrue(path.read_bytes().startswith(PNG_MAGIC))

    def test_lxc_screenshot_is_unsupported_and_never_touches_qm_monitor(
        self,
    ) -> None:
        self.fake.add(r"^pct status 102$", stdout=b"status: stopped\n")
        result = self.capture(
            console_module.cmd_screenshot,
            argparse.Namespace(vmid=102, out=None),
        )
        self.assertEqual(
            result,
            {
                "vmid": 102,
                "supported": False,
                "reason": "lxc guests have no qm monitor",
            },
        )
        for call in self.fake.calls:
            self.assertNotIn("qm monitor", " ".join(call["argv"]))
        self.assertFalse((self.root / "screens").exists())
        self.assertEqual(self.lab.audits, [])

    def test_bad_screendump_is_an_error_and_writes_nothing(self) -> None:
        self.fake.add(r"^qm status 101$", stdout=b"status: running\n")
        self.fake.add(r"^qm monitor 101$")
        self.fake.add(r"^cat /tmp/pxl-shot-101\.ppm$", stdout=b"junk")
        out_path = self.root / "nope.png"
        with self.assertRaises(LabError) as caught:
            console_module.cmd_screenshot(
                self.lab, argparse.Namespace(vmid=101, out=str(out_path))
            )
        self.assertIn("PPM", str(caught.exception))
        self.assertFalse(out_path.exists())


class TypeTests(_ConsoleCase):
    def setUp(self) -> None:
        super().setUp()
        _register_lease_guest(self.lab, "L1", "qemu", 101)
        self.fake.add(r"^qm sendkey 101")

    def type_args(self, **overrides: object) -> argparse.Namespace:
        fields: dict = {
            "lease": "L1", "vmid": 101, "text": "", "text_stdin": False,
            "enter": False, "chars_per_second": 20.0,
        }
        fields.update(overrides)
        return argparse.Namespace(**fields)

    def test_paces_between_sends_and_enter_appends_ret(self) -> None:
        result = self.capture(
            console_module.cmd_type,
            self.type_args(text="Hi!", enter=True, chars_per_second=1000.0),
        )
        sends = [call["argv"] for call in self.fake.calls]
        self.assertEqual(
            sends,
            [
                ["qm", "sendkey", "101", "shift-h"],
                ["qm", "sendkey", "101", "i"],
                ["qm", "sendkey", "101", "shift-1"],
                ["qm", "sendkey", "101", "ret"],
            ],
        )
        # Between every pair of sends, paced to at most 20 keys/second even
        # though 1000 was asked for.
        self.assertEqual(len(self.sleeps), len(sends) - 1)
        self.assertGreaterEqual(min(self.sleeps), 0.05)
        self.assertEqual(
            result,
            {"vmid": 101, "lease": "L1", "sent": 4, "chars": 3,
             "enter": True},
        )
        # The text is never audited -- it may be a password.
        self.assertNotIn("Hi!", json.dumps(self.lab.audits))
        self.assertEqual(
            self.lab.audits,
            [{"event": "console-type", "lease": "L1", "vmid": 101,
              "chars": 3}],
        )

    def test_unmappable_character_types_nothing(self) -> None:
        with self.assertRaises(LabError):
            console_module.cmd_type(self.lab, self.type_args(text="oké"))
        self.assertEqual(self.fake.calls, [])

    def test_text_stdin_reads_the_text(self) -> None:
        with mock.patch("sys.stdin", io.StringIO("ab")):
            self.capture(
                console_module.cmd_type,
                self.type_args(text_stdin=True),
            )
        self.assertEqual(
            [call["argv"] for call in self.fake.calls],
            [["qm", "sendkey", "101", "a"], ["qm", "sendkey", "101", "b"]],
        )
        self.assertEqual(len(self.sleeps), 1)

    def test_no_text_and_no_enter_is_refused(self) -> None:
        with self.assertRaises(LabError):
            console_module.cmd_type(self.lab, self.type_args(text=""))
        self.assertEqual(self.fake.calls, [])


class KeysTests(_ConsoleCase):
    def setUp(self) -> None:
        super().setUp()
        _register_lease_guest(self.lab, "L1", "qemu", 101)
        self.fake.add(r"^qm sendkey 101")

    def keys_args(self, keys: list, **overrides: object) -> argparse.Namespace:
        fields: dict = {
            "lease": "L1", "vmid": 101, "keys": keys, "screenshot_after": None,
        }
        fields.update(overrides)
        return argparse.Namespace(**fields)

    def test_each_name_is_its_own_sendkey_call(self) -> None:
        result = self.capture(
            console_module.cmd_keys,
            self.keys_args(["ret", "f2", "ctrl-alt-delete"]),
        )
        self.assertEqual(
            [call["argv"] for call in self.fake.calls],
            [
                ["qm", "sendkey", "101", "ret"],
                ["qm", "sendkey", "101", "f2"],
                ["qm", "sendkey", "101", "ctrl-alt-delete"],
            ],
        )
        self.assertEqual(len(self.sleeps), 2)
        self.assertEqual(
            result,
            {"vmid": 101, "lease": "L1", "sent_keys": 3, "ok": True},
        )
        # Key names can spell typed content: only the count is logged.
        self.assertEqual(
            self.lab.audits,
            [{"event": "console-keys", "lease": "L1", "vmid": 101,
              "count": 3}],
        )

    def test_character_tokens_translate_through_the_table(self) -> None:
        self.capture(
            console_module.cmd_keys, self.keys_args(["A", "!", "spc"])
        )
        self.assertEqual(
            [call["argv"] for call in self.fake.calls],
            [
                ["qm", "sendkey", "101", "shift-a"],
                ["qm", "sendkey", "101", "shift-1"],
                ["qm", "sendkey", "101", "spc"],
            ],
        )

    def test_unknown_key_name_lists_names_and_sends_nothing(self) -> None:
        with self.assertRaises(LabError) as caught:
            console_module.cmd_keys(self.lab, self.keys_args(["nope"]))
        message = str(caught.exception)
        self.assertIn("ret", message)
        self.assertIn("f2", message)
        self.assertIn("ctrl-alt-delete", message)
        self.assertEqual(self.fake.calls, [])

    def test_screenshot_after_captures_through_the_pipeline(self) -> None:
        self.fake.add(r"^qm monitor 101$")
        self.fake.add(r"^cat /tmp/pxl-shot-101\.ppm$", stdout=GOLDEN_PPM)
        result = self.capture(
            console_module.cmd_keys,
            self.keys_args(["ret"], screenshot_after=0.5),
        )
        self.assertEqual(self.sleeps, [0.5])
        shot = result["screenshot"]
        self.assertEqual(
            {key: shot[key] for key in ("vmid", "width", "height")},
            {"vmid": 101, "width": 2, "height": 2},
        )
        png = Path(shot["path"]).read_bytes()
        self.assertTrue(png.startswith(PNG_MAGIC))
        self.assertEqual(shot["bytes"], len(png))
        self.assertEqual(
            [event["event"] for event in self.lab.audits],
            ["console-keys", "console-screenshot"],
        )


class GateTests(_ConsoleCase):
    """The ownership gate refuses before the seam is ever built."""

    def assert_gate_refuses(self, function: object,
                            args: argparse.Namespace) -> str:
        seam = mock.Mock(
            side_effect=AssertionError("seam built before the ownership gate")
        )
        with mock.patch.object(console_module, "_make_ssh", seam):
            with self.assertRaises(LabError) as caught:
                function(self.lab, args)  # type: ignore[operator]
        seam.assert_not_called()
        self.assertEqual(self.fake.calls, [])
        return str(caught.exception)

    def test_keys_refuses_an_unregistered_guest(self) -> None:
        message = self.assert_gate_refuses(
            console_module.cmd_keys,
            argparse.Namespace(
                lease="L1", vmid=999, keys=["ret"], screenshot_after=None
            ),
        )
        self.assertIn("lease-register", message)

    def test_type_refuses_an_unregistered_guest(self) -> None:
        message = self.assert_gate_refuses(
            console_module.cmd_type,
            argparse.Namespace(
                lease="L1", vmid=999, text="x", text_stdin=False,
                enter=False, chars_per_second=20.0,
            ),
        )
        self.assertIn("lease-register", message)

    def test_keys_refuses_a_registered_container(self) -> None:
        _register_lease_guest(self.lab, "L1", "lxc", 102)
        message = self.assert_gate_refuses(
            console_module.cmd_keys,
            argparse.Namespace(
                lease="L1", vmid=102, keys=["ret"], screenshot_after=None
            ),
        )
        self.assertIn("lxc", message)


class RegisterTests(unittest.TestCase):
    """`console` registers the capture, keyboard and pointer surface."""

    #: Every console subcommand the surface promises. Pointer input, burst
    #: capture, grid overlay and calibration are ported from vnc-mcp
    #: (BSD 2-Clause, see NOTICE) and ride the same seam.
    EXPECTED = {
        "screenshot", "type", "keys", "move", "click", "drag",
        "calibrate", "grid", "burst",
    }

    @staticmethod
    def _subcommand_names(parser: argparse.ArgumentParser) -> set[str]:
        names: set[str] = set()
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                names |= set(action.choices)
        return names

    def test_exactly_the_expected_console_subcommands_exist(self) -> None:
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command", required=True)
        console_module.register(sub, object())
        self.assertEqual(
            self._subcommand_names(sub.choices["console"]), self.EXPECTED
        )
        for argv in (
            ["console", "screenshot", "--vmid", "1"],
            ["console", "type", "--lease", "L", "--vmid", "1", "--text", "x"],
            ["console", "keys", "--lease", "L", "--vmid", "1", "ret"],
            ["console", "move", "--lease", "L", "--vmid", "1",
             "--x", "5", "--y", "6"],
            ["console", "click", "--lease", "L", "--vmid", "1",
             "--x", "5", "--y", "6"],
            ["console", "drag", "--lease", "L", "--vmid", "1",
             "--x", "5", "--y", "6", "--to-x", "7", "--to-y", "8"],
            ["console", "calibrate", "--vmid", "1"],
            ["console", "grid", "--vmid", "1"],
            ["console", "burst", "--vmid", "1"],
        ):
            with self.subTest(argv=argv):
                args = parser.parse_args(argv)
                self.assertTrue(callable(args.func))

    def test_cut_console_subcommands_stay_gone(self) -> None:
        # Names from the pre-rework console stack. None may come back: each
        # needs a guest agent or a websocket server this project does not run.
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command", required=True)
        console_module.register(sub, object())
        for dead in ("inspect", "text", "bridge", "exec",
                     "screenshot-burst", "preflight", "share", "vision"):
            with self.subTest(dead=dead):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        parser.parse_args(["console", dead, "--vmid", "1"])


def _ppm(width: int, height: int) -> bytes:
    """A binary P6 framebuffer of exactly this size."""
    return f"P6\n{width} {height}\n255\n".encode() + bytes(width * height * 3)


def calibration_positions() -> list[tuple[str, float, float]]:
    """The marker grid `console calibrate --action start` lays down."""
    from proxmox_agent_lab import calibration

    return calibration.marker_positions(640, 480)


def cleanup_calibration(client_id: str, endpoint: str,
                        width: int, height: int) -> None:
    """Drop a saved calibration so one test cannot leak into the next."""
    from proxmox_agent_lab import calibration

    calibration.remove(client_id, endpoint, width, height)


class PointerTests(_ConsoleCase):
    """Lease-gated mouse input over HMP, and the coordinate guard.

    The pointer rides `qm monitor` (`mouse_set`/`mouse_move`/`mouse_button`),
    the only input transport that stays inside the ssh allowlist: QMP would
    need socat/nc/python3 on the host, and the seam refuses those on purpose.
    Pinned here: the tablet is selected before every gesture, a click is a
    press+release pair, a drag interpolates, an out-of-bounds or uncalibrated
    point is refused before any input, and an HMP rejection is never mistaken
    for a delivered click.
    """

    def setUp(self) -> None:
        super().setUp()
        _register_lease_guest(self.lab, "lease-a", "qemu", 101)
        self.fake.add(r"^qm status 101$", stdout=b"status: running\n")
        self.fake.add(r"^qm monitor 101$", stdout=b"")
        self.fake.add(r"^cat /tmp/pxl-shot-101\.ppm$", stdout=_ppm(640, 480))

    def monitor_scripts(self) -> list[str]:
        return [
            call["stdin"].decode()
            for call in self.fake.calls
            if call["argv"][:2] == ["qm", "monitor"]
        ]

    def input_scripts(self) -> list[str]:
        """Only the scripts that MOVE or PRESS something.

        Resolving a point legitimately takes one screendump to learn the
        framebuffer size, so "nothing at all reached the host" is the wrong
        assertion for a refusal. What must never happen is input.
        """
        return [s for s in self.monitor_scripts() if "mouse_" in s]

    def click_args(self, **overrides: object) -> argparse.Namespace:
        base: dict[str, object] = {
            "lease": "lease-a", "vmid": 101, "x": 100, "y": 200,
            "button": "left", "double": False, "space": "framebuffer",
            "client": None, "screenshot_after": None,
        }
        base.update(overrides)
        return argparse.Namespace(**base)  # type: ignore[arg-type]

    def test_click_selects_the_tablet_then_presses_and_releases(self) -> None:
        payload = self.capture(console_module.cmd_click, self.click_args())
        self.assertTrue(payload["clicked"])
        self.assertEqual((payload["x"], payload["y"]), (100, 200))
        self.assertEqual(payload["coordinates"], "framebuffer")
        scripts = self.monitor_scripts()
        self.assertTrue(any("mouse_set 3" in s for s in scripts))
        self.assertTrue(any("mouse_move 100 200" in s for s in scripts))
        self.assertTrue(any("mouse_button 1" in s for s in scripts))
        self.assertTrue(any("mouse_button 0" in s for s in scripts))
        # the audit records the pixel, never anything read off the screen
        self.assertEqual(
            self.lab.audits[-1],
            {"event": "console-click", "lease": "lease-a", "vmid": 101,
             "x": 100, "y": 200, "button": "left", "clicks": 1},
        )

    def test_double_click_is_two_complete_press_release_pairs(self) -> None:
        self.capture(console_module.cmd_click, self.click_args(double=True))
        scripts = self.monitor_scripts()
        self.assertEqual(sum("mouse_button 1" in s for s in scripts), 2)
        self.assertEqual(sum("mouse_button 0" in s for s in scripts), 2)

    def test_the_right_button_is_qemus_button_three(self) -> None:
        self.capture(
            console_module.cmd_click, self.click_args(button="right")
        )
        self.assertTrue(
            any("mouse_button 3" in s for s in self.monitor_scripts())
        )

    def test_move_never_clicks(self) -> None:
        payload = self.capture(
            console_module.cmd_move,
            argparse.Namespace(
                lease="lease-a", vmid=101, x=5, y=6,
                space="framebuffer", client=None,
            ),
        )
        self.assertFalse(payload["clicked"])
        self.assertFalse(
            any("mouse_button" in s for s in self.monitor_scripts())
        )

    def test_drag_interpolates_between_the_endpoints(self) -> None:
        self.capture(
            console_module.cmd_drag,
            argparse.Namespace(
                lease="lease-a", vmid=101, x=0, y=0, to_x=100, to_y=0,
                steps=4, space="framebuffer", client=None,
                screenshot_after=None,
            ),
        )
        moves = [s for s in self.monitor_scripts() if "mouse_move" in s]
        # one for the start point, then 4 interpolated hops
        self.assertEqual(len(moves), 5)
        self.assertIn("mouse_move 100 0", moves[-1])

    def test_a_point_outside_the_framebuffer_is_refused_before_any_input(
        self,
    ) -> None:
        with self.assertRaises(LabError) as caught:
            self.capture(console_module.cmd_click, self.click_args(x=9999))
        self.assertIn("640x480", str(caught.exception))
        self.assertEqual(self.input_scripts(), [])
        self.assertEqual(self.lab.audits, [])

    def test_a_monitor_rejection_is_not_a_delivered_click(self) -> None:
        # HMP is silent on success and prints "unknown command: ..." on
        # refusal, so exit status alone would report a dropped click as
        # a delivered one.
        self.fake._rules.clear()
        self.fake.add(r"^qm status 101$", stdout=b"status: running\n")
        self.fake.add(
            r"^qm monitor 101$",
            stdout=b"unknown command: 'mouse_button 1'\n",
        )
        self.fake.add(r"^cat /tmp/pxl-shot-101\.ppm$", stdout=_ppm(640, 480))
        with self.assertRaises(LabError) as caught:
            self.capture(console_module.cmd_click, self.click_args())
        self.assertIn("rejected", str(caught.exception))
        self.assertEqual(self.lab.audits, [])

    def test_pointer_input_refuses_an_unowned_guest(self) -> None:
        with self.assertRaises(LabError):
            self.capture(console_module.cmd_click, self.click_args(lease="lease-b"))
        self.assertEqual(self.input_scripts(), [])

    def test_image_space_without_a_calibration_is_refused(self) -> None:
        # Passing image-space numbers to the tablet as framebuffer pixels is
        # exactly the wrong-pixel click this guard exists to prevent.
        with self.assertRaises(LabError) as caught:
            self.capture(
                console_module.cmd_click,
                self.click_args(x=30, y=60, space="image", client="my-ide"),
            )
        self.assertIn("calibrat", str(caught.exception).lower())
        self.assertEqual(self.input_scripts(), [])

    def test_image_space_with_a_saved_calibration_maps_the_point(self) -> None:
        from proxmox_agent_lab import calibration

        calibration.upsert(
            calibration.Record(
                client_id="my-ide", client_name="My IDE", endpoint_id="101",
                width=640, height=480, x_a=2.0, x_b=0.0, y_a=2.0, y_b=0.0,
                rmse=0.1, rounds=1, created_at=1, updated_at=1,
            )
        )
        self.addCleanup(cleanup_calibration, "my-ide", "101", 640, 480)
        payload = self.capture(
            console_module.cmd_click,
            self.click_args(x=30, y=60, space="image", client="My IDE"),
        )
        self.assertEqual((payload["x"], payload["y"]), (60, 120))
        self.assertEqual(payload["coordinates"], "image->framebuffer")

    def test_a_poor_fit_still_refuses_the_click(self) -> None:
        # A saved calibration whose residual is too high means the client's
        # scaling changed; clicking off it is the failure being prevented.
        from proxmox_agent_lab import calibration

        calibration.upsert(
            calibration.Record(
                client_id="my-ide", client_name="My IDE", endpoint_id="101",
                width=640, height=480, x_a=2.0, x_b=0.0, y_a=2.0, y_b=0.0,
                rmse=99.0, rounds=1, created_at=1, updated_at=1,
            )
        )
        self.addCleanup(cleanup_calibration, "my-ide", "101", 640, 480)
        with self.assertRaises(LabError):
            self.capture(
                console_module.cmd_click,
                self.click_args(x=30, y=60, space="image", client="my-ide"),
            )
        self.assertEqual(self.input_scripts(), [])

    def test_drag_steps_are_bounded(self) -> None:
        with self.assertRaises(LabError):
            self.capture(
                console_module.cmd_drag,
                argparse.Namespace(
                    lease="lease-a", vmid=101, x=0, y=0, to_x=10, to_y=10,
                    steps=0, space="framebuffer", client=None,
                    screenshot_after=None,
                ),
            )


class CalibrateAndCaptureTests(_ConsoleCase):
    """`calibrate`, `grid` and `burst` over the same read-only seam."""

    def setUp(self) -> None:
        super().setUp()
        _register_lease_guest(self.lab, "lease-a", "qemu", 101)
        self.fake.add(r"^qm status 101$", stdout=b"status: running\n")
        self.fake.add(r"^qm monitor 101$", stdout=b"")
        self.fake.add(r"^cat /tmp/pxl-shot-101\.ppm$", stdout=_ppm(640, 480))

    def test_calibrate_start_marks_the_grid_without_touching_the_guest(
        self,
    ) -> None:
        payload = self.capture(
            console_module.cmd_calibrate,
            argparse.Namespace(
                vmid=101, action="start", samples=None, client="My IDE",
            ),
        )
        self.assertEqual(payload["framebuffer"], [640, 480])
        self.assertEqual(len(payload["markers"]), 9)
        self.assertTrue(Path(payload["screenshot"]["path"]).exists())
        # markers are burned into the returned image only: the guest was
        # never clicked, only screenshotted
        self.assertEqual(
            [a["event"] for a in self.lab.audits], ["console-screenshot"]
        )

    def test_calibrate_refuses_a_generic_client_identity(self) -> None:
        with self.assertRaises(LabError) as caught:
            self.capture(
                console_module.cmd_calibrate,
                argparse.Namespace(
                    vmid=101, action="start", samples=None, client="mcp",
                ),
            )
        self.assertIn("generic", str(caught.exception))

    def test_calibrate_refuses_too_few_readings(self) -> None:
        with self.assertRaises(LabError) as caught:
            self.capture(
                console_module.cmd_calibrate,
                argparse.Namespace(
                    vmid=101, action="commit",
                    samples='[{"id":"M1","x":1,"y":1}]', client="My IDE",
                ),
            )
        self.assertIn("at least two", str(caught.exception))

    def test_calibrate_commits_a_real_fit_and_says_it_is_saved(self) -> None:
        # A 0.5x client: report readings at half the marker's real position.
        samples = [
            {"id": mid, "x": round(fx / 2), "y": round(fy / 2)}
            for mid, fx, fy in calibration_positions()
        ]
        payload = self.capture(
            console_module.cmd_calibrate,
            argparse.Namespace(
                vmid=101, action="commit", samples=json.dumps(samples),
                client="My IDE",
            ),
        )
        self.assertTrue(payload["saved"])
        self.assertTrue(payload["trustworthy"])
        self.assertEqual(payload["markers_used"], 9)
        self.assertAlmostEqual(payload["x"]["a"], 2.0, places=1)
        self.addCleanup(cleanup_calibration, "my-ide", "101", 640, 480)

    def test_grid_keeps_the_untouched_capture_as_evidence(self) -> None:
        payload = self.capture(
            console_module.cmd_grid,
            argparse.Namespace(vmid=101, out=None, step=100),
        )
        self.assertTrue(Path(payload["path"]).exists())
        # the clean capture survives alongside the annotated one
        self.assertTrue(Path(payload["original"]).exists())
        self.assertNotEqual(payload["path"], payload["original"])

    def test_burst_stitches_several_frames_and_keeps_each_one(self) -> None:
        payload = self.capture(
            console_module.cmd_burst,
            argparse.Namespace(vmid=101, out=None, frames=3, interval=0.1),
        )
        self.assertEqual(payload["frames"], 3)
        self.assertEqual(len(payload["frame_paths"]), 3)
        self.assertTrue(Path(payload["path"]).exists())
        # frames are never scaled to match, so a stable guest tiles exactly
        self.assertEqual(payload["width"], 640 * 3 + 4 * 2)
        self.assertEqual(self.lab.audits[-1]["event"], "console-burst")
        self.assertEqual(self.sleeps, [0.1, 0.1])

    def test_burst_frame_count_is_bounded(self) -> None:
        for frames in (1, 31):
            with self.subTest(frames=frames):
                with self.assertRaises(LabError):
                    self.capture(
                        console_module.cmd_burst,
                        argparse.Namespace(
                            vmid=101, out=None, frames=frames, interval=0.5,
                        ),
                    )




if __name__ == "__main__":  # pragma: no cover
    unittest.main()
