from __future__ import annotations

import os
import io
import json
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
from proxmox_agent_lab import guest as GUEST  # noqa: E402


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

    def test_update_cache_is_invalidated_after_controller_upgrade(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        response.read.return_value = b'{"tag_name":"v0.6.0"}'
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(LAB, "STATE_ROOT", Path(tmp)), \
             mock.patch.object(LAB.request, "urlopen", return_value=response) as open_url:
            with mock.patch.object(LAB, "__version__", "0.5.3"):
                first = LAB.check_for_updates(now=100_000)
            with mock.patch.object(LAB, "__version__", "0.6.2"):
                second = LAB.check_for_updates(now=100_001)

        self.assertTrue(first["update_available"])
        self.assertFalse(second["update_available"])
        self.assertFalse(second["cached"])
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

    def test_guest_agent_http_596_explains_recovery_options(self) -> None:
        import io

        failure = LAB.error.HTTPError(
            "https://proxmox.invalid/api2/json/nodes/test/qemu/100/agent/exec",
            596,
            "guest agent unavailable",
            None,
            io.BytesIO(b"guest agent unavailable"),
        )
        api = LAB.ProxmoxAPI()
        try:
            with mock.patch.object(LAB, "keychain_secret", return_value="token"), \
                 mock.patch.object(LAB.request, "urlopen", side_effect=failure):
                with self.assertRaises(LAB.LabError) as caught:
                    api.call("POST", "/nodes/test/qemu/100/agent/exec")
        finally:
            failure.close()

        message = str(caught.exception)
        self.assertIn("guest agent is not responding", message)
        self.assertIn("guest may be hung or its storage offline", message)
        self.assertIn("console screenshot or serial", message)

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

    def test_android_x86_recipe_pins_media_and_avoids_the_isolated_bridge(self) -> None:
        import contextlib
        import io
        import json

        args = LAB.parser().parse_args(["recipe", "android-x86"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            args.func(args)
        recipe = json.loads(output.getvalue())
        self.assertEqual(recipe["release"], "9.0-r2")
        self.assertEqual(recipe["image"]["checksum_algorithm"], "md5")
        self.assertEqual(len(recipe["image"]["checksum"]), 32)
        self.assertFalse(recipe["qemu"]["guest_agent"])
        self.assertIn("vmbr0", recipe["qemu"]["network_bridge"])
        self.assertTrue(
            any("isolated" in shortcut for shortcut in recipe["invalid_shortcuts"])
        )
        self.assertTrue(
            any("Burp" in shortcut for shortcut in recipe["invalid_shortcuts"])
        )
        self.assertIn("http_proxy", recipe["proxying"]["set_proxy"])

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

    def test_cmd_api_refuses_power_actions_for_unregistered_preexisting_guest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_lease_root = LAB.LEASE_ROOT
            LAB.LEASE_ROOT = Path(tmp) / "leases"
            try:
                lease_id = "20260814120000-guard35"
                LAB.save_lease({
                    "id": lease_id,
                    "state": "active",
                    "kind": "standard",
                    "resources": [],
                    "initial_vmids": [9000, 9001],
                })
                api = mock.Mock()
                operations = [
                    ("qemu", 9000, action)
                    for action in ("start", "stop", "shutdown", "reset", "suspend")
                ]
                operations.append(("lxc", 9001, "start"))
                with mock.patch.object(LAB, "ProxmoxAPI", return_value=api):
                    for kind, vmid, action in operations:
                        with self.subTest(kind=kind, action=action):
                            args = LAB.parser().parse_args([
                                "api", "--lease", lease_id, "--method", "POST",
                                "--path",
                                f"/nodes/{LAB.NODE}/{kind}/{vmid}/status/{action}",
                            ])
                            with self.assertRaisesRegex(
                                LAB.LabError,
                                rf"VMID {vmid} existed before this lease",
                            ):
                                LAB.cmd_api(args)
                            api.call.assert_not_called()
            finally:
                LAB.LEASE_ROOT = old_lease_root

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

    def test_guest_mutation_requires_registered_resource(self) -> None:
        lease = {"resources": [], "initial_vmids": []}
        args = LAB.parser().parse_args([
            "api", "--lease", "lease-1", "--method", "POST",
            "--path", f"/nodes/{LAB.NODE}/qemu/9000/status/start",
        ])
        api = mock.Mock()
        with mock.patch.object(LAB, "ProxmoxAPI", return_value=api), \
             mock.patch.object(LAB, "load_lease", return_value=lease):
            with self.assertRaisesRegex(LAB.LabError, "not a qemu guest"):
                LAB.cmd_api(args)
        api.call.assert_not_called()

    def test_audit_intent_blocks_write_before_api_call(self) -> None:
        lease = {
            "resources": [{"kind": "qemu", "vmid": 9000}],
            "initial_vmids": [],
        }
        args = LAB.parser().parse_args([
            "api", "--lease", "lease-1", "--method", "POST",
            "--path", f"/nodes/{LAB.NODE}/qemu/9000/status/start",
        ])
        api = mock.Mock()
        with mock.patch.object(LAB, "ProxmoxAPI", return_value=api), \
             mock.patch.object(LAB, "load_lease", return_value=lease), \
             mock.patch.object(LAB, "audit", side_effect=LAB.LabError("ledger down")):
            with self.assertRaisesRegex(LAB.LabError, "ledger down"):
                LAB.cmd_api(args)
        api.call.assert_not_called()

    def test_completion_audit_failure_reports_successful_write(self) -> None:
        lease = {
            "resources": [{"kind": "qemu", "vmid": 9000}],
            "initial_vmids": [],
        }
        args = LAB.parser().parse_args([
            "api", "--lease", "lease-1", "--method", "POST",
            "--path", f"/nodes/{LAB.NODE}/qemu/9000/status/start",
        ])
        api = mock.Mock()
        api.call.return_value = "UPID:success"
        output = io.StringIO()
        with mock.patch.object(LAB, "ProxmoxAPI", return_value=api), \
             mock.patch.object(LAB, "load_lease", return_value=lease), \
             mock.patch.object(LAB, "audit", side_effect=[None, OSError("disk full")]), \
             mock.patch("sys.stdout", output):
            LAB.cmd_api(args)
        report = LAB.json.loads(output.getvalue())
        self.assertTrue(report["operation_succeeded"])
        self.assertEqual(report["audit_recording_failed"], "disk full")

    def test_version_flag_reports_controller_version(self) -> None:
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            with self.assertRaises(SystemExit) as caught:
                LAB.parser().parse_args(["--version"])
        self.assertEqual(caught.exception.code, 0)
        self.assertEqual(output.getvalue().strip(), LAB.__version__)

    def test_guest_run_preserves_argv_for_agent_channel(self) -> None:
        session = object.__new__(GUEST.GuestSession)
        session.channel = "agent"
        session.lab = mock.Mock()
        session.api = mock.Mock()
        session.vmid = 9000
        command = ["printf", "%s", "one value", "two;value"]
        with mock.patch.object(
            GUEST.console, "agent_exec",
            return_value={"stdout": "", "stderr": "", "exitcode": 0},
        ) as execute:
            session.run_argv(command)
        self.assertEqual(execute.call_args.args[3], command)
    def test_lease_begin_rolls_back_saved_lease_when_audit_fails(self) -> None:
        import contextlib
        import io

        args = LAB.parser().parse_args(["lease-begin", "--purpose", "rollback"])
        audit_error = LAB.LabError("audit backend rejected the event")
        api = mock.Mock()
        api.call.return_value = []
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            lease_root = state_root / "leases"
            with mock.patch.object(LAB, "STATE_ROOT", state_root), \
                 mock.patch.object(LAB, "LEASE_ROOT", lease_root), \
                 mock.patch.object(LAB, "LOCK_PATH", state_root / "controller.lock"), \
                 mock.patch.object(LAB, "ProxmoxAPI", return_value=api), \
                 mock.patch.object(LAB, "ensure_on", return_value=False), \
                 mock.patch.object(LAB.secrets, "token_hex", return_value="deadbeef"), \
                 mock.patch.object(LAB, "audit", side_effect=audit_error), \
                 contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(LAB.LabError) as caught:
                    LAB.cmd_lease_begin(args)

                self.assertIs(caught.exception, audit_error)
                self.assertEqual(LAB.active_leases(), [])
                self.assertEqual(list(lease_root.glob("*.json")), [])
                api.call.assert_called_once_with(
                    "GET", "/cluster/resources", {"type": "vm"}
                )
    def test_pocketbase_audit_auth_rejection_spools_the_event(self) -> None:
        """Losing audit credentials mid-session must not abort the action:
        the event is spooled locally for a later 'journal --flush-spool'."""
        import contextlib
        import io

        rejected = LAB.pocketbase_module.PocketBaseError(
            "PocketBase HTTP 403: Only superusers can perform this action.",
            status=403,
        )
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(LAB, "AUDIT_BACKEND", "pocketbase"), \
                 mock.patch.object(LAB, "JOURNAL_ROOT", Path(tmp)), \
                 mock.patch.object(LAB, "pocketbase_client"), \
                 mock.patch.object(
                     LAB.journal_module, "append", side_effect=rejected,
                 ), \
                 contextlib.redirect_stderr(stderr):
                LAB.audit("lease-begin", sync=False)
            spool = Path(tmp) / "spool.jsonl"
            self.assertTrue(spool.exists())
            record = json.loads(spool.read_text().splitlines()[0])
        self.assertEqual(record["event"], "lease-begin")
        self.assertIn("event_id", record)
        self.assertIn("--flush-spool", stderr.getvalue())

    def test_pocketbase_audit_expired_token_spools_instead_of_failing(self) -> None:
        import contextlib
        import io

        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(LAB, "AUDIT_BACKEND", "pocketbase"), \
                 mock.patch.object(LAB, "JOURNAL_ROOT", Path(tmp)), \
                 mock.patch.object(
                     LAB, "pocketbase_client",
                     side_effect=LAB.LabError("token is expired"),
                 ), \
                 mock.patch.object(LAB.journal_module, "append") as append, \
                 contextlib.redirect_stderr(stderr):
                LAB.audit("guest-exec", sync=False)
            append.assert_not_called()
            self.assertTrue((Path(tmp) / "spool.jsonl").exists())
        self.assertIn("token is expired", stderr.getvalue())

    def test_journal_flush_spool_uploads_and_clears_the_backlog(self) -> None:
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as tmp:
            spool = Path(tmp) / "spool.jsonl"
            spool.write_text(
                '{"event": "a", "event_id": "1", "timestamp": "t"}\n'
                '{"event": "b", "event_id": "2", "timestamp": "t"}\n'
            )
            args = LAB.parser().parse_args(["journal", "--flush-spool"])
            stdout = io.StringIO()
            with mock.patch.object(LAB, "AUDIT_BACKEND", "pocketbase"), \
                 mock.patch.object(LAB, "JOURNAL_ROOT", Path(tmp)), \
                 mock.patch.object(LAB, "pocketbase_client"), \
                 mock.patch.object(LAB.journal_module, "append") as append, \
                 contextlib.redirect_stdout(stdout):
                LAB.cmd_journal(args)
            self.assertEqual(append.call_count, 2)
            self.assertFalse(spool.exists())
            result = json.loads(stdout.getvalue())
        self.assertEqual(result["uploaded"], 2)
        self.assertEqual(result["remaining"], 0)

    def test_journal_flush_spool_keeps_events_after_a_hard_failure(self) -> None:
        import contextlib
        import io

        boom = LAB.pocketbase_module.PocketBaseError("down", status=500)
        with tempfile.TemporaryDirectory() as tmp:
            spool = Path(tmp) / "spool.jsonl"
            spool.write_text(
                '{"event": "a", "event_id": "1", "timestamp": "t"}\n'
                '{"event": "b", "event_id": "2", "timestamp": "t"}\n'
            )
            args = LAB.parser().parse_args(["journal", "--flush-spool"])
            stdout = io.StringIO()
            with mock.patch.object(LAB, "AUDIT_BACKEND", "pocketbase"), \
                 mock.patch.object(LAB, "JOURNAL_ROOT", Path(tmp)), \
                 mock.patch.object(LAB, "pocketbase_client"), \
                 mock.patch.object(
                     LAB.journal_module, "append", side_effect=boom,
                 ), \
                 contextlib.redirect_stdout(stdout):
                with self.assertRaises(LAB.LabError):
                    LAB.cmd_journal(args)
            self.assertEqual(len(spool.read_text().splitlines()), 2)

    def test_cmd_api_create_registers_under_lock_with_reloaded_lease(self) -> None:
        """A registration racing the create must survive: cmd_api reloads the
        lease inside controller_lock instead of saving the stale snapshot."""
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as tmp:
            old_lease_root = LAB.LEASE_ROOT
            old_state_root = LAB.STATE_ROOT
            LAB.LEASE_ROOT = Path(tmp) / "leases"
            LAB.STATE_ROOT = Path(tmp) / "state"
            try:
                lease_id = "20260811120000-race01"
                LAB.save_lease({
                    "id": lease_id,
                    "state": "active",
                    "kind": "standard",
                    "resources": [],
                    "initial_vmids": [],
                })

                api = mock.Mock()

                def create_and_concurrent_register(method, path, data=None):
                    # While the create request is in flight, another process
                    # registers a guest; the stale-snapshot save used to
                    # clobber this entry.
                    concurrent = LAB.load_lease(lease_id)
                    concurrent["resources"].append(
                        {"kind": "qemu", "vmid": 9100,
                         "policy": "delete", "name": "other"}
                    )
                    LAB.save_lease(concurrent)
                    return {"vmid": 9090}

                api.call.side_effect = create_and_concurrent_register
                args = LAB.parser().parse_args([
                    "api", "--lease", lease_id, "--method", "POST",
                    "--path", f"/nodes/{LAB.NODE}/qemu",
                    "--data", "vmid=9090", "--data", "name=created",
                ])
                with mock.patch.object(LAB, "ProxmoxAPI", return_value=api), \
                     mock.patch.object(LAB, "audit"):
                    with contextlib.redirect_stdout(io.StringIO()):
                        LAB.cmd_api(args)

                final = LAB.load_lease(lease_id)
                vmids = sorted(int(r["vmid"]) for r in final["resources"])
                self.assertEqual(vmids, [9090, 9100])
            finally:
                LAB.LEASE_ROOT = old_lease_root
                LAB.STATE_ROOT = old_state_root

    def test_cmd_api_warns_when_pve_reorders_requested_boot(self) -> None:
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as tmp:
            old_lease_root = LAB.LEASE_ROOT
            LAB.LEASE_ROOT = Path(tmp) / "leases"
            try:
                lease_id = "20260811120000-boot01"
                LAB.save_lease({
                    "id": lease_id,
                    "state": "active",
                    "kind": "standard",
                    "resources": [{"kind": "qemu", "vmid": 9092}],
                    "initial_vmids": [],
                })
                api = mock.Mock()
                api.call.side_effect = lambda method, path, data=None: (
                    None if method == "PUT" else {"boot": "order=ide0;ide2"}
                )
                args = LAB.parser().parse_args([
                    "api", "--lease", lease_id, "--method", "PUT",
                    "--path", f"/nodes/{LAB.NODE}/qemu/9092/config",
                    "--data", "ide2=usb-bulk:iso/x.iso,media=cdrom",
                    "--data", "boot=order=ide2;ide0",
                ])
                stdout, stderr = io.StringIO(), io.StringIO()
                with mock.patch.object(LAB, "ProxmoxAPI", return_value=api), \
                     mock.patch.object(LAB, "audit"), \
                     contextlib.redirect_stdout(stdout), \
                     contextlib.redirect_stderr(stderr):
                    LAB.cmd_api(args)
                self.assertEqual(
                    LAB.json.loads(stdout.getvalue()),
                    {"data": None, "task_status": None},
                )
                self.assertIn(
                    "PVE persisted boot order 'ide0;ide2' instead of "
                    "requested 'ide2;ide0'",
                    stderr.getvalue(),
                )
                self.assertIn("separate calls", stderr.getvalue())
            finally:
                LAB.LEASE_ROOT = old_lease_root


    def test_cmd_api_boot_readback_is_silent_when_matching_or_unreadable(self) -> None:
        import contextlib
        import io

        for persisted_boot, expect_warning in (
            ("order=IDE2;ide0", False),  # same order, different case
            ("order=ide0;ide2", True),   # PVE reordered the devices
            (None, False),               # read-back failed -> skip warning
        ):
            with self.subTest(persisted=persisted_boot):
                with tempfile.TemporaryDirectory() as tmp:
                    old_lease_root = LAB.LEASE_ROOT
                    LAB.LEASE_ROOT = Path(tmp) / "leases"
                    try:
                        lease_id = "20260811120000-boot02"
                        LAB.save_lease({
                            "id": lease_id,
                            "state": "active",
                            "kind": "standard",
                            "resources": [{"kind": "qemu", "vmid": 9092}],
                            "initial_vmids": [],
                        })
                        api = mock.Mock()

                        def api_call(method, path, data=None):
                            if method == "PUT":
                                return None
                            if persisted_boot is None:
                                raise LAB.LabError("read-back failed")
                            return {"boot": persisted_boot}

                        api.call.side_effect = api_call
                        args = LAB.parser().parse_args([
                            "api", "--lease", lease_id, "--method", "PUT",
                            "--path", f"/nodes/{LAB.NODE}/qemu/9092/config",
                            "--data", "boot=order=ide2;ide0",
                        ])
                        stdout, stderr = io.StringIO(), io.StringIO()
                        with mock.patch.object(
                            LAB, "ProxmoxAPI", return_value=api
                        ), mock.patch.object(LAB, "audit"), \
                                contextlib.redirect_stdout(stdout), \
                                contextlib.redirect_stderr(stderr):
                            LAB.cmd_api(args)
                        if expect_warning:
                            self.assertIn(
                                "PVE persisted boot order", stderr.getvalue()
                            )
                        else:
                            self.assertEqual(stderr.getvalue(), "")
                        self.assertEqual(
                            LAB.json.loads(stdout.getvalue()),
                            {"data": None, "task_status": None},
                        )
                    finally:
                        LAB.LEASE_ROOT = old_lease_root


    def test_cmd_api_boot_accepts_bare_form_without_order_prefix(self) -> None:
        """PVE documents boot as [order=]dev;dev — a bare value must not warn."""
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as tmp:
            old_lease_root = LAB.LEASE_ROOT
            LAB.LEASE_ROOT = Path(tmp) / "leases"
            try:
                lease_id = "20260811120000-boot03"
                LAB.save_lease({
                    "id": lease_id,
                    "state": "active",
                    "kind": "standard",
                    "resources": [{"kind": "qemu", "vmid": 9092}],
                    "initial_vmids": [],
                })
                api = mock.Mock()
                api.call.side_effect = lambda method, path, data=None: (
                    None if method == "PUT" else {"boot": "order=ide2;ide0"}
                )
                args = LAB.parser().parse_args([
                    "api", "--lease", lease_id, "--method", "PUT",
                    "--path", f"/nodes/{LAB.NODE}/qemu/9092/config",
                    "--data", "boot=ide2;ide0",
                ])
                stdout, stderr = io.StringIO(), io.StringIO()
                with mock.patch.object(LAB, "ProxmoxAPI", return_value=api), \
                     mock.patch.object(LAB, "audit"), \
                     contextlib.redirect_stdout(stdout), \
                     contextlib.redirect_stderr(stderr):
                    LAB.cmd_api(args)
                self.assertEqual(stderr.getvalue(), "")
            finally:
                LAB.LEASE_ROOT = old_lease_root



    def test_lease_end_hints_on_rapid_reuse(self) -> None:
        """A lease ended seconds after it began gets a reuse hint."""
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as tmp:
            old_lease_root = LAB.LEASE_ROOT
            LAB.LEASE_ROOT = Path(tmp) / "leases"
            try:
                lease_id = "20260811120000-short01"
                LAB.save_lease({
                    "id": lease_id, "state": "active", "kind": "standard",
                    "created_at": LAB.iso_now(), "resources": [],
                    "initial_vmids": [],
                })
                args = LAB.parser().parse_args(
                    ["lease-end", "--lease", lease_id]
                )
                stdout, stderr = io.StringIO(), io.StringIO()
                with mock.patch.object(LAB, "ProxmoxAPI"), \
                        mock.patch.object(LAB, "finalize_lease",
                                          return_value=[]), \
                        mock.patch.object(LAB, "active_leases",
                                          return_value=[]), \
                        mock.patch.object(LAB, "shutdown_host",
                                          return_value=True), \
                        mock.patch.object(LAB, "audit"), \
                        contextlib.redirect_stdout(stdout), \
                        contextlib.redirect_stderr(stderr):
                    LAB.cmd_lease_end(args)
                self.assertIn("hint: lease", stderr.getvalue())
                self.assertIn("ended after", stderr.getvalue())
                self.assertIn("lease-heartbeat", stderr.getvalue())
            finally:
                LAB.LEASE_ROOT = old_lease_root

    def test_lease_abandon_closes_stopped_ordinary_lease_without_mutation(self) -> None:
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as tmp:
            old_root, old_lock = LAB.LEASE_ROOT, LAB.LOCK_PATH
            LAB.LEASE_ROOT = Path(tmp) / "leases"
            LAB.LOCK_PATH = Path(tmp) / "controller.lock"
            try:
                lease_id = "20260811120000-stale01"
                LAB.save_lease({
                    "id": lease_id,
                    "state": "active",
                    "kind": "session",
                    "resources": [
                        {"kind": "qemu", "vmid": 9201, "policy": "delete"},
                    ],
                })
                api = mock.Mock()
                api.reachable.return_value = True
                api.call.return_value = {"status": "stopped"}
                args = LAB.parser().parse_args([
                    "lease-abandon", "--lease", lease_id, "--confirm",
                ])
                stdout = io.StringIO()
                with mock.patch.object(LAB, "ProxmoxAPI", return_value=api), \
                        mock.patch.object(LAB, "audit") as audit, \
                        contextlib.redirect_stdout(stdout):
                    LAB.cmd_lease_abandon(args)
                final = LAB.load_lease(lease_id, active=False)
                self.assertEqual(final["state"], "closed")
                self.assertEqual(
                    final["abandoned_reason"],
                    "registered guests verified stopped; no guest or host mutation",
                )
                self.assertEqual(
                    api.call.call_args_list,
                    [mock.call(
                        "GET",
                        f"/nodes/{LAB.NODE}/qemu/9201/status/current",
                    )],
                )
                audit.assert_called_once_with(
                    "lease-abandon",
                    lease=lease_id,
                    stopped=["qemu/9201"],
                    missing=[],
                    reason=final["abandoned_reason"],
                )
                result = LAB.json.loads(stdout.getvalue())
                self.assertEqual(result["guests_verified_stopped"], ["qemu/9201"])
                self.assertFalse(result["guest_mutation"])
                self.assertFalse(result["host_mutation"])
                self.assertTrue(result["audit_recorded"])
            finally:
                LAB.LEASE_ROOT, LAB.LOCK_PATH = old_root, old_lock

    def test_lease_abandon_refuses_long_term_or_running_guest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_root, old_lock = LAB.LEASE_ROOT, LAB.LOCK_PATH
            LAB.LEASE_ROOT = Path(tmp) / "leases"
            LAB.LOCK_PATH = Path(tmp) / "controller.lock"
            try:
                for kind, status in (("long-term", None), ("session", "running")):
                    with self.subTest(kind=kind, status=status):
                        lease_id = (
                            "20260811120000-longterm"
                            if kind == "long-term"
                            else "20260811120000-running1"
                        )
                        LAB.save_lease({
                            "id": lease_id,
                            "state": "active",
                            "kind": kind,
                            "resources": [
                                {"kind": "qemu", "vmid": 9202, "policy": "delete"},
                            ],
                        })
                        api = mock.Mock()
                        api.reachable.return_value = True
                        api.call.return_value = {"status": status}
                        args = LAB.parser().parse_args([
                            "lease-abandon", "--lease", lease_id, "--confirm",
                        ])
                        with mock.patch.object(
                            LAB, "ProxmoxAPI", return_value=api
                        ), self.assertRaises(LAB.LabError):
                            LAB.cmd_lease_abandon(args)
                        self.assertEqual(
                            LAB.load_lease(lease_id, active=False)["state"],
                            "active",
                        )
                        if kind == "long-term":
                            api.reachable.assert_not_called()
                        else:
                            api.call.assert_called_once()
            finally:
                LAB.LEASE_ROOT, LAB.LOCK_PATH = old_root, old_lock

    def test_lease_abandon_reports_audit_failure_after_closing_lease(self) -> None:
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as tmp:
            old_root, old_lock = LAB.LEASE_ROOT, LAB.LOCK_PATH
            LAB.LEASE_ROOT = Path(tmp) / "leases"
            LAB.LOCK_PATH = Path(tmp) / "controller.lock"
            try:
                lease_id = "20260811120000-audit001"
                LAB.save_lease({
                    "id": lease_id,
                    "state": "active",
                    "kind": "session",
                    "resources": [],
                })
                api = mock.Mock()
                api.reachable.return_value = True
                args = LAB.parser().parse_args([
                    "lease-abandon", "--lease", lease_id, "--confirm",
                ])
                stdout, stderr = io.StringIO(), io.StringIO()
                with mock.patch.object(LAB, "ProxmoxAPI", return_value=api), \
                        mock.patch.object(
                            LAB, "audit", side_effect=LAB.LabError("audit denied")
                        ), contextlib.redirect_stdout(stdout), \
                        contextlib.redirect_stderr(stderr):
                    LAB.cmd_lease_abandon(args)
                self.assertEqual(
                    LAB.load_lease(lease_id, active=False)["state"], "closed"
                )
                result = LAB.json.loads(stdout.getvalue())
                self.assertFalse(result["audit_recorded"])
                self.assertEqual(result["audit_error"], "audit denied")
                self.assertIn("could not be recorded: audit denied", stderr.getvalue())
            finally:
                LAB.LEASE_ROOT, LAB.LOCK_PATH = old_root, old_lock

    def test_delete_guest_treats_already_gone_as_success(self) -> None:
        api = mock.Mock()
        api.call.side_effect = LAB.LabError(
            "Proxmox HTTP 500 for DELETE /nodes/pve/qemu/9101: "
            '{"data":null,"message":"Configuration file '
            "'nodes/pve/qemu-server/9101.conf' does not exist\\n\"}"
        )
        LAB.delete_guest(api, "qemu", 9101)  # must not raise

    def test_delete_guest_treats_404_as_success(self) -> None:
        api = mock.Mock()
        api.call.side_effect = LAB.LabError(
            "Proxmox HTTP 404 for DELETE /nodes/pve/qemu/9101: not found"
        )
        LAB.delete_guest(api, "qemu", 9101)  # must not raise

    def test_delete_guest_still_raises_on_a_real_failure(self) -> None:
        api = mock.Mock()
        api.call.side_effect = LAB.LabError(
            "Proxmox HTTP 500 for DELETE /nodes/pve/qemu/9101: storage locked"
        )
        with self.assertRaises(LAB.LabError):
            LAB.delete_guest(api, "qemu", 9101)

    def test_lease_end_retries_without_unreferenced_disks_on_storage_io_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_lease_root = LAB.LEASE_ROOT
            LAB.LEASE_ROOT = Path(tmp) / "leases"
            try:
                lease_id = "20260814120000-storage38"
                LAB.save_lease({
                    "id": lease_id, "state": "active", "kind": "session",
                    "created_at": LAB.iso_now(),
                    "resources": [{"kind": "qemu", "vmid": 9038}],
                    "initial_vmids": [],
                })
                delete_data: list[dict[str, int]] = []
                api = mock.Mock()
                api.reachable.return_value = True

                def call(method, path, data=None):
                    if path.endswith("/status/current"):
                        return {"status": "stopped"}
                    if method == "DELETE":
                        delete_data.append(data)
                        if data.get("destroy-unreferenced-disks"):
                            raise LAB.LabError(
                                "failed to create content directory "
                                "'/mnt/pve/offline/dump': Input/output error"
                            )
                        return "UPID:delete-without-unreferenced-disks"
                    self.fail(f"unexpected Proxmox API call: {method} {path}")

                api.call.side_effect = call
                args = LAB.parser().parse_args(["lease-end", "--lease", lease_id])
                with mock.patch.object(LAB, "ProxmoxAPI", return_value=api), \
                     mock.patch.object(LAB, "wait_task"), \
                     mock.patch.object(LAB, "shutdown_host", return_value=True) as shutdown, \
                     mock.patch.object(LAB, "audit"):
                    LAB.cmd_lease_end(args)

                self.assertEqual(
                    delete_data,
                    [
                        {"purge": 1, "destroy-unreferenced-disks": 1},
                        {"purge": 1},
                    ],
                )
                self.assertEqual(LAB.load_lease(lease_id, active=False)["state"], "closed")
                shutdown.assert_called_once_with(api)
            finally:
                LAB.LEASE_ROOT = old_lease_root

    def test_running_guest_vmids_filters_to_running_status(self) -> None:
        api = mock.Mock()
        api.call.return_value = [
            {"vmid": 100, "status": "stopped"},
            {"vmid": 9112, "status": "running"},
            {"vmid": 9210, "status": "running"},
        ]
        self.assertEqual(LAB.running_guest_vmids(api), [9112, 9210])

    def test_shutdown_host_refuses_while_a_guest_is_running(self) -> None:
        api = mock.Mock()
        api.reachable.return_value = True
        api.call.return_value = [{"vmid": 9112, "status": "running"}]
        with mock.patch.object(LAB, "audit") as audit:
            result = LAB.shutdown_host(api)
        self.assertFalse(result)
        api.call.assert_called_once_with(
            "GET", "/cluster/resources", {"type": "vm"}
        )
        audit.assert_called_once()
        self.assertEqual(audit.call_args[0][0],
                         "lab-power-off-blocked-by-running-guest")
        self.assertEqual(audit.call_args[1]["vmids"], [9112])

    def test_shutdown_host_proceeds_when_nothing_is_running(self) -> None:
        api = mock.Mock()
        api.reachable.side_effect = [True, False, False]
        api.call.return_value = []
        with mock.patch.object(LAB, "audit"), \
             mock.patch.object(LAB, "time") as fake_time:
            fake_time.monotonic.side_effect = [0, 1, 2, 3]
            fake_time.sleep.return_value = None
            result = LAB.shutdown_host(api)
        self.assertTrue(result)

    def test_lease_end_reports_an_unleased_running_guest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_root = LAB.LEASE_ROOT
            LAB.LEASE_ROOT = Path(tmp) / "leases"
            try:
                lease_id = "20260811120000-short02"
                LAB.save_lease({
                    "id": lease_id, "state": "active", "kind": "standard",
                    "created_at": LAB.iso_now(), "resources": [],
                    "initial_vmids": [],
                })
                args = LAB.parser().parse_args(
                    ["lease-end", "--lease", lease_id]
                )
                api = mock.Mock()
                api.reachable.return_value = True
                api.call.return_value = [{"vmid": 9112, "status": "running"}]
                with mock.patch.object(LAB, "ProxmoxAPI", return_value=api), \
                     mock.patch.object(LAB, "finalize_lease", return_value=[]), \
                     mock.patch.object(LAB, "active_leases", return_value=[]), \
                     mock.patch.object(LAB, "audit"), \
                     mock.patch("builtins.print") as printed, \
                     self.assertRaises(LAB.LabError):
                    LAB.cmd_lease_end(args)
                payload = LAB.json.loads(printed.call_args_list[0][0][0])
                self.assertTrue(payload["host_left_running"])
                self.assertIn("9112", payload["reason"])
            finally:
                LAB.LEASE_ROOT = old_root

    def test_audit_through_boot_succeeds_immediately_when_backend_is_up(self) -> None:
        with mock.patch.object(LAB, "audit") as audit:
            LAB._audit_through_boot("lab-power-on-requested", mode="wake-on-lan")
        audit.assert_called_once_with(
            "lab-power-on-requested", mode="wake-on-lan"
        )

    def test_audit_through_boot_retries_while_the_hosted_backend_wakes_up(self) -> None:
        audit = mock.Mock(side_effect=[
            LAB.pocketbase_module.PocketBaseError("connection refused"),
            LAB.pocketbase_module.PocketBaseError("connection refused"),
            None,
        ])
        with mock.patch.object(LAB, "audit", audit), \
             mock.patch.object(LAB, "time") as fake_time:
            fake_time.monotonic.side_effect = [0, 1, 2, 3, 4]
            fake_time.sleep.return_value = None
            LAB._audit_through_boot("lab-power-on-verified", host="h", node="n")
        self.assertEqual(audit.call_count, 3)

    def test_audit_through_boot_warns_and_gives_up_after_the_retry_window(self) -> None:
        audit = mock.Mock(
            side_effect=LAB.pocketbase_module.PocketBaseError("still down")
        )
        with mock.patch.object(LAB, "audit", audit), \
             mock.patch.object(LAB, "time") as fake_time, \
             mock.patch("builtins.print") as printed:
            fake_time.monotonic.side_effect = [0, 1, 31, 32]
            fake_time.sleep.return_value = None
            LAB._audit_through_boot("lab-power-on-requested", mode="wake-on-lan")
        warning = printed.call_args[0][0]
        self.assertIn("lab-power-on-requested", warning)
        self.assertIn("still down", warning)

    def test_pocketbase_client_converts_a_superuser_token_to_an_agent(self) -> None:
        """A superuser token pasted into the audit slot is converted once
        into a permanent least-privileged agent whose token replaces it."""
        import base64
        import contextlib
        import io
        import json

        payload = base64.urlsafe_b64encode(json.dumps({
            "collectionId": "_superusers",
            "exp": 1_000_000,
        }).encode()).decode().rstrip("=")
        token = f"header.{payload}.signature"
        audit_config = {
            "pocketbase_url": "https://pb.example",
            "pocketbase_collection": "events",
            "pocketbase_token_secret": "audit-token",
            "pocketbase_timeout_seconds": 10,
            "pocketbase_auth_refresh_before_seconds": 300,
        }
        provisioned = {
            "token": "agent-token",
            "agent_collection": "proxmox_lab_agents",
        }
        stderr = io.StringIO()
        with mock.patch.dict(LAB.CONFIG.audit._values, audit_config), \
             mock.patch.object(LAB.secrets_store, "get", return_value=token), \
             mock.patch.object(
                 LAB, "_provision_pocketbase_agent", return_value=provisioned,
             ) as provision, \
             mock.patch.object(LAB.time, "time", return_value=800), \
             contextlib.redirect_stderr(stderr):
            client = LAB.pocketbase_client()
        self.assertEqual(client.token, "agent-token")
        self.assertEqual(provision.call_args[0][0].token, token)
        self.assertIn("superuser token", stderr.getvalue())
        self.assertIn("proxmox_lab_agents", stderr.getvalue())

    def test_pocketbase_client_refreshes_a_near_expiry_agent_token(self) -> None:
        import base64
        import json

        payload = base64.urlsafe_b64encode(json.dumps({
            "collectionId": "agents",
            "exp": 1_000,
        }).encode()).decode().rstrip("=")
        token = f"header.{payload}.signature"
        audit_config = {
            "pocketbase_url": "https://pb.example",
            "pocketbase_collection": "events",
            "pocketbase_token_secret": "audit-token",
            "pocketbase_timeout_seconds": 10,
            "pocketbase_auth_refresh_before_seconds": 300,
        }
        with mock.patch.dict(LAB.CONFIG.audit._values, audit_config), \
             mock.patch.object(
                 LAB.secrets_store, "get", return_value=token
             ), \
             mock.patch.object(LAB.secrets_store, "store") as store, \
             mock.patch.object(
                 LAB.pocketbase_module.Client,
                 "refresh_auth_token",
                 return_value="refreshed-token",
             ) as refresh, \
             mock.patch.object(LAB.time, "time", return_value=800):
            client = LAB.pocketbase_client()
        refresh.assert_called_once_with("agents")
        store.assert_called_once_with(LAB.CONFIG, "audit-token", "refreshed-token")
        self.assertEqual(client.token, "refreshed-token")

    def test_pocketbase_client_reauthenticates_an_expired_agent_token(self) -> None:
        import base64
        import json

        payload = base64.urlsafe_b64encode(json.dumps({
            "collectionId": "agents",
            "exp": 1_000,
        }).encode()).decode().rstrip("=")
        token = f"header.{payload}.signature"
        audit_config = {
            "pocketbase_url": "https://pb.example",
            "pocketbase_collection": "events",
            "pocketbase_token_secret": "audit-token",
            "pocketbase_timeout_seconds": 10,
            "pocketbase_auth_refresh_before_seconds": 300,
        }
        with mock.patch.dict(LAB.CONFIG.audit._values, audit_config), \
             mock.patch.object(
                 LAB.secrets_store,
                 "get",
                 side_effect=[token, "agent@lab.invalid", "agent-password"],
             ), \
             mock.patch.object(LAB.secrets_store, "store") as store, \
             mock.patch.object(
                 LAB.pocketbase_module.Client,
                 "refresh_auth_token",
                 side_effect=LAB.pocketbase_module.PocketBaseError(
                     "expired", status=401
                 ),
             ), \
             mock.patch.object(
                 LAB.pocketbase_module.Client,
                 "authenticate_password",
                 return_value="reauthenticated-token",
             ) as authenticate, \
             mock.patch.object(LAB.time, "time", return_value=800):
            client = LAB.pocketbase_client()
        authenticate.assert_called_once_with(
            "https://pb.example",
            "agents",
            "agent@lab.invalid",
            "agent-password",
            timeout=10,
        )
        store.assert_called_once_with(
            LAB.CONFIG, "audit-token", "reauthenticated-token"
        )
        self.assertEqual(client.token, "reauthenticated-token")

    def _nonrenewable_token(self, exp: int) -> str:
        import base64
        import json

        payload = base64.urlsafe_b64encode(json.dumps({
            "collectionId": "agents",
            "exp": exp,
            "refreshable": False,
        }).encode()).decode().rstrip("=")
        return f"header.{payload}.signature"

    def _pocketbase_client_stderr(self, token: str) -> str:
        import contextlib
        import io

        audit_config = {
            "pocketbase_url": "https://pb.example",
            "pocketbase_collection": "events",
            "pocketbase_token_secret": "audit-token",
            "pocketbase_timeout_seconds": 10,
            "pocketbase_auth_refresh_before_seconds": 300,
        }
        stderr = io.StringIO()
        with mock.patch.dict(LAB.CONFIG.audit._values, audit_config), \
             mock.patch.object(LAB.secrets_store, "get", return_value=token), \
             mock.patch.object(LAB.time, "time", return_value=800), \
             contextlib.redirect_stderr(stderr):
            LAB.pocketbase_client()
        return stderr.getvalue()

    def test_pocketbase_client_warns_before_nonrenewable_token_lapses(self) -> None:
        output = self._pocketbase_client_stderr(
            self._nonrenewable_token(800 + 3600)
        )
        self.assertIn("nonrenewable", output)
        self.assertIn("--provision-pocketbase-agent", output)

    def test_pocketbase_client_stays_quiet_for_distant_expiry(self) -> None:
        output = self._pocketbase_client_stderr(
            self._nonrenewable_token(800 + 60 * 24 * 3600)
        )
        self.assertEqual(output, "")

    def test_agent_provision_stores_reauth_credentials_and_token(self) -> None:
        import contextlib
        import io
        import json

        args = LAB.parser().parse_args(["journal", "--provision-pocketbase-agent"])
        provisioner = mock.Mock()
        provisioner.provision_agent.return_value = {
            "agent_collection": "agents",
            "agent_created": True,
            "audit_collection": {"created": True},
            "token": "agent-token",
        }
        audit_config = {
            "pocketbase_url": "https://pb.example",
            "pocketbase_collection": "events",
            "pocketbase_token_secret": "audit-token",
            "pocketbase_timeout_seconds": 10,
            "pocketbase_auth_refresh_before_seconds": 300,
            "pocketbase_agent_collection": "agents",
        }
        output = io.StringIO()
        with mock.patch.dict(LAB.CONFIG.audit._values, audit_config), \
             mock.patch.object(
                 LAB.secrets_store, "get", return_value=""
             ), \
             mock.patch.object(
                 LAB.pocketbase_module.Client,
                 "new_agent_credentials",
                 return_value=("agent@lab.invalid", "agent-password"),
             ), \
             mock.patch.object(LAB.secrets_store, "store") as store, \
             mock.patch.object(
                 LAB, "pocketbase_superuser_client", return_value=provisioner
             ), \
             contextlib.redirect_stdout(output):
            LAB.cmd_journal(args)
        provisioner.provision_agent.assert_called_once_with(
            "agents",
            "agent@lab.invalid",
            "agent-password",
            rotate_existing=True,
        )
        self.assertEqual(
            [call.args[1] for call in store.call_args_list],
            ["pocketbase-agent-email", "pocketbase-agent-password", "audit-token"],
        )
        self.assertNotIn("agent-token", output.getvalue())
        self.assertEqual(
            json.loads(output.getvalue())["credential_mode"],
            "password-reauthentication",
        )


if __name__ == "__main__":
    unittest.main()
