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

import shutil
import tempfile
# ...and at a disposable state directory: a test must never write into the
# developer's real controller state. Cleared here so a previous run cannot
# leak into this one; imports all happen before any test runs.
_TEST_STATE = Path(tempfile.gettempdir()) / "proxmox-agent-lab-test-state"
shutil.rmtree(_TEST_STATE, ignore_errors=True)
_TEST_STATE.mkdir(parents=True, exist_ok=True)
os.environ["PROXMOX_AGENT_LAB_STATE"] = str(_TEST_STATE)
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
                            ) as caught:
                                LAB.cmd_api(args)
                            # The refusal has to name its own remedy: the
                            # earlier wording sent operators looking for a
                            # broken guest instead of an unregistered one.
                            self.assertIn(
                                f"proxmox-lab lease-register --lease "
                                f"{lease_id} --kind {kind} --vmid {vmid} "
                                f"--allow-existing",
                                str(caught.exception),
                            )
                            api.call.assert_not_called()
            finally:
                LAB.LEASE_ROOT = old_lease_root

    def test_a_guest_path_the_regex_cannot_read_is_refused(self) -> None:
        """A guest write whose vmid does not parse skips no ownership check.

        `/nodes/N/qemu//9000/sendkey` sits inside the safe write surface and
        reaches the same guest, but `path_resource` reads no vmid from it, so
        accepting it would send the mutation with the lease check skipped.
        """
        lease = {
            "id": "20260814120000-guard92",
            "resources": [{"kind": "qemu", "vmid": 9000}],
            "initial_vmids": [],
        }
        api = mock.Mock()
        for path in (
            f"/nodes/{LAB.NODE}/qemu//9000/sendkey",
            f"/nodes/{LAB.NODE}/qemu/vm9000/sendkey",
            f"/nodes/{LAB.NODE}/lxc//9000/status/start",
        ):
            with self.subTest(path=path):
                args = LAB.parser().parse_args([
                    "api", "--lease", lease["id"], "--method", "PUT",
                    "--path", path, "--data", "key=ret",
                ])
                with mock.patch.object(LAB, "ProxmoxAPI", return_value=api), \
                     mock.patch.object(LAB, "load_lease", return_value=lease), \
                     mock.patch.object(LAB, "audit"):
                    with self.assertRaisesRegex(
                        LAB.LabError, "names no readable guest"
                    ):
                        LAB.cmd_api(args)
                api.call.assert_not_called()

    def test_guest_creation_and_node_paths_stay_writable(self) -> None:
        """The readable-guest check must not block the paths that have no vmid."""
        lease = {
            "id": "20260814120000-guard93",
            "resources": [],
            "initial_vmids": [],
        }
        api = mock.Mock()
        api.call.return_value = None
        for path, data in (
            (f"/nodes/{LAB.NODE}/qemu", ["vmid=9000"]),
            (f"/nodes/{LAB.NODE}/qemu/", ["vmid=9001"]),
        ):
            with self.subTest(path=path):
                args = LAB.parser().parse_args([
                    "api", "--lease", lease["id"], "--method", "POST",
                    "--path", path, *sum((["--data", d] for d in data), []),
                ])
                with mock.patch.object(LAB, "ProxmoxAPI", return_value=api), \
                     mock.patch.object(LAB, "load_lease", return_value=lease), \
                     mock.patch.object(LAB, "register_resource"), \
                     mock.patch.object(LAB, "audit"), \
                     mock.patch("sys.stdout", io.StringIO()):
                    LAB.cmd_api(args)
        self.assertEqual(api.call.call_count, 2)

    def test_require_lease_resource_names_the_registration_command(self) -> None:
        lease = {
            "id": "20260814120000-guard94",
            "resources": [],
            "initial_vmids": [9246],
        }
        with self.assertRaises(LAB.LabError) as pre_existing:
            LAB.require_lease_resource(lease, "qemu", 9246)
        self.assertEqual(
            str(pre_existing.exception),
            "VMID 9246 existed before this lease; register it with "
            "'proxmox-lab lease-register --lease 20260814120000-guard94 "
            "--kind qemu --vmid 9246 --allow-existing' if you intend to "
            "drive it",
        )

        with self.assertRaises(LAB.LabError) as unknown:
            LAB.require_lease_resource(lease, "lxc", 9247)
        self.assertEqual(
            str(unknown.exception),
            "VMID 9247 is not a lxc guest registered to this lease; register "
            "it with 'proxmox-lab lease-register --lease "
            "20260814120000-guard94 --kind lxc --vmid 9247' if you intend to "
            "drive it",
        )

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
            settings = mock.Mock()
            with mock.patch.object(LAB, "JOURNAL_ROOT", Path(tmp)), \
                 mock.patch.object(LAB, "ledger", return_value=settings), \
                 mock.patch.object(
                     LAB.mariadb_module, "append_many", return_value=2
                 ) as append_many, \
                 contextlib.redirect_stdout(stdout):
                LAB.cmd_journal(args)
            self.assertEqual(len(append_many.call_args.args[1]), 2)
            self.assertFalse(spool.exists())
            result = json.loads(stdout.getvalue())
        self.assertEqual(result["uploaded"], 2)
        self.assertEqual(result["already_present"], 0)

    def test_journal_flush_spool_keeps_events_after_a_hard_failure(self) -> None:
        """A failed upload must not lose the backlog."""
        import contextlib
        import io

        boom = LAB.mariadb_module.MariaDBError("ledger down")
        with tempfile.TemporaryDirectory() as tmp:
            spool = Path(tmp) / "spool.jsonl"
            spool.write_text(
                '{"event": "a", "event_id": "1", "timestamp": "t"}\n'
                '{"event": "b", "event_id": "2", "timestamp": "t"}\n'
            )
            args = LAB.parser().parse_args(["journal", "--flush-spool"])
            settings = mock.Mock()
            with mock.patch.object(LAB, "JOURNAL_ROOT", Path(tmp)), \
                 mock.patch.object(LAB, "ledger", return_value=settings), \
                 mock.patch.object(
                     LAB.mariadb_module, "append_many", side_effect=boom,
                 ), \
                 contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(LAB.mariadb_module.MariaDBError):
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

    def test_api_password_stdin_still_refuses_an_empty_password(self) -> None:
        """Deliberately unlike the guest console. There an empty password
        describes a guest that has none; here it would be *written* into a
        Proxmox object as a blank credential."""
        with tempfile.TemporaryDirectory() as tmp:
            old_lease_root = LAB.LEASE_ROOT
            LAB.LEASE_ROOT = Path(tmp) / "leases"
            try:
                lease_id = "20260822120000-apipw01"
                LAB.save_lease({
                    "id": lease_id, "state": "active", "kind": "session",
                    "resources": [{"kind": "qemu", "vmid": 9092}],
                    "initial_vmids": [],
                })
                api = mock.Mock()
                args = LAB.parser().parse_args([
                    "api", "--lease", lease_id, "--method", "PUT",
                    "--path", f"/nodes/{LAB.NODE}/qemu/9092/config",
                    "--password-stdin", "--password-key", "cipassword",
                ])
                with mock.patch.object(LAB, "ProxmoxAPI", return_value=api), \
                     mock.patch.object(LAB.sys, "stdin", io.StringIO("\n")), \
                     mock.patch.object(LAB, "audit"):
                    with self.assertRaises(LAB.LabError) as caught:
                        LAB.cmd_api(args)
                self.assertIn("empty password for cipassword",
                              str(caught.exception))
                self.assertIn("guest run --password-stdin",
                              str(caught.exception))
                api.call.assert_not_called()
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


