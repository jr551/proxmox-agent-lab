"""Ground truth for "is this guest actually writing to its disk?"

Why this exists
---------------
Proxmox reports a ``diskwrite`` counter on ``qemu/<vmid>/status/current``, and
it is the obvious thing to poll when deciding whether a guest is making
progress or has hung. On its own it is not trustworthy. On a qcow2 image over
directory-backed storage the counter has been observed reading **0 bytes for a
whole session** while the guest was demonstrably writing, and a cumulative
counter that never moves looks exactly like an idle guest.

So this module does not ask Proxmox what it thinks happened. It measures
twice, with a gap in between, and reports what each independent signal saw
across that gap:

* ``proxmox_diskwrite`` -- the same cached counter, kept precisely so the
  reader can watch it disagree with the others.
* ``qmp_blockstats`` -- QEMU's own block-layer counters, read through the
  Proxmox monitor endpoint. This is what the emulator itself believes it
  wrote, and it needs no SSH.
* ``host_image_du`` -- ``du --block-size=1`` on the backing image file, over
  the opt-in memflow host SSH channel. ``du`` reports *allocated* bytes;
  ``ls`` reports the apparent size of a sparse image and would call an
  untouched 100 GB qcow2 a hundred gigabytes of I/O.

Every one of the three has a blind spot, which is why all three are reported
rather than one being picked as the answer. The counter can stall. The monitor
endpoint wants a privilege the ``PVEVMAdmin``-scoped lab token does not have,
and it answers in plain text -- including its refusals -- rather than with an
HTTP error, so a refusal read as success looks like a guest that wrote
nothing. ``du`` needs the host SSH boundary switched on, measures nothing
useful on LVM or ZFS where there is no file to grow, and legitimately reports
zero for a guest overwriting blocks its image already owns.

A signal that is unavailable is reported as unavailable. It never fails the
measurement: half an answer beats an exception when the question is "is this
thing alive".

The interesting output is ``disagreement``. Two signals that both say zero, or
both say millions, tell you the same thing. One saying zero while another says
millions tells you the counter is lying -- which is the failure this module
was written for.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

# A single reading of a cumulative counter says nothing; only the change
# between two readings does. Five seconds is long enough for a writing guest
# to move at least one qcow2 cluster and short enough to wait for.
DEFAULT_INTERVAL_SECONDS = 5.0
# The whole measurement -- both samples and every round trip inside them -- is
# bounded, so a wedged host cannot turn a diagnostic into a hang.
DEFAULT_DEADLINE_SECONDS = 120.0
MAX_INTERVAL_SECONDS = 600.0

MONITOR_COMMAND = "info blockstats"
DU_TIMEOUT_SECONDS = 30

SIGNAL_PROXMOX = "proxmox_diskwrite"
SIGNAL_BLOCKSTATS = "qmp_blockstats"
SIGNAL_HOST_DU = "host_image_du"

# scsi0..30, virtio0..15, sata0..5, ide0..3, plus the two state disks QEMU
# writes to by itself. `unused<n>` is deliberately absent: it names a volume
# no running guest has open, so it can never grow.
_DISK_KEY = re.compile(
    r"^(?:scsi|virtio|sata|ide)\d{1,2}$|^(?:efidisk|tpmstate)\d$"
)
_STAT_LINE = re.compile(r"^(?P<device>\S+?):\s+(?P<stats>\S.*)$")
_STAT_PAIR = re.compile(r"\b([a-z_]+)=(-?\d+)\b")
_DU_LINE = re.compile(r"^(\d+)\s+(\S.*)$")
_COUNTERS = ("wr_bytes", "rd_bytes", "wr_operations", "rd_operations")


# --------------------------------------------------------------------------- #
# Parsing. Pure functions, so the awkward real-world shapes are testable.
# --------------------------------------------------------------------------- #

def parse_blockstats(text: str) -> dict[str, dict[str, int]]:
    """Devices and their counters out of an ``info blockstats`` transcript.

    Indented lines are QEMU's per-node breakdown (backing files, filter
    nodes) and are skipped on purpose: folding them into the device totals
    counts the same byte more than once. A drive with no medium prints
    ``[not inserted]`` and carries no counters, so it drops out too.
    """
    devices: dict[str, dict[str, int]] = {}
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if not line.strip() or line[:1] in (" ", "\t"):
            continue
        match = _STAT_LINE.match(line)
        if not match:
            continue
        pairs = {
            key: int(value)
            for key, value in _STAT_PAIR.findall(match.group("stats"))
        }
        if "wr_bytes" not in pairs:
            continue
        devices[match.group("device")] = {
            key: pairs[key] for key in _COUNTERS if key in pairs
        }
    return devices


def written_from_blockstats(devices: dict[str, dict[str, int]]) -> int:
    """Total bytes QEMU says it wrote, across every device with a medium."""
    return sum(int(stats.get("wr_bytes", 0)) for stats in devices.values())


def parse_du(text: str) -> dict[str, int]:
    """``du --block-size=1`` output as {path: allocated bytes}."""
    sizes: dict[str, int] = {}
    for raw in (text or "").splitlines():
        match = _DU_LINE.match(raw.strip())
        if match:
            sizes[match.group(2)] = int(match.group(1))
    return sizes


def image_volumes(config: dict[str, Any]) -> list[tuple[str, str]]:
    """The writable disk volumes of a guest config, as (key, volid) pairs.

    CD-ROM entries are excluded: an ISO is read-only, so its file can never
    grow and including it would only add a path that always reports zero.
    """
    volumes: list[tuple[str, str]] = []
    for key in sorted(str(name) for name in config):
        if not _DISK_KEY.match(key):
            continue
        value = str(config.get(key) or "").strip()
        if not value:
            continue
        parts = [item.strip() for item in value.split(",")]
        volid = parts[0]
        if not volid or volid == "none" or ":" not in volid:
            continue
        if "media=cdrom" in parts[1:]:
            continue
        volumes.append((key, volid))
    return volumes


def disagreements(written: dict[str, int]) -> list[str]:
    """Which available signals contradict each other, spelled out.

    Only the zero/non-zero split counts as a contradiction. The magnitudes are
    *expected* to differ: qcow2 allocates in clusters, the host page cache
    delays writeback, and a guest rewriting blocks its image already owns
    grows the file by nothing at all. "One says nothing happened while another
    says something did" is the disagreement that precedes a misdiagnosis, so
    that is the one worth naming.
    """
    names = sorted(written)
    found: list[str] = []
    for index, left in enumerate(names):
        for right in names[index + 1:]:
            if (written[left] == 0) == (written[right] == 0):
                continue
            quiet, loud = (
                (left, right) if written[left] == 0 else (right, left)
            )
            found.append(
                f"{quiet} saw no write while {loud} saw {written[loud]} bytes"
            )
    return found


# --------------------------------------------------------------------------- #
# The three signals. Each returns (value, reason-it-is-missing).
# --------------------------------------------------------------------------- #

def blockstats(
    lab: Any, api: Any, vmid: int
) -> tuple[dict[str, dict[str, int]], str | None]:
    """QEMU's own block counters, or the reason they could not be read."""
    try:
        answer = api.call(
            "POST", f"/nodes/{lab.NODE}/qemu/{vmid}/monitor",
            {"command": MONITOR_COMMAND},
        )
    except lab.LabError as exc:
        # The lab token is PVEVMAdmin-scoped by design and this endpoint
        # wants Sys.Audit|Sys.Modify, so a refusal is an expected outcome,
        # not an exceptional one. It costs one signal, never the run.
        return {}, f"monitor endpoint unavailable: {str(exc)[:200]}"
    if not isinstance(answer, str):
        return {}, (
            "the monitor endpoint returned "
            f"{type(answer).__name__}, not a monitor transcript"
        )
    devices = parse_blockstats(answer)
    if not devices:
        # QEMU reports a refusal in the response body rather than as an HTTP
        # error, so an unknown command, a stopped guest or a permission
        # problem would otherwise read as a guest that wrote nothing.
        detail = " ".join(answer.split())[:200] or "empty response"
        return {}, f"'{MONITOR_COMMAND}' returned no block devices: {detail}"
    return devices, None


