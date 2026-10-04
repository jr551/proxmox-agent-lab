"""Guest lifecycle over the proxmox seam: create/clone/start/stop/destroy, run/probe/list.

The slim rework module (docs/rework-plan.md §D control mapping, §E guest_*
shapes). Everything here goes through the injected :mod:`proxmox_agent_lab.proxmox`
seam one argv at a time, and every mutation belongs to a lease:

* the SQLite registry (``store.resources``) is the ownership proof --
  :func:`require_owned` is the gate and it is store-only (zero seam calls, so
  every refusal raises before anything is driven);
* the durable copy is the §F guest metadata -- :func:`metadata_for` /
  :func:`stamp_guest` delegate the format strings to :mod:`leases` and write
  ``tags`` + ``pxl-lease=/pxl-expiry=`` description in one call wherever the
  create command allows it (``qm create``/``pct create``), because a crash
  between two calls is exactly how untagged guests are born;
* registry-vouched guests -- a ``policy="retain"`` row or a config template
  (``template: 1``) -- are clone sources and read-only surface only.
  :func:`require_owned` refuses the retain rows it can see in the registry;
  ``destroy`` re-checks both flavors itself (plus the ``pxl`` tag) and always
  refuses them.

Read-only probes (:func:`cmd_probe`, :func:`cmd_list`) gate nothing and audit
nothing; every mutating handler audits with identity fields only -- never
typed text, file contents, or full command lines.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from typing import Any

from . import leases as leases_module
from . import proxmox as proxmox_module
from . import store as store_module
from .errors import LabError

_CONFIG_LINE = re.compile(r"^([A-Za-z0-9_-]+):\s?(.*)$")
_PXL_EXPIRY = re.compile(r"pxl-expiry=(\d+)")
_SNAP_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,39}$")
_ISO_VOLID = re.compile(r"^[A-Za-z0-9_.-]+:iso/[A-Za-z0-9_.+-]+$")
_NIC_MODELS = frozenset({
    "virtio", "e1000", "rtl8139", "pcnet", "ne2k_pci", "vmxnet3",
})
_DISK_BUSES = frozenset({"scsi", "ide"})
_VGA_MODELS = frozenset({"std", "cirrus", "vmware", "none"})
_OSTYPES = frozenset({"other", "wxp", "w2k", "l24", "l26", "solaris"})
_MACHINE = re.compile(r"^(pc|q35)(-[A-Za-z0-9._+-]+)?$")
_CPU_TYPE = re.compile(r"^[A-Za-z0-9_+-]{1,32}$")
_BRIDGE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,14}$")
_BOOT = re.compile(
    r"^order=(ide[0-3]|scsi[0-9]|floppy|net0|sata[0-5])"
    r"(;(ide[0-3]|scsi[0-9]|floppy|net0|sata[0-5]))*$"
)
_MAX_SNAPSHOT_DESCRIPTION = 200

DEFAULT_STOP_TIMEOUT = 120
DEFAULT_RUN_TIMEOUT = 300


# -- guest metadata (docs/rework-plan.md §F; format strings live in leases) --

def metadata_for(lease_id: str, expires_at: int) -> tuple[str, str]:
    """The ``(tags, description)`` pair stamping one guest as this lease's."""
    return (
        leases_module.metadata_tags(lease_id),
        leases_module.metadata_description(lease_id, expires_at),
    )


def stamp_guest(
    prox: Any, kind: str, vmid: int, lease_id: str, expires_at: int
) -> None:
    """Write one guest's pxl metadata (tags + expiry line) in a single call."""
    tags, description = metadata_for(lease_id, expires_at)
    prox.set_metadata(kind, vmid, tags=tags, description=description)


# -- the ownership gate ----------------------------------------------------

def _open_store(lab: Any) -> store_module.Store:
    """The lab database under this lab's state root."""
    return store_module.Store(Path(lab.STATE_ROOT) / "lab.db")


def _registry_row(
    store: store_module.Store, lease_id: str, kind: str | None, vmid: int
) -> dict | None:
    for row in store.resources_for(lease_id):
        if row.get("destroyed_at"):
            continue
        if kind is not None and row["kind"] != kind:
            continue
        if row["vmid"] is None or int(row["vmid"]) != int(vmid):
            continue
        return dict(row)
    return None


def _unregistered(lease_id: str, kind: str | None, vmid: int) -> LabError:
    hint_kind = kind or "qemu|lxc"
    return LabError(
        f"vmid {vmid} is not a registered guest of lease {lease_id}: nothing "
        f"local vouches for it, so it is never driven or destroyed. Register "
        f"it with 'proxmox-lab lease-register --lease {lease_id} "
        f"--kind {hint_kind} --vmid {vmid}' first."
    )


def _vouched(row: dict | None, cfg: dict | None, kind: str, vmid: int) -> LabError:
    reasons = []
    if row is not None and row.get("policy") == "retain":
        reasons.append("its registry row carries policy=retain")
    if cfg is not None and _is_template(cfg):
        reasons.append("its config is a template (template: 1)")
    return LabError(
        f"{kind} {vmid} is registry-vouched ({'; '.join(reasons) or 'no reason'}): "
        f"a template or a retained guest is a clone source. guest destroy "
        f"and lease-end leave it in place. Delete the clones, not the source."
    )


def require_owned(lab: Any, lease_id: str, kind: str | None, vmid: int) -> dict:
    """The live ``store.resources`` row for ``(lease_id, kind, vmid)``.

    The ownership gate for every mutating operation. It reads the registry
    only -- no seam call, no config read -- so a refusal always raises before
    anything is driven. ``kind=None`` matches whichever kind the registry
    recorded (handlers whose CLI takes no ``--kind``).

    Refused: a missing (or already destroyed) row, naming ``lease-register``
    as the remedy; and a registry-vouched row (``policy="retain"``), which is
    a clone source and read-only surface, never a lease-driven machine.
    """
    with _open_store(lab) as store:
        row = _registry_row(store, lease_id, kind, vmid)
    if row is None:
        raise _unregistered(lease_id, kind, vmid)
    if row.get("policy") == "retain":
        raise _vouched(row, None, str(row["kind"]), int(vmid))
    return row


