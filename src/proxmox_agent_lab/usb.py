"""USB passthrough and traffic sniffing for lab guests.

For driver development you often need to watch the USB traffic between a guest
and a real device passed through to it. When Proxmox passes a physical device to
a QEMU guest (``usb-host``), QEMU claims it through usbfs, so the host's
``usbmon`` facility still sees every URB. This module captures that traffic on
the host with ``tcpdump`` and returns a standard pcap that Wireshark decodes as
USB -- no agent, no driver, nothing installed in the guest.

Like memflow, the capture has to run on the hypervisor as root, so this module
reuses the same opt-in **SSH channel** (the ``[memflow]`` host connection) --
the one trust boundary that is not the API token. Sniffing is passive; attaching
or detaching a device is a passthrough change and is gated behind
``--host-change-authorized`` on top of the lease, matching the rest of the tool.

Nothing here touches a device's *contents*; a pcap of URBs is written to a local
file you name, and the audit ledger records only that a capture happened.
"""

from __future__ import annotations

import base64
import json
import os
import re
from typing import Any

from . import memflow as _mf


# --------------------------------------------------------------------------- #
# Host channel (shared with memflow: same host, same trust boundary).
# --------------------------------------------------------------------------- #

def _require_enabled(lab: Any) -> None:
    if not _mf.ENABLED or not _mf.SSH_HOST:
        raise lab.LabError(
            "USB sniffing runs on the Proxmox host over SSH -- the same host "
            "connection memflow uses -- so it is off until you set [memflow] "
            "enabled = true and ssh_host. See docs/usb.md."
        )


def _ssh(lab: Any, argv: list[str], *, timeout: int = 60):
    return _mf._ssh(lab, argv, timeout=timeout)


def _lsusb(lab: Any) -> list[dict[str, Any]]:
    """Every USB device the host sees, as {bus, dev, id, vendor, product, name}."""
    proc = _ssh(lab, ["lsusb"], timeout=30)
    if proc.returncode not in (0, None):
        raise lab.LabError(f"lsusb failed on the host: {proc.stderr.strip()[:200]}")
    devices = []
    # "Bus 001 Device 002: ID 04e8:61b6 Samsung Electronics ... M3 Portable"
    pattern = re.compile(
        r"Bus (\d+) Device (\d+): ID ([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\s*(.*)"
    )
    for line in proc.stdout.splitlines():
        m = pattern.match(line.strip())
        if not m:
            continue
        devices.append({
            "bus": int(m.group(1)),
            "dev": int(m.group(2)),
            "vendor": m.group(3).lower(),
            "product": m.group(4).lower(),
            "id": f"{m.group(3).lower()}:{m.group(4).lower()}",
            "name": m.group(5).strip(),
        })
    return devices


def _resolve(lab: Any, spec: str) -> dict[str, Any]:
    """Find a device by 'vendor:product' or 'bus-dev' (e.g. 04e8:61b6 or 1-2)."""
    spec = spec.strip().lower()
    devices = _lsusb(lab)
    if ":" in spec:
        matches = [d for d in devices if d["id"] == spec]
    elif "-" in spec:
        try:
            bus, dev = (int(x) for x in spec.split("-", 1))
        except ValueError:
            raise lab.LabError(f"not a bus-dev address: {spec!r}") from None
        matches = [d for d in devices if d["bus"] == bus and d["dev"] == dev]
    else:
        raise lab.LabError(
            f"specify the device as vendor:product (04e8:61b6) or bus-dev (1-2), "
            f"not {spec!r}"
        )
    if not matches:
        raise lab.LabError(
            f"no USB device {spec!r} on the host; run 'proxmox-lab usb list'"
        )
    if len(matches) > 1:
        raise lab.LabError(
            f"{spec!r} matches {len(matches)} devices; use the bus-dev form to "
            "pick one"
        )
    return matches[0]


# --------------------------------------------------------------------------- #
# Commands.
# --------------------------------------------------------------------------- #

