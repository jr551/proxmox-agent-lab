"""Guest console capture and keyboard input: `console screenshot|type|keys`.

The whole surface rides the ssh seam (`ssh.py`), one allowlisted argv at a
time: `qm monitor` takes a `screendump`, `cat` fetches it, and `qm sendkey`
delivers key input. `screenshot` writes a PNG on the controller; `type` and
`keys` inject keystrokes into a live QEMU guest.

Screenshot host-temp caveat: QEMU's `screendump` writes a PPM **on the
host**, at a FIXED per-vmid path ``/tmp/pxl-shot-<vmid>.ppm`` that is
OVERWRITTEN on every capture. It is deliberately never deleted: `rm` is a
host-changing command on the ssh allowlist, and a screenshot must stay a
read-only operation, so the file simply holds the guest's most recent
screen until the next capture replaces it.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import time
from pathlib import Path
from string import ascii_lowercase, digits
from typing import Any

from . import calibration
from . import guest
from . import png as png_module
from . import ssh as ssh_module
from .errors import LabError

#: The fastest `type` will ever pace itself: 20 keys/second (§D). A slower
#: --chars-per-second is honoured; a faster request is clamped to this.
MAX_CHARS_PER_SECOND = 20.0

#: Single-character translation for `type` text and `keys` tokens (§D).
#: Letters and digits are handled in `key_for`; nothing outside the table is
#: ever guessed.
KEYMAP: dict[str, str] = {
    # Shift-modified glyphs: the shifted number row plus the two shifted
    # punctuation keys.
    "!": "shift-1", "@": "shift-2", "#": "shift-3", "$": "shift-4",
    "%": "shift-5", "^": "shift-6", "&": "shift-7", "*": "shift-8",
    "(": "shift-9", ")": "shift-0", "_": "shift-minus", "+": "shift-equal",
    # Plain punctuation and whitespace.
    " ": "spc", ".": "dot", ",": "comma", "-": "minus", "=": "equal",
    "/": "slash", ";": "semicolon", "'": "quote", "\\": "backslash",
    "\t": "tab", "\n": "ret",
}

#: Bare key names `keys` accepts verbatim, alone or stacked into a
#: combination (`ctrl-alt-delete`, `shift-a`): QEMU's own sendkey names.
NAMED_KEYS: frozenset[str] = (
    frozenset({
        "esc", "tab", "ret", "backspace", "spc", "minus", "equal",
        "bracket_left", "bracket_right", "backslash", "semicolon", "quote",
        "grave_accent", "comma", "dot", "slash", "caps_lock", "print",
        "scroll_lock", "pause", "insert", "delete", "home", "end", "pgup",
        "pgdn", "up", "down", "left", "right", "num_lock", "kp_divide",
        "kp_multiply", "kp_subtract", "kp_add", "kp_decimal", "kp_enter",
        "shift", "shift_r", "alt", "alt_r", "altgr", "ctrl", "ctrl_r",
        "meta_l", "meta_r", "menu", "power", "sysrq",
    })
    | {f"f{number}" for number in range(1, 13)}
    | {f"kp_{number}" for number in range(10)}
    | set(ascii_lowercase)
    | set(digits)
)

_ACCEPTED_NAMES = ", ".join(sorted(NAMED_KEYS))

#: Injectable so tests can observe pacing and delays without wall-clock waits.
_sleep = time.sleep


def _make_ssh(config: Any) -> ssh_module.SSH:
    """The ssh seam for this configuration (tests substitute a double)."""
    return ssh_module.SSH(config.ssh.target)


# -- key translation -------------------------------------------------------

def _unknown_key(key: object) -> LabError:
    return LabError(
        f"unknown key {key!r}; accepted names: {_ACCEPTED_NAMES} "
        f"(or a stacked combination such as ctrl-alt-delete / shift-a, or a "
        f"single character that KEYMAP and the letters/digits cover)"
    )


def key_for(key: str) -> list[str]:
    """Translate one typed character or key token to QEMU sendkey name(s).

    A single character goes through KEYMAP (`!` -> ``shift-1``, ` ` ->
    ``spc``); letters and digits pass through, uppercase as `shift-<letter>`.
    A longer token is a bare key name (`ret`, `f2`, `spc`) or a stacked
    combination (`ctrl-alt-delete`, `shift-a`), passed through verbatim once
    every part is a known name. Anything else raises `LabError` listing the
    accepted names -- a guessed key would type the wrong thing at a real
    console.
    """
    if not key:
        raise _unknown_key(key)
    if len(key) == 1:
        mapped = KEYMAP.get(key)
        if mapped is not None:
            return [mapped]
        if "a" <= key <= "z" or "0" <= key <= "9":
            return [key]
        if "A" <= key <= "Z":
            return ["shift-" + key.lower()]
        raise _unknown_key(key)
    if all(part in NAMED_KEYS for part in key.split("-")):
        return [key]
    raise _unknown_key(key)


# -- shared pipeline -------------------------------------------------------

def _describe(result: Any) -> str:
    """What a failed remote call said, bounded (remote output is untrusted)."""
    detail = result.stderr.decode("utf-8", "replace").strip()
    return detail[:200] or f"exit status {result.returncode}"


def _guest_kind(ssh: Any, vmid: int) -> str:
    """`"qemu"` or `"lxc"` by probing `qm status`, then `pct status`.

    Raw argv on the ssh seam: exit status is the answer, so no output has to
    be parsed and a missing guest is simply a refusal from both.
    """
    if ssh.run(["qm", "status", str(vmid)]).ok:
        return "qemu"
    if ssh.run(["pct", "status", str(vmid)]).ok:
        return "lxc"
    raise LabError(
        f"guest {vmid} is not on this node: neither qm nor pct answers for it"
    )


def _capture_png(lab: Any, ssh: Any, vmid: int,
                 out: str | Path | None) -> dict[str, Any]:
    """The screenshot pipeline: screendump -> fetch -> PNG -> file -> audit.

    Only QEMU guests: `screendump` is a `qm monitor` command. The host temp
    is the FIXED ``/tmp/pxl-shot-<vmid>.ppm`` (see the module docstring):
    overwritten every capture, never removed, so this stays read-only on the
    host. Returns ``{vmid, path, bytes, width, height}``.
    """
    ppm_path = f"/tmp/pxl-shot-{vmid}.ppm"
    monitor = ssh.run(
        ["qm", "monitor", str(vmid)],
        stdin=f"screendump {ppm_path}\nquit\n".encode(),
    )
    if not monitor.ok:
        raise LabError(
            f"qm monitor {vmid} would not take a screendump: {_describe(monitor)}"
        )
    fetched = ssh.run(["cat", ppm_path])
    if not fetched.ok:
        raise LabError(
            f"the host would not hand back {ppm_path}: {_describe(fetched)}"
        )
    try:
        png = png_module.ppm_to_png(fetched.stdout)
    except ValueError as exc:
        raise LabError(
            f"the screendump of guest {vmid} is not a valid PPM: {exc}"
        ) from None
    width, height = struct.unpack(">II", png[16:24])
    path = (
        Path(out).expanduser()
        if out
        else Path(lab.STATE_ROOT) / "screens"
        / f"pxl-shot-{vmid}-{int(time.time() * 1000)}.png"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)
    # The fact and the size, never the pixels: a screen can show anything.
    lab.audit("console-screenshot", vmid=vmid, bytes=len(png))
    return {
        "vmid": vmid, "path": str(path), "bytes": len(png),
        "width": width, "height": height,
    }


def _require_qemu_guest(lab: Any, lease_id: str, vmid: int) -> dict[str, Any]:
    """Refuse keys/type unless the lease owns this live QEMU guest.

    The gate is a pure registry lookup (`guest.require_owned`, store only,
    zero seam calls) and runs BEFORE any ssh call: an unowned guest is
    refused without touching the host at all. `kind=None` asks for whichever
    kind the registry recorded; `qm sendkey` is a QEMU keyboard, so a
    registered container is refused here too. Registry-vouched guests
    (retained/template) are refused by the same call: console input is a
    mutation, and vouched guests are clone sources and read-only targets.
    """
    row = guest.require_owned(lab, lease_id, None, vmid)
    if row.get("kind") != "qemu":
        raise LabError(
            f"console keys/type drive the QEMU keyboard via qm sendkey: "
            f"{row.get('kind', '?')}/{vmid} is not a qemu guest"
        )
    return row


# -- pointer input ----------------------------------------------------------

#: The QEMU HID Tablet, reported by `qm monitor`'s `mouse` command. Absolute
#: pointer devices report a position over the full framebuffer, which is
#: what a coordinate has to be to be clickable; the default PS/2 mouse is
#: relative and would need a current position we cannot read. Selecting the
#: tablet is therefore the first thing every pointer action does.
TABLET_INDEX = 3

#: Upper bound on any single pointer action's wall-clock cost, so a stuck
#: monitor session cannot hold an MCP request open indefinitely.
POINTER_TIMEOUT = 30.0


def _monitor(ssh: Any, vmid: int, script: str, timeout: float) -> Any:
    """Run one `qm monitor` script; the result is the answer, not the text.

    HMP reports a bad command on stderr ("unknown command: '...'") and is
    silent on success, so ``ok`` alone is not enough: the monitor echoes
    every command and the greeting to stdout, and a rejection shows up in
    the same stream. So the reply is checked for the rejection text too,
    or a refused click would look exactly like a delivered one.
    """
    result = ssh.run(
        ["qm", "monitor", str(vmid)], stdin=script.encode(), timeout=timeout
    )
    if not result.ok:
        raise LabError(f"qm monitor {vmid} failed: {_describe(result)}")
    for stream in (result.stdout, result.stderr):
        text = stream.decode("utf-8", "replace").lower()
        for marker in ("unknown command", "invalid", "not found", "error:"):
            if marker in text:
                raise LabError(
                    f"qm monitor {vmid} rejected {script.strip()!r}: "
                    f"{_describe(result)}"
                )
    return result


def _select_tablet(ssh: Any, vmid: int) -> None:
    """Point the guest's absolute pointer at the HID tablet.

    Idempotent and required before any move: without it `mouse_move` is
    interpreted in the PS/2 relative device's coordinate space, and the
    click lands somewhere else entirely. Re-selecting on every action is
    what makes a sequence of moves behave as one continuous gesture.
    """
    _monitor(ssh, vmid, f"mouse_set {TABLET_INDEX}\nquit\n", POINTER_TIMEOUT)


def _framebuffer_size(ssh: Any, vmid: int) -> tuple[int, int]:
    """(width, height) of the guest's current framebuffer, via a screendump.

    A capture is the only way to learn the real frame size, and it doubles
    as a liveness check: a stopped guest cannot produce one. Reusing the
    fixed host temp keeps this read-only, exactly like ``screenshot``.
    """
    ppm_path = f"/tmp/pxl-shot-{vmid}.ppm"
    _monitor(ssh, vmid, f"screendump {ppm_path}\nquit\n", POINTER_TIMEOUT)
    fetched = ssh.run(["cat", ppm_path])
    if not fetched.ok:
        raise LabError(
            f"qm monitor {vmid} could not screendump the guest: the host "
            f"would not return {ppm_path} ({_describe(fetched)}). Is the "
            f"guest running? Pointer input needs a live display."
        )
    try:
        width, height, _ = png_module._decode_ppm(fetched.stdout)
    except ValueError as exc:
        raise LabError(
            f"the screendump of guest {vmid} is unusable: {exc}"
        ) from None
    return width, height



def resolve_point(
    lab: Any, ssh: Any, vmid: int, x: int, y: int,
    space: str = "framebuffer", client: str | None = None,
) -> tuple[int, int, str]:
    """Map a requested point into framebuffer pixels; report how it was read.

    ``space="framebuffer"`` (the default) takes the numbers as already being
    real pixels, which is what a human reading the returned screenshot's
    width/height wants. ``space="image"`` treats them as coordinates read off
    a scaled-down view and applies this client's saved calibration for the
    guest's current resolution -- the case where a click would otherwise
    land near the target instead of on it.

    An uncalibrated request is REFUSED rather than passed through: sending
    image-space numbers to the tablet as if they were framebuffer pixels is
    precisely the failure this exists to prevent, and a wrong click on
    someone's machine is worse than an error message.
    """
    if space not in ("framebuffer", "image"):
        raise LabError(
            f"unknown coordinate space {space!r}: use 'framebuffer' for real "
            f"pixel coordinates, or 'image' for coordinates read off a "
            f"downscaled screenshot (that needs a saved calibration)"
        )
    width, height = _framebuffer_size(ssh, vmid)
    if space == "framebuffer":
        if not (0 <= x < width and 0 <= y < height):
            raise LabError(
                f"({x}, {y}) is outside this guest's {width}x{height} "
                f"framebuffer"
            )
        return x, y, "framebuffer"
    name = client or _client_name()
    record = calibration.find(calibration.normalize_client_id(name),
                              str(vmid), width, height)
    if record is None or not record.trustworthy:
        raise LabError(
            f"image-space coordinates need a saved calibration for guest "
            f"{vmid} at {width}x{height}, and there is none that fits "
            f"(client {calibration.normalize_client_id(name) or 'unnamed'!r}). "
            f"Run `console calibrate` first, or pass --space framebuffer with "
            f"real pixel coordinates."
        )
    fb_x, fb_y = record.to_framebuffer(float(x), float(y))
    return fb_x, fb_y, "image->framebuffer"


def _client_name() -> str:
    """This agent's identity for calibration keying, if it offered one.

    Defaults to a generic name, which calibration deliberately refuses to
    key on: a transform depends on how *this* client scales images, and a
    shared generic key would apply one client's scaling to another.
    """
    return "proxmox-lab-cli"


def _pointer_gate(lab: Any, lease_id: str, vmid: int) -> dict[str, Any]:
    """Pointer actions are mutations: same lease gate as keys/type."""
    row = _require_qemu_guest(lab, lease_id, vmid)
    if _guest_kind(_make_ssh(lab.CONFIG), vmid) != "qemu":
        raise LabError(
            f"pointer input drives the QEMU HID tablet: {vmid} is not a qemu guest"
        )
    return row


def _move(ssh: Any, vmid: int, x: int, y: int) -> None:
    """Move the absolute pointer to one framebuffer point, no click."""
    _monitor(
        ssh, vmid, f"mouse_move {x} {y}\nquit\n", POINTER_TIMEOUT
    )


def _click(ssh: Any, vmid: int, button: int = 1, count: int = 1) -> None:
    """Press and release a mouse button `count` times at the current point.

    HMP button numbers are QEMU's: 1 left, 2 middle, 3 right. Each press is
    a separate `mouse_button 1` / `mouse_button 0` pair, so a double-click
    is two complete clicks rather than a held button.
    """
    for _ in range(count):
        _monitor(ssh, vmid, f"mouse_button {button}\n", POINTER_TIMEOUT)
        _monitor(ssh, vmid, f"mouse_button 0\nquit\n", POINTER_TIMEOUT)


def _drag(ssh: Any, vmid: int, x1: int, y1: int, x2: int, y2: int,
          steps: int) -> None:
    """Press at one point, move to another in `steps` hops, release.

    The interpolated hops matter: a guest application that tracks a drag
    (a window, a slider, a file) usually needs intermediate motion events,
    and a single jump registers as a teleport the app discards.
    """
    _select_tablet(ssh, vmid)
    _move(ssh, vmid, x1, y1)
    _monitor(ssh, vmid, f"mouse_button 1\n", POINTER_TIMEOUT)
    for step in range(1, steps + 1):
        x = round(x1 + (x2 - x1) * step / steps)
        y = round(y1 + (y2 - y1) * step / steps)
        _monitor(
            ssh, vmid, f"mouse_move {x} {y}\nquit\n", POINTER_TIMEOUT
        )
    _monitor(ssh, vmid, "mouse_button 0\nquit\n", POINTER_TIMEOUT)



def cmd_move(lab: Any, args: argparse.Namespace) -> dict[str, Any]:
    """Move the guest pointer without clicking (`console move`).

    The safe way to find out where a coordinate lands: it changes nothing
    but the cursor, so it can be used to probe a layout before committing
    to a click.
    """
    vmid = int(args.vmid)
    _pointer_gate(lab, str(args.lease), vmid)
    ssh = _make_ssh(lab.CONFIG)
    x, y, how = resolve_point(
        lab, ssh, vmid, int(args.x), int(args.y), args.space, args.client
    )
    _select_tablet(ssh, vmid)
    _move(ssh, vmid, x, y)
    lab.audit("console-move", lease=str(args.lease), vmid=vmid, x=x, y=y)
    payload = {
        "vmid": vmid, "lease": str(args.lease), "x": x, "y": y,
        "coordinates": how, "clicked": False,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return payload


def cmd_click(lab: Any, args: argparse.Namespace) -> dict[str, Any]:
    """Click in the guest at a point (`console click`).

    Mutating and lease-gated. Coordinates are framebuffer pixels unless
    ``--space image`` is given, which requires a saved calibration and is
    refused without one -- clicking the wrong pixel on a real machine is
    worse than not clicking. The audit records the resulting pixel, never
    anything read off the screen.
    """
    vmid = int(args.vmid)
    lease_id = str(args.lease)
    _pointer_gate(lab, lease_id, vmid)
    buttons = {"left": 1, "middle": 2, "right": 3}
    if args.button not in buttons:
        raise LabError(
            f"unknown button {args.button!r}: use left, middle or right"
        )
    count = 2 if args.double else 1
    ssh = _make_ssh(lab.CONFIG)
    x, y, how = resolve_point(
        lab, ssh, vmid, int(args.x), int(args.y), args.space, args.client
    )
    _select_tablet(ssh, vmid)
    _move(ssh, vmid, x, y)
    _click(ssh, vmid, buttons[args.button], count)
    lab.audit(
        "console-click", lease=lease_id, vmid=vmid, x=x, y=y,
        button=args.button, clicks=count,
    )
    payload: dict[str, Any] = {
        "vmid": vmid, "lease": lease_id, "x": x, "y": y,
        "coordinates": how, "button": args.button, "clicks": count,
        "clicked": True,
    }
    if args.screenshot_after is not None:
        _sleep(float(args.screenshot_after))
        payload["screenshot"] = _capture_png(lab, ssh, vmid, None)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return payload


def cmd_drag(lab: Any, args: argparse.Namespace) -> dict[str, Any]:
    """Press, drag and release inside the guest (`console drag`).

    Both endpoints are resolved and bounds-checked through the same gate as
    a click, so a drag can never leave the framebuffer halfway.
    """
    vmid = int(args.vmid)
    lease_id = str(args.lease)
    _pointer_gate(lab, lease_id, vmid)
    steps = int(args.steps)
    if not 1 <= steps <= 200:
        raise LabError("--steps must be between 1 and 200")
    ssh = _make_ssh(lab.CONFIG)
    x1, y1, how1 = resolve_point(
        lab, ssh, vmid, int(args.x), int(args.y), args.space, args.client
    )
    x2, y2, how2 = resolve_point(
        lab, ssh, vmid, int(args.to_x), int(args.to_y), args.space, args.client
    )
    _drag(ssh, vmid, x1, y1, x2, y2, steps)
    lab.audit(
        "console-drag", lease=lease_id, vmid=vmid,
        x1=x1, y1=y1, x2=x2, y2=y2, steps=steps,
    )
    payload: dict[str, Any] = {
        "vmid": vmid, "lease": lease_id,
        "from": [x1, y1], "to": [x2, y2], "steps": steps,
        "coordinates": how1 if how1 == how2 else f"{how1}/{how2}",
    }
    if args.screenshot_after is not None:
        _sleep(float(args.screenshot_after))
        payload["screenshot"] = _capture_png(lab, ssh, vmid, None)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return payload


def cmd_calibrate(lab: Any, args: argparse.Namespace) -> dict[str, Any]:
    """Measure how this client scales the guest's screen (`console calibrate`).

    Three actions. ``start`` lays a numbered marker grid on the real guest
    display and returns a PNG; the client reports where each marker landed
    *in the image it was shown*; ``submit`` solves the transform and reports
    its error; ``commit`` saves it so later ``--space image`` clicks are
    mapped for you. The calibration is only useful if the marker readings
    are measured, not guessed -- which is why ``submit`` refuses a fit that
    does not actually fit.

    Ported from vnc-mcp (BSD 2-Clause); see NOTICE.
    """
    vmid = int(args.vmid)
    ssh = _make_ssh(lab.CONFIG)
    if _guest_kind(ssh, vmid) != "qemu":
        raise LabError(
            f"calibration needs a live qemu display: {vmid} is not a qemu guest"
        )
    width, height = _framebuffer_size(ssh, vmid)
    name = args.client or _client_name()
    client_id = calibration.normalize_client_id(name)
    if not calibration.is_usable_client(client_id):
        raise LabError(
            f"client identity {client_id!r} is too generic to calibrate "
            f"against: the transform depends on how this client scales "
            f"images, so pass --client with something specific"
        )
    action = args.action
    if action == "status":
        payload = {
            "vmid": vmid, "framebuffer": [width, height],
            "client": client_id,
            "status": calibration.status_line(name, str(vmid), width, height),
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return payload
    if action == "start":
        grid = calibration.marker_positions(width, height)
        drawn = _draw_markers(lab, ssh, vmid, grid, width, height)
        payload = {
            "vmid": vmid, "framebuffer": [width, height], "client": client_id,
            "screenshot": drawn,
            "markers": [
                {"id": mid, "x": round(fx), "y": round(fy)}
                for mid, fx, fy in grid
            ],
            "next": (
                "read where each marker appears in the image you were shown "
                "and re-run with --action submit --samples "
                '[{"id":"M1","x":..,"y":..}, ...]'
            ),
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return payload
    samples = json.loads(args.samples) if args.samples else []
    markers = calibration.build_markers(width, height, samples)
    if len(markers) < 2:
        raise LabError(
            f"only {len(markers)} usable marker reading(s); at least two are "
            f"needed to fit a transform. Read the markers off the start "
            f"screenshot rather than guessing."
        )
    solution = calibration.solve(markers)
    if solution is None:
        raise LabError(
            "these marker readings do not determine a transform (every "
            "reading is identical, or they do not vary). Re-read them."
        )
    payload: dict[str, Any] = {
        "vmid": vmid, "framebuffer": [width, height], "client": client_id,
        "markers_used": len(markers),
        "x": {"a": round(solution.x_a, 6), "b": round(solution.x_b, 6)},
        "y": {"a": round(solution.y_a, 6), "b": round(solution.y_b, 6)},
        "rmse_px": round(solution.rmse, 3),
        "trustworthy": solution.trustworthy,
        "worst_marker_px": round(max(solution.residuals), 3),
    }
    if action == "submit":
        payload["note"] = (
            "not saved; re-run with --action commit --samples ... to keep it"
        )
    else:
        calibration.upsert(
            calibration.Record(
                client_id=client_id, client_name=name, endpoint_id=str(vmid),
                width=width, height=height,
                x_a=solution.x_a, x_b=solution.x_b,
                y_a=solution.y_a, y_b=solution.y_b,
                rmse=solution.rmse, rounds=1,
                created_at=0, updated_at=0,
            )
        )
        payload["saved"] = True
        payload["id"] = calibration.record_id(
            client_id, str(vmid), width, height
        )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return payload


def _draw_markers(lab: Any, ssh: Any, vmid: int,
                   grid: list[tuple[str, float, float]],
                   width: int, height: int) -> dict[str, Any]:
    """Capture the guest and stamp the calibration grid onto the PNG.

    The guest is NOT altered: the markers are burned into the returned
    image only, so what the agent reads back is the true display with an
    overlay. Clicking the real guest during calibration would mean moving
    the operator's pointer around without asking.
    """
    payload = _capture_png(lab, ssh, vmid, None)
    raw = Path(payload["path"]).read_bytes()
    frame_width, frame_height, rgb = png_module.decode_png(raw)
    out = bytearray(rgb)
    for _, fx, fy in grid:
        for dy in range(-3, 4):
            for dx in range(-3, 4):
                if abs(dx) + abs(dy) > 4:
                    continue
                x, y = int(fx) + dx, int(fy) + dy
                if 0 <= x < frame_width and 0 <= y < frame_height:
                    offset = (y * frame_width + x) * 3
                    out[offset:offset + 3] = b"\xff\x00\xff"
    marked = png_module.encode_png(frame_width, frame_height, bytes(out))
    path = Path(payload["path"]).with_name(path_stem(payload) + "-marked.png")
    path.write_bytes(marked)
    return {
        "path": str(path), "width": frame_width, "height": frame_height,
        "bytes": len(marked),
    }


def path_stem(payload: dict[str, Any]) -> str:
    """The capture's filename without directories or extension."""
    return Path(payload["path"]).stem


