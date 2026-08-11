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
        # Proxmox's terminal protocol: authenticate, then set the window size.
        self.socket.send(f"{proxy['user']}:{proxy['ticket']}\n".encode())
        self.socket.send(b"1:120:40:")

    def send_line(self, text: str) -> None:
        # Proxmox's terminal frame is "0:<length>:<data>" where length counts
        # bytes, not characters. Measuring the str would under-declare any
        # non-ASCII payload and desynchronise the stream.
        payload = (text + "\n").encode()
        self.socket.send(b"0:" + str(len(payload)).encode() + b":" + payload)

    def read(self, seconds: float) -> str:
        deadline = time.monotonic() + seconds
        chunks: list[bytes] = []
        while time.monotonic() < deadline:
            data = self.socket.read_available(max(0.2, deadline - time.monotonic()))
            if data:
                chunks.append(data)
            elif chunks:
                break
        text = b"".join(chunks).decode("utf-8", "replace")
        # Proxmox acknowledges the terminal authentication with a bare "OK"
        # before any guest output; it is protocol noise, not screen content.
        if text.startswith("OK\n"):
            text = text[3:]
        elif text.startswith("OK"):
            text = text[2:]
        return text

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
            chunk = self.socket.read_available(1.5)
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
        self.expect(("assword:",), timeout=60)
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

            return {
                "exitcode": status.get("exitcode"),
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


def _screenshot_path(vmid: int, override: str | None) -> Path:
    if override:
        return Path(override).expanduser()
    DEFAULT_SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return DEFAULT_SCREENSHOT_DIR / f"vm{vmid}-{stamp}.png"


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


def cmd_screenshot(lab: Any, args: Any) -> None:
    api = lab.ProxmoxAPI()
    with VncSession(lab, api, args.vmid) as session:
        rgb = session.client.capture(timeout=args.timeout, settle=args.settle)
        width, height = session.client.width, session.client.height
    result = _save_screenshot(
        args.vmid, rgb, width, height, args.out, state_root=lab.STATE_ROOT
    )
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


def cmd_inspect(lab: Any, args: Any) -> None:
    """Capture and explicitly send one lease-owned screen to cloud vision."""
    lease = lab.load_lease(args.lease)
    owned = any(
        item.get("kind") == "qemu" and int(item.get("vmid", -1)) == args.vmid
        for item in lease.get("resources", [])
    )
    if not owned:
        raise _api_error(
            lab, f"VMID {args.vmid} is not a qemu guest registered to this lease"
        )
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
    lab.load_lease(args.lease)
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
    lab.load_lease(args.lease)
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
    lab.load_lease(args.lease)
    target = str(getattr(args, "target", "") or "").strip()
    if len(target) < 2:
        raise _api_error(
            lab, "--target must describe the visible control in at least 2 characters"
        )
    if len(target) > 80 or any(ord(char) < 32 for char in target):
        raise _api_error(lab, "--target must be a single printable label of at most 80 characters")
    target_json = json.dumps(target, ensure_ascii=False)
    with VncSession(lab, api, args.vmid) as session:
        if not (0 <= args.x < session.client.width
                and 0 <= args.y < session.client.height):
            raise _api_error(
                lab,
                f"({args.x},{args.y}) is outside the "
                f"{session.client.width}x{session.client.height} screen",
            )
        width, height = session.client.width, session.client.height
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
            raise _api_error(lab, f"click blocked: vision checkpoint failed: {exc}") from None
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


def cmd_text(lab: Any, args: Any) -> None:
    """Read the real terminal stream -- exact text, no OCR involved."""
    api = lab.ProxmoxAPI()
    kind = args.kind or _kind_of(lab, api, args.vmid)
    with TermSession(lab, api, kind, args.vmid) as session:
        if args.send:
            # Typing into a guest console is guest mutation, so it needs an
            # active lease like any other write. Reading alone does not.
            if not args.lease:
                raise _api_error(
                    lab, "--send types into the guest console and requires --lease"
                )
            lab.load_lease(args.lease)
            session.send_line(args.send)
        elif args.nudge:
            session.send_line("")
        output = session.read(args.seconds)
    print(json.dumps(
        {"vmid": args.vmid, "kind": kind, "text": textmode.strip_ansi(output)},
        indent=2,
    ))


def cmd_exec(lab: Any, args: Any) -> None:
    api = lab.ProxmoxAPI()
    lab.load_lease(args.lease)
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


def cmd_push(lab: Any, args: Any) -> None:
    """Copy a local file into a guest via the S3 scratch bucket."""
    api = lab.ProxmoxAPI()
    lab.load_lease(args.lease)
    source = Path(args.file).expanduser().resolve()
    if not source.is_file():
        raise _api_error(lab, f"not a regular file: {source}")
    payload = source.read_bytes()
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
    api = lab.ProxmoxAPI()
    lab.load_lease(args.lease)
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
    shot.set_defaults(func=bind(cmd_screenshot))

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
    click.add_argument("--target", required=True,
                       help="short visible label of the intended control")
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

    text = console_sub.add_parser(
        "text", help="read the real terminal stream (preferred over OCR)"
    )
    text.add_argument("--vmid", type=int, required=True)
    text.add_argument("--kind", choices=("qemu", "lxc"))
    text.add_argument("--seconds", type=float, default=3.0)
    text.add_argument("--send", help="send this line first, then read the reply")
    text.add_argument("--nudge", action="store_true",
                      help="send a bare newline to redraw the prompt")
    text.add_argument("--lease")
    text.set_defaults(func=bind(cmd_text))

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
