"""Lease ownership, expiry and orphan reclamation against the lifecycle layer."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import bootstrap  # noqa: E402,F401
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from proxmox_agent_lab import cli as LAB  # noqa: E402


class LeaseOwnershipTests(unittest.TestCase):
    """Expiry cleanup stops and deletes guests, so ownership has to be settled
    before anything destructive happens."""

    def _lease(self, lease_id: str, *, state: str = "active",
               expires_in: int = 3600, vmid: int = 9001) -> dict:
        return {
            "id": lease_id,
            "state": state,
            "kind": "session",
            "created_at": LAB.iso_now(),
            "expires_at": LAB.new_expiry(expires_in),
            "initial_vmids": [],
            "resources": [{"kind": "qemu", "vmid": vmid, "policy": "delete",
                           "name": "reactos"}],
        }

    def _sweep(self, leases: list[dict], *, stop=None, delete=None,
               extra_args: list[str] | None = None) -> dict:
        """Run one cleanup-expired sweep over a temporary lease store."""
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as tmp:
            old = (LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT)
            LAB.LEASE_ROOT = Path(tmp) / "leases"
            LAB.LOCK_PATH = Path(tmp) / "controller.lock"
            LAB.STATE_ROOT = Path(tmp)
            try:
                for lease in leases:
                    LAB.save_lease(lease)
                api = mock.Mock()
                api.reachable.return_value = True
                args = LAB.parser().parse_args(
                    ["cleanup-expired", "--no-backup", *(extra_args or [])]
                )
                stdout = io.StringIO()
                with mock.patch.object(LAB, "ProxmoxAPI", return_value=api), \
                     mock.patch.object(LAB, "audit"), \
                     mock.patch.object(LAB, "stop_guest",
                                       side_effect=stop) as stopped, \
                     mock.patch.object(LAB, "delete_guest",
                                       side_effect=delete) as deleted, \
                     mock.patch.object(LAB, "shutdown_host",
                                       return_value=True), \
                     contextlib.redirect_stdout(stdout):
                    error = None
                    try:
                        LAB.cmd_cleanup_expired(args)
                    except LAB.LabError as exc:
                        error = str(exc)
                return {
                    "result": json.loads(stdout.getvalue()),
                    "error": error,
                    "stopped": stopped,
                    "deleted": deleted,
                    "leases": {
                        lease["id"]: json.loads(
                            LAB.lease_path(lease["id"]).read_text()
                        )
                        for lease in leases
                    },
                }
            finally:
                LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT = old

    def test_an_expired_lease_does_not_delete_a_live_lease_s_guest(self) -> None:
        """Found live: several 'active' records were already past their expiry
        and named the same VMID as the newest lease, whose guests were
        running. A sweep would have destroyed a VM in use."""
        expired = self._lease("20260821100000-old0", expires_in=-3600)
        live = self._lease("20260821120000-new0", expires_in=3600)
        run = self._sweep([expired, live])

        run["stopped"].assert_not_called()
        run["deleted"].assert_not_called()
        self.assertEqual(run["leases"][expired["id"]]["state"], "closed")
        self.assertEqual(
            run["leases"][expired["id"]]["transferred_resources"],
            ["qemu/9001"],
        )
        self.assertEqual(run["leases"][live["id"]]["state"], "active")
        self.assertEqual(
            run["result"]["left_to_another_lease"][expired["id"]],
            ["qemu/9001"],
        )

    def test_a_long_term_lease_also_protects_its_guest_from_a_sweep(self) -> None:
        expired = self._lease("20260821100000-old1", expires_in=-3600)
        persistent = self._lease("20260821110000-lt01", expires_in=-9999)
        persistent["kind"] = "long-term"
        persistent["expires_at"] = None
        persistent["resources"][0]["policy"] = "retain"
        run = self._sweep([expired, persistent])
        run["deleted"].assert_not_called()
        self.assertEqual(run["leases"][expired["id"]]["state"], "closed")

    def test_two_expired_leases_still_release_the_guest(self) -> None:
        """Deferring to another lease must not become a way for a guest to be
        cleaned up by nobody."""
        first = self._lease("20260821100000-old2", expires_in=-3600)
        second = self._lease("20260821101000-old3", expires_in=-3600)
        run = self._sweep([first, second])
        self.assertTrue(run["deleted"].called)
        self.assertEqual(run["leases"][first["id"]]["state"], "closed")
        self.assertEqual(run["leases"][second["id"]]["state"], "closed")

    def test_a_lease_still_cleans_up_a_guest_nobody_else_claims(self) -> None:
        expired = self._lease("20260821100000-old4", expires_in=-3600)
        other = self._lease("20260821120000-new1", expires_in=3600, vmid=9002)
        run = self._sweep([expired, other])
        self.assertEqual(run["deleted"].call_args.args[2], 9001)
        self.assertEqual(run["leases"][expired["id"]]["state"], "closed")

    def test_register_refuses_a_guest_a_live_lease_already_owns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old = (LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT)
            LAB.LEASE_ROOT = Path(tmp) / "leases"
            LAB.LOCK_PATH = Path(tmp) / "controller.lock"
            LAB.STATE_ROOT = Path(tmp)
            try:
                owner = self._lease("20260821120000-own0", expires_in=3600)
                LAB.save_lease(owner)
                newcomer = self._lease("20260821130000-new2", expires_in=3600)
                newcomer["resources"] = []
                LAB.save_lease(newcomer)
                args = LAB.parser().parse_args([
                    "lease-register", "--lease", newcomer["id"],
                    "--kind", "qemu", "--vmid", "9001",
                ])
                with mock.patch.object(LAB, "audit"):
                    with self.assertRaises(LAB.LabError) as caught:
                        LAB.cmd_lease_register(args)
                self.assertIn(owner["id"], str(caught.exception))
                stored = json.loads(
                    LAB.lease_path(newcomer["id"]).read_text()
                )
                self.assertEqual(stored["resources"], [])
            finally:
                LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT = old

    def test_an_expired_claim_does_not_block_registration(self) -> None:
        """A stale record must not make a VMID unusable for ever."""
        with tempfile.TemporaryDirectory() as tmp:
            old = (LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT)
            LAB.LEASE_ROOT = Path(tmp) / "leases"
            LAB.LOCK_PATH = Path(tmp) / "controller.lock"
            LAB.STATE_ROOT = Path(tmp)
            try:
                stale = self._lease("20260821100000-old5", expires_in=-3600)
                LAB.save_lease(stale)
                fresh = self._lease("20260821130000-new3", expires_in=3600)
                fresh["resources"] = []
                LAB.save_lease(fresh)
                args = LAB.parser().parse_args([
                    "lease-register", "--lease", fresh["id"],
                    "--kind", "qemu", "--vmid", "9001",
                ])
                import contextlib
                import io

                with mock.patch.object(LAB, "audit"), \
                     contextlib.redirect_stdout(io.StringIO()):
                    LAB.cmd_lease_register(args)
                stored = json.loads(LAB.lease_path(fresh["id"]).read_text())
                self.assertEqual(len(stored["resources"]), 1)
            finally:
                LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT = old

    def test_a_failed_cleanup_is_retried_by_the_next_sweep(self) -> None:
        """Found live: a QEMU lock timeout left the lease 'cleanup_failed', and
        every later sweep skipped it -- so its guests and the host stayed up
        until someone reran lease-end by hand."""
        lease = self._lease("20260821100000-lock0", expires_in=-3600)

        first = self._sweep(
            [lease], stop=LAB.LabError("VM is locked (clone)")
        )
        self.assertIn(lease["id"], first["result"]["failed"])
        self.assertEqual(
            first["leases"][lease["id"]]["state"], "cleanup_failed"
        )
        self.assertIsNotNone(first["error"])

        # The watchdog's next pass, with the lock gone.
        retried = dict(first["leases"][lease["id"]])
        second = self._sweep([retried])
        self.assertEqual(second["result"]["retried"], [lease["id"]])
        self.assertEqual(second["result"]["cleaned"], [lease["id"]])
        self.assertEqual(second["result"]["failed"], {})
        self.assertEqual(second["leases"][lease["id"]]["state"], "closed")
        self.assertTrue(second["deleted"].called)
        self.assertIsNone(second["error"])

    def test_a_failed_cleanup_is_retried_even_without_the_all_flag(self) -> None:
        lease = self._lease("20260821100000-lock1", expires_in=-3600)
        lease["state"] = "cleanup_failed"
        lease["failures"] = ["qemu/9001: VM is locked"]
        run = self._sweep([lease])
        self.assertEqual(run["result"]["retried"], [lease["id"]])
        self.assertEqual(run["leases"][lease["id"]]["state"], "closed")


class LeaseEndCrossReferenceTests(unittest.TestCase):
    """lease-end used to consult other leases only *after* finalize_lease had
    already destroyed this lease's guests, so a guest another active lease
    still registered could be deleted with nothing said in advance."""

    def _lease(self, lease_id: str, *, vmid: int = 9001,
               expires_in: int = 3600, kind: str = "session",
               policy: str = "delete", state: str = "active") -> dict:
        return {
            "id": lease_id,
            "state": state,
            "kind": kind,
            "created_at": LAB.iso_now(),
            "expires_at": None if kind == "long-term"
            else LAB.new_expiry(expires_in),
            "initial_vmids": [],
            "resources": [{"kind": "qemu", "vmid": vmid, "policy": policy,
                           "name": "ghidra-lab"}],
        }

    def _end(self, leases: list[dict], ending: str,
             *extra_args: str) -> dict:
        """Run one lease-end over a temporary lease store."""
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as tmp:
            old = (LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT)
            LAB.LEASE_ROOT = Path(tmp) / "leases"
            LAB.LOCK_PATH = Path(tmp) / "controller.lock"
            LAB.STATE_ROOT = Path(tmp)
            try:
                for lease in leases:
                    LAB.save_lease(lease)
                api = mock.Mock()
                api.reachable.return_value = True
                args = LAB.parser().parse_args(
                    ["lease-end", "--lease", ending, *extra_args]
                )
                stdout, stderr = io.StringIO(), io.StringIO()
                with mock.patch.object(LAB, "ProxmoxAPI", return_value=api), \
                     mock.patch.object(LAB, "ensure_on") as powered_on, \
                     mock.patch.object(LAB, "stop_guest") as stopped, \
                     mock.patch.object(LAB, "delete_guest") as deleted, \
                     mock.patch.object(LAB, "shutdown_host",
                                       return_value=True), \
                     mock.patch.object(LAB, "audit") as audited, \
                     contextlib.redirect_stdout(stdout), \
                     contextlib.redirect_stderr(stderr):
                    error = None
                    try:
                        LAB.cmd_lease_end(args)
                    except LAB.LabError as exc:
                        error = str(exc)
                payload = stdout.getvalue()
                return {
                    "result": json.loads(payload) if payload else None,
                    "error": error,
                    "stderr": stderr.getvalue(),
                    "stopped": stopped,
                    "deleted": deleted,
                    "powered_on": powered_on,
                    "audit": audited,
                    "leases": {
                        lease["id"]: json.loads(
                            LAB.lease_path(lease["id"]).read_text()
                        )
                        for lease in leases
                    },
                }
            finally:
                LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT = old

    def _events(self, audited: mock.Mock) -> list[str]:
        return [call.args[0] for call in audited.call_args_list]

    def test_a_guest_another_active_lease_registers_blocks_the_end(self) -> None:
        """Reachable: an idempotent 'memflow ghidra-setup --lxc N' re-run
        registers the same container under a second lease without going
        through lease-register's guard."""
        ending = self._lease("20260822100000-endme0")
        other = self._lease("20260822110000-other0")
        run = self._end([ending, other], ending["id"])

        self.assertIn("would destroy guest(s)", run["error"])
        self.assertIn("qemu/9001", run["error"])
        self.assertIn(other["id"], run["error"])
        self.assertIn("--shared-guests-authorized", run["error"])
        # The whole point: nothing was touched before the refusal.
        run["stopped"].assert_not_called()
        run["deleted"].assert_not_called()
        run["powered_on"].assert_not_called()
        self.assertEqual(run["leases"][ending["id"]]["state"], "active")
        self.assertIn("lease-end-refused-shared-guest",
                      self._events(run["audit"]))

    def test_the_refusal_also_covers_an_expired_but_active_lease(self) -> None:
        """An expired claim does not shield a guest from a *sweep*, but the
        lease record is one heartbeat from live again, so an operator ending
        another lease by hand must be told before the guest is destroyed."""
        ending = self._lease("20260822100000-endme1")
        stale = self._lease("20260822090000-stale1", expires_in=-3600)
        run = self._end([ending, stale], ending["id"])

        self.assertIn("expired but still active", run["error"])
        run["deleted"].assert_not_called()

    def test_a_long_term_lease_s_guest_is_named_in_the_refusal(self) -> None:
        ending = self._lease("20260822100000-endme2")
        persistent = self._lease("20260822080000-lt0002", kind="long-term",
                                 policy="retain")
        run = self._end([ending, persistent], ending["id"])

        self.assertIn(persistent["id"], run["error"])
        run["deleted"].assert_not_called()

    def test_the_override_flag_proceeds_and_reports_loudly(self) -> None:
        ending = self._lease("20260822100000-endme3")
        stale = self._lease("20260822090000-stale3", expires_in=-3600)
        run = self._end([ending, stale], ending["id"],
                        "--shared-guests-authorized")

        self.assertIsNone(run["error"])
        run["deleted"].assert_called_once()
        shared = run["result"]["shared_with_other_leases"]
        self.assertEqual(shared[0]["resource"], "qemu/9001")
        self.assertEqual(shared[0]["lease"], stale["id"])
        self.assertFalse(shared[0]["lease_live"])
        self.assertIn("--shared-guests-authorized", run["result"]["warning"])
        self.assertIn("warning: destroying guest(s)", run["stderr"])
        end_event = next(
            call for call in run["audit"].call_args_list
            if call.args[0] == "lease-end"
        )
        self.assertEqual(
            end_event.kwargs["shared_with_other_leases"][0]["lease"],
            stale["id"],
        )

    def test_the_override_still_defers_to_a_live_lease_inside_finalize(self) -> None:
        """The override lets the command run; it does not disable the
        per-resource check finalize_lease already makes."""
        ending = self._lease("20260822100000-endme4")
        live = self._lease("20260822110000-live04")
        run = self._end([ending, live], ending["id"],
                        "--shared-guests-authorized")

        run["deleted"].assert_not_called()
        self.assertEqual(run["result"]["left_to_another_lease"], ["qemu/9001"])

    def test_a_retained_resource_keeps_todays_behaviour(self) -> None:
        """This lease never deletes a retained guest, so sharing one is not a
        destroy hazard and must not start refusing."""
        ending = self._lease("20260822100000-endme5", policy="retain")
        stale = self._lease("20260822090000-stale5", expires_in=-3600)
        run = self._end([ending, stale], ending["id"])

        self.assertIsNone(run["error"])
        self.assertNotIn("shared_with_other_leases", run["result"])
        run["deleted"].assert_not_called()
        run["stopped"].assert_called_once()

    def test_a_different_guest_in_another_lease_is_not_cross_referenced(self) -> None:
        ending = self._lease("20260822100000-endme6", vmid=9001)
        other = self._lease("20260822110000-other6", vmid=9002)
        run = self._end([ending, other], ending["id"])

        self.assertIsNone(run["error"])
        self.assertNotIn("shared_with_other_leases", run["result"])
        run["deleted"].assert_called_once()

    def test_the_ordinary_single_lease_path_is_unchanged(self) -> None:
        ending = self._lease("20260822100000-endme7")
        run = self._end([ending], ending["id"])

        self.assertIsNone(run["error"])
        self.assertEqual(run["result"]["failures"], [])
        self.assertTrue(run["result"]["host_powered_off"])
        self.assertNotIn("shared_with_other_leases", run["result"])
        run["deleted"].assert_called_once()

    def test_a_closed_lease_never_cross_references_anything(self) -> None:
        """Only 'active' records can be mid-run; a closed one is bookkeeping."""
        ending = self._lease("20260822100000-endme8")
        closed = self._lease("20260822090000-closed8", state="closed")
        run = self._end([ending, closed], ending["id"])

        self.assertIsNone(run["error"])
        run["deleted"].assert_called_once()


