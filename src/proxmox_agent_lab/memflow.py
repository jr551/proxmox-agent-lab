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
import os
import shlex
import subprocess
import sys
from typing import Any

from . import config as _config

_CONFIG = _config.get()
MF = _CONFIG.memflow

ENABLED = bool(MF.get("enabled"))
SSH_HOST = MF.get("ssh_host", "")
SSH_USER = MF.get("ssh_user", "root") or "root"
SSH_PORT = int(MF.get("ssh_port", 22) or 22)
SSH_KEY = MF.get("ssh_key", "")
SSH_OPTIONS = MF.get("ssh_options", "")
HELPER = MF.get("helper", "pxl-memflow-run") or "pxl-memflow-run"
CONNECT_TIMEOUT = int(MF.get("connect_timeout", 10) or 10)

# ssh's own failure vs a POSIX "command not found": one means the connection
# never carried a command, the other means the helper is not installed.
_SSH_FAILURE = 255
_NOT_FOUND = 127


# --------------------------------------------------------------------------- #
# The SSH channel.
# --------------------------------------------------------------------------- #

def _require_enabled(lab: Any) -> None:
    if not ENABLED or not SSH_HOST:
        raise lab.LabError(
            "[memflow] is off. memflow introspection reaches the Proxmox host "
            "over SSH -- a separate trust boundary from the API token -- so it "
            "stays disabled until you opt in. Set [memflow] enabled = true and "
            "ssh_host, prepare the host with 'proxmox-lab memflow host-setup', "
            "then run 'proxmox-lab memflow doctor'. See docs/memflow.md."
        )


def _check_len(lab: Any, n: int, max_bytes: int = 16 * 1024 * 1024) -> None:
    if n <= 0 or n > max_bytes:
        raise lab.LabError(f"--len must be between 1 and {max_bytes} bytes")


def _parse_hex(lab: Any, value: str) -> str:
    hexbytes = value.strip().lower()
    if not hexbytes or len(hexbytes) % 2 or any(
        c not in "0123456789abcdef" for c in hexbytes
    ):
        raise lab.LabError("--hex must be an even-length string of hex digits")
    return hexbytes



def _ssh_argv(remote: str) -> list[str]:
    argv = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={CONNECT_TIMEOUT}",
        "-o", "StrictHostKeyChecking=accept-new",
    ]
    if SSH_PORT != 22:
        argv += ["-p", str(SSH_PORT)]
    if SSH_KEY:
        argv += ["-i", os.path.expanduser(SSH_KEY)]
    if SSH_OPTIONS:
        argv += shlex.split(SSH_OPTIONS)
    argv.append(f"{SSH_USER}@{SSH_HOST}")
    argv.append("--")
    argv.append(remote)
    return argv


def _ssh(lab: Any, remote_argv: list[str], *, timeout: int = 60,
         stdin: str | None = None) -> subprocess.CompletedProcess:
    remote = shlex.join(remote_argv)
    try:
        proc = subprocess.run(
            _ssh_argv(remote), capture_output=True, text=True,
            timeout=timeout, input=stdin, check=False,
        )
    except FileNotFoundError:
        raise lab.LabError(
            "ssh was not found on this machine; it is required to reach the "
            "introspection host"
        ) from None
    except subprocess.TimeoutExpired:
        raise lab.LabError(
            f"memflow: no response from {SSH_HOST} within {timeout}s"
        ) from None
    if proc.returncode == _SSH_FAILURE:
        raise lab.LabError(
            f"memflow: cannot SSH to {SSH_USER}@{SSH_HOST}: "
            f"{(proc.stderr or '').strip()[:300] or 'connection failed'}. "
            "Check [memflow] ssh_host/ssh_user/ssh_key and that the key is "
            "authorised on the host."
        )
    return proc


# --------------------------------------------------------------------------- #
# The same channel, offered to the rest of the package.
#
# One subsystem outside introspection needs a file back from the host:
# 'console screenshot --via monitor', where QEMU's screendump writes the PNG on
# the node. It reuses this SSH identity rather than opening a second host
# trust boundary, and stays behind the same opt-in gate.
# --------------------------------------------------------------------------- #