def _volume_path(lab: Any, api: Any, volid: str) -> tuple[str | None, str]:
    """Resolve one volid to a host path, API first and `pvesm path` after."""
    store = volid.split(":", 1)[0]
    detail = ""
    try:
        info = api.call(
            "GET", f"/nodes/{lab.NODE}/storage/{store}/content/{volid}"
        )
        if isinstance(info, dict) and info.get("path"):
            return str(info["path"]), ""
        detail = "the storage content endpoint reported no path"
    except lab.LabError as exc:
        detail = str(exc)[:160]
    from . import memflow

    if not memflow.host_ssh_enabled():
        return None, detail
    try:
        proc = memflow.host_run(lab, ["pvesm", "path", volid], timeout=30)
    except lab.LabError as exc:
        return None, f"{detail}; pvesm path failed: {str(exc)[:120]}"
    path = (proc.stdout or "").strip().splitlines()
    if proc.returncode or not path:
        return None, (
            f"{detail}; pvesm path failed: "
            f"{(proc.stderr or '').strip()[:120] or proc.returncode}"
        )
    return path[0].strip(), ""


def image_paths(
    lab: Any, api: Any, vmid: int
) -> tuple[dict[str, str], str | None]:
    """Host paths of the guest's writable disks, as {volid: path}.

    Returns a reason instead of raising when nothing resolves: an
    unresolvable image costs the ``du`` signal, not the measurement.
    """
    try:
        config = api.call("GET", f"/nodes/{lab.NODE}/qemu/{vmid}/config")
    except lab.LabError as exc:
        return {}, f"could not read the guest config: {str(exc)[:200]}"
    if not isinstance(config, dict):
        return {}, "the guest config endpoint returned no configuration"
    volumes = image_volumes(config)
    if not volumes:
        return {}, "the guest config lists no writable disk image"
    paths: dict[str, str] = {}
    problems: list[str] = []
    for key, volid in volumes:
        path, detail = _volume_path(lab, api, volid)
        if path:
            paths[volid] = path
        else:
            problems.append(f"{key} ({volid}): {detail or 'unresolved'}")
    if not paths:
        return {}, "no disk image resolved to a host path: " + "; ".join(
            problems
        )
    return paths, "; ".join(problems) or None


