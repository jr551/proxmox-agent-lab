"""The controller-side GC command: ``gc install|status|uninstall`` (§F).

This is the CLI half of the host-side garbage collector: it ships the
standalone ``resources/pxl-gc.py`` script to the Proxmox host, keeps exactly
one root crontab line running it, reports the installed state, and removes
both again. Every remote action is one argv through the single ssh seam
(:mod:`proxmox_agent_lab.ssh`) -- no raw subprocess, no hand-composed remote
shell text, so the allowlist and quoting rules hold for every call here.

Crontab contract (§F): a ``# pxl-gc`` marker line directly above the
schedule line, detected and removed as a unit so re-running ``install`` never
duplicates the line. ``install`` and ``uninstall`` change the host and raise
:class:`~proxmox_agent_lab.ssh.PolicyError` before any ssh call unless
``--host-change-authorized`` was passed; ``status`` is read-only and runs
without the flag (its ``crontab -l`` is the seam's one read-only exception).

The seam object arrives as ``lab.ssh`` (the injected
:class:`~proxmox_agent_lab.ssh.SSH` in production, a ``FakeSSH`` in tests).
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

from .errors import LabError
from .ssh import PolicyError

#: Where the script must live on the host (mode 0755) -- the path the
#: crontab line invokes and the only path ``rm`` needs on uninstall.
SCRIPT_INSTALL_PATH = "/usr/local/sbin/pxl-gc"

#: Where the script is staged on the host before ``install`` copies it into
#: place; removed right after a successful install (host temp hygiene).
SCRIPT_STAGING_PATH = "/tmp/pxl-gc"

#: Host-side stamp directory the GC script uses (§F: install ensures it).
STATE_DIR = "/var/lib/pxl-gc"

#: The crontab marker and line, exactly as §F specifies. The marker is what
#: makes detect/replace/remove idempotent; the 10-minute cadence matches the
#: GC's two-consecutive-clear-runs rule.
CRON_MARKER = "# pxl-gc"
CRON_LINE = "*/10 * * * * /usr/local/sbin/pxl-gc >>/var/log/pxl-gc.log 2>&1"
CRON_LINE_AUTO = (
    "*/10 * * * * PXL_GC_AUTO_SHUTDOWN=1 "
    "/usr/local/sbin/pxl-gc >>/var/log/pxl-gc.log 2>&1"
)
CRON_BLOCK = CRON_MARKER + "\n" + CRON_LINE + "\n"

#: The host-side log the crontab line appends to; ``gc status`` tails it.
LOG_PATH = "/var/log/pxl-gc.log"

#: How many trailing log lines ``gc status`` reports.
TAIL_LINES = 20

#: The copy bundled with this package; the remote copy is checksummed
#: against it for ``status`` and re-uploaded when it differs.
BUNDLED_SCRIPT = Path(__file__).resolve().parent / "resources" / "pxl-gc.py"


class GcError(LabError):
    """A host-side GC install/status/uninstall step failed."""


def _text(raw: bytes) -> str:
    return raw.decode("utf-8", "replace")


def _bundled_script() -> bytes:
    try:
        return BUNDLED_SCRIPT.read_bytes()
    except OSError as raised:
        raise GcError(f"bundled GC script missing from the install: {raised}") from raised


def _remote_script(ssh: Any, *, host_change: bool) -> bytes | None:
    """The installed script's bytes, or None when it is absent."""
    result = ssh.run(["base64", SCRIPT_INSTALL_PATH], host_change=host_change)
    if not result.ok:
        return None
    try:
        return base64.b64decode(result.stdout, validate=False)
    except (ValueError, TypeError) as raised:
        raise GcError(
            f"could not decode {SCRIPT_INSTALL_PATH} read from the host: {raised}"
        ) from raised


def _crontab_text(ssh: Any, *, host_change: bool) -> str | None:
    """Root's crontab as text, or None when there is no crontab to read."""
    result = ssh.run(["crontab", "-l"], host_change=host_change)
    return _text(result.stdout) if result.ok else None