# -- seam helpers ----------------------------------------------------------

def _make_proxmox(config: Any) -> proxmox_module.Proxmox:
    """The proxmox seam for this configuration (tests substitute a double)."""
    return proxmox_module.from_config(config)


def _ssh_of(seam: Any) -> Any:
    """The ssh transport underneath a proxmox seam (raw ``qm config`` reads)."""
    return getattr(seam, "_ssh", seam)


def _guest_config(prox: Any, kind: str, vmid: int) -> dict | None:
    """The guest's ``qm config``/``pct config`` key/values, or ``None``.

    Read-only and best-effort: an unreachable seam, a rejected command, or
    output that is not text all read as "unknown", never as a reason to act.
    """
    tool = "pct" if kind == "lxc" else "qm"
    try:
        result = _ssh_of(prox).run(
            [tool, "config", str(vmid)], timeout=proxmox_module.DEFAULT_TIMEOUT
        )
    except LabError:
        return None
    if not result.ok:
        return None
    raw = result.stdout
    if isinstance(raw, (bytes, bytearray)):
        text = bytes(raw).decode("utf-8", "replace")
    elif isinstance(raw, str):
        text = raw
    else:
        return None
    parsed: dict[str, str] = {}
    for line in text.splitlines():
        match = _CONFIG_LINE.match(line)
        if match and match.group(1) not in parsed:
            parsed[match.group(1)] = match.group(2)
    return parsed or None


def _is_template(cfg: dict) -> bool:
    return str(cfg.get("template", "")).strip() in ("1", "on", "true")


def _has_pxl_tag(cfg: dict) -> bool:
    tokens = {token.strip() for token in str(cfg.get("tags", "")).split(";")}
    return (leases_module.OWNERSHIP_TAG in tokens
            or leases_module.LEGACY_OWNERSHIP_TAG in tokens)


def _pxl_expiry(cfg: dict) -> int | None:
    match = _PXL_EXPIRY.search(str(cfg.get("description", "")))
    return int(match.group(1)) if match else None


def _text(raw: Any) -> str:
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw).decode("utf-8", "replace")
    return raw if isinstance(raw, str) else ""


def _emit(payload: dict) -> dict:
    print(json.dumps(payload, indent=2, sort_keys=True))
    return payload


# -- registry writes -------------------------------------------------------

_GIB = 1024 ** 3


def _format_free(nbytes: int) -> str:
    """Free space as GiB when it lands on a GiB boundary, plus the raw bytes."""
    if nbytes >= 0 and nbytes % _GIB == 0:
        return f"{nbytes // _GIB} GiB ({nbytes} bytes)"
    return f"{nbytes} bytes"


def _assert_disk_fits(prox: Any, storage: str, disk_gb: int) -> None:
    """Refuse a fresh disk larger than the free space on ``storage``.

    This is a read of storage status, then a hard stop, before any create.
    Memory is not part of the check: RAM headroom is the caller's judgment.
    """
    if disk_gb <= 0:
        raise LabError(f"disk size must be a positive integer in GB, not {disk_gb}")
    row = next(
        (
            item for item in prox.storage_status()
            if isinstance(item, dict) and item.get("storage") == storage
        ),
        None,
    )
    if row is None:
        raise LabError(
            f"storage {storage!r} is not on this node, so a {disk_gb} GB "
            f"disk cannot be checked against free space. "
            f"'proxmox-lab storage status' lists the stores."
        )
    avail = row.get("avail")
    try:
        free = int(avail)
    except (TypeError, ValueError):
        raise LabError(
            f"storage {storage!r} did not report free space "
            f"(avail={avail!r}); refusing a {disk_gb} GB disk"
        ) from None
    if disk_gb * _GIB > free:
        raise LabError(
            f"refusing a {disk_gb} GB disk on storage {storage!r}: "
            f"{_format_free(free)} free. Pick a smaller disk. "
            f"'proxmox-lab storage status' shows each store."
        )


def _lease_for_mutation(lab: Any, lease_id: str) -> dict:
    """The lease backing a new guest; refuses unknown and dead leases."""
    with _open_store(lab) as store:
        row = store.get_lease(lease_id)
    if row is None:
        raise LabError(
            f"unknown lease {lease_id!r}: every guest belongs to a lease. "
            f"Open one with 'proxmox-lab lease-begin --purpose ...'."
        )
    if row["state"] in store_module.TERMINAL_STATES:
        raise LabError(
            f"lease {lease_id} is {row['state']} and owns nothing any more. "
            f"Open a new one with 'proxmox-lab lease-begin'."
        )
    return row


def _register_resource(
    lab: Any, lease_id: str, kind: str, vmid: int, *, name: str | None
) -> None:
    with _open_store(lab) as store:
        store.register_resource(
            lease_id, kind, vmid, name=name, policy="disposable"
        )


# -- handlers --------------------------------------------------------------

def cmd_nextid(lab: Any, args: Any) -> dict:
    """The cluster's next free VMID. Read-only.

    Pass the printed id to ``guest create``. This command does not create
    anything, and create does not call it: the agent has to choose the id
    it just read, because a collision destroys a real machine.
    """
    del args
    prox = _make_proxmox(lab.CONFIG)
    return _emit({"vmid": prox.cluster_nextid()})


