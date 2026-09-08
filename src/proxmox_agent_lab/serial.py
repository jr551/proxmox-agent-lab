"""Guest serial/terminal transport: the Proxmox websocket session and the
line discipline that turns its byte stream into terminal text.

This is a transport: it knows the termproxy handshake, the status records
Proxmox itself puts on the stream, and the console/filter bookkeeping. It
does not know about commands, screenshots or transfers -- `lab` supplies the
shared configuration the same way it does for feature modules.
"""
from __future__ import annotations

from . import ws
from typing import Any

from . import textmode
import re
import secrets
import time


WS_PATH_TEMPLATE = "/api2/json/nodes/{node}/{kind}/{vmid}/vncwebsocket"


def _open_websocket(lab: Any, kind: str, vmid: int, proxy: dict[str, Any],
                    timeout: float) -> ws.WebSocket:
    token = lab.keychain_secret()
    return ws.WebSocket(
        lab.HOST,
        lab.PORT,
        WS_PATH_TEMPLATE.format(node=lab.NODE, kind=kind, vmid=vmid),
        {"port": str(proxy["port"]), "vncticket": proxy["ticket"]},
        {
            "Authorization": (
                f"PVEAPIToken={lab.TOKEN_USER}!{lab.TOKEN_NAME}={token}"
            )
        },
        timeout=timeout,
        # Same certificate policy as the REST client: a console carries guest
        # keystrokes and screen contents, so it must not be the one path that
        # trusts any certificate.
        verify_tls=bool(getattr(lab, "VERIFY_TLS", True)),
        ca_file=lab.CONFIG.proxmox.get("ca_file") or None,
    )


TERM_STATUS_RECORDS = (
    re.compile(rb"starting serial terminal on interface \S+"
               rb"(?: \(press [^)]*\))?"),
    re.compile(rb"Connected to tty \d+"),
    re.compile(rb"Type <Ctrl\+a q> to exit the console"
               rb"(?:, <Ctrl\+a Ctrl\+a> to enter Ctrl\+a itself)?"),
    # A stopped guest is reported *in the stream*: termproxy issues a ticket,
    # the websocket opens, and 'qm terminal' writes this and exits. It is a
    # transport answer, not something the guest printed.
    re.compile(rb"(?:VM|CT|Container) \d+ (?:is )?not running"),
)


TERM_STATUS_PREFIXES = (
    b"starting serial terminal on interface",
    b"Connected to tty",
    b"Type <Ctrl+a q> to exit the console",
)


TERM_HANDSHAKE_ACK = b"OK"


TERM_STATUS_PREFIX_MIN = 4


TERM_STATUS_PREFIX_MAX = 256


TERM_STATUS_WINDOW = 4096


def _is_status_record(line: bytes) -> bool:
    cleaned = line.strip(b"\r").strip()
    return any(
        pattern.fullmatch(cleaned) for pattern in TERM_STATUS_RECORDS
    )


def _is_blank_line(line: bytes) -> bool:
    """True for a line with no visible characters once escapes are removed.

    Blank lines and bare cursor/bracketed-paste sequences arrive around the
    status records, so they must not be mistaken for the guest's first real
    output and end the search early.
    """
    return not textmode.strip_ansi(line.decode("utf-8", "replace")).strip()


def _may_grow_into_status_record(partial: bytes) -> bool:
    """True while `partial` could still become a complete status record."""
    cleaned = partial.strip(b"\r")
    if not TERM_STATUS_PREFIX_MIN <= len(cleaned) <= TERM_STATUS_PREFIX_MAX:
        return False
    return any(
        prefix.startswith(cleaned) or cleaned.startswith(prefix)
        for prefix in TERM_STATUS_PREFIXES
    )


