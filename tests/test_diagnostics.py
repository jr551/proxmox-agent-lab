"""Doctor's surviving checks, the store-backed journal command, host-update."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import bootstrap  # noqa: E402,F401
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from proxmox_agent_lab import cli as LAB  # noqa: E402
from proxmox_agent_lab import config as config_module  # noqa: E402
from proxmox_agent_lab import store as store_module  # noqa: E402


class DoctorReportTests(unittest.TestCase):
    """The checks that survive the ledger/spool removal still report, and the
    report no longer carries ledger, spool, or force-off keys."""

    def _doctor(self, **overrides: object) -> dict:
        # Hand-built args: these tests drive the command implementations, and
        # cli.parser() is transitional surface a later wave rewrites.
        values: dict[str, object] = {"host_checks": False}
        values.update(overrides)
        args = argparse.Namespace(**values)
        out = io.StringIO()
        # Without a stored API token the Proxmox blocks are skipped entirely:
        # no network, no API client, just the local install checks.
        with mock.patch.object(
            LAB.secrets_store, "get",
            side_effect=LAB.secrets_store.SecretError("not stored"),
        ), contextlib.redirect_stdout(out):
            try:
                LAB.cmd_doctor(args)
            except LAB.LabError:
                pass
        return json.loads(out.getvalue())

    def test_a_broken_config_is_reported_as_a_problem(self) -> None:
        with mock.patch.object(
            LAB, "CONFIG_ERROR", "synthetic parse failure"
        ):
            report = self._doctor()
        self.assertIn(
            "config could not be read: synthetic parse failure",
            report["problems"],
        )
        self.assertFalse(report["ok"])

    def test_the_state_dir_is_reported(self) -> None:
        report = self._doctor()
        self.assertEqual(report["state_dir"], str(LAB.STATE_ROOT))

    def test_ledger_spool_and_force_off_are_gone_from_the_report(self) -> None:
        report = self._doctor()
        self.assertNotIn("audit", report)
        self.assertNotIn("journal_dir", report)
        self.assertNotIn("ledger_reachable", report)
        self.assertNotIn("can_force_off", report["power"])


class JournalCommandTests(unittest.TestCase):
    """`proxmox-lab journal` reads the local store; the ledger flags are gone."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        patcher = mock.patch.object(
            config_module, "state_dir", return_value=self.root
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = store_module.Store(self.root / "lab.db")
        self.addCleanup(self.store.close)

    def _journal(self, **overrides: object) -> list[dict]:
        # Hand-built args: these tests drive the command implementation, and
        # cli.parser() is transitional surface a later wave rewrites.
        values: dict[str, object] = {
            "lease": None, "since": None, "limit": 50,
            "event": None, "controller": None,
            "summary": False, "flush_spool": False, "migrate": False,
            "migrations": False, "host_setup": False,
        }
        values.update(overrides)
        args = argparse.Namespace(**values)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            LAB.cmd_journal(args)
        return json.loads(out.getvalue())

    def test_the_output_is_the_store_rows_with_legacy_columns(self) -> None:
        self.store.record("guest-clone", lease="L1", vmid=7, data={"ok": True})
        rows = self._journal(lease="L1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            set(rows[0]), {"id", "timestamp", "event", "lease", "vmid", "data"}
        )
        self.assertEqual(rows[0]["event"], "guest-clone")
        self.assertEqual(rows[0]["lease"], "L1")
        self.assertEqual(rows[0]["vmid"], 7)
        self.assertEqual(json.loads(rows[0]["data"]), {"ok": True})

    def test_since_and_limit_filter_the_output(self) -> None:
        self.store.record("a", lease="L1", timestamp="2026-01-01T00:00:00Z")
        self.store.record("b", lease="L1", timestamp="2026-01-02T00:00:00Z")
        self.assertEqual(
            [row["event"] for row in
             self._journal(since="2026-01-02T00:00:00Z")],
            ["b"],
        )
        self.assertEqual(
            [row["event"] for row in self._journal(limit=1)], ["b"]
        )

    def test_the_ledger_flags_are_refused_loudly(self) -> None:
        for overrides in (
            {"flush_spool": True},
            {"migrate": True},
            {"migrations": True},
            {"summary": True},
            {"host_setup": True},
            {"event": "guest-clone"},
            {"controller": "pc-1"},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaisesRegex(LAB.LabError, "is gone"):
                    self._journal(**overrides)


class HostUpdateReportTests(unittest.TestCase):
    """Advisory only: the node needing patches is a thing to schedule between
    leases, not a reason for doctor to fail."""

    def _report(self, upgrade_stdout: str, reboot: str = "no",
                returncode: int = 0) -> dict:
        from proxmox_agent_lab import host_transport

        def host_run(_lab, argv, **_kwargs):
            command = argv[-1]
            if "apt-get" in command:
                return mock.Mock(returncode=returncode,
                                 stdout=upgrade_stdout, stderr="")
            return mock.Mock(returncode=0, stdout=reboot, stderr="")

        with mock.patch.object(host_transport, "host_ssh_enabled", return_value=True), \
             mock.patch.object(host_transport, "host_run", side_effect=host_run):
            return LAB.host_update_report()

    def test_pending_upgrades_are_counted(self) -> None:
        report = self._report(
            "Inst libexpat1 [2.6.2] (2.6.4 Debian:13/stable [amd64])\n"
            "Inst util-linux [2.40] (2.41 Debian:13/stable [amd64])\n"
            "Conf libexpat1 (2.6.4 Debian:13/stable [amd64])\n"
        )
        self.assertTrue(report["checked"])
        self.assertEqual(report["updates_pending"], 2, "Conf lines are not upgrades")
        self.assertFalse(report["security_updates"])
        self.assertFalse(report["reboot_required"])

    def test_a_security_origin_is_flagged(self) -> None:
        report = self._report(
            "Inst libexpat1 [2.6.2] (2.6.4 Debian-Security:13/stable [amd64])\n"
        )
        self.assertTrue(report["security_updates"])

    def test_a_pending_reboot_is_reported(self) -> None:
        report = self._report("Inst x [1] (2 Debian:13/stable [amd64])\n",
                              reboot="yes")
        self.assertTrue(report["reboot_required"])

    def test_a_failed_check_says_so_rather_than_reporting_zero(self) -> None:
        report = self._report("", returncode=100)
        self.assertFalse(report["checked"])
        self.assertNotIn("updates_pending", report)

    def test_without_the_ssh_channel_it_is_simply_not_checked(self) -> None:
        from proxmox_agent_lab import host_transport

        with mock.patch.object(host_transport, "host_ssh_enabled", return_value=False):
            report = LAB.host_update_report()
        self.assertFalse(report["checked"])
        self.assertIn("memflow", report["reason"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
