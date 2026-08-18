"""Offline guest-disk debugging: boot structures and filesystem read/write.

Two jobs a running-guest tool cannot do:

* **Boot debugging** -- read a disk's MBR/GPT partition tables and the ESP,
  which is where a guest that reaches the firmware but never loads an OS has
  gone wrong. `disk boot-info` parses those structures either from a local
  image (``--image``) or from a *stopped* guest's disk (``--vmid``).
* **Offline filesystem access** -- list, read and (carefully) write files in a
  powered-off guest's filesystem, for repairing a broken boot config, dropping
  in a driver, or pulling a log out of a guest that will not boot. `disk ls`,
  `disk read`, `disk write`.

Local ``--image`` parsing is pure and needs nothing. The guest paths reuse the
**memflow SSH-to-host trust boundary** (a separate, root-on-host channel), and
run libguestfs on the host, so they require ``[memflow] enabled`` and a host
prepared with ``disk host-setup``. Every guest operation refuses a running
guest: touching a mounted filesystem underneath a live kernel corrupts it.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

from . import bootstruct

# Streamed to the host over the memflow SSH channel. Resolves a VMID's first
# disk to a host path and performs one read-only or write operation with
# libguestfs. Guest must be stopped; the script re-checks on the host.
_HOST_DISK_SCRIPT = r'''#!/usr/bin/env bash
set -euo pipefail
op="${1:?op}"; vmid="${2:?vmid}"
command -v guestfish >/dev/null 2>&1 || { echo "guestfish not installed; run 'proxmox-lab disk host-setup'" >&2; exit 3; }
state=$(qm status "$vmid" 2>/dev/null | awk '{print $2}')
[ "$state" = "running" ] && { echo "VMID $vmid is running; stop it before offline disk access" >&2; exit 4; }
volid=$(qm config "$vmid" 2>/dev/null | sed -n -E 's/^(scsi0|virtio0|sata0|ide0|efidisk0): ([^,]+).*/\2/p' | head -n1)
[ -n "$volid" ] || { echo "no primary disk found for VMID $vmid" >&2; exit 5; }
disk=$(pvesm path "$volid" 2>/dev/null) || { echo "cannot resolve $volid" >&2; exit 6; }
[ -e "$disk" ] || { echo "resolved disk $disk does not exist" >&2; exit 6; }
case "$op" in
  boot-sectors)
    dd if="$disk" bs=512 count="${3:-34}" 2>/dev/null | base64 -w0 ;;
  ls)
    virt-ls -a "$disk" -m "${3:?mount}" "${4:-/}" ;;
  cat)
    virt-cat -a "$disk" -m "${3:?mount}" "${4:?path}" | base64 -w0 ;;
  *) echo "unknown op $op" >&2; exit 64 ;;
esac
'''

_HOST_SETUP_SCRIPT = r'''#!/usr/bin/env bash
set -euo pipefail
echo "installing libguestfs-tools for offline guest disk access..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends libguestfs-tools >/dev/null
install -m 0755 /dev/stdin /usr/local/bin/pxl-disk <<'SCRIPT'
%SCRIPT%
SCRIPT
echo "installed /usr/local/bin/pxl-disk"
'''


def _build_write_script(payload_b64: str) -> str:
    """A one-shot host write script with the base64 body in a quoted heredoc.

    vmid/mount/dest arrive as ``$1/$2/$3`` (never interpolated); only the
    base64 payload -- a safe ``[A-Za-z0-9+/=]`` charset with no shell meaning --
    is embedded, inside a quoted heredoc so nothing in it is expanded.
    """
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'vmid="${1:?vmid}"; mount="${2:?mount}"; dest="${3:?dest}"\n'
        'command -v guestfish >/dev/null 2>&1 || { echo "guestfish not '
        "installed; run 'proxmox-lab disk host-setup'\" >&2; exit 3; }\n"
        "state=$(qm status \"$vmid\" 2>/dev/null | awk '{print $2}')\n"
        '[ "$state" = "running" ] && { echo "VMID $vmid is running; stop it '
        'first" >&2; exit 4; }\n'
        "volid=$(qm config \"$vmid\" 2>/dev/null | sed -n -E "
        "'s/^(scsi0|virtio0|sata0|ide0): ([^,]+).*/\\2/p' | head -n1)\n"
        '[ -n "$volid" ] || { echo "no primary disk for $vmid" >&2; exit 5; }\n'
        'disk=$(pvesm path "$volid" 2>/dev/null) || { echo "cannot resolve '
        '$volid" >&2; exit 6; }\n'
        'tmp=$(mktemp)\n'
        "base64 -d > \"$tmp\" <<'PXLB64'\n"
        + payload_b64 + "\n"
        "PXLB64\n"
        'guestfish --rw -a "$disk" -m "$mount" upload "$tmp" "$dest"\n'
        'rm -f "$tmp"; echo "wrote $dest"\n'
    )


def _run_host(lab: Any, argv: list[str], *, timeout: int = 120) -> str:
    from . import memflow

    memflow._require_enabled(lab)
    script = _HOST_DISK_SCRIPT
    proc = memflow._ssh(
        lab, ["bash", "-s", "--", *argv], timeout=timeout, stdin=script)
    if proc.returncode != 0:
        raise lab.LabError(
            f"disk: host operation failed: "
            f"{(proc.stderr or '').strip()[:300] or 'unknown error'}"
        )
    return proc.stdout


def _require_stopped(lab: Any, api: Any, vmid: int) -> None:
    status = api.call("GET", f"/nodes/{lab.NODE}/qemu/{vmid}/status/current")
    if not isinstance(status, dict) or status.get("status") != "stopped":
        raise lab.LabError(
            f"VMID {vmid} is not stopped; offline disk access needs the guest "
            "powered off so its filesystem is not mounted under a live kernel"
        )


def cmd_boot_info(lab: Any, args: Any) -> None:
    """Parse MBR/GPT from a local image or a stopped guest's disk."""
    if args.image:
        path = os.path.expanduser(args.image)
        if not os.path.isfile(path):
            raise lab.LabError(f"no such image: {path}")
        with open(path, "rb") as fh:
            data = fh.read(34 * bootstruct.SECTOR)
        source = {"image": path}
    else:
        api = lab.ProxmoxAPI()
        if args.lease:
            lab.load_lease(args.lease)
        _require_stopped(lab, api, args.vmid)
        b64 = _run_host(lab, ["boot-sectors", str(args.vmid), "34"]).strip()
        data = base64.b64decode(b64) if b64 else b""
        lab.audit("disk-boot-info", lease=args.lease, vmid=args.vmid, sync=False)
        source = {"vmid": args.vmid}
    parsed = bootstruct.parse_boot_sectors(data)
    print(json.dumps({**source, **parsed}, indent=2, sort_keys=True))


