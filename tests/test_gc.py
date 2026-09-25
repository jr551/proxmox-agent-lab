"""Tests for the controller-side GC command (``gc.py``) against FakeSSH.

Every test drives the REAL ``proxmox_agent_lab.gc`` -- handlers, core
functions and the ``register(sub, lab)`` wiring -- with the argv-recording
``FakeSSH`` from ``tests/support``. The full remote argv sequence of each
command is asserted exactly, element by element, which is also what proves
nothing composes a remote shell string: every call arrives as a list of
plain strings for the seam to quote.

Crontab contract under test (rework plan §F): one ``# pxl-gc`` marker line
above exactly one schedule line; re-running install never duplicates it;
uninstall strips it as a unit and leaves everything else in the crontab.
"""

from __future__ import annotations

from pathlib import Path
import sys  # noqa: E402

# Shared bootstrap: fixture configuration plus a per-process state directory,
# applied before any proxmox_agent_lab import. `support` sits beside this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import bootstrap  # noqa: E402,F401

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import argparse  # noqa: E402
import base64  # noqa: E402
import contextlib  # noqa: E402
import hashlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import types  # noqa: E402
import unittest  # noqa: E402

from proxmox_agent_lab import gc  # noqa: E402
from proxmox_agent_lab import ssh as ssh_module  # noqa: E402
from support.fakessh import FakeSSH  # noqa: E402

BUNDLED = gc.BUNDLED_SCRIPT.read_bytes()
BUNDLED_B64 = base64.b64encode(BUNDLED)
BUNDLED_SHA = hashlib.sha256(BUNDLED).hexdigest()
SCRIPT = gc.SCRIPT_INSTALL_PATH
STAGING = gc.SCRIPT_STAGING_PATH
BLOCK = gc.CRON_BLOCK.encode()


class GcTestCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeSSH()
        self.lab = types.SimpleNamespace(ssh=self.fake)

    # -- script/crontab scripting helpers --------------------------------

    def script_absent(self):
        self.fake.add(f"^base64 {SCRIPT}$", returncode=1, stderr=b"missing")

    def script_present(self, payload=BUNDLED_B64):
        self.fake.add(f"^base64 {SCRIPT}$", stdout=payload)

    def no_crontab(self):
        self.fake.add("^crontab -l$", returncode=1, stderr=b"no crontab for root")

    def crontab_is(self, text):
        self.fake.add("^crontab -l$", stdout=text.encode() if isinstance(text, str) else text)

    def host_ok(self, *patterns):
        for pattern in patterns:
            self.fake.add(pattern)

    # -- assertion helpers ------------------------------------------------

    def assert_argv_sequence(self, expected):
        """The exact ordered remote argv list -- nothing more, nothing less."""
        self.assertEqual([call["argv"] for call in self.fake.calls], expected)

    def run_cmd(self, fn, args):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            fn(self.lab, args)
        output = buffer.getvalue()
        self.assertTrue(output.endswith("\n"))
        return json.loads(output)