class UploadTlsTests(unittest.TestCase):
    """Found live: upload always passed curl --insecure, so an operator who had
    turned certificate verification on still got an unverified upload."""

    def _argv(self, verify: bool) -> list[str]:
        with mock.patch.object(LAB, "VERIFY_TLS", verify):
            return LAB.upload_curl_argv(
                "/tmp/curl.conf", Path("/tmp/answer.iso"), "iso", "local"
            )

    def test_verification_on_means_no_insecure_flag(self) -> None:
        argv = self._argv(True)
        self.assertNotIn("--insecure", argv)
        self.assertIn("--config", argv)

    def test_the_self_signed_opt_out_is_still_available(self) -> None:
        self.assertIn("--insecure", self._argv(False))

    def test_the_token_is_never_in_the_argv(self) -> None:
        for verify in (True, False):
            joined = " ".join(self._argv(verify))
            self.assertNotIn("PVEAPIToken", joined)
            self.assertIn("/storage/local/upload", joined)


class LeaseOwnershipTests(unittest.TestCase):
    """Expiry cleanup stops and deletes guests, so ownership has to be settled
    before anything destructive happens."""

    def _lease(self, lease_id: str, *, state: str = "active",
               expires_in: int = 3600, vmid: int = 9001) -> dict:
        return {
            "id": lease_id,
            "state": state,
            "kind": "session",
            "created_at": LAB.iso_now(),
            "expires_at": LAB.new_expiry(expires_in),
            "initial_vmids": [],
            "resources": [{"kind": "qemu", "vmid": vmid, "policy": "delete",
                           "name": "reactos"}],
        }

    def _sweep(self, leases: list[dict], *, stop=None, delete=None,
               extra_args: list[str] | None = None) -> dict:
        """Run one cleanup-expired sweep over a temporary lease store."""
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as tmp:
            old = (LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT)
            LAB.LEASE_ROOT = Path(tmp) / "leases"
            LAB.LOCK_PATH = Path(tmp) / "controller.lock"
            LAB.STATE_ROOT = Path(tmp)
            try:
                for lease in leases:
                    LAB.save_lease(lease)
                api = mock.Mock()
                api.reachable.return_value = True
                args = LAB.parser().parse_args(
                    ["cleanup-expired", "--no-backup", *(extra_args or [])]
                )
                stdout = io.StringIO()
                with mock.patch.object(LAB, "ProxmoxAPI", return_value=api), \
                     mock.patch.object(LAB, "audit"), \
                     mock.patch.object(LAB, "stop_guest",
                                       side_effect=stop) as stopped, \
                     mock.patch.object(LAB, "delete_guest",
                                       side_effect=delete) as deleted, \
                     mock.patch.object(LAB, "shutdown_host",
                                       return_value=True), \
                     contextlib.redirect_stdout(stdout):
                    error = None
                    try:
                        LAB.cmd_cleanup_expired(args)
                    except LAB.LabError as exc:
                        error = str(exc)
                return {
                    "result": json.loads(stdout.getvalue()),
                    "error": error,
                    "stopped": stopped,
                    "deleted": deleted,
                    "leases": {
                        lease["id"]: json.loads(
                            LAB.lease_path(lease["id"]).read_text()
                        )
                        for lease in leases
                    },
                }
            finally:
                LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT = old

    def test_an_expired_lease_does_not_delete_a_live_lease_s_guest(self) -> None:
        """Found live: several 'active' records were already past their expiry
        and named the same VMID as the newest lease, whose guests were
        running. A sweep would have destroyed a VM in use."""
        expired = self._lease("20260821100000-old0", expires_in=-3600)
        live = self._lease("20260821120000-new0", expires_in=3600)
        run = self._sweep([expired, live])

        run["stopped"].assert_not_called()
        run["deleted"].assert_not_called()
        self.assertEqual(run["leases"][expired["id"]]["state"], "closed")
        self.assertEqual(
            run["leases"][expired["id"]]["transferred_resources"],
            ["qemu/9001"],
        )
        self.assertEqual(run["leases"][live["id"]]["state"], "active")
        self.assertEqual(
            run["result"]["left_to_another_lease"][expired["id"]],
            ["qemu/9001"],
        )

    def test_a_long_term_lease_also_protects_its_guest_from_a_sweep(self) -> None:
        expired = self._lease("20260821100000-old1", expires_in=-3600)
        persistent = self._lease("20260821110000-lt01", expires_in=-9999)
        persistent["kind"] = "long-term"
        persistent["expires_at"] = None
        persistent["resources"][0]["policy"] = "retain"
        run = self._sweep([expired, persistent])
        run["deleted"].assert_not_called()
        self.assertEqual(run["leases"][expired["id"]]["state"], "closed")

    def test_two_expired_leases_still_release_the_guest(self) -> None:
        """Deferring to another lease must not become a way for a guest to be
        cleaned up by nobody."""
        first = self._lease("20260821100000-old2", expires_in=-3600)
        second = self._lease("20260821101000-old3", expires_in=-3600)
        run = self._sweep([first, second])
        self.assertTrue(run["deleted"].called)
        self.assertEqual(run["leases"][first["id"]]["state"], "closed")
        self.assertEqual(run["leases"][second["id"]]["state"], "closed")

    def test_a_lease_still_cleans_up_a_guest_nobody_else_claims(self) -> None:
        expired = self._lease("20260821100000-old4", expires_in=-3600)
        other = self._lease("20260821120000-new1", expires_in=3600, vmid=9002)
        run = self._sweep([expired, other])
        self.assertEqual(run["deleted"].call_args.args[2], 9001)
        self.assertEqual(run["leases"][expired["id"]]["state"], "closed")

    def test_register_refuses_a_guest_a_live_lease_already_owns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old = (LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT)
            LAB.LEASE_ROOT = Path(tmp) / "leases"
            LAB.LOCK_PATH = Path(tmp) / "controller.lock"
            LAB.STATE_ROOT = Path(tmp)
            try:
                owner = self._lease("20260821120000-own0", expires_in=3600)
                LAB.save_lease(owner)
                newcomer = self._lease("20260821130000-new2", expires_in=3600)
                newcomer["resources"] = []
                LAB.save_lease(newcomer)
                args = LAB.parser().parse_args([
                    "lease-register", "--lease", newcomer["id"],
                    "--kind", "qemu", "--vmid", "9001",
                ])
                with mock.patch.object(LAB, "audit"):
                    with self.assertRaises(LAB.LabError) as caught:
                        LAB.cmd_lease_register(args)
                self.assertIn(owner["id"], str(caught.exception))
                stored = json.loads(
                    LAB.lease_path(newcomer["id"]).read_text()
                )
                self.assertEqual(stored["resources"], [])
            finally:
                LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT = old

    def test_an_expired_claim_does_not_block_registration(self) -> None:
        """A stale record must not make a VMID unusable for ever."""
        with tempfile.TemporaryDirectory() as tmp:
            old = (LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT)
            LAB.LEASE_ROOT = Path(tmp) / "leases"
            LAB.LOCK_PATH = Path(tmp) / "controller.lock"
            LAB.STATE_ROOT = Path(tmp)
            try:
                stale = self._lease("20260821100000-old5", expires_in=-3600)
                LAB.save_lease(stale)
                fresh = self._lease("20260821130000-new3", expires_in=3600)
                fresh["resources"] = []
                LAB.save_lease(fresh)
                args = LAB.parser().parse_args([
                    "lease-register", "--lease", fresh["id"],
                    "--kind", "qemu", "--vmid", "9001",
                ])
                import contextlib
                import io

                with mock.patch.object(LAB, "audit"), \
                     contextlib.redirect_stdout(io.StringIO()):
                    LAB.cmd_lease_register(args)
                stored = json.loads(LAB.lease_path(fresh["id"]).read_text())
                self.assertEqual(len(stored["resources"]), 1)
            finally:
                LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT = old

    def test_a_failed_cleanup_is_retried_by_the_next_sweep(self) -> None:
        """Found live: a QEMU lock timeout left the lease 'cleanup_failed', and
        every later sweep skipped it -- so its guests and the host stayed up
        until someone reran lease-end by hand."""
        lease = self._lease("20260821100000-lock0", expires_in=-3600)

        first = self._sweep(
            [lease], stop=LAB.LabError("VM is locked (clone)")
        )
        self.assertIn(lease["id"], first["result"]["failed"])
        self.assertEqual(
            first["leases"][lease["id"]]["state"], "cleanup_failed"
        )
        self.assertIsNotNone(first["error"])

        # The watchdog's next pass, with the lock gone.
        retried = dict(first["leases"][lease["id"]])
        second = self._sweep([retried])
        self.assertEqual(second["result"]["retried"], [lease["id"]])
        self.assertEqual(second["result"]["cleaned"], [lease["id"]])
        self.assertEqual(second["result"]["failed"], {})
        self.assertEqual(second["leases"][lease["id"]]["state"], "closed")
        self.assertTrue(second["deleted"].called)
        self.assertIsNone(second["error"])

    def test_a_failed_cleanup_is_retried_even_without_the_all_flag(self) -> None:
        lease = self._lease("20260821100000-lock1", expires_in=-3600)
        lease["state"] = "cleanup_failed"
        lease["failures"] = ["qemu/9001: VM is locked"]
        run = self._sweep([lease])
        self.assertEqual(run["result"]["retried"], [lease["id"]])
        self.assertEqual(run["leases"][lease["id"]]["state"], "closed")