def cmd_create(lab: Any, args: Any) -> dict:
    """Create a lease-owned guest: clone the template, or build fresh.

    The clone path restamps the metadata after the clone (a clone copies the
    source's expiry, which is never ours to trust). The fresh path passes
    tags+description in the same ``qm create``/``pct create`` call. Either way
    the resource is registered BEFORE any start, so a half-built guest is
    still visible to cleanup.
    """
    lease_id, vmid, kind = str(args.lease), int(args.vmid), str(args.kind)
    if vmid <= 0:
        raise LabError(f"vmid must be a positive integer, not {vmid}")
    if getattr(args, "memory", None) is not None and int(args.memory) <= 0:
        raise LabError("--memory must be a positive integer in MB")
    if getattr(args, "cores", None) is not None and int(args.cores) <= 0:
        raise LabError("--cores must be a positive integer")
    if getattr(args, "iso", None) and kind != "qemu":
        raise LabError("--iso is only for a fresh qemu guest")
    lease = _lease_for_mutation(lab, lease_id)
    expires_at = int(lease["expires_at"])
    name = getattr(args, "name", None) or f"pxl-{vmid}"
    tags, description = metadata_for(lease_id, expires_at)
    prox = _make_proxmox(lab.CONFIG)
    template = _template_vmid(lab, args)
    if template is None:
        if kind == "lxc":
            if not args.ostemplate:
                raise LabError(
                    "a fresh lxc guest needs --ostemplate "
                    "(e.g. local:vztmpl/debian-12.tar.zst)"
                )
            rootfs = None
            if args.storage:
                disk_gb = int(args.disk_gb or 8)
                _assert_disk_fits(prox, str(args.storage), disk_gb)
                rootfs = f"{args.storage}:{disk_gb}"
            prox.lxc_create(
                vmid,
                ostemplate=str(args.ostemplate),
                hostname=name,
                tags=tags,
                description=description,
                rootfs=rootfs,
            )
        else:
            iso = getattr(args, "iso", None) or None
            if iso is not None and _ISO_VOLID.fullmatch(str(iso)) is None:
                raise LabError(
                    "--iso must be a storage volid like local:iso/name.iso"
                )
            bus = str(getattr(args, "disk_bus", None) or "scsi")
            if bus not in _DISK_BUSES:
                raise LabError("--disk-bus must be scsi or ide")
            nic = str(getattr(args, "nic", None) or "virtio")
            if nic not in _NIC_MODELS:
                raise LabError(
                    "--nic must be one of " + ", ".join(sorted(_NIC_MODELS))
                )
            bridge = str(getattr(args, "bridge", None) or "vmbr0")
            if _BRIDGE.fullmatch(bridge) is None:
                raise LabError("--bridge must be a bridge name like vmbr0")
            cpu = _hardware_token(getattr(args, "cpu", None), _CPU_TYPE, "--cpu")
            machine = _hardware_token(
                getattr(args, "machine", None), _MACHINE, "--machine"
            )
            vga = getattr(args, "vga", None) or None
            if vga is not None and vga not in _VGA_MODELS:
                raise LabError(
                    "--vga must be one of " + ", ".join(sorted(_VGA_MODELS))
                )
            ostype = getattr(args, "ostype", None) or None
            if ostype is not None and ostype not in _OSTYPES:
                raise LabError(
                    "--ostype must be one of " + ", ".join(sorted(_OSTYPES))
                )
            boot = getattr(args, "boot", None) or None
            if boot is not None and _BOOT.fullmatch(str(boot)) is None:
                raise LabError(
                    "--boot must look like order=ide2;ide0 "
                    "(ide, scsi, sata, floppy, or net0)"
                )
            disk = None
            if args.storage:
                disk_gb = int(args.disk_gb or (8 if bus == "ide" else 32))
                _assert_disk_fits(prox, str(args.storage), disk_gb)
                disk = f"{args.storage}:{disk_gb}"
            net0 = None
            if nic != "virtio" or bridge != "vmbr0":
                net0 = f"{nic},bridge={bridge}"
            prox.qemu_create(
                vmid,
                name=name,
                tags=tags,
                description=description,
                scsi0=disk if bus == "scsi" else None,
                ide0=disk if bus == "ide" else None,
                memory=args.memory,
                cores=args.cores,
                iso=str(iso) if iso else None,
                net0=net0 or "virtio,bridge=vmbr0",
                cpu=cpu,
                machine=machine,
                vga=vga,
                ostype=ostype,
                boot=boot,
            )
    else:
        if getattr(args, "iso", None):
            raise LabError(
                "--iso is only for a fresh qemu guest; pass --fresh"
            )
        source_kind = _clone_source_kind(lab, prox, template)
        if source_kind != kind:
            raise LabError(
                f"template {template} is a {source_kind} guest, not {kind}. "
                f"Pass --kind {source_kind}, or --fresh to build without cloning."
            )
        prox.clone(kind, template, vmid, name=name)
        stamp_guest(prox, kind, vmid, lease_id, expires_at)
    _register_resource(lab, lease_id, kind, vmid, name=name)
    if args.start:
        prox.start(kind, vmid)
    state = prox.status(kind, vmid)
    lab.audit(
        "guest-create",
        lease=lease_id,
        vmid=vmid,
        kind=kind,
        name=name,
        template=template,
        started=bool(args.start),
    )
    return _emit({
        "lease_id": lease_id,
        "vmid": vmid,
        "kind": kind,
        "name": name,
        "state": state,
        "tags": tags,
        "pxl_expiry": expires_at,
    })


