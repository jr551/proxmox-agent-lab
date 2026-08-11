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
    def test_github_update_check_runs_at_most_once_per_day(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        response.read.return_value = b'{"tag_name":"v99.0.0"}'
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(LAB, "STATE_ROOT", Path(tmp)), \
             mock.patch.object(LAB.request, "urlopen", return_value=response) as open_url:
            first = LAB.check_for_updates(now=100_000)
            second = LAB.check_for_updates(now=100_001)
            third = LAB.check_for_updates(now=186_401)

        self.assertTrue(first["update_available"])
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertFalse(third["cached"])
        self.assertEqual(open_url.call_count, 2)

    def test_failed_update_check_is_cached_and_non_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(LAB, "STATE_ROOT", Path(tmp)), \
             mock.patch.object(LAB.request, "urlopen", side_effect=OSError("offline")) as open_url:
            first = LAB.check_for_updates(now=100_000)
            second = LAB.check_for_updates(now=100_100)

        self.assertEqual(first["error"], "github update check unavailable")
        self.assertTrue(second["cached"])
        open_url.assert_called_once()

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

    def test_standalone_power_on_requires_explicit_human_authorization(self) -> None:
        args = LAB.parser().parse_args(["power-on"])
        with mock.patch.object(LAB, "ensure_on") as ensure_on:
            with self.assertRaises(LAB.LabError) as caught:
                LAB.cmd_power_on(args)
        self.assertIn("Use lease-begin", str(caught.exception))
        ensure_on.assert_not_called()

    def test_reactos_recipe_is_machine_readable_and_lease_first(self) -> None:
        import contextlib
        import io
        import json

        args = LAB.parser().parse_args(["recipe", "reactos"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            args.func(args)
        recipe = json.loads(output.getvalue())
        self.assertEqual(recipe["release"], "0.4.15")
        self.assertEqual(len(recipe["archive"]["sha256"]), 64)
        self.assertEqual(recipe["qemu"]["firmware"], "seabios")
        self.assertEqual(recipe["qemu"]["machine"], "pc-i440fx")
        self.assertEqual(recipe["qemu"]["disk_bus"], "ide")
        self.assertEqual(recipe["qemu"]["network_model"], "e1000")
        self.assertFalse(recipe["qemu"]["guest_agent"])
        self.assertLess(
            recipe["phase_order"].index("installer-text-mode"),
            recipe["phase_order"].index("installer-gui"),
        )
        self.assertLess(
            recipe["phase_order"].index("api-create-qemu-and-register"),
            recipe["phase_order"].index("console-screenshot-or-inspect"),
        )
        self.assertIn("lease-begin", recipe["rules"][0])
        self.assertIn("Do not use console exec", recipe["invalid_shortcuts"][0])
        self.assertTrue(
            any("UEFI/OVMF" in shortcut for shortcut in recipe["invalid_shortcuts"])
        )

    def test_obscure_os_recipes_pin_media_and_keep_console_after_vm(self) -> None:
        import contextlib
        import io
        import json

        for name, filesystem in (
            ("dragonfly", "HAMMER"),
            ("haiku", "BFS"),
        ):
            args = LAB.parser().parse_args(["recipe", name])
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                args.func(args)
            recipe = json.loads(output.getvalue())
            self.assertEqual(recipe["filesystem"], filesystem)
            self.assertTrue(recipe["image"]["checksum"])
            self.assertLess(
                recipe["phase_order"].index("api-create-qemu-and-register"),
                recipe["phase_order"].index("console-screenshot-or-inspect"),
            )

    def test_openbsd_recipe_prevents_transcript_command_errors(self) -> None:
        import contextlib
        import io
        import json

        args = LAB.parser().parse_args(["recipe", "openbsd"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            args.func(args)
        recipe = json.loads(output.getvalue())
        self.assertEqual(recipe["release"], "7.9")
        self.assertEqual(recipe["image"]["content"], "iso")
        self.assertEqual(recipe["qemu"]["vmid_field"], "vmid")
        self.assertIn("console text", " ".join(recipe["invalid_shortcuts"]))

    def test_windows_me_recipe_requires_user_media_and_legacy_hardware(self) -> None:
        import contextlib
        import io
        import json

        args = LAB.parser().parse_args(["recipe", "windows-me"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            args.func(args)
        recipe = json.loads(output.getvalue())
        self.assertTrue(recipe["media"]["user_supplied"])
        self.assertIsNone(recipe["media"]["download"])
        self.assertEqual(recipe["qemu"]["firmware"], "SeaBIOS")
        self.assertEqual(recipe["qemu"]["disk"].split("=", 1)[0], "ide0")

    def test_checksum_prefix_selects_algorithm(self) -> None:
        from proxmox_agent_lab import storage

        digest = "ab" * 32
        self.assertEqual(
            storage._normalise_checksum(f"SHA256:{digest}", None),
            (digest, "sha256"),
        )
        with self.assertRaisesRegex(ValueError, "prefix says sha256"):
            storage._normalise_checksum(f"sha256:{digest}", "sha512")

    def test_checksum_accepts_sha1(self) -> None:
        from proxmox_agent_lab import storage

        digest = "ab" * 20  # a full 40-hex-character SHA-1 digest
        self.assertEqual(
            storage._normalise_checksum(f"sha1:{digest}", None),
            (digest, "sha1"),
        )
        self.assertEqual(
            storage._normalise_checksum(f"SHA1={digest}", None),
            (digest, "sha1"),
        )
        # A bare digest keeps the explicitly requested algorithm.
        self.assertEqual(
            storage._normalise_checksum(digest, "sha1"),
            (digest, "sha1"),
        )
        # The download-url parser accepts sha1 as an explicit choice too.
        args = LAB.parser().parse_args([
            "storage", "download-url",
            "--lease", "L1",
            "--url", "http://example.invalid/image.iso",
            "--filename", "image.iso",
            "--checksum", f"sha1:{digest}",
            "--checksum-algorithm", "sha1",
        ])
        self.assertEqual(args.checksum_algorithm, "sha1")

    def test_api_method_is_case_insensitive(self) -> None:
        self.assertEqual(
            LAB.parser().parse_args([
                "api", "--method", "get", "--path", "/version",
            ]).method,
            "GET",
        )

    def test_protocol_errors_are_rendered_without_tracebacks(self) -> None:
        from proxmox_agent_lab import rfb, ws

        self.assertIn(rfb.RFBError, LAB._EXPECTED_ERRORS)
        self.assertIn(ws.WebSocketError, LAB._EXPECTED_ERRORS)

    def test_sqlite_audit_can_export_redacted_jsonl_to_git(self) -> None:
        """The local query backend and remote logging transport are separate."""
        record = {"timestamp": "2026-08-08T12:00:00Z", "event": "test"}
        config = mock.Mock()
        config.audit.get.side_effect = lambda key, default=None: {
            "git_sync": True,
            "git_repo": "/tmp/dedicated-audit-repo",
            "git_branch": "logs",
        }.get(key, default)
        with mock.patch.object(LAB, "CONFIG", config), \
             mock.patch.object(LAB, "AUDIT_BACKEND", "sqlite"), \
             mock.patch.object(LAB.journal_module, "sync_git") as sync:
            LAB.sync_repo(record, "test")
        sync.assert_called_once_with(
            Path("/tmp/dedicated-audit-repo"), record, "test", "logs"
        )

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