def cmd_list(lab: Any, args: Any) -> None:
    """List host USB devices and which guests they are passed through to."""
    _require_enabled(lab)
    devices = _lsusb(lab)
    # Which VMs reference a usbN passthrough, and for what device.
    cfgs = _ssh(
        lab,
        ["bash", "-c",
         "for v in $(qm list | awk 'NR>1{print $1}'); do "
         "qm config $v 2>/dev/null | sed -n 's/^\\(usb[0-9]*\\): /'$v' \\1 /p'; done"],
        timeout=60,
    )
    passthrough = []
    for line in (cfgs.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 3:
            passthrough.append({"vmid": int(parts[0]), "index": parts[1],
                                "spec": " ".join(parts[2:])})
    lab.audit("usb-list", host=_mf.SSH_HOST, count=len(devices), sync=False)
    print(json.dumps({"devices": devices, "passthrough": passthrough},
                     indent=2, sort_keys=True))


def _usb_index(lab: Any, api: Any, vmid: int) -> str:
    """The next free usbN slot on a guest (Proxmox allows usb0..usb4)."""
    config = api.call("GET", f"/nodes/{lab.NODE}/qemu/{vmid}/config")
    for i in range(5):
        if f"usb{i}" not in config:
            return f"usb{i}"
    raise lab.LabError(f"VMID {vmid} already has usb0..usb4 in use")


def cmd_attach(lab: Any, args: Any) -> None:
    """Pass a host USB device through to a guest. This is a host change."""
    _require_enabled(lab)
    if not args.host_change_authorized:
        raise lab.LabError(
            "Passing a physical USB device to a guest takes it away from the "
            "host and is a passthrough change. Re-run with "
            "--host-change-authorized once the user has asked for that exact "
            "device. Never pass through a disk backing an active storage."
        )
    api = lab.ProxmoxAPI()
    lab.require_lease_resource(lab.load_lease(args.lease), "qemu", args.vmid)
    device = _resolve(lab, args.device)
    slot = _usb_index(lab, api, args.vmid)
    # Address by vendor:product so it survives re-enumeration.
    api.call("PUT", f"/nodes/{lab.NODE}/qemu/{args.vmid}/config",
             {slot: f"host={device['id']}"})
    lab.audit("usb-attach", lease=args.lease, vmid=args.vmid, slot=slot,
              device=device["id"], sync=False)
    print(json.dumps(
        {"vmid": args.vmid, "slot": slot, "device": device["id"],
         "name": device["name"],
         "note": "hotplugged if the guest is running; otherwise attaches at next start"},
        indent=2, sort_keys=True,
    ))


def cmd_detach(lab: Any, args: Any) -> None:
    """Remove a USB passthrough slot from a guest, returning it to the host."""
    _require_enabled(lab)
    api = lab.ProxmoxAPI()
    lab.require_lease_resource(lab.load_lease(args.lease), "qemu", args.vmid)
    if not re.fullmatch(r"usb[0-4]", args.slot):
        raise lab.LabError("--slot must be usb0..usb4")
    config = api.call("GET", f"/nodes/{lab.NODE}/qemu/{args.vmid}/config")
    if args.slot not in config:
        raise lab.LabError(f"{args.slot} is not configured on VMID {args.vmid}")
    api.call("PUT", f"/nodes/{lab.NODE}/qemu/{args.vmid}/config",
             {"delete": args.slot})
    lab.audit("usb-detach", lease=args.lease, vmid=args.vmid, slot=args.slot,
              sync=False)
    print(json.dumps({"vmid": args.vmid, "slot": args.slot, "detached": True},
                     indent=2, sort_keys=True))


def cmd_sniff(lab: Any, args: Any) -> None:
    """Capture the USB traffic of a device to a local pcap via host usbmon.

    Captures the whole bus the device sits on (usbmon is per-bus); the device's
    address is reported so Wireshark can filter to it
    (``usb.device_address == N``). Works whether the device is owned by the host
    or passed through to a guest -- usbmon sees QEMU's usbfs traffic either way.
    """
    _require_enabled(lab)
    lab.load_lease(args.lease)
    device = _resolve(lab, args.device)
    bus = device["bus"]
    remote_pcap = f"/tmp/pxl-usb-{bus}-{device['dev']}.pcap"
    limit = f"-c {args.count}" if args.count else ""
    # timeout bounds the capture even if the packet count is never reached; a
    # timeout-killed tcpdump still flushes a valid pcap.
    script = (
        "set -e; modprobe usbmon 2>/dev/null || true; "
        f"timeout {args.seconds} tcpdump -i usbmon{bus} {limit} "
        f"-w {remote_pcap} >/dev/null 2>&1 || true; "
        f"pkts=$(tcpdump -r {remote_pcap} 2>/dev/null | wc -l); "
        f"echo \"PKTS=$pkts\"; base64 {remote_pcap}; rm -f {remote_pcap}"
    )
    proc = _ssh(lab, ["bash", "-c", script], timeout=args.seconds + 60)
    if proc.returncode not in (0, None):
        raise lab.LabError(
            f"usb capture failed on the host: {(proc.stderr or '').strip()[:300]}"
        )
    lines = proc.stdout.splitlines()
    pkts = 0
    b64 = []
    for line in lines:
        if line.startswith("PKTS="):
            pkts = int(line.split("=", 1)[1] or 0)
        else:
            b64.append(line)
    out = os.path.expanduser(args.out)
    with open(out, "wb") as fh:
        fh.write(base64.b64decode("".join(b64) or ""))
    lab.audit("usb-sniff", lease=args.lease, device=device["id"], bus=bus,
              seconds=args.seconds, packets=pkts, sync=False)
    print(json.dumps(
        {"device": device["id"], "name": device["name"],
         "bus": bus, "device_address": device["dev"],
         "packets": pkts, "out": out,
         "wireshark_filter": f"usb.device_address == {device['dev']}"},
        indent=2, sort_keys=True,
    ))


# --------------------------------------------------------------------------- #
# Registration.
# --------------------------------------------------------------------------- #

def register(sub: Any, lab: Any) -> None:
    def bind(handler: Any) -> Any:
        return lambda args: handler(lab, args)

    usb = sub.add_parser("usb", help="USB passthrough and traffic sniffing")
    usb_sub = usb.add_subparsers(dest="usb_command", required=True)

    listp = usb_sub.add_parser("list", help="list host USB devices and passthroughs")
    listp.set_defaults(func=bind(cmd_list))

    attach = usb_sub.add_parser(
        "attach", help="pass a host USB device to a guest (passthrough change)"
    )
    attach.add_argument("--lease", required=True)
    attach.add_argument("--vmid", type=int, required=True)
    attach.add_argument("--device", required=True,
                        help="vendor:product (04e8:61b6) or bus-dev (1-2)")
    attach.add_argument("--host-change-authorized", action="store_true")
    attach.set_defaults(func=bind(cmd_attach))

    detach = usb_sub.add_parser("detach", help="remove a USB passthrough slot")
    detach.add_argument("--lease", required=True)
    detach.add_argument("--vmid", type=int, required=True)
    detach.add_argument("--slot", required=True, help="usb0..usb4")
    detach.set_defaults(func=bind(cmd_detach))

    sniff = usb_sub.add_parser(
        "sniff", help="capture a device's USB traffic to a local pcap"
    )
    sniff.add_argument("--lease", required=True)
    sniff.add_argument("--device", required=True,
                       help="vendor:product (04e8:61b6) or bus-dev (1-2)")
    sniff.add_argument("--seconds", type=int, default=15,
                       help="capture duration")
    sniff.add_argument("--count", type=int, default=0,
                       help="stop after N packets (0 = until the duration)")
    sniff.add_argument("--out", required=True, help="local pcap output file")
    sniff.set_defaults(func=bind(cmd_sniff))