def _template_vmid(lab: Any, args: Any) -> int | None:
    """The template to clone from, or ``None`` for a fresh build.

    ``--fresh`` (or an explicitly empty ``--template``) means fresh; otherwise
    ``--template`` wins and ``[pve] template_vmid`` is the default. An
    unconfigured default (0) is fresh too -- never a clone from vmid 0.
    """
    if getattr(args, "fresh", False):
        if args.template not in (None, ""):
            raise LabError("--fresh contradicts --template: pick one")
        return None
    if args.template is not None:
        text = str(args.template).strip()
        if not text:
            return None
        try:
            value = int(text)
        except ValueError:
            raise LabError(
                f"--template must be a template vmid or empty, not {text!r}"
            ) from None
        if value <= 0:
            raise LabError(f"--template must be a template vmid, not {text!r}")
        return value
    return int(lab.CONFIG.pve.template_vmid or 0) or None


def cmd_clone(lab: Any, args: Any) -> dict:
    """Clone a registry-vouched template into a new lease-owned guest.

    The source need NOT be lease-owned: a ``policy="retain"`` registry row or
    a config template (``template: 1``) is vouched for by the registry alone
    and is accepted as a clone source. Only full copies exist on this seam.
    """
    lease_id, vmid, source = str(args.lease), int(args.vmid), int(args.source)
    if not args.full:
        raise LabError(
            "only full copies are supported: this seam's clone always makes "
            "a full copy of the source"
        )
    lease = _lease_for_mutation(lab, lease_id)
    expires_at = int(lease["expires_at"])
    prox = _make_proxmox(lab.CONFIG)
    kind = _clone_source_kind(lab, prox, source)
    prox.clone(kind, source, vmid, name=args.name)
    stamp_guest(prox, kind, vmid, lease_id, expires_at)
    _register_resource(lab, lease_id, kind, vmid, name=args.name)
    state = prox.status(kind, vmid)
    lab.audit(
        "guest-clone",
        lease=lease_id,
        vmid=vmid,
        kind=kind,
        source=source,
        name=args.name,
    )
    return _emit({
        "lease_id": lease_id,
        "vmid": vmid,
        "source": source,
        "name": args.name,
        "state": state,
        "upid": None,
    })


def _clone_source_kind(lab: Any, prox: Any, source: int) -> str:
    """The kind of a vouched clone source; refused when nobody vouches."""
    with _open_store(lab) as store:
        for lease in store.list_leases(include_ended=True):
            row = _registry_row(store, str(lease["id"]), None, source)
            if row is not None:
                if row.get("policy") == "retain":
                    return str(row["kind"])
                break
    for kind in ("qemu", "lxc"):
        cfg = _guest_config(prox, kind, source)
        if cfg is not None and _is_template(cfg):
            return kind
    raise LabError(
        f"clone source {source} is not a registry-vouched template: it is "
        f"neither a retain-policy registry row nor a template (template: 1). "
        f"Clone from a registered template, or turn {source} into one."
    )


def cmd_start(lab: Any, args: Any) -> dict:
    lease_id, vmid = str(args.lease), int(args.vmid)
    row = require_owned(lab, lease_id, None, vmid)
    kind = str(row["kind"])
    prox = _make_proxmox(lab.CONFIG)
    prox.start(kind, vmid)
    state = prox.status(kind, vmid)
    lab.audit("guest-start", lease=lease_id, vmid=vmid, kind=kind)
    return _emit({
        "lease_id": lease_id,
        "vmid": vmid,
        "state": state,
        "graceful": None,
    })


def cmd_stop(lab: Any, args: Any) -> dict:
    """Stop a guest: graceful shutdown first, hard stop as the fallback.

    ``--timeout`` bounds the graceful wait (§D teardown shape: shutdown, wait
    for stopped, then ``stop``). It reports which path actually happened --
    a graceful stop is never assumed.
    """
    lease_id, vmid = str(args.lease), int(args.vmid)
    row = require_owned(lab, lease_id, None, vmid)
    kind = str(row["kind"])
    prox = _make_proxmox(lab.CONFIG)
    timeout = int(args.timeout or 0)
    graceful = False
    if timeout > 0:
        graceful = prox.shutdown(kind, vmid, timeout=float(timeout))
    if not graceful:
        prox.stop(kind, vmid)
    state = prox.status(kind, vmid)
    lab.audit(
        "guest-stop", lease=lease_id, vmid=vmid, kind=kind, graceful=graceful
    )
    return _emit({
        "lease_id": lease_id,
        "vmid": vmid,
        "state": state,
        "graceful": graceful,
    })


def cmd_destroy(lab: Any, args: Any) -> dict:
    """Destroy a lease-owned guest -- the one irreversible command.

    ``--confirm`` is demanded before any seam call. The gate
    (:func:`require_owned`) refuses unregistered and retain rows; this handler
    then re-checks vouching itself and always refuses templates and retain
    rows, and refuses a guest whose config carries no ``pxl`` tag (when the
    config is readable -- the registry row is the pxl proof when it is not).
    """
    lease_id, vmid = str(args.lease), int(args.vmid)
    if not args.confirm:
        raise LabError(
            "guest destroy is irreversible; pass --confirm to mean it "
            "(there is no interactive prompt)"
        )
    row = require_owned(lab, lease_id, None, vmid)
    kind = str(row["kind"])
    with _open_store(lab) as store:
        owner = store.owner_elsewhere(lease_id, kind, vmid)
    if owner:
        # `require_owned` only asks whether THIS lease has a live row. A
        # vmid registered to two live leases (dual registration is reachable:
        # an expired-but-active lease still counts) would let one lease
        # destroy a machine the other is using.
        raise LabError(
            f"{kind} {vmid} is also registered to lease {owner}, which is "
            f"still live: a guest another lease owns is never destroyed from "
            f"under it (end that lease first)"
        )
    prox = _make_proxmox(lab.CONFIG)
    cfg = _guest_config(prox, kind, vmid)
    if row.get("policy") == "retain" or (cfg is not None and _is_template(cfg)):
        raise _vouched(row, cfg, kind, vmid)
    if cfg is not None and not _has_pxl_tag(cfg):
        raise LabError(
            f"{kind} {vmid} carries no pxl tag (tags={cfg.get('tags', '')!r}): "
            f"only pxl-tagged, lease-registered guests are ever destroyed, so "
            f"a machine somebody else owns can never be taken for ours."
        )
    try:
        if prox.status(kind, vmid) == "running":
            if not prox.shutdown(kind, vmid):
                prox.stop(kind, vmid)
    except proxmox_module.ProxmoxError:
        # Vanished between the ownership check and now -- a destroy that
        # finds nothing to destroy is still a success.
        pass
    prox.destroy(kind, vmid)
    with _open_store(lab) as store:
        store.mark_destroyed(lease_id, kind, vmid)
    lab.audit(
        "guest-destroy", lease=lease_id, vmid=vmid, kind=kind, purged=True
    )
    return _emit({
        "lease_id": lease_id,
        "vmid": vmid,
        "destroyed": True,
        "purged": True,
    })


