"""The MariaDB module's own behavior against a real MariaDB.

Skipped unless PXL_TEST_MARIADB points at a throwaway server, e.g.

    docker run -d --name pxl-mariadb-test -p 13306:3306 \\
      -e MARIADB_ROOT_PASSWORD=roottest -e MARIADB_DATABASE=proxmox_lab \\
      -e MARIADB_USER=proxmox_lab -e MARIADB_PASSWORD=labtest mariadb:11
    PXL_TEST_MARIADB=proxmox_lab:labtest@127.0.0.1:13306/proxmox_lab \\
      python3 -m unittest tests.test_mariadb

A stub cannot tell you that INSERT IGNORE really is idempotent or that
GET_LOCK really serialises two clients. Those are the properties the module
is built on, so they are checked against the real server.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import bootstrap  # noqa: E402,F401

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

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
        """Replaying the same event must land once, not twice."""
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


if __name__ == "__main__":
    unittest.main()
