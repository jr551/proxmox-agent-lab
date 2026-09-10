"""Console access for lab guests: VNC screenshots, keyboard, pointer,
serial/LXC terminal text, and screen inspection.

The terminal session itself lives in `serial`, guest-agent primitives in
`guest_agent`, and S3 file transfer in `transfer`; the names are
re-exported here so `console.X` keeps working for feature modules and
tests.

Design notes
------------
* Screenshots are PNG. A screen is read by a vision model: `console inspect`
  sends one to a configured provider, and `console screenshot --for-model`
  hands the pixels back to the caller for its own vision instead.
* When a guest really is a terminal, prefer `console text`: Proxmox hands over
  the actual character stream, which is exact where any pixel read is a guess.
* File transfer goes through the S3 scratch bucket using presigned URLs. No
  credential ever reaches the guest, the command line, or the audit ledger.
"""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import re
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
from . import transfer as transfer_module
from .serial import (
    TermFilter, TermSession, WS_PATH_TEMPLATE, _open_websocket,
)
from .guest_agent import (
    agent_exec, agent_ready, bootstrap_guest_agent,
    clear_bootstrap_password, ensure_agent, exec_guest,
    exec_guest_script, prepare_cloudinit_worker, wait_agent_ready,
    write_guest_file,
)

DEFAULT_SCREENSHOT_DIR = Path.home() / ".local" / "state" / "proxmox-agent-lab" / "screens"

# Vision prompt for click-target verification: filled at call time with the
# concrete target label and cursor coordinate. Hoisted so the 50-line f-string
# does not dominate cmd_click.
VISION_CLICK_PROMPT = """Verify one cursor checkpoint. Return only JSON:
{{
  "screen": "short checkpoint name",
  "summary": "what is visibly happening",
  "controls": [{{"label": {target_json}, "bbox": [x0, y0, x1, y1], "confidence": 0.0}}],
  "recommended_action": {{"kind": "click", "value": "{x},{y}", "reason": "cursor visibly overlaps the named control"}},
  "expected_change": "the named control opens",
  "warnings": []
}}
The harness has already moved the visible cursor to ({x},{y}) and will
click exactly there. Locate the one control named {target_repr} in the image and
report its bounding box as "bbox": [x0, y0, x1, y1] in framebuffer pixels
(origin top-left, x increases right, y increases down, x0 < x1 and y0 < y1);
the bbox must cover the visible control body, not a single guessed point. Then
decide only whether the cursor visibly overlaps that control's body: if it
does, recommended_action is kind=click with value "{x},{y}"; if it
does not overlap, is ambiguous, or the named control is absent, return
controls=[] and recommended_action kind=stop. Never infer overlap from the
supplied coordinates alone; judge from the image."""

# Chunked transfers: files above SINGLE_OBJECT_MAX_MB are moved in parts so a
# retry resumes instead of restarting, and the assembled file is verified
# against a SHA-256 on both ends. Linux guests only (curl + split); Windows
# and --url-only keep the single-object path.


def _api_error(lab: Any, message: str) -> Exception:
    return lab.LabError(message)


# Removal signposts. Glyph-matching OCR was removed because it can only read a
# guest whose console font the controller happens to hold, and a guest is free
# to ship its own -- which is exactly what made it unusable in practice. These
# two entry points stay registered so an upgrade fails with an explanation
# instead of argparse's bare "unrecognized arguments". They are removed
# only when a release note announces it.
OCR_REMOVED = (
    "--ocr was removed: glyph-matching OCR could only read a guest whose "
    "console font this controller already had, and a guest with its own font "
    "decoded to nothing. A screen is read by a vision model now -- use "
    "'console screenshot --for-model' to get the screen back as a compressed "
    "base64 PNG for your own vision, 'console inspect' to send it to a "
    "configured vision provider, or 'console text' for a real terminal stream."
)
IMPORT_FONT_REMOVED = (
    "'console import-font' was removed along with glyph-matching OCR: there "
    "is no font table left to import into. A screen is read by a vision model "
    "now -- use 'console screenshot --for-model' to get the screen back as a "
    "compressed base64 PNG for your own vision, 'console inspect' to send it "
    "to a configured vision provider, or 'console text' for a real terminal "
    "stream."
)


def cmd_import_font(lab: Any, args: Any) -> None:
    """Removal signpost for the deleted OCR font import."""
    raise _api_error(lab, IMPORT_FONT_REMOVED)


