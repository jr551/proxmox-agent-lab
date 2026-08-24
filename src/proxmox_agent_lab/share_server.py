#!/usr/bin/env python3
"""The console-sharing server. Runs *on the share worker*, not the controller.

Serves noVNC over a disposable, pre-authenticated URL and relays it to a
guest's console.

Why a relay and not just a link to Proxmox
------------------------------------------
A Proxmox VNC ticket lasts seconds and is consumed by the first connection,
so a share link cannot carry one: by the time anyone clicks, it is dead. This
server therefore mints a fresh ticket at the moment of connection, which means
it -- and only it -- holds an API token. The person you send the link to needs
no Proxmox account, and gets nothing but that one console.

What a link grants
------------------
One VMID, until its expiry, and nothing else. The token is the only
credential; it is long, random, and never logged. Sessions are held in memory,
so a restart revokes every link.

Standard library only, like the rest of the project.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib import error, parse, request

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
STATE_PATH = Path(os.environ.get("PXL_SHARE_STATE", "/var/lib/pxl-share/sessions.json"))
CONFIG_PATH = Path(os.environ.get("PXL_SHARE_CONFIG", "/etc/pxl-share/config.json"))
NOVNC_ROOT = Path(os.environ.get("PXL_SHARE_NOVNC", "/opt/novnc"))


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text())


class Sessions:
    """Live share links. In memory, so a restart revokes everything."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, dict[str, Any]] = {}
        self._mtime = 0.0
        self._load()

    def _load(self) -> None:
        """Re-read the store if it changed underneath us.

        `add` and `revoke` run as separate short-lived processes, so the
        long-running server must notice their writes. Cheap: one stat per
        lookup, and a reload only when the file actually moved.
        """
        try:
            mtime = STATE_PATH.stat().st_mtime
        except OSError:
            return
        if mtime == self._mtime:
            return
        try:
            self._sessions = json.loads(STATE_PATH.read_text())
            self._mtime = mtime
        except (OSError, ValueError):
            pass

    def _persist(self) -> None:
        """Write the whole store atomically.

        `add`/`revoke` run as separate processes racing this server's writes;
        a plain write_text can interleave or leave a truncated file behind.
        Write to a unique temp file, fsync, then os.replace (atomic on POSIX)
        so a reader always sees one complete store (audit 2026-08-24).
        """
        tmp = None
        try:
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = STATE_PATH.with_name(
                f".{STATE_PATH.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
            with open(tmp, "w") as fh:
                fh.write(json.dumps(self._sessions))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, STATE_PATH)
            tmp = None
            STATE_PATH.chmod(0o600)
            self._mtime = STATE_PATH.stat().st_mtime
        except OSError:
            pass
        finally:
            if tmp is not None:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass

    def add(self, vmid: int, minutes: int, kind: str = "qemu",
            label: str = "", once: bool = False) -> dict[str, Any]:
        token = secrets.token_urlsafe(24)
        entry = {
            "vmid": int(vmid),
            "kind": kind,
            "label": label[:80],
            "expires_at": time.time() + minutes * 60,
            "once": bool(once),
            "used": 0,
            "created_at": time.time(),
        }
        with self._lock:
            self._load()
            self._sessions[token] = entry
            self._persist()
        return {"token": token, **entry}

    def get(self, token: str) -> dict[str, Any] | None:
        with self._lock:
            self._load()
            entry = self._sessions.get(token)
            if entry is None:
                return None
            if time.time() > entry["expires_at"]:
                self._sessions.pop(token, None)
                self._persist()
                return None
            if entry["once"] and entry["used"] >= 1:
                return None
            return dict(entry)

    def mark_used(self, token: str) -> None:
        with self._lock:
            self._load()
            if token in self._sessions:
                self._sessions[token]["used"] += 1
                self._persist()

    def revoke(self, token: str) -> bool:
        with self._lock:
            self._load()
            removed = self._sessions.pop(token, None) is not None
            if removed:
                self._persist()
        return removed

    def revoke_all(self) -> int:
        with self._lock:
            self._load()
            count = len(self._sessions)
            self._sessions.clear()
            self._persist()
        return count

    def listing(self) -> list[dict[str, Any]]:
        now = time.time()
        with self._lock:
            self._load()
            return [
                {
                    "token": token[:8] + "...",
                    "vmid": entry["vmid"],
                    "label": entry["label"],
                    "expires_in_seconds": int(entry["expires_at"] - now),
                    "used": entry["used"],
                }
                for token, entry in self._sessions.items()
                if entry["expires_at"] > now
            ]


SESSIONS = Sessions()


# --- talking to Proxmox ---------------------------------------------------


