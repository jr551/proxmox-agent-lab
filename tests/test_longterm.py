"""Long-term leases invert this tool's central promise -- the machine stays
on and the guests survive -- so each half of that inversion gets a test."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

os.environ["PROXMOX_AGENT_LAB_CONFIG"] = str(
    Path(__file__).parent / "fixtures" / "config.toml"
)

import json  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402
from unittest import mock  # noqa: E402

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from proxmox_agent_lab import cli as LAB  # noqa: E402
from proxmox_agent_lab import longterm  # noqa: E402


def make_lease(**overrides: object) -> dict:
    lease = {
        "id": "20260101000000-abcdef01",
        "kind": "session",
        "purpose": "test",
        "state": "active",
        "expires_at": "2026-01-01T02:00:00Z",
        "initial_vmids": [],
        "resources": [],
    }
    lease.update(overrides)
    return lease


class LeaseKindTests(unittest.TestCase):
    def test_session_leases_are_not_long_term(self) -> None:
        self.assertFalse(LAB.is_long_term(make_lease()))
        self.assertFalse(LAB.is_long_term({"id": "x"}))  # legacy, no kind

    def test_long_term_leases_never_expire(self) -> None:
        lease = make_lease(kind="long-term", expires_at=None)
        self.assertTrue(LAB.is_long_term(lease))
        self.assertIsNone(lease["expires_at"])


class HostStaysOnTests(unittest.TestCase):
    """The promise: while a long-term lease lives, nothing powers off."""

    def _leases(self, leases: list[dict]):
        return mock.patch.object(LAB, "active_leases",
                                 side_effect=lambda excluding=None: [
                                     x for x in leases if x["id"] != excluding
                                 ])

    def test_ending_a_session_lease_leaves_the_host_on(self) -> None:
        persistent = make_lease(id="20260101000000-11111111", kind="long-term", expires_at=None)
        session = make_lease(id="20260101000000-22222222")
        with tempfile.TemporaryDirectory() as tmp:
            old_root, LAB.LEASE_ROOT = LAB.LEASE_ROOT, Path(tmp)
            try:
                LAB.save_lease(session)
                api = mock.Mock()
                api.reachable.return_value = True
                with self._leases([persistent, session]), \
                     mock.patch.object(LAB, "ProxmoxAPI", return_value=api), \
                     mock.patch.object(LAB, "finalize_lease", return_value=[]), \
                     mock.patch.object(LAB, "shutdown_host") as shutdown, \
                     mock.patch.object(LAB, "audit"), \
                     mock.patch("builtins.print") as printed:
                    LAB.cmd_lease_end(mock.Mock(lease="20260101000000-22222222"))
            finally:
                LAB.LEASE_ROOT = old_root
        shutdown.assert_not_called()
        payload = json.loads(printed.call_args[0][0])
        self.assertTrue(payload["host_left_running"])
        self.assertFalse(payload["host_powered_off"])
        self.assertIn("20260101000000-11111111", payload["reason"])

    def test_ending_the_last_session_lease_still_powers_off(self) -> None:
        session = make_lease(id="20260101000000-22222222")
        with tempfile.TemporaryDirectory() as tmp:
            old_root, LAB.LEASE_ROOT = LAB.LEASE_ROOT, Path(tmp)
            try:
                LAB.save_lease(session)
                api = mock.Mock()
                api.reachable.return_value = True
                with self._leases([session]), \
                     mock.patch.object(LAB, "ProxmoxAPI", return_value=api), \
                     mock.patch.object(LAB, "finalize_lease", return_value=[]), \
                     mock.patch.object(LAB, "shutdown_host",
                                       return_value=True) as shutdown, \
                     mock.patch.object(LAB, "audit"), \
                     mock.patch("builtins.print") as printed:
                    LAB.cmd_lease_end(mock.Mock(lease="20260101000000-22222222"))
            finally:
                LAB.LEASE_ROOT = old_root
        shutdown.assert_called_once()
        self.assertTrue(json.loads(printed.call_args[0][0])["host_powered_off"])

    def test_lease_end_refuses_a_long_term_lease(self) -> None:
        lease = make_lease(id="20260101000000-11111111", kind="long-term", expires_at=None)
        with tempfile.TemporaryDirectory() as tmp:
            old_root, LAB.LEASE_ROOT = LAB.LEASE_ROOT, Path(tmp)
            try:
                LAB.save_lease(lease)
                with mock.patch.object(LAB, "ProxmoxAPI"):
                    with self.assertRaises(LAB.LabError) as caught:
                        LAB.cmd_lease_end(mock.Mock(lease="20260101000000-11111111"))
            finally:
                LAB.LEASE_ROOT = old_root
        self.assertIn("lease-destroy", str(caught.exception))


class BackupTests(unittest.TestCase):
    def test_due_calculation(self) -> None:
        now = dt.datetime(2026, 1, 10, tzinfo=dt.timezone.utc)
        self.assertTrue(longterm._due(None, now, 7), "never backed up")
        self.assertTrue(longterm._due("2026-01-01T00:00:00Z", now, 7))
        self.assertFalse(longterm._due("2026-01-08T00:00:00Z", now, 7))
        self.assertTrue(longterm._due("garbage", now, 7), "unparseable = due")

    def test_backup_uses_snapshot_mode_and_prunes(self) -> None:
        """Snapshot mode so a long-lived guest is not stopped to back it up."""
        lab = mock.Mock()
        lab.NODE = "testnode"
        lab.LabError = RuntimeError
        api = mock.Mock()
        api.call.return_value = "UPID:task"
        lab.wait_task.return_value = {"exitstatus": "OK"}
        lease = make_lease(kind="long-term",
                           resources=[{"kind": "qemu", "vmid": 9001}])
        results = longterm.backup_lease(lab, api, lease, storage="bulk",
                                        keep=2, timeout=60)
        self.assertTrue(results["9001"]["ok"])
        payload = api.call.call_args[0][2]
        self.assertEqual(payload["mode"], "snapshot")
        self.assertEqual(payload["storage"], "bulk")
        self.assertEqual(payload["prune-backups"], "keep-last=2")
        self.assertEqual(payload["remove"], 0)

    def test_backup_failure_is_reported_not_swallowed(self) -> None:
        lab = mock.Mock()
        lab.NODE = "testnode"
        lab.LabError = RuntimeError
        api = mock.Mock()
        api.call.side_effect = RuntimeError("storage full")
        lease = make_lease(resources=[{"kind": "qemu", "vmid": 9001}])
        results = longterm.backup_lease(lab, api, lease, storage="bulk",
                                        keep=2, timeout=60)
        self.assertFalse(results["9001"]["ok"])
        self.assertIn("storage full", results["9001"]["error"])

    def test_backup_storage_prefers_explicit_then_bulk(self) -> None:
        self.assertEqual(longterm.backup_storage(mock.Mock()), "bulk")


class DestroyTests(unittest.TestCase):
    def _lab(self, lease: dict):
        lab = mock.Mock()
        lab.NODE = "testnode"
        lab.LabError = RuntimeError
        lab.is_long_term = LAB.is_long_term
        lab.load_lease.return_value = lease
        lab.finalize_lease.return_value = []
        lab.active_leases.return_value = []
        lab.shutdown_host.return_value = True
        return lab

    def test_destroy_without_confirm_lists_what_would_be_lost(self) -> None:
        lease = make_lease(kind="long-term", expires_at=None, resources=[
            {"kind": "qemu", "vmid": 9001, "name": "buildbox"},
        ])
        lab = self._lab(lease)
        with self.assertRaises(RuntimeError) as caught:
            longterm.cmd_destroy(lab, mock.Mock(lease="20260101000000-11111111", confirm=False))
        message = str(caught.exception)
        self.assertIn("buildbox", message)
        self.assertIn("--confirm", message)

    def test_destroy_refuses_an_ordinary_lease(self) -> None:
        lab = self._lab(make_lease())
        with self.assertRaises(RuntimeError) as caught:
            longterm.cmd_destroy(lab, mock.Mock(lease="20260101000000-22222222", confirm=True))
        self.assertIn("lease-end", str(caught.exception))

    def test_destroy_lifts_protection_before_deleting(self) -> None:
        """Proxmox refuses to delete a protected guest, so order matters."""
        lease = make_lease(kind="long-term", expires_at=None, resources=[
            {"kind": "qemu", "vmid": 9001, "name": "buildbox"},
        ])
        lab = self._lab(lease)
        api = mock.Mock()
        api.reachable.return_value = True
        lab.ProxmoxAPI.return_value = api
        order: list[str] = []
        lab.finalize_lease.side_effect = lambda *a, **k: (
            order.append("delete") or []
        )
        with mock.patch.object(longterm, "set_protection",
                               side_effect=lambda *a, **k: order.append("unprotect")), \
             mock.patch("builtins.print"):
            longterm.cmd_destroy(lab, mock.Mock(lease="20260101000000-11111111", confirm=True))
        self.assertEqual(order, ["unprotect", "delete"])

    def test_destroy_powers_the_host_off_when_nothing_else_holds_it(self) -> None:
        lease = make_lease(kind="long-term", expires_at=None, resources=[])
        lab = self._lab(lease)
        api = mock.Mock()
        api.reachable.return_value = True
        lab.ProxmoxAPI.return_value = api
        with mock.patch("builtins.print") as printed:
            longterm.cmd_destroy(lab, mock.Mock(lease="20260101000000-11111111", confirm=True))
        lab.shutdown_host.assert_called_once()
        self.assertTrue(json.loads(printed.call_args[0][0])["host_powered_off"])

    def test_release_preserves_resources_and_closes_the_pin(self) -> None:
        lease = make_lease(kind="long-term", expires_at=None, resources=[
            {"kind": "qemu", "vmid": 9001, "name": "template",
             "policy": "retain"},
        ])
        lab = self._lab(lease)
        api = mock.Mock()
        api.reachable.return_value = True
        lab.ProxmoxAPI.return_value = api
        observed = {}
        def finalize(_api, value):
            observed.update(value)
            return []
        lab.finalize_lease.side_effect = finalize
        with mock.patch.object(longterm, "set_protection") as protection, \
             mock.patch("builtins.print") as printed:
            longterm.cmd_release(
                lab, mock.Mock(lease=lease["id"], confirm=True)
            )
        self.assertEqual(observed["kind"], "session")
        self.assertEqual(observed["resources"][0]["policy"], "retain")
        protection.assert_called_once_with(lab, api, "qemu", 9001, False)
        payload = json.loads(printed.call_args.args[0])
        self.assertEqual(payload["retained_guests"], ["qemu/9001 (template)"])
        self.assertTrue(payload["host_powered_off"])

    def test_release_without_confirm_is_non_mutating(self) -> None:
        lease = make_lease(kind="long-term", expires_at=None, resources=[
            {"kind": "qemu", "vmid": 9001, "name": "template"},
        ])
        lab = self._lab(lease)
        with self.assertRaises(RuntimeError) as caught:
            longterm.cmd_release(
                lab, mock.Mock(lease=lease["id"], confirm=False)
            )
        self.assertIn("--confirm", str(caught.exception))
        lab.finalize_lease.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class ExpiryTests(unittest.TestCase):
    def test_registering_a_guest_never_gives_a_long_term_lease_an_expiry(self) -> None:
        """Found live: register_resource set an expiry unconditionally, so a
        long-term lease silently acquired one the moment a guest joined it."""
        lease = make_lease(id="20260101000000-33333333", kind="long-term",
                           expires_at=None)
        with tempfile.TemporaryDirectory() as tmp:
            old_root, LAB.LEASE_ROOT = LAB.LEASE_ROOT, Path(tmp)
            try:
                LAB.register_resource(lease, "qemu", 9001, "retain", "box")
            finally:
                LAB.LEASE_ROOT = old_root
        self.assertIsNone(lease["expires_at"])
        self.assertEqual(lease["resources"][0]["policy"], "retain")

    def test_registering_a_guest_does_extend_an_ordinary_lease(self) -> None:
        lease = make_lease(id="20260101000000-44444444", expires_at=None)
        with tempfile.TemporaryDirectory() as tmp:
            old_root, LAB.LEASE_ROOT = LAB.LEASE_ROOT, Path(tmp)
            try:
                LAB.register_resource(lease, "qemu", 9001, "delete", "box")
            finally:
                LAB.LEASE_ROOT = old_root
        self.assertIsNotNone(lease["expires_at"])