def cmd_ls(lab: Any, args: Any) -> None:
    """List a directory in a stopped guest's filesystem (offline)."""
    api = lab.ProxmoxAPI()
    lab.load_lease(args.lease)
    _require_stopped(lab, api, args.vmid)
    out = _run_host(lab, ["ls", str(args.vmid), args.mount, args.path])
    lab.audit("disk-ls", lease=args.lease, vmid=args.vmid,
              mount=args.mount, sync=False)
    print(json.dumps(
        {"vmid": args.vmid, "mount": args.mount, "path": args.path,
         "entries": out.splitlines()},
        indent=2, sort_keys=True,
    ))


def cmd_read(lab: Any, args: Any) -> None:
    """Read a file out of a stopped guest's filesystem (offline)."""
    api = lab.ProxmoxAPI()
    lab.load_lease(args.lease)
    _require_stopped(lab, api, args.vmid)
    b64 = _run_host(
        lab, ["cat", str(args.vmid), args.mount, args.path], timeout=180).strip()
    blob = base64.b64decode(b64) if b64 else b""
    lab.audit("disk-read", lease=args.lease, vmid=args.vmid,
              mount=args.mount, length=len(blob), sync=False)
    if args.out:
        out = os.path.expanduser(args.out)
        with open(out, "wb") as fh:
            fh.write(blob)
        print(json.dumps(
            {"vmid": args.vmid, "path": args.path, "bytes": len(blob),
             "out": out}, indent=2, sort_keys=True))
    else:
        print(blob.decode("utf-8", "replace"), end="")


