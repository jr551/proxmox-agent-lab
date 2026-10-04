"""Agentless guest memory introspection, over the one ssh seam.

The helper (``pxl-memflow-run``) runs on the Proxmox host and reads a qemu
process's ``/proc/<pid>/mem``. This module is the policy in front of that
binary: an active lease, a qemu guest that lease owns, a running guest, a
length cap, and an audit record of the fact of the read. Guest bytes, process
names and scanned text never enter the journal.

``host-setup`` is the one host change. It installs a reviewed script through
the same ``tee``/``install`` path confinement as the GC, then runs that exact
path. Live writes (``write``, ``phys-write``) need ``--i-understand``; the
seam refuses those subcommands unless ``memory_write=True`` was passed.

There is no separate ``[memflow]`` ssh target. The lab's ``[ssh] target`` is
already root on this host. The helper is CLI-only, like ``gc``: it is not an
MCP tool.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from . import guest as guest_module
from .errors import LabError
from .ssh import (
    MAX_MEMFLOW_BREAK_TIMEOUT,
    MAX_MEMFLOW_HITS,
    MAX_MEMFLOW_NEEDLE,
    MAX_MEMFLOW_READ,
    MAX_MEMFLOW_STEPS,
    MAX_MEMFLOW_WRITE,
    MEMFLOW_HELPER,
    MEMFLOW_SETUP,
    PolicyError,
)

_NOT_FOUND = 127
_STAGING = "/tmp/pxl-memflow-setup"
_SETUP_SCRIPT = (
    Path(__file__).resolve().parent / "resources" / "memflow-host-setup.sh"
)
_STATUS_LINE = re.compile(r"status:\s*(\S+)")

# Text a stuck boot leaves in guest RAM. Matched literally; kept specific so
# ordinary log lines are not reported as a failed boot.
_BOOT_SIGNATURES: tuple[tuple[str, str, str], ...] = (
    ("linux-panic", "Kernel panic - not syncing", "linux"),
    ("linux-no-root", "VFS: Unable to mount root fs", "linux"),
    ("linux-no-init", "No working init found", "linux"),
    ("linux-init-died", "Attempted to kill init", "linux"),
    ("linux-halted", "---[ end Kernel panic", "linux"),
    ("dracut-fatal", "dracut: FATAL", "linux"),
    ("dracut-emergency", "Entering emergency mode", "linux"),
    ("fsck-fail", "fsck failed", "linux"),
    ("bios-no-boot", "No bootable device", "firmware"),
    ("bios-boot-failed", "Boot failed", "firmware"),
    ("seabios-notfound", "Could not read the boot disk", "firmware"),
    ("grub-rescue", "grub rescue>", "bootloader"),
    ("grub-no-device", "error: no such device", "bootloader"),
    ("grub-not-found", "error: file not found", "bootloader"),
    ("win-inaccessible-boot", "INACCESSIBLE_BOOT_DEVICE", "windows"),
    ("win-bootmgr-missing", "BOOTMGR is missing", "windows"),
    ("win-winload-missing", "\\Windows\\system32\\winload", "windows"),
    ("win-bsod", "A problem has been detected", "windows"),
)


def _emit(payload: dict) -> dict:
    print(json.dumps(payload, indent=2, sort_keys=True), flush=True)
    return payload


def _text(raw: bytes | str) -> str:
    if isinstance(raw, str):
        return raw
    return bytes(raw).decode("utf-8", "replace")


def _setup_script() -> bytes:
    try:
        return _SETUP_SCRIPT.read_bytes()
    except OSError as raised:
        raise LabError(
            f"bundled memflow host-setup script missing: {raised}"
        ) from raised


def _check_len(lab: Any, n: int, *, max_bytes: int = MAX_MEMFLOW_READ) -> None:
    if n <= 0 or n > max_bytes:
        raise lab.LabError(f"--len must be between 1 and {max_bytes} bytes")


def _parse_hex(lab: Any, value: str, *, max_bytes: int) -> str:
    hexbytes = value.strip().lower()
    if (
        not hexbytes
        or len(hexbytes) % 2
        or any(c not in "0123456789abcdef" for c in hexbytes)
        or len(hexbytes) // 2 > max_bytes
    ):
        raise lab.LabError(
            f"--hex must be even-length hex of 1..{max_bytes} bytes"
        )
    return hexbytes


def _owned_running_qemu(lab: Any, lease_id: str, vmid: int) -> None:
    """Refuse before any helper call unless this lease owns a running qemu."""
    lab.load_lease(lease_id)
    guest_module.require_owned(lab, lease_id, "qemu", vmid)
    result = lab.ssh.run(["qm", "status", str(vmid)], timeout=30)
    if not result.ok:
        detail = _text(result.stderr).strip()[:200]
        raise lab.LabError(
            f"VMID {vmid} is not a running qemu guest"
            + (f" ({detail})" if detail else "")
        )
    match = _STATUS_LINE.search(_text(result.stdout))
    state = match.group(1) if match else ""
    if state != "running":
        raise lab.LabError(
            f"VMID {vmid} is not running (status: {state or 'unknown'}); "
            "memflow reads live memory, so the guest must be powered on"
        )


def _helper(
    lab: Any,
    argv: list[str],
    *,
    timeout: float,
    memory_write: bool = False,
) -> Any:
    result = lab.ssh.run(
        [MEMFLOW_HELPER, *argv],
        timeout=timeout,
        memory_write=memory_write,
    )
    if result.returncode == _NOT_FOUND:
        raise lab.LabError(
            f"memflow: {MEMFLOW_HELPER} is not installed on the host. "
            "Review 'proxmox-lab memflow host-setup --print', then run it "
            "with --host-change-authorized. See docs/memflow.md."
        )
    return result


def _helper_json(
    lab: Any,
    argv: list[str],
    *,
    timeout: float,
    memory_write: bool = False,
) -> Any:
    result = _helper(lab, argv, timeout=timeout, memory_write=memory_write)
    if not result.ok:
        # stderr only: stdout of a failed read can be guest memory.
        detail = _text(result.stderr).strip()[:400]
        raise lab.LabError(
            f"memflow {' '.join(argv)} failed on the host"
            + (f": {detail}" if detail else "")
        )
    text = _text(result.stdout).strip()
    try:
        return json.loads(text) if text else {}
    except json.JSONDecodeError:
        raise lab.LabError(
            f"memflow {' '.join(argv)}: the helper did not return JSON"
        ) from None


def cmd_doctor(lab: Any, args: Any) -> dict:
    """Prove the helper is installed, and optionally that one guest reads."""
    checks: dict[str, Any] = {"ssh_reachable": bool(lab.ssh.probe())}
    if not checks["ssh_reachable"]:
        payload = _emit({
            "healthy": False,
            "checks": checks,
            "hint": "the host did not answer ssh; see 'proxmox-lab doctor'",
        })
        raise lab.LabError("memflow doctor: host is not reachable")
    try:
        checks.update(_helper_json(lab, ["doctor"], timeout=60))
    except lab.LabError as raised:
        checks["helper"] = False
        _emit({"healthy": False, "checks": checks, "hint": str(raised)})
        raise
    if getattr(args, "vmid", None):
        if not getattr(args, "lease", None):
            raise lab.LabError(
                "memflow doctor --vmid needs --lease: introspection is "
                "limited to a qemu guest that lease owns"
            )
        _owned_running_qemu(lab, str(args.lease), int(args.vmid))
        checks["guest_introspectable"] = _helper_json(
            lab, ["check", str(args.vmid)], timeout=90
        )
    healthy = checks["ssh_reachable"] and all(
        value is not False
        for value in checks.values()
        if isinstance(value, bool)
    ) and bool(checks.get("tool_installed", True))
    lab.audit("memflow-doctor", healthy=healthy, vmid=getattr(args, "vmid", None))
    payload = _emit({"healthy": healthy, "checks": checks})
    if not healthy:
        raise lab.LabError(
            "the introspection host is not fully ready; see the checks above "
            "and docs/memflow.md"
        )
    return payload


def cmd_host_setup(lab: Any, args: Any) -> dict:
    """Install the helper. A host change, refused without the flag."""
    script = _setup_script()
    if getattr(args, "print_only", False):
        print(script.decode("utf-8"), end="" if script.endswith(b"\n") else "\n")
        return {"printed": True}
    if not args.host_change_authorized:
        raise PolicyError(
            "refused: memflow host-setup changes the host and needs "
            "--host-change-authorized"
        )
    staged = lab.ssh.run(
        ["tee", _STAGING], stdin=script, host_change=True
    )
    if not staged.ok:
        raise lab.LabError(
            "staging the memflow setup script failed: "
            + _text(staged.stderr).strip()[:400]
        )
    copied = lab.ssh.run(
        ["install", "-m", "0755", _STAGING, MEMFLOW_SETUP],
        host_change=True,
    )
    if not copied.ok:
        raise lab.LabError(
            f"installing {MEMFLOW_SETUP} failed: "
            + _text(copied.stderr).strip()[:400]
        )
    lab.ssh.run(["rm", "-f", _STAGING], host_change=True)
    ran = lab.ssh.run(
        [MEMFLOW_SETUP], host_change=True, timeout=float(args.timeout)
    )
    if ran.stdout:
        print(_text(ran.stdout), end="")
    if ran.stderr and _text(ran.stderr).strip():
        print(_text(ran.stderr).strip(), file=sys.stderr)
    lab.audit("memflow-host-setup", exit_code=ran.returncode)
    if not ran.ok:
        raise lab.LabError(
            "host setup did not complete: "
            + (_text(ran.stderr) or _text(ran.stdout)).strip()[-600:]
        )
    return _emit({
        "prepared": True,
        "script": MEMFLOW_SETUP,
        "next": ["proxmox-lab memflow doctor"],
    })


def cmd_processes(lab: Any, args: Any) -> dict:
    """List the guest's processes as the hypervisor sees them."""
    _owned_running_qemu(lab, str(args.lease), int(args.vmid))
    rows = _helper_json(lab, ["process-list", str(args.vmid)], timeout=120)
    count = len(rows) if isinstance(rows, list) else None
    lab.audit(
        "memflow-processes", lease=args.lease, vmid=args.vmid, count=count
    )
    return _emit({
        "vmid": args.vmid,
        "process_count": count or 0,
        "processes": rows,
    })


