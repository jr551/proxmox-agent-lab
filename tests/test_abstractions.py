"""Tests for the layers that hide platform and guest differences:
config, secrets, power, the audit ledger, and guest channel selection."""

from __future__ import annotations

import os
from pathlib import Path

os.environ["PROXMOX_AGENT_LAB_CONFIG"] = str(
    Path(__file__).parent / "fixtures" / "config.toml"
)

import sys  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402
from unittest import mock  # noqa: E402

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from proxmox_agent_lab import config as config_module  # noqa: E402
from proxmox_agent_lab import guest as guest_module  # noqa: E402
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


class JournalTests(unittest.TestCase):
    def _event(self, name: str, **fields: object) -> dict[str, object]:
        return {"timestamp": "2026-01-01T00:00:00Z", "event": name, **fields}

    def test_sqlite_round_trip_and_ordering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index in range(3):
                journal_module.append(
                    root, "sqlite",
                    {"timestamp": f"2026-01-0{index + 1}T00:00:00Z",
                     "event": "lease-begin", "lease": f"L{index}"},
                )
            events = journal_module.query(root, limit=10)
            self.assertEqual(len(events), 3)
            self.assertEqual(events[0]["lease"], "L2", "newest first")
            self.assertEqual(
                journal_module.query(root, lease="L1")[0]["lease"], "L1"
            )

    def test_event_wildcard_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            journal_module.append(root, "sqlite", self._event("lease-begin"))
            journal_module.append(root, "sqlite", self._event("lease-end"))
            journal_module.append(root, "sqlite", self._event("guest-run"))
            self.assertEqual(len(journal_module.query(root, event="lease-*")), 2)

    def test_query_on_a_fresh_install_is_empty_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(journal_module.query(Path(tmp)), [])
            self.assertFalse(journal_module.summary(Path(tmp))["exists"])

    def test_jsonl_backend_still_works(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            journal_module.append(root, "jsonl", self._event("lease-begin"))
            written = list(root.glob("*.jsonl"))
            self.assertEqual(len(written), 1)
            self.assertIn("lease-begin", written[0].read_text())

    def test_legacy_jsonl_can_be_imported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            journal_module.append(root, "jsonl", self._event("lease-begin"))
            journal_module.append(root, "jsonl", self._event("lease-end"))
            self.assertEqual(journal_module.import_jsonl(root), 2)
            self.assertEqual(len(journal_module.query(root)), 2)

    def test_summary_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for _ in range(4):
                journal_module.append(root, "sqlite",
                                      self._event("guest-run", lease="L1"))
            summary = journal_module.summary(root)
            self.assertEqual(summary["events"], 4)
            self.assertEqual(summary["distinct_leases"], 1)
            self.assertEqual(summary["most_common"]["guest-run"], 4)


class GuestChannelTests(unittest.TestCase):
    """Channel selection is the whole point: callers should not have to know
    whether a guest has an agent."""

    def _lab(self, config: dict[str, object], agent: bool):
        lab = mock.Mock()
        lab.LabError = RuntimeError
        lab.NODE = "testnode"
        api = mock.Mock()
        api.call.return_value = config
        return lab, api, mock.patch.object(
            guest_module.console, "agent_ready", return_value=agent
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
