"""Connection handoffs must preserve settings without exposing credentials."""
from __future__ import annotations

import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import bootstrap  # noqa: E402,F401
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from proxmox_agent_lab import cli, config, connection, secrets_store


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.settings = config.load(Path(__file__).parent / "fixtures/config.toml")
        self.settings.audit._values["controller_id"] = "original-controller"
        self.secret = "fixture-'\"\\\n$(touch must-not-exist)`false`"
        self.reader = mock.patch.object(secrets_store, "get", side_effect=lambda cfg, name, **kw: self.secret if name == "proxmox-token" else "")
        self.reader.start()
        self.addCleanup(self.reader.stop)

    def test_roundtrip_is_private_and_has_fresh_local_paths(self):
        original = copy.deepcopy(self.settings.as_dict())
        with tempfile.TemporaryDirectory() as tmp:
            bundle = connection.export_bundle(self.settings)
            root = Path(tmp) / "recipient"
            target = connection.import_bundle(bundle, root)
            loaded = config.load(target)
            self.assertEqual(loaded.proxmox.host, self.settings.proxmox.host)
            self.assertEqual(loaded.audit.controller_id, "")
            self.assertEqual(loaded.audit.journal_dir, "")
            self.assertEqual(loaded.secrets.file_path, str(root / "secrets.toml"))
            self.assertEqual(secrets_store._read_file_secret(loaded, "proxmox-token"), self.secret)
            self.assertNotIn(self.secret, target.read_text())
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            for path in root.iterdir():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertFalse((root / "leases").exists())
        self.assertEqual(original, self.settings.as_dict())

    def test_ca_and_opt_in_ssh_key_are_relocated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ca, key = root / "source-ca", root / "source-key"
            ca.write_text("fixture CA")
            key.write_text("fixture SSH material")
            self.settings.proxmox._values["ca_file"] = str(ca)
            self.settings.memflow._values.update(enabled=True, ssh_key=str(key), ssh_options="-i /old/path")
            basic = connection.export_bundle(self.settings)
            self.assertFalse(basic["config"]["memflow"]["enabled"])
            self.assertNotIn("host-ssh-key", basic["files"])
            shared = connection.export_bundle(self.settings, include_ssh_key=True)
            target = connection.import_bundle(shared, root / "recipient")
            loaded = config.load(target)
            self.assertEqual(Path(loaded.proxmox.ca_file).read_text(), "fixture CA")
            self.assertEqual(Path(loaded.memflow.ssh_key).read_text(), "fixture SSH material")
            self.assertTrue(loaded.memflow.enabled)
            self.assertEqual(loaded.memflow.ssh_options, "")

    def test_existing_destination_is_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sentinel = root / "config.toml"
            sentinel.write_text("existing")
            with self.assertRaisesRegex(cli.LabError, "already exists"):
                connection.import_bundle(connection.export_bundle(self.settings), root)
            self.assertEqual(sentinel.read_text(), "existing")
            self.assertEqual(list(root.iterdir()), [sentinel])

    def test_malformed_bundles_fail_before_writes_without_echoing_secrets(self):
        original = connection.export_bundle(self.settings)
        cases = []
        for field, value in [("files", {"../escape": self.secret}), ("config", {}), ("secrets", {}), ("version", 99)]:
            bundle = copy.deepcopy(original)
            bundle[field] = value
            cases.append(bundle)
        bad = copy.deepcopy(original)
        bad["config"]["proxmox"]["port"] = self.secret
        cases.append(bad)
        with tempfile.TemporaryDirectory() as tmp:
            for bundle in cases:
                with self.assertRaises(cli.LabError) as caught:
                    connection.import_bundle(bundle, Path(tmp) / "recipient")
                self.assertNotIn(self.secret, str(caught.exception))
                self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_failed_write_cleans_up_new_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "recipient"
            with mock.patch.object(connection, "_private_write", side_effect=OSError("fixture")), self.assertRaises(OSError):
                connection.import_bundle(connection.export_bundle(self.settings), root)
            self.assertFalse(root.exists())

    def test_shell_block_passes_credentials_only_to_import_stdin(self):
        # Execute the generated block with a fake interpreter/CLI. This catches
        # heredoc escaping, shell injection, and accidental secret argv usage.
        bundle = connection.export_bundle(self.settings)
        script = connection.shell_handoff(bundle)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            envdir = root / ".local/share/proxmox-agent-lab" / ("connection-env-" + cli.__version__)
            bindir = envdir / "bin"
            bindir.mkdir(parents=True)
            fake = root / "python3.14"
            fake.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$TEST_ARGV"\nexit 0\n')
            fake.chmod(0o700)
            (bindir / "python").symlink_to(fake)
            receiver = bindir / "proxmox-lab"
            receiver.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$TEST_ARGV"\ncat > "$TEST_STDIN"\n')
            receiver.chmod(0o700)
            environment = {**os.environ, "HOME": tmp, "PATH": tmp + ":/usr/bin:/bin",
                           "TEST_ARGV": str(root / "argv"), "TEST_STDIN": str(root / "stdin")}
            run = subprocess.run(["bash"], input=script, text=True, capture_output=True, env=environment, cwd=tmp, timeout=10)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(json.loads((root / "stdin").read_text()), bundle)
            self.assertNotIn(self.secret, (root / "argv").read_text())
            self.assertNotIn(self.secret, run.stdout + run.stderr)
            self.assertFalse((root / "must-not-exist").exists())

    def test_output_file_is_private_and_commands_do_not_audit_payload(self):
        import contextlib
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(cli, "CONFIG", self.settings), mock.patch.object(cli, "audit") as audit:
            output = Path(tmp) / "handoff"
            args = cli.parser().parse_args(["connection", "export", "--out", str(output)])
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
                args.func(args)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(self.secret, stdout.getvalue())
            audit.assert_not_called()
            with self.assertRaisesRegex(cli.LabError, "Output already exists"):
                args.func(args)


class SharedFileBootstrapTests(unittest.TestCase):
    def test_shared_store_uses_file_bootstrap_without_environment(self):
        from proxmox_agent_lab import mariadb
        settings = config.load(Path(__file__).parent / "fixtures/config.toml")
        settings.secrets._values["backend"] = "file"
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(secrets_store, "_read_file_secret", side_effect=lambda cfg, name: "fixture-password" if name == "mariadb-password" else None), \
             mock.patch.object(mariadb, "get_secret", return_value="fixture-shared") as fetch:
            self.assertEqual(secrets_store.get(settings, "s3-secret-key"), "fixture-shared")
            self.assertEqual(fetch.call_args.args[0].password, "fixture-password")
            fetch.assert_called_once()


class PrivateSecretWriteTests(unittest.TestCase):
    def test_secret_file_is_private_before_becoming_visible(self):
        settings = config.load(Path(__file__).parent / "fixtures/config.toml")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "secrets.toml"
            settings.secrets._values.update(backend="file", file_path=str(target))
            replace = os.replace
            def check_private(source, dest):
                self.assertEqual(Path(source).stat().st_mode & 0o777, 0o600)
                self.assertFalse(target.exists())
                replace(source, dest)
            with mock.patch.object(secrets_store.os, "replace", side_effect=check_private):
                secrets_store.store(settings, "fixture.name", "fixture-value")
            self.assertEqual(secrets_store._read_file_secret(settings, "fixture.name"), "fixture-value")
            self.assertEqual(list(Path(tmp).iterdir()), [target])