def _gated_read(lab: Any, args: Any, *, helper_cmd: str, audit_event: str) -> dict:
    _check_len(lab, int(args.len))
    _owned_running_qemu(lab, str(args.lease), int(args.vmid))
    result = _helper_json(
        lab,
        [helper_cmd, str(args.vmid), str(args.addr), str(int(args.len))],
        timeout=90,
    )
    lab.audit(
        audit_event, lease=args.lease, vmid=args.vmid,
        addr=str(args.addr), length=int(args.len),
    )
    if not isinstance(result, dict):
        raise lab.LabError(f"memflow {helper_cmd}: helper JSON was not an object")
    return _emit({"vmid": args.vmid, **result})


def _gated_write(
    lab: Any, args: Any, *, helper_cmd: str, audit_event: str, understand: str
) -> dict:
    if not getattr(args, "i_understand", False):
        raise lab.LabError(understand)
    hexbytes = _parse_hex(lab, str(args.hex), max_bytes=MAX_MEMFLOW_WRITE)
    _owned_running_qemu(lab, str(args.lease), int(args.vmid))
    result = _helper_json(
        lab,
        [helper_cmd, str(args.vmid), str(args.addr), hexbytes],
        timeout=90,
        memory_write=True,
    )
    lab.audit(
        audit_event, lease=args.lease, vmid=args.vmid,
        addr=str(args.addr), length=len(hexbytes) // 2,
    )
    if not isinstance(result, dict):
        raise lab.LabError(f"memflow {helper_cmd}: helper JSON was not an object")
    return _emit({"vmid": args.vmid, **result})