def _kind_of(lab: Any, api: Any, vmid: int) -> str:
    """Return 'qemu' or 'lxc' for a VMID on the lab node."""
    for kind in ("qemu", "lxc"):
        try:
            api.call("GET", f"/nodes/{lab.NODE}/{kind}/{vmid}/status/current")
            return kind
        except lab.LabError:
            continue
    raise _api_error(lab, f"VMID {vmid} is not a QEMU VM or LXC container on {lab.NODE}")




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
# The literal openings of the records above, used to recognise one that is
# still arriving: a websocket read is not a record boundary.
# 'VM <id> not running' is deliberately absent above: its opening is short and
# generic, and it is matched only as a complete line.

# A partial is only treated as a possibly-incomplete record once it is this
# long. Below it, output goes straight through: an interactive prompt ends
# without a newline, and must never be held back waiting for one.
# A record is well under this. Past it, whatever is buffered is guest output
# that merely started like one.
# How much guest output is watched for a status record before the filter stops
# looking. The records are emitted once, at session start.












# --- guest agent ---------------------------------------------------------




















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
            "This looks like a character-cell screen: prefer console text for "
            "exact characters when the guest has a real terminal"
        )
    else:
        result["agent_hint"] = (
            "Read this PNG with vision. If this model has no vision, add "
            "--for-model to get the screen back as compressed base64, or use "
            "console inspect; do not build a crop-and-filter loop."
        )
    identical, comparable = _mark_stale_frame(
        vmid, rgb, width, height, state_root or DEFAULT_SCREENSHOT_DIR.parent
    )
    result["identical_to_previous_capture"] = identical
    result["comparable_to_previous_capture"] = comparable
    if identical:
        result["stale_possible"] = (
            "screen unchanged since last capture; if input was sent in "
            "between, the framebuffer may be stale \u2014 recapture before acting"
        )
    return result


def _mark_stale_frame(vmid: int, rgb: bytes, width: int, height: int,
                      state_root: Path) -> tuple[bool, bool]:
    """Compare a capture with the previous one for the same VM+resolution.

    QEMU's VNC dirty tracking can hand back the pre-action frame right after
    rapid input.  Keeping one raw frame per VM lets callers notice a
    pixel-identical repeat instead of acting on a stale screen.  This is
    best-effort: any store failure degrades to "not identical" rather than
    failing the capture.

    Returns `(identical, comparable)`.  `comparable` is false when there was
    no earlier frame of this size to compare against, or the store failed --
    a first capture is not evidence that the screen changed, so callers that
    read the comparison as a signal must be able to tell the two apart.
    """
    previous_dir = Path(state_root) / "vision-previous"
    key = previous_dir / f"screenshot-vm{vmid}-{width}x{height}.rgb"
    previous = b""
    try:
        previous = key.read_bytes()
    except OSError:
        pass
    comparable = len(previous) == len(rgb)
    identical = comparable and previous == rgb
    try:
        previous_dir.mkdir(parents=True, exist_ok=True)
        temporary = key.with_suffix(".tmp")
        temporary.write_bytes(rgb)
        temporary.replace(key)
    except OSError:
        return False, False
    return identical, comparable


# --- handing the screen back to the caller -------------------------------
#
# When no vision provider can read a screen, the agent calling this CLI often
# can. Returning a bounded, downscaled, maximally-compressed PNG as base64 in
# the JSON beats returning nothing at all.
#
# 1280px on the longest edge is the default bound. It leaves an 80-column VGA
# text screen at roughly 16 pixels per glyph and turns a 1920x1080 desktop
# into 1280x720 -- still comfortably enough to name an installer page and read
# its on-screen text -- while cutting the pixel count of a 1080p capture by
# 56%. Below 640px an 80-column line falls under 8 pixels per glyph and stops
# being reliably readable, so that is the floor the stepping loop will not go
# past. The base64 cap is 1.5 MB: large enough that a real screen never hits
# it, small enough that no caller is handed an unbounded blob.
IMAGE_MAX_EDGE = 1280
IMAGE_MIN_EDGE = 640
IMAGE_MAX_BASE64_BYTES = 1_500_000
IMAGE_STEP_NUMERATOR, IMAGE_STEP_DENOMINATOR = 3, 4