def cmd_grid(lab: Any, args: argparse.Namespace) -> dict[str, Any]:
    """Screenshot with a labelled coordinate grid burned in (`console grid`).

    Model guidance only: the untouched capture remains on disk and is the
    audit checkpoint, because a screenshot is evidence and an annotated
    one is not.
    """
    vmid = int(args.vmid)
    ssh = _make_ssh(lab.CONFIG)
    if _guest_kind(ssh, vmid) != "qemu":
        raise LabError(f"grid capture needs a qemu guest: {vmid} is not one")
    capture = _capture_png(lab, ssh, vmid, args.out)
    width, height, rgb = png_module.decode_png(
        Path(capture["path"]).read_bytes()
    )
    stepped = png_module.overlay_coordinate_grid(width, height, rgb, int(args.step))
    encoded = png_module.encode_png(width, height, stepped)
    path = Path(capture["path"]).with_suffix(".grid.png")
    path.write_bytes(encoded)
    payload = {
        "vmid": vmid, "path": str(path), "width": width, "height": height,
        "bytes": len(encoded), "step": int(args.step),
        "original": capture["path"],
        "note": "grid labels are framebuffer pixels; (0,0) is top-left",
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return payload


def cmd_burst(lab: Any, args: argparse.Namespace) -> dict[str, Any]:
    """Capture several frames in one call (`console burst`).

    For watching something that moves -- a progress bar, an installer step,
    a boot animation -- as one image instead of a sleep-then-screenshot
    loop. The frames are stitched side by side, never scaled or cropped to
    match, because a guest can change resolution mid-sequence and a
    silently rescaled frame would be evidence of something that never
    happened. Each individual frame is kept too.
    """
    vmid = int(args.vmid)
    count = int(args.frames)
    if not 2 <= count <= 30:
        raise LabError("--frames must be between 2 and 30")
    delay = float(args.interval)
    if not 0.05 <= delay <= 5.0:
        raise LabError("--interval must be between 0.05 and 5 seconds")
    ssh = _make_ssh(lab.CONFIG)
    if _guest_kind(ssh, vmid) != "qemu":
        raise LabError(f"burst capture needs a qemu guest: {vmid} is not one")
    frames: list[tuple[int, int, bytes, str]] = []
    paths: list[str] = []
    started = time.monotonic()
    for index in range(count):
        if index:
            _sleep(delay)
        capture = _capture_png(lab, ssh, vmid, None)
        width, height, rgb = png_module.decode_png(
            Path(capture["path"]).read_bytes()
        )
        elapsed = time.monotonic() - started
        frames.append((width, height, rgb, f"{elapsed:.1f}s"))
        paths.append(capture["path"])
    total_w, total_h, rgb = png_module.stitch_horizontal(frames)
    encoded = png_module.encode_png(total_w, total_h, rgb)
    path = (
        Path(args.out).expanduser()
        if args.out
        else Path(lab.STATE_ROOT) / "screens" / f"pxl-burst-{vmid}-{count}.png"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded)
    lab.audit("console-burst", vmid=vmid, frames=count, bytes=len(encoded))
    payload = {
        "vmid": vmid, "path": str(path), "frames": count,
        "interval_s": delay, "width": total_w, "height": total_h,
        "bytes": len(encoded), "frame_paths": paths,
        "note": "frames are top-left aligned, never scaled; a resolution "
                "change mid-sequence shows as a shorter frame",
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return payload

# -- command handlers ------------------------------------------------------

def cmd_screenshot(lab: Any, args: argparse.Namespace) -> dict[str, Any]:
    """Capture one guest screen as a PNG (`console screenshot`).

    Read-only on the host by construction: the screendump lands at the FIXED
    ``/tmp/pxl-shot-<vmid>.ppm`` and is overwritten next call (removing it
    would need the host-changing `rm` authorization). LXC guests have no qm
    monitor, so they are reported unsupported and nothing else happens.
    """
    vmid = int(args.vmid)
    ssh = _make_ssh(lab.CONFIG)
    if _guest_kind(ssh, vmid) != "qemu":
        payload: dict[str, Any] = {
            "vmid": vmid,
            "supported": False,
            "reason": "lxc guests have no qm monitor",
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return payload
    payload = _capture_png(lab, ssh, vmid, args.out)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return payload


def cmd_type(lab: Any, args: argparse.Namespace) -> dict[str, Any]:
    """Type text at one guest's console (`console type`).

    Mutating: gated on lease ownership before any seam call. The text is
    NEVER audited, printed, or logged -- it may be a password; only its
    length and the vmid are recorded. Each character becomes one
    `qm sendkey`, paced at most MAX_CHARS_PER_SECOND keys/second.
    """
    vmid = int(args.vmid)
    lease_id = str(args.lease)
    _require_qemu_guest(lab, lease_id, vmid)
    text = sys.stdin.read() if args.text_stdin else (args.text or "")
    # Translate first: an unmappable character sends nothing at all.
    sequence = [key_for(char) for char in text]
    if args.enter:
        sequence.append(["ret"])
    if not sequence:
        raise LabError("nothing to type: pass --text, --text-stdin, or --enter")
    rate = float(args.chars_per_second)
    if rate <= 0:
        raise LabError("--chars-per-second must be positive")
    delay = 1.0 / min(rate, MAX_CHARS_PER_SECOND)
    ssh = _make_ssh(lab.CONFIG)
    for index, names in enumerate(sequence):
        result = ssh.run(["qm", "sendkey", str(vmid), *names])
        if not result.ok:
            raise LabError(f"qm sendkey {vmid} failed: {_describe(result)}")
        if index + 1 < len(sequence):
            _sleep(delay)
    # The fact and the length, never the text.
    lab.audit("console-type", lease=lease_id, vmid=vmid, chars=len(text))
    payload: dict[str, Any] = {
        "vmid": vmid, "lease": lease_id, "sent": len(sequence),
        "chars": len(text), "enter": bool(args.enter),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return payload


def cmd_keys(lab: Any, args: argparse.Namespace) -> dict[str, Any]:
    """Send key names and combinations to one guest (`console keys`).

    Mutating: gated on lease ownership before any seam call. Each token is a
    QEMU key name (`ret`, `f2`, `spc`), a stacked combination
    (`ctrl-alt-delete`), or a single character translated through KEYMAP.
    Current ``qm sendkey`` accepts one key per call, so each name is its own
    argv; a combination stays one hyphenated argument. Only the key count
    is audited -- key names can spell typed content.
    """
    vmid = int(args.vmid)
    lease_id = str(args.lease)
    _require_qemu_guest(lab, lease_id, vmid)
    names = [name for token in args.keys for name in key_for(token)]
    ssh = _make_ssh(lab.CONFIG)
    for index, name in enumerate(names):
        result = ssh.run(["qm", "sendkey", str(vmid), name])
        if not result.ok:
            raise LabError(f"qm sendkey {vmid} failed: {_describe(result)}")
        if index + 1 < len(names):
            _sleep(1.0 / MAX_CHARS_PER_SECOND)
    lab.audit("console-keys", lease=lease_id, vmid=vmid, count=len(names))
    output: dict[str, Any] = {
        "vmid": vmid, "lease": lease_id, "sent_keys": len(names), "ok": True,
    }
    if args.screenshot_after is not None:
        _sleep(float(args.screenshot_after))
        output["screenshot"] = _capture_png(lab, ssh, vmid, None)
    print(json.dumps(output, indent=2, sort_keys=True))
    return output


def register(sub: Any, lab: Any) -> None:
    """Attach the console group: exactly `screenshot`, `type`, `keys`."""
    from .cli import _bind

    console = sub.add_parser(
        "console", help="guest screenshots and keyboard input"
    )
    commands = console.add_subparsers(dest="console_command", required=True)

    shot = commands.add_parser(
        "screenshot", help="capture the guest screen as a PNG"
    )
    shot.add_argument("--vmid", type=int, required=True)
    shot.add_argument(
        "--out", help="path for the PNG (default: state screens directory)"
    )
    shot.set_defaults(func=_bind(lab, cmd_screenshot))

    typing = commands.add_parser(
        "type", help="type text at the guest console (one key per character)"
    )
    typing.add_argument("--lease", required=True)
    typing.add_argument("--vmid", type=int, required=True)
    text = typing.add_mutually_exclusive_group()
    text.add_argument("--text", help="the text to type (never audited)")
    text.add_argument(
        "--text-stdin", action="store_true",
        help="read the text to type from stdin (never audited)",
    )
    typing.add_argument(
        "--enter", action="store_true", help="press Enter after the text"
    )
    typing.add_argument(
        "--chars-per-second", type=float, default=20.0, metavar="RATE",
        help="typing speed (bounded to at most 20 keys/second)",
    )
    typing.set_defaults(func=_bind(lab, cmd_type))

    keys = commands.add_parser(
        "keys", help="send key names/combinations via qm sendkey"
    )
    keys.add_argument("--lease", required=True)
    keys.add_argument("--vmid", type=int, required=True)
    keys.add_argument(
        "--screenshot-after", type=float, metavar="SECONDS",
        help="after sending, wait this long and include a PNG capture in "
             "the result",
    )
    keys.add_argument(
        "keys", nargs="+", metavar="KEY",
        help="QEMU key names: ret, f2, spc, ctrl-alt-delete, or characters",
    )
    keys.set_defaults(func=_bind(lab, cmd_keys))


    def _space_args(parser: Any) -> None:
        parser.add_argument(
            "--space", choices=("framebuffer", "image"), default="framebuffer",
            help="where the coordinates come from: 'framebuffer' for real "
                 "pixels (default), 'image' for points read off a "
                 "downscaled screenshot (needs a saved calibration)",
        )
        parser.add_argument(
            "--client", help="client identity for calibration lookup",
        )

    move = commands.add_parser(
        "move", help="move the guest pointer without clicking"
    )
    move.add_argument("--lease", required=True)
    move.add_argument("--vmid", type=int, required=True)
    move.add_argument("--x", type=int, required=True)
    move.add_argument("--y", type=int, required=True)
    _space_args(move)
    move.set_defaults(func=_bind(lab, cmd_move))

    click = commands.add_parser(
        "click", help="click at a point in the guest (lease-gated)"
    )
    click.add_argument("--lease", required=True)
    click.add_argument("--vmid", type=int, required=True)
    click.add_argument("--x", type=int, required=True)
    click.add_argument("--y", type=int, required=True)
    click.add_argument(
        "--button", choices=("left", "middle", "right"), default="left",
    )
    click.add_argument("--double", action="store_true", help="double-click")
    _space_args(click)
    click.add_argument(
        "--screenshot-after", type=float, metavar="SECONDS",
        help="after clicking, wait this long and include a PNG capture",
    )
    click.set_defaults(func=_bind(lab, cmd_click))

    drag = commands.add_parser(
        "drag", help="press, drag and release between two points"
    )
    drag.add_argument("--lease", required=True)
    drag.add_argument("--vmid", type=int, required=True)
    drag.add_argument("--x", type=int, required=True)
    drag.add_argument("--y", type=int, required=True)
    drag.add_argument("--to-x", type=int, required=True)
    drag.add_argument("--to-y", type=int, required=True)
    drag.add_argument(
        "--steps", type=int, default=10,
        help="interpolated move hops (1-200, default 10); guest apps that "
             "track a drag need intermediate motion events",
    )
    _space_args(drag)
    drag.add_argument(
        "--screenshot-after", type=float, metavar="SECONDS",
        help="after the drag, wait this long and include a PNG capture",
    )
    drag.set_defaults(func=_bind(lab, cmd_drag))

    calibrate = commands.add_parser(
        "calibrate",
        help="measure how this client scales the guest screen, so clicks "
             "read off a screenshot land on the right pixel",
    )
    calibrate.add_argument("--vmid", type=int, required=True)
    calibrate.add_argument(
        "--action", choices=("start", "submit", "commit", "status"),
        default="status",
        help="start lays the marker grid; submit reports the fit without "
             "saving; commit saves it; status just reports (default)",
    )
    calibrate.add_argument(
        "--samples", metavar="JSON",
        help="marker readings from the start screenshot as JSON, e.g. "
             '[{"id":"M1","x":144,"y":81}]',
    )
    calibrate.add_argument(
        "--client", help="client identity (must be specific, not generic)",
    )
    calibrate.set_defaults(func=_bind(lab, cmd_calibrate))

    grid = commands.add_parser(
        "grid", help="screenshot with a labelled coordinate grid burned in"
    )
    grid.add_argument("--vmid", type=int, required=True)
    grid.add_argument("--out", help="path for the grid PNG")
    grid.add_argument(
        "--step", type=int, default=100, metavar="PX",
        help="grid spacing in pixels (minimum 20, default 100)",
    )
    grid.set_defaults(func=_bind(lab, cmd_grid))

    burst = commands.add_parser(
        "burst", help="capture several frames in one call and stitch them"
    )
    burst.add_argument("--vmid", type=int, required=True)
    burst.add_argument(
        "--frames", type=int, default=3, metavar="N",
        help="how many frames to capture (2-30, default 3)",
    )
    burst.add_argument(
        "--interval", type=float, default=0.5, metavar="SECONDS",
        help="delay between frames (0.05-5, default 0.5)",
    )
    burst.add_argument("--out", help="path for the stitched PNG")
    burst.set_defaults(func=_bind(lab, cmd_burst))