def cmd_read(lab: Any, args: Any) -> dict:
    """Read bytes from the guest's kernel virtual address space."""
    return _gated_read(lab, args, helper_cmd="read", audit_event="memflow-read")


def cmd_phys_read(lab: Any, args: Any) -> dict:
    """Read bytes from guest-physical RAM. Works for any guest OS."""
    return _gated_read(
        lab, args, helper_cmd="phys-read", audit_event="memflow-phys-read"
    )


def cmd_registers(lab: Any, args: Any) -> dict:
    """Report the guest's vCPU registers via the QEMU monitor."""
    _owned_running_qemu(lab, str(args.lease), int(args.vmid))
    regs = _helper_json(lab, ["registers", str(args.vmid)], timeout=60)
    lab.audit("memflow-registers", lease=args.lease, vmid=args.vmid)
    return _emit({"vmid": args.vmid, "registers": regs})


def cmd_write(lab: Any, args: Any) -> dict:
    """Write bytes into live kernel virtual memory. Requires ``--i-understand``."""
    return _gated_write(
        lab, args, helper_cmd="write", audit_event="memflow-write",
        understand=(
            "memflow write mutates the live memory of a running guest; a "
            "wrong byte will crash or corrupt it. Re-run with --i-understand "
            "only when the user has explicitly asked to patch guest memory."
        ),
    )