def host_ssh_enabled() -> bool:
    """True when the opt-in host SSH channel is configured."""
    return bool(ENABLED and SSH_HOST)


def require_host_ssh(lab: Any) -> None:
    """Raise unless the host SSH channel is enabled and configured."""
    _require_enabled(lab)


def host_run(lab: Any, argv: list[str], *, timeout: int = 60
             ) -> subprocess.CompletedProcess:
    """Run one command on the host and return the completed process.

    The argv is joined with shlex, so no caller can inject through an
    argument. Reserved for read-only host inspection by other subsystems --
    anything that changes the host keeps its own authorization gate.
    """
    _require_enabled(lab)
    return _ssh(lab, argv, timeout=timeout)


def host_mkdir(lab: Any, directory: str, *, timeout: int = 30) -> None:
    """Create one private directory on the host."""
    _require_enabled(lab)
    proc = _ssh(lab, ["mkdir", "-p", "-m", "700", "--", directory],
                timeout=timeout)
    if proc.returncode:
        raise lab.LabError(
            f"could not create {directory} on {SSH_HOST}: "
            f"{(proc.stderr or '').strip()[:200] or proc.returncode}"
        )


def host_read_bytes(lab: Any, path: str, *, timeout: int = 120) -> bytes:
    """Read one file from the host verbatim, without decoding it as text."""
    _require_enabled(lab)
    remote = shlex.join(["cat", "--", path])
    try:
        proc = subprocess.run(
            _ssh_argv(remote), capture_output=True, timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        raise lab.LabError(
            "ssh was not found on this machine; it is required to reach the "
            "Proxmox host"
        ) from None
    except subprocess.TimeoutExpired:
        raise lab.LabError(
            f"no response from {SSH_HOST} within {timeout}s while reading a "
            "host file"
        ) from None
    if proc.returncode:
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip()[:200]
        raise lab.LabError(
            f"could not read {path} from {SSH_HOST}: "
            f"{detail or proc.returncode}"
        )
    return proc.stdout


def host_remove_empty_dir(lab: Any, directory: str, *, timeout: int = 30) -> bool:
    """Remove one directory on the host, only if it is empty.

    `rmdir` refuses a non-empty directory, so a concurrent capture in the same
    lease cannot lose its file to this cleanup.
    """
    if not host_ssh_enabled():
        return False
    try:
        proc = _ssh(lab, ["rmdir", "--", directory], timeout=timeout)
    except (OSError, lab.LabError):
        return False
    return proc.returncode == 0


def host_remove_file(lab: Any, path: str, *, timeout: int = 30) -> bool:
    """Delete one file on the host. Best effort: reports, never raises."""
    if not host_ssh_enabled():
        return False
    try:
        proc = _ssh(lab, ["rm", "-f", "--", path], timeout=timeout)
    except (OSError, lab.LabError):
        return False
    return proc.returncode == 0

def run_remote_capture(lab: Any, script: str, *, timeout: int = 60
                       ) -> tuple[int, bytes]:
    """Run a host-side tcpdump script and return (packet_count, pcap_bytes).

    The script must print ``PKTS=N`` on its own line and base64-encode the
    pcap on the remaining lines, as netcap/usb do. Failures raise LabError
    with the host's stderr (if any).
    """
    import base64

    proc = host_run(lab, ["bash", "-c", script], timeout=timeout)
    if proc.returncode not in (0, None):
        raise lab.LabError(
            f"capture failed on the host: {(proc.stderr or '').strip()[:300]}"
        )
    return decode_capture_output(proc.stdout or "")


def decode_capture_output(stdout: str) -> tuple[int, bytes]:
    """Parse PKTS= and base64 pcap from a capture script's stdout."""
    import base64

    pkts = 0
    b64: list[str] = []
    for line in stdout.splitlines():
        if line.startswith("PKTS="):
            try:
                pkts = int(line.split("=", 1)[1] or 0)
            except ValueError:
                pkts = 0
        else:
            b64.append(line)
    data = base64.b64decode("".join(b64) or "")
    return pkts, data
def _helper(lab: Any, sub_argv: list[str], *, timeout: int = 90,
            ) -> subprocess.CompletedProcess:
    proc = _ssh(lab, [HELPER, *sub_argv], timeout=timeout)
    if proc.returncode == _NOT_FOUND:
        raise lab.LabError(
            f"memflow: the '{HELPER}' helper is not installed on {SSH_HOST}. "
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
    checks: dict[str, Any] = {"config_enabled": bool(ENABLED and SSH_HOST)}
    if not checks["config_enabled"]:
        print(json.dumps(
            {"healthy": False, "checks": checks,
             "hint": "set [memflow] enabled = true and ssh_host; see docs/memflow.md"},
            indent=2, sort_keys=True,
        ))
        raise lab.LabError("[memflow] is not configured")

    whoami = _ssh(lab, ["id", "-un"], timeout=CONNECT_TIMEOUT + 20)
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
    lab.audit("memflow-doctor", host=SSH_HOST, healthy=healthy, sync=False)
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
            f"memflow tool on {SSH_HOST} -- a host change. Re-run with "
            "--host-change-authorized once the user has asked for it. To review "
            "first, run 'proxmox-lab memflow host-setup --print'."
        )
    proc = _ssh(lab, ["bash", "-s"], timeout=args.timeout, stdin=script)
    lab.audit("memflow-host-setup", host=SSH_HOST,
              exit_code=proc.returncode, sync=False)
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
        {"host": SSH_HOST, "prepared": True,
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
              count=len(rows) if isinstance(rows, list) else None, sync=False)
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
              addr=args.addr, length=args.len, sync=False)
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
              addr=args.addr, length=len(hexbytes) // 2, sync=False)
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
    lab.audit("memflow-registers", lease=args.lease, vmid=args.vmid, sync=False)
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
              hits=len(result.get("hits", [])), sync=False)
    print(json.dumps({"vmid": args.vmid, **result}, indent=2, sort_keys=True))


