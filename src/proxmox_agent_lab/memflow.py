"""Agentless guest introspection with memflow.

What this is
------------
Every other way this tool reaches a guest works *from inside* it -- the guest
agent, the serial console, VNC. memflow works from *underneath*: it reads a
running guest's memory straight from the hypervisor and reconstructs the guest
OS's own view of itself (its process list, for a start). That makes it the
right tool for malware triage and rootkit hunting on a disposable VM -- a
process hidden from inside the guest still shows up here.

Why memflow, and why it is light
--------------------------------
memflow's QEMU connector reads guest memory from the `qemu-system` process's
own address space via `/proc/<pid>/mem`. There is **no patched kernel, no
kernel module, and no reboot** -- the whole reason it fits a stock Proxmox host
where live LibVMI would not. The guest-OS layer is `memflow-win32`, so process
introspection is fully supported for Windows guests; raw memory access works
for any guest, and Linux OS support is best-effort.

Why it is different from the rest of the skill
----------------------------------------------
memflow has to run resident on the hypervisor, as root, to read that `/proc`
memory. It cannot go through the Proxmox API token like everything else here,
so this one feature reaches the host over **SSH** -- a deliberately separate
trust boundary -- and expects the host to have been prepared with `memflow
host-setup` (which installs Rust, builds the `pxl-memflow` tool, and installs
the helper). Nothing here does anything until `[memflow] enabled` is true and
`ssh_host` is set.

Fail-closed and audited
-----------------------
`memflow doctor` proves each layer -- SSH reachable, the tool installed, `/proc`
memory readable, and (with a VMID) that the specific guest is introspectable.
Reads happen inside a lease and are audited by the *fact* of the read only:
guest memory can contain anything, so the process list itself never enters the
ledger. The SSH key is referenced by file path; no key material or guest data
is ever placed on a command line.
"""

from __future__ import annotations

import json
from pathlib import Path
import os
import subprocess
import sys
from typing import Any

from . import config as _config
from . import host_transport as _ht

_CONFIG = _config.get()
MF = _CONFIG.memflow

# The host SSH channel lives in host_transport and is shared with usb, netcap
# and disk; these names stay here because the channel is still configured by
# the [memflow] section and memflow's own messages refer to them.
HELPER = MF.get("helper", "pxl-memflow-run") or "pxl-memflow-run"

# ssh propagates the remote exit status: 127 means the helper is not installed.
_NOT_FOUND = 127


# --------------------------------------------------------------------------- #
# The opt-in gate. The channel itself is host_transport; this wrapper keeps
# memflow's own guidance for its commands.
# --------------------------------------------------------------------------- #

def _require_enabled(lab: Any) -> None:
    _ht.require_host_ssh(
        lab,
        "[memflow] is off. memflow introspection reaches the Proxmox host "
        "over SSH -- a separate trust boundary from the API token -- so it "
        "stays disabled until you opt in. Set [memflow] enabled = true and "
        "ssh_host, prepare the host with 'proxmox-lab memflow host-setup', "
        "then run 'proxmox-lab memflow doctor'. See docs/memflow.md.",
    )


def _check_len(lab: Any, n: int, max_bytes: int = 16 * 1024 * 1024) -> None:
    # The host-side helper allocates the full buffer as root, so an oversized
    # request could OOM it (audit 2026-08-24).
    if n <= 0 or n > max_bytes:
        raise lab.LabError(f"--len must be between 1 and {max_bytes} bytes")


def _parse_hex(lab: Any, value: str) -> str:
    hexbytes = value.strip().lower()
    if not hexbytes or len(hexbytes) % 2 or any(
        c not in "0123456789abcdef" for c in hexbytes
    ):
        raise lab.LabError("--hex must be an even-length string of hex digits")
    return hexbytes


def _helper(lab: Any, sub_argv: list[str], *, timeout: int = 90,
            ) -> subprocess.CompletedProcess:
    proc = _ht.run(lab, [HELPER, *sub_argv], timeout=timeout)
    if proc.returncode == _NOT_FOUND:
        raise lab.LabError(
            f"memflow: the '{HELPER}' helper is not installed on "
            f"{_ht.SSH_HOST}. "
            "Run 'proxmox-lab memflow host-setup --host-change-authorized' to "
            "install the memflow stack. See docs/memflow.md."
        )
    return proc


