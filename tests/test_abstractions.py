"""Tests for the layers that hide platform differences: config and audit.

Rewritten for the SQLite/SSH rework. The credential-store, ledger-queue,
guest-channel and inventory-registry layers this file used to cover are cut
or rewritten elsewhere; what survives here is the config surface
(docs/rework-plan.md §G plus the transitional namespaces the dying modules
still read) and the audit facade over store.py (redaction, one event per
action, never fails the action).
"""

from __future__ import annotations

import os
from pathlib import Path

import sys  # noqa: E402

# Shared bootstrap: fixture configuration plus a per-process state directory,
# applied before any proxmox_agent_lab import. `support` sits beside this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import bootstrap  # noqa: E402,F401
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402
from unittest import mock  # noqa: E402

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from proxmox_agent_lab import audit as audit_module  # noqa: E402
from proxmox_agent_lab import config as config_module  # noqa: E402
from proxmox_agent_lab import store as store_module  # noqa: E402


class ConfigTests(unittest.TestCase):
    def test_missing_file_is_not_an_error(self) -> None:
        """`init` must be able to run before a config exists."""
        with tempfile.TemporaryDirectory() as tmp:
            absent = Path(tmp) / "nope.toml"
            config = config_module.load(absent)
        self.assertFalse(config.configured)
        self.assertEqual(config.intended, absent)
        self.assertEqual(config.ssh.target, "")
        self.assertEqual(config.lease.ttl_seconds, 7200)

    def test_a_misspelled_section_is_reported_but_not_fatal(self) -> None:
        """It is surfaced by `doctor` rather than discarding the config."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.toml"
            path.write_text('[proxmoxx]\nhost = "x"\n\n[pve]\nnode = "n"\n')
            config = config_module.load(path)
        self.assertEqual(config.unknown_sections, ["proxmoxx"])
        self.assertEqual(config.pve.node, "n")

    def test_require_names_the_setting_and_the_file(self) -> None:
        config = config_module.defaults()
        with self.assertRaises(config_module.ConfigError) as caught:
            config.require("ssh.target", "the ssh target")
        message = str(caught.exception)
        self.assertIn("[ssh] target", message)
        self.assertIn("the ssh target", message)

    def test_partial_config_keeps_defaults_for_everything_else(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.toml"
            path.write_text('[pve]\nnode = "n"\n')
            config = config_module.load(path)
        self.assertEqual(config.pve.node, "n")
        self.assertEqual(config.pve.template_vmid, 0)
        self.assertEqual(config.power.port, 9)
        self.assertEqual(config.lease.idle_shutdown_seconds, 28800)

    def test_every_module_shares_one_instance(self) -> None:
        self.assertIs(config_module.get(), config_module.get())

    def test_the_template_it_writes_is_valid_and_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.toml"
            path.write_text(config_module.TEMPLATE)
            config = config_module.load(path)   # must not raise
        self.assertTrue(config.configured)
        self.assertEqual(config.unknown_sections, [], "template uses only known keys")
        self.assertEqual(config.ssh.target, "proxmox")
        self.assertEqual(config.pve.node, "pve")
        self.assertEqual(config.pve.template_vmid, 100)
        self.assertEqual(config.power.port, 9)
        self.assertEqual(config.state.dir, "~/.local/share/proxmox-agent-lab")
        self.assertEqual(config.lease.ttl_seconds, 7200)
        self.assertEqual(config.lease.idle_shutdown_seconds, 28800)

    def test_the_nine_key_schema_resolves_from_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.toml"
            path.write_text(
                '[ssh]\ntarget = "box"\n'
                '[pve]\nnode = "alpha"\ntemplate_vmid = 9025\n'
                '[power]\nmac = "aa:bb:cc:dd:ee:ff"\n'
                'broadcast = "192.0.2.255"\nport = 7\n'
                '[state]\ndir = "/tmp/pxl-state"\n'
                '[lease]\nttl_seconds = 3600\nidle_shutdown_seconds = 100\n'
            )
            config = config_module.load(path)
        self.assertEqual(config.ssh.target, "box")
        self.assertEqual(config.pve.node, "alpha")
        self.assertEqual(config.pve.template_vmid, 9025)
        self.assertEqual(config.power.mac, "aa:bb:cc:dd:ee:ff")
        self.assertEqual(config.power.broadcast, "192.0.2.255")
        self.assertEqual(config.power.port, 7)
        self.assertEqual(config.state.dir, "/tmp/pxl-state")
        self.assertEqual(config.lease.ttl_seconds, 3600)
        self.assertEqual(config.lease.idle_shutdown_seconds, 100)

    def test_the_fixture_loads_through_the_new_loader(self) -> None:
        config = config_module.get()   # the fixture tests/support/bootstrap pinned
        self.assertTrue(config.configured)
        self.assertEqual(config.unknown_sections, [], "fixture uses only known keys")
        self.assertEqual(config.ssh.target, "fixture-host")
        self.assertEqual(config.pve.node, "pve")
        self.assertEqual(config.pve.template_vmid, 9025)
        self.assertEqual(config.power.mac, "aa:bb:cc:dd:ee:ff")
        self.assertEqual(config.lease.ttl_seconds, 7200)
        self.assertEqual(config.lease.idle_shutdown_seconds, 28800)

    def test_state_dir_expands_the_state_dir_setting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.toml"
            path.write_text('[state]\ndir = "~/pxl-state-under-test"\n')
            with mock.patch.dict(os.environ,
                                 {config_module.ENV_CONFIG: str(path),
                                  config_module.ENV_STATE: ""}):
                config_module.reset_cache()
                try:
                    self.assertEqual(config_module.state_dir(),
                                     Path.home() / "pxl-state-under-test")
                finally:
                    config_module.reset_cache()

    def test_the_state_env_override_still_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict(os.environ, {config_module.ENV_STATE: tmp}):
            self.assertEqual(config_module.state_dir(), Path(tmp))

    def test_a_broken_file_is_an_error_only_when_loaded_explicitly(self) -> None:
        """The split `doctor` relies on: `load()` reports the broken file,
        while every import-time reader (`get()`) survives it."""
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "c.toml"
            broken.write_text("[ssh\ntarget = ")
            with self.assertRaises(config_module.ConfigError) as caught:
                config_module.load(broken)
            self.assertIn(str(broken), str(caught.exception))
            with mock.patch.dict(os.environ, {config_module.ENV_CONFIG: str(broken)}):
                config_module.reset_cache()
                try:
                    config = config_module.get()   # the import-time path
                    self.assertFalse(config.configured)
                    self.assertEqual(config.ssh.target, "")
                    self.assertIn(str(broken), config_module.CONFIG_ERROR or "")
                finally:
                    config_module.reset_cache()

    def test_a_fresh_interpreter_imports_with_missing_and_broken_config(self) -> None:
        program = (
            "import proxmox_agent_lab.config as c;"
            "c.get();"
            "print(c.CONFIG_ERROR or '')"
        )
        env_base = {
            **os.environ,
            "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
        }
        for label, contents in (("missing", None),
                                ("malformed", "[ssh\ntarget = ")):
            with self.subTest(config=label), tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "c.toml"
                if contents is not None:
                    target.write_text(contents)
                env = {
                    **env_base,
                    config_module.ENV_CONFIG: str(target),
                    config_module.ENV_STATE: str(Path(tmp) / "state"),
                }
                result = subprocess.run(
                    [sys.executable, "-c", program],
                    env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                if contents is None:
                    self.assertEqual(result.stdout.strip(), "")
                else:
                    self.assertIn("not valid TOML", result.stdout)


class ConfigDiscoveryTests(unittest.TestCase):
    def test_the_env_variable_is_the_first_choice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            chosen = Path(tmp) / "chosen.toml"
            with mock.patch.dict(os.environ, {config_module.ENV_CONFIG: str(chosen)}), \
                 mock.patch.object(config_module.Path, "cwd",
                                   return_value=Path(tmp)):
                self.assertEqual(config_module.config_path(), chosen)

    def test_a_checkout_config_is_the_next_choice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / f"{config_module.APP_NAME}.toml"
            local.write_text("")
            with mock.patch.dict(os.environ, {config_module.ENV_CONFIG: ""}), \
                 mock.patch.object(config_module.Path, "cwd",
                                   return_value=Path(tmp)):
                self.assertEqual(config_module.config_path(), local)

    def test_xdg_then_the_user_path_come_last(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {config_module.ENV_CONFIG: "",
                                              "XDG_CONFIG_HOME": tmp}):
                self.assertEqual(
                    config_module.config_path(),
                    Path(tmp) / config_module.APP_NAME / "config.toml",
                )
            home = Path(tmp) / "home"
            with mock.patch.dict(os.environ, {config_module.ENV_CONFIG: "",
                                              "XDG_CONFIG_HOME": "",
                                              "HOME": str(home)}):
                self.assertEqual(
                    config_module.config_path(),
                    home / ".config" / config_module.APP_NAME / "config.toml",
                )


class ConfigForwardCompatibilityTests(unittest.TestCase):
    def test_an_unknown_section_is_ignored_not_fatal(self) -> None:
        """A leftover section from another version must not discard the whole
        config; that presents as 'target is not set' and misdirects entirely."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.toml"
            path.write_text('[ssh]\ntarget = "x"\n\n[frombuture]\nx = 1\n')
            config = config_module.load(path)
        self.assertTrue(config.configured)
        self.assertEqual(config.ssh.target, "x")
        self.assertEqual(config.unknown_sections, ["frombuture"])


