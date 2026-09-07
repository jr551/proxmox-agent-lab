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
import binascii
import hashlib
import os
import socket
import ssl
import struct
import time
from urllib import parse


class WebSocketError(RuntimeError):
    pass


OPCODE_CONTINUATION = 0x0
OPCODE_TEXT = 0x1
OPCODE_BINARY = 0x2
OPCODE_CLOSE = 0x8
OPCODE_PING = 0x9
OPCODE_PONG = 0xA

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_FRAME_SIZE = 64 * 1024 * 1024
MAX_HANDSHAKE_SIZE = 64 * 1024


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
        if timeout <= 0:
            raise ValueError("WebSocket timeout must be positive")
        self.timeout = timeout
        self._read_deadline: float | None = None
        self._recv_buffer = bytearray()
        self._payload_buffer = bytearray()
        self._fragment_opcode: int | None = None
        self._fragments = bytearray()
        context = ssl.create_default_context()
        # The console carries guest input and output, so it gets the same
        # certificate policy as the REST client -- [proxmox] verify_tls -- and
        # not a private exemption. Verification stays off only while the node
        # still has the self-signed certificate a fresh install ships with.
        if not verify_tls:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        raw = socket.create_connection((host, port), timeout=timeout)
        try:
            self._socket = context.wrap_socket(
                raw, server_hostname=host if verify_tls else None
            )
        except BaseException:
            raw.close()
            raise
        try:
            self._read_deadline = time.monotonic() + timeout
            self._remaining_timeout()
            self._handshake(host, port, path, query, headers, subprotocols)
            self._read_deadline = None
            self._socket.settimeout(timeout)
        except BaseException:
            self._socket.close()
            raise

    def _handshake(self, host, port, path, query, headers, subprotocols) -> None:
        self._key = base64.b64encode(os.urandom(16)).decode()
        target = path + "?" + parse.urlencode(query)
        request_lines = [
            f"GET {target} HTTP/1.1",
            f"Host: {host}:{port}",
            "Connection: Upgrade",
            "Upgrade: websocket",
            "Sec-WebSocket-Version: 13",
            f"Sec-WebSocket-Key: {self._key}",
            f"Sec-WebSocket-Protocol: {', '.join(subprotocols)}",
        ]
        request_lines += [f"{name}: {value}" for name, value in headers.items()]
        self._socket.sendall(("\r\n".join(request_lines) + "\r\n\r\n").encode())
        response = self._read_until(b"\r\n\r\n")
        head, _, rest = response.partition(b"\r\n\r\n")
        self._recv_buffer += rest
        text = head.decode("latin-1")
        status = text.split("\r\n", 1)[0]
        if status.split()[:2] != ["HTTP/1.1", "101"]:
            raise WebSocketError(f"WebSocket upgrade refused: {status.strip()}")
        expected = base64.b64encode(
            hashlib.sha1((self._key + WS_GUID).encode()).digest()
        ).decode()
        response_headers = {}
        for line in text.split("\r\n")[1:]:
            name, _, value = line.partition(":")
            response_headers[name.strip().lower()] = value.strip()
        if response_headers.get("upgrade", "").lower() != "websocket":
            raise WebSocketError("WebSocket upgrade header missing or invalid")
        connection = {part.strip().lower() for part in response_headers.get("connection", "").split(",")}
        if "upgrade" not in connection:
            raise WebSocketError("WebSocket connection upgrade was not confirmed")
        accept = response_headers.get("sec-websocket-accept", "")
        self.subprotocol = response_headers.get("sec-websocket-protocol", "")
        if accept != expected:
            raise WebSocketError(
                f"WebSocket accept mismatch: got {accept!r}, expected {expected!r}"
            )
        if self.subprotocol and self.subprotocol not in subprotocols:
            raise WebSocketError("Proxmox selected an unsupported WebSocket subprotocol")
        self._base64 = self.subprotocol == "base64"

    def _read_until(self, marker: bytes) -> bytes:
        data = bytearray(self._recv_buffer)
        self._recv_buffer.clear()
        while marker not in data:
            if len(data) >= MAX_HANDSHAKE_SIZE:
                raise WebSocketError("WebSocket handshake headers are too large")
            self._remaining_timeout()
            chunk = self._socket.recv(4096)
            if not chunk:
                raise WebSocketError("connection closed during handshake")
            data += chunk
        if data.index(marker) + len(marker) > MAX_HANDSHAKE_SIZE:
            raise WebSocketError("WebSocket handshake headers are too large")
        return bytes(data)

    def _remaining_timeout(self) -> None:
        deadline = self._read_deadline
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("WebSocket read deadline exceeded")
            self._socket.settimeout(remaining)

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

    def _peek_exact(self, count: int) -> None:
        # Leave the entire frame buffered until complete: read_available may
        # time out between any two bytes and resume on its next invocation.
        self._remaining_timeout()
        while len(self._recv_buffer) < count:
            self._remaining_timeout()
            chunk = self._socket.recv(65536)
            if not chunk:
                raise WebSocketError("connection closed by Proxmox")
            self._recv_buffer += chunk

    def _read_frame(self) -> tuple[bool, int, bytes]:
        self._peek_exact(2)
        first, second = self._recv_buffer[:2]
        final = bool(first & 0x80)
        opcode = first & 0x0F
        if first & 0x70:
            raise WebSocketError("Proxmox sent unsupported WebSocket extensions")
        if opcode not in (0, 1, 2, 8, 9, 10):
            raise WebSocketError("Proxmox sent an unknown WebSocket opcode")
        if second & 0x80:
            raise WebSocketError("Proxmox sent a masked frame")
        length = second & 0x7F
        if opcode >= OPCODE_CLOSE and (not final or length > 125):
            raise WebSocketError("Proxmox sent an invalid control frame")
        offset = 2
        if length in (126, 127):
            width = 2 if length == 126 else 8
            self._peek_exact(offset + width)
            length = int.from_bytes(self._recv_buffer[offset:offset + width], "big")
            offset += width
        if length > MAX_FRAME_SIZE:
            raise WebSocketError(
                f"Proxmox sent a WebSocket frame larger than {MAX_FRAME_SIZE} bytes"
            )
        self._peek_exact(offset + length)
        payload = bytes(memoryview(self._recv_buffer)[offset:offset + length])
        del self._recv_buffer[:offset + length]
        return final, opcode, payload

    def send(self, data: bytes) -> None:
        """Send application payload, encoding it if base64 was negotiated."""
        if self._base64:
            self._send_frame(OPCODE_TEXT, base64.b64encode(data))
        else:
            self._send_frame(OPCODE_BINARY, data)

    def recv(self) -> bytes:
        """Return the next application payload, handling control frames."""
        while True:
            final, opcode, payload = self._read_frame()
            if opcode == OPCODE_CLOSE:
                raise WebSocketError("Proxmox closed the console stream")
            if opcode == OPCODE_PING:
                self._send_frame(OPCODE_PONG, payload)
                continue
            if opcode == OPCODE_PONG:
                continue
            if opcode == OPCODE_CONTINUATION:
                if self._fragment_opcode is None:
                    raise WebSocketError("Proxmox sent an unexpected continuation")
            elif self._fragment_opcode is not None:
                raise WebSocketError("Proxmox interrupted a fragmented message")
            else:
                self._fragment_opcode = opcode
            if len(self._fragments) + len(payload) > MAX_FRAME_SIZE:
                raise WebSocketError("Proxmox sent an oversized WebSocket message")
            if not final:
                self._fragments += payload
                continue
            if self._fragments:
                self._fragments += payload
                payload = bytes(self._fragments)
                self._fragments.clear()
            self._fragment_opcode = None
            if self._base64:
                try:
                    payload = base64.b64decode(
                        payload + b"=" * (-len(payload) % 4), validate=True
                    )
                except binascii.Error:
                    raise WebSocketError("Proxmox sent invalid base64 console data") from None
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
        if timeout <= 0:
            return b""
        previous = self._socket.gettimeout()
        previous_deadline = self._read_deadline
        self._read_deadline = time.monotonic() + timeout
        try:
            return self.recv()
        except (TimeoutError, socket.timeout, ssl.SSLWantReadError):
            return b""
        finally:
            self._read_deadline = previous_deadline
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