def _helper_json(lab: Any, sub_argv: list[str], *, timeout: int = 90) -> Any:
    proc = _helper(lab, sub_argv, timeout=timeout)
    text = proc.stdout.strip()
    if proc.returncode not in (0, None):
        detail = (proc.stderr or text).strip()[:400]
        raise lab.LabError(
            f"memflow {' '.join(sub_argv)} failed on the host: {detail}"
        )
    try:
        return json.loads(text) if text else {}
    except json.JSONDecodeError:
        raise lab.LabError(
            f"memflow {' '.join(sub_argv)}: the helper did not return JSON "
            f"({text[:200]!r})"
        ) from None


# --------------------------------------------------------------------------- #
# Commands.
# --------------------------------------------------------------------------- #

def _require_running_qemu(lab: Any, api: Any, vmid: int) -> None:
    status = api.call("GET", f"/nodes/{lab.NODE}/qemu/{vmid}/status/current")
    if status.get("status") != "running":
        raise lab.LabError(
            f"VMID {vmid} is not running; memflow reads live memory, so the "
            "guest must be powered on"
        )


def cmd_doctor(lab: Any, args: Any) -> None:
    """Prove every layer the read path depends on, fail-closed."""
    checks: dict[str, Any] = {"config_enabled": bool(_ht.ENABLED and _ht.SSH_HOST)}
    if not checks["config_enabled"]:
        print(json.dumps(
            {"healthy": False, "checks": checks,
             "hint": "set [memflow] enabled = true and ssh_host; see docs/memflow.md"},
            indent=2, sort_keys=True,
        ))
        raise lab.LabError("[memflow] is not configured")

    whoami = _ht.run(lab, ["id", "-un"], timeout=_ht.CONNECT_TIMEOUT + 20)
    checks["ssh_reachable"] = whoami.returncode == 0
    checks["ssh_user"] = (whoami.stdout or "").strip() or None

    checks.update(_helper_json(lab, ["doctor"], timeout=60))

    if getattr(args, "vmid", None):
        checks["guest_introspectable"] = _helper_json(
            lab, ["check", str(args.vmid)], timeout=90
        )

    healthy = checks["ssh_reachable"] and all(
        value is not False
        for value in checks.values()
        if isinstance(value, bool)
    ) and bool(checks.get("tool_installed", True))
    lab.audit("memflow-doctor", host=_ht.SSH_HOST, healthy=healthy)
    print(json.dumps({"healthy": healthy, "checks": checks},
                     indent=2, sort_keys=True))
    if not healthy:
        raise lab.LabError(
            "the introspection host is not fully ready; see the checks above "
            "and docs/memflow.md"
        )


def cmd_host_setup(lab: Any, args: Any) -> None:
    """Install the memflow stack on the host. This changes the host.

    memflow needs no patched kernel or reboot, but it does install a Rust
    toolchain and build the tool, which is a host change -- so it is gated the
    same way as every other host change here.
    """
    _require_enabled(lab)
    script = HOST_SETUP_SCRIPT
    if getattr(args, "print_only", False):
        print(script)
        return
    if not args.host_change_authorized:
        raise lab.LabError(
            "Preparing the host installs a Rust toolchain and builds the "
            f"memflow tool on {_ht.SSH_HOST} -- a host change. Re-run with "
            "--host-change-authorized once the user has asked for it. To review "
            "first, run 'proxmox-lab memflow host-setup --print'."
        )
    proc = _ht.run(lab, ["bash", "-s"], timeout=args.timeout, stdin=script)
    lab.audit("memflow-host-setup", host=_ht.SSH_HOST,
              exit_code=proc.returncode)
    if proc.stdout:
        print(proc.stdout, end="" if proc.stdout.endswith("\n") else "\n")
    if proc.stderr and proc.stderr.strip():
        print(proc.stderr.strip(), file=sys.stderr)
    if proc.returncode not in (0, None):
        raise lab.LabError(
            "host setup did not complete: "
            + (proc.stderr or proc.stdout).strip()[-600:]
        )
    print(json.dumps(
        {"host": _ht.SSH_HOST, "prepared": True,
         "next": ["proxmox-lab memflow doctor"]},
        indent=2, sort_keys=True,
    ))