def host_image_sizes(
    lab: Any, paths: dict[str, str]
) -> tuple[dict[str, int], str | None]:
    """Allocated bytes of each backing image, measured on the host with du."""
    from . import memflow

    if not memflow.host_ssh_enabled():
        return {}, (
            "the opt-in [memflow] host SSH channel is off, so the image file "
            "cannot be measured on the host; see docs/memflow.md"
        )
    if not paths:
        return {}, "no disk image resolved to a host path"
    targets = sorted(set(paths.values()))
    if all(target.startswith("/dev/") for target in targets):
        return {}, (
            "the guest's disks are block devices (LVM, ZFS or similar), which "
            "have no file to grow; read the qmp_blockstats signal instead"
        )
    try:
        # --block-size=1 gives allocated bytes. `ls` would give the apparent
        # size of a sparse image, which for an untouched 100 GB qcow2 is 100
        # GB of I/O that never happened.
        proc = memflow.host_run(
            lab, ["du", "--block-size=1", "--", *targets],
            timeout=DU_TIMEOUT_SECONDS,
        )
    except lab.LabError as exc:
        return {}, f"host du failed: {str(exc)[:200]}"
    sizes = parse_du(proc.stdout or "")
    if proc.returncode:
        # du exits non-zero when *one* of several paths is unreadable but
        # still prints the ones that worked, so keep whatever came back.
        detail = (proc.stderr or "").strip()[:200] or str(proc.returncode)
        if not sizes:
            return {}, f"host du failed: {detail}"
        return sizes, f"host du reported a problem: {detail}"
    return sizes, None


# --------------------------------------------------------------------------- #
# Sampling.
# --------------------------------------------------------------------------- #

@dataclass
class _Sample:
    """One reading of every signal that answered."""

    at: float
    state: str = "unknown"
    diskwrite: int | None = None
    devices: dict[str, dict[str, int]] = field(default_factory=dict)
    devices_error: str | None = None
    host_sizes: dict[str, int] = field(default_factory=dict)
    host_error: str | None = None


