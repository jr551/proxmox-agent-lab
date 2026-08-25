"""Tests for the layers that hide platform and guest differences:
config, secrets, power, the audit ledger, and guest channel selection."""

from __future__ import annotations

import os
from pathlib import Path

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
import sys  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402
from unittest import mock  # noqa: E402

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import json  # noqa: E402
import sqlite3  # noqa: E402

from proxmox_agent_lab import config as config_module  # noqa: E402
from proxmox_agent_lab import guest as guest_module  # noqa: E402
from proxmox_agent_lab import inventory as inventory_module  # noqa: E402
from proxmox_agent_lab import journal as journal_module  # noqa: E402
from proxmox_agent_lab import power as power_module  # noqa: E402
from proxmox_agent_lab import secrets_store  # noqa: E402


class ConfigTests(unittest.TestCase):
    def test_missing_file_is_not_an_error(self) -> None:
        """`init` must be able to run before a config exists."""
        with tempfile.TemporaryDirectory() as tmp:
            absent = Path(tmp) / "nope.toml"
            config = config_module.load(absent)
        self.assertFalse(config.configured)
        self.assertEqual(config.intended, absent)
        self.assertEqual(config.power.mode, "wake-on-lan")

    def test_a_misspelled_section_is_reported_but_not_fatal(self) -> None:
        """It is surfaced by `doctor` rather than discarding the config."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.toml"
            path.write_text('[proxmoxx]\nhost = "x"\n\n[proxmox]\nnode = "n"\n')
            config = config_module.load(path)
        self.assertEqual(config.unknown_sections, ["proxmoxx"])
        self.assertEqual(config.proxmox.node, "n")

    def test_require_names_the_setting_and_the_file(self) -> None:
        config = config_module.defaults()
        with self.assertRaises(config_module.ConfigError) as caught:
            config.require("proxmox.host", "the Proxmox address")
        message = str(caught.exception)
        self.assertIn("[proxmox] host", message)
        self.assertIn("the Proxmox address", message)

    def test_partial_config_keeps_defaults_for_everything_else(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.toml"
            path.write_text('[proxmox]\nhost = "10.0.0.1"\n')
            config = config_module.load(path)
        self.assertEqual(config.proxmox.host, "10.0.0.1")
        self.assertEqual(config.proxmox.port, 8006)
        self.assertEqual(config.lease.default_ttl_seconds, 7200)

    def test_every_module_shares_one_instance(self) -> None:
        self.assertIs(config_module.get(), config_module.get())

    def test_the_template_it_writes_is_valid_and_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.toml"
            path.write_text(config_module.TEMPLATE)
            config = config_module.load(path)   # must not raise
        self.assertTrue(config.configured)


class PowerTests(unittest.TestCase):
    def test_magic_packet_shape(self) -> None:
        packet = power_module.magic_packet("aa:bb:cc:dd:ee:ff")
        self.assertEqual(len(packet), 102)          # 6 + 16 * 6
        self.assertEqual(packet[:6], b"\xff" * 6)
        self.assertEqual(packet[6:12], bytes.fromhex("aabbccddeeff"))
        self.assertEqual(packet[-6:], bytes.fromhex("aabbccddeeff"))

    def test_mac_separators_are_all_accepted(self) -> None:
        expected = power_module.magic_packet("aa:bb:cc:dd:ee:ff")
        for spelling in ("AA-BB-CC-DD-EE-FF", "aabb.ccdd.eeff", "AABBCCDDEEFF"):
            self.assertEqual(power_module.magic_packet(spelling), expected)

    def test_a_bad_mac_is_rejected(self) -> None:
        for bad in ("", "not-a-mac", "aa:bb:cc:dd:ee", "zz:bb:cc:dd:ee:ff"):
            with self.assertRaises(power_module.PowerError):
                power_module.magic_packet(bad)

    def test_wake_on_lan_cannot_force_off(self) -> None:
        """Silently pretending would be worse than admitting it."""
        config = config_module.defaults()
        self.assertFalse(power_module.can_force_off(config))
        with self.assertRaises(power_module.PowerError) as caught:
            power_module.force_off(config)
        self.assertIn("cannot force", str(caught.exception))

    def test_force_off_available_only_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.toml"
            path.write_text(
                '[power]\nmode = "command"\n'
                'on_command = "true"\noff_command = "true"\n'
            )
            config = config_module.load(path)
        self.assertTrue(power_module.can_force_off(config))

    def test_power_mode_none_explains_itself(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.toml"
            path.write_text('[power]\nmode = "none"\n')
            config = config_module.load(path)
        with self.assertRaises(power_module.PowerError) as caught:
            power_module.power_on(config)
        self.assertIn("switch the machine on yourself", str(caught.exception))

    def _composite_config(self, tmp: str) -> "config_module.Config":
        path = Path(tmp) / "c.toml"
        path.write_text(
            '[power]\nmode = "wake-on-lan+home-assistant"\n'
            'mac = "aa:bb:cc:dd:ee:ff"\nbroadcast = "192.168.1.255"\n'
            'home_assistant_url = "https://ha.example"\n'
            'entity_on = "script.lab_power_on"\n'
            'entity_off = "script.lab_force_off"\n'
        )
        return config_module.load(path)

    def test_composite_mode_sends_both_wol_and_home_assistant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = self._composite_config(tmp)
        with mock.patch.object(power_module, "wake_on_lan") as wol, \
             mock.patch.object(power_module, "_home_assistant") as ha:
            result = power_module.power_on(config)
        wol.assert_called_once_with("aa:bb:cc:dd:ee:ff", "192.168.1.255", 9)
        ha.assert_called_once_with(config, "script.lab_power_on")
        self.assertEqual(result["mode"], "wake-on-lan+home-assistant")
        self.assertIsNone(result["errors"])

    def test_composite_mode_survives_one_path_failing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = self._composite_config(tmp)
        with mock.patch.object(power_module, "wake_on_lan",
                                side_effect=power_module.PowerError("boom")), \
             mock.patch.object(power_module, "_home_assistant") as ha:
            result = power_module.power_on(config)
        ha.assert_called_once()
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("wake-on-lan: boom", result["errors"][0])

    def test_composite_mode_raises_only_if_both_paths_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = self._composite_config(tmp)
        with mock.patch.object(power_module, "wake_on_lan",
                                side_effect=power_module.PowerError("wol down")), \
             mock.patch.object(power_module, "_home_assistant",
                                side_effect=power_module.PowerError("ha down")):
            with self.assertRaises(power_module.PowerError) as caught:
                power_module.power_on(config)
        self.assertIn("wol down", str(caught.exception))
        self.assertIn("ha down", str(caught.exception))

    def test_composite_mode_force_off_goes_through_home_assistant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = self._composite_config(tmp)
        self.assertTrue(power_module.can_force_off(config))
        with mock.patch.object(power_module, "_home_assistant") as ha:
            result = power_module.force_off(config)
        ha.assert_called_once_with(config, "script.lab_force_off")
        self.assertEqual(result["mode"], "wake-on-lan+home-assistant")


class SecretsTests(unittest.TestCase):
    def test_env_backend_reads_the_namespaced_variable(self) -> None:
        config = config_module.defaults()
        config._values["secrets"]["backend"] = "env"
        with mock.patch.dict(
            os.environ, {"PROXMOX_AGENT_LAB_PROXMOX_TOKEN": "abc123"}
        ):
            self.assertEqual(secrets_store.get(config, "proxmox-token"), "abc123")

    def test_a_missing_secret_says_how_to_store_it(self) -> None:
        config = config_module.defaults()
        config._values["secrets"]["backend"] = "env"
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(secrets_store.SecretError) as caught:
                secrets_store.get(config, "proxmox-token")
        self.assertIn("proxmox-lab secrets set proxmox-token",
                      str(caught.exception))

    def test_optional_secrets_return_empty_rather_than_raising(self) -> None:
        config = config_module.defaults()
        config._values["secrets"]["backend"] = "env"
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                secrets_store.get(config, "s3-key-id", required=False), ""
            )

    def test_env_backend_refuses_to_pretend_it_can_store(self) -> None:
        config = config_module.defaults()
        config._values["secrets"]["backend"] = "env"
        with self.assertRaises(secrets_store.SecretError):
            secrets_store.store(config, "proxmox-token", "x")

    def test_file_backend_refuses_world_readable_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "secrets.toml"
            path.write_text('proxmox-token = "abc"\n')
            path.chmod(0o644)
            config = config_module.defaults()
            config._values["secrets"].update(
                {"backend": "file", "file_path": str(path)}
            )
            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(secrets_store.SecretError) as caught:
                    secrets_store.get(config, "proxmox-token")
        self.assertIn("chmod 600", str(caught.exception))

    def test_file_backend_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "secrets.toml"
            config = config_module.defaults()
            config._values["secrets"].update(
                {"backend": "file", "file_path": str(path)}
            )
            secrets_store.store(config, "proxmox-token", "sekrit")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(
                    secrets_store.get(config, "proxmox-token"), "sekrit"
                )


journal = journal_module
config = config_module


class JournalTests(unittest.TestCase):
    """The ledger layer: spooling, deterministic ids, and legacy migration.

    The MariaDB side itself is covered against a real server in
    test_mariadb.py; these cover the logic that must hold with the ledger
    unreachable, which is most of the time.
    """

    def _event(self, name: str, **fields: object) -> dict[str, object]:
        return {"timestamp": "2026-01-01T00:00:00Z", "event": name, **fields}

    def test_an_unreachable_ledger_spools_instead_of_failing(self) -> None:
        """The lab host is off between leases; an action must not fail for it."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outcome = journal.record(None, root, self._event("lease-begin"))
            self.assertEqual(outcome, "spooled")
            self.assertEqual(len(journal.read_spool(root)), 1)

    def test_a_spooled_event_keeps_its_controller_and_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            journal.record(None, root, self._event("x"), controller="pc-1")
            entry = journal.read_spool(root)[0]
            self.assertEqual(entry["controller"], "pc-1")
            self.assertTrue(entry["event_id"])

    def test_event_ids_are_derived_from_content(self) -> None:
        """Deterministic, so replaying a spool or re-running a migration is a
        no-op against the unique index rather than duplicated history."""
        a = self._event("guest-run", vmid=1)
        b = self._event("guest-run", vmid=1)
        c = self._event("guest-run", vmid=2)
        self.assertEqual(journal.event_id_for(a), journal.event_id_for(b))
        self.assertNotEqual(journal.event_id_for(a), journal.event_id_for(c))

    def test_an_explicit_event_id_is_kept(self) -> None:
        given = self._event("x", event_id="abc123")
        self.assertEqual(journal.event_id_for(given), "abc123")

    def test_legacy_sqlite_and_jsonl_are_found_for_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            root.mkdir(exist_ok=True)
            connection = sqlite3.connect(journal.legacy_database_path(root))
            connection.execute(
                "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "timestamp TEXT, event TEXT, lease TEXT, vmid INTEGER, data TEXT)"
            )
            connection.execute(
                "INSERT INTO events (timestamp, event, data) VALUES (?, ?, ?)",
                ("2026-01-01T00:00:00Z", "old",
                 json.dumps(self._event("old"), sort_keys=True)),
            )
            connection.commit()
            connection.close()
            (root / "2026-01-02.jsonl").write_text(
                json.dumps(self._event("older"), sort_keys=True) + "\n"
            )
            self.assertEqual(
                journal.legacy_counts(root), {"sqlite": 1, "jsonl": 1}
            )

    def test_the_spool_is_not_mistaken_for_a_legacy_jsonl_ledger(self) -> None:
        """The spool is also .jsonl; importing it as history would double it."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            journal.record(None, root, self._event("pending"))
            self.assertEqual(journal.legacy_counts(root)["jsonl"], 0)

    def test_migration_marker_stops_it_running_twice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertFalse(journal.migration_done(root))
            journal.mark_migrated(root, {"legacy_events": 0})
            self.assertTrue(journal.migration_done(root))
            self.assertIsNone(journal.auto_migrate(None, root))

    def test_settings_fall_back_to_the_proxmox_host(self) -> None:
        """The ledger runs on the lab host, so that is the sensible default."""
        cfg = config.Config({
            "proxmox": {"host": "192.0.2.10"},
            "audit": {},
        }, None, Path("/nonexistent"))
        settings = journal.settings_from_config(cfg, "pw")
        self.assertEqual(settings.host, "192.0.2.10")
        self.assertEqual(settings.port, 3306)

    def test_no_host_anywhere_means_no_ledger(self) -> None:
        cfg = config.Config({"proxmox": {}, "audit": {}}, None,
                            Path("/nonexistent"))
        self.assertIsNone(journal.settings_from_config(cfg, "pw"))


class GuestChannelTests(unittest.TestCase):
    """Channel selection is the whole point: callers should not have to know
    whether a guest has an agent."""

    def _lab(self, config: dict[str, object], agent: bool):
        lab = mock.Mock()
        lab.LabError = RuntimeError
        lab.NODE = "testnode"
        api = mock.Mock()
        api.call.return_value = config
        return lab, api, mock.patch.multiple(
            guest_module.console,
            agent_ready=mock.Mock(return_value=agent),
            agent_exec=mock.Mock(return_value={"exitcode": 0}),
        )

    def test_agent_is_preferred_when_available(self) -> None:
        lab, api, patched = self._lab({"serial0": "socket", "vga": "std"}, True)
        with patched:
            session = guest_module.GuestSession(lab, api, 100, password="pw")
        self.assertEqual(session.channel, "agent")

    def test_falls_back_to_serial_without_an_agent(self) -> None:
        lab, api, patched = self._lab({"serial0": "socket"}, False)
        with patched:
            session = guest_module.GuestSession(lab, api, 100, password="pw")
        self.assertEqual(session.channel, "serial")

    def test_agent_that_only_pings_is_not_a_command_channel(self) -> None:
        lab = mock.Mock()
        lab.LabError = RuntimeError
        lab.NODE = "testnode"
        api = mock.Mock()
        api.call.return_value = {"serial0": "socket", "agent": "enabled=1"}
        with mock.patch.object(
            guest_module.console, "agent_ready", return_value=True
        ), mock.patch.object(
            guest_module.console,
            "agent_exec",
            side_effect=RuntimeError("Proxmox HTTP 596 for POST agent/exec"),
        ) as execute:
            caps = guest_module.probe(lab, api, 100)

        self.assertFalse(caps.agent)
        self.assertTrue(any(
            "agent pings but cannot complete a command" in note
            for note in caps.notes
        ))
        execute.assert_called_once_with(
            lab, api, 100, ["/bin/true"], timeout=5,
        )

    def test_serial_needs_a_password_and_says_so(self) -> None:
        lab, api, patched = self._lab({"serial0": "socket"}, False)
        with patched:
            with self.assertRaises(guest_module.GuestError) as caught:
                guest_module.GuestSession(lab, api, 100)
        self.assertIn("console password", str(caught.exception))

    def test_no_channel_at_all_explains_both_remedies(self) -> None:
        lab, api, patched = self._lab({"vga": "std"}, False)
        with patched:
            with self.assertRaises(guest_module.GuestError) as caught:
                guest_module.GuestSession(lab, api, 100, password="pw")
        message = str(caught.exception)
        self.assertIn("serial0: socket", message)
        self.assertIn("qemu-guest-agent", message)

    def test_serial_display_is_flagged_as_unable_to_take_keystrokes(self) -> None:
        """The failure that wasted an hour: a picture, but typing goes nowhere."""
        lab, api, patched = self._lab({"serial0": "socket", "vga": "serial0"},
                                      True)
        with patched:
            caps = guest_module.probe(lab, api, 100)
        self.assertFalse(caps.keyboard_input)
        self.assertTrue(caps.graphical_console is False)
        self.assertTrue(any("keyboard input does not" in n for n in caps.notes))

    def test_graphical_display_accepts_keystrokes(self) -> None:
        lab, api, patched = self._lab({"vga": "std", "serial0": "socket"}, True)
        with patched:
            caps = guest_module.probe(lab, api, 100)
        self.assertTrue(caps.keyboard_input)

    def test_command_result_treats_absent_exit_code_as_not_failed(self) -> None:
        self.assertTrue(
            guest_module.CommandResult("out", None, "serial").ok
        )
        self.assertFalse(guest_module.CommandResult("out", 1, "agent").ok)


if __name__ == "__main__":
    unittest.main()


class ConfigForwardCompatibilityTests(unittest.TestCase):
    def test_an_unknown_section_is_ignored_not_fatal(self) -> None:
        """A leftover section from another version must not discard the whole
        config; that presents as 'host is not set' and misdirects entirely."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.toml"
            path.write_text(
                '[proxmox]\nhost = "10.0.0.1"\n\n[frombuture]\nx = 1\n'
            )
            config = config_module.load(path)
        self.assertTrue(config.configured)
        self.assertEqual(config.proxmox.host, "10.0.0.1")
        self.assertEqual(config.unknown_sections, ["frombuture"])

    def test_malformed_toml_is_still_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.toml"
            path.write_text("[proxmox\nhost = ")
            with self.assertRaises(config_module.ConfigError):
                config_module.load(path)


