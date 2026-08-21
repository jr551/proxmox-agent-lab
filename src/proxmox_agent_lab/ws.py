"""Minimal stdlib-only WebSocket client for the Proxmox console endpoints.

Proxmox tunnels both the VNC (RFB) stream and the serial/LXC terminal stream
over `/api2/json/nodes/<node>/<kind>/<vmid>/vncwebsocket`. Depending on the
negotiated subprotocol the payload is either raw binary or base64 text, so both
are handled here and hidden from callers.

Certificate verification is the caller's decision and defaults to on: the
console stream is not less sensitive than the REST API, so it follows the same
`[proxmox] verify_tls` switch instead of quietly trusting any certificate.
"""

from __future__ import annotations

import base64
import os
import socket
import ssl
import struct
from urllib import parse


class WebSocketError(RuntimeError):
    pass


OPCODE_CONTINUATION = 0x0
OPCODE_TEXT = 0x1
OPCODE_BINARY = 0x2
OPCODE_CLOSE = 0x8
OPCODE_PING = 0x9
OPCODE_PONG = 0xA


class WebSocket:
    """A client-side WebSocket over TLS, framed for one console session."""

    def __init__(
        self,
        host: str,
        port: int,
        path: str,
        query: dict[str, str],
        headers: dict[str, str],
        *,
        subprotocols: tuple[str, ...] = ("binary", "base64"),
        timeout: float = 20.0,
        verify_tls: bool = True,
    ) -> None:
        self.timeout = timeout
        self._recv_buffer = bytearray()
        self._payload_buffer = bytearray()
        context = ssl.create_default_context()
        # The console carries guest input and output, so it gets the same
        # certificate policy as the REST client -- [proxmox] verify_tls -- and
        # not a private exemption. Verification stays off only while the node
        # still has the self-signed certificate a fresh install ships with.
        if not verify_tls:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        raw = socket.create_connection((host, port), timeout=timeout)
        self._socket = context.wrap_socket(
            raw, server_hostname=host if verify_tls else None
        )
        self._socket.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        target = path + "?" + parse.urlencode(query)
        request_lines = [
            f"GET {target} HTTP/1.1",
            f"Host: {host}:{port}",
            "Connection: Upgrade",
            "Upgrade: websocket",
            "Sec-WebSocket-Version: 13",
            f"Sec-WebSocket-Key: {key}",
            f"Sec-WebSocket-Protocol: {', '.join(subprotocols)}",
        ]
        request_lines += [f"{name}: {value}" for name, value in headers.items()]
        self._socket.sendall(("\r\n".join(request_lines) + "\r\n\r\n").encode())
        response = self._read_until(b"\r\n\r\n")
        head, _, rest = response.partition(b"\r\n\r\n")
        self._recv_buffer += rest
        text = head.decode("latin-1")
        status = text.split("\r\n", 1)[0]
        if "101" not in status:
            raise WebSocketError(f"WebSocket upgrade refused: {status.strip()}")
        self.subprotocol = ""
        for line in text.split("\r\n")[1:]:
            name, _, value = line.partition(":")
            if name.strip().lower() == "sec-websocket-protocol":
                self.subprotocol = value.strip().lower()
        self._base64 = self.subprotocol == "base64"

    def _read_until(self, marker: bytes) -> bytes:
        data = bytearray(self._recv_buffer)
        self._recv_buffer.clear()
        while marker not in data:
            chunk = self._socket.recv(4096)
            if not chunk:
                raise WebSocketError("connection closed during handshake")
            data += chunk
        return bytes(data)

    def _recv_exact(self, count: int) -> bytes:
        while len(self._recv_buffer) < count:
            chunk = self._socket.recv(65536)
            if not chunk:
                raise WebSocketError("connection closed by Proxmox")
            self._recv_buffer += chunk
        out = bytes(self._recv_buffer[:count])
        del self._recv_buffer[:count]
        return out

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        mask = os.urandom(4)
        header += mask
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self._socket.sendall(bytes(header) + masked)

    def _read_frame(self) -> tuple[int, bytes]:
        first, second = self._recv_exact(2)
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._recv_exact(8))[0]
        if second & 0x80:  # a server frame must not be masked
            raise WebSocketError("Proxmox sent a masked frame")
        return opcode, self._recv_exact(length)

    def send(self, data: bytes) -> None:
        """Send application payload, encoding it if base64 was negotiated."""
        if self._base64:
            self._send_frame(OPCODE_TEXT, base64.b64encode(data))
        else:
            self._send_frame(OPCODE_BINARY, data)

    def recv(self) -> bytes:
        """Return the next application payload, handling control frames."""
        while True:
            opcode, payload = self._read_frame()
            if opcode == OPCODE_CLOSE:
                raise WebSocketError("Proxmox closed the console stream")
            if opcode == OPCODE_PING:
                self._send_frame(OPCODE_PONG, payload)
                continue
            if opcode == OPCODE_PONG:
                continue
            if self._base64:
                payload = base64.b64decode(payload + b"=" * (-len(payload) % 4))
            return payload

    def read_exact(self, count: int) -> bytes:
        """Return exactly `count` bytes of application payload."""
        while len(self._payload_buffer) < count:
            self._payload_buffer += self.recv()
        out = bytes(self._payload_buffer[:count])
        del self._payload_buffer[:count]
        return out

    def read_available(self, timeout: float) -> bytes:
        """Return whatever payload arrives within `timeout`, possibly empty."""
        if self._payload_buffer:
            out = bytes(self._payload_buffer)
            self._payload_buffer.clear()
            return out
        previous = self._socket.gettimeout()
        self._socket.settimeout(timeout)
        try:
            return self.recv()
        except (TimeoutError, socket.timeout, ssl.SSLWantReadError):
            return b""
        finally:
            self._socket.settimeout(previous)

    def close(self) -> None:
        try:
            self._send_frame(OPCODE_CLOSE, b"")
        except OSError:
            pass
        try:
            self._socket.close()
        except OSError:
            pass

    def __enter__(self) -> "WebSocket":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