def _probe_kind(prox: Any, vmid: int) -> tuple[str | None, str | None]:
    """``(kind, state)`` for a vmid, or ``(None, None)`` when neither tool knows it."""
    for kind in ("qemu", "lxc"):
        try:
            return kind, prox.status(kind, vmid)
        except proxmox_module.ProxmoxError:
            continue
    return None, None


def _safe_ip(prox: Any, vmid: int) -> str | None:
    try:
        return prox.guest_ip(vmid)
    except proxmox_module.ProxmoxError:
        return None


def _ipv4_host(text: str) -> str | None:
    """An IPv4 host from ``10.1.2.3`` or ``10.1.2.3/24``, never loopback."""
    host = text.split("/", 1)[0].strip()
    parts = host.split(".")
    if len(parts) != 4:
        return None
    try:
        numbers = [int(part) for part in parts]
    except ValueError:
        return None
    if any(number < 0 or number > 255 for number in numbers) or numbers[0] == 127:
        return None
    return ".".join(str(number) for number in numbers)


def _first_lxc_ipv4(records: Any) -> str | None:
    if not isinstance(records, list):
        return None
    for item in records:
        if not isinstance(item, dict) or item.get("name") == "lo":
            continue
        inet = item.get("inet")
        if not isinstance(inet, str):
            continue
        for piece in inet.replace(",", " ").split():
            address = _ipv4_host(piece)
            if address is not None:
                return address
    return None


def _lxc_ip(prox: Any, vmid: int) -> str | None:
    """First non-loopback IPv4, or ``None`` when the interface read fails."""
    try:
        return _first_lxc_ipv4(prox.lxc_interfaces(vmid))
    except proxmox_module.ProxmoxError:
        return None


def cmd_probe(lab: Any, args: Any) -> dict:
    """How one guest can be reached, right now. Read-only."""
    vmid = int(args.vmid)
    prox = _make_proxmox(lab.CONFIG)
    kind, state = _probe_kind(prox, vmid)
    exists = kind is not None
    running = bool(exists and state == "running")
    if kind == "qemu":
        agent_ok = bool(prox.guest_ping(vmid))
        ip = _safe_ip(prox, vmid) if agent_ok else None
        channel = "agent" if agent_ok else "ssh"
    elif kind == "lxc":
        # pct exec is the native channel and needs no agent; it is usable
        # exactly while the container runs. The address comes from the
        # container interface list when that read works.
        agent_ok = running
        ip = _lxc_ip(prox, vmid)
        channel = "pct"
    else:
        agent_ok = False
        ip = None
        channel = None
    return _emit({
        "vmid": vmid,
        "exists": exists,
        "running": running,
        "kind": kind,
        "agent_ok": agent_ok,
        "ip": ip,
        "channel": channel,
    })


def cmd_list(lab: Any, args: Any) -> dict:
    """Every registered guest (optionally one lease's), joined with live state.

    Read-only: registry rows joined against ``prox.status`` and the guest's
    own metadata. A registered guest the node no longer knows is reported
    ``state="missing"`` -- the registry never silently forgets it.
    """
    prox = _make_proxmox(lab.CONFIG)
    with _open_store(lab) as store:
        if args.lease is not None:
            lease_ids = [str(args.lease)]
        else:
            lease_ids = [
                str(lease["id"])
                for lease in store.list_leases(include_ended=False)
            ]
        rows = [
            row
            for lease_id in lease_ids
            for row in store.resources_for(lease_id)
            if not row.get("destroyed_at")
        ]
    guests = []
    for row in sorted(rows, key=lambda item: int(item["vmid"])):
        kind, vmid = str(row["kind"]), int(row["vmid"])
        try:
            state = prox.status(kind, vmid)
        except proxmox_module.ProxmoxError:
            state = "missing"
        cfg = _guest_config(prox, kind, vmid)
        guests.append({
            "vmid": vmid,
            "kind": kind,
            "name": row.get("name"),
            "lease_id": str(row["lease_id"]),
            "state": state,
            "tags": cfg.get("tags") if cfg is not None else None,
            "pxl_expiry": _pxl_expiry(cfg) if cfg is not None else None,
        })
    return _emit({"guests": guests})


def _hardware_token(value: str | None, pattern: re.Pattern[str], flag: str) -> str | None:
    if value is None or value == "":
        return None
    if pattern.fullmatch(str(value)) is None:
        raise LabError(f"{flag} is not a plain hardware name")
    return str(value)


def _snapshot_name(name: str) -> str:
    if name == "current" or _SNAP_NAME.fullmatch(name) is None:
        raise LabError(
            "snapshot name must start with a letter and use only letters, "
            "digits, '_' and '-' (not 'current')"
        )
    return name


