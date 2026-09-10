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
anything but an exact allowlisted set of ``info`` queries. A caller cannot use
it to change guest state even by mistake: ``_monitor`` validates the complete
command grammar, including device paths and queue indexes, before sending it.

The feature-bit decoder is the porting workhorse and is fully offline: give it
a hex feature value and a device type and it names every bit, so a driver
author can check their negotiated features against the device's without a
running guest at all.
"""

from __future__ import annotations

import json
import math
import re
import time
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
    "virtio-queue-element", "qtree", "pci", "block",
})
_SIMPLE_INFO = frozenset({"virtio", "qtree", "pci", "block"})

# QEMU exposes virtio devices at monitor paths like "/machine/peripheral/...".
_DEV_PATH_RE = re.compile(r"/[\w./@-]*virtio[\w./@-]*")
# "virtio-net", "virtio-blk", ... -> device-feature table key.
_DEV_TYPE_RE = re.compile(r"virtio-(\w+)")
_MONITOR_PATH_RE = re.compile(r"^/(?:[A-Za-z0-9_.@-]+/)*[A-Za-z0-9_.@-]+$")
_MONITOR_UINT_RE = re.compile(r"^(?:0|[1-9][0-9]*)$")
MAX_QUEUE_INDEX = 65535
MAX_ELEMENT_INDEX = 65535
MAX_QUEUE_SAMPLES = 64
MAX_QUEUE_INTERVAL_SECONDS = 60.0
MAX_QUEUE_DEADLINE_SECONDS = 300.0
DEFAULT_QUEUE_SAMPLES = 2
DEFAULT_QUEUE_INTERVAL_SECONDS = 1.0
DEFAULT_QUEUE_DEADLINE_SECONDS = 15.0
_QUEUE_COUNTERS = ("used_idx", "signalled_used", "last_avail_idx", "shadow_avail_idx")
# shadow_avail_idx is absent from QEMU 11.0 queue-status output, so it is
# reported when present but never required for interpretation.
_REQUIRED_COUNTERS = ("used_idx", "signalled_used", "last_avail_idx")
_QUEUE_FIELDS = frozenset({
    "device_name", "queue_index", "inuse", *_QUEUE_COUNTERS,
    "signalled_used_valid", "vring_num", "vring_num_default", "vring_align",
    "vring_desc", "vring_avail", "vring_used",
})


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


def _valid_monitor_path(path: str) -> bool:
    return (
        isinstance(path, str)
        and "virtio" in path
        and _MONITOR_PATH_RE.fullmatch(path) is not None
    )


def _valid_monitor_uint(text: str, maximum: int) -> bool:
    return (
        _MONITOR_UINT_RE.fullmatch(text) is not None
        and int(text) <= maximum
    )


def _monitor_command_is_safe(command: str) -> bool:
    """Check one exact read-only HMP command grammar.

    HMP accepts command separators and other free-form text.  The monitor
    endpoint is therefore protected by exact matches rather than a first-word
    allowlist.
    """
    if not isinstance(command, str) or any(c in command for c in "\r\n;|&"):
        return False
    if command in {"info virtio", "info qtree", "info pci", "info block"}:
        return True
    match = re.fullmatch(r"info virtio-status ([^ ]+)", command)
    if match:
        return _valid_monitor_path(match.group(1))
    match = re.fullmatch(r"info virtio-queue-status ([^ ]+) ([0-9]+)", command)
    if match:
        return _valid_monitor_path(match.group(1)) and _valid_monitor_uint(
            match.group(2), MAX_QUEUE_INDEX
        )
    match = re.fullmatch(
        r"info virtio-queue-element ([^ ]+) ([0-9]+)(?: ([0-9]+))?", command
    )
    if match:
        return (
            _valid_monitor_path(match.group(1))
            and _valid_monitor_uint(match.group(2), MAX_QUEUE_INDEX)
            and (
                match.group(3) is None
                or _valid_monitor_uint(match.group(3), MAX_ELEMENT_INDEX)
            )
        )
    return False


def _monitor(lab: Any, api: Any, vmid: int, command: str,
             *, timeout: float | None = None) -> str:
    """Send one exact, allowlisted, read-only ``info`` command."""
    if not _monitor_command_is_safe(command):
        raise lab.LabError(
            f"refusing to send monitor command {command!r}: only exact "
            "read-only 'info' queries are permitted"
        )
    request = {"command": command}
    kwargs: dict[str, Any] = {}
    if timeout is not None:
        # ProxmoxAPI accepts integer timeouts.  Ceiling preserves the caller's
        # bounded budget as closely as that API permits; never send zero.
        kwargs["timeout"] = max(1, min(30, math.ceil(float(timeout))))
    result = api.call(
        "POST", f"/nodes/{lab.NODE}/qemu/{vmid}/monitor", request, **kwargs
    )
    return result if isinstance(result, str) else json.dumps(result)


def _require_running(lab: Any, api: Any, vmid: int,
                     *, timeout: float | None = None) -> None:
    kwargs: dict[str, Any] = {}
    if timeout is not None:
        kwargs["timeout"] = max(1, min(30, math.ceil(float(timeout))))
    status = api.call(
        "GET", f"/nodes/{lab.NODE}/qemu/{vmid}/status/current", **kwargs
    )
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


def _scalar(text: str) -> Any:
    text = text.strip()
    if text.lower() in {"true", "false"}:
        return text.lower() == "true"
    if re.fullmatch(r"(?:0x[0-9a-fA-F]+|[0-9]+)", text):
        try:
            return int(text, 0)
        except ValueError:
            pass
    return text


def parse_queue_status(raw: str) -> dict[str, Any]:
    """Parse known queue fields and retain fields QEMU may add later."""
    fields: dict[str, Any] = {}
    unknown_fields: list[dict[str, str]] = []
    unknown_lines: list[str] = []
    section = ""
    ring_format: str | None = None
    if not isinstance(raw, str):
        return {"available": False, "fields": fields,
                "unknown_fields": unknown_fields, "unknown_lines": unknown_lines,
                "ring_format": ring_format}
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.endswith(":") and ":" not in stripped[:-1]:
            section = stripped[:-1].strip().lower().replace("-", "_")
            if "packed" in section:
                ring_format = "packed"
            elif "split" in section:
                ring_format = "split"
            continue
        match = re.match(r"^\s*([A-Za-z][A-Za-z0-9_-]*)\s*:\s*(.*?)\s*$", line)
        if not match:
            unknown_lines.append(line)
            if "packed" in stripped.lower():
                ring_format = "packed"
            elif "split" in stripped.lower() and ring_format is None:
                ring_format = "split"
            continue
        key, value = match.groups()
        normalized = key.lower().replace("-", "_")
        field_name = normalized
        if section in {"vring", "ring", "packed", "split", "packed_ring", "split_ring"}:
            field_name = f"vring_{normalized}"
        if field_name in _QUEUE_FIELDS:
            fields[field_name] = _scalar(value)
        else:
            unknown_fields.append({"key": field_name, "value": value})
        lower = f"{key} {value}".lower()
        if "packed" in lower:
            ring_format = "packed"
        elif "split" in lower and ring_format is None:
            ring_format = "split"
    result: dict[str, Any] = {
        "available": bool(fields), "fields": fields,
        "unknown_fields": unknown_fields, "unknown_lines": unknown_lines,
        "ring_format": ring_format,
    }
    result.update(fields)
    return result


_parse_queue_status = parse_queue_status


def parse_queue_element(raw: str) -> dict[str, Any]:
    """Parse queue-element key/value lines while preserving unknown lines.

    QEMU nests ``desc``/``avail``/``used`` sections whose keys repeat (``idx``,
    ``flags``), so section keys are prefixed to keep every value distinct.
    """
    fields: dict[str, Any] = {}
    unknown_lines: list[str] = []
    section = ""
    if isinstance(raw, str):
        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.endswith(":") and ":" not in stripped[:-1]:
                word = stripped[:-1].strip().lower().replace("-", "_")
                section = word if re.fullmatch(r"[a-z_][a-z0-9_]*", word) else ""
                continue
            match = re.match(r"^\s*([A-Za-z][A-Za-z0-9_-]*)\s*:\s*(.*?)\s*$", line)
            if match:
                key = match.group(1).lower().replace("-", "_")
                name = f"{section}_{key}" if section else key
                fields[name] = _scalar(match.group(2))
            else:
                unknown_lines.append(line)
    return {"available": bool(fields), "fields": fields,
            "unknown_lines": unknown_lines}


_parse_queue_element = parse_queue_element


def _counter_delta(previous: Any, current: Any, ring_format: str) -> dict[str, Any]:
    if not isinstance(previous, int) or isinstance(previous, bool):
        return {"available": False, "delta": None, "reason": "previous value unavailable"}
    if not isinstance(current, int) or isinstance(current, bool):
        return {"available": False, "delta": None, "reason": "current value unavailable"}
    if ring_format == "packed":
        return {"available": False, "delta": None,
                "reason": "packed ring; split-ring counter arithmetic is not applicable"}
    if ring_format != "split":
        return {"available": False, "delta": None,
                "reason": "ring format unavailable; refusing split-ring arithmetic"}
    if current >= previous:
        return {"available": True, "delta": current - previous,
                "wrapped": False, "reset": False}
    if previous >= 0xF000 and current <= 0x0FFF:
        return {"available": True, "delta": (current - previous) & 0xFFFF,
                "wrapped": True, "reset": False}
    return {"available": False, "delta": None, "wrapped": False, "reset": True,
            "reason": "counter moved backwards without a plausible 16-bit wrap"}


def _queue_deltas(snapshots: list[dict[str, Any]], ring_format: str) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for n, (old, new) in enumerate(zip(snapshots, snapshots[1:])):
        old_fields = (old.get("parsed") or {}).get("fields", {})
        new_fields = (new.get("parsed") or {}).get("fields", {})
        details: dict[str, Any] = {}
        values: dict[str, int] = {}
        reset = False
        for field in _QUEUE_COUNTERS:
            item = _counter_delta(old_fields.get(field), new_fields.get(field), ring_format)
            details[field] = item
            if item.get("delta") is not None:
                values[field] = item["delta"]
            reset = reset or bool(item.get("reset"))
        old_inuse, new_inuse = old_fields.get("inuse"), new_fields.get("inuse")
        if (isinstance(old_inuse, int) and not isinstance(old_inuse, bool)
                and isinstance(new_inuse, int) and not isinstance(new_inuse, bool)):
            details["inuse"] = {"available": True, "delta": new_inuse - old_inuse}
            values["inuse"] = new_inuse - old_inuse
        else:
            details["inuse"] = {"available": False, "delta": None,
                                 "reason": "inuse unavailable"}
        item: dict[str, Any] = {"from_sample": n, "to_sample": n + 1,
                                "fields": details, "values": values,
                                "reset": reset}
        item.update(values)
        output.append(item)
    return output


def _queue_interpretation(snapshots: list[dict[str, Any]], deltas: list[dict[str, Any]], ring_format: str) -> dict[str, Any]:
    unavailable = {"stalled_candidate": None, "status": "unavailable",
                   "driver_reclaimed_completions": None}
    if ring_format == "packed":
        return {**unavailable, "reason": "packed ring counters are not interpreted with split-ring arithmetic"}
    if ring_format != "split":
        return {**unavailable, "reason": "ring format unavailable; cannot interpret queue progress"}
    if len(snapshots) < 2:
        return {**unavailable, "reason": "at least two queue snapshots are needed"}
    if any(s.get("unavailable") for s in snapshots):
        return {**unavailable, "reason": "one or more queue snapshots were unavailable"}
    if any(d.get("reset") for d in deltas):
        return {**unavailable, "reason": "queue counters reset during sampling"}
    if any(not d["fields"][f].get("available") for d in deltas for f in _REQUIRED_COUNTERS):
        return {**unavailable, "reason": "required split-ring progress fields were unavailable"}
    occupancies = [(s.get("parsed") or {}).get("fields", {}).get("inuse") for s in snapshots]
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in occupancies):
        return {**unavailable, "reason": "inuse was unavailable; outstanding work cannot be assessed"}
    unchanged = all(
        d["fields"][f].get("delta") == 0
        for d in deltas for f in _QUEUE_COUNTERS
        if d["fields"][f].get("available")
    )
    candidate = unchanged and any(v > 0 for v in occupancies)
    return {
        "stalled_candidate": candidate,
        "status": "candidate" if candidate else "progress-or-idle",
        "reason": (
            "inuse stayed non-zero while split-ring indices did not advance; this is a stalled candidate, not proof of a driver or device hang"
            if candidate else "observed split-ring counters either advanced or had no outstanding work"
        ),
        "driver_reclaimed_completions": None,
    }


def _validate_queue_args(lab: Any, path: str, queue: int, samples: int,
                         interval: float, deadline: float, ring_format: str,
                         element_index: int | None) -> tuple[str, int, int | None]:
    if not _valid_monitor_path(path):
        raise lab.LabError("--path must be an absolute virtio QEMU device path")
    if not isinstance(queue, int) or isinstance(queue, bool) or queue < 0 or queue > MAX_QUEUE_INDEX:
        raise lab.LabError(f"--queue must be between 0 and {MAX_QUEUE_INDEX}")
    if element_index is not None and (
        not isinstance(element_index, int) or isinstance(element_index, bool)
        or element_index < 0 or element_index > MAX_ELEMENT_INDEX
    ):
        raise lab.LabError(f"--element-index must be between 0 and {MAX_ELEMENT_INDEX}")
    if ring_format not in {"auto", "split", "packed"}:
        raise lab.LabError("--ring-format must be auto, split, or packed")
    if not isinstance(samples, int) or isinstance(samples, bool) or not 1 <= samples <= MAX_QUEUE_SAMPLES:
        raise lab.LabError(f"--samples must be between 1 and {MAX_QUEUE_SAMPLES}")
    if not isinstance(interval, (int, float)) or isinstance(interval, bool) or not math.isfinite(float(interval)) or not 0 <= interval <= MAX_QUEUE_INTERVAL_SECONDS:
        raise lab.LabError(f"--interval must be between 0 and {MAX_QUEUE_INTERVAL_SECONDS:.0f}s")
    if not isinstance(deadline, (int, float)) or isinstance(deadline, bool) or not math.isfinite(float(deadline)) or not 0 < deadline <= MAX_QUEUE_DEADLINE_SECONDS:
        raise lab.LabError(f"--deadline must be greater than 0 and at most {MAX_QUEUE_DEADLINE_SECONDS:.0f}s")
    return path, queue, element_index


def sample_queues(lab: Any, api: Any, vmid: int, *, path: str, queue: int,
                  samples: int = DEFAULT_QUEUE_SAMPLES,
                  interval: float = DEFAULT_QUEUE_INTERVAL_SECONDS,
                  deadline: float = DEFAULT_QUEUE_DEADLINE_SECONDS,
                  ring_format: str = "auto",
                  element_index: int | None = None) -> dict[str, Any]:
    """Sample one queue with bounded HMP calls and conservative analysis."""
    _validate_queue_args(lab, path, queue, samples, interval, deadline, ring_format, element_index)
    _require_running(lab, api, vmid)
    started = time.monotonic()
    limit = started + float(deadline)
    snapshots: list[dict[str, Any]] = []
    unavailable: str | None = None
    deadline_exceeded = False
    for n in range(samples):
        if n:
            remaining = limit - time.monotonic()
            if remaining <= 0:
                deadline_exceeded = True
                break
            time.sleep(min(float(interval), remaining))
            if time.monotonic() > limit:
                deadline_exceeded = True
                break
        command = f"info virtio-queue-status {path} {queue}"
        try:
            raw = _monitor(lab, api, vmid, command)
        except Exception as exc:
            unavailable = str(exc) or exc.__class__.__name__
            snapshots.append({"sample": n, "raw": None, "parsed": None,
                              "unavailable": unavailable})
            break
        parsed = parse_queue_status(raw)
        snapshot: dict[str, Any] = {"sample": n, "raw": raw, "parsed": parsed}
        if not parsed["available"]:
            unavailable = "QEMU returned no recognized queue-status fields"
            snapshot["unavailable"] = unavailable
        snapshots.append(snapshot)
        if unavailable:
            break
    effective_format = ring_format
    if effective_format == "auto":
        for snapshot in snapshots:
            detected = (snapshot.get("parsed") or {}).get("ring_format")
            if detected in {"split", "packed"}:
                effective_format = detected
                break
        else:
            effective_format = "unknown"
    deltas = _queue_deltas(snapshots, effective_format)
    result: dict[str, Any] = {
        "vmid": int(vmid), "path": path, "queue": queue,
        "ring_format": effective_format, "samples_requested": samples,
        "samples_collected": len(snapshots), "interval_seconds": float(interval),
        "deadline_seconds": float(deadline), "snapshots": snapshots,
        "deltas": deltas,
        "interpretation": _queue_interpretation(snapshots, deltas, effective_format),
        "status": "unavailable" if unavailable else "ok",
    }
    if unavailable:
        result["unavailable"] = unavailable
    if deadline_exceeded or time.monotonic() > limit:
        result["deadline_exceeded"] = True
    if element_index is not None:
        if deadline_exceeded:
            result["element"] = {"index": element_index, "status": "unavailable",
                                  "reason": "sampling deadline exhausted before element read"}
        else:
            try:
                command = f"info virtio-queue-element {path} {queue} {element_index}"
                raw_element = _monitor(lab, api, vmid, command)
                parsed_element = parse_queue_element(raw_element)
                result["element"] = {"index": element_index, "raw": raw_element,
                                      "parsed": parsed_element,
                                      "status": "ok" if parsed_element["available"] else "unavailable"}
            except Exception as exc:
                result["element"] = {"index": element_index, "status": "unavailable",
                                      "reason": str(exc) or exc.__class__.__name__}
    return result


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


def cmd_queues(lab: Any, args: Any) -> None:
    """Sample and conservatively interpret one live virtqueue."""
    if args.lease:
        lab.load_lease(args.lease)
    api = lab.ProxmoxAPI()
    result = sample_queues(
        lab, api, args.vmid, path=args.path, queue=args.queue,
        samples=args.samples, interval=args.interval, deadline=args.deadline,
        ring_format=args.ring_format, element_index=args.element_index,
    )
    lab.audit(
        "virtio-queues", lease=args.lease, vmid=args.vmid, path=args.path,
        queue=args.queue, samples=result["samples_collected"],
        status=result["status"],
    )
    print(json.dumps(result, indent=2, sort_keys=True))


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
        "--query", required=True, choices=sorted(_SIMPLE_INFO),
        help="the 'info' subcommand to run (read-only)",
    )
    monitor.add_argument("--lease")
    monitor.set_defaults(func=_bind(lab, cmd_monitor))

    queues = virtio_sub.add_parser(
        "queues",
        help="sample one virtqueue through QEMU's read-only monitor",
        description=(
            "Samples info virtio-queue-status for one device path and queue. "
            "Raw monitor responses are retained; deltas and stalled-candidate "
            "interpretation are conservative and unavailable data is explicit."
        ),
    )
    queues.add_argument("--vmid", type=int, required=True)
    queues.add_argument("--path", "--device-path", "--device", dest="path", required=True,
                        help="absolute QEMU virtio device path")
    queues.add_argument("--queue", "--queue-index", dest="queue", type=int, required=True,
                        help="queue number (0..65535)")
    queues.add_argument("--lease", help="optional audit lease")
    queues.add_argument("--samples", type=int, default=DEFAULT_QUEUE_SAMPLES,
                        help="number of bounded samples (default: %(default)s)")
    queues.add_argument("--interval", type=float,
                        default=DEFAULT_QUEUE_INTERVAL_SECONDS,
                        help="seconds between samples (default: %(default)s)")
    queues.add_argument("--deadline", "--timeout", dest="deadline", type=float,
                        default=DEFAULT_QUEUE_DEADLINE_SECONDS,
                        help="overall sampling deadline (default: %(default)s)")
    queues.add_argument("--ring-format", "--format", dest="ring_format",
                        choices=("auto", "split", "packed"), default="auto",
                        help="ring arithmetic mode; auto requires an explicit format marker")
    queues.add_argument("--element-index", "--element", "--index", dest="element_index",
                        type=int, help="also read one bounded queue element")
    queues.set_defaults(func=_bind(lab, cmd_queues))