def cmd_phys_write(lab: Any, args: Any) -> dict:
    """Write bytes into live guest-physical RAM. Requires ``--i-understand``."""
    return _gated_write(
        lab, args, helper_cmd="phys-write", audit_event="memflow-phys-write",
        understand=(
            "memflow phys-write injects bytes into a running guest's live "
            "RAM; a wrong address will corrupt or crash it. Re-run with "
            "--i-understand only when the user has explicitly asked to patch "
            "guest memory."
        ),
    )


def cmd_scan(lab: Any, args: Any) -> dict:
    """Search guest-physical RAM for a byte signature."""
    if not 1 <= int(args.max_hits) <= MAX_MEMFLOW_HITS:
        raise lab.LabError(f"--max-hits must be 1..{MAX_MEMFLOW_HITS}")
    hexneedle = _parse_hex(lab, str(args.hex), max_bytes=MAX_MEMFLOW_NEEDLE)
    _owned_running_qemu(lab, str(args.lease), int(args.vmid))
    result = _helper_json(
        lab,
        ["scan", str(args.vmid), hexneedle, str(int(args.max_hits))],
        timeout=float(args.timeout),
    )
    hits = result.get("hits", []) if isinstance(result, dict) else []
    lab.audit(
        "memflow-scan", lease=args.lease, vmid=args.vmid,
        needle_len=len(hexneedle) // 2, hits=len(hits),
    )
    if not isinstance(result, dict):
        raise lab.LabError("memflow scan: helper JSON was not an object")
    return _emit({"vmid": args.vmid, **result})


