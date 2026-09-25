"""Tests for the SQLite store: schema setup, lease lifecycle, CAS transitions,
redaction-before-insert, event queries, and legacy journal.db readability.

Everything runs against a per-test ``TemporaryDirectory`` database -- no
external services, no network, no shared state between tests.
"""

from __future__ import annotations

from pathlib import Path
import sys  # noqa: E402

# Shared bootstrap: fixture configuration plus a per-process state directory,
# applied before any proxmox_agent_lab import. `support` sits beside this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import bootstrap  # noqa: E402,F401

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import json  # noqa: E402
import sqlite3  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
import unittest  # noqa: E402

from proxmox_agent_lab.errors import LabError  # noqa: E402
from proxmox_agent_lab.store import (  # noqa: E402
    SCHEMA_VERSION,
    Store,
    StoreError,
    redact_data,
)

LEGACY_EVENTS_DDL = (
    "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, "
    "event TEXT, lease TEXT, vmid INTEGER, data TEXT)"
)


class StoreTest(unittest.TestCase):
    """Schema, lease/resource lifecycle, and journal behavior of ``Store``."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = Path(tmp.name) / "lab.db"
        self.store = Store(self.db_path)
        self.addCleanup(self.store.close)

    def raw_conn(self, path: Path | None = None) -> sqlite3.Connection:
        """A raw sqlite3 handle for poking at the file behind the store."""
        conn = sqlite3.connect(self.db_path if path is None else path)
        self.addCleanup(conn.close)
        return conn

    # -- schema ----------------------------------------------------------

    def test_schema_created_with_wal_mode(self) -> None:
        conn = self.raw_conn()
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        self.assertLessEqual({"schema_meta", "events", "leases", "resources"}, tables)
        indexes = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        self.assertLessEqual(
            {"idx_events_lease", "idx_events_ts", "idx_resources_vmid"}, indexes
        )
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        version = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        self.assertEqual(version, SCHEMA_VERSION)

    def test_schema_version_mismatch_raises_store_error(self) -> None:
        path = Path(self.db_path).parent / "old.db"
        conn = self.raw_conn(path)
        conn.execute(
            "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO schema_meta VALUES ('schema_version', '0')")
        conn.commit()
        conn.close()
        with self.assertRaises(StoreError):
            Store(path)
        # StoreError is a deliberate, user-facing failure.
        self.assertTrue(issubclass(StoreError, LabError))

    # -- lease lifecycle --------------------------------------------------

    def test_create_get_list_heartbeat_lifecycle(self) -> None:
        expires = int(time.time()) + 3600
        self.store.create_lease("lease-a", purpose="demo", expires_at=expires)

        lease = self.store.get_lease("lease-a")
        assert lease is not None
        self.assertEqual(lease["state"], "active")
        self.assertEqual(lease["kind"], "ordinary")
        self.assertEqual(lease["purpose"], "demo")
        self.assertEqual(lease["expires_at"], expires)
        self.assertIsNone(lease["heartbeat_at"])
        self.assertIsNone(lease["ended_at"])
        self.assertIsNone(lease["last_error"])

        self.assertEqual([row["id"] for row in self.store.list_leases()], ["lease-a"])
        self.assertEqual([row["id"] for row in self.store.active_leases()], ["lease-a"])

        self.assertTrue(
            self.store.heartbeat(
                "lease-a", expires_at=expires + 600, now="2026-01-01T00:00:00Z"
            )
        )
        lease = self.store.get_lease("lease-a")
        assert lease is not None
        self.assertEqual(lease["expires_at"], expires + 600)
        self.assertEqual(lease["heartbeat_at"], "2026-01-01T00:00:00Z")

        # A duplicate id is refused, not silently ignored.
        with self.assertRaises(StoreError):
            self.store.create_lease("lease-a", expires_at=expires)

        # Ending hides the lease from the default listing and stops heartbeats.
        self.store.set_lease_state("lease-a", "ended", ended=True)
        self.assertFalse(self.store.heartbeat("lease-a", expires_at=expires))
        self.assertEqual(self.store.list_leases(), [])
        self.assertEqual(self.store.active_leases(), [])
        self.assertEqual(
            [row["id"] for row in self.store.list_leases(include_ended=True)],
            ["lease-a"],
        )
        lease = self.store.get_lease("lease-a")
        assert lease is not None
        self.assertIsNotNone(lease["ended_at"])

    def test_heartbeat_refused_when_not_active(self) -> None:
        self.store.create_lease("lease-a", expires_at=1)
        self.assertTrue(self.store.claim_lease("lease-a", from_state="active", to_state="ending"))
        self.assertFalse(self.store.heartbeat("lease-a", expires_at=2))
        self.assertFalse(self.store.heartbeat("lease-missing", expires_at=2))

    # -- CAS state transitions --------------------------------------------

    def test_claim_lease_is_compare_and_swap(self) -> None:
        self.store.create_lease("lease-a", expires_at=1)
        self.assertTrue(
            self.store.claim_lease("lease-a", from_state="active", to_state="ending")
        )
        # A second concurrent claim from the old state loses.
        self.assertFalse(
            self.store.claim_lease("lease-a", from_state="active", to_state="ending")
        )
        # A claim from a state the lease is not in loses too.
        self.assertFalse(
            self.store.claim_lease("lease-a", from_state="active", to_state="ended")
        )
        self.assertEqual(self.store.get_lease("lease-a")["state"], "ending")  # type: ignore[index]
        self.assertTrue(
            self.store.claim_lease("lease-a", from_state="ending", to_state="ended")
        )

    def test_cleanup_failed_records_error_and_keeps_lease(self) -> None:
        self.store.create_lease("lease-a", expires_at=1)
        self.store.set_lease_state(
            "lease-a", "cleanup_failed", error="qm destroy 101 failed"
        )
        lease = self.store.get_lease("lease-a")
        assert lease is not None
        self.assertEqual(lease["state"], "cleanup_failed")
        self.assertEqual(lease["last_error"], "qm destroy 101 failed")
        self.assertIsNone(lease["ended_at"])
        # cleanup_failed is non-terminal: the lease stays visible and owned.
        self.assertEqual([row["id"] for row in self.store.list_leases()], ["lease-a"])
        self.assertEqual([row["id"] for row in self.store.active_leases()], ["lease-a"])
        # A later state change without an error keeps the recorded one.
        self.store.set_lease_state("lease-a", "ending")
        lease = self.store.get_lease("lease-a")
        assert lease is not None
        self.assertEqual(lease["last_error"], "qm destroy 101 failed")

    # -- resources --------------------------------------------------------

    def test_resources_register_destroy_and_ownership(self) -> None:
        self.store.create_lease("lease-a", expires_at=1)
        self.store.create_lease("lease-b", expires_at=1)
        with self.assertRaises(StoreError):
            self.store.register_resource("lease-missing", "qemu", 101)

        res_id = self.store.register_resource("lease-a", "qemu", 101, name="box")
        self.assertIsInstance(res_id, int)
        rows = self.store.resources_for("lease-a")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "qemu")
        self.assertEqual(rows[0]["vmid"], 101)
        self.assertEqual(rows[0]["name"], "box")
        self.assertEqual(rows[0]["policy"], "disposable")
        self.assertIsNone(rows[0]["destroyed_at"])

        # A live lease shields its guest from other leases...
        self.assertEqual(self.store.owner_elsewhere("lease-b", "qemu", 101), "lease-a")
        self.assertIsNone(self.store.owner_elsewhere("lease-a", "qemu", 101))
        self.assertIsNone(self.store.owner_elsewhere("lease-b", "qemu", 102))

        # ...but an ended owner does not shield.
        self.store.set_lease_state("lease-a", "ended", ended=True)
        self.assertIsNone(self.store.owner_elsewhere("lease-b", "qemu", 101))

        # cleanup_failed keeps ownership, so it does shield.
        self.store.create_lease("lease-c", expires_at=1)
        self.store.register_resource("lease-c", "qemu", 101)
        self.store.set_lease_state("lease-c", "cleanup_failed", error="boom")
        self.assertEqual(self.store.owner_elsewhere("lease-b", "qemu", 101), "lease-c")

    def test_mark_destroyed_is_idempotent(self) -> None:
        self.store.create_lease("lease-a", expires_at=1)
        self.store.register_resource("lease-a", "qemu", 101)
        self.assertTrue(self.store.mark_destroyed("lease-a", "qemu", 101))
        self.assertFalse(self.store.mark_destroyed("lease-a", "qemu", 101))
        rows = self.store.resources_for("lease-a")
        self.assertIsNotNone(rows[0]["destroyed_at"])

    # -- events -----------------------------------------------------------

    def test_record_redacts_secrets_before_insert(self) -> None:
        self.store.record(
            "mcp_call",
            lease="lease-a",
            vmid=101,
            data={
                "password": "hunter2-secret",
                "token": "tok-123456789",
                "nested": {"api_key": "key-987654321", "ok": True},
                "note": "plain",
            },
        )
        rows = self.store.query_events()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event"], "mcp_call")
        self.assertEqual(rows[0]["lease"], "lease-a")
        self.assertEqual(rows[0]["vmid"], 101)
        data = json.loads(rows[0]["data"])
        self.assertEqual(data["password"], "[REDACTED]")
        self.assertEqual(data["token"], "[REDACTED]")
        self.assertEqual(data["nested"]["api_key"], "[REDACTED]")
        self.assertEqual(data["nested"]["ok"], True)
        self.assertEqual(data["note"], "plain")

        # The plaintext must not be on disk at all: close (WAL folds into the
        # main file on last close) and scan every byte of the database.
        self.store.close()
        for path in self.db_path.parent.iterdir():
            raw = path.read_bytes()
            self.assertNotIn(b"hunter2-secret", raw)
            self.assertNotIn(b"tok-123456789", raw)
            self.assertNotIn(b"key-987654321", raw)
        self.assertIn(b"[REDACTED]", self.db_path.read_bytes())

    def test_redact_data_masks_secret_keys_recursively(self) -> None:
        data = {
            "password": "x",
            "AUTH": "y",
            "cookie": "z",
            "api-key": "k",
            "apikey": "k2",
            "credential": "c",
            "private_key": "p",
            "secret": "s",
            "safe": "keep",
            "nested": {"token": "t", "deep": {"secret": "s", "keep": 1}},
            "items": [{"pass": "leaked", "keep": 2}],
        }
        out = redact_data(data)
        for key in (
            "password",
            "AUTH",
            "cookie",
            "api-key",
            "apikey",
            "credential",
            "private_key",
            "secret",
        ):
            self.assertEqual(out[key], "[REDACTED]", key)
        self.assertEqual(out["safe"], "keep")
        self.assertEqual(out["nested"]["token"], "[REDACTED]")
        self.assertEqual(out["nested"]["deep"], {"secret": "[REDACTED]", "keep": 1})
        self.assertEqual(out["items"], [{"pass": "[REDACTED]", "keep": 2}])
        # The input is never mutated.
        self.assertEqual(data["password"], "x")
        self.assertEqual(data["nested"]["token"], "t")

    def test_query_events_filters(self) -> None:
        first = self.store.record(
            "a", lease="lease-a", timestamp="2026-01-01T00:00:00Z"
        )
        second = self.store.record(
            "b", lease="lease-b", timestamp="2026-01-02T00:00:00Z"
        )
        third = self.store.record(
            "c", lease="lease-a", timestamp="2026-01-03T00:00:00Z"
        )
        self.assertLess(first, second)
        self.assertLess(second, third)

        def names(rows: list[dict]) -> list[str]:
            return [row["event"] for row in rows]

        # Newest first by default.
        self.assertEqual(names(self.store.query_events()), ["c", "b", "a"])
        self.assertEqual(
            names(self.store.query_events(lease="lease-a")), ["c", "a"]
        )
        self.assertEqual(
            names(self.store.query_events(since="2026-01-02T00:00:00Z")), ["c", "b"]
        )
        self.assertEqual(names(self.store.query_events(limit=2)), ["c", "b"])
        self.assertEqual(
            names(
                self.store.query_events(
                    lease="lease-a", since="2026-01-03T00:00:00Z", limit=1
                )
            ),
            ["c"],
        )

    def test_legacy_events_table_stays_readable(self) -> None:
        # A database built with the exact pre-MariaDB journal.db DDL and
        # old-style inserts must open cleanly and read back.
        path = Path(self.db_path).parent / "legacy.db"
        conn = self.raw_conn(path)
        conn.execute(LEGACY_EVENTS_DDL)
        conn.execute(
            "INSERT INTO events (timestamp, event, data) VALUES (?,?,?)",
            (
                "2025-06-01T00:00:00Z",
                "old_event",
                json.dumps({"controller": "cli"}),
            ),
        )
        conn.commit()
        conn.close()

        with Store(path) as store:
            rows = store.query_events()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["event"], "old_event")
            self.assertEqual(rows[0]["timestamp"], "2025-06-01T00:00:00Z")
            self.assertEqual(rows[0]["lease"], None)
            self.assertEqual(rows[0]["vmid"], None)
            self.assertEqual(rows[0]["data"], '{"controller": "cli"}')
            # New events append alongside the legacy rows.
            store.record("new_event")
            self.assertEqual(
                [row["event"] for row in store.query_events()],
                ["new_event", "old_event"],
            )
            # The legacy database is adopted at the current schema version.
            self.assertEqual(
                store.last_mcp_activity(), None
            )  # schema_meta seeded, idle clock unset

        raw = self.raw_conn(path)
        self.assertEqual(
            raw.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()[0],
            SCHEMA_VERSION,
        )

    # -- MCP idle clock ---------------------------------------------------

    def test_mcp_activity_round_trip(self) -> None:
        self.assertIsNone(self.store.last_mcp_activity())
        self.store.touch_mcp_activity(now=1700000000.5)
        self.assertEqual(self.store.last_mcp_activity(), 1700000000.5)
        self.store.touch_mcp_activity(now=1700000099.0)
        self.assertEqual(self.store.last_mcp_activity(), 1700000099.0)
        self.store.touch_mcp_activity()
        assert self.store.last_mcp_activity() is not None
        self.assertAlmostEqual(self.store.last_mcp_activity(), time.time(), delta=10)  # type: ignore[arg-type]


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