def cmd_dump(lab: Any, args: Any) -> None:
    """Extract a region of guest memory to a local file for offline analysis."""
    _require_enabled(lab)
    _check_len(lab, args.len)
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
              addr=args.addr, length=len(blob), sync=False)
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
    lab.load_lease(args.lease)
    script = GHIDRA_SETUP_SCRIPT.replace("__LXC__", str(args.lxc))
    proc = _ssh(lab, ["bash", "-s"], timeout=args.timeout, stdin=script)
    if proc.stdout:
        print(proc.stdout, end="" if proc.stdout.endswith("\n") else "\n")
    if proc.returncode not in (0, None):
        raise lab.LabError(
            "ghidra-setup did not complete: "
            + (proc.stderr or proc.stdout).strip()[-600:]
        )
    # Register the container so lease-end cleans it up like any lab guest.
    try:
        lab.register_resource(lab.load_lease(args.lease), "lxc", args.lxc,
                              "delete", "ghidra-lab")
    except Exception:  # pragma: no cover - best-effort registration
        pass
    lab.audit("memflow-ghidra-setup", lease=args.lease, lxc=args.lxc, sync=False)
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
              lxc=args.lxc, addr=args.addr, length=args.len, sync=False)
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
              steps=args.steps, over=bool(args.over), sync=False)
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
              addr=args.addr, hit=bool(result.get("hit")), sync=False)
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
              signature_count=len(findings), sync=False)
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