def _snapshot_description(raw: str | None) -> str | None:
    if raw is None or raw == "":
        return None
    if "\n" in raw or "\r" in raw or len(raw) > _MAX_SNAPSHOT_DESCRIPTION:
        raise LabError(
            "snapshot description must be one line of at most "
            f"{_MAX_SNAPSHOT_DESCRIPTION} characters"
        )
    return raw


def _refuse_shared(lab: Any, lease_id: str, kind: str, vmid: int) -> None:
    """Refuse when another live lease also registers this guest."""
    with _open_store(lab) as store:
        owner = store.owner_elsewhere(lease_id, kind, vmid)
    if owner:
        raise LabError(
            f"{kind} {vmid} is also registered to lease {owner}, which is "
            f"still live: a guest another lease owns is never snapshotted "
            f"away or turned into a template from under it"
        )


def _snapshot_children(records: list, name: str) -> list[str]:
    """Real snapshots whose ``parent`` is ``name``.

    ``current`` is the live state, not a snapshot the caller can delete, so
    it does not block rollback. Refusing a rollback that still has children
    is an idea from ProxmoxMCP-Plus (MIT); the check reads our snapshot list.
    """
    children: list[str] = []
    for item in records:
        if not isinstance(item, dict):
            continue
        child = str(item.get("name") or "")
        if child in ("", "current", name):
            continue
        if item.get("parent") == name:
            children.append(child)
    return children


def _require_stopped(prox: Any, kind: str, vmid: int, why: str) -> None:
    state = prox.status(kind, vmid)
    if state != "stopped":
        raise LabError(
            f"{kind} {vmid} must be stopped before {why} (status={state})"
        )


def cmd_snapshot(lab: Any, args: Any) -> dict:
    """List, create, delete or roll back snapshots of a lease-owned guest.

    List and create need the lease. Delete and rollback also need
    ``--confirm`` and a guest no other live lease holds, and rollback
    refuses a running guest and a snapshot that still has children.
    Rolling back a disk that is in use, or out from under a child
    snapshot, is how a session loses its machine.
    """
    lease_id, vmid = str(args.lease), int(args.vmid)
    action = str(args.action)
    row = require_owned(lab, lease_id, None, vmid)
    kind = str(row["kind"])
    prox = _make_proxmox(lab.CONFIG)
    if action == "list":
        records = prox.snapshot_list(kind, vmid)
        snapshots = [
            {
                "name": item.get("name"),
                "description": item.get("description") or "",
                "snaptime": item.get("snaptime"),
            }
            for item in records
            if isinstance(item, dict)
        ]
        return _emit({
            "lease_id": lease_id,
            "vmid": vmid,
            "kind": kind,
            "snapshots": snapshots,
        })
    name = _snapshot_name(str(getattr(args, "name", "") or ""))
    if action in ("delete", "rollback") and not getattr(args, "confirm", False):
        raise LabError(
            f"snapshot {action} removes guest state; pass --confirm to mean it "
            "(there is no interactive prompt)"
        )
    if action in ("delete", "rollback"):
        _refuse_shared(lab, lease_id, kind, vmid)
    if action == "rollback":
        _require_stopped(prox, kind, vmid, "rollback")
        children = _snapshot_children(prox.snapshot_list(kind, vmid), name)
        if children:
            listed = ", ".join(children)
            raise LabError(
                f"snapshot {name} has child snapshots ({listed}); "
                f"delete those children first"
            )
        prox.snapshot_rollback(kind, vmid, name)
        lab.audit(
            "guest-snapshot-rollback",
            lease=lease_id, vmid=vmid, kind=kind, name=name,
        )
        return _emit({
            "lease_id": lease_id, "vmid": vmid, "kind": kind,
            "snapshot": name, "rolled_back": True,
        })
    if action == "delete":
        prox.snapshot_delete(kind, vmid, name)
        lab.audit(
            "guest-snapshot-delete",
            lease=lease_id, vmid=vmid, kind=kind, name=name,
        )
        return _emit({
            "lease_id": lease_id, "vmid": vmid, "kind": kind,
            "snapshot": name, "deleted": True,
        })
    if action == "create":
        description = _snapshot_description(getattr(args, "description", None))
        prox.snapshot_create(kind, vmid, name, description=description)
        lab.audit(
            "guest-snapshot-create",
            lease=lease_id, vmid=vmid, kind=kind, name=name,
        )
        return _emit({
            "lease_id": lease_id, "vmid": vmid, "kind": kind,
            "snapshot": name, "created": True,
        })
    raise LabError(f"unknown snapshot action {action!r}")


def cmd_template(lab: Any, args: Any) -> dict:
    """Turn a stopped lease-owned guest into a clone source.

    ``guest create`` will only clone a guest whose config says
    ``template: 1``. This is the command that writes that bit. It is
    irreversible from the lease's point of view: teardown already refuses
    to destroy a template, so the machine outlives the lease.
    """
    lease_id, vmid = str(args.lease), int(args.vmid)
    if not getattr(args, "confirm", False):
        raise LabError(
            "guest template is hard to undo and teardown will not destroy "
            "it afterwards; pass --confirm to mean it "
            "(there is no interactive prompt)"
        )
    row = require_owned(lab, lease_id, None, vmid)
    kind = str(row["kind"])
    _refuse_shared(lab, lease_id, kind, vmid)
    prox = _make_proxmox(lab.CONFIG)
    cfg = _guest_config(prox, kind, vmid)
    if cfg is not None and _is_template(cfg):
        raise LabError(f"{kind} {vmid} is already a template")
    _require_stopped(prox, kind, vmid, "template conversion")
    prox.make_template(kind, vmid)
    lab.audit("guest-template", lease=lease_id, vmid=vmid, kind=kind)
    return _emit({
        "lease_id": lease_id,
        "vmid": vmid,
        "kind": kind,
        "template": True,
    })