def _sample(
    lab: Any, api: Any, vmid: int, *,
    paths: dict[str, str], path_problem: str | None, ground_truth: bool,
) -> _Sample:
    status = api.call("GET", f"/nodes/{lab.NODE}/qemu/{vmid}/status/current")
    if not isinstance(status, dict):
        raise lab.LabError(
            f"VMID {vmid} did not report a status; it may not be a QEMU guest "
            f"on {lab.NODE}"
        )
    sample = _Sample(at=time.monotonic(), state=str(
        status.get("status") or "unknown"))
    try:
        sample.diskwrite = int(status.get("diskwrite") or 0)
    except (TypeError, ValueError):
        sample.diskwrite = None
    if not ground_truth:
        return sample
    sample.devices, sample.devices_error = blockstats(lab, api, vmid)
    if paths:
        sample.host_sizes, sample.host_error = host_image_sizes(lab, paths)
    else:
        sample.host_error = path_problem or "no disk image resolved"
    return sample


def _signal(
    signals: dict[str, Any], written: dict[str, int], name: str,
    first: int | None, second: int | None, problem: str | None,
    extra: dict[str, Any] | None = None,
) -> None:
    if first is None or second is None:
        signals[name] = {
            "available": False,
            "reason": problem or "unavailable",
        }
        return
    entry: dict[str, Any] = {
        "available": True,
        "first_bytes": first,
        "second_bytes": second,
        "written_bytes": max(0, second - first),
    }
    if second < first:
        # A cumulative counter only goes backwards when the thing counting it
        # restarted, which is itself activity worth seeing.
        entry["counter_reset"] = True
    if problem:
        entry["warning"] = problem
    if extra:
        entry.update(extra)
    signals[name] = entry
    written[name] = entry["written_bytes"]


def _delta_map(
    first: dict[str, int], second: dict[str, int]
) -> dict[str, int]:
    return {
        key: max(0, second[key] - first.get(key, 0))
        for key in sorted(second)
    }