def cmd_dump(lab: Any, args: Any) -> dict:
    """Extract a region of guest kernel memory to a local file."""
    _check_len(lab, int(args.len))
    _owned_running_qemu(lab, str(args.lease), int(args.vmid))
    result = _helper_json(
        lab,
        ["read", str(args.vmid), str(args.addr), str(int(args.len))],
        timeout=max(90, int(args.len) // 4096),
    )
    if not isinstance(result, dict):
        raise lab.LabError("memflow dump: helper JSON was not an object")
    try:
        blob = bytes.fromhex(str(result.get("hex", "")))
    except ValueError as raised:
        raise lab.LabError(f"memflow dump: helper hex was not readable: {raised}") from None
    out = str(Path(args.out).expanduser())
    Path(out).write_bytes(blob)
    lab.audit(
        "memflow-dump", lease=args.lease, vmid=args.vmid,
        addr=str(args.addr), length=len(blob),
    )
    return _emit({
        "vmid": args.vmid,
        "addr": result.get("addr", args.addr),
        "bytes": len(blob),
        "out": out,
    })


def cmd_trace(lab: Any, args: Any) -> dict:
    """Single-step the guest via QEMU's gdbstub and disassemble each step."""
    steps = int(args.steps)
    if not 1 <= steps <= MAX_MEMFLOW_STEPS:
        raise lab.LabError(f"--steps must be 1..{MAX_MEMFLOW_STEPS}")
    _owned_running_qemu(lab, str(args.lease), int(args.vmid))
    sub = ["debug-trace", str(args.vmid), str(steps)]
    if args.over:
        sub.append("over")
    result = _helper_json(lab, sub, timeout=max(60, steps * 3))
    lab.audit(
        "memflow-trace", lease=args.lease, vmid=args.vmid,
        steps=steps, over=bool(args.over),
    )
    if not isinstance(result, dict):
        raise lab.LabError("memflow trace: helper JSON was not an object")
    return _emit({"vmid": args.vmid, **result})


def cmd_break(lab: Any, args: Any) -> dict:
    """Set a breakpoint, continue, and report where the guest stopped.

    If the address is not reached within ``--timeout``, ``hit`` is false and
    the guest is resumed. That is a result, not an error.
    """
    timeout = int(args.timeout)
    if not 1 <= timeout <= MAX_MEMFLOW_BREAK_TIMEOUT:
        raise lab.LabError(
            f"--timeout must be 1..{MAX_MEMFLOW_BREAK_TIMEOUT} seconds"
        )
    _owned_running_qemu(lab, str(args.lease), int(args.vmid))
    result = _helper_json(
        lab,
        ["debug-break", str(args.vmid), str(args.addr), str(timeout)],
        timeout=timeout + 30,
    )
    lab.audit(
        "memflow-break", lease=args.lease, vmid=args.vmid,
        addr=str(args.addr), hit=bool(result.get("hit")) if isinstance(result, dict) else None,
    )
    if not isinstance(result, dict):
        raise lab.LabError("memflow break: helper JSON was not an object")
    return _emit({"vmid": args.vmid, **result})


def _instruction_pointer(regs: Any) -> str | None:
    if not isinstance(regs, dict):
        return None
    for key in ("RIP", "EIP", "PC"):
        if key in regs:
            return str(regs[key])
    return None


def cmd_boot_diagnose(lab: Any, args: Any) -> dict:
    """Classify a stuck boot from registers and guest-physical text.

    Read-only. Two register samples tell a wedged instruction pointer from
    one that is still moving. A scan then looks for the text a failed boot
    leaves behind. The matched text is printed for the operator and is not
    audited — only the CPU state and the failure categories are.
    """
    if not 1 <= int(args.max_hits) <= MAX_MEMFLOW_HITS:
        raise lab.LabError(f"--max-hits must be 1..{MAX_MEMFLOW_HITS}")
    _owned_running_qemu(lab, str(args.lease), int(args.vmid))
    first = _helper_json(lab, ["registers", str(args.vmid)], timeout=60)
    time.sleep(max(0.0, float(args.settle)))
    second = _helper_json(lab, ["registers", str(args.vmid)], timeout=60)
    ip1, ip2 = _instruction_pointer(first), _instruction_pointer(second)
    advancing = ip1 is not None and ip2 is not None and ip1 != ip2
    if ip1 is None or ip2 is None:
        cpu_state = "unknown"
    elif advancing:
        cpu_state = "executing"
    else:
        cpu_state = "wedged"

    findings: list[dict[str, Any]] = []
    for name, text, category in _BOOT_SIGNATURES:
        result = _helper_json(
            lab,
            ["scan", str(args.vmid), text.encode().hex(), str(int(args.max_hits))],
            timeout=float(args.timeout),
        )
        hits = result.get("hits", []) if isinstance(result, dict) else []
        if hits:
            findings.append({
                "signature": name,
                "category": category,
                "text": text,
                "hits": hits,
            })
    categories = sorted({item["category"] for item in findings})
    if findings and cpu_state == "wedged":
        verdict = (
            "guest appears wedged mid-boot; RAM holds "
            + ", ".join(categories) + " boot-failure text"
        )
    elif findings:
        verdict = (
            "boot-failure text present in RAM ("
            + ", ".join(categories) + "); CPU still executing, so it may be "
            "retrying or logging past a recovered error"
        )
    elif cpu_state == "wedged":
        verdict = (
            "guest is wedged at a fixed instruction pointer but no known "
            "boot-failure text was found"
        )
    elif cpu_state == "executing":
        verdict = (
            "no boot-failure signatures found and the CPU is still executing; "
            "the guest may simply be booting slowly"
        )
    else:
        verdict = (
            "could not read vCPU registers to classify CPU state; check "
            "'memflow doctor' and that the guest is running"
        )
    lab.audit(
        "memflow-boot-diagnose", lease=args.lease, vmid=args.vmid,
        cpu_state=cpu_state, categories=categories,
        signature_count=len(findings),
    )
    return _emit({
        "vmid": args.vmid,
        "cpu_state": cpu_state,
        "instruction_pointer": ip1,
        "instruction_pointer_moved": advancing,
        "signatures_found": findings,
        "verdict": verdict,
    })


def _lease_vmid(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--lease", required=True)
    parser.add_argument("--vmid", type=int, required=True)


def register(sub: Any, lab: Any) -> None:
    from .cli import _bind

    mf = sub.add_parser(
        "memflow",
        help="read a lease-owned qemu guest's memory from the hypervisor",
    )
    mf_sub = mf.add_subparsers(dest="memflow_command", required=True)

    doctor = mf_sub.add_parser("doctor", help="prove the helper is installed")
    doctor.add_argument("--vmid", type=int)
    doctor.add_argument("--lease", help="required with --vmid")
    doctor.set_defaults(func=_bind(lab, cmd_doctor))

    setup = mf_sub.add_parser(
        "host-setup", help="install the memflow helper on the host"
    )
    setup.add_argument("--host-change-authorized", action="store_true")
    setup.add_argument(
        "--print", dest="print_only", action="store_true",
        help="print the host script instead of running it",
    )
    setup.add_argument("--timeout", type=int, default=1800)
    setup.set_defaults(func=_bind(lab, cmd_host_setup))

    procs = mf_sub.add_parser(
        "processes", help="list a running guest's processes from outside it"
    )
    _lease_vmid(procs)
    procs.set_defaults(func=_bind(lab, cmd_processes))

    read = mf_sub.add_parser("read", help="read guest kernel virtual memory")
    _lease_vmid(read)
    read.add_argument("--addr", required=True)
    read.add_argument("--len", type=int, default=64)
    read.set_defaults(func=_bind(lab, cmd_read))

    regs = mf_sub.add_parser("registers", help="report the guest vCPU registers")
    _lease_vmid(regs)
    regs.set_defaults(func=_bind(lab, cmd_registers))

    write = mf_sub.add_parser(
        "write", help="write into live guest kernel memory"
    )
    _lease_vmid(write)
    write.add_argument("--addr", required=True)
    write.add_argument("--hex", required=True)
    write.add_argument("--i-understand", dest="i_understand", action="store_true")
    write.set_defaults(func=_bind(lab, cmd_write))

    pread = mf_sub.add_parser(
        "phys-read", help="read guest-physical RAM (any guest OS)"
    )
    _lease_vmid(pread)
    pread.add_argument("--addr", required=True)
    pread.add_argument("--len", type=int, default=64)
    pread.set_defaults(func=_bind(lab, cmd_phys_read))

    pwrite = mf_sub.add_parser(
        "phys-write", help="write into guest-physical RAM"
    )
    _lease_vmid(pwrite)
    pwrite.add_argument("--addr", required=True)
    pwrite.add_argument("--hex", required=True)
    pwrite.add_argument("--i-understand", dest="i_understand", action="store_true")
    pwrite.set_defaults(func=_bind(lab, cmd_phys_write))

    scan = mf_sub.add_parser("scan", help="search guest-physical RAM for bytes")
    _lease_vmid(scan)
    scan.add_argument("--hex", required=True)
    scan.add_argument("--max-hits", type=int, default=8)
    scan.add_argument("--timeout", type=int, default=180)
    scan.set_defaults(func=_bind(lab, cmd_scan))

    dump = mf_sub.add_parser("dump", help="copy a memory region to a local file")
    _lease_vmid(dump)
    dump.add_argument("--addr", required=True)
    dump.add_argument("--len", type=int, default=4096)
    dump.add_argument("--out", required=True)
    dump.set_defaults(func=_bind(lab, cmd_dump))

    trace = mf_sub.add_parser("trace", help="single-step and disassemble")
    _lease_vmid(trace)
    trace.add_argument("--steps", type=int, default=10)
    trace.add_argument("--over", action="store_true")
    trace.set_defaults(func=_bind(lab, cmd_trace))

    brk = mf_sub.add_parser("break", help="breakpoint, then report where it stopped")
    _lease_vmid(brk)
    brk.add_argument("--addr", required=True)
    brk.add_argument("--timeout", type=int, default=15)
    brk.set_defaults(func=_bind(lab, cmd_break))

    boot = mf_sub.add_parser(
        "boot-diagnose",
        help="classify a stuck boot from registers and guest RAM text",
    )
    _lease_vmid(boot)
    boot.add_argument("--settle", type=float, default=1.0)
    boot.add_argument("--max-hits", type=int, default=4)
    boot.add_argument("--timeout", type=int, default=180)
    boot.set_defaults(func=_bind(lab, cmd_boot_diagnose))