class TermFilter:
    """Remove Proxmox terminal transport records from one session's stream.

    Stateful on purpose, for three reasons. The handshake acknowledgement is
    a bare "OK" that must be recognised exactly once, so a later guest line
    beginning "OK" is not truncated -- which the old prefix test did. A record
    can be split across websocket reads, so an undecidable tail is held rather
    than guessed at. And a record is not always the first thing on the stream:
    a blank line or the console's echo can precede it, so the search runs over
    a bounded startup window instead of stopping at the first guest byte.

    Nothing is held once the window closes, so an interactive session (the
    bridge, a debugger prompt) is never delayed by this.
    """

    def __init__(self) -> None:
        self._pending = bytearray()
        self._handshake_done = False
        self._watching = True
        self._scanned = 0

    def feed(self, data: bytes) -> bytes:
        """Return the guest bytes in `data`, holding an incomplete record."""
        if not self._watching:
            return data
        self._pending += data
        return self._drain()

    def flush(self) -> bytes:
        """Release what is held, at the end of a session.

        A tail that is still a prefix of a status record is the record that was
        already being matched, truncated by the session ending, so it is
        dropped. Anything else is guest output and is handed over.
        """
        pending = bytes(self._pending)
        self._pending.clear()
        self._watching = False
        if not self._handshake_done:
            self._handshake_done = True
            if TERM_HANDSHAKE_ACK.startswith(pending):
                return b""
            if pending.startswith(TERM_HANDSHAKE_ACK):
                pending = pending[len(TERM_HANDSHAKE_ACK):]
        if _may_grow_into_status_record(pending):
            return b""
        return pending

    def _take_handshake(self) -> bool:
        """Consume the auth acknowledgement. False while it is still arriving."""
        if self._pending[:2] == TERM_HANDSHAKE_ACK:
            del self._pending[:2]
            if self._pending[:2] == b"\r\n":
                del self._pending[:2]
            elif self._pending[:1] == b"\n":
                del self._pending[:1]
            self._handshake_done = True
            return True
        if TERM_HANDSHAKE_ACK.startswith(bytes(self._pending)):
            return False            # only "O" so far; the rest is in flight
        self._handshake_done = True  # no ack on this stream
        return True

    def _drain(self) -> bytes:
        if not self._handshake_done and not self._take_handshake():
            return b""
        out = bytearray()
        while self._pending:
            newline = self._pending.find(b"\n")
            if newline == -1:
                tail = bytes(self._pending)
                if _may_grow_into_status_record(tail):
                    break               # hold: the record may still complete
                out += tail
                self._pending.clear()
                self._scanned += len(tail)
                if self._scanned > TERM_STATUS_WINDOW:
                    self._watching = False
                break
            line = bytes(self._pending[:newline + 1])
            del self._pending[:newline + 1]
            if _is_status_record(line):
                # An LXC console emits two of these back to back, so keep
                # looking rather than stopping at the first.
                continue
            out += line
            self._scanned += len(line)
            if not _is_blank_line(line) or self._scanned > TERM_STATUS_WINDOW:
                # The guest has started talking: nothing more is transport.
                self._watching = False
                break
        if not self._watching and self._pending:
            out += self._pending
            self._pending.clear()
        return bytes(out)