class RetainedRegistryTests(unittest.TestCase):
    """A node tag lives for ever and a lease record does not, so the registry
    is the only durable owner a keep-forever guest has."""

    def test_record_is_idempotent_and_keeps_the_first_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inventory_module.record(
                root, kind="qemu", vmid=9231, lease="L1",
                now="2026-08-01T00:00:00Z", purpose="template", name="tpl",
            )
            inventory_module.record(
                root, kind="qemu", vmid=9231, lease="L2",
                now="2026-08-02T00:00:00Z", purpose="reused",
            )
            entries = inventory_module.entries(root)
            self.assertEqual(list(entries), ["qemu/9231"])
            item = entries["qemu/9231"]
            self.assertEqual(item["created_by_lease"], "L1", "provenance kept")
            self.assertEqual(item["last_lease"], "L2")
            self.assertEqual(item["name"], "tpl")
            self.assertEqual(item["purpose"], "reused")

    def test_forget_removes_only_the_named_guest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for vmid in (9231, 9232):
                inventory_module.record(root, kind="qemu", vmid=vmid,
                                        lease="L1", now="2026-08-01T00:00:00Z")
            self.assertTrue(inventory_module.forget(root, "qemu", 9231))
            self.assertFalse(inventory_module.forget(root, "qemu", 9231))
            self.assertEqual(list(inventory_module.entries(root)), ["qemu/9232"])

    def test_backup_time_is_recorded_for_coverage_tracking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inventory_module.record(root, kind="qemu", vmid=9231, lease="L1",
                                    now="2026-08-01T00:00:00Z")
            self.assertIsNone(
                inventory_module.entries(root)["qemu/9231"]["last_backup_at"]
            )
            inventory_module.mark_backup(root, "qemu", 9231,
                                         "2026-08-20T00:00:00Z")
            self.assertEqual(
                inventory_module.entries(root)["qemu/9231"]["last_backup_at"],
                "2026-08-20T00:00:00Z",
            )

    def test_a_damaged_registry_never_breaks_a_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inventory_module.registry_path(root).write_text("{not json")
            self.assertEqual(inventory_module.entries(root), {})
            inventory_module.record(root, kind="qemu", vmid=1, lease="L",
                                    now="2026-08-01T00:00:00Z")
            self.assertIn("qemu/1", inventory_module.entries(root))

    def test_the_registry_is_written_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inventory_module.record(root, kind="qemu", vmid=1, lease="L",
                                    now="2026-08-01T00:00:00Z")
            leftovers = [p.name for p in root.iterdir()
                         if p.name.startswith(".retained-")]
            self.assertEqual(leftovers, [], "no temp file left behind")
            self.assertTrue(inventory_module.registry_path(root).is_file())

    def test_lease_tag_parsing(self) -> None:
        self.assertEqual(
            inventory_module.lease_of_tags("codex-lab;lease-20260816-abc;windows"),
            "20260816-abc",
        )
        self.assertIsNone(inventory_module.lease_of_tags("codex-lab;windows"))
        self.assertIsNone(inventory_module.lease_of_tags(None))
        self.assertTrue(inventory_module.is_lab_guest("codex-lab;lease-x"))
        self.assertFalse(inventory_module.is_lab_guest("production"))


