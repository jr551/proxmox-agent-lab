"""The share server is the only component that faces the public internet,
so its access control and framing get direct tests."""

from __future__ import annotations

import os
from pathlib import Path

os.environ["PROXMOX_AGENT_LAB_CONFIG"] = str(
    Path(__file__).parent / "fixtures" / "config.toml"
)

import base64  # noqa: E402
import json  # noqa: E402
import struct  # noqa: E402
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

    def test_revoke_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = fresh_server_module(tmp)
            for _ in range(3):
                server.SESSIONS.add(vmid=1, minutes=5)
            self.assertEqual(server.SESSIONS.revoke_all(), 3)
            self.assertEqual(server.SESSIONS.listing(), [])


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