HOST_SETUP_SCRIPT = r'''#!/usr/bin/env bash
# Prepare a Proxmox host for memflow introspection. Streamed to the host by
# 'proxmox-lab memflow host-setup'. Runs as root.
#
# Installs a Rust toolchain (if absent), builds the pxl-memflow tool
# (memflow + memflow-qemu + memflow-win32), and installs it alongside the
# pxl-memflow-run helper. No kernel changes, no reboot.
set -euo pipefail

say()  { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
warn() { printf '!  %s\n' "$*" >&2; }
die()  { printf 'x  %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run this as root on the Proxmox host"
command -v qemu-system-x86_64 >/dev/null 2>&1 || \
  warn "qemu-system-x86_64 not found; is this the hypervisor?"

step "Rust toolchain"
if ! command -v cargo >/dev/null 2>&1 && [ ! -x "$HOME/.cargo/bin/cargo" ]; then
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- -y --profile minimal --default-toolchain stable
fi
. "$HOME/.cargo/env"
say "  cargo $(cargo --version | awk '{print $2}')"

step "build dependencies"
export DEBIAN_FRONTEND=noninteractive
# A Proxmox enterprise repo without a subscription makes 'update' exit non-zero
# even though the Debian repos we need refreshed fine; do not abort on it.
apt-get update -qq || warn "apt-get update reported errors (unsubscribed enterprise repo?); continuing"
apt-get install -y -qq pkg-config build-essential python3-capstone >/dev/null 2>&1 \
  || warn "apt build deps install reported errors; continuing"

step "build pxl-memflow"
mkdir -p /root/pxl-memflow/src
cat > /root/pxl-memflow/Cargo.toml <<'TOML'
[package]
name = "pxl-memflow"
version = "0.1.0"
edition = "2021"

[dependencies]
memflow = "0.2"
memflow-qemu = "0.2"
memflow-win32 = "0.2"
serde_json = "1"
hex = "0.4"

[[bin]]
name = "pxl-memflow"
path = "src/main.rs"
TOML
cat > /root/pxl-memflow/src/main.rs <<'RUST'
// Agentless introspection of a running QEMU guest via memflow. Reads/writes
// the guest's kernel virtual memory and lists Windows processes. The guest is
// addressed by its QEMU pid (robust on Proxmox, which launches QEMU directly
// rather than through libvirt).
use memflow::prelude::v1::*;
use memflow_win32::prelude::v1::*;
use serde_json::json;
use std::env;

fn parse_addr(s: &str) -> u64 {
    let s = s.trim();
    if let Some(h) = s.strip_prefix("0x") {
        u64::from_str_radix(h, 16).unwrap_or(0)
    } else {
        s.parse().unwrap_or(0)
    }
}

fn connect(target: &str) -> Result<impl PhysicalMemory> {
    let args = ConnectorArgs::new(Some(target), Default::default(), None);
    memflow_qemu::create_connector(&args)
}

fn build(target: &str) -> Result<impl MemoryView + Os> {
    // Build the connector inline here (not via connect()): the Win32 cache
    // layers need the concrete connector's full trait set, which an opaque
    // `impl PhysicalMemory` return would erase.
    let args = ConnectorArgs::new(Some(target), Default::default(), None);
    let connector = memflow_qemu::create_connector(&args)?;
    Win32Kernel::builder(connector).build_default_caches().build()
}

fn run(args: &[String]) -> Result<()> {
    let cmd = args.get(0).map(String::as_str).unwrap_or("");
    let target = args.get(1).map(String::as_str).unwrap_or("");
    // Physical-memory commands go straight through the QEMU connector with no
    // OS layer, so raw RAM read/write/scan works on any guest, not just
    // Windows -- this is the path a cert-pinning override rides on.
    match cmd {
        "phys-read" => {
            let mut conn = connect(target)?;
            let mut view = conn.phys_view();
            let addr = parse_addr(&args[2]);
            let len: usize = args[3].parse().unwrap_or(0);
            let mut buf = vec![0u8; len];
            view.read_raw_into(Address::from(addr), &mut buf).data_part()?;
            println!("{}", json!({
                "addr": format!("{:#x}", addr), "len": len, "hex": hex::encode(&buf)
            }));
            return Ok(());
        }
        "phys-write" => {
            let mut conn = connect(target)?;
            let mut view = conn.phys_view();
            let addr = parse_addr(&args[2]);
            let bytes = hex::decode(args[3].trim())
                .map_err(|_| Error(ErrorOrigin::Other, ErrorKind::Configuration))?;
            view.write_raw(Address::from(addr), &bytes).data_part()?;
            println!("{}", json!({
                "addr": format!("{:#x}", addr), "written": bytes.len()
            }));
            return Ok(());
        }
        "phys-scan" => {
            let mut conn = connect(target)?;
            let max = conn.metadata().max_address.to_umem();
            let mut view = conn.phys_view();
            let needle = hex::decode(args[2].trim())
                .map_err(|_| Error(ErrorOrigin::Other, ErrorKind::Configuration))?;
            let maxhits: usize = args.get(3).and_then(|s| s.parse().ok()).unwrap_or(8);
            let chunk: usize = 4 * 1024 * 1024;
            let overlap = needle.len().saturating_sub(1);
            let n = needle.len();
            let mut hits: Vec<String> = Vec::new();
            let mut base: u64 = 0;
            while (base as u128) < (max as u128) && hits.len() < maxhits {
                let want = std::cmp::min(
                    (chunk + overlap) as u128, (max as u128) - base as u128) as usize;
                let mut b = vec![0u8; want];
                // Physical RAM has holes; a failed window is skipped, not fatal.
                let _ = view.read_raw_into(Address::from(base), &mut b);
                if b.len() >= n {
                    let mut i = 0;
                    while i + n <= b.len() {
                        if &b[i..i + n] == needle.as_slice() {
                            hits.push(format!("{:#x}", base + i as u64));
                            if hits.len() >= maxhits { break; }
                        }
                        i += 1;
                    }
                }
                base += chunk as u64;
            }
            println!("{}", json!({"needle_len": n, "hits": hits}));
            return Ok(());
        }
        _ => {}
    }
    let mut kernel = build(target)?;
    match cmd {
        "check" => println!("{}", json!({"introspectable": true})),
        "process-list" => {
            let list = kernel.process_info_list()?;
            let rows: Vec<_> = list.iter()
                .map(|p| json!({"pid": p.pid, "name": p.name.to_string()}))
                .collect();
            println!("{}", serde_json::to_string(&rows).unwrap());
        }
        "read" => {
            let addr = parse_addr(&args[2]);
            let len: usize = args[3].parse().unwrap_or(0);
            let mut buf = vec![0u8; len];
            kernel.read_raw_into(Address::from(addr), &mut buf).data_part()?;
            println!("{}", json!({
                "addr": format!("{:#x}", addr), "len": len,
                "hex": hex::encode(&buf)
            }));
        }
        "write" => {
            let addr = parse_addr(&args[2]);
            let bytes = hex::decode(args[3].trim())
                .map_err(|_| Error(ErrorOrigin::Other, ErrorKind::Configuration))?;
            kernel.write_raw(Address::from(addr), &bytes).data_part()?;
            println!("{}", json!({
                "addr": format!("{:#x}", addr), "written": bytes.len()
            }));
        }
        _ => {
            eprintln!("usage: pxl-memflow <check|process-list|read|write|phys-read|phys-write|phys-scan> <target> [args]");
            std::process::exit(64);
        }
    }
    Ok(())
}

fn main() {
    let argv: Vec<String> = env::args().skip(1).collect();
    if let Err(e) = run(&argv) {
        eprintln!("{}", e);
        std::process::exit(3);
    }
}
RUST
( cd /root/pxl-memflow && cargo build --release >/dev/null )
install -m 0755 /root/pxl-memflow/target/release/pxl-memflow /usr/local/bin/pxl-memflow
say "  installed /usr/local/bin/pxl-memflow"

step "pxl-memflow-run helper"
install -m 0755 /dev/stdin /usr/local/bin/pxl-memflow-run <<'HELP'
#!/usr/bin/env bash
# Map a Proxmox VMID to its QEMU pid, then introspect it with pxl-memflow.
# 'registers' reads the vCPU state from the QEMU monitor (memflow's /proc
# connector sees RAM only, not registers).
set -euo pipefail
QMP_DIR=/var/run/qemu-server
cmd="${1:-}"; vmid="${2:-}"
pid_for(){ local f="$QMP_DIR/$1.pid"; [ -f "$f" ] && cat "$f" || return 1; }
need_vmid(){ [ -n "$vmid" ] || { echo "vmid required" >&2; exit 64; }; }
case "$cmd" in
  doctor)
    have_bin=false; command -v pxl-memflow >/dev/null 2>&1 && have_bin=true
    proc_ok=false; [ -r /proc/self/mem ] && proc_ok=true
    printf '{"tool_installed": %s, "proc_readable": %s}\n' "$have_bin" "$proc_ok"
    ;;
  check|process-list)
    need_vmid; pid=$(pid_for "$vmid") || { echo "VMID $vmid is not running" >&2; exit 3; }
    exec pxl-memflow "$cmd" "$pid"
    ;;
  read)
    need_vmid; pid=$(pid_for "$vmid") || { echo "VMID $vmid is not running" >&2; exit 3; }
    exec pxl-memflow read "$pid" "${3:?addr}" "${4:?len}"
    ;;
  write)
    need_vmid; pid=$(pid_for "$vmid") || { echo "VMID $vmid is not running" >&2; exit 3; }
    exec pxl-memflow write "$pid" "${3:?addr}" "${4:?hex}"
    ;;
  phys-read)
    need_vmid; pid=$(pid_for "$vmid") || { echo "VMID $vmid is not running" >&2; exit 3; }
    exec pxl-memflow phys-read "$pid" "${3:?addr}" "${4:?len}"
    ;;
  phys-write)
    need_vmid; pid=$(pid_for "$vmid") || { echo "VMID $vmid is not running" >&2; exit 3; }
    exec pxl-memflow phys-write "$pid" "${3:?addr}" "${4:?hex}"
    ;;
  scan)
    need_vmid; pid=$(pid_for "$vmid") || { echo "VMID $vmid is not running" >&2; exit 3; }
    exec pxl-memflow phys-scan "$pid" "${3:?hex}" "${4:-8}"
    ;;
  registers)
    need_vmid; pid_for "$vmid" >/dev/null || { echo "VMID $vmid is not running" >&2; exit 3; }
    echo "info registers" | qm monitor "$vmid" 2>/dev/null | python3 -c '
import re, sys, json
regs = {}
for m in re.finditer(r"\b([A-Z][A-Z0-9]{1,4})\s*=\s*([0-9a-fA-F]+)", sys.stdin.read()):
    regs.setdefault(m.group(1), m.group(2))
print(json.dumps(regs))
'
    ;;
  debug-trace)
    need_vmid; pid_for "$vmid" >/dev/null || { echo "VMID $vmid is not running" >&2; exit 3; }
    port=$((23000 + vmid % 1000))
    exec pxl-gdb "$port" "$vmid" trace "${3:?steps}" "${4:-}"
    ;;
  debug-break)
    need_vmid; pid_for "$vmid" >/dev/null || { echo "VMID $vmid is not running" >&2; exit 3; }
    port=$((23000 + vmid % 1000))
    exec pxl-gdb "$port" "$vmid" break "${3:?addr}" "${4:-15}"
    ;;
  analyze)
    # analyze <target_vmid> <lxc_vmid> <addr> <len> <base>
    need_vmid; pid_for "$vmid" >/dev/null || { echo "VMID $vmid is not running" >&2; exit 3; }
    exec pxl-ghidra "$vmid" "${4:?addr}" "${5:?len}" "${6:?base}" "${3:?lxc}"
    ;;
  *) echo "usage: pxl-memflow-run {doctor|check|process-list|read|write|phys-read|phys-write|scan|registers|debug-trace|debug-break|analyze} [vmid] [args]" >&2; exit 64;;
esac
HELP
say "  installed /usr/local/bin/pxl-memflow-run"

step "pxl-gdb helper (live stepping via the QEMU gdbstub)"
install -m 0755 /dev/stdin /usr/local/bin/pxl-gdb <<'PYEOF'
#!/usr/bin/env python3
"""Minimal GDB-remote (RSP) client for QEMU's built-in gdbstub.

Drives single-step (into), step-over, breakpoints and continue against a
running guest, so we get live debugging with no patched kernel. Enables the
stub via the QEMU monitor if it is not already listening. Prints JSON.
"""
import json, re, socket, subprocess, sys, time

RIP_REGNUM = 0x10  # x86-64 gdb regnum for RIP

def monitor(vmid, cmd):
    subprocess.run(["qm", "monitor", str(vmid)], input=cmd + "\n",
                   text=True, capture_output=True, timeout=15)

class RSP:
    def __init__(self, port):
        self.s = socket.create_connection(("127.0.0.1", port), timeout=10)
        self.s.settimeout(10)
    def _send(self, data):
        cksum = sum(data.encode()) & 0xff
        self.s.sendall(b"$" + data.encode() + b"#" + b"%02x" % cksum)
        self.s.recv(1)
    def _recv(self):
        buf = b""
        while True:
            c = self.s.recv(1)
            if not c:
                break
            if c == b"$":
                buf = b""
            elif c == b"#":
                self.s.recv(2)
                break
            else:
                buf += c
        self.s.sendall(b"+")
        return buf.decode(errors="replace")
    def cmd(self, data):
        self._send(data)
        return self._recv()
    def read_reg(self, num):
        r = self.cmd("p%x" % num)
        return int.from_bytes(bytes.fromhex(r), "little") \
            if re.fullmatch(r"[0-9a-fA-F]+", r) else None
    def read_mem(self, addr, length):
        r = self.cmd("m%x,%x" % (addr, length))
        return bytes.fromhex(r) if re.fullmatch(r"[0-9a-fA-F]+", r) else b""
    def step(self):
        return self.cmd("s")
    def set_bp(self, addr):
        return self.cmd("Z0,%x,1" % addr)
    def clr_bp(self, addr):
        return self.cmd("z0,%x,1" % addr)
    def cont(self, timeout=10):
        self._send("c")
        self.s.settimeout(timeout)
        try:
            return self._recv()
        except socket.timeout:
            return ""
        finally:
            self.s.settimeout(10)
    def detach(self):
        try:
            self.cmd("D")
        except Exception:
            pass
        self.s.close()

def disasm_one(md, code, addr):
    for insn in md.disasm(code, addr):
        return insn.mnemonic, insn.op_str, insn.size
    return "?", "", 1

def qemu_pid(vmid):
    with open("/var/run/qemu-server/%s.pid" % vmid) as fh:
        return fh.read().strip()

def listening_ports(pid):
    out = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True).stdout
    ports = []
    for line in out.splitlines():
        if ("pid=%s," % pid) not in line:
            continue
        parts = line.split()
        if len(parts) >= 4:
            m = re.search(r":(\d+)$", parts[3])
            if m:
                ports.append(int(m.group(1)))
    return sorted(set(ports))

def is_rsp(port):
    # QEMU allows only one gdbstub, on a port we may not have chosen, so probe
    # each of the qemu process's listeners: the gdbstub answers '?' with a stop
    # packet, nothing else will.
    try:
        r = RSP(port); r.s.settimeout(2)
        reply = r.cmd("?"); r.s.close()
        return bool(reply) and reply[0] in "STWXsO"
    except OSError:
        return False

def connect_stub(vmid, hint):
    pid = qemu_pid(vmid)
    for p in listening_ports(pid):
        if is_rsp(p):
            return RSP(p)
    monitor(vmid, "gdbserver tcp::%d" % hint); time.sleep(1)
    return RSP(hint)

def main():
    import capstone
    port = int(sys.argv[1]); vmid = sys.argv[2]; op = sys.argv[3]
    rsp = connect_stub(vmid, port)
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    try:
        if op == "trace":
            n = int(sys.argv[4]); over = (len(sys.argv) > 5 and sys.argv[5] == "over")
            steps = []
            for _ in range(n):
                rip = rsp.read_reg(RIP_REGNUM)
                mn, ops, size = disasm_one(md, rsp.read_mem(rip, 16), rip)
                steps.append({"rip": "%#x" % rip, "insn": (mn + " " + ops).strip()})
                if over and mn.startswith("call"):
                    ret = rip + size
                    rsp.set_bp(ret); rsp.cont(); rsp.clr_bp(ret)
                else:
                    rsp.step()
            print(json.dumps({"steps": steps}))
        elif op == "break":
            addr = int(sys.argv[4], 0)
            timeout = int(sys.argv[5]) if len(sys.argv) > 5 else 15
            rsp.set_bp(addr); rsp.cont(timeout)
            rip = rsp.read_reg(RIP_REGNUM); rsp.clr_bp(addr)
            print(json.dumps({"stopped_at": "%#x" % rip, "hit": rip == addr}))
        else:
            print(json.dumps({"error": "unknown op"})); sys.exit(64)
    finally:
        rsp.detach()

if __name__ == "__main__":
    main()
PYEOF
say "  installed /usr/local/bin/pxl-gdb"

step "pxl-ghidra helper + export script (memory -> Ghidra in an LXC)"
install -m 0755 /dev/stdin /usr/local/bin/pxl-ghidra <<'SH'
#!/usr/bin/env bash
# pxl-ghidra <target_vmid> <addr> <len> <base> <lxc_vmid>
# Read the target guest's memory, load the blob into the Ghidra LXC, run
# analyzeHeadless, and print the exported JSON. The LXC is prepared by
# 'proxmox-lab memflow ghidra-setup'.
set -euo pipefail
tgt="$1"; addr="$2"; len="$3"; base="$4"; lxc="$5"
JH=/opt/jdk21
pid=$(cat "/var/run/qemu-server/$tgt.pid")
hex=$(pxl-memflow read "$pid" "$addr" "$len" | python3 -c 'import json,sys;print(json.load(sys.stdin)["hex"])')
tmp=$(mktemp)
python3 -c "import sys;open('$tmp','wb').write(bytes.fromhex('$hex'))"
pct push "$lxc" "$tmp" /root/blob.bin >/dev/null; rm -f "$tmp"
pct exec "$lxc" -- bash -c "rm -rf /root/gproj /root/out.json; mkdir -p /root/gproj" >/dev/null 2>&1 || true
pct exec "$lxc" -- env JAVA_HOME="$JH" PATH="$JH/bin:/usr/bin:/bin" \
  /opt/ghidra/support/analyzeHeadless /root/gproj proj \
  -import /root/blob.bin -processor x86:LE:64:default \
  -loader BinaryLoader -loader-baseAddr "$base" \
  -scriptPath /root -postScript pxl_export.java -deleteProject \
  >/tmp/ghidra-$lxc.log 2>&1 || true
pct exec "$lxc" -- cat /root/out.json 2>/dev/null || {
  echo "{\"error\":\"no analysis output\",\"log_tail\":\"$(tail -3 /tmp/ghidra-$lxc.log | tr '\n' ' ' | tr -cd '[:print:] ')\"}"; exit 3;
}
SH
say "  installed /usr/local/bin/pxl-ghidra"
# The Ghidra headless export script (Java: needs no PyGhidra), staged on the
# host so ghidra-setup can push it into the analysis LXC.
cat > /usr/local/share/pxl_export.java <<'JAVA'
// Ghidra headless post-script: export functions + first instructions to JSON.
import ghidra.app.script.GhidraScript;
import ghidra.program.model.listing.*;
import java.io.PrintWriter;
public class pxl_export extends GhidraScript {
  String esc(String s){ return s.replace("\\","\\\\").replace("\"","\\\""); }
  public void run() throws Exception {
    StringBuilder sb = new StringBuilder();
    sb.append("{\"functions\":[");
    FunctionManager fm = currentProgram.getFunctionManager();
    FunctionIterator fi = fm.getFunctions(true);
    boolean first=true; int fcount=0;
    while (fi.hasNext()) {
      Function f = fi.next();
      if(!first) sb.append(","); first=false;
      sb.append("{\"name\":\""+esc(f.getName())+"\",\"entry\":\""+f.getEntryPoint()
        +"\",\"size\":"+f.getBody().getNumAddresses()+"}");
      fcount++;
    }
    sb.append("],\"function_count\":"+fcount+",\"instructions\":[");
    Listing listing = currentProgram.getListing();
    InstructionIterator it = listing.getInstructions(true);
    int n=0; first=true;
    while(it.hasNext() && n<300){
      Instruction ins=it.next();
      if(!first) sb.append(","); first=false;
      sb.append("{\"addr\":\""+ins.getAddress()+"\",\"text\":\""+esc(ins.toString())+"\"}");
      n++;
    }
    sb.append("],\"instruction_count_shown\":"+n+"}");
    PrintWriter pw=new PrintWriter("/root/out.json"); pw.print(sb.toString()); pw.close();
  }
}
JAVA
say "  staged /usr/local/share/pxl_export.java"

step "Done"
say "  Verify from your controller: proxmox-lab memflow doctor"
'''


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