def _image_handback(rgb: bytes, width: int, height: int, *, reason: str,
                    hint: str) -> dict[str, Any]:
    """Compress one framebuffer into a bounded base64 PNG for the caller.

    Downscales only when needed, re-encodes with maximum zlib compression, and
    steps the bound down until the encoded base64 fits `IMAGE_MAX_BASE64_BYTES`
    or the readability floor is reached. The result always states what it is:
    emitted and original dimensions, the scale factor, and the byte sizes.
    """
    longest = max(width, height)
    # Start at whichever is smaller: the bound, or the image itself. An image
    # already under the floor is never shrunk at all.
    edge = min(IMAGE_MAX_EDGE, longest)
    floor = min(IMAGE_MIN_EDGE, longest)
    while True:
        out_width, out_height, scaled = png_module.downscale_rgb(
            width, height, rgb, edge
        )
        encoded = png_module.encode_png(out_width, out_height, scaled, level=9)
        blob = base64.b64encode(encoded).decode("ascii")
        if len(blob) <= IMAGE_MAX_BASE64_BYTES or edge <= floor:
            break
        edge = max(
            floor, edge * IMAGE_STEP_NUMERATOR // IMAGE_STEP_DENOMINATOR
        )
    payload: dict[str, Any] = {
        "encoding": "base64",
        "mime_type": "image/png",
        "width": out_width,
        "height": out_height,
        "original_width": width,
        "original_height": height,
        "scale": round(out_width / width, 4) if width else 1.0,
        "bytes": len(encoded),
        "base64_bytes": len(blob),
        "reason": reason,
        "agent_hint": hint,
    }
    if len(blob) > IMAGE_MAX_BASE64_BYTES:
        # Refuse rather than emit an unbounded blob. Only a pathologically
        # noisy framebuffer reaches this; a real screen compresses far below.
        payload["error"] = (
            f"the screen still needs {len(blob)} base64 bytes at the "
            f"{edge}px readability floor, over the {IMAGE_MAX_BASE64_BYTES} "
            "byte cap; read the PNG written to disk instead"
        )
        return payload
    payload["base64"] = blob
    return payload


def _image_handback_from_png(data: bytes, *, reason: str,
                             hint: str) -> dict[str, Any]:
    """Same handback, for a path that already holds an encoded PNG."""
    try:
        width, height, rgb = png_module.decode_png(data)
    except ValueError as exc:
        return {
            "encoding": "base64",
            "mime_type": "image/png",
            "error": f"could not decode the captured PNG to resize it: {exc}",
            "reason": reason,
        }
    return _image_handback(rgb, width, height, reason=reason, hint=hint)


NO_VISION_HINT = (
    "No vision provider could read this screen, so the screenshot itself is "
    "returned here: base64-decode it into a PNG and read it with your own "
    "vision rather than acting blind."
)
FOR_MODEL_HINT = (
    "This is the screen as a base64 PNG for a caller that reads images "
    "directly. Decode it and look at it; coordinates in it are scaled by "
    "'scale' from the original framebuffer."
)