def cmd_write(lab: Any, args: Any) -> None:
    """Write a local file into a stopped guest's filesystem. Dangerous.

    Mutating a guest's on-disk filesystem can render it unbootable. It is
    hard-gated behind --i-understand on top of the lease, and requires the
    guest to be stopped.
    """
    if not getattr(args, "i_understand", False):
        raise lab.LabError(
            "disk write modifies a guest's on-disk filesystem; a wrong file or "
            "path can make it unbootable. Re-run with --i-understand only when "
            "the user has explicitly asked to write to the guest disk."
        )
    src = os.path.expanduser(args.src)
    if not os.path.isfile(src):
        raise lab.LabError(f"no such local file: {src}")
    api = lab.ProxmoxAPI()
    lab.load_lease(args.lease)
    _require_stopped(lab, api, args.vmid)
    with open(src, "rb") as fh:
        payload = base64.b64encode(fh.read()).decode()
    from . import memflow

    memflow._require_enabled(lab)
    proc = memflow._ssh(
        lab, ["bash", "-s", "--", str(args.vmid), args.mount, args.dest],
        timeout=240, stdin=_build_write_script(payload),
    )
    if proc.returncode != 0:
        raise lab.LabError(
            "disk: host write failed: "
            f"{(proc.stderr or '').strip()[:300] or 'unknown error'}")
    lab.audit("disk-write", lease=args.lease, vmid=args.vmid,
              mount=args.mount, dest=args.dest, sync=False)
    print(json.dumps(
        {"vmid": args.vmid, "dest": args.dest, "wrote": src},
        indent=2, sort_keys=True))


def cmd_host_setup(lab: Any, args: Any) -> None:
    """Install libguestfs + the pxl-disk helper on the host (host change)."""
    script = _HOST_SETUP_SCRIPT.replace("%SCRIPT%", _HOST_DISK_SCRIPT)
    if args.print_only:
        print(script)
        return
    if not args.host_change_authorized:
        raise lab.LabError(
            "installing libguestfs is a host change. Re-run with "
            "--host-change-authorized only when the user asked for it."
        )
    from . import memflow

    memflow._require_enabled(lab)
    proc = memflow._ssh(lab, ["bash", "-s"], timeout=args.timeout, stdin=script)
    print(json.dumps(
        {"ok": proc.returncode == 0,
         "output": (proc.stdout or proc.stderr or "").strip()[:2000]},
        indent=2, sort_keys=True,
    ))


def register(sub: Any, lab: Any) -> None:
    def bind(handler: Any) -> Any:
        return lambda args: handler(lab, args)

    disk = sub.add_parser(
        "disk",
        help="offline guest-disk boot debugging and filesystem access",
    )
    disk_sub = disk.add_subparsers(dest="disk_command", required=True)

    boot = disk_sub.add_parser(
        "boot-info",
        help="parse MBR/GPT from a local image or a stopped guest's disk",
    )
    boot.add_argument("--image", help="local disk image to parse (no host)")
    boot.add_argument("--vmid", type=int, help="stopped guest to read instead")
    boot.add_argument("--lease")
    boot.set_defaults(func=bind(cmd_boot_info))

    setup = disk_sub.add_parser(
        "host-setup", help="install libguestfs on the host (host change)")
    setup.add_argument("--host-change-authorized", action="store_true")
    setup.add_argument("--print", dest="print_only", action="store_true")
    setup.add_argument("--timeout", type=int, default=1200)
    setup.set_defaults(func=bind(cmd_host_setup))

    ls = disk_sub.add_parser(
        "ls", help="list a directory in a stopped guest's filesystem")
    ls.add_argument("--lease", required=True)
    ls.add_argument("--vmid", type=int, required=True)
    ls.add_argument("--mount", default="/dev/sda1",
                    help="guest device to mount (default: /dev/sda1)")
    ls.add_argument("--path", default="/")
    ls.set_defaults(func=bind(cmd_ls))

    read = disk_sub.add_parser(
        "read", help="read a file from a stopped guest's filesystem")
    read.add_argument("--lease", required=True)
    read.add_argument("--vmid", type=int, required=True)
    read.add_argument("--mount", default="/dev/sda1")
    read.add_argument("--path", required=True)
    read.add_argument("--out", help="write to this local file instead of stdout")
    read.set_defaults(func=bind(cmd_read))

    write = disk_sub.add_parser(
        "write", help="write a local file into a stopped guest (dangerous)")
    write.add_argument("--lease", required=True)
    write.add_argument("--vmid", type=int, required=True)
    write.add_argument("--mount", default="/dev/sda1")
    write.add_argument("--src", required=True, help="local file to upload")
    write.add_argument("--dest", required=True, help="destination path in guest")
    write.add_argument("--i-understand", action="store_true")
    write.set_defaults(func=bind(cmd_write))
