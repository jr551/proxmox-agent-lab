from __future__ import annotations

import os
from pathlib import Path

# Point every module at a fixture config *before* importing the package:
# site values are read at import time.
os.environ["PROXMOX_AGENT_LAB_CONFIG"] = str(
    Path(__file__).parent / "fixtures" / "config.toml"
)

from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from proxmox_agent_lab import cli as LAB  # noqa: E402


class ProxmoxLabTests(unittest.TestCase):
    def test_cold_boot_timeout_uses_config_and_rejects_impatient_override(self) -> None:
        api = mock.Mock()
        api.reachable.return_value = False
        with mock.patch.object(LAB.power_module, "power_on") as power_on:
            with self.assertRaises(LAB.LabError) as caught:
                LAB.ensure_on(api, timeout=20)
        self.assertIn("at least 90s", str(caught.exception))
        power_on.assert_not_called()

        begin = LAB.parser().parse_args(["lease-begin", "--purpose", "check"])
        power = LAB.parser().parse_args(["power-on"])
        self.assertIsNone(begin.timeout)
        self.assertIsNone(power.timeout)

    def test_redacts_nested_secrets(self) -> None:
        value = {
            "ok": "visible",
            "token": "must-not-appear",
            "nested": {"cipassword": "must-not-appear"},
        }
        redacted = LAB.redact(value)
        self.assertEqual(redacted["ok"], "visible")
        self.assertEqual(redacted["token"], "[REDACTED]")
        self.assertEqual(redacted["nested"]["cipassword"], "[REDACTED]")

    def test_parse_data(self) -> None:
        self.assertEqual(
            LAB.parse_data(["vmid=9000", "name=test"]),
            {"vmid": "9000", "name": "test"},
        )
        with self.assertRaises(LAB.LabError):
            LAB.parse_data(["missing-separator"])

    def test_path_resource(self) -> None:
        self.assertEqual(
            LAB.path_resource(f"/nodes/{LAB.NODE}/qemu/9000/status/start"),
            ("qemu", 9000),
        )
        self.assertIsNone(LAB.path_resource("/nodes/somewhere-else/qemu/9000"))

    def test_lease_requires_cleanup(self) -> None:
        self.assertTrue(
            LAB.lease_requires_cleanup(
                {"resources": [{"kind": "qemu", "vmid": 100, "policy": "delete"}]}
            )
        )
        self.assertFalse(
            LAB.lease_requires_cleanup(
                {"resources": [{"kind": "qemu", "vmid": 101, "policy": "retain"}]}
            )
        )

    def test_mcp_idle_elapsed_uses_activity_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_state_root = LAB.STATE_ROOT
            LAB.STATE_ROOT = Path(tmp)
            try:
                LAB.json_dump(
                    LAB.mcp_activity_path(),
                    {
                        "last_command_at": "2026-08-02T00:00:00Z",
                        "tool": "route_tools",
                    },
                )
                now = LAB.dt.datetime(
                    2026, 8, 2, 8, 0, 1, tzinfo=LAB.dt.timezone.utc
                )
                self.assertEqual(LAB.mcp_idle_elapsed(now), 28_801)
            finally:
                LAB.STATE_ROOT = old_state_root

    def test_record_mcp_activity_does_not_record_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_state_root = LAB.STATE_ROOT
            LAB.STATE_ROOT = Path(tmp)
            try:
                with mock.patch.object(LAB, "audit") as audit:
                    LAB.record_mcp_activity("route_tools")
                activity = LAB.json.loads(LAB.mcp_activity_path().read_text())
                self.assertEqual(activity["tool"], "route_tools")
                audit.assert_called_once()
            finally:
                LAB.STATE_ROOT = old_state_root

    def test_idle_shutdown_boundary_and_guards(self) -> None:
        self.assertFalse(
            LAB.idle_shutdown_due(
                reachable=True,
                active_lease_count=0,
                has_failures=False,
                idle_seconds=28_799,
            )
        )
        self.assertTrue(
            LAB.idle_shutdown_due(
                reachable=True,
                active_lease_count=0,
                has_failures=False,
                idle_seconds=28_800,
            )
        )
        self.assertFalse(
            LAB.idle_shutdown_due(
                reachable=True,
                active_lease_count=1,
                has_failures=False,
                idle_seconds=99_999,
            )
        )
        self.assertFalse(
            LAB.idle_shutdown_due(
                reachable=True,
                active_lease_count=0,
                has_failures=True,
                idle_seconds=99_999,
            )
        )

    def test_atomic_lease_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_root = LAB.LEASE_ROOT
            LAB.LEASE_ROOT = Path(tmp)
            try:
                lease = {"id": "20260802120000-deadbeef", "state": "active"}
                LAB.save_lease(lease)
                self.assertEqual(LAB.load_lease(lease["id"]), lease)
            finally:
                LAB.LEASE_ROOT = old_root


if __name__ == "__main__":
    unittest.main()