def _ssl_context(verify: bool) -> ssl.SSLContext:
    context = ssl.create_default_context()
    if not verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def mint_vnc_ticket(config: dict[str, Any], kind: str,
                    vmid: int) -> dict[str, Any]:
    """Ask Proxmox for a fresh, short-lived console ticket."""
    url = (f"https://{config['host']}:{config['port']}/api2/json/nodes/"
           f"{config['node']}/{kind}/{vmid}/vncproxy")
    req = request.Request(
        url, data=parse.urlencode({"websocket": 1}).encode(), method="POST",
        headers={
            "Authorization": (
                f"PVEAPIToken={config['token_user']}!{config['token_name']}"
                f"={config['token_secret']}"
            ),
            "Accept": "application/json",
        },
    )
    with request.urlopen(req, context=_ssl_context(config.get("verify_tls")),
                         timeout=20) as response:
        return json.load(response)["data"]


class UpstreamWebSocket:
    """Client side of the Proxmox console WebSocket."""

    def __init__(self, config: dict[str, Any], kind: str, vmid: int,
                 proxy: dict[str, Any]) -> None:
        raw = socket.create_connection(
            (config["host"], int(config["port"])), timeout=20
        )
        self.socket = _ssl_context(config.get("verify_tls")).wrap_socket(
            raw, server_hostname=None
        )
        key = base64.b64encode(os.urandom(16)).decode()
        target = (
            f"/api2/json/nodes/{config['node']}/{kind}/{vmid}/vncwebsocket?"
            + parse.urlencode({"port": proxy["port"],
                               "vncticket": proxy["ticket"]})
        )
        lines = [
            f"GET {target} HTTP/1.1",
            f"Host: {config['host']}:{config['port']}",
            "Connection: Upgrade",
            "Upgrade: websocket",
            "Sec-WebSocket-Version: 13",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Protocol: binary",
            (f"Authorization: PVEAPIToken={config['token_user']}"
             f"!{config['token_name']}={config['token_secret']}"),
        ]
        self.socket.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            chunk = self.socket.recv(4096)
            if not chunk:
                raise RuntimeError("Proxmox closed the console connection")
            buffer += chunk
        head, _, rest = buffer.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise RuntimeError(f"Proxmox refused the console upgrade: {head[:120]!r}")
        self.leftover = rest


# --- minimal WebSocket framing -------------------------------------------


def ws_frame(payload: bytes, opcode: int = 0x2, mask: bool = False) -> bytes:
    header = bytearray([0x80 | opcode])
    length = len(payload)
    flag = 0x80 if mask else 0
    if length < 126:
        header.append(flag | length)
    elif length < 65536:
        header.append(flag | 126)
        header += struct.pack(">H", length)
    else:
        header.append(flag | 127)
        header += struct.pack(">Q", length)
    if not mask:
        return bytes(header) + payload
    key = os.urandom(4)
    masked = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return bytes(header) + key + masked


class FrameReader:
    """Incremental WebSocket frame parser for a blocking socket."""

    def __init__(self, sock: Any, initial: bytes = b"") -> None:
        self.socket = sock
        self.buffer = bytearray(initial)

    def _need(self, count: int) -> bytes:
        while len(self.buffer) < count:
            chunk = self.socket.recv(65536)
            if not chunk:
                raise ConnectionError("closed")
            self.buffer += chunk
        out = bytes(self.buffer[:count])
        del self.buffer[:count]
        return out

    def read(self) -> tuple[int, bytes]:
        first, second = self._need(2)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._need(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._need(8))[0]
        key = self._need(4) if masked else b""
        payload = self._need(length)
        if masked:
            payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
        return opcode, payload


def relay(browser: Any, browser_initial: bytes, upstream: UpstreamWebSocket,
          deadline: float) -> None:
    """Pump RFB payloads between the browser and Proxmox until either ends.

    Both sides speak WebSocket, but a client must mask its frames and a
    server must not, so the payloads are re-framed rather than piped.
    """
    stop = threading.Event()

    def pump(reader: FrameReader, sink: Any, mask: bool) -> None:
        try:
            while not stop.is_set():
                if time.time() > deadline:
                    break
                opcode, payload = reader.read()
                if opcode == 0x8:
                    break
                if opcode in (0x1, 0x2):
                    sink.sendall(ws_frame(payload, 0x2, mask=mask))
        except (OSError, ConnectionError, struct.error):
            pass
        finally:
            stop.set()

    threads = [
        threading.Thread(
            target=pump,
            args=(FrameReader(browser, browser_initial), upstream.socket, True),
            daemon=True,
        ),
        threading.Thread(
            target=pump,
            args=(FrameReader(upstream.socket, upstream.leftover), browser,
                  False),
            daemon=True,
        ),
    ]
    for thread in threads:
        thread.start()
    while not stop.is_set() and time.time() < deadline:
        time.sleep(0.5)
    stop.set()
    for sock in (browser, upstream.socket):
        try:
            sock.close()
        except OSError:
            pass