class AuditTests(unittest.TestCase):
    """The audit facade over store.py: every action appends one redacted
    event, and an unrecordable event is reported, never raised into the
    action."""

    def test_redaction_masks_secrets_at_any_depth(self) -> None:
        masked = audit_module.redact({
            "password": "a",
            "API_Token": "b",
            "ssh_key": "c",
            "nested": {"private-key": "d", "keep": "visible"},
            "items": [{"secret": "e"}],
            "auth": "Bearer xyz",
            "ticket": "PVEAPI" + "Token=user-at-pve-name-zzz",
        })
        self.assertEqual(masked["password"], "[REDACTED]")
        self.assertEqual(masked["API_Token"], "[REDACTED]")
        self.assertEqual(masked["ssh_key"], "[REDACTED]")
        self.assertEqual(masked["nested"]["private-key"], "[REDACTED]")
        self.assertEqual(masked["nested"]["keep"], "visible")
        self.assertEqual(masked["items"][0]["secret"], "[REDACTED]")
        self.assertEqual(masked["auth"], "[REDACTED]")
        self.assertEqual(masked["ticket"], "[REDACTED]")

    def test_redaction_truncates_long_strings(self) -> None:
        self.assertEqual(audit_module.redact("x" * 5000), "x" * 1000)

    def test_one_action_writes_one_redacted_event(self) -> None:
        audit_module.audit("guest-run", lease="abs-audit-row", vmid=9246,
                           note={"token": "sekrit"})
        with store_module.Store(config_module.state_dir() / "lab.db") as db:
            rows = db.query_events(lease="abs-audit-row")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["event"], "guest-run")
        self.assertEqual(row["vmid"], 9246)
        self.assertNotIn("sekrit", row["data"] or "")
        self.assertIn("[REDACTED]", row["data"] or "")

    def test_a_broken_store_never_fails_the_action(self) -> None:
        with mock.patch.object(store_module.Store, "record",
                               side_effect=RuntimeError("disk full")):
            audit_module.audit("guest-run", lease="abs-audit-broken")


if __name__ == "__main__":
    unittest.main()