class OrphanReclamationTests(unittest.TestCase):
    """Found live: a guest whose lease record was gone ran for five days,
    invisible to every sweep, holding the host on because shutdown_host
    refuses while any guest runs."""

    def _guests(self, cpu: float = 0.00005) -> list[dict]:
        return [
            {"vmid": 9001, "type": "qemu", "status": "running", "cpu": 0.01,
             "tags": "codex-lab;lease-20260821120000-live", "name": "current"},
            {"vmid": 9002, "type": "qemu", "status": "running", "cpu": cpu,
             "mem": 1024, "diskwrite": 4096, "netin": 8192,
             "tags": "codex-lab;lease-20260814100000-gone", "name": "abandoned"},
            {"vmid": 9003, "type": "qemu", "status": "stopped",
             "tags": "codex-lab;lease-20260814100000-gone", "name": "cold"},
        ]

    def _state(self, tmp: str) -> tuple:
        old = (LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT)
        LAB.LEASE_ROOT = Path(tmp) / "leases"
        LAB.LOCK_PATH = Path(tmp) / "controller.lock"
        LAB.STATE_ROOT = Path(tmp)
        LAB.save_lease({
            "id": "20260821120000-live", "state": "active", "kind": "session",
            "created_at": LAB.iso_now(), "expires_at": LAB.new_expiry(3600),
            "initial_vmids": [],
            "resources": [{"kind": "qemu", "vmid": 9001, "policy": "delete"}],
        })
        return old

    def _api(self, *, tasks: dict | None = None,
             uptime: dict | None = None, cpu: float = 0.00005) -> mock.Mock:
        """`tasks` maps vmid -> task list, `uptime` maps vmid -> seconds."""
        tasks = tasks or {}
        uptime = uptime or {}

        def call(method: str, path: str, data: object = None) -> object:
            if path == "/cluster/resources":
                return self._guests(cpu=cpu)
            if path.endswith("/tasks"):
                vmid = int((data or {}).get("vmid", 0))
                return tasks.get(vmid, [])
            if path.endswith("/status/current"):
                vmid = int(path.split("/")[-3])
                return {"status": "running", "uptime": uptime.get(vmid, 999_999)}
            return None

        api = mock.Mock()
        api.reachable.return_value = True
        api.call.side_effect = call
        return api

    def test_a_pruned_lease_leaves_its_guest_detectable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old = self._state(tmp)
            try:
                orphans = LAB.orphaned_guests(self._api())
            finally:
                LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT = old
        self.assertEqual([x["vmid"] for x in orphans], [9002, 9003])

    def test_the_retained_registry_excludes_a_guest_from_reclamation(self) -> None:
        from proxmox_agent_lab import inventory as inventory_module

        with tempfile.TemporaryDirectory() as tmp:
            old = self._state(tmp)
            try:
                inventory_module.record(
                    Path(tmp), kind="qemu", vmid=9003, lease="20260814100000-gone",
                    now=LAB.iso_now(), purpose="template",
                )
                orphans = LAB.orphaned_guests(self._api())
            finally:
                LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT = old
        self.assertEqual([x["vmid"] for x in orphans], [9002])

    def test_reclamation_stops_and_never_deletes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old = self._state(tmp)
            try:
                api = self._api()
                with mock.patch.object(LAB, "stop_guest") as stopped, \
                     mock.patch.object(LAB, "delete_guest") as deleted, \
                     mock.patch.object(LAB, "audit") as audited:
                    result = LAB.reclaim_orphans(api)
            finally:
                LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT = old
        self.assertEqual(result["stopped"], [9002])
        self.assertEqual(result["already_stopped"], [9003])
        self.assertEqual(stopped.call_args.args[1:], ("qemu", 9002))
        deleted.assert_not_called()
        events = [call.args[0] for call in audited.call_args_list]
        self.assertIn("orphan-guest-stopped", events)

    def test_a_guest_something_is_still_driving_is_left_alone(self) -> None:
        """Found by running it: 'orphaned' means this controller has no record,
        not that nobody is using it. Another controller was taking a console
        screenshot of 9002 every 45 seconds through the same token, and the
        reclamation stopped it mid-run."""
        import time as _time

        with tempfile.TemporaryDirectory() as tmp:
            old = self._state(tmp)
            try:
                api = self._api(tasks={
                    9002: [{"type": "vncproxy",
                            "starttime": int(_time.time()) - 45}],
                })
                with mock.patch.object(LAB, "stop_guest") as stopped, \
                     mock.patch.object(LAB, "audit"):
                    result = LAB.reclaim_orphans(api)
            finally:
                LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT = old
        stopped.assert_not_called()
        self.assertEqual(result["stopped"], [])
        self.assertEqual(result["left_active"]["9002"]["signal"], "vncproxy")

    def test_a_guest_started_moments_ago_is_left_alone(self) -> None:
        """The task log can roll; a short uptime still says it is in use, and
        needs no clock agreement between controller and node."""
        with tempfile.TemporaryDirectory() as tmp:
            old = self._state(tmp)
            try:
                api = self._api(uptime={9002: 219})
                with mock.patch.object(LAB, "stop_guest") as stopped, \
                     mock.patch.object(LAB, "audit"):
                    result = LAB.reclaim_orphans(api)
            finally:
                LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT = old
        stopped.assert_not_called()
        self.assertEqual(
            result["left_active"]["9002"]["signal"], "started recently"
        )

    def test_a_guest_that_is_visibly_working_is_left_alone(self) -> None:
        """The blind spot the other two signals share: work *inside* a guest
        produces no Proxmox task and does not reset its uptime, so a long build
        in an unmanaged container looked idle to both."""
        with tempfile.TemporaryDirectory() as tmp:
            old = self._state(tmp)
            try:
                api = self._api(cpu=0.42)      # 42% -- unmistakably working
                with mock.patch.object(LAB, "stop_guest") as stopped, \
                     mock.patch.object(LAB, "audit"):
                    result = LAB.reclaim_orphans(api)
            finally:
                LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT = old
        stopped.assert_not_called()
        self.assertEqual(result["left_active"]["9002"]["signal"], "busy")
        self.assertEqual(result["left_active"]["9002"]["cpu_percent"], 42.0)

    def test_a_guest_merely_switched_on_is_still_reclaimed(self) -> None:
        """Measured on the node: an idle container sits near 0.005% CPU while a
        mostly-idle Debian guest sits near 1%, so the floor has to be well
        above background noise or nothing would ever be reclaimable."""
        with tempfile.TemporaryDirectory() as tmp:
            old = self._state(tmp)
            try:
                api = self._api(cpu=0.012)     # 1.2% -- background daemons
                with mock.patch.object(LAB, "stop_guest") as stopped, \
                     mock.patch.object(LAB, "audit"):
                    result = LAB.reclaim_orphans(api)
            finally:
                LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT = old
        self.assertEqual(result["stopped"], [9002])
        stopped.assert_called_once()

    def test_the_measured_load_is_reported_either_way(self) -> None:
        """So a reader can disagree with the threshold instead of trusting it."""
        import time as _time

        with tempfile.TemporaryDirectory() as tmp:
            old = self._state(tmp)
            try:
                api = self._api(tasks={
                    9002: [{"type": "vncproxy",
                            "starttime": int(_time.time()) - 30}],
                })
                with mock.patch.object(LAB, "stop_guest"), \
                     mock.patch.object(LAB, "audit") as audited:
                    result = LAB.reclaim_orphans(api)
            finally:
                LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT = old
        entry = result["left_active"]["9002"]
        self.assertEqual(entry["signal"], "vncproxy")
        self.assertEqual(entry["cpu_percent"], 0.005)
        self.assertEqual(entry["disk_written_bytes"], 4096)

    def test_our_own_stop_does_not_read_as_someone_using_it(self) -> None:
        """Otherwise the first reclamation would make every later run refuse."""
        import time as _time

        with tempfile.TemporaryDirectory() as tmp:
            old = self._state(tmp)
            try:
                api = self._api(tasks={
                    9002: [{"type": "qmshutdown",
                            "starttime": int(_time.time()) - 30},
                           {"type": "qmstop",
                            "starttime": int(_time.time()) - 20}],
                })
                with mock.patch.object(LAB, "stop_guest") as stopped, \
                     mock.patch.object(LAB, "audit"):
                    result = LAB.reclaim_orphans(api)
            finally:
                LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT = old
        self.assertEqual(result["stopped"], [9002])
        self.assertEqual(result["left_active"], {})
        stopped.assert_called_once()

    def test_an_unreadable_task_log_leaves_the_guest_running(self) -> None:
        """Not knowing must not resolve to stopping somebody's work."""
        with tempfile.TemporaryDirectory() as tmp:
            old = self._state(tmp)
            try:
                api = self._api()
                original = api.call.side_effect

                def call(method: str, path: str, data: object = None) -> object:
                    if path.endswith("/tasks"):
                        raise LAB.LabError("HTTP 403")
                    return original(method, path, data)

                api.call.side_effect = call
                with mock.patch.object(LAB, "stop_guest") as stopped, \
                     mock.patch.object(LAB, "audit"):
                    result = LAB.reclaim_orphans(api)
            finally:
                LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT = old
        stopped.assert_not_called()
        self.assertEqual(
            result["left_active"]["9002"]["signal"], "task log unreadable"
        )

    def test_include_active_stops_it_anyway(self) -> None:
        import time as _time

        with tempfile.TemporaryDirectory() as tmp:
            old = self._state(tmp)
            try:
                api = self._api(tasks={
                    9002: [{"type": "vncproxy",
                            "starttime": int(_time.time()) - 45}],
                })
                with mock.patch.object(LAB, "stop_guest") as stopped, \
                     mock.patch.object(LAB, "audit"):
                    result = LAB.reclaim_orphans(api, include_active=True)
            finally:
                LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT = old
        self.assertEqual(result["stopped"], [9002])
        stopped.assert_called_once()

    def test_a_stop_failure_is_reported_not_swallowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old = self._state(tmp)
            try:
                with mock.patch.object(LAB, "stop_guest",
                                       side_effect=LAB.LabError("locked")), \
                     mock.patch.object(LAB, "audit"):
                    result = LAB.reclaim_orphans(self._api())
            finally:
                LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT = old
        self.assertIn("9002", result["failed"])
        self.assertEqual(result["stopped"], [])

    def test_orphans_only_touches_no_lease_and_leaves_the_host_on(self) -> None:
        """Found while using it: reclamation was only reachable through a full
        sweep, which in the same run deletes every expired lease's guests and
        may power the host off. Wanting one is not consenting to the other."""
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as tmp:
            old = self._state(tmp)
            try:
                # An expired lease whose guest a full sweep would delete.
                LAB.save_lease({
                    "id": "20260814100000-expired", "state": "active",
                    "kind": "session", "created_at": LAB.iso_now(),
                    "expires_at": LAB.new_expiry(-3600), "initial_vmids": [],
                    "resources": [{"kind": "qemu", "vmid": 9001,
                                   "policy": "delete", "name": "keepme"}],
                })
                args = LAB.parser().parse_args([
                    "cleanup-expired", "--orphans-only",
                    "--host-change-authorized",
                ])
                out = io.StringIO()
                with mock.patch.object(LAB, "ProxmoxAPI",
                                       return_value=self._api()), \
                     mock.patch.object(LAB, "audit"), \
                     mock.patch.object(LAB, "stop_guest") as stopped, \
                     mock.patch.object(LAB, "delete_guest") as deleted, \
                     mock.patch.object(LAB, "finalize_lease") as finalized, \
                     mock.patch.object(LAB, "shutdown_host") as powered_off, \
                     contextlib.redirect_stdout(out):
                    LAB.cmd_cleanup_expired(args)
                result = json.loads(out.getvalue())
                lease = json.loads(
                    LAB.lease_path("20260814100000-expired").read_text()
                )
            finally:
                LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT = old
        self.assertEqual(result["reclaimed_orphans"]["stopped"], [9002])
        self.assertEqual(result["leases_swept"], [])
        self.assertFalse(result["host_powered_off"])
        # The expired lease and its guest are untouched.
        finalized.assert_not_called()
        deleted.assert_not_called()
        powered_off.assert_not_called()
        self.assertEqual(lease["state"], "active")
        self.assertEqual(stopped.call_args.args[1:], ("qemu", 9002))

    def test_orphans_only_still_requires_authorization(self) -> None:
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as tmp:
            old = self._state(tmp)
            try:
                args = LAB.parser().parse_args(
                    ["cleanup-expired", "--orphans-only"]
                )
                with mock.patch.object(LAB, "ProxmoxAPI",
                                       return_value=self._api()), \
                     mock.patch.object(LAB, "stop_guest") as stopped, \
                     contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(
                        LAB.LabError, "host-change-authorized"
                    ):
                        LAB.cmd_cleanup_expired(args)
                stopped.assert_not_called()
            finally:
                LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT = old

    def test_reclaiming_requires_explicit_authorization(self) -> None:
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as tmp:
            old = self._state(tmp)
            try:
                args = LAB.parser().parse_args(
                    ["cleanup-expired", "--no-backup", "--reclaim-orphans"]
                )
                with mock.patch.object(LAB, "ProxmoxAPI", return_value=self._api()), \
                     mock.patch.object(LAB, "audit"), \
                     mock.patch.object(LAB, "stop_guest") as stopped, \
                     mock.patch.object(LAB, "shutdown_host", return_value=False), \
                     contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(
                        LAB.LabError, "host-change-authorized"
                    ):
                        LAB.cmd_cleanup_expired(args)
                stopped.assert_not_called()
            finally:
                LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT = old

    def test_retain_registers_a_durable_owner(self) -> None:
        from proxmox_agent_lab import inventory as inventory_module

        with tempfile.TemporaryDirectory() as tmp:
            old = self._state(tmp)
            try:
                lease = LAB.load_lease("20260821120000-live")
                lease["purpose"] = "haiku template"
                with mock.patch.object(LAB, "audit"):
                    LAB.register_resource(lease, "qemu", 9077, "retain", "tpl")
                    LAB.register_resource(lease, "qemu", 9078, "delete", "tmp")
                entries = inventory_module.entries(Path(tmp))
            finally:
                LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT = old
        self.assertEqual(list(entries), ["qemu/9077"],
                         "only a retained guest outlives its lease")
        self.assertEqual(entries["qemu/9077"]["purpose"], "haiku template")

    def test_deleting_a_guest_clears_its_registry_entry(self) -> None:
        from proxmox_agent_lab import inventory as inventory_module

        with tempfile.TemporaryDirectory() as tmp:
            old = self._state(tmp)
            try:
                inventory_module.record(Path(tmp), kind="qemu", vmid=9077,
                                        lease="20260821120000-live",
                                        now=LAB.iso_now())
                api = mock.Mock()
                api.call.return_value = "UPID:x"
                with mock.patch.object(LAB, "wait_task", return_value={}):
                    LAB.delete_guest(api, "qemu", 9077)
                remaining = inventory_module.entries(Path(tmp))
            finally:
                LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT = old
        self.assertEqual(remaining, {})

    def test_the_sweep_lock_does_not_stack_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_state = LAB.STATE_ROOT
            LAB.STATE_ROOT = Path(tmp)
            try:
                with LAB.sweep_lock("retained-backup") as first:
                    self.assertTrue(first)
                    with LAB.sweep_lock("retained-backup") as second:
                        self.assertFalse(
                            second, "a second sweep must not start a second vzdump"
                        )
                with LAB.sweep_lock("retained-backup") as again:
                    self.assertTrue(again, "released when the first sweep ends")
            finally:
                LAB.STATE_ROOT = old_state


class StateIsolationTests(unittest.TestCase):
    def test_the_suite_never_points_at_real_controller_state(self) -> None:
        """Found the hard way: register_resource began writing a registry under
        STATE_ROOT, and one unisolated test put a bogus retained guest into the
        developer's live controller state -- where the backup sweep would then
        have picked it up."""
        self.assertEqual(
            str(LAB.STATE_ROOT), os.environ["PROXMOX_AGENT_LAB_STATE"]
        )
        # The danger is the *default* root, where the live controller state
        # actually lives -- not any path under the user profile: on Windows
        # the whole temp tree sits inside it, so "under home" would condemn
        # the suite's own isolation.
        saved = os.environ.pop("PROXMOX_AGENT_LAB_STATE", None)
        try:
            real_default = LAB.config_module.state_dir()
        finally:
            if saved is not None:
                os.environ["PROXMOX_AGENT_LAB_STATE"] = saved
        self.assertNotEqual(str(LAB.STATE_ROOT), str(real_default))


