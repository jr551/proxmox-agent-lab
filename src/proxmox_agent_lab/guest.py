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
        f"templates and retained registry guests are clone sources and "
        f"read-only surface, never driven or destroyed by a lease. Destroy "
        f"the clones instead, or drop the vouching first."
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

def cmd_create(lab: Any, args: Any) -> dict:
    """Create a lease-owned guest: clone the template, or build fresh.

    The clone path restamps the metadata after the clone (a clone copies the
    source's expiry, which is never ours to trust). The fresh path passes
    tags+description in the same ``qm create``/``pct create`` call. Either way
    the resource is registered BEFORE any start, so a half-built guest is
    still visible to cleanup.
    """
    lease_id, vmid, kind = str(args.lease), int(args.vmid), str(args.kind)
    lease = _lease_for_mutation(lab, lease_id)
    expires_at = int(lease["expires_at"])
    name = args.name or f"pxl-{vmid}"
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
            scsi0 = None
            if args.storage:
                disk_gb = int(args.disk_gb or 32)
                scsi0 = f"{args.storage}:{disk_gb}"
            prox.qemu_create(
                vmid,
                name=name,
                tags=tags,
                description=description,
                scsi0=scsi0,
                memory=args.memory,
                cores=args.cores,
            )
    else:
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
        # exactly while the container runs. There is no agent network view
        # for lxc through the seam.
        agent_ok = running
        ip = None
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
                             "(default: 8 LXC, 32 QEMU)")
    create.add_argument("--kind", choices=("qemu", "lxc"), default="qemu")
    create.add_argument(
        "--template",
        help="source template vmid; empty (or --fresh) builds from scratch "
             "(default: [pve] template_vmid)",
    )
    create.add_argument("--fresh", action="store_true",
                        help="build from scratch instead of cloning a template")
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
