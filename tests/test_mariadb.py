"""The audit ledger against a real MariaDB.

Skipped unless PXL_TEST_MARIADB points at a throwaway server, e.g.

    docker run -d --name pxl-mariadb-test -p 13306:3306 \\
      -e MARIADB_ROOT_PASSWORD=roottest -e MARIADB_DATABASE=proxmox_lab \\
      -e MARIADB_USER=proxmox_lab -e MARIADB_PASSWORD=labtest mariadb:11
    PXL_TEST_MARIADB=proxmox_lab:labtest@127.0.0.1:13306/proxmox_lab \\
      python3 -m unittest tests.test_mariadb

A stub cannot tell you that INSERT IGNORE really is idempotent, that the
unique index really does absorb a replayed spool, or that GET_LOCK really
serialises two controllers. Those are the properties the ledger is built on,
so they are checked against the real server.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from proxmox_agent_lab import journal as journal_module  # noqa: E402
from proxmox_agent_lab import mariadb  # noqa: E402

DSN = os.environ.get("PXL_TEST_MARIADB", "")


def _settings() -> mariadb.Settings:
    """user:password@host:port/database"""
    creds, _, rest = DSN.partition("@")
    user, _, password = creds.partition(":")
    hostport, _, database = rest.partition("/")
    host, _, port = hostport.partition(":")
    return mariadb.Settings(
        host, port=int(port or 3306), database=database,
        user=user, password=password,
    )


@unittest.skipUnless(DSN, "set PXL_TEST_MARIADB to run ledger integration tests")
class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = _settings()
        connection = mariadb.connect(self.settings)
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE IF EXISTS events")
            cursor.execute("DROP TABLE IF EXISTS migrations")
            cursor.execute("DROP TABLE IF EXISTS secrets")
        connection.close()
        mariadb.ensure_schema(self.settings)

    def _event(self, name: str, **fields: object) -> dict[str, object]:
        return {
            "timestamp": "2026-01-01T00:00:00Z", "event": name,
            "controller": "pc-1", "event_id": name, **fields,
        }

    def test_ensure_schema_is_safe_to_run_twice(self) -> None:
        mariadb.ensure_schema(self.settings)
        self.assertTrue(mariadb.ping(self.settings))

    def test_append_and_query_round_trip(self) -> None:
        mariadb.append(self.settings, self._event("lease-begin", lease="L1"))
        rows = mariadb.query(self.settings, limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event"], "lease-begin")

    def test_a_replayed_event_is_ignored_not_duplicated(self) -> None:
        """The property the spool and the migration both depend on."""
        event = self._event("guest-run")
        mariadb.append(self.settings, event)
        mariadb.append(self.settings, event)
        self.assertEqual(mariadb.count(self.settings), 1)

    def test_append_many_reports_only_what_was_new(self) -> None:
        first = [self._event(f"e{i}") for i in range(5)]
        self.assertEqual(mariadb.append_many(self.settings, first), 5)
        overlap = first[3:] + [self._event("e9")]
        self.assertEqual(mariadb.append_many(self.settings, overlap), 1)
        self.assertEqual(mariadb.count(self.settings), 6)

    def test_filters_match_the_documented_semantics(self) -> None:
        mariadb.append_many(self.settings, [
            self._event("guest-run", lease="L1", vmid=1),
            self._event("guest-push", lease="L1", vmid=2),
            self._event("lease-end", lease="L2"),
        ])
        self.assertEqual(len(mariadb.query(self.settings, lease="L1")), 2)
        self.assertEqual(len(mariadb.query(self.settings, event="guest-*")), 2)
        self.assertEqual(len(mariadb.query(self.settings, event="lease-end")), 1)
        self.assertEqual(
            len(mariadb.query(self.settings, since="2026-06-01T00:00:00Z")), 0
        )

    def test_query_is_newest_first(self) -> None:
        for i in range(3):
            mariadb.append(self.settings, self._event(
                f"e{i}", timestamp=f"2026-01-0{i + 1}T00:00:00Z"))
        rows = mariadb.query(self.settings, limit=3)
        self.assertEqual([r["event"] for r in rows], ["e2", "e1", "e0"])

    def test_summary_counts_leases_and_controllers(self) -> None:
        mariadb.append_many(self.settings, [
            self._event("a", lease="L1"),
            self._event("b", lease="L2", controller="pc-2"),
        ])
        summary = mariadb.summary(self.settings)
        self.assertEqual(summary["events"], 2)
        self.assertEqual(summary["distinct_leases"], 2)
        self.assertEqual(summary["distinct_controllers"], 2)

    def test_a_vmid_that_is_not_a_number_does_not_break_the_insert(self) -> None:
        mariadb.append(self.settings, self._event("odd", vmid="not-a-number"))
        self.assertEqual(mariadb.count(self.settings), 1)

    def test_the_migration_lock_excludes_a_second_holder(self) -> None:
        """Two controllers upgrading at once must serialise, not interleave."""
        with mariadb.migration_lock(self.settings):
            original = mariadb.MIGRATION_LOCK_TIMEOUT
            mariadb.MIGRATION_LOCK_TIMEOUT = 1
            try:
                with self.assertRaises(mariadb.MariaDBError):
                    with mariadb.migration_lock(self.settings):
                        pass
            finally:
                mariadb.MIGRATION_LOCK_TIMEOUT = original
        # Released on exit, so the next controller gets it.
        with mariadb.migration_lock(self.settings):
            pass

    def test_shared_secrets_round_trip_without_exposing_values(self) -> None:
        mariadb.put_secret(self.settings, "ngrok-authtoken", "tok",
                           updated_by="pc-1", updated_at="2026-01-01T00:00:00Z")
        self.assertEqual(
            mariadb.get_secret(self.settings, "ngrok-authtoken"), "tok"
        )
        listed = mariadb.list_secrets(self.settings)
        self.assertEqual([r["name"] for r in listed], ["ngrok-authtoken"])
        self.assertNotIn("value", listed[0])
        self.assertTrue(mariadb.delete_secret(self.settings, "ngrok-authtoken"))
        self.assertIsNone(mariadb.get_secret(self.settings, "ngrok-authtoken"))

    def test_a_missing_secret_is_none_not_an_error(self) -> None:
        self.assertIsNone(mariadb.get_secret(self.settings, "never-stored"))


@unittest.skipUnless(DSN, "set PXL_TEST_MARIADB to run ledger integration tests")
class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = _settings()
        connection = mariadb.connect(self.settings)
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE IF EXISTS events")
            cursor.execute("DROP TABLE IF EXISTS migrations")
        connection.close()
        mariadb.ensure_schema(self.settings)

    def _legacy(self, root: Path, events: list[str]) -> None:
        import sqlite3

        root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(journal_module.legacy_database_path(root))
        connection.execute(
            "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "timestamp TEXT, event TEXT, lease TEXT, vmid INTEGER, data TEXT)"
        )
        for name in events:
            record = {"timestamp": "2026-01-01T00:00:00Z", "event": name}
            connection.execute(
                "INSERT INTO events (timestamp, event, data) VALUES (?, ?, ?)",
                (record["timestamp"], name, json.dumps(record, sort_keys=True)),
            )
        connection.commit()
        connection.close()

    def test_a_second_controller_adds_only_its_own_events(self) -> None:
        """The case that matters: two machines, overlapping histories."""
        with tempfile.TemporaryDirectory() as tmp:
            one = Path(tmp) / "pc1"
            self._legacy(one, ["a", "b", "c"])
            first = journal_module.migrate_legacy(
                self.settings, one, controller="pc-1")
            self.assertEqual(first["uploaded"], 3)
            self.assertEqual(first["migrated_by_others"], [])

            two = Path(tmp) / "pc2"
            self._legacy(two, ["a", "b", "c", "d"])  # 3 shared, 1 of its own
            second = journal_module.migrate_legacy(
                self.settings, two, controller="pc-2")
            self.assertEqual(second["uploaded"], 1)
            self.assertEqual(second["already_present"], 3)
            self.assertEqual(second["migrated_by_others"], ["pc-1"])
            self.assertFalse(second["repeat_migration"])

            self.assertEqual(mariadb.count(self.settings), 4)

    def test_re_running_a_migration_is_a_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "pc1"
            self._legacy(root, ["a", "b"])
            journal_module.migrate_legacy(self.settings, root, controller="pc-1")
            again = journal_module.migrate_legacy(
                self.settings, root, controller="pc-1")
            self.assertEqual(again["uploaded"], 0)
            self.assertTrue(again["repeat_migration"])
            self.assertEqual(mariadb.count(self.settings), 2)

    def test_the_registry_records_who_has_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "pc1"
            self._legacy(root, ["a"])
            journal_module.migrate_legacy(self.settings, root, controller="pc-1")
            rows = mariadb.migrations(self.settings)
            self.assertEqual([r["controller"] for r in rows], ["pc-1"])

    def test_a_spool_flushes_and_clears(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("x", "y"):
                journal_module.record(
                    None, root,
                    {"timestamp": "2026-01-01T00:00:00Z", "event": name},
                    controller="pc-1",
                )
            self.assertEqual(len(journal_module.read_spool(root)), 2)
            result = journal_module.flush_spool(
                self.settings, root, controller="pc-1")
            self.assertEqual(result["uploaded"], 2)
            self.assertTrue(result["cleared"])
            self.assertEqual(journal_module.read_spool(root), [])

    def test_flushing_the_same_spool_twice_does_not_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry = {"timestamp": "2026-01-01T00:00:00Z", "event": "z"}
            journal_module.record(None, root, entry, controller="pc-1")
            journal_module.flush_spool(self.settings, root, controller="pc-1")
            journal_module.record(None, root, entry, controller="pc-1")
            result = journal_module.flush_spool(
                self.settings, root, controller="pc-1")
            self.assertEqual(result["uploaded"], 0)
            self.assertEqual(result["already_present"], 1)
            self.assertEqual(mariadb.count(self.settings), 1)


if __name__ == "__main__":
    unittest.main()