def _strip_own_block(text: str) -> tuple[str, bool]:
    """Remove every ``# pxl-gc`` marker line and every line invoking the GC
    script. Returns (remaining text, whether anything was removed)."""
    lines = text.splitlines()
    kept = [
        line
        for line in lines
        if line.strip() != CRON_MARKER and SCRIPT_INSTALL_PATH not in line
    ]
    body = "".join(line + "\n" for line in kept)
    return body, len(kept) != len(lines)


def _ensure_crontab(
    existing: str | None, *, block: str = CRON_BLOCK
) -> tuple[str, str]:
    """The crontab to write and what changed: added | replaced | unchanged."""
    if existing is None or not existing.strip():
        return block, "added"
    stripped, had_block = _strip_own_block(existing)
    desired = stripped + block
    if existing == desired:
        return existing, "unchanged"
    return desired, "replaced" if had_block else "added"


def _require_host_change(host_change: bool, action: str) -> None:
    if not host_change:
        raise PolicyError(
            f"refused: gc {action} changes the host and needs "
            "--host-change-authorized"
        )


def install(ssh: Any, *, host_change: bool, auto_shutdown: bool = False) -> dict:
    """Ship the script, ensure the state dir, ensure exactly one crontab line.

    Raises ``PolicyError`` before the first ssh call unless ``host_change``;
    every ssh call carries ``host_change=host_change`` so the seam re-checks
    the same authorization itself.
    """
    _require_host_change(host_change, "install")
    bundled = _bundled_script()
    digest = hashlib.sha256(bundled).hexdigest()
    remote = _remote_script(ssh, host_change=host_change)
    if remote is None:
        script_change = "written"
    elif remote == bundled:
        script_change = "unchanged"
    else:
        script_change = "updated"
    if script_change != "unchanged":
        staged = ssh.run(
            ["tee", SCRIPT_STAGING_PATH], stdin=bundled, host_change=host_change
        )
        if not staged.ok:
            raise GcError(
                f"staging the GC script on the host failed: {_text(staged.stderr)}"
            )
        copied = ssh.run(
            ["install", "-m", "0755", SCRIPT_STAGING_PATH, SCRIPT_INSTALL_PATH],
            host_change=host_change,
        )
        if not copied.ok:
            raise GcError(
                f"installing {SCRIPT_INSTALL_PATH} failed: {_text(copied.stderr)}"
            )
        # Hygiene: the staged copy is not meant to outlive the install. A
        # failed cleanup leaves only a /tmp file behind, so it is reported
        # nowhere and never fails an otherwise successful install.
        ssh.run(["rm", "-f", SCRIPT_STAGING_PATH], host_change=host_change)
    state_dir = ssh.run(
        ["install", "-d", "-m", "0755", STATE_DIR], host_change=host_change
    )
    if not state_dir.ok:
        raise GcError(f"creating {STATE_DIR} failed: {_text(state_dir.stderr)}")
    existing = _crontab_text(ssh, host_change=host_change)
    block = (
        CRON_MARKER + "\n" + CRON_LINE_AUTO + "\n"
        if auto_shutdown else CRON_BLOCK
    )
    desired, cron_change = _ensure_crontab(existing, block=block)
    if cron_change != "unchanged":
        written = ssh.run(
            ["crontab", "-"], stdin=desired.encode(), host_change=host_change
        )
        if not written.ok:
            raise GcError(
                f"installing the crontab line failed: {_text(written.stderr)}"
            )
    return {"script": script_change, "sha256": digest, "crontab": cron_change}


