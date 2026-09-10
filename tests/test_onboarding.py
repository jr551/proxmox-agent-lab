from __future__ import annotations

import argparse
import contextlib
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import socket
import ssl
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from unittest import mock

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import bootstrap  # noqa: E402,F401
sys.path.insert(0, str(ROOT / "src"))
from proxmox_agent_lab import cli, config, host_policy, onboarding as ob, onboarding_host as host


class OnboardingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.directory = Path(cls.temp.name) / "bundle"
        args = cli.parser().parse_args([
            "onboard", "prepare", "--mode", "vps", "--directory", str(cls.directory),
            "--controller-host", "127.0.0.1", "--api-host", "pve.example.invalid", "--fqdn", "pve.example.invalid",
        ])
        ob.prepare(args)
        cls.settings = ob.read_bundle(cls.directory)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def payload(self):
        return {"id": self.settings["id"], "mode": "vps", "host": "pve.example.invalid", "node": "pve",
                "token_user": f"pxl-{self.settings['id']}@pve", "token_name": "controller",
                "token_secret": "00000000-0000-4000-8000-000000000000", "ca": self.settings["controller_ca"], "wol_mac": ""}

    def test_generated_payload_compiles_and_bundle_is_private(self):
        code = (self.directory / "host-setup.py").read_text()
        compile(code, "host-setup.py", "exec")
        self.assertLess(len(code.encode()), 1024 * 1024)
        for name in ("host-setup.py", "receiver.key", "pairing.json"):
            self.assertEqual((self.directory / name).stat().st_mode & 0o077, 0)
        self.assertNotIn("PRIVATE KEY", code)

    def test_answer_uses_exact_disk_and_hashed_password(self):
        text = ob.answer_file(self.settings, "$6$salt$hash", "EXAMPLE_DISK_ID", "gb", "Europe/London", "en-gb", "root@example.invalid")
        answer = tomllib.loads(text)
        self.assertEqual(answer["disk-setup"]["filter"], {"ID_SERIAL": "EXAMPLE_DISK_ID"})
        self.assertNotIn("root-password", answer["global"])
        self.assertEqual(answer["first-boot"], {"source": "from-iso", "ordering": "fully-up"})
        for serial in ("", "*", "sda?", "[abc]", "bad\nserial"):
            with self.subTest(serial=serial), self.assertRaises(ValueError):
                ob.answer_file(self.settings, "$6$salt$hash", serial, "gb", "UTC", "en-us", "root@example.invalid")

    def test_iso_requires_explicit_disk_wipe_choice_before_writes(self):
        args = cli.parser().parse_args(["onboard", "prepare", "--mode", "iso", "--directory", "/not-created",
                                        "--controller-host", "pc.example.invalid", "--fqdn", "pve.example.invalid"])
        with mock.patch.object(Path, "mkdir") as mkdir, self.assertRaisesRegex(ValueError, "wipe-confirmed"):
            ob.prepare(args)
        mkdir.assert_not_called()

    def test_existing_mode_onboards_installed_proxmox_without_disk_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "bundle"
            args = cli.parser().parse_args([
                "onboard", "prepare", "--mode", "existing", "--directory", str(directory),
                "--controller-host", "127.0.0.1", "--fqdn", "pve.example.invalid",
            ])
            result = ob.prepare(args)
            self.assertEqual(result["mode"], "existing")
            self.assertIn("host-setup.py", result["next"])
            self.assertNotIn("reboot-authorized", result["next"])
            compile((directory / "host-setup.py").read_text(), "host-setup.py", "exec")
            self.assertFalse((directory / "answer.toml").exists())
            settings = ob.read_bundle(directory)
            self.assertEqual(settings["mode"], "existing")
            # Disk/partition options are meaningless for an installed host.
            bad = cli.parser().parse_args([
                "onboard", "prepare", "--mode", "existing", "--directory", str(Path(tmp) / "b2"),
                "--controller-host", "127.0.0.1", "--fqdn", "pve.example.invalid",
                "--disk-serial", "X", "--root-password-hash-file", "/dev/null", "--wipe-confirmed",
            ])
            with self.assertRaisesRegex(ValueError, "does not partition disks"):
                ob.prepare(bad)

    def test_existing_mode_accept_grants_full_guest_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            settings = {**self.settings, "mode": "existing"}
            payload = {**self.payload(), "mode": "existing", "wol_mac": "aa:bb:cc:dd:ee:ff"}
            ob.private_write(directory / "enrollment.json", json.dumps(payload))
            output = directory / "config.toml"
            response = io.BytesIO(b'{"data":{"uptime":123}}')
            with mock.patch.object(ob, "read_bundle", return_value=settings), \
                 mock.patch.object(ob.request.OpenerDirector, "open", return_value=response):
                self.assertTrue(ob.accept(directory, output)["api_verified"])
            configured = config.load(output)
            self.assertEqual(configured.proxmox.guest_mode, "all")

    def test_wifi_passphrase_is_derived_and_never_in_generated_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            password = root / "wifi"
            password.write_text('example " wifi passphrase\n')
            hashed = root / "hashed"
            hashed.write_text("$6$salt$hash")
            args = cli.parser().parse_args([
                "onboard", "prepare", "--mode", "iso", "--directory", str(root / "bundle"),
                "--controller-host", "pc.example.invalid", "--fqdn", "pve.example.invalid", "--disk-serial", "EXAMPLE",
                "--root-password-hash-file", str(hashed), "--wipe-confirmed", "--wifi-ssid", 'Example " WLAN',
                "--wifi-interface", "wlan0", "--wifi-password-file", str(password),
            ])
            # Reuse the real test certificate without another RSA key generation.
            def certificate(*args, **kwargs):
                dest = root / "bundle"
                (dest / "receiver.key").write_text("fixture")
                (dest / "receiver.crt").write_text(self.settings["controller_ca"])
            with mock.patch.object(ob.subprocess, "run", side_effect=certificate):
                ob.prepare(args)
            settings = ob.read_bundle(root / "bundle")
            self.assertEqual(len(settings["wifi"]["psk"]), 64)
            self.assertNotIn(password.read_text().strip(), (root / "bundle/host-setup.py").read_text())
            self.assertEqual(bytes.fromhex(settings["wifi"]["ssid_hex"]).decode(), 'Example " WLAN')

    def test_private_write_refuses_overwrite_and_is_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state"
            ob.private_write(path, "first")
            with self.assertRaises(FileExistsError):
                ob.private_write(path, "second")
            self.assertEqual(path.read_text(), "first")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_callback_tls_authentication_and_one_host_replay(self):
        receipt = self.directory / "enrollment.json"
        receipt.unlink(missing_ok=True)
        settings = {**self.settings, "port": 0}
        with mock.patch.object(ob, "read_bundle", return_value=settings):
            server = ob.receiver(self.directory, "127.0.0.1")
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        context = ssl.create_default_context(cadata=self.settings["controller_ca"])
        def post(payload, token=None):
            connection = http.client.HTTPSConnection("127.0.0.1", server.server_port, context=context, timeout=3)
            try:
                connection.request("POST", "/enroll", json.dumps(payload), {"Authorization": "Bearer " + (token or self.settings["token"])})
                response = connection.getresponse()
                response.read()
                return response.status
            finally:
                connection.close()
        try:
            self.assertEqual(post(self.payload(), "wrong"), 403)
            self.assertFalse(receipt.exists())
            self.assertEqual(post({**self.payload(), "host": "other.example.invalid"}), 400)
            self.assertFalse(receipt.exists())
            self.assertEqual(post(self.payload()), 200)
            self.assertEqual(post(self.payload()), 200)
            changed = {**self.payload(), "token_secret": "11111111-1111-4111-8111-111111111111"}
            self.assertEqual(post(changed), 409)
            self.assertEqual(json.loads(receipt.read_text()), self.payload())
            self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
        finally:
            server.shutdown()
            thread.join()
            server.server_close()
            receipt.unlink(missing_ok=True)

    def test_host_callback_reaches_real_pinned_tls_receiver(self):
        receipt = self.directory / "enrollment.json"
        receipt.unlink(missing_ok=True)
        with mock.patch.object(ob, "read_bundle", return_value={**self.settings, "port": 0}):
            server = ob.receiver(self.directory, "127.0.0.1")
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            settings = {**self.settings, "callback_url": f"https://127.0.0.1:{server.server_port}/enroll"}
            host.callback(settings, self.payload())
            self.assertEqual(json.loads(receipt.read_text()), self.payload())
        finally:
            server.shutdown()
            thread.join()
            server.server_close()
            receipt.unlink(missing_ok=True)

    def test_callback_does_not_follow_redirects_with_credentials(self):
        self.assertIsNone(host.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example.invalid"))

    def test_enrollment_rejects_wrong_identity(self):
        for key, value in [("id", "other"), ("mode", "iso"), ("node", "other"), ("token_user", "root@pam"),
                           ("host", "pve.example.invalid/path"), ("ca", "not a certificate")]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                ob.validate_enrollment({**self.payload(), key: value}, self.settings)

    def test_discovery_has_no_secrets_and_rejects_forgery_and_stale_packets(self):
        packet = host.beacon(self.settings, self.payload())
        self.assertNotIn(self.settings["token"].encode(), packet)
        self.assertNotIn(self.payload()["token_secret"].encode(), packet)
        self.assertEqual(ob.verify_beacon(packet, self.settings)["host"], self.payload()["host"])
        altered = json.loads(packet)
        altered["payload"]["host"] = "evil.example.invalid"
        with self.assertRaisesRegex(ValueError, "signature"):
            ob.verify_beacon(json.dumps(altered).encode(), self.settings)
        with mock.patch.object(ob.time, "time", return_value=time.time() + 120), self.assertRaisesRegex(ValueError, "stale"):
            ob.verify_beacon(packet, self.settings)

    def test_accept_verifies_api_before_publishing_and_keeps_secret_out_of_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            ob.private_write(directory / "enrollment.json", json.dumps(self.payload()))
            output = directory / "config.toml"
            response = io.BytesIO(b'{"data":{"uptime":123}}')
            with mock.patch.object(ob, "read_bundle", return_value=self.settings), mock.patch.object(ob.request.OpenerDirector, "open", return_value=response) as call:
                result = ob.accept(directory, output)
            self.assertTrue(result["api_verified"])
            call.assert_called_once()
            configured = config.load(output)
            self.assertEqual(configured.proxmox.guest_mode, "lxc-only")
            self.assertTrue(configured.proxmox.verify_tls)
            self.assertEqual(configured.power.mode, "none")
            self.assertNotIn(self.payload()["token_secret"], output.read_text())
            self.assertEqual(tomllib.loads(output.with_suffix(".secrets.toml").read_text())["proxmox-token"], self.payload()["token_secret"])
            with mock.patch.object(ob, "read_bundle", return_value=self.settings), mock.patch.object(ob.request.OpenerDirector, "open") as call, self.assertRaisesRegex(ValueError, "already exist"):
                ob.accept(directory, output)
            call.assert_not_called()

    def test_authenticated_receipt_can_be_accepted_after_pairing_expiry(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            directory.chmod(0o700)
            ob.private_write(directory / "pairing.json", json.dumps({**self.settings, "expires_at": 0}))
            ob.private_write(directory / "enrollment.json", json.dumps(self.payload()))
            with self.assertRaisesRegex(ValueError, "expired"):
                ob.read_bundle(directory)
            with mock.patch.object(ob.request.OpenerDirector, "open", return_value=io.BytesIO(b'{"data":{"uptime":1}}')):
                self.assertTrue(ob.accept(directory, directory / "config.toml")["api_verified"])

    def test_missing_builder_fails_before_opening_iso(self):
        args = argparse.Namespace(bundle=str(self.directory), source="/missing/source.iso", sha256="0" * 64)
        with mock.patch.object(ob, "read_bundle", return_value={**self.settings, "mode": "iso"}), mock.patch.object(ob.shutil, "which", return_value=None), mock.patch.object(Path, "open") as opened:
            with self.assertRaisesRegex(ValueError, "proxmox-auto-install-assistant"):
                ob.build_iso(args)
            opened.assert_not_called()

    def test_api_verification_failure_leaves_config_unpublished(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            ob.private_write(directory / "enrollment.json", json.dumps(self.payload()))
            with mock.patch.object(ob, "read_bundle", return_value=self.settings), mock.patch.object(ob.request.OpenerDirector, "open", side_effect=OSError("unreachable")), self.assertRaisesRegex(ValueError, "verification failed"):
                ob.accept(directory, directory / "config.toml")
            self.assertEqual([p.name for p in directory.iterdir()], ["enrollment.json"])

    def test_iso_checksum_failure_precedes_builder(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.iso"
            source.write_bytes(b"fixture")
            args = argparse.Namespace(bundle=str(self.directory), source=str(source), sha256="0" * 64)
            with mock.patch.object(ob, "read_bundle", return_value={**self.settings, "mode": "iso"}), mock.patch.object(ob.shutil, "which", return_value="/bin/assistant"), mock.patch.object(ob.subprocess, "run") as run, self.assertRaisesRegex(ValueError, "checksum mismatch"):
                ob.build_iso(args)
            run.assert_not_called()

    def test_iso_builder_validates_and_embeds_first_boot_without_secret_argv(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            source = directory / "source.iso"
            source.write_bytes(b"fixture")
            args = argparse.Namespace(bundle=str(directory), source=str(source), sha256=hashlib.sha256(b"fixture").hexdigest())
            calls = []
            def build(argv, **kwargs):
                calls.append(argv)
                if "prepare-iso" in argv:
                    (directory / "proxmox-agent-lab.iso").write_bytes(b"prepared")
            with mock.patch.object(ob, "read_bundle", return_value={**self.settings, "mode": "iso"}), mock.patch.object(ob.shutil, "which", return_value="/bin/assistant"), mock.patch.object(ob.subprocess, "run", side_effect=build):
                result = ob.build_iso(args)
            self.assertEqual(calls[0][1], "validate-answer")
            self.assertIn("--on-first-boot", calls[1])
            self.assertNotIn(self.settings["token"], repr(calls))
            self.assertEqual(result["sha256"], hashlib.sha256(b"prepared").hexdigest())


class VPSPolicyTests(unittest.TestCase):
    def setUp(self):
        self.config = config.defaults()
        self.config.proxmox._values["guest_mode"] = "lxc-only"

    def test_qemu_and_host_power_are_refused_before_token_or_network(self):
        with mock.patch.object(cli, "CONFIG", self.config), mock.patch.object(cli, "keychain_secret") as token, mock.patch.object(cli.request, "urlopen") as network:
            api = cli.ProxmoxAPI()
            for path in ("/nodes/test/qemu", "/nodes/test/%71emu/7/status/start", "/nodes/test/status"):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    api.call("POST", path)
            token.assert_not_called()
            network.assert_not_called()

    def test_lxc_create_requires_unprivileged(self):
        for data in ({}, {"unprivileged": 0}, {"unprivileged": "false"}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                host_policy.check_api(self.config, "POST", "/nodes/test/lxc", data)
        host_policy.check_api(self.config, "POST", "/nodes/test/lxc", {"unprivileged": 1})
        host_policy.check_api(self.config, "GET", "/nodes/test/lxc")

    def test_shutdown_policy_does_not_touch_host(self):
        api = mock.Mock()
        with mock.patch.object(cli, "CONFIG", self.config), mock.patch.object(cli, "audit"):
            self.assertFalse(cli.shutdown_host(api))
        self.assertEqual(api.mock_calls, [])

    def test_default_host_policy_is_unchanged_and_unknown_mode_fails_closed(self):
        host_policy.check_api(config.defaults(), "POST", "/nodes/test/qemu")
        self.config.proxmox._values["guest_mode"] = "typo"
        with self.assertRaises(ValueError):
            host_policy.check_api(self.config, "POST", "/nodes/test/lxc", {"unprivileged": 1})

    def test_vps_authorization_guard_precedes_files_and_commands(self):
        settings = {"mode": "vps", "id": "fixture", "expires_at": time.time() + 60}
        with mock.patch.object(host.os, "geteuid", return_value=0), mock.patch.object(sys, "argv", ["host-setup.py"]), mock.patch.object(host, "run") as run, mock.patch.object(Path, "mkdir") as mkdir, contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            host.entry(settings)
        run.assert_not_called()
        mkdir.assert_not_called()

    def test_wrong_kernel_blocks_proxmox_install(self):
        with mock.patch.object(host.os, "uname", return_value=argparse.Namespace(release="6.1-generic")), mock.patch.object(host, "run") as run, self.assertRaisesRegex(RuntimeError, "kernel"):
            host.install_proxmox(Path("/unused"))
        run.assert_not_called()


class HostBootstrapTests(unittest.TestCase):
    def test_container_vps_is_rejected_before_package_installation(self):
        with mock.patch.object(host.os, "geteuid", return_value=0), \
             mock.patch.object(Path, "read_text", return_value='ID=debian\nVERSION_ID="13"\n'), \
             mock.patch.object(host, "run", return_value="amd64\n") as run, \
             mock.patch.object(host.subprocess, "run", return_value=argparse.Namespace(returncode=0)), \
             self.assertRaisesRegex(RuntimeError, "full VPS"):
            host.vps_preflight()
        self.assertEqual(run.call_args_list, [mock.call("dpkg", "--print-architecture")])

    def test_kernel_stage_records_progress_before_reboot(self):
        settings = {"fqdn": "pve.example.invalid"}
        state = Path("/example-state")
        events = []
        def run(*argv, **kwargs):
            events.append(("command", argv))
            return ""
        def write(path, text, mode=0o600):
            events.append(("write", str(path), text))
        with mock.patch.object(host, "vps_preflight"), \
             mock.patch.object(host.shutil, "which", return_value=None), \
             mock.patch.object(host, "management_ip", return_value="192.0.2.10"), \
             mock.patch.object(Path, "read_text", return_value="127.0.0.1 localhost\n127.0.1.1 pve old-alias\n"), \
             mock.patch.object(Path, "write_bytes"), mock.patch.object(Path, "chmod"), \
             mock.patch.object(host.request, "urlopen", return_value=io.BytesIO(b"vendor-key")), \
             mock.patch.object(host, "run", side_effect=run), \
             mock.patch.object(host, "write", side_effect=write):
            host.install_kernel(settings, state)
        self.assertEqual(events[-2], ("write", "/example-state/stage", "kernel-installed"))
        self.assertEqual(events[-1], ("command", ("systemctl", "reboot")))
        self.assertIn(("command", ("apt-get", "install", "-y", "proxmox-default-kernel")), events)
        hosts = next(e[2] for e in events if e[:2] == ("write", "/etc/hosts"))
        self.assertIn("127.0.0.1 localhost", hosts)
        self.assertIn("127.0.1.1 old-alias", hosts)
        self.assertIn("192.0.2.10 pve.example.invalid pve", hosts)

    def test_proxmox_stage_checks_user_namespaces_before_packages(self):
        events = []
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(host.os, "uname", return_value=argparse.Namespace(release="6.14.0-pve")), \
             mock.patch.object(host, "vps_preflight"), \
             mock.patch.object(host, "run", side_effect=lambda *argv, **kw: events.append(argv)):
            host.install_proxmox(Path(tmp))
            self.assertEqual((Path(tmp) / "stage").read_text(), "proxmox-installed")
        self.assertEqual(events[0], ("unshare", "--user", "--map-root-user", "true"))
        self.assertEqual(events[1][:4], ("apt-get", "install", "-y", "proxmox-ve"))

    def test_service_setup_is_nonblocking_and_resumable(self):
        settings = {"mode": "vps", "id": "fixture", "expires_at": time.time() + 60}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = root / "onboard.service"
            with mock.patch.object(host, "ROOT", root / "state"), mock.patch.object(host, "SERVICE", service), \
                 mock.patch.object(host.os, "geteuid", return_value=0), mock.patch.object(host, "vps_preflight"), \
                 mock.patch.object(sys, "argv", ["host-setup.py", "--host-change-authorized", "--reboot-authorized"]), \
                 mock.patch.object(host, "run") as run, contextlib.redirect_stdout(io.StringIO()):
                host.entry(settings)
            self.assertIn("--resume", service.read_text())
            self.assertIn("TimeoutStartSec=7200", service.read_text())
            run.assert_any_call("systemctl", "start", "--no-block", service.name)
            self.assertEqual((root / "state/fixture/host-setup.py").stat().st_mode & 0o077, 0)

    def test_delivered_callback_cleans_private_host_state(self):
        settings = {"mode": "iso", "id": "fixture", "expires_at": time.time() + 60}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "fixture"
            state.mkdir()
            for name in ("host-setup.py", "api-token.json", "enrollment.json"):
                (state / name).write_text("{}")
            with mock.patch.object(host, "ROOT", root), mock.patch.object(host.os, "geteuid", return_value=0), \
                 mock.patch.object(sys, "argv", ["host-setup.py", "--resume"]), mock.patch.object(host, "callback") as callback, \
                 mock.patch.object(host, "run"), mock.patch.dict(os.environ), contextlib.redirect_stdout(io.StringIO()):
                host.entry(settings)
            callback.assert_called_once_with(settings, {})
            self.assertEqual([p.name for p in state.iterdir()], ["complete"])

    def test_vps_provisioning_grants_audit_but_never_power(self):
        settings = {"mode": "vps", "id": "fixture", "fqdn": "pve.example.invalid", "api_host": "pve.example.invalid"}
        commands = []
        def run(*argv, **kwargs):
            commands.append(argv)
            if argv == ("hostname", "-s"):
                return "pve\n"
            if argv[:3] == ("pveum", "user", "list"):
                return "[]"
            if argv[:4] == ("pveum", "user", "token", "add"):
                return '{"value":"fixture-token"}'
            return ""
        original_read = Path.read_text
        def read(path, *args, **kwargs):
            if str(path) == "/etc/pve/pve-root-ca.pem":
                return "fixture-ca"
            return original_read(path, *args, **kwargs)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(host, "configure_repositories"), mock.patch.object(host, "configure_bridge"), \
             mock.patch.object(host, "configure_wifi"), mock.patch.object(host.shutil, "which", return_value="/bin/pveum"), \
             mock.patch.object(host, "run", side_effect=run), mock.patch.object(Path, "read_text", read):
            payload = host.provision(settings, Path(tmp))
        self.assertEqual(payload["wol_mac"], "")
        self.assertEqual(payload["token_secret"], "fixture-token")
        self.assertTrue(any("PVEAuditor" in cmd for cmd in commands))
        self.assertFalse(any("Sys.PowerMgmt" in cmd or "PXLOnboardingPower" in cmd for cmd in commands))
