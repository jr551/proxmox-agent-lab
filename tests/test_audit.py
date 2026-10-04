"""The audit facade: one redacted row per action, and it never fails one."""
from __future__ import annotations

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

from proxmox_agent_lab import audit as audit_module  # noqa: E402
from proxmox_agent_lab import config as config_module  # noqa: E402
from proxmox_agent_lab import store as store_module  # noqa: E402


class AuditTests(unittest.TestCase):
    """Each call writes exactly one redacted event row -- or warns and moves on."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        patcher = mock.patch.object(
            config_module, "state_dir", return_value=self.root
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # The lazy per-process handle must not leak across tests -- close it
        # rather than dropping an open connection on the floor.
        def _close_cached_store() -> None:
            audit_module.close_store()

        audit_module.close_store()
        self.addCleanup(_close_cached_store)

    def rows(self) -> list[dict]:
        handle = store_module.Store(self.root / "lab.db")
        self.addCleanup(handle.close)
        return handle.query_events()

    def test_one_call_writes_one_row_with_the_legacy_columns(self) -> None:
        audit_module.audit("guest-clone", lease="lease-1", vmid=7, detail="x")
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(
            set(row), {"id", "timestamp", "event", "lease", "vmid", "data"}
        )
        self.assertEqual(row["event"], "guest-clone")
        self.assertEqual(row["lease"], "lease-1")
        self.assertEqual(row["vmid"], 7)
        self.assertTrue(row["timestamp"].endswith("Z"))
        data = json.loads(row["data"])
        self.assertEqual(data["actor"], "cli")
        self.assertEqual(data["tool"], "guest-clone")
        self.assertIs(data["ok"], True)
        self.assertIsNone(data["target"])
        self.assertEqual(data["detail"], "x")

    def test_the_caller_can_record_a_failed_action(self) -> None:
        audit_module.audit(
            "lab-power-off-unverified",
            lease="lease-1",
            ok=False,
            target="host",
            tool="shutdown",
        )
        data = json.loads(self.rows()[0]["data"])
        self.assertIs(data["ok"], False)
        self.assertEqual(data["target"], "host")
        self.assertEqual(data["tool"], "shutdown")

    def test_secrets_are_redacted_before_the_row_is_written(self) -> None:
        audit_module.audit(
            "guest-create",
            lease="lease-1",
            vmid=7,
            password="pw",
            ssh_key="key",
            endpoint="PVEAPI" + "Token=root-at-pam-deadbeef-cafe",
        )
        row = self.rows()[0]
        data = json.loads(row["data"])
        self.assertEqual(data["password"], "[REDACTED]")
        # audit's own key/value masking, not just the store's: `ssh_key` is
        # sensitive here, and a token embedded in a value is masked too.
        self.assertEqual(data["ssh_key"], "[REDACTED]")
        self.assertEqual(data["endpoint"], "[REDACTED]")
        self.assertNotIn("deadbeef", row["data"])
        self.assertIn("[REDACTED]", row["data"])

    def test_a_store_that_cannot_be_opened_never_fails_the_action(self) -> None:
        stderr = io.StringIO()
        with mock.patch.object(
            store_module, "Store", side_effect=OSError("no such device")
        ), contextlib.redirect_stderr(stderr):
            result = audit_module.audit("guest-clone", lease="lease-1", vmid=7)
        self.assertIsNone(result)
        self.assertIn("warning:", stderr.getvalue())
        self.assertIn("guest-clone", stderr.getvalue())

    def test_a_write_that_fails_never_fails_the_action(self) -> None:
        broken = mock.Mock()
        broken.record.side_effect = OSError("database is locked")
        stderr = io.StringIO()
        with mock.patch.object(audit_module, "_STORE", broken), \
             contextlib.redirect_stderr(stderr):
            result = audit_module.audit("guest-clone", lease="lease-1", vmid=7)
        self.assertIsNone(result)
        self.assertTrue(broken.record.called)
        self.assertIn("warning:", stderr.getvalue())

    def test_a_missing_config_never_fails_the_action(self) -> None:
        stderr = io.StringIO()
        with mock.patch.object(
            config_module, "state_dir",
            side_effect=config_module.ConfigError("config could not be read"),
        ), contextlib.redirect_stderr(stderr):
            result = audit_module.audit("guest-clone")
        self.assertIsNone(result)
        self.assertIn("warning:", stderr.getvalue())

    def test_redact_and_sensitive_key_keep_their_semantics(self) -> None:
        self.assertTrue(audit_module.SENSITIVE_KEY.search("password"))
        self.assertTrue(audit_module.SENSITIVE_KEY.search("ssh_keys"))
        self.assertFalse(audit_module.SENSITIVE_KEY.search("vmid"))
        self.assertEqual(audit_module.redact("value", "token"), "[REDACTED]")
        self.assertEqual(
            audit_module.redact({"a": {"b": 1}, "secret": "x"}),
            {"a": {"b": 1}, "secret": "[REDACTED]"},
        )
        self.assertEqual(audit_module.redact(["s"]), ["s"])
        self.assertEqual(
            audit_module.redact("PVEAPIToken=root@pam!x=y"), "[REDACTED]"
        )
        self.assertEqual(audit_module.redact("Bearer abc.def"), "[REDACTED]")
        self.assertEqual(len(audit_module.redact("x" * 2000)), 1000)
        self.assertEqual(audit_module.redact(5), 5)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
