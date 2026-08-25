"""Diagnose virtio devices for driver porting and debugging.

Porting a virtio driver to a new or obscure guest OS is mostly a question of
"what does the device actually offer, and what did my driver negotiate?" The
answers live in the host: the device model, its advertised feature bits, and
-- once a guest driver attaches -- the negotiated feature set and virtqueue
state. This module surfaces all of that from *outside* the guest, so it works
before the guest even has a working driver.

How it reaches the device
-------------------------
Everything here goes through the Proxmox API, the same token every other
command uses -- no SSH, no root, no memflow. The guest configuration comes from
the VM config endpoint; the live device state comes from QEMU's human monitor
via ``POST /nodes/<node>/qemu/<vmid>/monitor``.

Read-only by construction
-------------------------
The QEMU monitor can also mutate a guest, so this module refuses to send
anything but an allowlisted set of ``info`` queries. A caller cannot use it to
change guest state even by mistake: ``_monitor`` rejects any command whose
first word is not a known read-only ``info`` subcommand.

The feature-bit decoder is the porting workhorse and is fully offline: give it
a hex feature value and a device type and it names every bit, so a driver
author can check their negotiated features against the device's without a
running guest at all.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Transport / core feature bits, shared by every virtio device type. Numbers
# are the VIRTIO_F_* bit positions from the virtio 1.x specification.
_TRANSPORT_BITS: dict[int, str] = {
    24: "VIRTIO_F_NOTIFY_ON_EMPTY",
    27: "VIRTIO_F_ANY_LAYOUT",
    28: "VIRTIO_RING_F_INDIRECT_DESC",
    29: "VIRTIO_RING_F_EVENT_IDX",
    32: "VIRTIO_F_VERSION_1",
    33: "VIRTIO_F_ACCESS_PLATFORM",
    34: "VIRTIO_F_RING_PACKED",
    35: "VIRTIO_F_IN_ORDER",
    36: "VIRTIO_F_ORDER_PLATFORM",
    37: "VIRTIO_F_SR_IOV",
    38: "VIRTIO_F_NOTIFICATION_DATA",
    39: "VIRTIO_F_NOTIF_CONFIG_DATA",
    40: "VIRTIO_F_RING_RESET",
}

# Device-specific feature bits (0..31), keyed by the device type as QEMU names
# it (the qdev name without the "virtio-" transport suffix).
_DEVICE_BITS: dict[str, dict[int, str]] = {
    "net": {
        0: "VIRTIO_NET_F_CSUM",
        1: "VIRTIO_NET_F_GUEST_CSUM",
        2: "VIRTIO_NET_F_CTRL_GUEST_OFFLOADS",
        3: "VIRTIO_NET_F_MTU",
        5: "VIRTIO_NET_F_MAC",
        7: "VIRTIO_NET_F_GUEST_TSO4",
        8: "VIRTIO_NET_F_GUEST_TSO6",
        9: "VIRTIO_NET_F_GUEST_ECN",
        10: "VIRTIO_NET_F_GUEST_UFO",
        11: "VIRTIO_NET_F_HOST_TSO4",
        12: "VIRTIO_NET_F_HOST_TSO6",
        13: "VIRTIO_NET_F_HOST_ECN",
        14: "VIRTIO_NET_F_HOST_UFO",
        15: "VIRTIO_NET_F_MRG_RXBUF",
        16: "VIRTIO_NET_F_STATUS",
        17: "VIRTIO_NET_F_CTRL_VQ",
        18: "VIRTIO_NET_F_CTRL_RX",
        19: "VIRTIO_NET_F_CTRL_VLAN",
        21: "VIRTIO_NET_F_GUEST_ANNOUNCE",
        22: "VIRTIO_NET_F_MQ",
        23: "VIRTIO_NET_F_CTRL_MAC_ADDR",
    },
    "blk": {
        1: "VIRTIO_BLK_F_SIZE_MAX",
        2: "VIRTIO_BLK_F_SEG_MAX",
        4: "VIRTIO_BLK_F_GEOMETRY",
        5: "VIRTIO_BLK_F_RO",
        6: "VIRTIO_BLK_F_BLK_SIZE",
        9: "VIRTIO_BLK_F_FLUSH",
        10: "VIRTIO_BLK_F_TOPOLOGY",
        11: "VIRTIO_BLK_F_CONFIG_WCE",
        12: "VIRTIO_BLK_F_MQ",
        13: "VIRTIO_BLK_F_DISCARD",
        14: "VIRTIO_BLK_F_WRITE_ZEROES",
        15: "VIRTIO_BLK_F_LIFETIME",
        16: "VIRTIO_BLK_F_SECURE_ERASE",
    },
    "scsi": {
        0: "VIRTIO_SCSI_F_INOUT",
        1: "VIRTIO_SCSI_F_HOTPLUG",
        2: "VIRTIO_SCSI_F_CHANGE",
        3: "VIRTIO_SCSI_F_T10_PI",
    },
}

# The only monitor subcommands this module will ever send. The monitor can
# mutate a guest, so anything outside this read-only set is refused.
_ALLOWED_INFO = frozenset({
    "virtio", "virtio-status", "virtio-queue-status",
    "qtree", "pci", "block",
})

# QEMU exposes virtio devices at monitor paths like "/machine/peripheral/...".
_DEV_PATH_RE = re.compile(r"/[\w./@-]*virtio[\w./@-]*")
# "virtio-net", "virtio-blk", ... -> device-feature table key.
_DEV_TYPE_RE = re.compile(r"virtio-(\w+)")


def decode_features(value: int, device: str | None = None) -> list[dict[str, Any]]:
    """Name every set/known bit in a virtio feature value.

    Returns one entry per bit that is either set or has a known name, sorted by
    bit position, so a driver author can read a negotiated feature word
    directly. Device-specific bits are looked up when ``device`` is given.
    """
    device_bits = _DEVICE_BITS.get(device or "", {})
    positions = set(device_bits) | set(_TRANSPORT_BITS)
    for bit in range(64):
        if value & (1 << bit):
            positions.add(bit)
    out: list[dict[str, Any]] = []
    for bit in sorted(positions):
        name = _TRANSPORT_BITS.get(bit) or device_bits.get(bit)
        is_set = bool(value & (1 << bit))
        if name is None and not is_set:
            continue
        out.append({
            "bit": bit,
            "name": name or f"bit {bit} (unknown for device {device or '?'})",
            "set": is_set,
        })
    return out


def _monitor(lab: Any, api: Any, vmid: int, command: str) -> str:
    """Send one allowlisted, read-only ``info`` command to the guest monitor."""
    parts = command.split()
    if len(parts) < 2 or parts[0] != "info" or parts[1] not in _ALLOWED_INFO:
        raise lab.LabError(
            f"refusing to send monitor command {command!r}: only read-only "
            "'info' queries are permitted"
        )
    result = api.call(
        "POST", f"/nodes/{lab.NODE}/qemu/{vmid}/monitor", {"command": command}
    )
    return result if isinstance(result, str) else json.dumps(result)


def _require_running(lab: Any, api: Any, vmid: int) -> None:
    status = api.call("GET", f"/nodes/{lab.NODE}/qemu/{vmid}/status/current")
    if not isinstance(status, dict) or status.get("status") != "running":
        raise lab.LabError(
            f"VMID {vmid} is not a running QEMU guest; virtio state is only "
            "readable while it runs"
        )


def _configured_virtio(config: Any) -> list[dict[str, str]]:
    """Pick the virtio-backed devices out of a VM config."""
    devices: list[dict[str, str]] = []
    if not isinstance(config, dict):
        return devices
    for key, value in sorted(config.items()):
        text = str(value)
        if re.fullmatch(r"virtio\d+", key):
            devices.append({"slot": key, "kind": "virtio-blk", "config": text})
        elif re.fullmatch(r"net\d+", key) and "virtio" in text:
            devices.append({"slot": key, "kind": "virtio-net", "config": text})
        elif key == "scsihw" and "virtio-scsi" in text:
            devices.append({"slot": key, "kind": "virtio-scsi", "config": text})
        elif key == "rng0":
            devices.append({"slot": key, "kind": "virtio-rng", "config": text})
        elif key == "vmgenid":
            continue
        elif "virtio" in text and key not in {"vga"}:
            devices.append({"slot": key, "kind": "virtio", "config": text})
    return devices


def _device_paths(listing: str) -> list[str]:
    """Best-effort extraction of monitor device paths from 'info virtio'."""
    seen: list[str] = []
    for match in _DEV_PATH_RE.findall(listing):
        if match not in seen:
            seen.append(match)
    return seen


def _device_type_of(path: str) -> str | None:
    match = _DEV_TYPE_RE.search(path)
    return match.group(1) if match else None


def _hex_features_in(text: str) -> list[int]:
    """Pull hex feature words out of 'info virtio-status' text, best effort."""
    values: list[int] = []
    for match in re.finditer(r"features?[^\n]*?(0x[0-9a-fA-F]+)", text):
        values.append(int(match.group(1), 16))
    return values


def cmd_decode(lab: Any, args: Any) -> None:
    """Decode a virtio feature word offline -- the driver-porting workhorse."""
    raw = args.value.strip()
    try:
        value = int(raw, 0)
    except ValueError:
        raise lab.LabError(
            f"--value must be an integer (decimal or 0x-prefixed hex), got {raw!r}"
        ) from None
    if value < 0:
        raise lab.LabError("--value must not be negative")
    bits = decode_features(value, args.device)
    print(json.dumps({
        "value": hex(value),
        "device": args.device,
        "features": bits,
        "set_feature_names": [b["name"] for b in bits if b["set"]],
    }, indent=2, sort_keys=True))


def cmd_inspect(lab: Any, args: Any) -> None:
    """Report a guest's virtio devices for driver porting and debugging.

    Combines the configured virtio devices (from the VM config) with the live
    monitor view: the device list, and per device its raw ``info virtio-status``
    plus any feature words decoded against its type. Read-only.
    """
    api = lab.ProxmoxAPI()
    if args.lease:
        lab.load_lease(args.lease)
    _require_running(lab, api, args.vmid)

    config = api.call("GET", f"/nodes/{lab.NODE}/qemu/{args.vmid}/config")
    configured = _configured_virtio(config)

    listing = _monitor(lab, api, args.vmid, "info virtio")
    devices: list[dict[str, Any]] = []
    for path in _device_paths(listing):
        status = _monitor(lab, api, args.vmid, f"info virtio-status {path}")
        device_type = _device_type_of(path)
        decoded = [
            {"value": hex(value),
             "features": decode_features(value, device_type)}
            for value in _hex_features_in(status)
        ]
        devices.append({
            "path": path,
            "device_type": device_type,
            "status_raw": status,
            "decoded_features": decoded,
        })

    lab.audit("virtio-inspect", lease=args.lease, vmid=args.vmid,
              configured=len(configured), live_devices=len(devices))
    print(json.dumps({
        "vmid": args.vmid,
        "configured_devices": configured,
        "live_devices": devices,
        "listing_raw": listing,
        "note": (
            "status_raw/listing_raw are verbatim QEMU monitor output; its "
            "exact format varies by QEMU version, so decoded_features is "
            "best-effort. Use 'virtio decode' on a known feature word for an "
            "exact decode."
        ),
    }, indent=2, sort_keys=True))


def cmd_monitor(lab: Any, args: Any) -> None:
    """Run one allowlisted read-only virtio 'info' query and print it raw."""
    api = lab.ProxmoxAPI()
    if args.lease:
        lab.load_lease(args.lease)
    _require_running(lab, api, args.vmid)
    command = "info " + args.query
    output = _monitor(lab, api, args.vmid, command)
    lab.audit("virtio-monitor", lease=args.lease, vmid=args.vmid,
              query=args.query)
    print(json.dumps(
        {"vmid": args.vmid, "command": command, "output": output},
        indent=2, sort_keys=True,
    ))


def register(sub: Any, lab: Any) -> None:
    from .cli import _bind


    virtio = sub.add_parser(
        "virtio",
        help="diagnose virtio devices for driver porting and debugging",
    )
    virtio_sub = virtio.add_subparsers(dest="virtio_command", required=True)

    decode = virtio_sub.add_parser(
        "decode",
        help="decode a virtio feature word offline (names every bit)",
    )
    decode.add_argument("--value", required=True,
                        help="feature word, decimal or 0x-prefixed hex")
    decode.add_argument("--device",
                        choices=sorted(_DEVICE_BITS),
                        help="device type for device-specific bit names")
    decode.set_defaults(func=_bind(lab, cmd_decode))

    inspect = virtio_sub.add_parser(
        "inspect",
        help="report a running guest's virtio devices and negotiated features",
    )
    inspect.add_argument("--vmid", type=int, required=True)
    inspect.add_argument("--lease",
                         help="optional: audit the read against a lease")
    inspect.set_defaults(func=_bind(lab, cmd_inspect))

    monitor = virtio_sub.add_parser(
        "monitor",
        help="run one read-only virtio 'info' query against the guest monitor",
    )
    monitor.add_argument("--vmid", type=int, required=True)
    monitor.add_argument(
        "--query", required=True, choices=sorted(_ALLOWED_INFO),
        help="the 'info' subcommand to run (read-only)",
    )
    monitor.add_argument("--lease")
    monitor.set_defaults(func=_bind(lab, cmd_monitor))
