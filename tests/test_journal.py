"""The journal: reading events back out of the local lab store."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import bootstrap  # noqa: E402,F401
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from proxmox_agent_lab import config as config_module  # noqa: E402
from proxmox_agent_lab import journal as journal_module  # noqa: E402
from proxmox_agent_lab import store as store_module  # noqa: E402


class JournalTests(unittest.TestCase):
    """Query filters, ordering, output formatting -- and nothing else."""

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

    def test_events_come_back_newest_first(self) -> None:
        for i in range(3):
            self.store.record(
                f"e{i}", timestamp=f"2026-01-0{i + 1}T00:00:00Z"
            )
        rows = journal_module.query_events()
        self.assertEqual([row["event"] for row in rows], ["e2", "e1", "e0"])
        self.assertEqual(
            set(rows[0]), {"id", "timestamp", "event", "lease", "vmid", "data"}
        )

    def test_lease_since_and_limit_filter_the_query(self) -> None:
        self.store.record("a", lease="L1", timestamp="2026-01-01T00:00:00Z")
        self.store.record("b", lease="L1", timestamp="2026-01-02T00:00:00Z")
        self.store.record("c", lease="L2", timestamp="2026-01-03T00:00:00Z")
        self.assertEqual(
            [row["event"] for row in journal_module.query_events(lease="L1")],
            ["b", "a"],
        )
        self.assertEqual(
            [row["event"] for row in journal_module.query_events(
                since="2026-01-02T00:00:00Z")],
            ["c", "b"],
        )
        self.assertEqual(
            [row["event"] for row in journal_module.query_events(limit=1)],
            ["c"],
        )

    def test_format_events_round_trips_the_rows(self) -> None:
        self.store.record(
            "guest-clone", lease="L1", vmid=7, data={"ok": True, "detail": "x"}
        )
        rows = journal_module.query_events()
        self.assertEqual(json.loads(journal_module.format_events(rows)), rows)
        self.assertEqual(journal_module.format_events([]), "[]")

    def test_a_legacy_journal_db_is_not_auto_imported(self) -> None:
        """Fresh start (§C): an old journal.db is never read, even sitting
        exactly where the store lives."""
        legacy = sqlite3.connect(self.root / "journal.db")
        self.addCleanup(legacy.close)
        legacy.execute(
            "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "timestamp TEXT, event TEXT, lease TEXT, vmid INTEGER, data TEXT)"
        )
        legacy.execute(
            "INSERT INTO events (timestamp, event, lease, vmid, data) "
            "VALUES ('2020-01-01T00:00:00Z', 'legacy', 'L0', 1, '{}')"
        )
        legacy.commit()
        self.assertEqual(journal_module.query_events(), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