class LeaseEndCrossReferenceTests(unittest.TestCase):
    """lease-end used to consult other leases only *after* finalize_lease had
    already destroyed this lease's guests, so a guest another active lease
    still registered could be deleted with nothing said in advance."""

    def _lease(self, lease_id: str, *, vmid: int = 9001,
               expires_in: int = 3600, kind: str = "session",
               policy: str = "delete", state: str = "active") -> dict:
        return {
            "id": lease_id,
            "state": state,
            "kind": kind,
            "created_at": LAB.iso_now(),
            "expires_at": None if kind == "long-term"
            else LAB.new_expiry(expires_in),
            "initial_vmids": [],
            "resources": [{"kind": "qemu", "vmid": vmid, "policy": policy,
                           "name": "ghidra-lab"}],
        }

    def _end(self, leases: list[dict], ending: str,
             *extra_args: str) -> dict:
        """Run one lease-end over a temporary lease store."""
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as tmp:
            old = (LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT)
            LAB.LEASE_ROOT = Path(tmp) / "leases"
            LAB.LOCK_PATH = Path(tmp) / "controller.lock"
            LAB.STATE_ROOT = Path(tmp)
            try:
                for lease in leases:
                    LAB.save_lease(lease)
                api = mock.Mock()
                api.reachable.return_value = True
                args = LAB.parser().parse_args(
                    ["lease-end", "--lease", ending, *extra_args]
                )
                stdout, stderr = io.StringIO(), io.StringIO()
                with mock.patch.object(LAB, "ProxmoxAPI", return_value=api), \
                     mock.patch.object(LAB, "ensure_on") as powered_on, \
                     mock.patch.object(LAB, "stop_guest") as stopped, \
                     mock.patch.object(LAB, "delete_guest") as deleted, \
                     mock.patch.object(LAB, "shutdown_host",
                                       return_value=True), \
                     mock.patch.object(LAB, "audit") as audited, \
                     contextlib.redirect_stdout(stdout), \
                     contextlib.redirect_stderr(stderr):
                    error = None
                    try:
                        LAB.cmd_lease_end(args)
                    except LAB.LabError as exc:
                        error = str(exc)
                payload = stdout.getvalue()
                return {
                    "result": json.loads(payload) if payload else None,
                    "error": error,
                    "stderr": stderr.getvalue(),
                    "stopped": stopped,
                    "deleted": deleted,
                    "powered_on": powered_on,
                    "audit": audited,
                    "leases": {
                        lease["id"]: json.loads(
                            LAB.lease_path(lease["id"]).read_text()
                        )
                        for lease in leases
                    },
                }
            finally:
                LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT = old

    def _events(self, audited: mock.Mock) -> list[str]:
        return [call.args[0] for call in audited.call_args_list]

    def test_a_guest_another_active_lease_registers_blocks_the_end(self) -> None:
        """Reachable: an idempotent 'memflow ghidra-setup --lxc N' re-run
        registers the same container under a second lease without going
        through lease-register's guard."""
        ending = self._lease("20260822100000-endme0")
        other = self._lease("20260822110000-other0")
        run = self._end([ending, other], ending["id"])

        self.assertIn("would destroy guest(s)", run["error"])
        self.assertIn("qemu/9001", run["error"])
        self.assertIn(other["id"], run["error"])
        self.assertIn("--shared-guests-authorized", run["error"])
        # The whole point: nothing was touched before the refusal.
        run["stopped"].assert_not_called()
        run["deleted"].assert_not_called()
        run["powered_on"].assert_not_called()
        self.assertEqual(run["leases"][ending["id"]]["state"], "active")
        self.assertIn("lease-end-refused-shared-guest",
                      self._events(run["audit"]))

    def test_the_refusal_also_covers_an_expired_but_active_lease(self) -> None:
        """An expired claim does not shield a guest from a *sweep*, but the
        lease record is one heartbeat from live again, so an operator ending
        another lease by hand must be told before the guest is destroyed."""
        ending = self._lease("20260822100000-endme1")
        stale = self._lease("20260822090000-stale1", expires_in=-3600)
        run = self._end([ending, stale], ending["id"])

        self.assertIn("expired but still active", run["error"])
        run["deleted"].assert_not_called()

    def test_a_long_term_lease_s_guest_is_named_in_the_refusal(self) -> None:
        ending = self._lease("20260822100000-endme2")
        persistent = self._lease("20260822080000-lt0002", kind="long-term",
                                 policy="retain")
        run = self._end([ending, persistent], ending["id"])

        self.assertIn(persistent["id"], run["error"])
        run["deleted"].assert_not_called()

    def test_the_override_flag_proceeds_and_reports_loudly(self) -> None:
        ending = self._lease("20260822100000-endme3")
        stale = self._lease("20260822090000-stale3", expires_in=-3600)
        run = self._end([ending, stale], ending["id"],
                        "--shared-guests-authorized")

        self.assertIsNone(run["error"])
        run["deleted"].assert_called_once()
        shared = run["result"]["shared_with_other_leases"]
        self.assertEqual(shared[0]["resource"], "qemu/9001")
        self.assertEqual(shared[0]["lease"], stale["id"])
        self.assertFalse(shared[0]["lease_live"])
        self.assertIn("--shared-guests-authorized", run["result"]["warning"])
        self.assertIn("warning: destroying guest(s)", run["stderr"])
        end_event = next(
            call for call in run["audit"].call_args_list
            if call.args[0] == "lease-end"
        )
        self.assertEqual(
            end_event.kwargs["shared_with_other_leases"][0]["lease"],
            stale["id"],
        )

    def test_the_override_still_defers_to_a_live_lease_inside_finalize(self) -> None:
        """The override lets the command run; it does not disable the
        per-resource check finalize_lease already makes."""
        ending = self._lease("20260822100000-endme4")
        live = self._lease("20260822110000-live04")
        run = self._end([ending, live], ending["id"],
                        "--shared-guests-authorized")

        run["deleted"].assert_not_called()
        self.assertEqual(run["result"]["left_to_another_lease"], ["qemu/9001"])

    def test_a_retained_resource_keeps_todays_behaviour(self) -> None:
        """This lease never deletes a retained guest, so sharing one is not a
        destroy hazard and must not start refusing."""
        ending = self._lease("20260822100000-endme5", policy="retain")
        stale = self._lease("20260822090000-stale5", expires_in=-3600)
        run = self._end([ending, stale], ending["id"])

        self.assertIsNone(run["error"])
        self.assertNotIn("shared_with_other_leases", run["result"])
        run["deleted"].assert_not_called()
        run["stopped"].assert_called_once()

    def test_a_different_guest_in_another_lease_is_not_cross_referenced(self) -> None:
        ending = self._lease("20260822100000-endme6", vmid=9001)
        other = self._lease("20260822110000-other6", vmid=9002)
        run = self._end([ending, other], ending["id"])

        self.assertIsNone(run["error"])
        self.assertNotIn("shared_with_other_leases", run["result"])
        run["deleted"].assert_called_once()

    def test_the_ordinary_single_lease_path_is_unchanged(self) -> None:
        ending = self._lease("20260822100000-endme7")
        run = self._end([ending], ending["id"])

        self.assertIsNone(run["error"])
        self.assertEqual(run["result"]["failures"], [])
        self.assertTrue(run["result"]["host_powered_off"])
        self.assertNotIn("shared_with_other_leases", run["result"])
        run["deleted"].assert_called_once()

    def test_a_closed_lease_never_cross_references_anything(self) -> None:
        """Only 'active' records can be mid-run; a closed one is bookkeeping."""
        ending = self._lease("20260822100000-endme8")
        closed = self._lease("20260822090000-closed8", state="closed")
        run = self._end([ending, closed], ending["id"])

        self.assertIsNone(run["error"])
        run["deleted"].assert_called_once()