# --- HTTP -----------------------------------------------------------------

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>{label}</title>
<style>html,body{{margin:0;height:100%;background:#111;color:#ddd;
font:14px system-ui,sans-serif}} #status{{padding:8px 12px}}</style></head>
<body><div id="status">connecting to {label}…</div>
<div id="screen" style="width:100%;height:100%"></div>
<script type="module">
import RFB from './core/rfb.js';
const status = document.getElementById('status');
try {{
  const rfb = new RFB(document.getElementById('screen'),
      (location.protocol === 'https:' ? 'wss://' : 'ws://') +
      location.host + location.pathname.replace(/\\/$/, '') + '/ws');
  rfb.viewOnly = {view_only};
  rfb.scaleViewport = true;
  rfb.addEventListener('connect', () => status.textContent = '{label}');
  rfb.addEventListener('disconnect', e =>
      status.textContent = 'disconnected' + (e.detail.clean ? '' : ' unexpectedly'));
}} catch (e) {{ status.textContent = 'failed: ' + e; }}
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "pxl-share"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        # Never log the path: it contains the token, which is the credential.
        sys.stderr.write("%s - %s\n" % (self.address_string(), args[1] if len(args) > 1 else ""))

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _deny(self, message: str = "This link is not valid or has expired.") -> None:
        self._send(404, f"<html><body style='font:14px system-ui;padding:2em'>"
                        f"<h3>{message}</h3></body></html>".encode(),
                   "text/html; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        path = parse.urlparse(self.path).path
        parts = [p for p in path.split("/") if p]

        if path == "/healthz":
            self._send(200, b'{"ok":true}', "application/json")
            return
        if not parts or parts[0] != "v" or len(parts) < 2:
            self._deny()
            return

        token = parts[1]
        session = SESSIONS.get(token)
        if session is None:
            self._deny()
            return

        # Static noVNC assets, served under the token so the page is
        # self-contained and the token never leaks via a Referer to elsewhere.
        if len(parts) > 2 and parts[2] != "ws":
            relative = "/".join(parts[2:])
            target = (NOVNC_ROOT / relative).resolve()
            try:
                # relative_to rejects prefix-siblings like /opt/novnc-backup
                # that startswith() would accept (audit 2026-08-24).
                target.relative_to(NOVNC_ROOT.resolve())
            except ValueError:
                self._deny("Not found.")
                return
            if not target.is_file():
                self._deny("Not found.")
                return
            kinds = {".js": "text/javascript", ".css": "text/css",
                     ".html": "text/html", ".json": "application/json"}
            self._send(200, target.read_bytes(),
                       kinds.get(target.suffix, "application/octet-stream"))
            return

        if len(parts) > 2 and parts[2] == "ws":
            self._websocket(token, session)
            return

        label = session["label"] or f"VM {session['vmid']}"
        safe_label = html.escape(label, quote=True)
        self._send(200,
                   PAGE.format(label=safe_label, view_only="false").encode(),
                   "text/html; charset=utf-8")

    def _websocket(self, token: str, session: dict[str, Any]) -> None:
        key = self.headers.get("Sec-WebSocket-Key")
        if not key or "websocket" not in (
                self.headers.get("Upgrade", "").lower()):
            self._deny("Not a WebSocket request.")
            return
        accept = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode()).digest()
        ).decode()
        config = load_config()
        try:
            proxy = mint_vnc_ticket(config, session["kind"], session["vmid"])
            upstream = UpstreamWebSocket(config, session["kind"],
                                         session["vmid"], proxy)
        except (error.HTTPError, error.URLError, OSError, RuntimeError,
                KeyError) as exc:
            sys.stderr.write(f"upstream failed for vm {session['vmid']}: {exc}\n")
            self._deny("Could not reach that console.")
            return

        SESSIONS.mark_used(token)
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.send_header("Sec-WebSocket-Protocol", "binary")
        self.end_headers()
        self.wfile.flush()
        relay(self.connection, b"", upstream, session["expires_at"])
        self.close_connection = True


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="pxl-share")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=8900)
    add = sub.add_parser("add")
    add.add_argument("--vmid", type=int, required=True)
    add.add_argument("--kind", default="qemu")
    add.add_argument("--minutes", type=int, default=30)
    add.add_argument("--label", default="")
    add.add_argument("--once", action="store_true")
    sub.add_parser("list")
    revoke = sub.add_parser("revoke")
    revoke.add_argument("--token")
    revoke.add_argument("--all", action="store_true")
    args = parser.parse_args()

    if args.command == "serve":
        server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
        server.daemon_threads = True
        sys.stderr.write(f"pxl-share listening on {args.port}\n")
        server.serve_forever()
        return 0
    if args.command == "add":
        print(json.dumps(SESSIONS.add(args.vmid, args.minutes, args.kind,
                                      args.label, args.once)))
        return 0
    if args.command == "list":
        print(json.dumps(SESSIONS.listing()))
        return 0
    if args.command == "revoke":
        if args.all:
            print(json.dumps({"revoked": SESSIONS.revoke_all()}))
        else:
            print(json.dumps({"revoked": int(SESSIONS.revoke(args.token))}))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