def _capture_after_action(lab: Any, api: Any, args: Any,
                          session: VncSession | None = None) -> dict[str, Any] | None:
    """Optionally capture the settled screen as part of an input command.

    Keeping input and observation in one command avoids the common agent loop
    of click, reconnect, screenshot, crop, and repeat.
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


def _delivery_signal(screenshot: dict[str, Any] | None,
                     what: str) -> dict[str, Any]:
    """Whether the post-action capture shows the input landing anywhere.

    `keys_sent` and `characters_sent` count what this controller transmitted,
    not what the guest received; an operator reading them as delivery can
    drive a screen for hours that never moved.  The post-action capture
    already knows whether the framebuffer differs from the previous one, so
    report that as an explicit signal.  It is evidence, not proof -- a guest
    can change on its own, and a settled screen can legitimately look the
    same -- so nothing here fails or blocks.

    Returns the keys to merge into the command result; empty when no
    post-action screenshot was taken.
    """
    if not screenshot:
        return {}
    if not screenshot.get("comparable_to_previous_capture"):
        return {
            "screen_changed": None,
            "agent_hint": (
                "no earlier capture of this screen to compare against, so "
                f"the {what} carry no delivery evidence yet; capture again "
                "with --screenshot-after to get a comparison"
            ),
        }
    if not screenshot.get("identical_to_previous_capture"):
        return {"screen_changed": True}
    return {
        "screen_changed": False,
        "agent_hint": (
            "the screen is pixel-identical to the previous capture, so the "
            f"{what} may not have reached the guest: check that the guest is "
            "running and awake, that this VMID is registered to the lease, "
            "and re-read the screen with 'console screenshot' or "
            "'console text' before sending more input"
        ),
    }


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
    from . import host_transport

    if not getattr(args, "lease", None):
        raise _api_error(lab, "console screenshot --via monitor requires --lease")
    _require_owned_qemu(lab, args.lease, args.vmid)
    host_transport.require_host_ssh(lab)
    remote = _monitor_remote_path(args.lease, args.vmid)
    command = _screendump_command(remote)
    host_transport.host_mkdir(lab, remote.rsplit("/", 1)[0])
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
        data = host_transport.host_read_bytes(lab, remote, timeout=timeout)
    finally:
        removed = host_transport.host_remove_file(lab, remote)
        # Best effort, and only if empty: leaves nothing of ours on the host.
        host_transport.host_remove_empty_dir(lab, remote.rsplit("/", 1)[0])
    width, height = _png_dimensions(data)
    target = _screenshot_path(args.vmid, getattr(args, "out", None), "-monitor")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    # The fact of the capture, never its contents: a screen can show anything.
    lab.audit("console-screenshot", lease=args.lease, vmid=args.vmid,
              source="monitor", width=width, height=height,
              bytes=len(data), host_file_removed=removed)
    result: dict[str, Any] = {
        "vmid": args.vmid,
        "source": "monitor",
        "path": str(target),
        "width": width,
        "height": height,
        "bytes": len(data),
        "host_file_removed": removed,
        "agent_hint": (
            "Read this PNG with vision, or re-run with --for-model to get it "
            "back as base64. This capture came from QEMU, not VNC, so it "
            "carries no screen analysis and no stale-frame check -- prefer "
            "the default --via vnc unless VNC itself is the problem."
        ),
    }
    if not removed:
        result["host_file_warning"] = (
            f"could not delete {remote} on the host; remove it manually"
        )
    if getattr(args, "for_model", False):
        result["image"] = _image_handback_from_png(
            data, reason="requested with --for-model", hint=FOR_MODEL_HINT,
        )
        lab.audit("console-screenshot-for-model", lease=args.lease,
                  vmid=args.vmid, source="monitor",
                  bytes=result["image"].get("bytes", 0))
    if getattr(args, "upload", False):
        key = f"screens/vm{args.vmid}-{int(time.time())}.png"
        s3.put_bytes(key, data, "image/png")
        result["s3_key"] = key
        result["s3_url"] = s3.presign(key, expires=args.url_expiry)
    return result


def cmd_screenshot(lab: Any, args: Any) -> None:
    # Guard before anything is opened or captured: a removed flag must fail
    # with an explanation, not halfway through a console session.
    if getattr(args, "ocr", False):
        raise _api_error(lab, OCR_REMOVED)
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
    if args.upload:
        key = f"screens/vm{args.vmid}-{int(time.time())}.png"
        s3.put_bytes(key, png, "image/png")
        result["s3_key"] = key
        result["s3_url"] = s3.presign(key, expires=args.url_expiry)
    if getattr(args, "for_model", False):
        result["image"] = _image_handback(
            rgb, width, height, reason="requested with --for-model",
            hint=FOR_MODEL_HINT,
        )
        # The fact and the size, never the pixels: a screen can show anything.
        lab.audit("console-screenshot-for-model", vmid=args.vmid, source="vnc",
                  bytes=result["image"].get("bytes", 0))
    print(json.dumps(result, indent=2, sort_keys=True))


def _require_owned_qemu(lab: Any, lease_id: str, vmid: int) -> None:
    lab.require_lease_resource(lab.load_lease(lease_id), "qemu", vmid)

def _require_keyboard_input(lab: Any, api: Any, vmid: int, force: bool) -> None:
    """Refuse to send VNC input to a guest that provably cannot receive it.

    RFB key/pointer events go to the emulated PS/2 keyboard, which only
    exists when the guest has a graphical display. On `vga: serial*` the
    screen still renders but input is silently dropped, so reporting
    `keys_sent`/`characters_sent` would claim a delivery that never happened.
    `--force` overrides for the rare case the config read is wrong.
    """
    # Only an explicit --force (a real True) bypasses; a truthy non-bool must
    # not silently disable the guard.
    if force is True:
        return
    config = api.call("GET", f"/nodes/{lab.NODE}/qemu/{vmid}/config") or {}
    vga = str(config.get("vga") or "")
    if vga.startswith("serial"):
        raise _api_error(
            lab,
            f"VMID {vmid} has no graphical display (vga is serial), so VNC "
            "keyboard/pointer input cannot reach it. Drive it over serial "
            "('console text --send' / 'guest run'), or pass --force to send "
            "anyway.",
        )


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
        reason = str(exc)
        # Last resort: no provider could read the screen, but the agent that
        # called this may have vision of its own. Hand the pixels back rather
        # than leaving it blind. This is additive -- the failure is still
        # audited, still reported, and the command still exits non-zero.
        handback = None
        if getattr(args, "image_fallback", True):
            handback = _image_handback(
                rgb, width, height,
                reason=f"no vision provider could read this screen: {reason}"[:300],
                hint=NO_VISION_HINT,
            )
        lab.audit(
            "console-vision-inspect-failed", lease=args.lease, vmid=args.vmid,
            error=reason[:200], provider=args.provider or "auto",
            # The fact and the size of the handback, never its pixels.
            image_returned=handback is not None,
            image_bytes=(handback or {}).get("bytes", 0),
        )
        failure: dict[str, Any] = {
            "vmid": args.vmid,
            "screenshot": screenshot,
            "model_input": model_input,
            "vision_error": reason,
        }
        if handback is not None:
            failure["image"] = handback
        print(json.dumps(failure, indent=2, sort_keys=True))
        raise _api_error(lab, reason) from None
    lab.audit(
        "console-vision-inspect", lease=args.lease, vmid=args.vmid,
        provider=analysis["provider"], model=analysis["model"],
    )
    destination = {
        "nvidia": "integrate.api.nvidia.com",
        "kilo": "api.kilo.ai",
    }.get(analysis["provider"], "openrouter.ai")
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
    _require_keyboard_input(lab, api, args.vmid, getattr(args, "force", False))
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
              count=len(combos), via=args.via)
    result: dict[str, Any] = {
        "vmid": args.vmid, "keys_sent": len(combos), "via": args.via,
    }
    if screenshot is not None:
        result["screenshot_after"] = screenshot
    result.update(_delivery_signal(screenshot, "keystrokes"))
    print(json.dumps(result, indent=2, sort_keys=True))


def cmd_type(lab: Any, args: Any) -> None:
    api = lab.ProxmoxAPI()
    _require_owned_qemu(lab, args.lease, args.vmid)
    _require_keyboard_input(lab, api, args.vmid, getattr(args, "force", False))
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
              characters=sent)
    result: dict[str, Any] = {"vmid": args.vmid, "characters_sent": sent}
    if screenshot is not None:
        result["screenshot_after"] = screenshot
    result.update(_delivery_signal(screenshot, "typed characters"))
    print(json.dumps(result, indent=2, sort_keys=True))


def _cursor_checkpoint(lab: Any, session: VncSession, args: Any,
                       target: str) -> tuple[dict[str, Any], dict[str, Any], bool, str, dict[str, Any]]:
    """Move the cursor, capture, run vision, and verify the target.

    Returns (checkpoint, temporal, verified, reason, analysis). Raises the
    caller's LabError on vision failure so the click is blocked.
    """
    width, height = session.client.width, session.client.height
    target_json = json.dumps(target, ensure_ascii=False)
    session.client.pointer(args.x, args.y, 0)
    rgb = session.client.capture(timeout=25.0, settle=args.calibration_settle)
    checkpoint = _save_screenshot(
        args.vmid, rgb, width, height, getattr(args, "screenshot_out", None),
        state_root=lab.STATE_ROOT,
    )
    guided, temporal = _model_frame(lab, args.lease, args.vmid, rgb, width, height)
    gridded = png_module.overlay_coordinate_grid(width, height, guided, step=100)
    grid_png = png_module.encode_png(width, height, gridded)
    prompt = VISION_CLICK_PROMPT.format(
        target_json=target_json, x=args.x, y=args.y, target_repr=repr(target),
    )
    try:
        analysis = vision.analyze_png(
            lab.CONFIG, grid_png, width=width, height=height, prompt=prompt,
            timeout=args.vision_timeout, provider=args.provider,
        )
    except (vision.VisionError, secrets_store.SecretError) as exc:
        raise _api_error(lab, f"click blocked: vision checkpoint failed: {exc}") from None
    verified, reason = vision.verifies_target(analysis, target, args.x, args.y)
    lab.audit(
        "console-click-calibration", lease=args.lease, vmid=args.vmid,
        width=width, height=height, target=target, verified=verified,
        provider=analysis.get("provider"),
    )
    return checkpoint, temporal, verified, reason, analysis


def _emit_click_result(
    lab: Any, args: Any, screenshot: dict[str, Any] | None, *,
    empty_space: bool, target: str = "",
    temporal: dict[str, Any] | None = None,
    reason: str = "", analysis: dict[str, Any] | None = None,
) -> None:
    """Audit and print the final click result for both empty-space and verified paths."""
    if empty_space:
        lab.audit(
            "console-click-unverified", lease=args.lease, vmid=args.vmid,
            x=args.x, y=args.y, button=args.button,
        )
        result: dict[str, Any] = {
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
              x=args.x, y=args.y, button=args.button)
    result = {
        "vmid": args.vmid, "clicked": [args.x, args.y], "target": target,
        "verification": {"accepted": True, "reason": reason},
        "temporal": temporal,
    }
    if analysis is not None:
        control = vision.matched_control(analysis, target)
        if control is not None and isinstance(control.get("bbox"), list):
            result["control_bbox"] = control["bbox"]
    if screenshot is not None:
        result["screenshot_after"] = screenshot
    print(json.dumps(result, indent=2, sort_keys=True))


def cmd_click(lab: Any, args: Any) -> None:
    api = lab.ProxmoxAPI()
    _require_owned_qemu(lab, args.lease, args.vmid)
    _require_keyboard_input(lab, api, args.vmid, getattr(args, "force", False))
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
            checkpoint = temporal = verified = reason = analysis = None  # type: ignore[assignment]
        else:
            checkpoint, temporal, verified, reason, analysis = _cursor_checkpoint(
                lab, session, args, target
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
        _emit_click_result(lab, args, screenshot, empty_space=True)
        return
    _emit_click_result(
        lab, args, screenshot, empty_space=False, target=target,
        temporal=temporal, reason=reason, analysis=analysis,
    )


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
              locked_up=locked_up)
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
              kind=args.kind, port=port)
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
    """Read the real terminal stream -- exact characters, not pixels."""
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

        deadline = time.monotonic() + (args.timeout if args.timeout else 3600)
        with _attach_term(lab, api, kind, args.vmid, wait=wait) as session:
            if args.from_reset:
                api.call(
                    "POST", f"/nodes/{lab.NODE}/qemu/{args.vmid}/status/reset"
                )
                lab.audit("console-text-from-reset", lease=args.lease,
                          vmid=args.vmid)
            while time.monotonic() < deadline:
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
              argv0=command[0], exitcode=result["exitcode"])
    print(json.dumps(result, indent=2))
























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
            # Screen-reading readiness is now a question about vision keys,
            # not about an installed console font.
            "vision": {
                "any_provider_key": vision.available(lab.CONFIG),
                "provider_keys": vision.provider_keys(lab.CONFIG),
            },
        },
        indent=2,
        sort_keys=True,
    ))


def _register_console(console_sub: Any, lab: Any, add_after_screenshot: Any) -> None:
    from .cli import _bind

    shot = console_sub.add_parser("screenshot", help="capture the screen as PNG")
    shot.add_argument("--vmid", type=int, required=True)
    shot.add_argument("--out")
    shot.add_argument("--settle", type=float, default=0.0,
                      help="seconds to wait before capturing")
    shot.add_argument("--timeout", type=float, default=25.0)
    shot.add_argument("--upload", action="store_true",
                      help="also store the PNG in the S3 scratch bucket")
    shot.add_argument("--url-expiry", type=int, default=3600)
    shot.add_argument(
        "--for-model", dest="for_model", action="store_true",
        help="also return the screen as a bounded, downscaled base64 PNG in "
             "the JSON, for a caller that reads images with its own vision",
    )
    shot.add_argument(
        "--ocr", action="store_true",
        help="removed: glyph-matching OCR could not read a guest's own font; "
             "use --for-model or 'console inspect'",
    )
    shot.add_argument(
        "--via", choices=("vnc", "monitor"), default="vnc",
        help="capture path: 'vnc' (default) reads pixels over the console; "
             "'monitor' uses QEMU screendump on the host, which writes a "
             "lease-scoped temporary PNG there and needs --lease plus the "
             "opt-in [memflow] host SSH channel to fetch and delete it",
    )
    shot.add_argument("--lease", help="required with --via monitor")
    shot.set_defaults(func=_bind(lab, cmd_screenshot))

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
    burst.set_defaults(func=_bind(lab, cmd_screenshot_burst))

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
        choices=("auto", "nvidia", "openrouter-nemotron", "openrouter-free",
                 "kilo"),
        default="auto",
        help="provider override; auto uses the guarded fallback chain",
    )
    inspect.add_argument(
        "--no-image-fallback", dest="image_fallback", action="store_false",
        help="do not return the screen as base64 when every vision provider "
             "fails; error with no image instead",
    )
    inspect.set_defaults(func=_bind(lab, cmd_inspect))

    keys = console_sub.add_parser("keys", help="send key combinations")
    keys.add_argument("--lease", required=True)
    keys.add_argument("--vmid", type=int, required=True)
    keys.add_argument("keys", nargs="+", help="e.g. ctrl-alt-delete f2 enter")
    keys.add_argument("--via", choices=("vnc", "api"), default="vnc")
    keys.add_argument("--delay", type=float, default=0.08)
    keys.add_argument("--force", action="store_true",
                      help="send even when the guest has no graphical display")
    add_after_screenshot(keys)
    keys.set_defaults(func=_bind(lab, cmd_keys))

    typing = console_sub.add_parser("type", help="type text at the console")
    typing.add_argument("--lease", required=True)
    typing.add_argument("--vmid", type=int, required=True)
    typing.add_argument("--text")
    typing.add_argument("--text-stdin", action="store_true",
                        help="read the text from stdin, keeping it out of argv")
    typing.add_argument("--enter", action="store_true")
    typing.add_argument("--delay", type=float, default=0.012)
    typing.add_argument("--force", action="store_true",
                        help="send even when the guest has no graphical display")
    add_after_screenshot(typing)
    typing.set_defaults(func=_bind(lab, cmd_type))

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
    click.add_argument("--force", action="store_true",
                       help="send even when the guest has no graphical display")
    add_after_screenshot(click)
    click.set_defaults(func=_bind(lab, cmd_click))

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
    gui_lockup.set_defaults(func=_bind(lab, cmd_has_gui_locked_up))

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
    terminal_lockup.set_defaults(func=_bind(lab, cmd_has_terminal_locked_up))

    text = console_sub.add_parser(
        "text", help="read the real terminal stream (exact characters)"
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
    text.set_defaults(func=_bind(lab, cmd_text))

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
    bridge.set_defaults(func=_bind(lab, cmd_bridge))

    execute = console_sub.add_parser("exec", help="run a command via guest agent")
    execute.add_argument("--lease", required=True)
    execute.add_argument("--vmid", type=int, required=True)
    execute.add_argument("--shell", action="store_true")
    execute.add_argument("--windows", action="store_true")
    execute.add_argument("--timeout", type=int, default=300)
    execute.add_argument("command", nargs="+")
    execute.set_defaults(func=_bind(lab, cmd_exec))

    preflight = console_sub.add_parser(
        "preflight", help="check console privileges and scratch storage"
    )
    preflight.set_defaults(func=_bind(lab, cmd_preflight))

    import_font = console_sub.add_parser(
        "import-font",
        help="removed: a screen is read by a vision model, not a font table",
    )
    for ignored in ("--file", "--from-vmid", "--guest-path", "--lease"):
        import_font.add_argument(ignored, help=argparse.SUPPRESS)
    import_font.set_defaults(func=_bind(lab, cmd_import_font))






def register(sub: Any, lab: Any) -> None:
    """Attach the console, transfer and S3 subcommands to the main parser."""
    def add_after_screenshot(parser: Any) -> None:
        parser.add_argument(
            "--screenshot-after", type=float, metavar="SECONDS",
            help="after input, wait this long and include a PNG in the "
                 "result; 'keys' and 'type' also report screen_changed from "
                 "it, the only evidence the input reached the guest",
        )
        parser.add_argument(
            "--screenshot-out", "--out", dest="screenshot_out",
            help="path for --screenshot-after (default: state screens directory)",
        )

    console = sub.add_parser("console", help="VNC, terminal and guest access")
    console_sub = console.add_subparsers(dest="console_command", required=True)
    _register_console(console_sub, lab, add_after_screenshot)
    transfer_module._register_transfer(sub, lab)
    transfer_module._register_s3(sub, lab)