def measure(
    lab: Any, api: Any, vmid: int, *,
    interval: float = DEFAULT_INTERVAL_SECONDS,
    deadline: float = DEFAULT_DEADLINE_SECONDS,
    ground_truth: bool = False,
) -> dict[str, Any]:
    """Sample every available disk-write signal twice and compare the deltas.

    Read-only. Raises only when the guest itself cannot be measured -- it is
    not a QEMU guest, or it is not running. A signal that is switched off,
    refused or unresolvable is reported as unavailable and the rest of the
    measurement still happens.
    """
    if interval < 0:
        raise lab.LabError("--interval must not be negative")
    if interval > MAX_INTERVAL_SECONDS:
        raise lab.LabError(
            f"--interval must be at most {MAX_INTERVAL_SECONDS:.0f}s"
        )
    # The deadline bounds the round trips, so it has to leave room for the
    # gap that is the actual measurement.
    limit = time.monotonic() + max(deadline, interval + 1.0)
    paths: dict[str, str] = {}
    path_problem: str | None = None
    if ground_truth:
        paths, path_problem = image_paths(lab, api, vmid)
    first = _sample(lab, api, vmid, paths=paths, path_problem=path_problem,
                    ground_truth=ground_truth)
    if first.state != "running":
        raise lab.LabError(
            f"VMID {vmid} is {first.state}, not running; a stopped guest "
            "writes nothing and there is no block layer to ask"
        )
    time.sleep(max(0.0, min(float(interval), limit - time.monotonic())))
    second = _sample(lab, api, vmid, paths=paths, path_problem=path_problem,
                     ground_truth=ground_truth)
    overran = time.monotonic() > limit

    signals: dict[str, Any] = {}
    written: dict[str, int] = {}
    _signal(signals, written, SIGNAL_PROXMOX,
            first.diskwrite, second.diskwrite,
            None if first.diskwrite is not None
            else "status/current reported no diskwrite field")
    if ground_truth:
        both = bool(first.devices and second.devices)
        _signal(
            signals, written, SIGNAL_BLOCKSTATS,
            written_from_blockstats(first.devices) if both else None,
            written_from_blockstats(second.devices) if both else None,
            second.devices_error or first.devices_error,
            {"devices": {
                device: max(
                    0,
                    int(stats.get("wr_bytes", 0))
                    - int(first.devices.get(device, {}).get("wr_bytes", 0)),
                )
                for device, stats in sorted(second.devices.items())
            }} if both else None,
        )
        measured = bool(first.host_sizes and second.host_sizes)
        _signal(
            signals, written, SIGNAL_HOST_DU,
            sum(first.host_sizes.values()) if measured else None,
            sum(second.host_sizes.values()) if measured else None,
            second.host_error or first.host_error,
            {"paths": _delta_map(first.host_sizes, second.host_sizes)}
            if measured else None,
        )

    conflicts = disagreements(written)
    ground = [name for name in written if name != SIGNAL_PROXMOX]
    if any(written.values()):
        writing: bool | None = True
    elif ground:
        writing = False
    else:
        # Only the cached counter answered. It cannot prove idleness -- that
        # is the whole reason this command exists -- so refuse to claim it.
        writing = None

    result: dict[str, Any] = {
        "vmid": int(vmid),
        "kind": "qemu",
        "ground_truth": bool(ground_truth),
        "interval_seconds": float(interval),
        "elapsed_seconds": round(second.at - first.at, 3),
        "signals": signals,
        "written_bytes": written,
        "disagreement": conflicts,
        "writing": writing,
    }
    if overran:
        result["deadline_exceeded"] = True
    notes: list[str] = []
    if conflicts:
        notes.append(
            "The signals disagree, which is the diagnostically useful case: a "
            "signal reading zero while another reads bytes is measuring "
            "something the first one cannot see. Trust qmp_blockstats over "
            "proxmox_diskwrite, and read host_image_du as allocation growth "
            "rather than as write volume."
        )
    if writing is None:
        # Telling an operator to "rerun with --ground-truth" when they already
        # passed it wastes the run that just proved the signals are missing.
        remedy = (
            "rerun with --ground-truth."
            if not ground_truth
            else (
                "--ground-truth was asked for and neither extra signal was "
                "available on this install: see each signal's reason above. "
                "qmp_blockstats needs an API token with Sys.Audit on the "
                "guest, and host_image_du needs file-backed storage -- a disk "
                "on LVM or ZFS is a block device with no file to grow."
            )
        )
        notes.append(
            "Only the Proxmox diskwrite counter answered. It has been seen "
            "reading 0 for an entire session on a writing qcow2 guest, so "
            "this run cannot tell an idle guest from a stalled counter; "
            + remedy
        )
    if notes:
        result["note"] = " ".join(notes)
    return result


# --------------------------------------------------------------------------- #
# Command.
# --------------------------------------------------------------------------- #

def cmd_disk_activity(lab: Any, args: Any) -> None:
    import json

    ground_truth = bool(getattr(args, "ground_truth", False))
    lease = getattr(args, "lease", None)
    if ground_truth:
        # Guard before anything reaches the guest: --ground-truth drives the
        # QEMU monitor and reads the guest's image file on the host, the same
        # two boundaries 'console screenshot --via monitor' crosses, so it is
        # bound to a lease that owns the guest in the same way.
        if not lease:
            raise lab.LabError(
                "guest disk-activity --ground-truth requires --lease: it "
                "drives the QEMU monitor and reads the guest's image file on "
                "the host, so it stays bound to a lease that owns the guest"
            )
        lab.require_lease_resource(lab.load_lease(lease), "qemu", args.vmid)
    api = lab.ProxmoxAPI()
    result = measure(
        lab, api, args.vmid,
        interval=float(getattr(args, "interval", DEFAULT_INTERVAL_SECONDS)),
        deadline=float(getattr(args, "timeout", DEFAULT_DEADLINE_SECONDS)),
        ground_truth=ground_truth,
    )
    lab.audit(
        "guest-disk-activity", lease=lease or "", vmid=int(args.vmid),
        ground_truth=ground_truth,
        interval_seconds=result["interval_seconds"],
        proxmox_bytes=result["written_bytes"].get(SIGNAL_PROXMOX),
        blockstats_bytes=result["written_bytes"].get(SIGNAL_BLOCKSTATS),
        host_du_bytes=result["written_bytes"].get(SIGNAL_HOST_DU),
        disagreements=len(result["disagreement"]),
        sync=False,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