def status(ssh: Any) -> dict:
    """Read-only: script presence, checksum, crontab line, and the log tail.

    Four elements (§F): the installed script's presence, its checksum against
    the bundled copy, the crontab line, and the last :data:`TAIL_LINES` lines
    of :data:`LOG_PATH` read through the confined ``base64`` reader. A missing
    or unreadable log reports ``"absent"`` -- status never errors on it.
    Never requires host-change authorization: every call is ``host_change=False``.
    """
    bundled_digest = hashlib.sha256(_bundled_script()).hexdigest()
    remote = _remote_script(ssh, host_change=False)
    if remote is None:
        script, checksum = "absent", "n/a"
    else:
        script = "present"
        checksum = (
            "match"
            if hashlib.sha256(remote).hexdigest() == bundled_digest
            else "differs"
        )
    crontab_text = _crontab_text(ssh, host_change=False)
    crontab = (
        "present"
        if crontab_text and SCRIPT_INSTALL_PATH in crontab_text
        else "absent"
    )
    log_report: object = "absent"
    log_read = ssh.run(["base64", LOG_PATH], host_change=False)
    if log_read.ok:
        try:
            lines = (
                base64.b64decode(log_read.stdout, validate=False)
                .decode("utf-8", "replace")
                .splitlines()
            )
            log_report = lines[-TAIL_LINES:]
        except (ValueError, TypeError):
            log_report = "absent"
    return {
        "script": script,
        "path": SCRIPT_INSTALL_PATH,
        "checksum": checksum,
        "crontab": crontab,
        "log": log_report,
    }


def uninstall(ssh: Any, *, host_change: bool) -> dict:
    """Remove the crontab line first, then the script (idempotent).

    Guests are never touched on the way out (§F); the stamp directory stays,
    holding only a timestamp.
    """
    _require_host_change(host_change, "uninstall")
    existing = _crontab_text(ssh, host_change=host_change)
    if existing is None:
        cron_change = "absent"
    else:
        stripped, had_block = _strip_own_block(existing)
        if not had_block:
            cron_change = "absent"
        else:
            written = ssh.run(
                ["crontab", "-"], stdin=stripped.encode(), host_change=host_change
            )
            if not written.ok:
                raise GcError(
                    f"removing the crontab line failed: {_text(written.stderr)}"
                )
            cron_change = "removed"
    remote = _remote_script(ssh, host_change=host_change)
    script_change = "absent" if remote is None else "removed"
    removal = ssh.run(
        ["rm", "-f", SCRIPT_INSTALL_PATH, SCRIPT_STAGING_PATH],
        host_change=host_change,
    )
    if not removal.ok and script_change == "removed":
        raise GcError(
            f"removing {SCRIPT_INSTALL_PATH} failed: {_text(removal.stderr)}"
        )
    return {"script": script_change, "crontab": cron_change}


def cmd_install(lab: Any, args: Any) -> None:
    report = install(
        lab.ssh,
        host_change=bool(getattr(args, "host_change_authorized", False)),
        auto_shutdown=bool(
            getattr(
                getattr(getattr(lab, "CONFIG", None), "power", None),
                "auto_shutdown",
                False,
            )
        ),
    )
    print(json.dumps(report, sort_keys=True))


def cmd_status(lab: Any, args: Any) -> None:
    print(json.dumps(status(lab.ssh), sort_keys=True))


def cmd_uninstall(lab: Any, args: Any) -> None:
    report = uninstall(
        lab.ssh,
        host_change=bool(getattr(args, "host_change_authorized", False)),
    )
    print(json.dumps(report, sort_keys=True))


def register(sub: Any, lab: Any) -> None:
    gc = sub.add_parser(
        "gc", help="host-side garbage collector: install, status, uninstall"
    )
    gc_sub = gc.add_subparsers(dest="gc_command", required=True)

    install_cmd = gc_sub.add_parser(
        "install",
        help="copy the GC script to the host and ensure its crontab line",
    )
    install_cmd.add_argument(
        "--host-change-authorized",
        action="store_true",
        help="required: installs a root script, state directory and crontab line",
    )
    install_cmd.set_defaults(func=lambda args: cmd_install(lab, args))

    status_cmd = gc_sub.add_parser(
        "status", help="script presence, checksum and crontab line (read-only)"
    )
    status_cmd.set_defaults(func=lambda args: cmd_status(lab, args))

    uninstall_cmd = gc_sub.add_parser(
        "uninstall", help="remove the crontab line and the script"
    )
    uninstall_cmd.add_argument(
        "--host-change-authorized",
        action="store_true",
        help="required: removes the root script and crontab line",
    )
    uninstall_cmd.set_defaults(func=lambda args: cmd_uninstall(lab, args))