class InstallTests(GcTestCase):
    def test_install_writes_script_and_exactly_one_crontab_line(self):
        self.script_absent()
        self.host_ok(
            f"^tee {STAGING}$",
            f"^install -m 0755 {STAGING} {SCRIPT}$",
            f"^rm -f {STAGING}$",
            "^install -d -m 0755 /var/lib/pxl-gc$",
        )
        self.no_crontab()
        self.host_ok("^crontab -$")

        report = self.run_cmd(gc.cmd_install, argparse.Namespace(host_change_authorized=True))

        self.assertEqual(
            report,
            {"crontab": "added", "script": "written", "sha256": BUNDLED_SHA},
        )
        self.assert_argv_sequence(
            [
                ["base64", SCRIPT],
                ["tee", STAGING],
                ["install", "-m", "0755", STAGING, SCRIPT],
                ["rm", "-f", STAGING],
                ["install", "-d", "-m", "0755", "/var/lib/pxl-gc"],
                ["crontab", "-l"],
                ["crontab", "-"],
            ]
        )
        self.assertTrue(all(call["host_change"] for call in self.fake.calls))
        # The staged bytes ARE the bundled script; the crontab written IS the
        # §F block, with exactly one schedule line in it.
        self.assertEqual(self.fake.calls[1]["stdin"], BUNDLED)
        written_crontab = self.fake.calls[6]["stdin"].decode()
        self.assertEqual(written_crontab, gc.CRON_BLOCK)
        self.assertEqual(written_crontab.count(gc.CRON_LINE), 1)

    def test_second_install_is_idempotent(self):
        """Present-and-matching script plus an exact block: read-only run."""
        self.script_present()
        self.host_ok("^install -d -m 0755 /var/lib/pxl-gc$")
        self.crontab_is(gc.CRON_BLOCK)

        report = self.run_cmd(gc.cmd_install, argparse.Namespace(host_change_authorized=True))

        self.assertEqual(report["script"], "unchanged")
        self.assertEqual(report["crontab"], "unchanged")
        self.assert_argv_sequence(
            [
                ["base64", SCRIPT],
                ["install", "-d", "-m", "0755", "/var/lib/pxl-gc"],
                ["crontab", "-l"],
            ]
        )
        # No staging, no copy, no crontab write: nothing to duplicate.
        joined = [" ".join(call["argv"]) for call in self.fake.calls]
        self.assertNotIn(f"tee {STAGING}", joined)
        self.assertNotIn("crontab -", joined)

    def test_install_replaces_a_duplicated_block(self):
        """Two blocks in, exactly one block out -- the strip appends once."""
        self.script_present()
        self.host_ok("^install -d -m 0755 /var/lib/pxl-gc$", "^crontab -$")
        self.crontab_is(gc.CRON_BLOCK + gc.CRON_BLOCK)

        report = self.run_cmd(gc.cmd_install, argparse.Namespace(host_change_authorized=True))

        self.assertEqual(report["crontab"], "replaced")
        write = [c for c in self.fake.calls if c["argv"] == ["crontab", "-"]]
        self.assertEqual(len(write), 1)
        written = write[0]["stdin"].decode()
        self.assertEqual(written, gc.CRON_BLOCK)
        self.assertEqual(written.count(gc.CRON_LINE), 1)

    def test_install_keeps_unrelated_crontab_lines(self):
        """User lines survive the rewrite; the block is normalized to the end."""
        self.script_present()
        self.host_ok("^install -d -m 0755 /var/lib/pxl-gc$")
        self.crontab_is("# mine\n" + gc.CRON_BLOCK)

        report = self.run_cmd(gc.cmd_install, argparse.Namespace(host_change_authorized=True))

        # Block already present exactly once at the end: nothing to write.
        self.assertEqual(report["crontab"], "unchanged")
        self.assertNotIn(["crontab", "-"], [c["argv"] for c in self.fake.calls])

    def test_install_refused_without_host_change_flag(self):
        for args in (argparse.Namespace(), argparse.Namespace(host_change_authorized=False)):
            with self.subTest(args=args):
                self.fake.calls.clear()
                with self.assertRaises(ssh_module.PolicyError):
                    gc.cmd_install(self.lab, args)
                self.assertEqual(self.fake.calls, [])

    def test_install_core_gate_is_before_any_ssh_call(self):
        with self.assertRaises(ssh_module.PolicyError):
            gc.install(self.fake, host_change=False)
        self.assertEqual(self.fake.calls, [])


class StatusTests(GcTestCase):
    def test_status_reports_present_and_matching_without_any_flag(self):
        self.script_present()
        self.crontab_is("# mine\n" + gc.CRON_BLOCK)

        report = self.run_cmd(gc.cmd_status, argparse.Namespace())

        self.assertEqual(
            report,
            {
                "script": "present",
                "path": SCRIPT,
                "checksum": "match",
                "crontab": "present",
            },
        )
        self.assert_argv_sequence([["base64", SCRIPT], ["crontab", "-l"]])
        # Read-only: every call carries host_change=False, and the seam's own
        # policy accepts both argvs with no authorization at all.
        self.assertTrue(all(not call["host_change"] for call in self.fake.calls))
        ssh_module.check_allowed(["base64", SCRIPT])
        ssh_module.check_allowed(["crontab", "-l"])

    def test_status_reports_absent_everywhere(self):
        self.script_absent()
        self.no_crontab()

        report = self.run_cmd(gc.cmd_status, argparse.Namespace())

        self.assertEqual(report["script"], "absent")
        self.assertEqual(report["checksum"], "n/a")
        self.assertEqual(report["crontab"], "absent")

    def test_status_flags_a_drifted_script(self):
        self.script_present(payload=base64.b64encode(b"an older gc\n"))
        self.crontab_is(gc.CRON_BLOCK)

        report = self.run_cmd(gc.cmd_status, argparse.Namespace())

        self.assertEqual(report["script"], "present")
        self.assertEqual(report["checksum"], "differs")
        self.assertEqual(report["crontab"], "present")


