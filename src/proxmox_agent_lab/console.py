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
    (`ctrl-alt-delete`), or a single character translated through KEYMAP;
    all names go out in one `qm sendkey <vmid> <key...>`. Only the key count
    is audited -- key names can spell typed content.
    """
    vmid = int(args.vmid)
    lease_id = str(args.lease)
    _require_qemu_guest(lab, lease_id, vmid)
    names = [name for token in args.keys for name in key_for(token)]
    ssh = _make_ssh(lab.CONFIG)
    result = ssh.run(["qm", "sendkey", str(vmid), *names])
    if not result.ok:
        raise LabError(f"qm sendkey {vmid} failed: {_describe(result)}")
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
