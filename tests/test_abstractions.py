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

    def test_git_sync_pushes_only_the_daily_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            repo = root / "logs"
            subprocess.run(["git", "init", "--bare", str(remote)], check=True,
                           stdout=subprocess.DEVNULL)
            subprocess.run(["git", "init", "-b", "logs", str(repo)], check=True,
                           stdout=subprocess.DEVNULL)
            for key, value in (("user.name", "Test"),
                               ("user.email", "test@example.invalid")):
                subprocess.run(
                    ["git", "-C", str(repo), "config", key, value], check=True
                )
            (repo / "README.md").write_text("private audit logs\n")
            subprocess.run(["git", "-C", str(repo), "add", "README.md"],
                           check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"],
                           check=True, stdout=subprocess.DEVNULL)
            subprocess.run(
                ["git", "-C", str(repo), "remote", "add", "origin", str(remote)],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repo), "push", "-u", "origin", "logs"],
                check=True, stdout=subprocess.DEVNULL,
            )

            journal_module.sync_git(
                repo, self._event("lease-begin", vmid=9001), "lease-begin"
            )
            logged = subprocess.run(
                ["git", "--git-dir", str(remote), "show",
                 "logs:journal/2026-01-01.jsonl"],
                check=True, text=True, stdout=subprocess.PIPE,
            ).stdout
            self.assertIn('"event": "lease-begin"', logged)
            changed = subprocess.run(
                ["git", "--git-dir", str(remote), "diff-tree", "--no-commit-id",
                 "--name-only", "-r", "logs"],
                check=True, text=True, stdout=subprocess.PIPE,
            ).stdout.splitlines()
            self.assertEqual(changed, ["journal/2026-01-01.jsonl"])

    def test_git_sync_retries_a_rejected_non_fast_forward_push(self) -> None:
        # A competing writer that pushes between our fetch and our push leaves
        # the rebase against a stale origin; the push is then rejected as
        # non-fast-forward and sync_git must refetch, rebase and retry.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            repo = root / "logs"
            subprocess.run(["git", "init", "--bare", str(remote)], check=True,
                           stdout=subprocess.DEVNULL)
            subprocess.run(["git", "init", "-b", "logs", str(repo)], check=True,
                           stdout=subprocess.DEVNULL)
            for key, value in (("user.name", "Test"),
                               ("user.email", "test@example.invalid")):
                subprocess.run(
                    ["git", "-C", str(repo), "config", key, value], check=True
                )
            (repo / "README.md").write_text("private audit logs\n")
            subprocess.run(["git", "-C", str(repo), "add", "README.md"],
                           check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"],
                           check=True, stdout=subprocess.DEVNULL)
            subprocess.run(
                ["git", "-C", str(repo), "remote", "add", "origin", str(remote)],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repo), "push", "-u", "origin", "logs"],
                check=True, stdout=subprocess.DEVNULL,
            )

            competing = root / "competing"
            subprocess.run(["git", "clone", "-q", str(remote), str(competing)],
                           check=True, stdout=subprocess.DEVNULL)
            for key, value in (("user.name", "Test"),
                               ("user.email", "test@example.invalid")):
                subprocess.run(
                    ["git", "-C", str(competing), "config", key, value],
                    check=True,
                )
            subprocess.run(
                ["git", "-C", str(competing), "checkout", "-b", "logs",
                 "origin/logs"],
                check=True, stdout=subprocess.DEVNULL,
            )

            real_run = subprocess.run
            injected = {"done": False}

            def push_with_competing_writer(command, **kwargs):
                # On sync_git's first push attempt, land a conflicting commit
                # on origin first so the push is rejected as non-fast-forward.
                if not injected["done"] and command[:2] == ["git", "-C"] \
                        and command[3:4] == ["push"]:
                    injected["done"] = True
                    log = competing / "journal" / "2026-01-02.jsonl"
                    log.parent.mkdir(parents=True, exist_ok=True)
                    log.write_text('{"event": "competing"}\n')
                    real_run(["git", "-C", str(competing), "add",
                              "journal/2026-01-02.jsonl"], check=True,
                             stdout=subprocess.DEVNULL)
                    real_run(["git", "-C", str(competing), "commit",
                              "-m", "competing"], check=True,
                             stdout=subprocess.DEVNULL)
                    real_run(["git", "-C", str(competing), "push",
                              "origin", "logs"], check=True,
                             stdout=subprocess.DEVNULL)
                return real_run(command, **kwargs)

            with mock.patch.object(journal_module.subprocess, "run",
                                   side_effect=push_with_competing_writer), \
                 mock.patch.object(journal_module, "SYNC_GIT_RETRY_DELAY", 0):
                journal_module.sync_git(
                    repo, self._event("lease-begin", vmid=9001), "lease-begin"
                )
            self.assertTrue(injected["done"], "the competing push never ran")
            logged = subprocess.run(
                ["git", "--git-dir", str(remote), "show",
                 "logs:journal/2026-01-01.jsonl"],
                check=True, text=True, stdout=subprocess.PIPE,
            ).stdout
            self.assertIn('"event": "lease-begin"', logged)
            competing_log = subprocess.run(
                ["git", "--git-dir", str(remote), "show",
                 "logs:journal/2026-01-02.jsonl"],
                check=True, text=True, stdout=subprocess.PIPE,
            ).stdout
            self.assertIn('"event": "competing"', competing_log)

    def test_git_sync_refuses_a_mixed_purpose_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init", "-b", "logs", str(repo)], check=True,
                           stdout=subprocess.DEVNULL)
            (repo / "source.py").write_text("do_not_commit = True\n")
            with self.assertRaisesRegex(RuntimeError, "dirty"):
                journal_module.sync_git(
                    repo, self._event("lease-begin"), "lease-begin"
                )
            self.assertFalse((repo / "journal").exists())

    def test_legacy_jsonl_can_be_imported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            journal_module.append(root, "jsonl", self._event("lease-begin"))
            journal_module.append(root, "jsonl", self._event("lease-end"))
            self.assertEqual(journal_module.import_jsonl(root), 2)
            self.assertEqual(len(journal_module.query(root)), 2)

    def test_jsonl_query_reads_what_the_jsonl_backend_wrote(self) -> None:
        """Found live: query() opened journal.db whatever the backend was, so a
        configured JSONL ledger reported no events at all -- worst of all
        during an incident review."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index in range(3):
                journal_module.append(
                    root, "jsonl",
                    {"timestamp": f"2026-01-0{index + 1}T00:00:00Z",
                     "event": "lease-begin", "lease": f"L{index}", "vmid": 1},
                )
            events = journal_module.query(root, backend="jsonl")
            self.assertEqual(len(events), 3)
            self.assertEqual(events[0]["lease"], "L2", "newest first")
            self.assertFalse(journal_module.database_path(root).exists())

    def test_jsonl_query_applies_the_same_filters_as_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for backend in ("sqlite", "jsonl"):
                journal_module.append(
                    root, backend,
                    {"timestamp": "2026-01-01T00:00:00Z",
                     "event": "lease-begin", "lease": "L1"},
                )
                journal_module.append(
                    root, backend,
                    {"timestamp": "2026-01-02T00:00:00Z",
                     "event": "lease-end", "lease": "L2"},
                )
                journal_module.append(
                    root, backend,
                    {"timestamp": "2026-01-03T00:00:00Z",
                     "event": "guest-run", "lease": "L1"},
                )
            for backend in ("sqlite", "jsonl"):
                with self.subTest(backend=backend):
                    self.assertEqual(
                        len(journal_module.query(root, lease="L1",
                                                 backend=backend)), 2)
                    self.assertEqual(
                        len(journal_module.query(root, event="lease-*",
                                                 backend=backend)), 2)
                    self.assertEqual(
                        len(journal_module.query(root, since="2026-01-02",
                                                 backend=backend)), 2)
                    self.assertEqual(
                        len(journal_module.query(root, limit=1,
                                                 backend=backend)), 1)

    def test_jsonl_summary_counts_the_jsonl_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertFalse(
                journal_module.summary(root, backend="jsonl")["exists"]
            )
            for _ in range(4):
                journal_module.append(root, "jsonl",
                                      self._event("guest-run", lease="L1"))
            summary = journal_module.summary(root, backend="jsonl")
            self.assertTrue(summary["exists"])
            self.assertEqual(summary["events"], 4)
            self.assertEqual(summary["distinct_leases"], 1)
            self.assertEqual(summary["most_common"]["guest-run"], 4)
            self.assertEqual(summary["backend"], "jsonl")

    def test_git_sync_status_reports_an_unusable_mirror(self) -> None:
        """Found live: git_sync pointed at a path that did not exist, every
        command printed a warning nobody read, and doctor said nothing."""
        with tempfile.TemporaryDirectory() as tmp:
            missing = journal_module.git_sync_status(Path(tmp) / "nope")
            self.assertFalse(missing["ok"])
            self.assertIn("does not exist", missing["problem"])

            plain = Path(tmp) / "plain"
            plain.mkdir()
            not_a_repo = journal_module.git_sync_status(plain)
            self.assertFalse(not_a_repo["ok"])
            self.assertIn("not a git repository", not_a_repo["problem"])

            repo = Path(tmp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "logs", str(repo)], check=True,
                           stdout=subprocess.DEVNULL)
            self.assertTrue(journal_module.git_sync_status(repo)["ok"])

            (repo / "source.py").write_text("do_not_commit = True\n")
            dirty = journal_module.git_sync_status(repo)
            self.assertFalse(dirty["ok"])
            self.assertIn("dirty", dirty["problem"])

    def test_git_sync_status_refuses_an_unsafe_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status = journal_module.git_sync_status(Path(tmp), "logs/../../x")
            self.assertFalse(status["ok"])
            self.assertIn("unsafe", status["problem"])

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