class DoctorAuditTests(unittest.TestCase):
    """Found live: 1,547 events sat in the local spool and doctor said nothing.

    The ledger is unreachable whenever the lab host is off, which is most of
    the time, so a growing backlog is the thing worth reporting."""

    def _doctor(self, audit_overrides: dict, journal_root: Path) -> dict:
        import contextlib
        import io

        from proxmox_agent_lab import config as config_module

        args = LAB.parser().parse_args(["doctor"])
        stdout = io.StringIO()
        audit = config_module.Section(
            "audit", {**LAB.CONFIG.audit.as_dict(), **audit_overrides}
        )
        with mock.patch.object(LAB.CONFIG, "audit", audit), \
             mock.patch.object(LAB, "JOURNAL_ROOT", journal_root), \
             contextlib.redirect_stdout(stdout):
            try:
                LAB.cmd_doctor(args)
            except LAB.LabError:
                pass
        return json.loads(stdout.getvalue())

    def test_a_local_audit_spool_backlog_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            journal_root = Path(tmp) / "journal"
            journal_root.mkdir()
            (journal_root / "spool.jsonl").write_text(
                '{"event": "lease-begin"}\n{"event": "lease-end"}\n'
            )
            report = self._doctor({}, journal_root)
        self.assertEqual(report["audit"]["spooled_records"], 2)
        self.assertTrue(any("flush-spool" in problem
                            for problem in report["problems"]))

    def test_an_empty_spool_is_not_a_problem(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = self._doctor({}, Path(tmp) / "journal")
        self.assertEqual(report["audit"]["spooled_records"], 0)
        self.assertFalse(any("spool" in problem
                             for problem in report["problems"]))


class OrphanReclamationTests(unittest.TestCase):
    """Found live: a guest whose lease record was gone ran for five days,
    invisible to every sweep, holding the host on because shutdown_host
    refuses while any guest runs."""

    def _guests(self, cpu: float = 0.00005) -> list[dict]:
        return [
            {"vmid": 9001, "type": "qemu", "status": "running", "cpu": 0.01,
             "tags": "codex-lab;lease-20260821120000-live", "name": "current"},
            {"vmid": 9002, "type": "qemu", "status": "running", "cpu": cpu,
             "mem": 1024, "diskwrite": 4096, "netin": 8192,
             "tags": "codex-lab;lease-20260814100000-gone", "name": "abandoned"},
            {"vmid": 9003, "type": "qemu", "status": "stopped",
             "tags": "codex-lab;lease-20260814100000-gone", "name": "cold"},
        ]

    def _state(self, tmp: str) -> tuple:
        old = (LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT)
        LAB.LEASE_ROOT = Path(tmp) / "leases"
        LAB.LOCK_PATH = Path(tmp) / "controller.lock"
        LAB.STATE_ROOT = Path(tmp)
        LAB.save_lease({
            "id": "20260821120000-live", "state": "active", "kind": "session",
            "created_at": LAB.iso_now(), "expires_at": LAB.new_expiry(3600),
            "initial_vmids": [],
            "resources": [{"kind": "qemu", "vmid": 9001, "policy": "delete"}],
        })
        return old

    def _api(self, *, tasks: dict | None = None,
             uptime: dict | None = None, cpu: float = 0.00005) -> mock.Mock:
        """`tasks` maps vmid -> task list, `uptime` maps vmid -> seconds."""
        tasks = tasks or {}
        uptime = uptime or {}

        def call(method: str, path: str, data: object = None) -> object:
            if path == "/cluster/resources":
                return self._guests(cpu=cpu)
            if path.endswith("/tasks"):
                vmid = int((data or {}).get("vmid", 0))
                return tasks.get(vmid, [])
            if path.endswith("/status/current"):
                vmid = int(path.split("/")[-3])
                return {"status": "running", "uptime": uptime.get(vmid, 999_999)}
            return None

        api = mock.Mock()
        api.reachable.return_value = True
        api.call.side_effect = call
        return api

    def test_a_pruned_lease_leaves_its_guest_detectable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old = self._state(tmp)
            try:
                orphans = LAB.orphaned_guests(self._api())
            finally:
                LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT = old
        self.assertEqual([x["vmid"] for x in orphans], [9002, 9003])

    def test_the_retained_registry_excludes_a_guest_from_reclamation(self) -> None:
        from proxmox_agent_lab import inventory as inventory_module

        with tempfile.TemporaryDirectory() as tmp:
            old = self._state(tmp)
            try:
                inventory_module.record(
                    Path(tmp), kind="qemu", vmid=9003, lease="20260814100000-gone",
                    now=LAB.iso_now(), purpose="template",
                )
                orphans = LAB.orphaned_guests(self._api())
            finally:
                LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT = old
        self.assertEqual([x["vmid"] for x in orphans], [9002])

    def test_reclamation_stops_and_never_deletes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old = self._state(tmp)
            try:
                api = self._api()
                with mock.patch.object(LAB, "stop_guest") as stopped, \
                     mock.patch.object(LAB, "delete_guest") as deleted, \
                     mock.patch.object(LAB, "audit") as audited:
                    result = LAB.reclaim_orphans(api)
            finally:
                LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT = old
        self.assertEqual(result["stopped"], [9002])
        self.assertEqual(result["already_stopped"], [9003])
        self.assertEqual(stopped.call_args.args[1:], ("qemu", 9002))
        deleted.assert_not_called()
        events = [call.args[0] for call in audited.call_args_list]
        self.assertIn("orphan-guest-stopped", events)

    def test_a_guest_something_is_still_driving_is_left_alone(self) -> None:
        """Found by running it: 'orphaned' means this controller has no record,
        not that nobody is using it. Another controller was taking a console
        screenshot of 9002 every 45 seconds through the same token, and the
        reclamation stopped it mid-run."""
        import time as _time

        with tempfile.TemporaryDirectory() as tmp:
            old = self._state(tmp)
            try:
                api = self._api(tasks={
                    9002: [{"type": "vncproxy",
                            "starttime": int(_time.time()) - 45}],
                })
                with mock.patch.object(LAB, "stop_guest") as stopped, \
                     mock.patch.object(LAB, "audit"):
                    result = LAB.reclaim_orphans(api)
            finally:
                LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT = old
        stopped.assert_not_called()
        self.assertEqual(result["stopped"], [])
        self.assertEqual(result["left_active"]["9002"]["signal"], "vncproxy")

    def test_a_guest_started_moments_ago_is_left_alone(self) -> None:
        """The task log can roll; a short uptime still says it is in use, and
        needs no clock agreement between controller and node."""
        with tempfile.TemporaryDirectory() as tmp:
            old = self._state(tmp)
            try:
                api = self._api(uptime={9002: 219})
                with mock.patch.object(LAB, "stop_guest") as stopped, \
                     mock.patch.object(LAB, "audit"):
                    result = LAB.reclaim_orphans(api)
            finally:
                LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT = old
        stopped.assert_not_called()
        self.assertEqual(
            result["left_active"]["9002"]["signal"], "started recently"
        )

    def test_a_guest_that_is_visibly_working_is_left_alone(self) -> None:
        """The blind spot the other two signals share: work *inside* a guest
        produces no Proxmox task and does not reset its uptime, so a long build
        in an unmanaged container looked idle to both."""
        with tempfile.TemporaryDirectory() as tmp:
            old = self._state(tmp)
            try:
                api = self._api(cpu=0.42)      # 42% -- unmistakably working
                with mock.patch.object(LAB, "stop_guest") as stopped, \
                     mock.patch.object(LAB, "audit"):
                    result = LAB.reclaim_orphans(api)
            finally:
                LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT = old
        stopped.assert_not_called()
        self.assertEqual(result["left_active"]["9002"]["signal"], "busy")
        self.assertEqual(result["left_active"]["9002"]["cpu_percent"], 42.0)

    def test_a_guest_merely_switched_on_is_still_reclaimed(self) -> None:
        """Measured on the node: an idle container sits near 0.005% CPU while a
        mostly-idle Debian guest sits near 1%, so the floor has to be well
        above background noise or nothing would ever be reclaimable."""
        with tempfile.TemporaryDirectory() as tmp:
            old = self._state(tmp)
            try:
                api = self._api(cpu=0.012)     # 1.2% -- background daemons
                with mock.patch.object(LAB, "stop_guest") as stopped, \
                     mock.patch.object(LAB, "audit"):
                    result = LAB.reclaim_orphans(api)
            finally:
                LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT = old
        self.assertEqual(result["stopped"], [9002])
        stopped.assert_called_once()

    def test_the_measured_load_is_reported_either_way(self) -> None:
        """So a reader can disagree with the threshold instead of trusting it."""
        import time as _time

        with tempfile.TemporaryDirectory() as tmp:
            old = self._state(tmp)
            try:
                api = self._api(tasks={
                    9002: [{"type": "vncproxy",
                            "starttime": int(_time.time()) - 30}],
                })
                with mock.patch.object(LAB, "stop_guest"), \
                     mock.patch.object(LAB, "audit") as audited:
                    result = LAB.reclaim_orphans(api)
            finally:
                LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT = old
        entry = result["left_active"]["9002"]
        self.assertEqual(entry["signal"], "vncproxy")
        self.assertEqual(entry["cpu_percent"], 0.005)
        self.assertEqual(entry["disk_written_bytes"], 4096)

    def test_our_own_stop_does_not_read_as_someone_using_it(self) -> None:
        """Otherwise the first reclamation would make every later run refuse."""
        import time as _time

        with tempfile.TemporaryDirectory() as tmp:
            old = self._state(tmp)
            try:
                api = self._api(tasks={
                    9002: [{"type": "qmshutdown",
                            "starttime": int(_time.time()) - 30},
                           {"type": "qmstop",
                            "starttime": int(_time.time()) - 20}],
                })
                with mock.patch.object(LAB, "stop_guest") as stopped, \
                     mock.patch.object(LAB, "audit"):
                    result = LAB.reclaim_orphans(api)
            finally:
                LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT = old
        self.assertEqual(result["stopped"], [9002])
        self.assertEqual(result["left_active"], {})
        stopped.assert_called_once()

    def test_an_unreadable_task_log_leaves_the_guest_running(self) -> None:
        """Not knowing must not resolve to stopping somebody's work."""
        with tempfile.TemporaryDirectory() as tmp:
            old = self._state(tmp)
            try:
                api = self._api()
                original = api.call.side_effect

                def call(method: str, path: str, data: object = None) -> object:
                    if path.endswith("/tasks"):
                        raise LAB.LabError("HTTP 403")
                    return original(method, path, data)

                api.call.side_effect = call
                with mock.patch.object(LAB, "stop_guest") as stopped, \
                     mock.patch.object(LAB, "audit"):
                    result = LAB.reclaim_orphans(api)
            finally:
                LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT = old
        stopped.assert_not_called()
        self.assertEqual(
            result["left_active"]["9002"]["signal"], "task log unreadable"
        )

    def test_include_active_stops_it_anyway(self) -> None:
        import time as _time

        with tempfile.TemporaryDirectory() as tmp:
            old = self._state(tmp)
            try:
                api = self._api(tasks={
                    9002: [{"type": "vncproxy",
                            "starttime": int(_time.time()) - 45}],
                })
                with mock.patch.object(LAB, "stop_guest") as stopped, \
                     mock.patch.object(LAB, "audit"):
                    result = LAB.reclaim_orphans(api, include_active=True)
            finally:
                LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT = old
        self.assertEqual(result["stopped"], [9002])
        stopped.assert_called_once()

    def test_a_stop_failure_is_reported_not_swallowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old = self._state(tmp)
            try:
                with mock.patch.object(LAB, "stop_guest",
                                       side_effect=LAB.LabError("locked")), \
                     mock.patch.object(LAB, "audit"):
                    result = LAB.reclaim_orphans(self._api())
            finally:
                LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT = old
        self.assertIn("9002", result["failed"])
        self.assertEqual(result["stopped"], [])

    def test_orphans_only_touches_no_lease_and_leaves_the_host_on(self) -> None:
        """Found while using it: reclamation was only reachable through a full
        sweep, which in the same run deletes every expired lease's guests and
        may power the host off. Wanting one is not consenting to the other."""
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as tmp:
            old = self._state(tmp)
            try:
                # An expired lease whose guest a full sweep would delete.
                LAB.save_lease({
                    "id": "20260814100000-expired", "state": "active",
                    "kind": "session", "created_at": LAB.iso_now(),
                    "expires_at": LAB.new_expiry(-3600), "initial_vmids": [],
                    "resources": [{"kind": "qemu", "vmid": 9001,
                                   "policy": "delete", "name": "keepme"}],
                })
                args = LAB.parser().parse_args([
                    "cleanup-expired", "--orphans-only",
                    "--host-change-authorized",
                ])
                out = io.StringIO()
                with mock.patch.object(LAB, "ProxmoxAPI",
                                       return_value=self._api()), \
                     mock.patch.object(LAB, "audit"), \
                     mock.patch.object(LAB, "stop_guest") as stopped, \
                     mock.patch.object(LAB, "delete_guest") as deleted, \
                     mock.patch.object(LAB, "finalize_lease") as finalized, \
                     mock.patch.object(LAB, "shutdown_host") as powered_off, \
                     contextlib.redirect_stdout(out):
                    LAB.cmd_cleanup_expired(args)
                result = json.loads(out.getvalue())
                lease = json.loads(
                    LAB.lease_path("20260814100000-expired").read_text()
                )
            finally:
                LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT = old
        self.assertEqual(result["reclaimed_orphans"]["stopped"], [9002])
        self.assertEqual(result["leases_swept"], [])
        self.assertFalse(result["host_powered_off"])
        # The expired lease and its guest are untouched.
        finalized.assert_not_called()
        deleted.assert_not_called()
        powered_off.assert_not_called()
        self.assertEqual(lease["state"], "active")
        self.assertEqual(stopped.call_args.args[1:], ("qemu", 9002))

    def test_orphans_only_still_requires_authorization(self) -> None:
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as tmp:
            old = self._state(tmp)
            try:
                args = LAB.parser().parse_args(
                    ["cleanup-expired", "--orphans-only"]
                )
                with mock.patch.object(LAB, "ProxmoxAPI",
                                       return_value=self._api()), \
                     mock.patch.object(LAB, "stop_guest") as stopped, \
                     contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(
                        LAB.LabError, "host-change-authorized"
                    ):
                        LAB.cmd_cleanup_expired(args)
                stopped.assert_not_called()
            finally:
                LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT = old

    def test_reclaiming_requires_explicit_authorization(self) -> None:
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as tmp:
            old = self._state(tmp)
            try:
                args = LAB.parser().parse_args(
                    ["cleanup-expired", "--no-backup", "--reclaim-orphans"]
                )
                with mock.patch.object(LAB, "ProxmoxAPI", return_value=self._api()), \
                     mock.patch.object(LAB, "audit"), \
                     mock.patch.object(LAB, "stop_guest") as stopped, \
                     mock.patch.object(LAB, "shutdown_host", return_value=False), \
                     contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(
                        LAB.LabError, "host-change-authorized"
                    ):
                        LAB.cmd_cleanup_expired(args)
                stopped.assert_not_called()
            finally:
                LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT = old

    def test_retain_registers_a_durable_owner(self) -> None:
        from proxmox_agent_lab import inventory as inventory_module

        with tempfile.TemporaryDirectory() as tmp:
            old = self._state(tmp)
            try:
                lease = LAB.load_lease("20260821120000-live")
                lease["purpose"] = "haiku template"
                with mock.patch.object(LAB, "audit"):
                    LAB.register_resource(lease, "qemu", 9077, "retain", "tpl")
                    LAB.register_resource(lease, "qemu", 9078, "delete", "tmp")
                entries = inventory_module.entries(Path(tmp))
            finally:
                LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT = old
        self.assertEqual(list(entries), ["qemu/9077"],
                         "only a retained guest outlives its lease")
        self.assertEqual(entries["qemu/9077"]["purpose"], "haiku template")

    def test_deleting_a_guest_clears_its_registry_entry(self) -> None:
        from proxmox_agent_lab import inventory as inventory_module

        with tempfile.TemporaryDirectory() as tmp:
            old = self._state(tmp)
            try:
                inventory_module.record(Path(tmp), kind="qemu", vmid=9077,
                                        lease="20260821120000-live",
                                        now=LAB.iso_now())
                api = mock.Mock()
                api.call.return_value = "UPID:x"
                with mock.patch.object(LAB, "wait_task", return_value={}):
                    LAB.delete_guest(api, "qemu", 9077)
                remaining = inventory_module.entries(Path(tmp))
            finally:
                LAB.LEASE_ROOT, LAB.LOCK_PATH, LAB.STATE_ROOT = old
        self.assertEqual(remaining, {})

    def test_the_sweep_lock_does_not_stack_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_state = LAB.STATE_ROOT
            LAB.STATE_ROOT = Path(tmp)
            try:
                with LAB.sweep_lock("retained-backup") as first:
                    self.assertTrue(first)
                    with LAB.sweep_lock("retained-backup") as second:
                        self.assertFalse(
                            second, "a second sweep must not start a second vzdump"
                        )
                with LAB.sweep_lock("retained-backup") as again:
                    self.assertTrue(again, "released when the first sweep ends")
            finally:
                LAB.STATE_ROOT = old_state


class SlowStorageGuardTests(unittest.TestCase):
    """Found live: both ReactOS benchmark guests had their disks on the USB
    bulk store, which measured ~25 MB/s -- the benchmark measured the cable."""

    def test_a_guest_disk_on_bulk_storage_is_flagged(self) -> None:
        with mock.patch.object(LAB.CONFIG.storage, "_values",
                               {"bulk_storage": "usb-bulk",
                                "upload_storages": ["local"]}):
            self.assertEqual(
                LAB.slow_storage_disks({"scsi0": "usb-bulk:100", "memory": "4096"}),
                ["scsi0=usb-bulk:100"],
            )

    def test_an_iso_mounted_from_bulk_storage_is_fine(self) -> None:
        """The docs recommend exactly this; only guest disks are the problem."""
        with mock.patch.object(LAB.CONFIG.storage, "_values",
                               {"bulk_storage": "usb-bulk"}):
            self.assertEqual(
                LAB.slow_storage_disks(
                    {"ide2": "usb-bulk:iso/reactos.iso,media=cdrom"}
                ),
                [],
            )

    def test_fast_storage_is_not_flagged(self) -> None:
        with mock.patch.object(LAB.CONFIG.storage, "_values",
                               {"bulk_storage": "usb-bulk"}):
            self.assertEqual(
                LAB.slow_storage_disks({"scsi0": "local-lvm:32"}), []
            )

    def test_every_disk_bus_is_covered(self) -> None:
        with mock.patch.object(LAB.CONFIG.storage, "_values",
                               {"bulk_storage": "usb-bulk"}):
            flagged = LAB.slow_storage_disks({
                "virtio0": "usb-bulk:32", "sata1": "usb-bulk:32",
                "ide0": "usb-bulk:32", "rootfs": "usb-bulk:8",
                "efidisk0": "usb-bulk:1", "net0": "virtio,bridge=vmbr1",
            })
        self.assertEqual(len(flagged), 5)
        self.assertNotIn("net0", " ".join(flagged))


class HostUpdateReportTests(unittest.TestCase):
    """Advisory only: the node needing patches is a thing to schedule between
    leases, not a reason for doctor to fail."""

    def _report(self, upgrade_stdout: str, reboot: str = "no",
                returncode: int = 0) -> dict:
        from proxmox_agent_lab import memflow

        def host_run(_lab, argv, **_kwargs):
            command = argv[-1]
            if "apt-get" in command:
                return mock.Mock(returncode=returncode,
                                 stdout=upgrade_stdout, stderr="")
            return mock.Mock(returncode=0, stdout=reboot, stderr="")

        with mock.patch.object(memflow, "host_ssh_enabled", return_value=True), \
             mock.patch.object(memflow, "host_run", side_effect=host_run):
            return LAB.host_update_report()

    def test_pending_upgrades_are_counted(self) -> None:
        report = self._report(
            "Inst libexpat1 [2.6.2] (2.6.4 Debian:13/stable [amd64])\n"
            "Inst util-linux [2.40] (2.41 Debian:13/stable [amd64])\n"
            "Conf libexpat1 (2.6.4 Debian:13/stable [amd64])\n"
        )
        self.assertTrue(report["checked"])
        self.assertEqual(report["updates_pending"], 2, "Conf lines are not upgrades")
        self.assertFalse(report["security_updates"])
        self.assertFalse(report["reboot_required"])

    def test_a_security_origin_is_flagged(self) -> None:
        report = self._report(
            "Inst libexpat1 [2.6.2] (2.6.4 Debian-Security:13/stable [amd64])\n"
        )
        self.assertTrue(report["security_updates"])

    def test_a_pending_reboot_is_reported(self) -> None:
        report = self._report("Inst x [1] (2 Debian:13/stable [amd64])\n",
                              reboot="yes")
        self.assertTrue(report["reboot_required"])

    def test_a_failed_check_says_so_rather_than_reporting_zero(self) -> None:
        report = self._report("", returncode=100)
        self.assertFalse(report["checked"])
        self.assertNotIn("updates_pending", report)

    def test_without_the_ssh_channel_it_is_simply_not_checked(self) -> None:
        from proxmox_agent_lab import memflow

        with mock.patch.object(memflow, "host_ssh_enabled", return_value=False):
            report = LAB.host_update_report()
        self.assertFalse(report["checked"])
        self.assertIn("memflow", report["reason"])


class DoctorInventoryTests(unittest.TestCase):
    """A running guest nothing owns is the reason the node stayed on for five
    days, so doctor has to fail on it rather than mention it."""

    def _doctor(self, guests: list[dict], tmp: str) -> dict:
        import contextlib
        import io

        api = mock.Mock()
        api.reachable.return_value = True
        api.call.side_effect = lambda method, path, data=None: (
            guests if path == "/cluster/resources"
            else {"/vms": {name: 1 for name in (
                "VM.Allocate", "VM.Config.Disk", "VM.PowerMgmt",
                "VM.Console", "VM.Audit")}}
            if path == "/access/permissions" else {}
        )
        args = LAB.parser().parse_args(["doctor"])
        out = io.StringIO()
        old = (LAB.LEASE_ROOT, LAB.STATE_ROOT, LAB.JOURNAL_ROOT)
        LAB.LEASE_ROOT = Path(tmp) / "leases"
        LAB.STATE_ROOT = Path(tmp)
        LAB.JOURNAL_ROOT = Path(tmp) / "journal"
        try:
            with mock.patch.object(LAB, "ProxmoxAPI", return_value=api), \
                 mock.patch.object(LAB.secrets_store, "get",
                                   return_value="token"), \
                 contextlib.redirect_stdout(out):
                try:
                    LAB.cmd_doctor(args)
                except LAB.LabError:
                    pass
        finally:
            LAB.LEASE_ROOT, LAB.STATE_ROOT, LAB.JOURNAL_ROOT = old
        return json.loads(out.getvalue())

    def test_a_running_orphan_is_a_problem(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = self._doctor(
                [{"vmid": 9002, "type": "qemu", "status": "running",
                  "tags": "codex-lab;lease-20260814100000-gone"}],
                tmp,
            )
        self.assertEqual(report["guests"]["orphaned_running"], [9002])
        self.assertTrue(any("cannot power off" in problem
                            for problem in report["problems"]))
        self.assertFalse(report["ok"])

    def test_a_stopped_orphan_is_a_note_not_a_problem(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = self._doctor(
                [{"vmid": 9003, "type": "qemu", "status": "stopped",
                  "tags": "codex-lab;lease-20260814100000-gone"}],
                tmp,
            )
        self.assertEqual(report["guests"]["orphaned"], 1)
        self.assertEqual(report["guests"]["orphaned_running"], [])
        self.assertIn("note", report["guests"])
        self.assertFalse(any("cannot power off" in problem
                             for problem in report["problems"]))

    def test_an_untagged_guest_is_never_called_an_orphan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = self._doctor(
                [{"vmid": 100, "type": "qemu", "status": "running",
                  "name": "not-ours"}],
                tmp,
            )
        self.assertEqual(report["guests"]["orphaned"], 0)


class StateIsolationTests(unittest.TestCase):
    def test_the_suite_never_points_at_real_controller_state(self) -> None:
        """Found the hard way: register_resource began writing a registry under
        STATE_ROOT, and one unisolated test put a bogus retained guest into the
        developer's live controller state -- where the backup sweep would then
        have picked it up."""
        self.assertEqual(
            str(LAB.STATE_ROOT), os.environ["PROXMOX_AGENT_LAB_STATE"]
        )
        self.assertNotIn(str(Path.home()), str(LAB.STATE_ROOT))


if __name__ == "__main__":
    unittest.main()


class InfrastructureGuestTests(unittest.TestCase):
    """The audit ledger runs on the host and outlives every lease.

    Found live: the first lease-end after provisioning it refused to power the
    host off, naming the ledger container as an untracked running guest. Left
    alone that means the machine can never power itself off again, which is
    the entire point of it.
    """

    def _resources(self) -> list[dict]:
        return [
            {"vmid": 9310, "status": "running", "tags": "codex-lab-infra"},
            {"vmid": 9001, "status": "running", "tags": "codex-lab;lease-x"},
            {"vmid": 9002, "status": "stopped", "tags": ""},
        ]

    def test_the_ledger_container_is_not_an_untracked_guest(self) -> None:
        api = mock.Mock()
        api.call.return_value = self._resources()
        self.assertEqual(LAB.running_guest_vmids(api), [9001])

    def test_an_ordinary_running_guest_still_blocks_power_off(self) -> None:
        api = mock.Mock()
        api.call.return_value = [
            {"vmid": 9001, "status": "running", "tags": "codex-lab;lease-x"},
        ]
        self.assertEqual(LAB.running_guest_vmids(api), [9001])

    def test_comma_separated_tags_are_understood(self) -> None:
        """Proxmox has used both separators; the guard must not depend on it."""
        api = mock.Mock()
        api.call.return_value = [
            {"vmid": 9310, "status": "running", "tags": "codex-lab-infra,other"},
        ]
        self.assertEqual(LAB.running_guest_vmids(api), [])

    def test_a_guest_with_no_tags_is_still_counted(self) -> None:
        api = mock.Mock()
        api.call.return_value = [{"vmid": 9005, "status": "running"}]
        self.assertEqual(LAB.running_guest_vmids(api), [9005])


class UploadStorageDefaultTests(unittest.TestCase):
    """Big images belong on bulk, not on the hypervisor's root filesystem.

    Found live: 50 GB of ISOs had accumulated on the Proxmox root filesystem,
    taking it to 96% full. A full root takes the hypervisor down with it, which
    is a much worse failure than a slow ISO read.
    """

    def _default(self, upload: tuple, bulk: str) -> str:
        """The rule cli.py applies at import time."""
        return bulk if bulk in upload else (upload[0] if upload else "local")

    def test_bulk_is_preferred_when_it_is_an_upload_target(self) -> None:
        self.assertEqual(
            self._default(("local", "usb-bulk"), "usb-bulk"), "usb-bulk"
        )

    def test_falls_back_when_bulk_is_not_an_upload_target(self) -> None:
        """A config that never allowed bulk uploads must still work."""
        self.assertEqual(self._default(("local",), "usb-bulk"), "local")

    def test_the_shipped_default_is_not_the_root_filesystem(self) -> None:
        """local is /var/lib/vz on the Proxmox root; bulk is not."""
        self.assertIn(LAB.DEFAULT_UPLOAD_STORAGE, LAB.UPLOAD_STORAGES)