def cmd_media(lab: Any, args: Any) -> dict:
    """Change the CD or floppy of a lease-owned qemu guest.

    Setup disks for an old installer are not one image. This swaps the
    media the guest will see at the next read; it does not reboot.
    """
    lease_id, vmid = str(args.lease), int(args.vmid)
    row = require_owned(lab, lease_id, None, vmid)
    kind = str(row["kind"])
    if kind != "qemu":
        raise LabError(f"{kind} {vmid} has no qemu CD or floppy to change")
    cdrom = getattr(args, "cdrom", None) or None
    floppy = getattr(args, "floppy", None) or None
    eject = getattr(args, "eject", None) or None
    if eject is not None and eject not in ("cdrom", "floppy"):
        raise LabError("--eject must be cdrom or floppy")
    if not any((cdrom, floppy, eject)):
        raise LabError("pass --cdrom, --floppy, or --eject")
    if cdrom is not None and _ISO_VOLID.fullmatch(str(cdrom)) is None:
        raise LabError("--cdrom must be a storage volid like local:iso/name.iso")
    if floppy is not None and _ISO_VOLID.fullmatch(str(floppy)) is None:
        raise LabError("--floppy must be a storage volid like local:iso/disk1.img")
    prox = _make_proxmox(lab.CONFIG)
    changed = []
    if eject == "cdrom" or cdrom is not None:
        prox.set_cdrom(vmid, None if eject == "cdrom" else str(cdrom))
        changed.append("cdrom")
    if eject == "floppy" or floppy is not None:
        prox.set_floppy(vmid, None if eject == "floppy" else str(floppy))
        changed.append("floppy")
    lab.audit("guest-media", lease=lease_id, vmid=vmid, slots=",".join(changed))
    return _emit({
        "lease_id": lease_id,
        "vmid": vmid,
        "cdrom": cdrom,
        "floppy": floppy,
        "ejected": eject,
    })


def cmd_run(lab: Any, args: Any) -> dict:
    """Run one command in a lease-owned guest; the guest's real exit code.

    The channel follows the kind: ``pct exec`` for lxc, the qemu guest agent
    for qemu. A non-zero exit is a result, not an error. Only ``argv0`` and
    the exit code are audited -- never the command text or its output.
    """
    lease_id, vmid = str(args.lease), int(args.vmid)
    row = require_owned(lab, lease_id, None, vmid)
    kind = str(row["kind"])
    command = [str(part) for part in (args.command or [])]
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        raise LabError("run needs a command to execute")
    prox = _make_proxmox(lab.CONFIG)
    started = time.monotonic()
    if kind == "lxc":
        result = prox.pct_exec(vmid, command, timeout=float(args.timeout))
    else:
        result = prox.guest_exec(vmid, command, timeout=float(args.timeout))
    duration_ms = int((time.monotonic() - started) * 1000)
    lab.audit(
        "guest-run",
        lease=lease_id,
        vmid=vmid,
        argv0=command[0],
        exit_code=result.exit_code,
    )
    return _emit({
        "lease_id": lease_id,
        "vmid": vmid,
        "exit_code": result.exit_code,
        "stdout": _text(result.stdout),
        "stderr": _text(result.stderr),
        "duration_ms": duration_ms,
    })


# -- CLI surface -----------------------------------------------------------

