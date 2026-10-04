"""Finalize-lease, cleanup-expired, shared-guest/ownership checks.

Covers the director's invariant list for the teardown side: the lease-end
shared-guest pre-check refuses BEFORE any stop/destroy (and audits the
refusal); ``cleanup_failed`` + ``last_error`` is recorded and RETRIED by the
next sweep; a guest another live lease owns is left (``left_to_another_lease``)
and only lease-owned disposable ones are destroyed; finalize is idempotent
(already-gone is success); `lease-destroy` refuses without ``--confirm`` and,
with it, lifts long-term protection (``pxl-expiry`` -> past) before finalizing;
and the end JSON reports ``host_powered_off`` truthfully.

Everything runs against a ``TemporaryDirectory`` state root and a fake seam
over ``FakeSSH``; ``power.shutdown_verified`` is substituted so no probe loop
can sleep. No test spawns a real ssh.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sys  # noqa: E402
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import bootstrap  # noqa: E402,F401

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from proxmox_agent_lab import cleanup  # noqa: E402
from proxmox_agent_lab import leases  # noqa: E402
from proxmox_agent_lab import store  # noqa: E402
from proxmox_agent_lab.errors import LabError  # noqa: E402
from proxmox_agent_lab.proxmox import ProxmoxError  # noqa: E402
from support.fakessh import FakeSSH  # noqa: E402


def fake_config() -> SimpleNamespace:
    return SimpleNamespace(
        ssh=SimpleNamespace(target="fixture-host"),
        pve=SimpleNamespace(node="pve", template_vmid=9000),
        lease=SimpleNamespace(ttl_seconds=7200, idle_shutdown_seconds=28800),
        power=SimpleNamespace(
            mac="aa:bb:cc:dd:ee:ff", broadcast="255.255.255.255", port=9
        ),
    )


class FakeSeam:
    """Records lifecycle calls; scripts statuses and teardown failures."""

    def __init__(self, *, reachable: bool = True) -> None:
        self.calls: list[tuple] = []
        self._ssh = FakeSSH()
        if reachable:
            self._ssh.add("true")
        self._ssh.add(r"pvesh get /cluster/resources", stdout=b"[]")
        self.statuses: dict[tuple[str, int], str] = {}
        self.gone: set[tuple[str, int]] = set()
        self.shutdown_will_stop = True
        self.fail_destroy: set[tuple[str, int]] = set()

    def set_metadata(self, kind, vmid, *, tags=None, description=None):
        self.calls.append(("set_metadata", kind, vmid, tags, description))

    def status(self, kind, vmid):
        self.calls.append(("status", kind, vmid))
        if (kind, vmid) in self.gone:
            raise ProxmoxError(
                f"qm status {vmid} failed: Configuration file does not exist"
            )
        return self.statuses.get((kind, vmid), "running")

    def shutdown(self, kind, vmid, *, timeout=None):
        self.calls.append(("shutdown", kind, vmid))
        return self.shutdown_will_stop

    def stop(self, kind, vmid):
        self.calls.append(("stop", kind, vmid))

    def destroy(self, kind, vmid, *, purge=True):
        self.calls.append(("destroy", kind, vmid))
        if (kind, vmid) in self.fail_destroy:
            raise ProxmoxError(f"qm destroy {vmid} failed: storage locked")

    def destructive(self) -> list[tuple]:
        return [
            call for call in self.calls
            if call[0] in ("shutdown", "stop", "destroy")
        ]


class FakeLab:
    def __init__(self, state_root: Path) -> None:
        self.STATE_ROOT = Path(state_root)
        self.CONFIG = fake_config()
        self.audits: list[tuple[str, dict]] = []

    def audit(self, event: str, **fields) -> None:
        self.audits.append((event, fields))


class CleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_root = Path(self.tmp.name)
        self.lease_root = self.state_root / "leases"
        self.lab = FakeLab(self.state_root)
        self.seam = FakeSeam()
        for module in (cleanup, leases):
            seam_patcher = mock.patch.object(
                module, "_make_proxmox", return_value=self.seam
            )
            seam_patcher.start()
            self.addCleanup(seam_patcher.stop)
        power_patcher = mock.patch.object(
            cleanup.power_module,
            "shutdown_verified",
            return_value={"host_powered_off": True, "failures": 0,
                          "elapsed": 0.2},
        )
        self.power_off = power_patcher.start()
        self.addCleanup(power_patcher.stop)
        self._known: set[str] = set()

    # -- helpers -----------------------------------------------------------

    def db(self) -> store.Store:
        return store.Store(self.state_root / "lab.db")

    def capture(self, fn):
        buffer = io.StringIO()
        error = None
        with contextlib.redirect_stdout(buffer), \
             contextlib.redirect_stderr(io.StringIO()):
            try:
                fn()
            except LabError as exc:
                error = exc
        text = buffer.getvalue()
        return (json.loads(text) if text.strip() else None), error

    def begin(self, *, long_term=False, ttl=7200):
        args = SimpleNamespace(
            purpose="test work", long_term=long_term, ttl=ttl, timeout=None
        )
        _, error = self.capture(lambda: leases.cmd_lease_begin(self.lab, args))
        self.assertIsNone(error)
        with self.db() as database:
            rows = database.list_leases(include_ended=True)
        fresh = [row for row in rows if row["id"] not in self._known]
        self.assertEqual(len(fresh), 1)
        self._known.add(fresh[0]["id"])
        return fresh[0]

    def register(self, row, kind, vmid, *, policy="delete", name=None):
        """Through the guarded command path (single owner enforced)."""
        args = SimpleNamespace(
            lease=row["id"], kind=kind, vmid=vmid, policy=policy, name=name,
            allow_existing=True,
        )
        _, error = self.capture(lambda: leases.cmd_lease_register(self.lab, args))
        self.assertIsNone(error)

    def register_shared(self, row, kind, vmid):
        """The one registration path that skips `lease-register`: a module
        calling `register_resource` directly can name a guest another lease
        also registers (the docstring's ghidra-setup re-run case)."""
        lease = leases.load_lease(self.lease_root, row["id"])
        leases.register_resource(
            lease, kind, vmid, "delete", None,
            lease_root=self.lease_root,
            state_root=self.state_root,
            default_ttl=7200,
        )

    def expire(self, row, seconds_ago: int = 30) -> None:
        with self.db() as database:
            database.heartbeat(
                row["id"], expires_at=int(time.time()) - seconds_ago
            )

    def sweep(self, **overrides):
        values = {
            "all": False, "orphans_only": False, "reclaim_orphans": False,
            "include_active": False, "host_change_authorized": False,
        }
        values.update(overrides)
        args = SimpleNamespace(**values)
        return self.capture(lambda: cleanup.cmd_cleanup_expired(self.lab, args))

    def end(self, row, **overrides):
        args = SimpleNamespace(
            lease=row["id"], shared_guests_authorized=False, **overrides
        )
        return self.capture(lambda: cleanup.cmd_lease_end(self.lab, args))

    def destroy(self, row, **overrides):
        args = SimpleNamespace(lease=row["id"], **overrides)
        return self.capture(lambda: cleanup.cmd_lease_destroy(self.lab, args))

    def lease_row(self, lease_id: str) -> dict:
        with self.db() as database:
            return database.get_lease(lease_id)

    # -- lease-end pre-check -----------------------------------------------

    def test_end_refuses_a_shared_guest_before_touching_anything(self):
        row_a = self.begin()
        row_b = self.begin()
        self.register(row_a, "qemu", 101)
        self.register_shared(row_b, "qemu", 101)
        self.seam.calls.clear()

        payload, error = self.end(row_a)
        self.assertIsInstance(error, LabError)
        # The refusal names the guest and the other lease.
        self.assertIn("qemu/101", str(error))
        self.assertIn(row_b["id"], str(error))
        # ...and it happened before any stop/destroy.
        self.assertEqual(self.seam.destructive(), [])
        events = [event for event, _ in self.lab.audits]
        self.assertIn("lease-end-refused-shared-guest", events)
        fields = dict(self.lab.audits)["lease-end-refused-shared-guest"]
        self.assertEqual(
            fields["shared_with_other_leases"][0]["lease"], row_b["id"]
        )
        # The claim was released: the lease is still endable later.
        self.assertEqual(self.lease_row(row_a["id"])["state"], "active")

    def test_end_losing_the_claim_reports_gracefully_without_touching(self):
        row = self.begin()
        self.register(row, "qemu", 101)
        with self.db() as database:
            self.assertTrue(database.claim_lease(
                row["id"], from_state="active", to_state="ending"
            ))
        self.seam.calls.clear()

        payload, error = self.end(row)
        self.assertIsNone(error)
        self.assertIn("already finalizing", payload["note"])
        self.assertEqual(self.seam.destructive(), [])

    # -- cleanup_failed recorded and retried -------------------------------

    def test_failed_teardown_is_recorded_and_retried_by_the_next_sweep(self):
        row = self.begin()
        self.register(row, "qemu", 101)
        self.expire(row)
        self.seam.fail_destroy.add(("qemu", 101))
        self.seam.calls.clear()

        payload, error = self.sweep()
        self.assertIsInstance(error, LabError)
        self.assertIn(row["id"], payload["failed"])
        record = self.lease_row(row["id"])
        self.assertEqual(record["state"], "cleanup_failed")
        self.assertIn("storage locked", record["last_error"])

        # The next sweep retries it whatever the expiry says, and finishes.
        self.seam.fail_destroy.clear()
        self.seam.calls.clear()
        payload, error = self.sweep()
        self.assertIsNone(error)
        self.assertEqual(payload["retried"], [row["id"]])
        self.assertEqual(payload["cleaned"], [row["id"]])
        self.assertEqual(
            [call for call in self.seam.calls if call[0] == "destroy"],
            [("destroy", "qemu", 101)],
        )
        record = self.lease_row(row["id"])
        self.assertEqual(record["state"], "ended")

    # -- ownership and policy ----------------------------------------------

    def test_sweep_leaves_another_lease_s_guest_and_destroys_only_disposable(self):
        row_a = self.begin()
        row_b = self.begin()             # stays live (expires in the future)
        self.register(row_a, "qemu", 101)         # disposable, only A owns it
        self.register_shared(row_b, "qemu", 102)  # both A and B register 102
        self.register_shared(row_a, "qemu", 102)
        self.register(row_a, "qemu", 103, policy="retain")
        self.expire(row_a)
        self.seam.calls.clear()

        payload, error = self.sweep()
        self.assertIsNone(error)
        self.assertEqual(payload["cleaned"], [row_a["id"]])
        self.assertEqual(
            payload["left_to_another_lease"], {row_a["id"]: ["qemu/102"]}
        )
        destroys = [
            (call[1], call[2]) for call in self.seam.calls
            if call[0] == "destroy"
        ]
        self.assertEqual(destroys, [("qemu", 101)])
        touched = {(call[1], call[2]) for call in self.seam.destructive()}
        self.assertEqual(touched, {("qemu", 101)})   # 102 and 103 untouched
        self.assertEqual(self.lease_row(row_a["id"])["state"], "ended")
        self.assertEqual(self.lease_row(row_b["id"])["state"], "active")

    def test_finalize_is_idempotent_and_an_already_gone_guest_is_success(self):
        row = self.begin()
        self.register(row, "qemu", 101)
        self.seam.calls.clear()

        lease = leases.load_lease(self.lease_root, row["id"])
        self.assertEqual(cleanup.finalize_lease(self.lab, None, lease), [])

        # Second run: nothing left to do, nothing called.
        self.seam.calls.clear()
        lease = leases.load_lease(self.lease_root, row["id"], active=False)
        self.assertEqual(cleanup.finalize_lease(self.lab, None, lease), [])
        self.assertEqual(self.seam.destructive(), [])

        # A guest someone removed by hand is success, not a failure.
        row2 = self.begin()
        self.register(row2, "qemu", 201)
        self.seam.gone.add(("qemu", 201))
        self.expire(row2)
        self.seam.calls.clear()
        payload, error = self.sweep()
        self.assertIsNone(error)
        self.assertEqual(payload["cleaned"], [row2["id"]])
        self.assertEqual(self.lease_row(row2["id"])["state"], "ended")

    def test_a_graceful_shutdown_falls_back_to_a_hard_stop(self):
        row = self.begin()
        self.register(row, "lxc", 301)
        self.seam.shutdown_will_stop = False
        self.expire(row)
        self.seam.calls.clear()

        payload, error = self.sweep()
        self.assertIsNone(error)
        kinds = [call[0] for call in self.seam.destructive()]
        self.assertEqual(kinds, ["shutdown", "stop", "destroy"])

    def test_sweep_never_destroys_a_template(self) -> None:
        # `guest destroy` refuses a template because it is the shared clone
        # source; the expiry sweep must not do what the operator-facing
        # command refuses. The registry row says nothing, so the host config
        # decides.
        row = self.begin()
        self.register(row, "qemu", 101)
        self.seam._ssh.add(
            r"^qm config 101", stdout=b"template: 1\nname: tpl\n"
        )
        self.expire(row)
        self.seam.calls.clear()

        payload, error = self.sweep()
        self.assertIsNone(error)
        self.assertNotIn(
            ("destroy", "qemu", 101), self.seam.calls,
            "the sweep destroyed a template",
        )
        self.assertIn("qemu/101", payload["left_to_another_lease"].get(
            row["id"], []
        ))

    def test_a_destroy_error_naming_a_missing_path_is_not_success(self) -> None:
        # The stderr "No such file or directory" can describe a disk the
        # destroy could not remove, not the guest being absent. Trusting that
        # text stamped the resource destroyed, ended the lease and orphaned a
        # live VM with no sweep left to retry it.
        row = self.begin()
        self.register(row, "qemu", 101)

        def destroy(kind, vmid, *, purge=True):
            self.seam.calls.append(("destroy", kind, vmid))
            raise ProxmoxError(
                f"qm destroy {vmid} failed: unable to remove directory "
                f"'/var/lib/vz/images/101': No such file or directory"
            )

        self.seam.destroy = destroy
        self.expire(row)
        self.seam.calls.clear()

        payload, error = self.sweep()
        # the guest still answers a status probe -> it is NOT gone
        self.assertEqual(self.lease_row(row["id"])["state"], "cleanup_failed")
        self.assertNotIn(row["id"], payload["cleaned"])

    # -- lease-destroy ------------------------------------------------------

    def test_destroy_refuses_without_confirm_before_any_side_effect(self):
        row = self.begin(long_term=True)
        self.register(row, "qemu", 201)
        self.seam.calls.clear()

        payload, error = self.destroy(row, confirm=False)
        self.assertIsInstance(error, LabError)
        self.assertIn("permanently destroys", str(error))
        self.assertIn("qemu/201", str(error))
        self.assertEqual(self.seam.calls, [])
        self.assertFalse(self.power_off.called)
        self.assertEqual(self.lease_row(row["id"])["state"], "active")

    def test_destroy_confirm_lifts_long_term_protection_then_finalizes(self):
        row = self.begin(long_term=True)
        self.register(row, "qemu", 201)
        _, _, _, _, before = self.seam.calls[-1]
        self.assertEqual(before, f"pxl-lease={row['id']} pxl-expiry=0")
        self.seam.calls.clear()

        payload, error = self.destroy(row, confirm=True)
        self.assertIsNone(error)
        calls = self.seam.calls
        stamps = [call for call in calls if call[0] == "set_metadata"]
        self.assertEqual(len(stamps), 1)
        # Protection is lifted as metadata: pxl-expiry -> the past, never 0.
        self.assertRegex(stamps[0][4], r"pxl-expiry=[1-9]\d*$")
        destroys = [call for call in calls if call[0] == "destroy"]
        self.assertEqual(
            [(call[1], call[2]) for call in destroys], [("qemu", 201)]
        )
        # ...and lifted strictly before the teardown began.
        self.assertLess(
            calls.index(stamps[0]),
            calls.index(destroys[0]),
        )
        self.assertEqual(self.lease_row(row["id"])["state"], "destroyed")
        self.assertEqual(payload["destroyed_guests"], ["qemu/201 (unnamed)"])
        self.assertTrue(payload["host_powered_off"])

    def test_the_sweep_never_finalizes_a_long_term_lease(self):
        row = self.begin(long_term=True)
        self.register(row, "qemu", 201)
        self.seam.calls.clear()

        payload, error = self.sweep(all=True)
        self.assertIsNone(error)
        self.assertEqual(payload["cleaned"], [])
        self.assertEqual(self.seam.destructive(), [])
        self.assertEqual(self.lease_row(row["id"])["state"], "active")

    # -- host power-off -----------------------------------------------------

    def test_end_stays_successful_when_other_guests_keep_the_host_up(self):
        row = self.begin()
        with mock.patch.object(
            cleanup, "running_guest_vmids", return_value=[101, 2000]
        ):
            payload, error = self.end(row)
        self.assertIsNone(error)
        self.assertFalse(payload["host_powered_off"])
        self.assertTrue(payload["host_left_running"])
        self.assertIn("101", payload["reason"])
        self.assertIn("2000", payload["reason"])
        self.assertFalse(self.power_off.called)
        self.assertEqual(self.lease_row(row["id"])["state"], "ended")

    def test_end_reports_host_powered_off_truthfully(self):
        row = self.begin()
        self.register(row, "qemu", 101)
        self.power_off.return_value = {
            "host_powered_off": False, "failures": 6, "elapsed": 61.0,
        }
        self.seam.calls.clear()
        payload, error = self.end(row)
        self.assertIsInstance(error, LabError)      # "did not complete"
        self.assertFalse(payload["host_powered_off"])
        self.assertTrue(payload["host_left_running"])
        self.assertIn("could not be verified", payload["reason"])
        self.assertTrue(self.power_off.called)
        kwargs = self.power_off.call_args.kwargs
        self.assertTrue(callable(kwargs["request_fn"]))
        self.assertTrue(callable(kwargs["probe_fn"]))

        # Truthful the other way too: verified off is reported as off.
        row2 = self.begin()
        self.register(row2, "qemu", 201)
        self.power_off.return_value = {
            "host_powered_off": True, "failures": 6, "elapsed": 40.0,
        }
        payload, error = self.end(row2)
        self.assertIsNone(error)
        self.assertTrue(payload["host_powered_off"])
        self.assertNotIn("host_left_running", payload)
        self.assertEqual(self.lease_row(row2["id"])["state"], "ended")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
