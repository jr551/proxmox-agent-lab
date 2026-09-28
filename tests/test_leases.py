"""Lease lifecycle over the SQLite store and the proxmox seam.

Covers the director's invariant list for the lease side: begin requires a
reachable host and writes the lease row (with GC metadata args to the seam
when a guest joins the lease); heartbeat refreshes expiry metadata on every
registered guest and extends ``expires_at``; claim_lease is compare-and-swap;
long-term is ``kind='long_term'`` + ``expires_at=0`` + ``pxl-expiry=0``; and
the MCP idle clock fires only when idle AND unowned.

Everything runs against a ``TemporaryDirectory`` state root and a fake seam
over ``FakeSSH`` -- no test spawns a real ssh or opens real controller state.
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

from proxmox_agent_lab import leases  # noqa: E402
from proxmox_agent_lab import store  # noqa: E402
from proxmox_agent_lab.errors import LabError  # noqa: E402
from support.fakessh import FakeSSH  # noqa: E402


def fake_config() -> SimpleNamespace:
    """The §G surface the lifecycle reads, and nothing else."""
    return SimpleNamespace(
        ssh=SimpleNamespace(target="fixture-host"),
        pve=SimpleNamespace(node="pve", template_vmid=9000),
        lease=SimpleNamespace(ttl_seconds=7200, idle_shutdown_seconds=28800),
        power=SimpleNamespace(
            mac="aa:bb:cc:dd:ee:ff", broadcast="255.255.255.255", port=9
        ),
    )


class FakeSeam:
    """Records the metadata writes the GC contract cares about."""

    def __init__(self, *, reachable: bool = True) -> None:
        self.calls: list[tuple] = []
        self._ssh = FakeSSH()
        if reachable:
            self._ssh.add("true")

    def set_metadata(self, kind, vmid, *, tags=None, description=None):
        self.calls.append(("set_metadata", kind, vmid, tags, description))


class FakeLab:
    """The `lab` surface the handlers use: config, state root, audit."""

    def __init__(self, state_root: Path) -> None:
        self.STATE_ROOT = Path(state_root)
        self.CONFIG = fake_config()
        self.audits: list[tuple[str, dict]] = []

    def audit(self, event: str, **fields) -> None:
        self.audits.append((event, fields))


class LeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_root = Path(self.tmp.name)
        self.lease_root = self.state_root / "leases"
        self.lab = FakeLab(self.state_root)
        self.seam = FakeSeam()
        patcher = mock.patch.object(
            leases, "_make_proxmox", return_value=self.seam
        )
        patcher.start()
        self.addCleanup(patcher.stop)

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

    def begin(self, *, purpose="test work", long_term=False, ttl=7200):
        args = SimpleNamespace(
            purpose=purpose, long_term=long_term, ttl=ttl, timeout=None
        )
        payload, error = self.capture(lambda: leases.cmd_lease_begin(self.lab, args))
        self.assertIsNone(error)
        self.last_output = payload
        with self.db() as database:
            rows = database.list_leases(include_ended=True)
        self.assertEqual(len(rows), 1)
        return rows[0]

    def register(self, row, kind, vmid, *, policy="delete", name=None):
        args = SimpleNamespace(
            lease=row["id"], kind=kind, vmid=vmid, policy=policy, name=name,
            allow_existing=True,
        )
        _, error = self.capture(
            lambda: leases.cmd_lease_register(self.lab, args)
        )
        self.assertIsNone(error)

    def stamps(self):
        return [call for call in self.seam.calls if call[0] == "set_metadata"]

    # -- begin -------------------------------------------------------------

    def test_begin_requires_a_reachable_host_and_writes_no_lease_row(self):
        self.seam._ssh = FakeSSH()          # no "true" rule: host unreachable
        args = SimpleNamespace(
            purpose="test work", long_term=False, ttl=7200, timeout=None
        )
        _, error = self.capture(lambda: leases.cmd_lease_begin(self.lab, args))
        self.assertIsInstance(error, LabError)
        self.assertIn("not reachable", str(error))
        with self.db() as database:
            self.assertEqual(database.list_leases(include_ended=True), [])

    def test_begin_writes_the_lease_row_and_registration_stamps_the_seam(self):
        row = self.begin()
        self.assertEqual(row["kind"], "ordinary")
        self.assertEqual(row["state"], "active")
        self.assertEqual(row["purpose"], "test work")
        self.assertGreater(row["expires_at"], int(time.time()))
        self.assertLessEqual(row["expires_at"], int(time.time()) + 7200)

        # A guest created through the lease carries GC metadata written in
        # the same breath as its registration.
        self.register(row, "qemu", 101, name="worker")
        with self.db() as database:
            refreshed = database.get_lease(row["id"])
        stamps = self.stamps()
        self.assertEqual(len(stamps), 1)
        _, kind, vmid, tags, description = stamps[0]
        self.assertEqual((kind, vmid), ("qemu", 101))
        # Ownership tag, the machine that created it, and the lease id.
        self.assertTrue(tags.startswith("proxmoxagentlab;"))
        self.assertTrue(tags.endswith(f";lease-{row['id']}"))
        self.assertEqual(
            description,
            f"pxl-lease={row['id']} pxl-expiry={refreshed['expires_at']}",
        )
        # The old --policy spelling maps onto the store vocabulary.
        resources = leases.load_lease(self.lease_root, row["id"])["resources"]
        self.assertEqual(resources[0]["policy"], "disposable")

    # -- heartbeat ---------------------------------------------------------

    def test_heartbeat_extends_expiry_and_refreshes_every_guest_metadata(self):
        row = self.begin(ttl=3600)
        self.register(row, "qemu", 101)
        self.register(row, "lxc", 102)
        with self.db() as database:
            old_expiry = database.get_lease(row["id"])["expires_at"]
        self.seam.calls.clear()

        args = SimpleNamespace(lease=row["id"], ttl=10800)
        payload, error = self.capture(
            lambda: leases.cmd_lease_heartbeat(self.lab, args)
        )
        self.assertIsNone(error)
        new_expiry = payload["expires_at"]
        self.assertEqual(payload["lease"], row["id"])
        self.assertGreater(new_expiry, old_expiry)
        with self.db() as database:
            self.assertEqual(
                database.get_lease(row["id"])["expires_at"], new_expiry
            )

        # Every registered guest gets the new epoch -- the GC is metadata
        # driven, and stale metadata on one guest would reap live work.
        stamps = self.stamps()
        self.assertEqual(len(stamps), 2)
        self.assertEqual({stamp[2] for stamp in stamps}, {101, 102})
        for stamp in stamps:
            self.assertEqual(
                stamp[4],
                f"pxl-lease={row['id']} pxl-expiry={new_expiry}",
            )

    # -- CAS --------------------------------------------------------------

    def test_claim_lease_is_compare_and_swap(self):
        row = self.begin()
        self.assertTrue(leases.claim_lease(
            self.lease_root, row["id"],
            from_state="active", to_state="ending",
        ))
        self.assertFalse(leases.claim_lease(
            self.lease_root, row["id"],
            from_state="active", to_state="ending",
        ))
        with self.db() as database:
            self.assertEqual(database.get_lease(row["id"])["state"], "ending")

    # -- long-term ---------------------------------------------------------

    def test_long_term_is_kind_expiry_zero_and_pxl_expiry_zero(self):
        args = SimpleNamespace(
            purpose="kept machine", long_term=True, ttl=7200, timeout=None
        )
        payload, error = self.capture(
            lambda: leases.cmd_lease_begin(self.lab, args)
        )
        self.assertIsNone(error)
        with self.db() as database:
            rows = database.list_leases(include_ended=True)
        row = rows[0]
        self.assertEqual(row["kind"], "long_term")
        self.assertEqual(row["expires_at"], 0)
        self.assertIn("long-term", payload["warning"])
        self.assertTrue(leases.is_long_term(row))
        self.assertTrue(leases.lease_is_live(row, now=time.time() + 10**9))

        self.register(row, "qemu", 201)
        _, _, _, _, description = self.stamps()[-1]
        self.assertEqual(
            description, f"pxl-lease={row['id']} pxl-expiry=0"
        )
        # Registering must never hand a long-term lease an expiry.
        with self.db() as database:
            self.assertEqual(database.get_lease(row["id"])["expires_at"], 0)

        # A heartbeat is a no-op by design: nothing expires it.
        heartbeat_args = SimpleNamespace(lease=row["id"], ttl=6000)
        payload, error = self.capture(
            lambda: leases.cmd_lease_heartbeat(self.lab, heartbeat_args)
        )
        self.assertIsNone(error)
        self.assertIn("do not expire", payload["note"])
        with self.db() as database:
            self.assertEqual(database.get_lease(row["id"])["expires_at"], 0)

    # -- MCP idle clock ----------------------------------------------------

    def test_mcp_idle_clock_gates_only_on_idle_and_ownership(self):
        audited: list[tuple[str, dict]] = []

        def audit(event, **fields):
            audited.append((event, fields))

        leases.record_mcp_activity(self.state_root, "guest run", audit=audit)
        self.assertEqual([event for event, _ in audited], ["mcp-command"])
        with self.db() as database:
            last = database.last_mcp_activity()
        self.assertIsNotNone(last)
        self.assertEqual(leases.mcp_idle_elapsed(self.state_root, now=last), 0.0)

        # Idle for hours, but a lease is active: somebody is thinking, not an
        # empty lab. Never shut down.
        row = self.begin()
        self.assertFalse(leases.mcp_idle_shutdown_due(
            self.state_root, idle_shutdown_seconds=1.0, now=last + 3600
        ))

        with self.db() as database:
            self.assertTrue(database.claim_lease(
                row["id"], from_state="active", to_state="ended"
            ))

        # Idle AND unowned: due.
        self.assertTrue(leases.mcp_idle_shutdown_due(
            self.state_root, idle_shutdown_seconds=1.0, now=last + 3600
        ))
        # Unowned but recently active: not due.
        self.assertFalse(leases.mcp_idle_shutdown_due(
            self.state_root, idle_shutdown_seconds=60.0, now=last + 1
        ))

        # A fresh tool call resets the clock.
        leases.record_mcp_activity(self.state_root, "again", audit=audit)
        with self.db() as database:
            refreshed = database.last_mcp_activity()
        self.assertGreaterEqual(refreshed, last)
        self.assertFalse(leases.mcp_idle_shutdown_due(
            self.state_root, idle_shutdown_seconds=1.0, now=refreshed
        ))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