def cmd_processes(lab: Any, args: Any) -> None:
    """List the guest's processes as the hypervisor sees them."""
    _require_enabled(lab)
    api = lab.ProxmoxAPI()
    lab.load_lease(args.lease)
    _require_running_qemu(lab, api, args.vmid)
    rows = _helper_json(lab, ["process-list", str(args.vmid)], timeout=120)
    lab.audit("memflow-processes", lease=args.lease, vmid=args.vmid,
              count=len(rows) if isinstance(rows, list) else None)
    print(json.dumps(
        {"vmid": args.vmid, "process_count": len(rows) if isinstance(rows, list) else 0,
         "processes": rows},
        indent=2, sort_keys=True,
    ))


def _gated_read_cmd(lab: Any, args: Any, *, helper_cmd: str, audit_event: str) -> None:
    """Shared read path: _require_enabled→len-cap→lease→running→helper→audit→print."""
    _require_enabled(lab)
    _check_len(lab, args.len)
    api = lab.ProxmoxAPI()
    lab.load_lease(args.lease)
    _require_running_qemu(lab, api, args.vmid)
    result = _helper_json(
        lab, [helper_cmd, str(args.vmid), args.addr, str(args.len)], timeout=90
    )
    lab.audit(audit_event, lease=args.lease, vmid=args.vmid,
              addr=args.addr, length=args.len)
    print(json.dumps({"vmid": args.vmid, **result}, indent=2, sort_keys=True))


