"""The share server is the only component that faces the public internet,
so its access control and framing get direct tests."""

from __future__ import annotations

import os
from pathlib import Path

import sys  # noqa: E402

# Shared bootstrap: fixture configuration plus a per-process state directory,
# applied before any proxmox_agent_lab import. `support` sits beside this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import bootstrap  # noqa: E402,F401
import shutil
import tempfile
import base64  # noqa: E402
import concurrent.futures  # noqa: E402
import json  # noqa: E402
import struct  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import unittest  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))


from proxmox_agent_lab import share_server  # noqa: E402


def fresh_server_module(tmp: str):
    """Point the server's paths at a temp directory, with a clean store.

    Re-importing does not work: deleting the entry from sys.modules leaves the
    parent package's attribute pointing at the old module, so its paths --
    read once at import -- keep referring to a temp directory that has since
    been removed, and state silently leaks between tests.
    """
    share_server.STATE_PATH = Path(tmp) / "sessions.json"
    share_server.CONFIG_PATH = Path(tmp) / "config.json"
    share_server.NOVNC_ROOT = Path(tmp) / "novnc"
    share_server.SESSIONS = share_server.Sessions()
    return share_server


class SessionTests(unittest.TestCase):
    def test_a_link_expires(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = fresh_server_module(tmp)
            entry = server.SESSIONS.add(vmid=100, minutes=1)
            self.assertIsNotNone(server.SESSIONS.get(entry["token"]))
            # Reach in and age it rather than sleeping a minute.
            server.SESSIONS._sessions[entry["token"]]["expires_at"] = 0
            self.assertIsNone(server.SESSIONS.get(entry["token"]))

    def test_single_use_links_die_after_one_connection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = fresh_server_module(tmp)
            entry = server.SESSIONS.add(vmid=100, minutes=10, once=True)
            self.assertIsNotNone(server.SESSIONS.get(entry["token"]))
            server.SESSIONS.mark_used(entry["token"])
            self.assertIsNone(server.SESSIONS.get(entry["token"]))

    def test_a_link_is_bound_to_one_vmid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = fresh_server_module(tmp)
            entry = server.SESSIONS.add(vmid=4242, minutes=10)
            self.assertEqual(server.SESSIONS.get(entry["token"])["vmid"], 4242)

    def test_tokens_are_long_and_unguessable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = fresh_server_module(tmp)
            tokens = {server.SESSIONS.add(vmid=1, minutes=5)["token"]
                      for _ in range(20)}
            self.assertEqual(len(tokens), 20)
            self.assertTrue(all(len(t) >= 30 for t in tokens))

    def test_the_store_is_shared_between_processes(self) -> None:
        """`add` runs as a separate process from `serve`, so the running
        server must notice writes it did not make itself."""
        with tempfile.TemporaryDirectory() as tmp:
            server = fresh_server_module(tmp)
            entry = server.SESSIONS.add(vmid=7, minutes=10)
            other = server.Sessions()          # a second process, in effect
            self.assertIsNotNone(other.get(entry["token"]))
            other.revoke(entry["token"])
            self.assertIsNone(server.SESSIONS.get(entry["token"]),
                              "a revoke from elsewhere must take effect")

    def test_concurrent_process_writes_are_serialised(self) -> None:
        """Several short-lived `add` processes racing one another must not
        corrupt the store or lose links."""
        with tempfile.TemporaryDirectory() as tmp:
            server = fresh_server_module(tmp)
            env = os.environ.copy()
            env["PXL_SHARE_STATE"] = str(server.STATE_PATH)
            env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
            code = (
                "from proxmox_agent_lab import share_server\n"
                "print(share_server.SESSIONS.add(vmid=7, minutes=5)['token'])"
            )
            n = 8
            with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
                futures = [
                    pool.submit(
                        subprocess.run,
                        [sys.executable, "-c", code],
                        env=env,
                        capture_output=True,
                        text=True,
                    )
                    for _ in range(n)
                ]
                results = [f.result() for f in concurrent.futures.as_completed(futures)]
            for result in results:
                self.assertEqual(result.returncode, 0, result.stderr)
            tokens = {r.stdout.strip() for r in results}
            self.assertEqual(len(tokens), n, "each process must add one unique link")
            for token in tokens:
                self.assertIsNotNone(server.SESSIONS.get(token))
            self.assertEqual(len(server.SESSIONS.listing()), n)

    def test_revoke_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = fresh_server_module(tmp)
            for _ in range(3):
                server.SESSIONS.add(vmid=1, minutes=5)
            self.assertEqual(server.SESSIONS.revoke_all(), 3)
            self.assertEqual(server.SESSIONS.listing(), [])

    def test_threads_add_and_revoke_concurrently(self) -> None:
        """In-process callers are serialised by the threading.Lock half of
        _ProcessThreadLock; the final state must be internally consistent."""
        with tempfile.TemporaryDirectory() as tmp:
            server = fresh_server_module(tmp)
            tokens: set[str] = set()

            def add_one(i: int) -> str:
                return server.SESSIONS.add(vmid=i, minutes=5)["token"]

            with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
                for token in pool.map(add_one, range(32)):
                    tokens.add(token)
            self.assertEqual(len(tokens), 32)
            self.assertEqual(len(server.SESSIONS.listing()), 32)

            def revoke(token: str) -> None:
                server.SESSIONS.revoke(token)

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                pool.map(revoke, tokens)
            self.assertEqual(server.SESSIONS.listing(), [])

    def test_corrupt_store_is_tolerated(self) -> None:
        """A damaged sessions.json on disk must not crash the server."""
        with tempfile.TemporaryDirectory() as tmp:
            server = fresh_server_module(tmp)
            server.STATE_PATH.write_text("not valid json")
            server.SESSIONS = server.Sessions()
            self.assertEqual(server.SESSIONS.listing(), [])
            entry = server.SESSIONS.add(vmid=9, minutes=5)
            self.assertIsNotNone(server.SESSIONS.get(entry["token"]))
            self.assertEqual(len(server.SESSIONS.listing()), 1)

    def test_lock_file_and_store_have_restrictive_permissions(self) -> None:
        """The lock file and sessions store are created with owner-only access."""
        with tempfile.TemporaryDirectory() as tmp:
            server = fresh_server_module(tmp)
            server.SESSIONS.add(vmid=1, minutes=5)
            self.assertTrue(server.STATE_PATH.with_suffix(".lock").exists())
            self.assertEqual(server.STATE_PATH.stat().st_mode & 0o777, 0o600)

    def test_module_import_does_not_create_state_directory(self) -> None:
        """Importing share_server must not eagerly create /var/lib/pxl-share."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "missing" / "path"
            state_path = state_dir / "sessions.json"
            env = os.environ.copy()
            env["PXL_SHARE_STATE"] = str(state_path)
            env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
            code = "from proxmox_agent_lab import share_server"
            result = subprocess.run(
                [sys.executable, "-c", code],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(state_dir.exists())

    def test_subprocess_cli_add_list_revoke(self) -> None:
        """The module can be run as `python -m proxmox_agent_lab.share_server`."""
        with tempfile.TemporaryDirectory() as tmp:
            server = fresh_server_module(tmp)
            env = os.environ.copy()
            env["PXL_SHARE_STATE"] = str(server.STATE_PATH)
            env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
            cmd = [sys.executable, "-m", "proxmox_agent_lab.share_server"]

            add = subprocess.run(
                [*cmd, "add", "--vmid", "42", "--minutes", "10", "--label", "cli"],
                env=env, capture_output=True, text=True,
            )
            self.assertEqual(add.returncode, 0, add.stderr)
            entry = json.loads(add.stdout)
            self.assertEqual(entry["vmid"], 42)
            self.assertEqual(entry["label"], "cli")

            listing = subprocess.run([*cmd, "list"], env=env, capture_output=True, text=True)
            self.assertEqual(listing.returncode, 0, listing.stderr)
            self.assertEqual(len(json.loads(listing.stdout)), 1)

            revoke = subprocess.run(
                [*cmd, "revoke", "--token", entry["token"]],
                env=env, capture_output=True, text=True,
            )
            self.assertEqual(revoke.returncode, 0, revoke.stderr)
            self.assertEqual(json.loads(revoke.stdout)["revoked"], 1)

            self.assertIsNone(server.SESSIONS.get(entry["token"]))

    def test_concurrent_add_and_revoke_all_processes(self) -> None:
        """A `revoke --all` racing several `add` processes must not corrupt
        the JSON store."""
        with tempfile.TemporaryDirectory() as tmp:
            server = fresh_server_module(tmp)
            env = os.environ.copy()
            env["PXL_SHARE_STATE"] = str(server.STATE_PATH)
            env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
            cmd = [sys.executable, "-m", "proxmox_agent_lab.share_server"]
            add_code = (
                "from proxmox_agent_lab import share_server\n"
                "print(share_server.SESSIONS.add(vmid=7, minutes=5)['token'])"
            )

            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                futures = []
                for _ in range(5):
                    futures.append(pool.submit(
                        subprocess.run,
                        [sys.executable, "-c", add_code],
                        env=env, capture_output=True, text=True,
                    ))
                futures.append(pool.submit(
                    subprocess.run, [*cmd, "revoke", "--all"],
                    env=env, capture_output=True, text=True,
                ))
                results = [f.result() for f in concurrent.futures.as_completed(futures)]

            for result in results:
                self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(server.STATE_PATH.read_text())
            self.assertIsInstance(data, dict)
            final = server.SESSIONS.listing()
            self.assertGreaterEqual(len(final), 0)
            self.assertLessEqual(len(final), 5)


class FramingTests(unittest.TestCase):
    def test_server_frames_are_unmasked_and_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = fresh_server_module(tmp)
            for size in (5, 200, 70000):
                payload = bytes(range(256)) * (size // 256 + 1)
                payload = payload[:size]
                frame = server.ws_frame(payload, 0x2, mask=False)
                self.assertEqual(frame[1] & 0x80, 0, "server must not mask")

                class Fake:
                    def __init__(self, data): self.data = data
                    def recv(self, _n):
                        out, self.data = self.data, b""
                        return out
                opcode, decoded = server.FrameReader(Fake(frame)).read()
                self.assertEqual(opcode, 0x2)
                self.assertEqual(decoded, payload)

    def test_client_frames_are_masked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = fresh_server_module(tmp)
            frame = server.ws_frame(b"hello", 0x2, mask=True)
            self.assertEqual(frame[1] & 0x80, 0x80, "client must mask")

            class Fake:
                def __init__(self, data): self.data = data
                def recv(self, _n):
                    out, self.data = self.data, b""
                    return out
            self.assertEqual(server.FrameReader(Fake(frame)).read()[1], b"hello")


class AccessControlTests(unittest.TestCase):
    """A live server on localhost: the only credential is the token."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.server_module = fresh_server_module(self.tmp.name)
        novnc = Path(self.tmp.name) / "novnc" / "core"
        novnc.mkdir(parents=True)
        (novnc / "rfb.js").write_text("// stub")
        (Path(self.tmp.name) / "secret.txt").write_text("do not serve me")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                         self.server_module.Handler)
        self.port = self.httpd.server_address[1]
        self.server_thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True
        )
        self.server_thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.server_thread.join(timeout=5)
        self.tmp.cleanup()

    def get(self, path: str):
        return urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}{path}", timeout=5)

    def test_health_needs_no_token(self) -> None:
        with self.get("/healthz") as response:
            self.assertEqual(json.load(response)["ok"], True)

    def test_no_token_is_refused(self) -> None:
        for path in ("/", "/v/", "/v/wrong-token/"):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.get(path)
            with caught.exception:
                self.assertEqual(caught.exception.code, 404, path)

    def test_a_valid_token_serves_the_viewer(self) -> None:
        entry = self.server_module.SESSIONS.add(vmid=101, minutes=5,
                                                label="build box")
        with self.get(f"/v/{entry['token']}/") as response:
            body = response.read().decode()
        self.assertIn("build box", body)
        self.assertIn("rfb.js", body)

    def test_an_expired_token_is_refused(self) -> None:
        entry = self.server_module.SESSIONS.add(vmid=101, minutes=5)
        self.server_module.SESSIONS._sessions[entry["token"]]["expires_at"] = 0
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get(f"/v/{entry['token']}/")
        with caught.exception:
            self.assertEqual(caught.exception.code, 404)

    def test_static_assets_cannot_escape_the_novnc_directory(self) -> None:
        """Path traversal would turn a console link into file disclosure."""
        entry = self.server_module.SESSIONS.add(vmid=101, minutes=5)
        for attack in ("../secret.txt", "..%2fsecret.txt",
                       "core/../../secret.txt"):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.get(f"/v/{entry['token']}/{attack}")
            with caught.exception:
                self.assertEqual(caught.exception.code, 404, attack)

    def test_a_valid_asset_is_served(self) -> None:
        entry = self.server_module.SESSIONS.add(vmid=101, minutes=5)
        with self.get(f"/v/{entry['token']}/core/rfb.js") as response:
            body = response.read()
        self.assertIn(b"stub", body)

    def test_a_plain_get_on_the_ws_path_is_not_upgraded(self) -> None:
        entry = self.server_module.SESSIONS.add(vmid=101, minutes=5)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get(f"/v/{entry['token']}/ws")
        with caught.exception:
            self.assertEqual(caught.exception.code, 404)

    def _subprocess_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["PXL_SHARE_STATE"] = str(self.server_module.STATE_PATH)
        env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
        return env

    def test_subprocess_cli_add_is_visible_to_running_server(self) -> None:
        """The long-running HTTP server must notice a link minted by a
        separate `python -m proxmox_agent_lab.share_server add` process."""
        env = self._subprocess_env()
        cmd = [sys.executable, "-m", "proxmox_agent_lab.share_server", "add",
               "--vmid", "101", "--minutes", "5"]
        result = subprocess.run(cmd, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        entry = json.loads(result.stdout)
        with self.get(f"/v/{entry['token']}/") as response:
            self.assertEqual(response.status, 200)

    def test_subprocess_cli_revoke_is_visible_to_running_server(self) -> None:
        """A `revoke` from the CLI must take effect immediately in the HTTP
        server."""
        entry = self.server_module.SESSIONS.add(vmid=101, minutes=5)
        with self.get(f"/v/{entry['token']}/") as response:
            self.assertEqual(response.status, 200)

        env = self._subprocess_env()
        cmd = [sys.executable, "-m", "proxmox_agent_lab.share_server",
               "revoke", "--token", entry["token"]]
        result = subprocess.run(cmd, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get(f"/v/{entry['token']}/")
        with caught.exception:
            self.assertEqual(caught.exception.code, 404)


class SetupScriptTests(unittest.TestCase):
    def test_placeholders_substituted(self) -> None:
        from proxmox_agent_lab import share
        script = share.setup_script()
        for placeholder in ("__NOVNC__", "__PORT__", "__REGION__"):
            self.assertNotIn(placeholder, script)
        self.assertIn("novnc", script.lower())
        self.assertIn("ngrok", script)

    def test_disabled_by_default(self) -> None:
        from proxmox_agent_lab import config as config_module
        self.assertFalse(config_module.defaults().share.get("enabled"))


if __name__ == "__main__":
    unittest.main()