class GuestOwnershipTests(unittest.TestCase):
    """Found live: ~20 guests carried tags whose lease records were gone, so
    resolving tag -> lease file called nearly every retained guest unowned."""

    GUESTS = [
        {"vmid": 9001, "type": "qemu", "status": "running",
         "tags": "codex-lab;lease-live", "name": "current"},
        {"vmid": 9002, "type": "qemu", "status": "running",
         "tags": "codex-lab;lease-pruned", "name": "abandoned"},
        {"vmid": 9003, "type": "qemu", "status": "stopped",
         "tags": "codex-lab;lease-pruned", "name": "kept", "template": 1},
        {"vmid": 9004, "type": "qemu", "status": "running", "name": "untagged"},
    ]

    def _classify(self, retained: dict) -> dict:
        described = inventory_module.classify(
            self.GUESTS, known_leases={"live"}, retained=retained
        )
        return {item["vmid"]: item for item in described}

    def test_a_tag_with_no_local_record_is_an_orphan(self) -> None:
        by_vmid = self._classify({})
        self.assertFalse(by_vmid[9001]["orphaned"], "its lease still exists")
        self.assertTrue(by_vmid[9001]["lease_known"])
        self.assertTrue(by_vmid[9002]["orphaned"])
        self.assertTrue(by_vmid[9003]["orphaned"])

    def test_the_registry_rescues_a_guest_whose_lease_is_gone(self) -> None:
        by_vmid = self._classify(
            {"qemu/9003": {"vmid": 9003, "purpose": "haiku template",
                           "last_backup_at": None}}
        )
        self.assertTrue(by_vmid[9003]["retained"])
        self.assertFalse(by_vmid[9003]["orphaned"],
                         "the registry vouches for it")
        self.assertEqual(by_vmid[9003]["retained_purpose"], "haiku template")

    def test_a_guest_this_tool_never_made_is_not_an_orphan(self) -> None:
        by_vmid = self._classify({})
        self.assertFalse(by_vmid[9004]["orphaned"])
        self.assertFalse(by_vmid[9004]["lab_guest"])

    def test_orphans_filters_to_the_unowned(self) -> None:
        described = inventory_module.classify(
            self.GUESTS, known_leases={"live"}, retained={}
        )
        self.assertEqual(
            [x["vmid"] for x in inventory_module.orphans(described)],
            [9002, 9003],
        )

    def test_malformed_guest_entries_are_skipped(self) -> None:
        described = inventory_module.classify(
            [{"no": "vmid"}, {"vmid": "not-a-number"}, {"vmid": 5, "type": "lxc"}],
            known_leases=set(), retained={},
        )
        self.assertEqual([x["vmid"] for x in described], [5])