def _gated_write_cmd(lab: Any, args: Any, *, helper_cmd: str, audit_event: str,
                     understand_message: str) -> None:
    """Shared write path: gated behind --i-understand, hex-parsed, then helper."""
    _require_enabled(lab)
    if not getattr(args, "i_understand", False):
        raise lab.LabError(understand_message)
    hexbytes = _parse_hex(lab, args.hex)
    api = lab.ProxmoxAPI()
    lab.load_lease(args.lease)
    _require_running_qemu(lab, api, args.vmid)
    result = _helper_json(
        lab, [helper_cmd, str(args.vmid), args.addr, hexbytes], timeout=90
    )
    lab.audit(audit_event, lease=args.lease, vmid=args.vmid,
              addr=args.addr, length=len(hexbytes) // 2)
    print(json.dumps({"vmid": args.vmid, **result}, indent=2, sort_keys=True))


def cmd_read(lab: Any, args: Any) -> None:
    """Read raw bytes from the guest's kernel virtual address space."""
    _gated_read_cmd(lab, args, helper_cmd="read", audit_event="memflow-read")


def cmd_registers(lab: Any, args: Any) -> None:
    """Report the guest's vCPU register set (via the QEMU monitor)."""
    _require_enabled(lab)
    api = lab.ProxmoxAPI()
    lab.load_lease(args.lease)
    _require_running_qemu(lab, api, args.vmid)
    regs = _helper_json(lab, ["registers", str(args.vmid)], timeout=60)
    lab.audit("memflow-registers", lease=args.lease, vmid=args.vmid)
    print(json.dumps({"vmid": args.vmid, "registers": regs},
                     indent=2, sort_keys=True))


def cmd_write(lab: Any, args: Any) -> None:
    """Write raw bytes into the guest's live kernel memory. Dangerous.

    This mutates a running kernel: the wrong byte crashes or silently
    compromises the guest. It is therefore hard-gated behind --i-understand on
    top of the lease, and the bytes themselves are never audited.
    """
    _gated_write_cmd(
        lab, args, helper_cmd="write", audit_event="memflow-write",
        understand_message=(
            "memflow write mutates the live memory of a running guest kernel; a "
            "wrong byte will crash or compromise it. Re-run with --i-understand "
            "only when the user has explicitly asked to patch guest memory."
        ),
    )


def cmd_phys_read(lab: Any, args: Any) -> None:
    """Read raw bytes from the guest's *physical* RAM (any guest OS).

    Unlike `read`, which walks the Windows kernel's virtual address space, this
    goes straight through the QEMU connector to guest-physical memory, so it
    works on Linux guests too. Useful once `scan` has located an address.
    """
    _gated_read_cmd(lab, args, helper_cmd="phys-read", audit_event="memflow-phys-read")


def cmd_phys_write(lab: Any, args: Any) -> None:
    """Write raw bytes into the guest's live physical RAM. Dangerous.

    This is RAM injection: it mutates the running guest's memory at a physical
    address (any guest OS). A wrong address corrupts the guest, so it is
    hard-gated behind --i-understand, and the bytes are never audited.
    """
    _gated_write_cmd(
        lab, args, helper_cmd="phys-write", audit_event="memflow-phys-write",
        understand_message=(
            "memflow phys-write injects bytes into a running guest's live RAM; "
            "a wrong address will corrupt or crash it. Re-run with "
            "--i-understand only when the user has explicitly asked to patch "
            "guest memory."
        ),
    )


def cmd_scan(lab: Any, args: Any) -> None:
    """Search the guest's physical RAM for a byte signature, return addresses.

    A unique needle (hex) usually resolves to a single physical address; that
    address is then the anchor for `phys-read`/`phys-write`. This is how a
    marker planted by a program -- or a known code/constant pattern -- is
    located without any guest cooperation.
    """
    _require_enabled(lab)
    api = lab.ProxmoxAPI()
    lab.load_lease(args.lease)
    _require_running_qemu(lab, api, args.vmid)
    hexneedle = _parse_hex(lab, args.hex)
    result = _helper_json(
        lab, ["scan", str(args.vmid), hexneedle, str(args.max_hits)],
        timeout=args.timeout,
    )
    lab.audit("memflow-scan", lease=args.lease, vmid=args.vmid,
              needle_len=len(hexneedle) // 2,
              hits=len(result.get("hits", [])))
    print(json.dumps({"vmid": args.vmid, **result}, indent=2, sort_keys=True))


def cmd_dump(lab: Any, args: Any) -> None:
    """Extract a region of guest memory to a local file for offline analysis."""
    _require_enabled(lab)
    _check_len(lab, args.len)
    api = lab.ProxmoxAPI()
    lab.load_lease(args.lease)
    _require_running_qemu(lab, api, args.vmid)
    result = _helper_json(
        lab, ["read", str(args.vmid), args.addr, str(args.len)],
        timeout=max(90, args.len // 4096),
    )
    blob = bytes.fromhex(result.get("hex", ""))
    out = os.path.expanduser(args.out)
    with open(out, "wb") as fh:
        fh.write(blob)
    lab.audit("memflow-dump", lease=args.lease, vmid=args.vmid,
              addr=args.addr, length=len(blob))
    print(json.dumps(
        {"vmid": args.vmid, "addr": result.get("addr", args.addr),
         "bytes": len(blob), "out": out},
        indent=2, sort_keys=True,
    ))


def cmd_ghidra_setup(lab: Any, args: Any) -> None:
    """Prepare a disposable LXC with a JDK and Ghidra for headless analysis.

    Creates the container if it does not exist, installs JDK 21 + Ghidra and the
    export script, and registers it to the lease so it is destroyed on
    lease-end. Idempotent: re-running against a ready LXC is a no-op.
    """
    _require_enabled(lab)
    api = lab.ProxmoxAPI()
    lease = lab.load_lease(args.lease)
    script = GHIDRA_SETUP_SCRIPT.replace("__LXC__", str(args.lxc))
    proc = _ht.run(lab, ["bash", "-s"], timeout=args.timeout, stdin=script)
    if proc.stdout:
        print(proc.stdout, end="" if proc.stdout.endswith("\n") else "\n")
    if proc.returncode not in (0, None):
        raise lab.LabError(
            "ghidra-setup did not complete: "
            + (proc.stderr or proc.stdout).strip()[-600:]
        )
    # Register the container so lease-end cleans it up like any lab guest.
    lab.register_resource(lease, "lxc", args.lxc, "delete", "ghidra-lab")
    lab.audit("memflow-ghidra-setup", lease=args.lease, lxc=args.lxc)
    print(json.dumps({"lxc": args.lxc, "prepared": True}, indent=2, sort_keys=True))


def cmd_analyze(lab: Any, args: Any) -> None:
    """Dump a region of guest memory and analyse it with Ghidra in the LXC."""
    _require_enabled(lab)
    _check_len(lab, args.len, max_bytes=4 * 1024 * 1024)
    api = lab.ProxmoxAPI()
    lab.load_lease(args.lease)
    _require_running_qemu(lab, api, args.vmid)
    base = args.base or args.addr
    result = _helper_json(
        lab,
        ["analyze", str(args.vmid), str(args.lxc), args.addr, str(args.len), base],
        timeout=args.timeout,
    )
    if isinstance(result, dict) and result.get("error"):
        raise lab.LabError(
            "Ghidra analysis failed on the host: "
            + str(result.get("log_tail", result["error"]))[:400]
        )
    lab.audit("memflow-analyze", lease=args.lease, vmid=args.vmid,
              lxc=args.lxc, addr=args.addr, length=args.len)
    print(json.dumps({"vmid": args.vmid, "base": base, **result},
                     indent=2, sort_keys=True))


def cmd_trace(lab: Any, args: Any) -> None:
    """Single-step the guest and return a disassembled instruction trace.

    Uses QEMU's gdbstub (no patched kernel). With --over, `call` instructions
    are stepped over (a temporary breakpoint past the call) instead of into.
    The guest is paused only for the duration of the trace and resumes on
    detach.
    """
    _require_enabled(lab)
    api = lab.ProxmoxAPI()
    lab.load_lease(args.lease)
    _require_running_qemu(lab, api, args.vmid)
    sub = ["debug-trace", str(args.vmid), str(args.steps)]
    if args.over:
        sub.append("over")
    result = _helper_json(lab, sub, timeout=max(60, args.steps * 3))
    lab.audit("memflow-trace", lease=args.lease, vmid=args.vmid,
              steps=args.steps, over=bool(args.over))
    print(json.dumps({"vmid": args.vmid, **result}, indent=2, sort_keys=True))


def cmd_break(lab: Any, args: Any) -> None:
    """Set a breakpoint, continue, and report where the guest stopped.

    Best-effort: if the address is not reached within --timeout the guest keeps
    running and `hit` is false, rather than blocking forever.
    """
    _require_enabled(lab)
    api = lab.ProxmoxAPI()
    lab.load_lease(args.lease)
    _require_running_qemu(lab, api, args.vmid)
    result = _helper_json(
        lab, ["debug-break", str(args.vmid), args.addr, str(args.timeout)],
        timeout=args.timeout + 30,
    )
    lab.audit("memflow-break", lease=args.lease, vmid=args.vmid,
              addr=args.addr, hit=bool(result.get("hit")))
    print(json.dumps({"vmid": args.vmid, **result}, indent=2, sort_keys=True))


# A guest that hangs mid-boot cannot be reached from inside -- no agent, no
# usable console -- but its RAM still holds the reason. These signatures are
# the text a stuck boot leaves behind; each is matched literally against
# guest-physical memory. Kept specific to avoid matching ordinary log text.
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


def cmd_boot_diagnose(lab: Any, args: Any) -> None:
    """Diagnose a stuck boot from the guest's RAM, without entering the guest.

    A guest that never finishes booting cannot be reached over the agent or a
    usable console, but its physical memory still holds the evidence. This
    composes two agentless primitives:

      * it samples the vCPU registers twice, a moment apart, to tell a guest
        that is wedged at a fixed instruction pointer (panic spin, HLT loop,
        firmware dead end) from one that is still executing; and
      * it scans guest-physical RAM for the text a failed boot leaves behind
        (kernel panic, missing root fs, GRUB rescue, BIOS "no bootable
        device", Windows boot errors).

    It is read-only and works on any guest OS. The matched text is not audited
    (guest RAM can contain anything); only the fact and category are.
    """
    import time as _time

    _require_enabled(lab)
    api = lab.ProxmoxAPI()
    lab.load_lease(args.lease)
    _require_running_qemu(lab, api, args.vmid)

    first = _helper_json(lab, ["registers", str(args.vmid)], timeout=60)
    _time.sleep(max(0.5, args.settle))
    second = _helper_json(lab, ["registers", str(args.vmid)], timeout=60)

    def _ip(regs: Any) -> str | None:
        if not isinstance(regs, dict):
            return None
        for key in ("RIP", "EIP", "PC"):
            if key in regs:
                return regs[key]
        return None

    ip1, ip2 = _ip(first), _ip(second)
    advancing = ip1 is not None and ip2 is not None and ip1 != ip2
    if ip1 is None or ip2 is None:
        cpu_state = "unknown"
    elif advancing:
        cpu_state = "executing"
    else:
        cpu_state = "wedged"

    findings: list[dict[str, Any]] = []
    for name, text, category in _BOOT_SIGNATURES:
        hexneedle = text.encode("utf-8", "replace").hex()
        result = _helper_json(
            lab, ["scan", str(args.vmid), hexneedle, str(args.max_hits)],
            timeout=args.timeout,
        )
        hits = result.get("hits", []) if isinstance(result, dict) else []
        if hits:
            findings.append({
                "signature": name,
                "category": category,
                "text": text,
                "hits": hits,
            })

    categories = sorted({f["category"] for f in findings})
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
            "boot-failure text was found; capture the serial console and, if "
            "it is a kernel, try 'memflow trace' at the current IP"
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

    lab.audit("memflow-boot-diagnose", lease=args.lease, vmid=args.vmid,
              cpu_state=cpu_state, categories=categories,
              signature_count=len(findings))
    print(json.dumps({
        "vmid": args.vmid,
        "cpu_state": cpu_state,
        "instruction_pointer": ip1,
        "instruction_pointer_moved": advancing,
        "signatures_found": findings,
        "verdict": verdict,
    }, indent=2, sort_keys=True))


# --------------------------------------------------------------------------- #
# Host-side assets, embedded so the feature is self-contained (as netgw does
# with its provisioning script). host-setup streams this to the host over SSH.
# --------------------------------------------------------------------------- #

HOST_SETUP_SCRIPT = (Path(__file__).parent / "resources" / "memflow-host-setup.sh").read_text()



GHIDRA_SETUP_SCRIPT = r'''#!/usr/bin/env bash
# Prepare a disposable LXC for Ghidra headless analysis. Streamed to the host
# by 'proxmox-lab memflow ghidra-setup'. Runs as root. Idempotent.
set -euo pipefail
LXC=__LXC__

if ! pct status "$LXC" >/dev/null 2>&1; then
  TMPL=$(pveam list local 2>/dev/null | awk '/debian-1[23]-standard/{print $1}' | head -1)
  if [ -z "$TMPL" ]; then
    pveam update >/dev/null 2>&1 || true
    NAME=$(pveam available --section system 2>/dev/null | awk '/debian-12-standard/{print $2}' | tail -1)
    pveam download local "$NAME" >/dev/null
    TMPL="local:vztmpl/$NAME"
  fi
  pct create "$LXC" "$TMPL" --hostname ghidra-lab --cores 4 --memory 4096 \
    --swap 512 --rootfs local-lvm:16 --net0 name=eth0,bridge=vmbr0,ip=dhcp \
    --unprivileged 1 --features nesting=1 --onboot 0 --tags codex-lab >/dev/null
fi
pct start "$LXC" >/dev/null 2>&1 || true
for i in $(seq 1 30); do
  pct exec "$LXC" -- getent hosts github.com >/dev/null 2>&1 && break; sleep 2
done

if ! pct exec "$LXC" -- test -x /opt/ghidra/support/analyzeHeadless 2>/dev/null; then
  pct exec "$LXC" -- bash -c '
    set -e; export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq || true
    apt-get install -y -qq unzip wget curl python3 >/dev/null
    if [ ! -x /opt/jdk21/bin/java ]; then
      wget -q "https://api.adoptium.net/v3/binary/latest/21/ga/linux/x64/jdk/hotspot/normal/eclipse" -O /tmp/jdk21.tgz
      mkdir -p /opt/jdk21; tar -xzf /tmp/jdk21.tgz -C /opt/jdk21 --strip-components=1
    fi
    URL=$(curl -s https://api.github.com/repos/NationalSecurityAgency/ghidra/releases/latest | grep -o "https://[^\"]*_PUBLIC_[0-9]*\.zip" | head -1)
    wget -q "$URL" -O /tmp/ghidra.zip; unzip -q /tmp/ghidra.zip -d /opt; mv /opt/ghidra_* /opt/ghidra
    sed -i "/^JAVA_HOME_OVERRIDE=/d" /opt/ghidra/support/launch.properties
    echo "JAVA_HOME_OVERRIDE=/opt/jdk21" >> /opt/ghidra/support/launch.properties
  '
fi
pct push "$LXC" /usr/local/share/pxl_export.java /root/pxl_export.java
echo "ghidra-lxc-ready $LXC"
'''


# --------------------------------------------------------------------------- #
# Registration.
# --------------------------------------------------------------------------- #

def register(sub: Any, lab: Any) -> None:
    from .cli import _bind


    mf = sub.add_parser(
        "memflow", help="agentless guest introspection with memflow (advanced)"
    )
    mf_sub = mf.add_subparsers(dest="memflow_command", required=True)

    doctor = mf_sub.add_parser(
        "doctor", help="prove the host is ready for introspection"
    )
    doctor.add_argument("--vmid", type=int,
                        help="also check this specific guest is introspectable")
    doctor.set_defaults(func=_bind(lab, cmd_doctor))

    setup = mf_sub.add_parser(
        "host-setup", help="install the memflow stack on the host (host change)"
    )
    setup.add_argument("--host-change-authorized", action="store_true")
    setup.add_argument("--print", dest="print_only", action="store_true",
                       help="print the host script instead of running it")
    setup.add_argument("--timeout", type=int, default=1800)
    setup.set_defaults(func=_bind(lab, cmd_host_setup))

    procs = mf_sub.add_parser(
        "processes", help="list a running guest's processes from outside it"
    )
    procs.add_argument("--lease", required=True,
                       help="required: this reads a running guest")
    procs.add_argument("--vmid", type=int, required=True)
    procs.set_defaults(func=_bind(lab, cmd_processes))

    read = mf_sub.add_parser(
        "read", help="read raw bytes from guest kernel memory"
    )
    read.add_argument("--lease", required=True)
    read.add_argument("--vmid", type=int, required=True)
    read.add_argument("--addr", required=True,
                      help="kernel virtual address, e.g. 0xfffff80000000000")
    read.add_argument("--len", type=int, default=64, help="number of bytes")
    read.set_defaults(func=_bind(lab, cmd_read))

    regs = mf_sub.add_parser(
        "registers", help="report the guest's vCPU registers (via QEMU monitor)"
    )
    regs.add_argument("--lease", required=True)
    regs.add_argument("--vmid", type=int, required=True)
    regs.set_defaults(func=_bind(lab, cmd_registers))

    write = mf_sub.add_parser(
        "write", help="write raw bytes into live guest memory (dangerous)"
    )
    write.add_argument("--lease", required=True)
    write.add_argument("--vmid", type=int, required=True)
    write.add_argument("--addr", required=True, help="kernel virtual address")
    write.add_argument("--hex", required=True,
                       help="bytes to write, as hex (e.g. 9090)")
    write.add_argument("--i-understand", dest="i_understand",
                       action="store_true",
                       help="required: confirm you intend to mutate live "
                            "guest memory")
    write.set_defaults(func=_bind(lab, cmd_write))

    pread = mf_sub.add_parser(
        "phys-read", help="read raw bytes from guest physical RAM (any OS)"
    )
    pread.add_argument("--lease", required=True)
    pread.add_argument("--vmid", type=int, required=True)
    pread.add_argument("--addr", required=True, help="physical address, e.g. 0x1a2b3c")
    pread.add_argument("--len", type=int, default=64, help="number of bytes")
    pread.set_defaults(func=_bind(lab, cmd_phys_read))

    pwrite = mf_sub.add_parser(
        "phys-write", help="inject raw bytes into guest physical RAM (dangerous)"
    )
    pwrite.add_argument("--lease", required=True)
    pwrite.add_argument("--vmid", type=int, required=True)
    pwrite.add_argument("--addr", required=True, help="physical address")
    pwrite.add_argument("--hex", required=True, help="bytes to write, as hex")
    pwrite.add_argument("--i-understand", dest="i_understand",
                        action="store_true",
                        help="required: confirm you intend to inject into live RAM")
    pwrite.set_defaults(func=_bind(lab, cmd_phys_write))

    scan = mf_sub.add_parser(
        "scan", help="search guest physical RAM for a byte signature"
    )
    scan.add_argument("--lease", required=True)
    scan.add_argument("--vmid", type=int, required=True)
    scan.add_argument("--hex", required=True,
                      help="needle to search for, as hex (e.g. a marker string)")
    scan.add_argument("--max-hits", type=int, default=8,
                      help="stop after this many matches")
    scan.add_argument("--timeout", type=int, default=180,
                      help="seconds; a full RAM sweep can take a while")
    scan.set_defaults(func=_bind(lab, cmd_scan))

    dump = mf_sub.add_parser(
        "dump", help="extract a region of guest memory to a local file"
    )
    dump.add_argument("--lease", required=True)
    dump.add_argument("--vmid", type=int, required=True)
    dump.add_argument("--addr", required=True, help="kernel virtual address")
    dump.add_argument("--len", type=int, default=4096, help="bytes to extract")
    dump.add_argument("--out", required=True, help="local output file")
    dump.set_defaults(func=_bind(lab, cmd_dump))

    trace = mf_sub.add_parser(
        "trace", help="single-step the guest and disassemble each instruction"
    )
    trace.add_argument("--lease", required=True)
    trace.add_argument("--vmid", type=int, required=True)
    trace.add_argument("--steps", type=int, default=10)
    trace.add_argument("--over", action="store_true",
                       help="step over calls instead of into them")
    trace.set_defaults(func=_bind(lab, cmd_trace))

    brk = mf_sub.add_parser(
        "break", help="set a breakpoint, continue, and report where it stops"
    )
    brk.add_argument("--lease", required=True)
    brk.add_argument("--vmid", type=int, required=True)
    brk.add_argument("--addr", required=True, help="breakpoint address")
    brk.add_argument("--timeout", type=int, default=15,
                     help="seconds to wait for the breakpoint before giving up")
    brk.set_defaults(func=_bind(lab, cmd_break))

    bootdiag = mf_sub.add_parser(
        "boot-diagnose",
        help="diagnose a stuck boot from guest RAM (no agent, any guest OS)",
    )
    bootdiag.add_argument("--lease", required=True)
    bootdiag.add_argument("--vmid", type=int, required=True)
    bootdiag.add_argument("--settle", type=float, default=1.0,
                          help="seconds between the two register samples "
                               "(default: 1.0)")
    bootdiag.add_argument("--max-hits", type=int, default=4,
                          help="max physical addresses to report per signature")
    bootdiag.add_argument("--timeout", type=int, default=180,
                          help="per-signature RAM scan timeout in seconds")
    bootdiag.set_defaults(func=_bind(lab, cmd_boot_diagnose))

    gsetup = mf_sub.add_parser(
        "ghidra-setup", help="prepare a disposable LXC with Ghidra headless"
    )
    gsetup.add_argument("--lease", required=True)
    gsetup.add_argument("--lxc", type=int, required=True,
                        help="VMID for the analysis container")
    gsetup.add_argument("--timeout", type=int, default=1800)
    gsetup.set_defaults(func=_bind(lab, cmd_ghidra_setup))

    analyze = mf_sub.add_parser(
        "analyze", help="dump guest code and analyse it with Ghidra in the LXC"
    )
    analyze.add_argument("--lease", required=True)
    analyze.add_argument("--vmid", type=int, required=True,
                         help="the target guest to read")
    analyze.add_argument("--lxc", type=int, required=True,
                         help="the prepared Ghidra LXC (see ghidra-setup)")
    analyze.add_argument("--addr", required=True, help="kernel virtual address")
    analyze.add_argument("--len", type=int, default=4096, help="bytes to analyse")
    analyze.add_argument("--base",
                         help="load base for the blob (defaults to --addr)")
    analyze.add_argument("--timeout", type=int, default=600)
    analyze.set_defaults(func=_bind(lab, cmd_analyze))