class TermSession:
    """A live Proxmox terminal session (LXC console or QEMU serial)."""

    def __init__(self, lab: Any, api: Any, kind: str, vmid: int,
                 timeout: float = 25.0) -> None:
        self.lab = lab
        proxy = api.call("POST", f"/nodes/{lab.NODE}/{kind}/{vmid}/termproxy")
        if not isinstance(proxy, dict) or "ticket" not in proxy:
            raise lab.LabError(f"termproxy did not return a ticket for {kind}/{vmid}. A QEMU "
                "guest needs a serial device (serial0: socket) for this path.",
            )
        self.socket = _open_websocket(lab, kind, vmid, proxy, timeout)
        self.filter = TermFilter()
        self.last_read_was_empty = True
        # Proxmox's terminal protocol: authenticate, then set the window size.
        self.socket.send(f"{proxy['user']}:{proxy['ticket']}\n".encode())
        self.socket.send(b"1:120:40:")

    def read_bytes(self, timeout: float) -> bytes:
        """Guest bytes only. Transport records never reach the caller.

        An empty return does not mean the socket was idle -- a read that
        contained nothing but a transport record filters down to nothing -- so
        `last_read_was_empty` records what actually arrived, for callers that
        stop at the first gap in output.
        """
        raw = self.socket.read_available(timeout)
        self.last_read_was_empty = not raw
        return self.filter.feed(raw)

    def flush_bytes(self) -> bytes:
        """Guest bytes still held back when the session ends."""
        return self.filter.flush()

    def send_line(self, text: str) -> None:
        # Proxmox's terminal frame is "0:<length>:<data>" where length counts
        # bytes, not characters. Measuring the str would under-declare any
        # non-ASCII payload and desynchronise the stream.
        payload = (text + "\n").encode()
        self.socket.send(b"0:" + str(len(payload)).encode() + b":" + payload)

    def send_raw(self, text: str) -> None:
        # No trailing newline: a kernel debugger prompt (KDB, GRUB, a paused
        # bootloader) often acts on bare characters, and appending "\n" would
        # change their meaning.
        payload = text.encode()
        if payload:
            self.socket.send(b"0:" + str(len(payload)).encode() + b":" + payload)

    def read(self, seconds: float) -> str:
        deadline = time.monotonic() + seconds
        chunks: list[bytes] = []
        while time.monotonic() < deadline:
            data = self.read_bytes(max(0.2, deadline - time.monotonic()))
            if data:
                chunks.append(data)
            elif self.last_read_was_empty and chunks:
                # Stop at a real gap in guest output. A read that held only a
                # transport record is not a gap: stopping there would drop the
                # prompt or boot line that follows it.
                break
        chunks.append(self.flush_bytes())
        return b"".join(chunks).decode("utf-8", "replace")

    def expect(self, patterns: tuple[str, ...], timeout: float = 60.0,
               poke: bool = False) -> tuple[str, str]:
        """Read until one of `patterns` appears. Returns (matched, transcript).

        Cloud images print asynchronously and may already have drawn their
        prompt before we attach, so `poke` sends a newline periodically to
        make an idle console redraw it.
        """
        deadline = time.monotonic() + timeout
        buffer = ""
        last_poke = 0.0
        while time.monotonic() < deadline:
            chunk = self.read_bytes(1.5)
            if chunk:
                buffer += chunk.decode("utf-8", "replace")
                for pattern in patterns:
                    if pattern in buffer:
                        return pattern, buffer
            elif poke and time.monotonic() - last_poke > 5:
                last_poke = time.monotonic()
                self.send_line("")
        raise TimeoutError(
            f"none of {patterns} appeared within {timeout}s; last saw: "
            + repr(textmode.strip_ansi(buffer)[-300:])
        )

    def login(self, user: str, password: str, timeout: float = 240.0) -> None:
        """Log in at a getty prompt, or do nothing if already at a shell.

        A serial console keeps whatever state the last session left, so a
        second run would otherwise hang waiting for a login prompt that will
        never be printed again.
        """
        self.send_line("")
        try:
            matched, _ = self.expect(("login:", "$ ", "# "), timeout=15)
            if matched in ("$ ", "# "):
                return
        except TimeoutError:
            pass
        self.expect(("login:",), timeout=timeout, poke=True)
        self.send_line(user)
        # A guest with no password set -- an installer, a rescue shell, a
        # stock appliance -- drops straight to a shell and never prints a
        # password prompt. Waiting only for "assword:" hung there for the full
        # timeout, which made an empty password useless even once it was
        # allowed through.
        matched, _ = self.expect(("assword:", "$ ", "# "), timeout=60)
        if matched in ("$ ", "# "):
            return
        self.send_line(password)
        matched, transcript = self.expect(
            ("$ ", "# ", "Login incorrect"), timeout=60
        )
        if matched == "Login incorrect":
            raise RuntimeError("serial login was rejected")

    def run(self, command: str, timeout: float = 600.0) -> str:
        """Run one shell command and return only its output."""
        return self.run_status(command, timeout)[0]

    def run_status(
        self, command: str, timeout: float = 600.0
    ) -> tuple[str, int | None]:
        """Run one command; return (output, exit code).

        The output is bracketed by two markers so the caller gets the
        command's output alone. Without that, the transcript also contains
        the console's echo of the command, which callers then have to parse
        around -- a reliable source of subtle bugs, since a command
        mentioning "nameserver" or "REACHABLE" looks just like its own result.

        Each marker is typed with a split string literal (`__b""<token>__`)
        that the shell rejoins but the echo cannot reproduce, so a marker can
        never match its own echo -- including when the console hard-wraps the
        command mid-token.
        """
        token = secrets.token_hex(4)
        begin, end = f"__b{token}__", f"__e{token}__"
        self.send_line(
            f'echo "__b""{token}__"; {command}; echo "__e""{token}__$?"'
        )
        _, transcript = self.expect((end,), timeout=timeout)
        text = textmode.strip_ansi(transcript).replace("\r", "")

        opened = text.find(begin)
        body_start = 0
        if opened != -1:
            newline = text.find("\n", opened)
            body_start = len(text) if newline == -1 else newline + 1

        closed = text.find(end, body_start)
        if closed == -1:
            return text[body_start:].strip("\n"), None
        line_start = text.rfind("\n", body_start, closed) + 1
        tail = text[closed + len(end):].split("\n", 1)[0].strip()
        return (
            text[body_start:line_start].strip("\n"),
            int(tail) if tail.isdigit() else None,
        )

    def close(self) -> None:
        self.socket.close()

    def __enter__(self) -> "TermSession":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
