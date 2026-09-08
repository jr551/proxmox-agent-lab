"""Doctor reports, spool visibility, inventory judgement and host-update checks."""
from __future__ import annotations

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


class DoctorAuditTests(unittest.TestCase):
    """Found live: 1,547 events sat in the local spool and doctor said nothing.

    The ledger is unreachable whenever the lab host is off, which is most of
    the time, so a growing backlog is the thing worth reporting."""

    def _doctor(self, audit_overrides: dict, journal_root: Path) -> dict:
        import contextlib
        import io

        from proxmox_agent_lab import config as config_module

        args = LAB.parser().parse_args(["doctor"])
        stdout = io.StringIO()
        audit = config_module.Section(
            "audit", {**LAB.CONFIG.audit.as_dict(), **audit_overrides}
        )
        with mock.patch.object(LAB.CONFIG, "audit", audit), \
             mock.patch.object(LAB, "JOURNAL_ROOT", journal_root), \
             contextlib.redirect_stdout(stdout):
            try:
                LAB.cmd_doctor(args)
            except LAB.LabError:
                pass
        return json.loads(stdout.getvalue())

    def test_a_local_audit_spool_backlog_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            journal_root = Path(tmp) / "journal"
            journal_root.mkdir()
            (journal_root / "spool.jsonl").write_text(
                '{"event": "lease-begin"}\n{"event": "lease-end"}\n'
            )
            report = self._doctor({}, journal_root)
        self.assertEqual(report["audit"]["spooled_records"], 2)
        self.assertTrue(any("flush-spool" in problem
                            for problem in report["problems"]))

    def test_an_empty_spool_is_not_a_problem(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = self._doctor({}, Path(tmp) / "journal")
        self.assertEqual(report["audit"]["spooled_records"], 0)
        self.assertFalse(any("spool" in problem
                             for problem in report["problems"]))


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


class DoctorInventoryTests(unittest.TestCase):
    """A running guest nothing owns is the reason the node stayed on for five
    days, so doctor has to fail on it rather than mention it."""

    def _doctor(self, guests: list[dict], tmp: str) -> dict:
        import contextlib
        import io

        api = mock.Mock()
        api.reachable.return_value = True
        api.call.side_effect = lambda method, path, data=None: (
            guests if path == "/cluster/resources"
            else {"/vms": {name: 1 for name in (
                "VM.Allocate", "VM.Config.Disk", "VM.PowerMgmt",
                "VM.Console", "VM.Audit")}}
            if path == "/access/permissions" else {}
        )
        args = LAB.parser().parse_args(["doctor"])
        out = io.StringIO()
        old = (LAB.LEASE_ROOT, LAB.STATE_ROOT, LAB.JOURNAL_ROOT)
        LAB.LEASE_ROOT = Path(tmp) / "leases"
        LAB.STATE_ROOT = Path(tmp)
        LAB.JOURNAL_ROOT = Path(tmp) / "journal"
        try:
            with mock.patch.object(LAB, "ProxmoxAPI", return_value=api), \
                 mock.patch.object(LAB.secrets_store, "get",
                                   return_value="token"), \
                 contextlib.redirect_stdout(out):
                try:
                    LAB.cmd_doctor(args)
                except LAB.LabError:
                    pass
        finally:
            LAB.LEASE_ROOT, LAB.STATE_ROOT, LAB.JOURNAL_ROOT = old
        return json.loads(out.getvalue())

    def test_a_running_orphan_is_a_problem(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = self._doctor(
                [{"vmid": 9002, "type": "qemu", "status": "running",
                  "tags": "codex-lab;lease-20260814100000-gone"}],
                tmp,
            )
        self.assertEqual(report["guests"]["orphaned_running"], [9002])
        self.assertTrue(any("cannot power off" in problem
                            for problem in report["problems"]))
        self.assertFalse(report["ok"])

    def test_a_stopped_orphan_is_a_note_not_a_problem(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = self._doctor(
                [{"vmid": 9003, "type": "qemu", "status": "stopped",
                  "tags": "codex-lab;lease-20260814100000-gone"}],
                tmp,
            )
        self.assertEqual(report["guests"]["orphaned"], 1)
        self.assertEqual(report["guests"]["orphaned_running"], [])
        self.assertIn("note", report["guests"])
        self.assertFalse(any("cannot power off" in problem
                             for problem in report["problems"]))

    def test_an_untagged_guest_is_never_called_an_orphan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = self._doctor(
                [{"vmid": 100, "type": "qemu", "status": "running",
                  "name": "not-ours"}],
                tmp,
            )
        self.assertEqual(report["guests"]["orphaned"], 0)


class InfrastructureGuestTests(unittest.TestCase):
    """The audit ledger runs on the host and outlives every lease.

    Found live: the first lease-end after provisioning it refused to power the
    host off, naming the ledger container as an untracked running guest. Left
    alone that means the machine can never power itself off again, which is
    the entire point of it.
    """

    def _resources(self) -> list[dict]:
        return [
            {"vmid": 9310, "status": "running", "tags": "codex-lab-infra"},
            {"vmid": 9001, "status": "running", "tags": "codex-lab;lease-x"},
            {"vmid": 9002, "status": "stopped", "tags": ""},
        ]

    def test_the_ledger_container_is_not_an_untracked_guest(self) -> None:
        api = mock.Mock()
        api.call.return_value = self._resources()
        self.assertEqual(LAB.running_guest_vmids(api), [9001])

    def test_an_ordinary_running_guest_still_blocks_power_off(self) -> None:
        api = mock.Mock()
        api.call.return_value = [
            {"vmid": 9001, "status": "running", "tags": "codex-lab;lease-x"},
        ]
        self.assertEqual(LAB.running_guest_vmids(api), [9001])

    def test_comma_separated_tags_are_understood(self) -> None:
        """Proxmox has used both separators; the guard must not depend on it."""
        api = mock.Mock()
        api.call.return_value = [
            {"vmid": 9310, "status": "running", "tags": "codex-lab-infra,other"},
        ]
        self.assertEqual(LAB.running_guest_vmids(api), [])

    def test_a_guest_with_no_tags_is_still_counted(self) -> None:
        api = mock.Mock()
        api.call.return_value = [{"vmid": 9005, "status": "running"}]
        self.assertEqual(LAB.running_guest_vmids(api), [9005])


