"""Console access for lab guests: VNC screenshots, keyboard, pointer,
serial/LXC terminal text, guest-agent execution, and file transfer.

Design notes
------------
* Screenshots are PNG. Multimodal models read them directly, so OCR is never
  applied automatically -- see `lab_textmode` for the opt-in text-mode decoder.
* When a guest really is a terminal, prefer `console text`: Proxmox hands over
  the actual character stream, which is exact where any OCR is a guess.
* File transfer goes through the S3 scratch bucket using presigned URLs. No
  credential ever reaches the guest, the command line, or the audit ledger.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
import re
import secrets
import shlex
import struct
import time
from typing import Any

from . import png as png_module
from . import rfb
from . import s3
from . import secrets_store
from . import textmode
from . import vision
from . import ws

WS_PATH_TEMPLATE = "/api2/json/nodes/{node}/{kind}/{vmid}/vncwebsocket"
DEFAULT_SCREENSHOT_DIR = Path.home() / ".local" / "state" / "proxmox-agent-lab" / "screens"
SAFE_KEY = re.compile(r"^[a-z0-9-]{1,40}$")

# Chunked transfers: files above SINGLE_OBJECT_MAX_MB are moved in parts so a
# retry resumes instead of restarting, and the assembled file is verified
# against a SHA-256 on both ends. Linux guests only (curl + split); Windows
# and --url-only keep the single-object path.
SINGLE_OBJECT_MAX_MB = 32
CHUNK_DEFAULT_MB = 64
MAX_CHUNK_PARTS = 256


def _api_error(lab: Any, message: str) -> Exception:
    return lab.LabError(message)


def _kind_of(lab: Any, api: Any, vmid: int) -> str:
    """Return 'qemu' or 'lxc' for a VMID on the lab node."""
    for kind in ("qemu", "lxc"):
        try:
            api.call("GET", f"/nodes/{lab.NODE}/{kind}/{vmid}/status/current")
            return kind
        except lab.LabError:
            continue
    raise _api_error(lab, f"VMID {vmid} is not a QEMU VM or LXC container on {lab.NODE}")


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
    )


class VncSession:
    """A live RFB session against one QEMU guest."""

    def __init__(self, lab: Any, api: Any, vmid: int, timeout: float = 25.0) -> None:
        self.lab = lab
        self.vmid = vmid
        proxy = api.call(
            "POST", f"/nodes/{lab.NODE}/qemu/{vmid}/vncproxy", {"websocket": 1}
        )
        if not isinstance(proxy, dict) or "ticket" not in proxy:
            raise _api_error(lab, f"vncproxy did not return a ticket for {vmid}")
        self.socket = _open_websocket(lab, "qemu", vmid, proxy, timeout)
        try:
            self.client = rfb.RFBClient(self.socket, proxy["ticket"])
        except Exception:
            self.socket.close()
            raise

    def close(self) -> None:
        self.socket.close()

    def __enter__(self) -> "VncSession":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


# Proxmox's terminal path puts its own records on the same stream as the
# guest: the websocket auth is acknowledged with a bare "OK", and the process
# behind termproxy ('qm terminal' for a QEMU serial line, lxc-console for a
# container) announces itself before the guest has said anything. None of it is
# guest output, and a caller cannot tell the difference: it saves the line into
# a boot log, matches it as a boot marker, or feeds it to a kernel debugger.
# So it is removed here, once, for every consumer of a session.
#
# Observed framing on a Proxmox 9.2 node, which is why this is not a simple
# prefix test:
#
#     b'OK'                                                  <- ack, no newline
#     b'\r\n'                                                 <- may or may not
#     b'starting serial terminal on interface serial0\r\n'    <- the record
#     b'de' b'bian' b'@' ...                                 <- guest, byte-wise
#
# The ack arrives alone, the record is CRLF-terminated, blank lines and console
# echo can precede it, and guest output is split at arbitrary byte boundaries.
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
# The literal openings of the records above, used to recognise one that is
# still arriving: a websocket read is not a record boundary.
TERM_STATUS_PREFIXES = (
    b"starting serial terminal on interface",
    b"Connected to tty",
    b"Type <Ctrl+a q> to exit the console",
)
# 'VM <id> not running' is deliberately absent above: its opening is short and
# generic, and it is matched only as a complete line.
TERM_HANDSHAKE_ACK = b"OK"

# A partial is only treated as a possibly-incomplete record once it is this
# long. Below it, output goes straight through: an interactive prompt ends
# without a newline, and must never be held back waiting for one.
TERM_STATUS_PREFIX_MIN = 4
# A record is well under this. Past it, whatever is buffered is guest output
# that merely started like one.
TERM_STATUS_PREFIX_MAX = 256
# How much guest output is watched for a status record before the filter stops
# looking. The records are emitted once, at session start.
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
            raise _api_error(
                lab,
                f"termproxy did not return a ticket for {kind}/{vmid}. A QEMU "
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


# --- guest agent ---------------------------------------------------------


def agent_exec(lab: Any, api: Any, vmid: int, command: list[str], *,
               input_data: str | None = None, timeout: int = 300) -> dict[str, Any]:
    """Run a command through qemu-guest-agent and wait for its result."""
    payload: dict[str, Any] = {"command": command}
    if input_data is not None:
        payload["input-data"] = base64.b64encode(input_data.encode()).decode()
    started = api.call(
        "POST", f"/nodes/{lab.NODE}/qemu/{vmid}/agent/exec", payload
    )
    pid = started.get("pid") if isinstance(started, dict) else None
    if pid is None:
        raise _api_error(lab, f"guest agent did not return a pid: {started}")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = api.call(
            "GET", f"/nodes/{lab.NODE}/qemu/{vmid}/agent/exec-status", {"pid": pid}
        )
        if status.get("exited"):
            def decode(field: str) -> str:
                # Proxmox already decodes what qemu-guest-agent base64s, so
                # out-data/err-data arrive as plain text. Decoding again
                # corrupts any output that is *coincidentally* valid base64 --
                # a bare timestamp, a hex digest -- while everything else
                # raises and silently falls through looking correct.
                raw = status.get(field, "")
                return raw if isinstance(raw, str) else ""

            exitcode = status.get("exitcode")
            signal = status.get("signal")
            if exitcode is None and signal is not None:
                # qemu-guest-agent reports either exitcode or signal, never
                # both. Every caller checks `exitcode not in (0, None)` to
                # decide success -- leaving this as None would make a
                # signal-killed process (OOM, crash, an external kill) look
                # like the "no code available" case serial legitimately
                # has, instead of the failure it actually is. 128+signal is
                # the standard shell convention for "killed by signal N".
                exitcode = 128 + int(signal)
            return {
                "exitcode": exitcode,
                "signal": signal,
                "stdout": decode("out-data"),
                "stderr": decode("err-data"),
                "truncated": bool(
                    status.get("out-truncated") or status.get("err-truncated")
                ),
            }
        time.sleep(1)
    raise _api_error(lab, f"guest command did not finish within {timeout}s")


def agent_ready(lab: Any, api: Any, vmid: int) -> bool:
    try:
        api.call("POST", f"/nodes/{lab.NODE}/qemu/{vmid}/agent/ping")
        return True
    except lab.LabError:
        return False


# --- command handlers ----------------------------------------------------


def _screenshot_path(vmid: int, override: str | None, suffix: str = "") -> Path:
    if override:
        return Path(override).expanduser()
    DEFAULT_SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return DEFAULT_SCREENSHOT_DIR / f"vm{vmid}-{stamp}{suffix}.png"


def _save_screenshot(vmid: int, rgb: bytes, width: int, height: int,
                     override: str | None = None,
                     state_root: Path | None = None) -> dict[str, Any]:
    """Write one captured framebuffer and return its machine-readable facts."""
    encoded = png_module.encode_png(width, height, rgb)
    target = _screenshot_path(vmid, override)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(encoded)
    analysis = textmode.analyse(rgb, width, height)
    result: dict[str, Any] = {
        "vmid": vmid,
        "path": str(target),
        "width": width,
        "height": height,
        "bytes": len(encoded),
        "looks_like_text_console": analysis["looks_like_text_console"],
        "distinct_colours": analysis["distinct_colours"],
    }
    if analysis["looks_like_text_console"]:
        result["agent_hint"] = (
            "Prefer console text for exact characters; use --ocr only for a "
            "VGA text grid"
        )
    else:
        result["agent_hint"] = (
            "Read this PNG with vision. If this model has no vision, delegate "
            "the single-screen decision to a vision-capable model; do not use "
            "Tesseract, OCR, crops, or image filters."
        )
    identical = _mark_stale_frame(
        vmid, rgb, width, height, state_root or DEFAULT_SCREENSHOT_DIR.parent
    )
    result["identical_to_previous_capture"] = identical
    if identical:
        result["stale_possible"] = (
            "screen unchanged since last capture; if input was sent in "
            "between, the framebuffer may be stale \u2014 recapture before acting"
        )
    return result


def _mark_stale_frame(vmid: int, rgb: bytes, width: int, height: int,
                      state_root: Path) -> bool:
    """Compare a capture with the previous one for the same VM+resolution.

    QEMU's VNC dirty tracking can hand back the pre-action frame right after
    rapid input.  Keeping one raw frame per VM lets callers notice a
    pixel-identical repeat instead of acting on a stale screen.  This is
    best-effort: any store failure degrades to "not identical" rather than
    failing the capture.
    """
    previous_dir = Path(state_root) / "vision-previous"
    key = previous_dir / f"screenshot-vm{vmid}-{width}x{height}.rgb"
    previous = b""
    try:
        previous = key.read_bytes()
    except OSError:
        pass
    identical = len(previous) == len(rgb) and previous == rgb
    try:
        previous_dir.mkdir(parents=True, exist_ok=True)
        temporary = key.with_suffix(".tmp")
        temporary.write_bytes(rgb)
        temporary.replace(key)
    except OSError:
        return False
    return identical


def _capture_after_action(lab: Any, api: Any, args: Any,
                          session: VncSession | None = None) -> dict[str, Any] | None:
    """Optionally capture the settled screen as part of an input command.

    Keeping input and observation in one command avoids the common agent loop
    of click, reconnect, screenshot, crop, OCR, and repeat.
    """
    settle = getattr(args, "screenshot_after", None)
    if settle is None:
        return None
    if session is None:
        with VncSession(lab, api, args.vmid) as new_session:
            rgb = new_session.client.capture(timeout=25.0, settle=settle)
            width, height = new_session.client.width, new_session.client.height
    else:
        rgb = session.client.capture(timeout=25.0, settle=settle)
        width, height = session.client.width, session.client.height
    return _save_screenshot(
        args.vmid, rgb, width, height, getattr(args, "screenshot_out", None),
        state_root=lab.STATE_ROOT,
    )


def _model_frame(lab: Any, lease_id: str, vmid: int, rgb: bytes, width: int,
                 height: int) -> tuple[bytes, dict[str, Any]]:
    """Build temporal model guidance while retaining the untouched frame."""
    state = Path(lab.STATE_ROOT) / "vision-previous"
    state.mkdir(parents=True, exist_ok=True)
    safe_lease = "".join(c for c in lease_id if c.isalnum() or c in "-_")
    target = state / f"{safe_lease}-vm{vmid}-{width}x{height}.rgb"
    previous = b""
    try:
        previous = target.read_bytes()
    except OSError:
        pass
    temporary = target.with_suffix(".tmp")
    temporary.write_bytes(rgb)
    temporary.replace(target)
    if len(previous) != len(rgb):
        return rgb, {"mode": "full", "baseline": False, "changed_pixels": None}
    highlighted, changed = png_module.highlight_changes(
        width, height, rgb, previous
    )
    ratio = changed / (width * height)
    # Nearly identical frames and wholesale screen transitions are clearer in
    # full. Temporal emphasis is for cursor, dialog and progress changes.
    if ratio < 0.0001 or ratio > 0.35:
        return rgb, {
            "mode": "full", "baseline": True, "changed_pixels": changed,
            "changed_ratio": round(ratio, 6),
        }
    return highlighted, {
        "mode": "changed-highlight", "baseline": True,
        "changed_pixels": changed, "changed_ratio": round(ratio, 6),
        "unchanged_brightness_percent": 35, "outline": "magenta",
    }


# --- screendump fallback -------------------------------------------------
#
# VNC is the screenshot path: it returns pixels to the controller and touches
# nothing on the host. QEMU's own screendump exists for the cases VNC cannot
# serve, but it *writes a file on the Proxmox host*, so it is not read-only the
# way 'virtio monitor' is, and it needs the opt-in host SSH channel to bring
# the PNG back. Hence: explicit, lease-scoped, PNG-only, and never the default.
# Arbitrary monitor commands are deliberately not exposed.
MONITOR_SCREENSHOT_ROOT = "/var/tmp/proxmox-agent-lab-screens"


def _monitor_remote_path(lease_id: str, vmid: int) -> str:
    """The one host path a monitor screenshot may write, built here.

    Scoped to the lease so two leases cannot collide or read each other's
    capture, and never taken from an argument: there is no way to ask this
    command to write somewhere else on the host.
    """
    safe_lease = "".join(c for c in str(lease_id) if c.isalnum() or c in "-_")
    if not safe_lease:
        raise ValueError("a monitor screenshot needs a lease id")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return f"{MONITOR_SCREENSHOT_ROOT}/{safe_lease[:64]}/vm{int(vmid)}-{stamp}.png"


def _screendump_command(remote_path: str) -> str:
    """The only monitor command this path will ever send."""
    if not remote_path.startswith(MONITOR_SCREENSHOT_ROOT + "/"):
        raise ValueError(
            f"refusing a screendump outside {MONITOR_SCREENSHOT_ROOT}"
        )
    if not remote_path.endswith(".png"):
        raise ValueError("a monitor screenshot may only be written as PNG")
    if ".." in remote_path or any(c.isspace() for c in remote_path):
        raise ValueError(f"unsafe screendump path: {remote_path!r}")
    return f"screendump {remote_path} -f png"


def _png_dimensions(data: bytes) -> tuple[int, int]:
    """Read width and height out of a PNG header, proving it is one."""
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise ValueError("the host did not return a PNG")
    width, height = struct.unpack(">II", data[16:24])
    if not width or not height:
        raise ValueError("the host returned a PNG with no pixels")
    return int(width), int(height)


def _screenshot_via_monitor(lab: Any, api: Any, args: Any) -> dict[str, Any]:
    """Capture with QEMU screendump, fetch the PNG, delete the host copy."""
    from . import memflow

    if not getattr(args, "lease", None):
        raise _api_error(lab, "console screenshot --via monitor requires --lease")
    if getattr(args, "ocr", False):
        raise _api_error(
            lab,
            "--ocr needs the raw framebuffer; use the default --via vnc for it",
        )
    _require_owned_qemu(lab, args.lease, args.vmid)
    memflow.require_host_ssh(lab)
    remote = _monitor_remote_path(args.lease, args.vmid)
    command = _screendump_command(remote)
    memflow.host_mkdir(lab, remote.rsplit("/", 1)[0])
    timeout = max(30, int(getattr(args, "timeout", 25) or 25))
    removed = False
    try:
        answer = api.call(
            "POST", f"/nodes/{lab.NODE}/qemu/{args.vmid}/monitor",
            {"command": command},
        )
        # QEMU's monitor reports a refusal in the response body, not as an
        # HTTP error, so an unsupported format or a stopped guest would
        # otherwise look like success with no file to read.
        if isinstance(answer, str) and answer.strip():
            raise _api_error(
                lab, f"QEMU screendump refused: {answer.strip()[:300]}"
            )
        data = memflow.host_read_bytes(lab, remote, timeout=timeout)
    finally:
        removed = memflow.host_remove_file(lab, remote)
        # Best effort, and only if empty: leaves nothing of ours on the host.
        memflow.host_remove_empty_dir(lab, remote.rsplit("/", 1)[0])
    width, height = _png_dimensions(data)
    target = _screenshot_path(args.vmid, getattr(args, "out", None), "-monitor")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    # The fact of the capture, never its contents: a screen can show anything.
    lab.audit("console-screenshot", lease=args.lease, vmid=args.vmid,
              source="monitor", width=width, height=height,
              bytes=len(data), host_file_removed=removed, sync=False)
    result: dict[str, Any] = {
        "vmid": args.vmid,
        "source": "monitor",
        "path": str(target),
        "width": width,
        "height": height,
        "bytes": len(data),
        "host_file_removed": removed,
        "agent_hint": (
            "Read this PNG with vision. This capture came from QEMU, not VNC, "
            "so it carries no text-console analysis and no stale-frame check "
            "-- prefer the default --via vnc unless VNC itself is the problem."
        ),
    }
    if not removed:
        result["host_file_warning"] = (
            f"could not delete {remote} on the host; remove it manually"
        )
    if getattr(args, "upload", False):
        key = f"screens/vm{args.vmid}-{int(time.time())}.png"
        s3.put_bytes(key, data, "image/png")
        result["s3_key"] = key
        result["s3_url"] = s3.presign(key, expires=args.url_expiry)
    return result


def cmd_screenshot(lab: Any, args: Any) -> None:
    api = lab.ProxmoxAPI()
    if getattr(args, "via", "vnc") == "monitor":
        print(json.dumps(
            _screenshot_via_monitor(lab, api, args), indent=2, sort_keys=True
        ))
        return
    with VncSession(lab, api, args.vmid) as session:
        rgb = session.client.capture(timeout=args.timeout, settle=args.settle)
        width, height = session.client.width, session.client.height
    result = _save_screenshot(
        args.vmid, rgb, width, height, args.out, state_root=lab.STATE_ROOT
    )
    result["source"] = "vnc"
    target = Path(result["path"])
    png = target.read_bytes()
    analysis = textmode.analyse(rgb, width, height)
    if args.upload:
        key = f"screens/vm{args.vmid}-{int(time.time())}.png"
        s3.put_bytes(key, png, "image/png")
        result["s3_key"] = key
        result["s3_url"] = s3.presign(key, expires=args.url_expiry)
    if args.ocr:
        if not analysis["looks_like_text_console"]:
            result["ocr_error"] = (
                "screen is not a text console; read the PNG directly, or use "
                "'console text' for a real terminal stream"
            )
        else:
            result["ocr"] = textmode.decode_screen(rgb, width, height)
    print(json.dumps(result, indent=2, sort_keys=True))


def _require_owned_qemu(lab: Any, lease_id: str, vmid: int) -> None:
    lab.require_lease_resource(lab.load_lease(lease_id), "qemu", vmid)
def cmd_screenshot_burst(lab: Any, args: Any) -> None:
    """Capture several screenshots over time as one stitched image.

    For watching something that changes slowly -- a progress bar, an
    installer's copy step, a boot animation -- without a manual sleep-then-
    screenshot loop. One VNC session stays open for the whole burst.
    """
    if args.count < 1:
        raise lab.LabError("--count must be at least 1")
    if args.interval < 0:
        raise lab.LabError("--interval must not be negative")
    api = lab.ProxmoxAPI()
    frames: list[tuple[int, int, bytes, str]] = []
    started = time.monotonic()
    with VncSession(lab, api, args.vmid) as session:
        for index in range(args.count):
            rgb = session.client.capture(timeout=args.timeout, settle=0)
            width, height = session.client.width, session.client.height
            elapsed = int(time.monotonic() - started)
            frames.append((width, height, rgb, str(elapsed)))
            if index < args.count - 1:
                time.sleep(args.interval)
    total_width, total_height, stitched = png_module.stitch_horizontal(frames)
    encoded = png_module.encode_png(total_width, total_height, stitched)
    target = _screenshot_path(args.vmid, args.out, suffix="-burst")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(encoded)
    result: dict[str, Any] = {
        "vmid": args.vmid,
        "path": str(target),
        "width": total_width,
        "height": total_height,
        "bytes": len(encoded),
        "frame_count": len(frames),
        "interval_seconds": args.interval,
        "elapsed_seconds": [int(label) for _, _, _, label in frames],
        "agent_hint": (
            "Frames run left to right in capture order, each labelled with "
            "its elapsed seconds in its top-left corner. Read this PNG with "
            "vision to see what changed across the sequence."
        ),
    }
    if args.upload:
        key = f"screens/vm{args.vmid}-{int(time.time())}-burst.png"
        s3.put_bytes(key, encoded, "image/png")
        result["s3_key"] = key
        result["s3_url"] = s3.presign(key, expires=args.url_expiry)
    print(json.dumps(result, indent=2, sort_keys=True))


def cmd_inspect(lab: Any, args: Any) -> None:
    """Capture and explicitly send one lease-owned screen to cloud vision."""
    _require_owned_qemu(lab, args.lease, args.vmid)
    api = lab.ProxmoxAPI()
    with VncSession(lab, api, args.vmid) as session:
        rgb = session.client.capture(timeout=25.0, settle=args.settle)
        width, height = session.client.width, session.client.height
    screenshot = _save_screenshot(
        args.vmid, rgb, width, height, args.out, state_root=lab.STATE_ROOT
    )
    grid_step = 100
    guided, temporal = _model_frame(
        lab, args.lease, args.vmid, rgb, width, height
    )
    gridded = png_module.overlay_coordinate_grid(
        width, height, guided, step=grid_step
    )
    original_path = Path(screenshot["path"])
    grid_path = original_path.with_name(
        original_path.stem + "-grid" + original_path.suffix
    )
    grid_png = png_module.encode_png(width, height, gridded)
    grid_path.write_bytes(grid_png)
    model_input = {
        "path": str(grid_path),
        "bytes": len(grid_png),
        "width": width,
        "height": height,
        "grid_step": grid_step,
        "origin": "top-left",
        "x_direction": "right",
        "y_direction": "down",
        "temporal": temporal,
    }
    grid_prompt = (args.prompt or vision.DEFAULT_PROMPT) + (
        "\nA coordinate grid is overlaid every 100 pixels. The labels are "
        "original framebuffer coordinates: origin top-left, X increases "
        "right, Y increases down. Use the grid to estimate control centers."
    )
    try:
        analysis = vision.analyze_png(
            lab.CONFIG, grid_png, width=width, height=height, prompt=grid_prompt,
            timeout=args.timeout, max_tokens=args.max_tokens,
            provider=args.provider,
        )
    except (vision.VisionError, secrets_store.SecretError) as exc:
        lab.audit(
            "console-vision-inspect-failed", lease=args.lease, vmid=args.vmid,
            error=str(exc)[:200], provider=args.provider or "auto", sync=False,
        )
        raise _api_error(lab, str(exc)) from None
    lab.audit(
        "console-vision-inspect", lease=args.lease, vmid=args.vmid,
        provider=analysis["provider"], model=analysis["model"], sync=False,
    )
    destination = (
        "integrate.api.nvidia.com"
        if analysis["provider"] == "nvidia"
        else "openrouter.ai"
    )
    print(json.dumps({
        "vmid": args.vmid,
        "screenshot": screenshot,
        "model_input": model_input,
        "transmitted_to": destination,
        "vision": analysis,
    }, indent=2, sort_keys=True))


def cmd_keys(lab: Any, args: Any) -> None:
    api = lab.ProxmoxAPI()
    _require_owned_qemu(lab, args.lease, args.vmid)
    combos = args.keys
    screenshot = None
    if args.via == "api":
        for combo in combos:
            api.call(
                "PUT", f"/nodes/{lab.NODE}/qemu/{args.vmid}/sendkey", {"key": combo}
            )
            time.sleep(args.delay)
        screenshot = _capture_after_action(lab, api, args)
    else:
        with VncSession(lab, api, args.vmid) as session:
            for combo in combos:
                modifiers, keysym = rfb.parse_key_combo(combo)
                session.client.tap(keysym, modifiers)
                time.sleep(args.delay)
            screenshot = _capture_after_action(lab, api, args, session)
    lab.audit("console-keys", lease=args.lease, vmid=args.vmid,
              count=len(combos), via=args.via, sync=False)
    result = {"vmid": args.vmid, "keys_sent": len(combos), "via": args.via}
    if screenshot is not None:
        result["screenshot_after"] = screenshot
    print(json.dumps(result, indent=2, sort_keys=True))


def cmd_type(lab: Any, args: Any) -> None:
    api = lab.ProxmoxAPI()
    _require_owned_qemu(lab, args.lease, args.vmid)
    text = args.text
    if args.text_stdin:
        import sys
        text = sys.stdin.read()
    if text is None:
        raise _api_error(lab, "provide --text or --text-stdin")
    with VncSession(lab, api, args.vmid) as session:
        sent = session.client.type_text(text, delay=args.delay)
        if args.enter:
            session.client.tap(rfb.KEYSYMS["enter"])
        screenshot = _capture_after_action(lab, api, args, session)
    # The text itself is never audited: it may contain a password.
    lab.audit("console-type", lease=args.lease, vmid=args.vmid,
              characters=sent, sync=False)
    result = {"vmid": args.vmid, "characters_sent": sent}
    if screenshot is not None:
        result["screenshot_after"] = screenshot
    print(json.dumps(result, indent=2, sort_keys=True))


def cmd_click(lab: Any, args: Any) -> None:
    api = lab.ProxmoxAPI()
    _require_owned_qemu(lab, args.lease, args.vmid)
    empty_space = args.empty_space
    target = str(getattr(args, "target", "") or "").strip()
    if empty_space:
        if target:
            raise _api_error(lab, "--empty-space cannot be combined with --target")
    else:
        if len(target) < 2:
            raise _api_error(
                lab, "--target must describe the visible control in at least 2 characters"
            )
        if len(target) > 80 or any(ord(char) < 32 for char in target):
            raise _api_error(
                lab, "--target must be a single printable label of at most 80 characters"
            )
    with VncSession(lab, api, args.vmid) as session:
        if not (0 <= args.x < session.client.width
                and 0 <= args.y < session.client.height):
            raise _api_error(
                lab,
                f"({args.x},{args.y}) is outside the "
                f"{session.client.width}x{session.client.height} screen",
            )
        if empty_space:
            session.client.click(args.x, args.y, button=args.button, double=args.double)
            screenshot = _capture_after_action(lab, api, args, session)
        else:
            width, height = session.client.width, session.client.height
            target_json = json.dumps(target, ensure_ascii=False)
            session.client.pointer(args.x, args.y, 0)
            rgb = session.client.capture(timeout=25.0, settle=args.calibration_settle)
            checkpoint = _save_screenshot(
                args.vmid, rgb, width, height, getattr(args, "screenshot_out", None),
                state_root=lab.STATE_ROOT,
            )
            guided, temporal = _model_frame(
                lab, args.lease, args.vmid, rgb, width, height
            )
            gridded = png_module.overlay_coordinate_grid(
                width, height, guided, step=100
            )
            grid_png = png_module.encode_png(width, height, gridded)
            prompt = f"""Verify one cursor checkpoint. Return only JSON:
{{
  "screen": "short checkpoint name",
  "summary": "what is visibly happening",
  "controls": [{{"label": {target_json}, "bbox": [x0, y0, x1, y1], "confidence": 0.0}}],
  "recommended_action": {{"kind": "click", "value": "{args.x},{args.y}", "reason": "cursor visibly overlaps the named control"}},
  "expected_change": "the named control opens",
  "warnings": []
}}
The harness has already moved the visible cursor to ({args.x},{args.y}) and will
click exactly there. Locate the one control named {target!r} in the image and
report its bounding box as "bbox": [x0, y0, x1, y1] in framebuffer pixels
(origin top-left, x increases right, y increases down, x0 < x1 and y0 < y1);
the bbox must cover the visible control body, not a single guessed point. Then
decide only whether the cursor visibly overlaps that control's body: if it
does, recommended_action is kind=click with value "{args.x},{args.y}"; if it
does not overlap, is ambiguous, or the named control is absent, return
controls=[] and recommended_action kind=stop. Never infer overlap from the
supplied coordinates alone; judge from the image."""
            try:
                analysis = vision.analyze_png(
                    lab.CONFIG, grid_png, width=width, height=height, prompt=prompt,
                    timeout=args.vision_timeout, provider=args.provider,
                )
            except (vision.VisionError, secrets_store.SecretError) as exc:
                raise _api_error(
                    lab, f"click blocked: vision checkpoint failed: {exc}"
                ) from None
            verified, reason = vision.verifies_target(
                analysis, target, args.x, args.y
            )
            lab.audit(
                "console-click-calibration", lease=args.lease, vmid=args.vmid,
                width=width, height=height, target=target, verified=verified,
                provider=analysis.get("provider"), sync=False,
            )
            if not verified:
                print(json.dumps({
                    "vmid": args.vmid, "clicked": False, "target": target,
                    "cursor_moved_to": [args.x, args.y], "checkpoint": checkpoint,
                    "temporal": temporal,
                    "verification": {"accepted": False, "reason": reason},
                    "next_step": "Stop. Take a fresh inspection; do not retry or reboot.",
                }, indent=2, sort_keys=True))
                return
            session.client.click(args.x, args.y, button=args.button, double=args.double)
            screenshot = _capture_after_action(lab, api, args, session)
    if empty_space:
        lab.audit(
            "console-click-unverified", lease=args.lease, vmid=args.vmid,
            x=args.x, y=args.y, button=args.button, sync=False,
        )
        result = {
            "vmid": args.vmid, "clicked": [args.x, args.y], "empty_space": True,
            "verification": {
                "accepted": True,
                "reason": "explicit empty-space opt-out; coordinate unverified",
            },
        }
        if screenshot is not None:
            result["screenshot_after"] = screenshot
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    lab.audit("console-click", lease=args.lease, vmid=args.vmid,
              x=args.x, y=args.y, button=args.button, sync=False)
    result = {
        "vmid": args.vmid, "clicked": [args.x, args.y], "target": target,
        "verification": {"accepted": True, "reason": reason},
        "temporal": temporal,
    }
    control = vision.matched_control(analysis, target)
    if control is not None and isinstance(control.get("bbox"), list):
        result["control_bbox"] = control["bbox"]
    if screenshot is not None:
        result["screenshot_after"] = screenshot
    print(json.dumps(result, indent=2, sort_keys=True))


def cmd_has_gui_locked_up(lab: Any, args: Any) -> None:
    """Best-effort GUI liveness probe: moves the pointer, checks for change.

    This client declares no support for RFB's Cursor pseudo-encoding (see
    `_set_encodings`), so a compliant server -- QEMU's among them -- falls
    back to drawing the pointer into the framebuffer itself rather than
    handing it to the client to composite. `console click`'s own vision
    verification already depends on this: it moves the cursor and expects
    vision to see it overlapping a control. A real pointer move should
    therefore be visible here too.

    Two probes to two different points guard against an unlucky move that
    coincidentally lands where the cursor already was. A screen that never
    changes despite both is good evidence of a hang, but not proof: an app
    that paints no hover/focus feedback would look the same. The verdict
    and the raw per-probe pixel deltas are both reported so a caller can
    judge for itself rather than trust a bare bool.
    """
    api = lab.ProxmoxAPI()
    lab.load_lease(args.lease)
    with VncSession(lab, api, args.vmid) as session:
        width, height = session.client.width, session.client.height
        probes = [(width // 4, height // 4), (3 * width // 4, 3 * height // 4)]
        previous = session.client.capture(timeout=args.timeout, settle=args.settle)
        deltas: list[int] = []
        for x, y in probes:
            session.client.pointer(x, y)
            time.sleep(args.settle)
            current = session.client.capture(timeout=args.timeout, settle=0)
            _, changed = png_module.highlight_changes(
                width, height, current, previous, threshold=args.threshold
            )
            deltas.append(changed)
            previous = current
    locked_up = all(delta == 0 for delta in deltas)
    lab.audit("console-has-gui-locked-up", lease=args.lease, vmid=args.vmid,
              locked_up=locked_up, sync=False)
    result: dict[str, Any] = {
        "vmid": args.vmid,
        "locked_up": locked_up,
        "probe_points": probes,
        "changed_pixels_per_probe": deltas,
    }
    if locked_up:
        result["caveat"] = (
            "no pixels changed after either pointer move -- likely a hang, "
            "but an app painting no hover/focus feedback would look the "
            "same; treat this as one signal, not certain proof"
        )
    print(json.dumps(result, indent=2, sort_keys=True))


def cmd_has_terminal_locked_up(lab: Any, args: Any) -> None:
    """Best-effort text-console liveness probe: samples for a while, checks
    for any change at all.

    A live text console's cursor normally blinks on its own, so this sends
    no input -- it just watches. Refuses a screen `console screenshot`
    would not call text-mode, since an idle GUI with nothing blinking would
    look identical to a hang here. A static result over the sampling window
    is good evidence of a freeze, but not proof: some consoles run with
    cursor blink disabled and would look the same either way.
    """
    if args.samples < 2:
        raise lab.LabError("--samples must be at least 2")
    api = lab.ProxmoxAPI()
    frames: list[bytes] = []
    with VncSession(lab, api, args.vmid) as session:
        width, height = session.client.width, session.client.height
        for index in range(args.samples):
            frames.append(session.client.capture(timeout=args.timeout, settle=0))
            if index < args.samples - 1:
                time.sleep(args.interval)
    analysis = textmode.analyse(frames[0], width, height)
    if not analysis["looks_like_text_console"]:
        raise lab.LabError(
            "screen is not a text console; use 'console has-gui-locked-up' instead"
        )
    deltas = [
        png_module.highlight_changes(width, height, current, previous,
                                     threshold=args.threshold)[1]
        for previous, current in zip(frames, frames[1:])
    ]
    locked_up = all(delta == 0 for delta in deltas)
    result: dict[str, Any] = {
        "vmid": args.vmid,
        "locked_up": locked_up,
        "samples": args.samples,
        "interval_seconds": args.interval,
        "changed_pixels_per_sample": deltas,
    }
    if locked_up:
        result["caveat"] = (
            "no pixels changed across the sampling window -- likely "
            "frozen, but some consoles run with cursor blink disabled and "
            "would look the same; treat this as one signal, not certain proof"
        )
    print(json.dumps(result, indent=2, sort_keys=True))


def _bridge_send_all(client: Any, data: bytes) -> bool:
    """Send all bytes to a non-blocking client; False when the client is gone."""
    import select

    while data:
        try:
            sent = client.send(data)
            data = data[sent:]
        except BlockingIOError:
            select.select([], [client], [], 0.2)
        except OSError:
            return False
    return True


def _bridge_serve(lab: Any, api: Any, kind: str, vmid: int,
                  client: Any) -> None:
    """Pipe one TCP client to the guest serial and back.

    Guest output is raw terminal bytes; client bytes are re-framed as Proxmox
    terminal input (`0:<len>:<data>`). One client at a time; the listener
    accepts the next after this one disconnects.
    """
    import select

    with TermSession(lab, api, kind, vmid) as term:
        # Transport records are filtered by the session itself, so a debugger
        # on the other end of this socket sees exactly what the guest sent --
        # the same stream 'console text' prints.
        client.setblocking(False)
        while True:
            data = term.read_bytes(0.2)
            if data and not _bridge_send_all(client, data):
                return
            readable, _, _ = select.select([client], [], [], 0.2)
            if not readable:
                continue
            try:
                chunk = client.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            term.socket.send(
                b"0:" + str(len(chunk)).encode() + b":" + chunk
            )


def cmd_bridge(lab: Any, args: Any) -> None:
    """Expose a guest serial console as a local TCP port for debuggers."""
    import socket

    api = lab.ProxmoxAPI()
    lease = lab.load_lease(args.lease)
    owned = any(
        item.get("kind") == args.kind and int(item.get("vmid", -1)) == args.vmid
        for item in lease.get("resources", [])
    )
    if not owned:
        raise _api_error(
            lab, f"VMID {args.vmid} is not a {args.kind} guest registered to "
            "this lease"
        )
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((args.host, args.port))
    listener.listen(1)
    port = listener.getsockname()[1]
    lab.audit("console-bridge", lease=args.lease, vmid=args.vmid,
              kind=args.kind, port=port, sync=False)
    print(
        f"bridge ready: {args.host}:{port} -> {args.kind}/{args.vmid} serial. "
        f"Connect with e.g. 'nc {args.host} {port}' (or a kernel debugger such "
        "as rosdbg). Ctrl+C to stop.",
        flush=True,
    )
    try:
        while True:
            client, _ = listener.accept()
            try:
                _bridge_serve(lab, api, args.kind, args.vmid, client)
            finally:
                client.close()
    finally:
        listener.close()


# Answers from a failed attach that mean "not yet" rather than "misconfigured
# guest" or "real API failure".
TERM_NOT_READY_MARKERS = (
    "not running",
    "no such file or directory",
    "failed to connect to",
    "connection refused",
)


def _term_not_ready(error: Exception) -> bool:
    message = str(error).lower()
    return any(marker in message for marker in TERM_NOT_READY_MARKERS)


def _guest_is_running(lab: Any, api: Any, kind: str, vmid: int) -> bool:
    """Whether the guest is running, per the API rather than the byte stream.

    This has to be asked explicitly. Proxmox issues a termproxy ticket for a
    *stopped* guest and lets the websocket open; only then does 'qm terminal'
    write "VM <id> not running" into the stream and exit. So a successful
    attach proves nothing about the guest, and a capture started too early
    used to record that sentence where boot output should have been.
    """
    try:
        return lab.guest_status(api, kind, vmid) == "running"
    except lab.LabError:
        return False


def _attach_term(lab: Any, api: Any, kind: str, vmid: int, *,
                 wait: float = 0.0, poll: float = 0.5) -> TermSession:
    """Open a terminal session, optionally waiting for the guest to start.

    The capture order that preserves boot output is attach first, power on
    second. That was not executable: the terminal only carries guest output
    once the guest is running, and attaching earlier produced a log containing
    one transport sentence and nothing else. Waiting here makes the documented
    order work -- the session is created as soon as the guest is up.

    This narrows the gap to one poll interval; it does not close it. Only
    'console text --from-reset' (or a bridge held open across a reset)
    guarantees output from t=0, because the QEMU process and its serial socket
    survive a reset.
    """
    deadline = time.monotonic() + max(0.0, wait)
    while True:
        if _guest_is_running(lab, api, kind, vmid):
            try:
                return TermSession(lab, api, kind, vmid)
            except lab.LabError as exc:
                if wait <= 0 or not _term_not_ready(exc):
                    raise
        elif wait <= 0:
            raise _api_error(
                lab,
                f"{kind}/{vmid} is not running, so its terminal carries no "
                "guest output. Start the guest first, or pass "
                "--wait-for-guest SECONDS to attach as soon as it starts.",
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _api_error(
                lab,
                f"the serial terminal for {kind}/{vmid} did not become "
                f"available within {wait:g}s: the guest is still not running",
            )
        time.sleep(min(poll, remaining))


def cmd_text(lab: Any, args: Any) -> None:
    """Read the real terminal stream -- exact text, no OCR involved."""
    api = lab.ProxmoxAPI()
    wait = float(getattr(args, "wait_for_guest", 0.0) or 0.0)
    # A stopped guest still answers status/current, so the kind is resolvable
    # before power-on; only the terminal itself has to be waited for.
    kind = args.kind or _kind_of(lab, api, args.vmid)
    if args.from_reset:
        # Attach the terminal session BEFORE resetting: the QEMU serial
        # chardev streams only to a connected client, so resetting first
        # loses the earliest boot output. A reset keeps the QEMU process
        # (and its serial socket) alive; only the guest restarts.
        if not args.follow:
            raise _api_error(lab, "--from-reset requires --follow")
        if kind != "qemu":
            raise _api_error(lab, "--from-reset only applies to QEMU guests")
        if not args.lease:
            raise _api_error(lab, "--from-reset requires --lease")
        lab.require_lease_resource(
            lab.load_lease(args.lease), kind, args.vmid
        )
    if args.follow:
        # Continuous capture (kernel boot logs, panic traces): read until the
        # timeout or Ctrl+C, printing each chunk as it arrives.
        import time as _time

        deadline = _time.monotonic() + (args.timeout if args.timeout else 3600)
        with _attach_term(lab, api, kind, args.vmid, wait=wait) as session:
            if args.from_reset:
                api.call(
                    "POST", f"/nodes/{lab.NODE}/qemu/{args.vmid}/status/reset"
                )
                lab.audit("console-text-from-reset", lease=args.lease,
                          vmid=args.vmid, sync=False)
            while _time.monotonic() < deadline:
                chunk = session.read_bytes(1.0)
                if chunk:
                    print(chunk.decode("utf-8", "replace"), end="", flush=True)
            tail = session.flush_bytes()
            if tail:
                print(tail.decode("utf-8", "replace"), end="", flush=True)
        return
    with _attach_term(lab, api, kind, args.vmid, wait=wait) as session:
        if args.send or args.send_raw or args.nudge:
            # Sending even a blank line mutates the guest console.
            if not args.lease:
                raise _api_error(
                    lab, "--send, --send-raw and --nudge require --lease"
                )
            lab.require_lease_resource(
                lab.load_lease(args.lease), kind, args.vmid
            )
            if args.send:
                session.send_line(args.send)
            elif args.send_raw:
                session.send_raw(args.send_raw)
            else:
                session.send_line("")
        output = session.read(args.seconds)
    print(json.dumps(
        {"vmid": args.vmid, "kind": kind, "text": textmode.strip_ansi(output)},
        indent=2,
    ))


def cmd_exec(lab: Any, args: Any) -> None:
    api = lab.ProxmoxAPI()
    _require_owned_qemu(lab, args.lease, args.vmid)
    command = args.command
    if args.shell:
        command = ["/bin/sh", "-c", " ".join(command)] if not args.windows else [
            "cmd.exe", "/c", " ".join(command)
        ]
    result = agent_exec(lab, api, args.vmid, command, timeout=args.timeout)
    lab.audit("guest-exec", lease=args.lease, vmid=args.vmid,
              argv0=command[0], exitcode=result["exitcode"], sync=False)
    print(json.dumps(result, indent=2))


def _fetch_command(url: str, dest: str, windows: bool) -> list[str]:
    if windows:
        script = (
            "$ProgressPreference='SilentlyContinue'; "
            f"Invoke-WebRequest -UseBasicParsing -Uri '{url}' "
            f"-OutFile '{dest}'"
        )
        return ["powershell.exe", "-NoProfile", "-Command", script]
    return [
        "/bin/sh", "-c",
        f"curl -fsSL -A proxmox-agent-lab -o {shlex.quote(dest)} {shlex.quote(url)}",
    ]


def _upload_command(url: str, source: str, windows: bool) -> list[str]:
    if windows:
        script = (
            "$ProgressPreference='SilentlyContinue'; "
            f"Invoke-WebRequest -UseBasicParsing -Method Put -Uri '{url}' "
            f"-InFile '{source}'"
        )
        return ["powershell.exe", "-NoProfile", "-Command", script]
    return [
        "/bin/sh", "-c",
        f"curl -fsS -A proxmox-agent-lab -X PUT --data-binary "
        f"@{shlex.quote(source)} {shlex.quote(url)}",
    ]


def _fetch_parts_command(urls: list[str], dest: str) -> list[str]:
    """Download and reassemble chunk parts in the guest, printing the hash."""
    steps = " && ".join(
        f"curl -fsSL -A proxmox-agent-lab -o "
        f"{shlex.quote(f'/tmp/pp-{i:04d}')} {shlex.quote(url)}"
        for i, url in enumerate(urls)
    )
    script = (
        f"rm -f {shlex.quote(dest)} /tmp/pp-*; {steps} "
        f"&& cat /tmp/pp-* > {shlex.quote(dest)} && rm -f /tmp/pp-* "
        f"&& sha256sum {shlex.quote(dest)} | cut -d' ' -f1"
    )
    return ["/bin/sh", "-c", script]


def _upload_parts_command(urls: list[str], source: str, chunk: int) -> list[str]:
    """Split a guest file into chunks, upload each, then report its hash."""
    steps = " && ".join(
        f"curl -fsS -A proxmox-agent-lab -X PUT --data-binary "
        f"@{shlex.quote(f'/tmp/pp-{i:04d}')} {shlex.quote(url)}"
        for i, url in enumerate(urls)
    )
    script = (
        f"rm -f /tmp/pp-*; split -b {int(chunk)} -d -a 4 "
        f"{shlex.quote(source)} /tmp/pp- && {steps} "
        f"&& rm -f /tmp/pp-* && sha256sum {shlex.quote(source)} | cut -d' ' -f1"
    )
    return ["/bin/sh", "-c", script]


def _chunk_size_mb(args: Any) -> int:
    return max(1, getattr(args, "chunk_size", None) or CHUNK_DEFAULT_MB)


def _push_chunked(lab: Any, api: Any, args: Any, source: Path,
                  payload: bytes, name: str) -> dict[str, Any]:
    chunk = _chunk_size_mb(args) * 1024 * 1024
    parts = [payload[i:i + chunk] for i in range(0, len(payload), chunk)]
    if len(parts) > MAX_CHUNK_PARTS:
        raise _api_error(
            lab,
            f"{name} needs {len(parts)} parts (max {MAX_CHUNK_PARTS}); "
            "raise --chunk-size",
        )
    base = args.key or f"push/{secrets.token_hex(6)}/{name}"
    keys = [f"{base}/part-{i:04d}" for i in range(len(parts))]
    for key, part in zip(keys, parts):
        s3.put_bytes(key, part)
    urls = [s3.presign(key, expires=args.url_expiry) for key in keys]
    dest = args.dest or f"/tmp/{name}"
    run = agent_exec(
        lab, api, args.vmid, _fetch_parts_command(urls, dest),
        timeout=args.timeout,
    )
    if run["exitcode"] not in (0, None):
        raise _api_error(lab, f"guest fetch failed: {run['stderr'][:400]}")
    guest_sha = run.get("stdout", "").strip()
    if args.sha256 and guest_sha != args.sha256:
        raise _api_error(
            lab, f"sha256 mismatch on guest: {guest_sha} != {args.sha256}"
        )
    return {
        "vmid": args.vmid, "s3_key": base, "bytes": len(payload),
        "parts": len(parts), "dest": dest, "chunked": True,
        "guest_sha256": guest_sha or None,
    }


def _pull_chunked(lab: Any, api: Any, args: Any, name: str) -> dict[str, Any]:
    import hashlib
    import math

    chunk = _chunk_size_mb(args) * 1024 * 1024
    base = args.key or f"pull/{args.vmid}/{name}"
    out = Path(args.out).expanduser() if args.out else Path(name)
    if args.sha256 and out.is_file():
        if hashlib.sha256(out.read_bytes()).hexdigest() == args.sha256:
            return {
                "vmid": args.vmid, "path": str(out),
                "bytes": out.stat().st_size, "sha256": args.sha256,
                "s3_key": base, "already_verified": True, "chunked": True,
            }
    size_run = agent_exec(
        lab, api, args.vmid,
        ["/bin/sh", "-c", f"stat -c %s {shlex.quote(args.remote)}"],
        timeout=args.timeout,
    )
    try:
        size = int(size_run.get("stdout", "").strip())
    except ValueError:
        raise _api_error(
            lab,
            f"cannot read size of {args.remote}: "
            f"{size_run.get('stderr', '')[:200]}",
        ) from None
    n_parts = max(1, math.ceil(size / chunk))
    if n_parts > MAX_CHUNK_PARTS:
        raise _api_error(
            lab,
            f"{name} needs {n_parts} parts (max {MAX_CHUNK_PARTS}); "
            "raise --chunk-size",
        )
    keys = [f"{base}/part-{i:04d}" for i in range(n_parts)]
    # Drop stale parts from an earlier interrupted attempt with the same key.
    for obj in s3.list_objects(base):
        key = str(obj.get("key", ""))
        if key.startswith(base + "/"):
            s3.delete_object(key)
    urls = [s3.presign(key, method="PUT", expires=args.url_expiry)
            for key in keys]
    run = agent_exec(
        lab, api, args.vmid,
        _upload_parts_command(urls, args.remote, chunk),
        timeout=args.timeout,
    )
    if run["exitcode"] not in (0, None):
        raise _api_error(lab, f"guest upload failed: {run['stderr'][:400]}")
    guest_sha = run.get("stdout", "").strip()
    payload = b"".join(s3.get_bytes(key) for key in keys)
    sha = hashlib.sha256(payload).hexdigest()
    if guest_sha and sha != guest_sha:
        raise _api_error(
            lab, f"sha256 mismatch: assembled {sha} != guest {guest_sha}"
        )
    if args.sha256 and sha != args.sha256:
        raise _api_error(
            lab, f"sha256 mismatch: {sha} != expected {args.sha256}"
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(payload)
    if not args.keep:
        for key in keys:
            s3.delete_object(key)
    return {
        "vmid": args.vmid, "path": str(out), "bytes": len(payload),
        "sha256": sha, "parts": n_parts, "s3_key": base, "chunked": True,
    }


def cmd_push(lab: Any, args: Any) -> None:
    """Copy a local file into a guest via the S3 scratch bucket."""
    api = lab.ProxmoxAPI()
    _require_owned_qemu(lab, args.lease, args.vmid)
    source = Path(args.file).expanduser().resolve()
    if not source.is_file():
        raise _api_error(lab, f"not a regular file: {source}")
    payload = source.read_bytes()
    chunked = (
        not args.windows and not args.url_only
        and len(payload) > SINGLE_OBJECT_MAX_MB * 1024 * 1024
    )
    if chunked:
        result = _push_chunked(lab, api, args, source, payload, source.name)
        lab.audit("guest-push", lease=args.lease, vmid=args.vmid,
                  s3_key=result["s3_key"], bytes=len(payload),
                  parts=result["parts"], chunked=True, sync=False)
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    key = args.key or f"push/{secrets.token_hex(6)}/{source.name}"
    s3.put_bytes(key, payload)
    url = s3.presign(key, expires=args.url_expiry)
    dest = args.dest or (
        f"C:\\Windows\\Temp\\{source.name}" if args.windows else f"/tmp/{source.name}"
    )
    result: dict[str, Any] = {
        "vmid": args.vmid,
        "s3_key": key,
        "bytes": len(payload),
        "dest": dest,
    }
    if args.url_only:
        result["fetch_url"] = url
        result["hint"] = "run the fetch inside the guest yourself"
    else:
        run = agent_exec(
            lab, api, args.vmid, _fetch_command(url, dest, args.windows),
            timeout=args.timeout,
        )
        result["guest"] = run
        if run["exitcode"] not in (0, None):
            raise _api_error(lab, f"guest fetch failed: {run['stderr'][:400]}")
    lab.audit("guest-push", lease=args.lease, vmid=args.vmid, s3_key=key,
              bytes=len(payload), dest=dest, sync=False)
    print(json.dumps(result, indent=2, sort_keys=True))


def cmd_pull(lab: Any, args: Any) -> None:
    """Copy a file out of a guest via the S3 scratch bucket."""
    import hashlib

    api = lab.ProxmoxAPI()
    _require_owned_qemu(lab, args.lease, args.vmid)
    name = Path(args.remote).name
    out = Path(args.out).expanduser() if args.out else Path(name)
    # Resume: when the local file already matches the expected hash there is
    # nothing to do, so make no guest or S3 traffic at all.
    if args.sha256 and out.is_file():
        if hashlib.sha256(out.read_bytes()).hexdigest() == args.sha256:
            lab.audit("guest-pull", lease=args.lease, vmid=args.vmid,
                      bytes=out.stat().st_size, sha256=args.sha256,
                      already_verified=True, sync=False)
            print(json.dumps({
                "vmid": args.vmid, "path": str(out),
                "bytes": out.stat().st_size, "sha256": args.sha256,
                "already_verified": True,
            }, indent=2, sort_keys=True))
            return
    if not args.windows:
        probe = agent_exec(
            lab, api, args.vmid,
            ["/bin/sh", "-c", f"stat -c %s {shlex.quote(args.remote)}"],
            timeout=args.timeout,
        )
        try:
            remote_size = int(probe.get("stdout", "").strip())
        except ValueError:
            remote_size = 0
        if remote_size > SINGLE_OBJECT_MAX_MB * 1024 * 1024:
            result = _pull_chunked(lab, api, args, name)
            lab.audit("guest-pull", lease=args.lease, vmid=args.vmid,
                      s3_key=result.get("s3_key", ""), bytes=result["bytes"],
                      parts=result.get("parts"), chunked=True, sync=False)
            print(json.dumps(result, indent=2, sort_keys=True))
            return
    key = args.key or f"pull/{secrets.token_hex(6)}/{Path(args.remote).name}"
    url = s3.presign(key, method="PUT", expires=args.url_expiry)
    run = agent_exec(
        lab, api, args.vmid, _upload_command(url, args.remote, args.windows),
        timeout=args.timeout,
    )
    if run["exitcode"] not in (0, None):
        raise _api_error(lab, f"guest upload failed: {run['stderr'][:400]}")
    payload = s3.get_bytes(key)
    target = Path(args.out).expanduser() if args.out else Path(Path(args.remote).name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    if not args.keep:
        s3.delete_object(key)
    lab.audit("guest-pull", lease=args.lease, vmid=args.vmid, s3_key=key,
              bytes=len(payload), sync=False)
    print(json.dumps(
        {"vmid": args.vmid, "path": str(target), "bytes": len(payload)}, indent=2
    ))


def cmd_s3(lab: Any, args: Any) -> None:
    if args.s3_command == "health":
        print(json.dumps(s3.health(), indent=2, sort_keys=True))
    elif args.s3_command == "list":
        print(json.dumps(s3.list_objects(args.prefix), indent=2, sort_keys=True))
    elif args.s3_command == "put":
        source = Path(args.file).expanduser().resolve()
        key = args.key or f"upload/{secrets.token_hex(6)}/{source.name}"
        s3.put_bytes(key, source.read_bytes())
        print(json.dumps({"key": key, "bytes": source.stat().st_size}, indent=2))
    elif args.s3_command == "get":
        payload = s3.get_bytes(args.key)
        target = Path(args.out).expanduser() if args.out else Path(Path(args.key).name)
        target.write_bytes(payload)
        print(json.dumps({"path": str(target), "bytes": len(payload)}, indent=2))
    elif args.s3_command == "presign":
        print(json.dumps(
            {"url": s3.presign(args.key, method=args.method,
                                   expires=args.expires)},
            indent=2,
        ))
    elif args.s3_command == "delete":
        s3.delete_object(args.key)
        print(json.dumps({"deleted": args.key}))


def cmd_preflight(lab: Any, args: Any) -> None:
    """Report whether the API token can actually drive consoles and agents."""
    api = lab.ProxmoxAPI()
    permissions = api.call("GET", "/access/permissions")
    node_scope = {}
    if isinstance(permissions, dict):
        for path in (f"/nodes/{lab.NODE}", "/vms", "/"):
            node_scope.update(permissions.get(path, {}) or {})
    # Proxmox 9 split the old VM.Monitor privilege into granular
    # VM.GuestAgent.* ones. Accept either, so this reports the truth on both
    # PVE 8 and PVE 9 rather than a privilege that no longer exists.
    needed = {
        "VNC screenshots, keyboard, pointer, serial terminal": ("VM.Console",),
        "qemu-guest-agent exec, push and pull": (
            "VM.GuestAgent.Unrestricted", "VM.Monitor",
        ),
        "writing files into guests": (
            "VM.GuestAgent.FileWrite", "VM.GuestAgent.Unrestricted", "VM.Monitor",
        ),
        "attaching install media": ("VM.Config.Disk",),
        "start and stop": ("VM.PowerMgmt",),
    }
    present = {
        purpose: any(node_scope.get(name) for name in names)
        for purpose, names in needed.items()
    }
    missing = [
        f"{purpose} (need one of: {', '.join(names)})"
        for purpose, names in needed.items()
        if not present[purpose]
    ]
    s3_state: dict[str, Any]
    try:
        s3_state = s3.health()
    except s3.S3Error as exc:
        s3_state = {"reachable": False, "error": str(exc)[:300]}
    print(json.dumps(
        {
            "capabilities": present,
            "missing": missing,
            "granted_privileges": sorted(
                name for name, value in node_scope.items() if value
            ),
            "s3": s3_state,
            "font_table_installed": textmode.font_table_path().exists(),
        },
        indent=2,
        sort_keys=True,
    ))


def register(sub: Any, lab: Any) -> None:
    """Attach the console, transfer and S3 subcommands to the main parser."""

    def bind(handler: Any) -> Any:
        return lambda args: handler(lab, args)

    def add_after_screenshot(parser: Any) -> None:
        parser.add_argument(
            "--screenshot-after", type=float, metavar="SECONDS",
            help="after input, wait this long and include a PNG in the result",
        )
        parser.add_argument(
            "--screenshot-out", "--out", dest="screenshot_out",
            help="path for --screenshot-after (default: state screens directory)",
        )

    console = sub.add_parser("console", help="VNC, terminal and guest access")
    console_sub = console.add_subparsers(dest="console_command", required=True)

    shot = console_sub.add_parser("screenshot", help="capture the screen as PNG")
    shot.add_argument("--vmid", type=int, required=True)
    shot.add_argument("--out")
    shot.add_argument("--settle", type=float, default=0.0,
                      help="seconds to wait before capturing")
    shot.add_argument("--timeout", type=float, default=25.0)
    shot.add_argument("--upload", action="store_true",
                      help="also store the PNG in the S3 scratch bucket")
    shot.add_argument("--url-expiry", type=int, default=3600)
    shot.add_argument("--ocr", action="store_true",
                      help="decode text-mode screens; refused on graphical screens")
    shot.add_argument(
        "--via", choices=("vnc", "monitor"), default="vnc",
        help="capture path: 'vnc' (default) reads pixels over the console; "
             "'monitor' uses QEMU screendump on the host, which writes a "
             "lease-scoped temporary PNG there and needs --lease plus the "
             "opt-in [memflow] host SSH channel to fetch and delete it",
    )
    shot.add_argument("--lease", help="required with --via monitor")
    shot.set_defaults(func=bind(cmd_screenshot))

    burst = console_sub.add_parser(
        "screenshot-burst",
        help="capture several screenshots over time as one stitched PNG",
    )
    burst.add_argument("--vmid", type=int, required=True)
    burst.add_argument("--out")
    burst.add_argument("--count", type=int, default=6,
                       help="number of captures (default 6)")
    burst.add_argument("--interval", type=float, default=10.0,
                       help="seconds between captures (default 10)")
    burst.add_argument("--timeout", type=float, default=25.0)
    burst.add_argument("--upload", action="store_true",
                       help="also store the PNG in the S3 scratch bucket")
    burst.add_argument("--url-expiry", type=int, default=3600)
    burst.set_defaults(func=bind(cmd_screenshot_burst))

    inspect = console_sub.add_parser(
        "inspect", help="inspect one lease-owned screenshot with cloud vision"
    )
    inspect.add_argument("--lease", required=True)
    inspect.add_argument("--vmid", type=int, required=True)
    inspect.add_argument("--out")
    inspect.add_argument("--settle", type=float, default=2.0)
    inspect.add_argument("--timeout", type=int, default=120)
    inspect.add_argument("--max-tokens", type=int, default=1024)
    inspect.add_argument("--prompt")
    inspect.add_argument(
        "--provider",
        choices=("auto", "nvidia", "openrouter-nemotron", "openrouter-free"),
        default="auto",
        help="provider override; auto uses the guarded fallback chain",
    )
    inspect.set_defaults(func=bind(cmd_inspect))

    keys = console_sub.add_parser("keys", help="send key combinations")
    keys.add_argument("--lease", required=True)
    keys.add_argument("--vmid", type=int, required=True)
    keys.add_argument("keys", nargs="+", help="e.g. ctrl-alt-delete f2 enter")
    keys.add_argument("--via", choices=("vnc", "api"), default="vnc")
    keys.add_argument("--delay", type=float, default=0.08)
    add_after_screenshot(keys)
    keys.set_defaults(func=bind(cmd_keys))

    typing = console_sub.add_parser("type", help="type text at the console")
    typing.add_argument("--lease", required=True)
    typing.add_argument("--vmid", type=int, required=True)
    typing.add_argument("--text")
    typing.add_argument("--text-stdin", action="store_true",
                        help="read the text from stdin, keeping it out of argv")
    typing.add_argument("--enter", action="store_true")
    typing.add_argument("--delay", type=float, default=0.012)
    add_after_screenshot(typing)
    typing.set_defaults(func=bind(cmd_type))

    click = console_sub.add_parser("click", help="click at a pixel position")
    click.add_argument("--lease", required=True)
    click.add_argument("--vmid", type=int, required=True)
    click.add_argument("--x", type=int, required=True)
    click.add_argument("--y", type=int, required=True)
    click.add_argument("--target",
                       help="short visible label of the intended control")
    click.add_argument(
        "--empty-space", action="store_true",
        help="click a known empty coordinate without target verification",
    )
    click.add_argument("--button", type=int, choices=(1, 2, 3), default=1)
    click.add_argument("--double", action="store_true")
    click.add_argument(
        "--calibration-settle", type=float, default=1.0,
        help="seconds to settle before the cursor calibration checkpoint",
    )
    click.add_argument("--vision-timeout", type=int, default=45)
    click.add_argument(
        "--provider",
        choices=("auto", "nvidia", "openrouter-nemotron", "openrouter-free"),
        default="auto",
    )
    add_after_screenshot(click)
    click.set_defaults(func=bind(cmd_click))

    gui_lockup = console_sub.add_parser(
        "has-gui-locked-up",
        help="probe a graphical screen for a hang by moving the pointer",
    )
    gui_lockup.add_argument("--lease", required=True)
    gui_lockup.add_argument("--vmid", type=int, required=True)
    gui_lockup.add_argument("--settle", type=float, default=0.3,
                            help="seconds to wait after each pointer move")
    gui_lockup.add_argument("--timeout", type=float, default=25.0)
    gui_lockup.add_argument("--threshold", type=int, default=24,
                            help="per-channel change to count a pixel as different")
    gui_lockup.set_defaults(func=bind(cmd_has_gui_locked_up))

    terminal_lockup = console_sub.add_parser(
        "has-terminal-locked-up",
        help="probe a text console for a hang by watching for any change",
    )
    terminal_lockup.add_argument("--vmid", type=int, required=True)
    terminal_lockup.add_argument("--samples", type=int, default=4,
                                 help="number of passive captures (default 4)")
    terminal_lockup.add_argument("--interval", type=float, default=0.6,
                                 help="seconds between captures (default 0.6)")
    terminal_lockup.add_argument("--timeout", type=float, default=25.0)
    terminal_lockup.add_argument("--threshold", type=int, default=24,
                                 help="per-channel change to count a pixel as different")
    terminal_lockup.set_defaults(func=bind(cmd_has_terminal_locked_up))

    text = console_sub.add_parser(
        "text", help="read the real terminal stream (preferred over OCR)"
    )
    text.add_argument("--vmid", type=int, required=True)
    text.add_argument("--kind", choices=("qemu", "lxc"))
    text.add_argument("--seconds", type=float, default=3.0)
    text.add_argument("--timeout", type=int,
                      help="seconds to follow (default: until Ctrl+C)")
    text.add_argument("--follow", action="store_true",
                      help="stream serial output continuously (boot/panic logs)")
    text.add_argument("--send", help="send this line first, then read the reply")
    text.add_argument("--send-raw",
                      help="send exactly these characters with no trailing "
                           "newline (kernel-debugger prompts such as KDB act "
                           "on bare characters)")
    text.add_argument("--nudge", action="store_true",
                      help="send a bare newline to redraw the prompt")
    text.add_argument("--from-reset", action="store_true",
                      help="with --follow: attach the serial session first, "
                           "then reset the guest, so output from t=0 is "
                           "captured (requires --lease; QEMU only)")
    text.add_argument(
        "--wait-for-guest", type=float, default=0.0, metavar="SECONDS",
        help="wait up to this long for the guest's serial terminal to exist, "
             "so a capture can be started before the guest is powered on",
    )
    text.add_argument("--lease")
    text.set_defaults(func=bind(cmd_text))

    bridge = console_sub.add_parser(
        "bridge",
        help="expose a guest serial console on a local TCP port (debuggers)",
        description="Bidirectional pipe between a local TCP port and the "
                    "guest serial console: bytes you type reach the guest "
                    "(e.g. a KDB prompt), and guest output streams back. "
                    "Tip: 'reset' restarts only the guest -- the QEMU "
                    "process and its serial socket stay alive, so a "
                    "connected bridge survives resets and captures output "
                    "from t=0. A stop/start replaces the QEMU process and "
                    "drops the bridge.",
    )
    bridge.add_argument("--lease", required=True)
    bridge.add_argument("--vmid", type=int, required=True)
    bridge.add_argument("--kind", choices=("qemu", "lxc"), default="qemu")
    bridge.add_argument("--host", default="127.0.0.1")
    bridge.add_argument("--port", type=int, default=0,
                        help="local TCP port (0 = pick a free one)")
    bridge.set_defaults(func=bind(cmd_bridge))

    execute = console_sub.add_parser("exec", help="run a command via guest agent")
    execute.add_argument("--lease", required=True)
    execute.add_argument("--vmid", type=int, required=True)
    execute.add_argument("--shell", action="store_true")
    execute.add_argument("--windows", action="store_true")
    execute.add_argument("--timeout", type=int, default=300)
    execute.add_argument("command", nargs="+")
    execute.set_defaults(func=bind(cmd_exec))

    preflight = console_sub.add_parser(
        "preflight", help="check console privileges and scratch storage"
    )
    preflight.set_defaults(func=bind(cmd_preflight))

    textmode.register(console_sub, lab)

    push = sub.add_parser("push", help="copy a local file into a guest")
    push.add_argument("--lease", required=True)
    push.add_argument("--vmid", type=int, required=True)
    push.add_argument("--file", required=True)
    push.add_argument("--dest")
    push.add_argument("--key", help="explicit S3 object key")
    push.add_argument("--windows", action="store_true")
    push.add_argument("--url-only", action="store_true",
                      help="print a presigned URL instead of using the guest agent")
    push.add_argument("--url-expiry", type=int, default=3600)
    push.add_argument("--timeout", type=int, default=600)
    push.add_argument("--chunk-size", type=int, metavar="MB", default=CHUNK_DEFAULT_MB,
                      help="part size for large-file transfers (default 64)")
    push.add_argument("--sha256",
                      help="expected SHA-256 of the file; verified on the guest")
    push.set_defaults(func=bind(cmd_push))

    pull = sub.add_parser("pull", help="copy a file out of a guest")
    pull.add_argument("--lease", required=True)
    pull.add_argument("--vmid", type=int, required=True)
    pull.add_argument("--remote", required=True)
    pull.add_argument("--out")
    pull.add_argument("--key")
    pull.add_argument("--keep", action="store_true",
                      help="keep the scratch object after download")
    pull.add_argument("--windows", action="store_true")
    pull.add_argument("--url-expiry", type=int, default=3600)
    pull.add_argument("--timeout", type=int, default=600)
    pull.add_argument("--chunk-size", type=int, metavar="MB", default=CHUNK_DEFAULT_MB,
                      help="part size for large-file transfers (default 64)")
    pull.add_argument("--sha256",
                      help="expected SHA-256; skips the transfer when the "
                           "local file already matches")
    pull.set_defaults(func=bind(cmd_pull))

    store = sub.add_parser("s3", help="scratch bucket operations")
    store_sub = store.add_subparsers(dest="s3_command", required=True)
    store_sub.add_parser("health").set_defaults(func=bind(cmd_s3))
    listing = store_sub.add_parser("list")
    listing.add_argument("--prefix", default="")
    listing.set_defaults(func=bind(cmd_s3))
    putter = store_sub.add_parser("put")
    putter.add_argument("--file", required=True)
    putter.add_argument("--key")
    putter.set_defaults(func=bind(cmd_s3))
    getter = store_sub.add_parser("get")
    getter.add_argument("--key", required=True)
    getter.add_argument("--out")
    getter.set_defaults(func=bind(cmd_s3))
    signer = store_sub.add_parser("presign")
    signer.add_argument("--key", required=True)
    signer.add_argument("--method", default="GET", choices=("GET", "PUT"))
    signer.add_argument("--expires", type=int, default=3600)
    signer.set_defaults(func=bind(cmd_s3))
    remover = store_sub.add_parser("delete")
    remover.add_argument("--key", required=True)
    remover.set_defaults(func=bind(cmd_s3))


def bootstrap_guest_agent(lab: Any, api: Any, vmid: int, user: str,
                                 password: str) -> None:
    """Install qemu-guest-agent through the serial.

    Generic cloud images have no guest agent, so there is no way in until one
    exists. The serial console is the only channel that needs nothing
    preinstalled.
    """
    with TermSession(lab, api, "qemu", vmid, timeout=30) as term:
        try:
            term.login(user, password)
        except (TimeoutError, RuntimeError) as exc:
            raise lab.LabError(f"serial login to the gateway failed: {exc}")
        term.run(
            "sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq "
            "&& sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
            "qemu-guest-agent",
            timeout=600,
        )
        term.run("sudo systemctl enable --now qemu-guest-agent", timeout=120)
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if agent_ready(lab, api, vmid):
            lab.audit("guest-agent-bootstrapped", vmid=vmid, via="serial",
                      sync=False)
            return
        time.sleep(5)
    raise lab.LabError(
        "installed qemu-guest-agent over serial but the agent still does not "
        "answer; check 'console text --vmid %s'" % vmid
    )


# --- change detection -----------------------------------------------------
#
# Cheap enough to run on every poll: a 16x16 grid of average brightness,
# compared cell by cell. Two hash designs were tried first and both missed
# real events -- a difference hash scored a dialog on a plain background at 7
# against a threshold of 12, and an average hash scored a dialog over a lit
# terminal at 0. Keeping the raw cell values costs the same and catches both.
#
# Measured on a 320x200 screen: nothing 0, blinking cursor 2, a new line of
# text 22, a dialog appearing 56.

CHANGE_THRESHOLD = 4
CELL_TOLERANCE = 8
# A console stays legible at half size, and a smaller PNG is cheaper for
# whoever has to look at it.
MAX_DIMENSION = 0   # 0 = full size


def frame_signature(rgb: bytes, width: int, height: int, size: int = 16,
                    subsamples: int = 3) -> bytes:
    """A coarse thumbnail of the screen: one average brightness per cell.

    Two hashes were tried first and both missed real events:

    * a *difference* hash encodes horizontal gradients, so a dialog appearing
      on a plain background only altered bits at its edges;
    * an *average* hash records each cell as above or below the frame mean, so
      brightening an already-bright region -- a dialog over a lit terminal --
      changed no bits at all.

    Keeping the actual cell values instead, and comparing them numerically,
    catches both. It is no more expensive: 256 cells, a fraction of a
    millisecond, and it compresses a megapixel screen to 256 bytes.
    """
    if width < 2 or height < 2:
        return b""
    cells = bytearray(size * size)
    for row in range(size):
        for column in range(size):
            total = 0
            for sy in range(subsamples):
                y = (row * subsamples + sy) * height // (size * subsamples)
                for sx in range(subsamples):
                    x = (column * subsamples + sx) * width // (size * subsamples)
                    offset = (y * width + x) * 3
                    # Rec. 601 luma, integer-only.
                    total += (
                        rgb[offset] * 299
                        + rgb[offset + 1] * 587
                        + rgb[offset + 2] * 114
                    ) // 1000
            cells[row * size + column] = min(
                255, total // (subsamples * subsamples)
            )
    return bytes(cells)


def signature_distance(first: bytes, second: bytes,
                       cell_tolerance: int = CELL_TOLERANCE) -> int:
    """How many cells changed by more than `cell_tolerance`.

    Counting cells rather than summing differences means a slow global drift
    (a fading backlight, a dithered gradient) does not accumulate into a false
    positive, while a localised change of any size is counted once per cell.
    """
    if not first or not second or len(first) != len(second):
        return len(second or first)
    return sum(
        1 for a, b in zip(first, second) if abs(a - b) > cell_tolerance
    )


def frames_differ(first: bytes, second: bytes,
                  threshold: int = CHANGE_THRESHOLD) -> bool:
    return signature_distance(first, second) >= threshold


# --- layer 2: the local model --------------------------------------------


def downscale(rgb: bytes, width: int, height: int,
              limit: int = MAX_DIMENSION) -> tuple[bytes, int, int]:
    """Shrink a frame for the model. Nearest-neighbour is fine here.

    The encoder's cost grows with pixel count, and a 1280x800 console carries
    far more detail than a four-way state classification needs.
    """
    if limit <= 0 or (width <= limit and height <= limit):
        return rgb, width, height
    scale = max(width, height) / limit
    new_width = max(1, int(width / scale))
    new_height = max(1, int(height / scale))
    out = bytearray(new_width * new_height * 3)
    for y in range(new_height):
        source_row = (y * height // new_height) * width
        target_row = y * new_width * 3
        for x in range(new_width):
            offset = (source_row + x * width // new_width) * 3
            target = target_row + x * 3
            out[target:target + 3] = rgb[offset:offset + 3]
    return bytes(out), new_width, new_height