def register(sub: Any, lab: Any) -> None:
    from .cli import _bind

    guest = sub.add_parser("guest", help="guest lifecycle over the proxmox seam")
    guest_sub = guest.add_subparsers(dest="guest_command", required=True)

    nextid = guest_sub.add_parser(
        "nextid",
        help="next free cluster VMID (read-only; pass it to guest create)",
    )
    nextid.set_defaults(func=_bind(lab, cmd_nextid))

    create = guest_sub.add_parser(
        "create",
        help="create a lease-owned guest from the template, or fresh",
    )
    create.add_argument("--lease", required=True)
    create.add_argument("--vmid", type=int, required=True)
    create.add_argument("--name")
    create.add_argument("--memory", type=int, help="qemu only")
    create.add_argument("--cores", type=int, help="qemu only")
    create.add_argument("--start", action="store_true",
                        help="start the guest once it is registered")
    create.add_argument("--ostemplate",
                        help="LXC ostemplate (fresh lxc create only)")
    create.add_argument("--storage",
                        help="fresh-create target storage, e.g. local-lvm "
                             "(LXC rootfs / QEMU scsi0)")
    create.add_argument("--disk-gb", type=int, default=None,
                        help="rootfs/disk size in GB for fresh creates "
                             "(default: 8 LXC, 32 QEMU). Refused when "
                             "larger than the free space on --storage")
    create.add_argument("--kind", choices=("qemu", "lxc"), default="qemu")
    create.add_argument(
        "--template",
        help="source template vmid; empty (or --fresh) builds from scratch "
             "(default: [pve] template_vmid)",
    )
    create.add_argument("--fresh", action="store_true",
                        help="build from scratch instead of cloning a template")
    create.add_argument(
        "--iso",
        help="fresh qemu only: boot this CD volid (local:iso/name.iso). "
             "The image's own menu may still default to the hard disk",
    )
    create.add_argument(
        "--disk-bus", choices=("scsi", "ide"), default="scsi",
        help="fresh qemu disk bus (default scsi; ide for old installers)",
    )
    create.add_argument(
        "--nic", default="virtio",
        help="fresh qemu NIC model (default virtio; pcnet or ne2k_pci "
             "for guests with no virtio driver)",
    )
    create.add_argument(
        "--bridge", default="vmbr0",
        help="fresh qemu bridge for net0 (default vmbr0)",
    )
    create.add_argument("--cpu", help="fresh qemu CPU type, for example pentium")
    create.add_argument(
        "--machine", help="fresh qemu machine, pc or q35",
    )
    create.add_argument(
        "--vga", choices=("std", "cirrus", "vmware", "none"),
        help="fresh qemu display",
    )
    create.add_argument(
        "--ostype",
        choices=("other", "wxp", "w2k", "l24", "l26", "solaris"),
        help="fresh qemu OS hint for Proxmox",
    )
    create.add_argument(
        "--boot",
        help="fresh qemu boot order, for example order=floppy;ide2;ide0",
    )
    create.set_defaults(func=_bind(lab, cmd_create))

    clone = guest_sub.add_parser(
        "clone", help="clone a registry-vouched template into a new guest"
    )
    clone.add_argument("--lease", required=True)
    clone.add_argument("--vmid", type=int, required=True)
    clone.add_argument("--source", type=int, required=True)
    clone.add_argument("--name")
    clone.add_argument("--full", action="store_true", default=True,
                       help="full copy (the only clone mode on this seam)")
    clone.set_defaults(func=_bind(lab, cmd_clone))

    media = guest_sub.add_parser(
        "media", help="change the CD or floppy of a lease-owned qemu guest"
    )
    media.add_argument("--lease", required=True)
    media.add_argument("--vmid", type=int, required=True)
    media.add_argument("--cdrom", help="volid to put in the CD drive")
    media.add_argument("--floppy", help="volid to put in the floppy drive")
    media.add_argument(
        "--eject", choices=("cdrom", "floppy"),
        help="remove that drive's media",
    )
    media.set_defaults(func=_bind(lab, cmd_media))

    start = guest_sub.add_parser("start", help="start a lease-owned guest")
    start.add_argument("--lease", required=True)
    start.add_argument("--vmid", type=int, required=True)
    start.set_defaults(func=_bind(lab, cmd_start))

    stop = guest_sub.add_parser(
        "stop", help="stop a lease-owned guest (graceful, then hard)"
    )
    stop.add_argument("--lease", required=True)
    stop.add_argument("--vmid", type=int, required=True)
    stop.add_argument("--timeout", type=int, default=DEFAULT_STOP_TIMEOUT,
                      help="seconds to wait for a graceful stop "
                           "(default: %(default)s; 0 = hard stop)")
    stop.set_defaults(func=_bind(lab, cmd_stop))

    destroy = guest_sub.add_parser(
        "destroy", help="destroy a lease-owned guest (irreversible)"
    )
    destroy.add_argument("--lease", required=True)
    destroy.add_argument("--vmid", type=int, required=True)
    destroy.add_argument("--confirm", action="store_true",
                         help="required: there is no interactive prompt")
    destroy.set_defaults(func=_bind(lab, cmd_destroy))

    probe = guest_sub.add_parser(
        "probe", help="how can this guest be reached? (read-only)"
    )
    probe.add_argument("--vmid", type=int, required=True)
    probe.set_defaults(func=_bind(lab, cmd_probe))

    listing = guest_sub.add_parser(
        "list", help="registered guests joined with live state (read-only)"
    )
    listing.add_argument("--lease", help="narrow to one lease")
    listing.set_defaults(func=_bind(lab, cmd_list))

    run = guest_sub.add_parser(
        "run", help="run a command in a lease-owned guest"
    )
    run.add_argument("--lease", required=True)
    run.add_argument("--vmid", type=int, required=True)
    run.add_argument("--timeout", type=int, default=DEFAULT_RUN_TIMEOUT,
                     help="seconds before the run is abandoned "
                          "(default: %(default)s)")
    run.add_argument("command", nargs=argparse.REMAINDER,
                     help="command and arguments (use -- before flag-like args)")
    run.set_defaults(func=_bind(lab, cmd_run))

    snap = guest_sub.add_parser(
        "snapshot", help="list, create, delete or roll back snapshots"
    )
    snap_sub = snap.add_subparsers(dest="action", required=True)
    snap_list = snap_sub.add_parser("list", help="snapshots on this guest")
    snap_list.add_argument("--lease", required=True)
    snap_list.add_argument("--vmid", type=int, required=True)
    snap_list.set_defaults(func=_bind(lab, cmd_snapshot))
    snap_create = snap_sub.add_parser("create", help="take a snapshot")
    snap_create.add_argument("--lease", required=True)
    snap_create.add_argument("--vmid", type=int, required=True)
    snap_create.add_argument("--name", required=True)
    snap_create.add_argument("--description")
    snap_create.set_defaults(func=_bind(lab, cmd_snapshot))
    snap_delete = snap_sub.add_parser("delete", help="delete a snapshot")
    snap_delete.add_argument("--lease", required=True)
    snap_delete.add_argument("--vmid", type=int, required=True)
    snap_delete.add_argument("--name", required=True)
    snap_delete.add_argument("--confirm", action="store_true",
                             help="required: there is no interactive prompt")
    snap_delete.set_defaults(func=_bind(lab, cmd_snapshot))
    snap_rollback = snap_sub.add_parser(
        "rollback",
        help="roll a stopped guest back to a snapshot that has no children",
    )
    snap_rollback.add_argument("--lease", required=True)
    snap_rollback.add_argument("--vmid", type=int, required=True)
    snap_rollback.add_argument("--name", required=True)
    snap_rollback.add_argument("--confirm", action="store_true",
                               help="required: there is no interactive prompt")
    snap_rollback.set_defaults(func=_bind(lab, cmd_snapshot))

    template = guest_sub.add_parser(
        "template",
        help="turn a stopped lease-owned guest into a template",
    )
    template.add_argument("--lease", required=True)
    template.add_argument("--vmid", type=int, required=True)
    template.add_argument("--confirm", action="store_true",
                          help="required: teardown will not destroy it afterwards")
    template.set_defaults(func=_bind(lab, cmd_template))