class UninstallTests(GcTestCase):
    def test_uninstall_removes_line_then_script(self):
        self.crontab_is(gc.CRON_BLOCK + "# mine\n")
        self.script_present()
        self.host_ok(f"^rm -f {SCRIPT} {STAGING}$", "^crontab -$")

        report = self.run_cmd(gc.cmd_uninstall, argparse.Namespace(host_change_authorized=True))

        self.assertEqual(report, {"crontab": "removed", "script": "removed"})
        self.assert_argv_sequence(
            [
                ["crontab", "-l"],
                ["crontab", "-"],
                ["base64", SCRIPT],
                ["rm", "-f", SCRIPT, STAGING],
            ]
        )
        self.assertTrue(all(call["host_change"] for call in self.fake.calls))
        # Cron is disarmed before the script disappears, and only our lines
        # are stripped: the user's own crontab line is what gets written back.
        self.assertEqual(self.fake.calls[1]["stdin"], b"# mine\n")

    def test_second_uninstall_is_idempotent(self):
        self.crontab_is("# mine\n")
        self.script_absent()
        self.host_ok(f"^rm -f {SCRIPT} {STAGING}$")

        report = self.run_cmd(gc.cmd_uninstall, argparse.Namespace(host_change_authorized=True))

        self.assertEqual(report, {"crontab": "absent", "script": "absent"})
        self.assert_argv_sequence(
            [
                ["crontab", "-l"],
                ["base64", SCRIPT],
                ["rm", "-f", SCRIPT, STAGING],
            ]
        )
        # rm -f of an already-gone script is the idempotent no-op, not an error.
        self.assertEqual(report["script"], "absent")

    def test_uninstall_refused_without_host_change_flag(self):
        for args in (argparse.Namespace(), argparse.Namespace(host_change_authorized=False)):
            with self.subTest(args=args):
                self.fake.calls.clear()
                with self.assertRaises(ssh_module.PolicyError):
                    gc.cmd_uninstall(self.lab, args)
                self.assertEqual(self.fake.calls, [])

    def test_uninstall_core_gate_is_before_any_ssh_call(self):
        with self.assertRaises(ssh_module.PolicyError):
            gc.uninstall(self.fake, host_change=False)
        self.assertEqual(self.fake.calls, [])


class SeamPolicyTests(GcTestCase):
    """The allowlist extensions gc.py depends on, asserted where they live."""

    def test_crontab_read_is_flag_free_but_write_is_gated(self):
        ssh_module.check_allowed(["crontab", "-l"])
        with self.assertRaises(ssh_module.PolicyError):
            ssh_module.check_allowed(["crontab", "-"])
        ssh_module.check_allowed(["crontab", "-"], host_change=True)

    def test_tee_is_host_change_and_confined_to_pxl_temp(self):
        ssh_module.check_allowed(["tee", STAGING], host_change=True)
        with self.assertRaises(ssh_module.PolicyError):
            ssh_module.check_allowed(["tee", STAGING])
        with self.assertRaises(ssh_module.PolicyError):
            ssh_module.check_allowed(["tee", "/etc/passwd"], host_change=True)

    def test_rm_is_host_change_and_confined_to_pxl_namespaces(self):
        ssh_module.check_allowed(["rm", "-f", SCRIPT, STAGING], host_change=True)
        with self.assertRaises(ssh_module.PolicyError):
            ssh_module.check_allowed(["rm", "-f", SCRIPT], host_change=False)
        with self.assertRaises(ssh_module.PolicyError):
            ssh_module.check_allowed(["rm", "-rf", "/"], host_change=True)
        with self.assertRaises(ssh_module.PolicyError):
            ssh_module.check_allowed(
                ["rm", "-f", "/usr/local/sbin/other"], host_change=True
            )


class RegisterTests(GcTestCase):
    def build_parser(self):
        parser = argparse.ArgumentParser(prog="proxmox-lab")
        gc.register(parser.add_subparsers(dest="command"), self.lab)
        return parser

    def test_register_wires_the_three_subcommands(self):
        parser = self.build_parser()
        install_args = parser.parse_args(["gc", "install"])
        self.assertFalse(install_args.host_change_authorized)
        authorized = parser.parse_args(["gc", "install", "--host-change-authorized"])
        self.assertTrue(authorized.host_change_authorized)
        uninstall_args = parser.parse_args(["gc", "uninstall"])
        self.assertFalse(uninstall_args.host_change_authorized)
        self.assertTrue(
            parser.parse_args(
                ["gc", "uninstall", "--host-change-authorized"]
            ).host_change_authorized
        )
        status_args = parser.parse_args(["gc", "status"])
        self.assertFalse(hasattr(status_args, "host_change_authorized"))
        for args in (install_args, authorized, uninstall_args, status_args):
            self.assertTrue(callable(args.func))

    def test_parsed_install_without_flag_raises_before_any_ssh(self):
        parser = self.build_parser()
        args = parser.parse_args(["gc", "install"])
        with self.assertRaises(ssh_module.PolicyError):
            args.func(args)
        self.assertEqual(self.fake.calls, [])

    def test_parsed_status_runs_read_only(self):
        self.script_present()
        self.crontab_is(gc.CRON_BLOCK)
        parser = self.build_parser()
        args = parser.parse_args(["gc", "status"])
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            args.func(args)
        report = json.loads(buffer.getvalue())
        self.assertEqual(report["script"], "present")
        self.assertEqual(report["crontab"], "present")
        self.assertTrue(all(not c["host_change"] for c in self.fake.calls))


if __name__ == "__main__":
    unittest.main()
