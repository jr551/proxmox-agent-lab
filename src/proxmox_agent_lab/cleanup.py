"""Reclamation: guest teardown, orphan recovery and host power-off.

Orchestration over the proxmox seam (``qm``/``pct`` via ``ssh.py``) and the
SQLite store: `finalize_lease` tears down what a lease owned, `cleanup-expired`
sweeps every lease past its expiry, and the host is only ever powered off with
a verified-shutdown probe loop (`power.shutdown_verified`, contract §2) --
never assumed.

Teardown touches only resources a lease registered (``policy='disposable'``,
pxl-stamped at registration), never one another live lease still claims, and
is idempotent: an already-gone guest is success and a resource stamped
``destroyed_at`` is skipped. Failures land the lease in ``cleanup_failed`` with
``last_error`` so every later sweep retries them.

The ``api`` parameter surviving callers still pass is unused everywhere below:
the proxmox seam built from the configuration replaced the old HTTP client.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from pathlib import Path
from typing import Any

from . import guest as guest_module
from . import leases as leases_module
from . import power as power_module
from . import proxmox as proxmox_module
from . import store as store_module
from .errors import LabError

INFRA_TAG = "codex-lab-infra"

ORPHAN_ACTIVITY_WINDOW_SECONDS = 1800

STOP_TASK_TYPES = frozenset(
    {"qmstop", "qmshutdown", "qmreset", "vzstop", "vzshutdown"}
)

#: A guest below this CPU floor is not proven idle, only not proven busy.
BUSY_CPU_FRACTION = 0.10

#: Seconds a graceful shutdown may take before the hard stop.
STOP_TIMEOUT = 130

DEFAULT_TIMEOUT = 30.0


def _make_proxmox(config: Any) -> proxmox_module.Proxmox:
    """The proxmox seam for this configuration (tests substitute a double)."""
    return proxmox_module.from_config(config)


def _ssh_of(seam: Any) -> Any:
    """The ssh transport underneath a proxmox seam (host probes, pvesh GETs)."""
    return getattr(seam, "_ssh", seam)


def _lease_root(lab: Any) -> Path:
    return leases_module.lease_root_for(Path(lab.STATE_ROOT))


def _pvesh_get(seam: Any, path: str, params: tuple = ()) -> Any:
    """One read-only ``pvesh get`` as JSON (the API client is gone; §D)."""
    argv = ["pvesh", "get", path, "--output-format", "json"]
    for key, value in params:
        argv += [f"--{key}", str(value)]
    result = _ssh_of(seam).run(argv, timeout=DEFAULT_TIMEOUT)
    if not result.ok:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise proxmox_module.ProxmoxError(f"pvesh get {path} failed: {detail}")
    raw = result.stdout.decode("utf-8", "replace").strip()
    try:
        return json.loads(raw) if raw else None
    except ValueError as raised:
        raise proxmox_module.ProxmoxError(
            f"pvesh get {path}: unparseable JSON output"
        ) from raised


def _is_lab_infrastructure(resource: dict[str, Any]) -> bool:
    tags = str(resource.get("tags") or "").replace(",", ";")
    return INFRA_TAG in [tag.strip() for tag in tags.split(";")]


def _tag_list(resource: dict[str, Any]) -> list[str]:
    tags = str(resource.get("tags") or "").replace(",", ";")
    return [tag.strip() for tag in tags.split(";") if tag.strip()]


def node_guests(lab: Any, api: Any) -> list[dict[str, Any]]:
    """Every guest on the node, as Proxmox reports it (``api`` unused)."""
    seam = _make_proxmox(lab.CONFIG)
    records = _pvesh_get(seam, "/cluster/resources", (("type", "vm"),)) or []
    if isinstance(records, dict):
        records = [records]
    return [
        item for item in records if isinstance(item, dict) and "vmid" in item
    ]


def describe_guests(lab: Any, api: Any) -> list[dict[str, Any]]:
    """Every guest on the node, with what the controller can prove about it.

    Ownership is guest metadata now (rework plan §F): a guest carries
    ``pxl`` and ``lease-<id>`` tags, and the store is the record of what that
    lease id still owns. A pxl guest whose lease id no longer exists is an
    orphan -- and while one runs, the host can never power itself off.
    """
    known = leases_module.all_lease_ids(_lease_root(lab))
    described: list[dict[str, Any]] = []
    for record in node_guests(lab, api):
        tags = _tag_list(record)
        lease_tag = next(
            (tag[len("lease-"):] for tag in tags if tag.startswith("lease-")),
            None,
        )
        described.append({
            "kind": str(record.get("type") or "qemu"),
            "vmid": int(record["vmid"]),
            "name": record.get("name"),
            "status": record.get("status"),
            "tags": record.get("tags"),
            "pxl": (leases_module.OWNERSHIP_TAG in tags
                    or leases_module.LEGACY_OWNERSHIP_TAG in tags),
            "lease_tag": lease_tag,
            "known_lease": bool(lease_tag and lease_tag in known),
            "load": guest_load(record),
        })
    return described


def orphaned_guests(lab: Any, api: Any) -> list[dict[str, Any]]:
    """pxl-tagged guests this controller has no lease record for."""
    return [
        guest for guest in describe_guests(lab, api)
        if guest["pxl"] and not guest["known_lease"]
        and not _is_lab_infrastructure(guest)
    ]


def running_guest_vmids(lab: Any, api: Any) -> list[int]:
    """VMIDs currently running on the node, lease or no lease.

    A guest can exist outside any lease's tracked resources -- a persistent
    builder kept alive on purpose across sessions. The decision to power off
    the host must not rely on lease bookkeeping alone, or a guest like that
    gets the host pulled out from under it.
    """
    return sorted(
        int(item["vmid"]) for item in node_guests(lab, api)
        if item.get("status") == "running" and not _is_lab_infrastructure(item)
    )


def host_power_policy(lab: Any) -> dict[str, Any]:
    """Policy that would keep the host powered on regardless of leases.

    The old LXC-only VPS policy lived on ``[proxmox] guest_mode`` and died
    with the API stack: the new config schema (rework plan §G) reads exactly
    its own keys and has no such knob, so nothing keeps the host on by policy.
    """
    return {}


def _tcp_probe(target: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((target, port), timeout=timeout):
            return True
    except OSError:
        return False


def shutdown_host(lab: Any, api: Any) -> bool:
    """Shut the lab machine down and confirm it actually went off.

    Verified by repeated probe failure across ssh AND TCP (contract §2) --
    never assumed, and there is no force-off path any more: a host that
    refuses to die is reported, loudly.
    """
    config = lab.CONFIG
    seam = _make_proxmox(config)
    transport = _ssh_of(seam)
    node = config.pve.node
    if not transport.probe():
        lab.audit("lab-power-off-already-verified", node=node)
        return True
    try:
        running = running_guest_vmids(lab, None)
    except LabError as exc:
        # Cannot prove the node is empty -> do not pull power on a guess.
        lab.audit("lab-power-off-unverified", node=node,
                  reason=f"could not enumerate guests: {exc}")
        return False
    if running:
        lab.audit("lab-power-off-blocked-by-running-guest", node=node,
                  vmids=running)
        return False

    def request_fn() -> None:
        # The ssh call routinely dies mid-request when sshd goes down with
        # the host -- that is the shutdown working, so the probes decide.
        try:
            transport.run(["shutdown", "-h", "now"],
                          timeout=DEFAULT_TIMEOUT, host_change=True)
        except LabError as exc:
            lab.audit("lab-graceful-shutdown-request-failed", node=node,
                      error=str(exc))

    def probe_fn(kind: str) -> bool:
        if kind == "ssh":
            return transport.probe()
        return _tcp_probe(config.ssh.target, 22)

    lab.audit("lab-graceful-shutdown-requested", node=node)
    result = power_module.shutdown_verified(
        request_fn=request_fn, probe_fn=probe_fn,
    )
    lab.audit(
        "lab-power-off-verified" if result["host_powered_off"]
        else "lab-power-off-unverified",
        node=node,
        failures=result["failures"],
        elapsed=result["elapsed"],
    )
    return bool(result["host_powered_off"])


def guest_status(lab: Any, api: Any, kind: str, vmid: int) -> str:
    return _make_proxmox(lab.CONFIG).status(kind, vmid)


def stop_guest(lab: Any, api: Any, kind: str, vmid: int) -> None:
    """Graceful shutdown first, hard stop if it will not go; gone is success."""
    seam = _make_proxmox(lab.CONFIG)
    try:
        if seam.status(kind, vmid) == "stopped":
            return
    except LabError as exc:
        if _guest_is_gone(lab, exc):
            return
        raise
    if seam.shutdown(kind, vmid, timeout=STOP_TIMEOUT):
        return
    lab.audit("guest-graceful-shutdown-timeout", vmid=vmid, kind=kind)
    seam.stop(kind, vmid)


def _guest_is_gone(lab: Any, error: LabError) -> bool:
    message = str(error).lower()
    # A prior finalizer run (or manual removal) beat us to it: `qm`/`pct`
    # report the missing config, and the old HTTP layer said 404.
    return (
        "does not exist" in message
        or "no such" in message
        or "404" in message
    )


def _storage_io_error(lab: Any, error: LabError) -> bool:
    message = str(error).lower()
    return "input/output error" in message or "i/o error" in message


def _delete_guest(
    lab: Any, api: Any, kind: str, vmid: int, *, destroy_unreferenced_disks: bool
) -> None:
    """`qm destroy <vmid> --purge 1` / `pct destroy <vmid>` through the seam.

    ``destroy_unreferenced_disks`` is accepted for the legacy call shape: the
    old REST DELETE retry it gated has no ``qm``/``pct`` counterpart (the §D
    fallback chains are ``pct create``/``pct shutdown``/``qm guest exec``
    only), so the purge destroy is the single path.
    """
    _make_proxmox(lab.CONFIG).destroy(kind, vmid, purge=True)


def _forget_retained(lab: Any, kind: str, vmid: int) -> None:
    """Keep the record honest: a guest that is gone is not retained."""
    leases_module.forget_resource(_lease_root(lab), kind, int(vmid))


def _confirm_absent(lab: Any, kind: str, vmid: int) -> bool:
    """Ask the host whether the guest is really gone, instead of trusting text.

    ``_guest_is_gone`` matches on message text, and a destroy can fail for
    an unrelated reason whose stderr merely *contains* "No such file or
    directory" (a disk path it could not remove, say). Treating that as
    success stamps the resource destroyed and ends the lease, so no later
    sweep retries and the live guest is orphaned forever.

    So absence must be *positively claimed* by a fresh probe: a status that
    comes back is proof the guest lives, and a probe that fails is proof only
    when the host itself says the guest does not exist. Anything else is no
    proof, and the record is kept so a later sweep retries.
    """
    try:
        _make_proxmox(lab.CONFIG).status(kind, vmid)
    except proxmox_module.ProxmoxError as exc:
        return _guest_is_gone(lab, exc)
    except Exception:  # noqa: BLE001 - an unreadable status is not proof
        return False
    return False  # the host answered with a status: the guest is still there


def delete_guest(lab: Any, api: Any, kind: str, vmid: int) -> None:
    try:
        _delete_guest(lab, api, kind, vmid, destroy_unreferenced_disks=True)
    except LabError as exc:
        if not _guest_is_gone(lab, exc) or not _confirm_absent(lab, kind, vmid):
            raise
    _forget_retained(lab, kind, vmid)


def guest_load(record: dict[str, Any] | None) -> dict[str, Any]:
    """The activity numbers Proxmox already reports for a guest.

    Returned with every orphan, busy or not, so the reader can disagree with
    the threshold instead of having to trust it.

    ``disk_written_bytes`` is **advisory and reported only**. Proxmox's
    ``diskwrite`` has been observed reading 0 for an entire session on a
    qcow2 guest over directory-backed storage that was demonstrably writing,
    so it is not a signal anything here decides on -- in either direction.
    """
    if not isinstance(record, dict):
        return {}
    load: dict[str, Any] = {}
    try:
        load["cpu_percent"] = round(float(record.get("cpu") or 0.0) * 100, 3)
    except (TypeError, ValueError):
        pass
    for source, name in (("mem", "mem_bytes"), ("diskwrite", "disk_written_bytes"),
                         ("netin", "net_in_bytes")):
        try:
            load[name] = int(record.get(source) or 0)
        except (TypeError, ValueError):
            continue
    return load


def recent_guest_activity(lab: Any, api: Any, kind: str, vmid: int, *,
                          within: int = ORPHAN_ACTIVITY_WINDOW_SECONDS,
                          record: dict[str, Any] | None = None,
                          ) -> dict[str, Any] | None:
    """Evidence that something is still using this guest, or None.

    "Orphaned" means *this* controller has no record of the guest. It does
    not mean nobody is using it: a second controller drives guests through
    the same host and its lease records are not here. Three independent
    signals, because each covers the others' blind spots: measurable CPU
    shows work happening *inside* the guest; a short uptime shows it was
    started recently; and recent tasks show somebody driving it from outside.

    An unreadable task log counts as activity: leaving a guest running is a
    much smaller mistake than stopping somebody's work. The disk counter is
    deliberately **not** one of the signals (see `guest_load`) and must never
    be measured from here -- that costs monitor round trips that do not
    belong on the path deciding whether to leave somebody's guest alone.
    """
    load = guest_load(record)
    if load.get("cpu_percent", 0) >= BUSY_CPU_FRACTION * 100:
        return {"signal": "busy", "cpu_percent": load["cpu_percent"], **load}
    seam = _make_proxmox(lab.CONFIG)
    node = lab.CONFIG.pve.node
    try:
        current = _pvesh_get(
            seam, f"/nodes/{node}/{kind}/{vmid}/status/current"
        ) or {}
        uptime = int(current.get("uptime") or 0)
    except (LabError, TypeError, ValueError):
        uptime = 0
    if 0 < uptime < within:
        return {"signal": "started recently", "seconds_ago": uptime}
    try:
        tasks = _pvesh_get(
            seam, f"/nodes/{node}/tasks",
            (("vmid", int(vmid)), ("limit", 20)),
        ) or []
    except LabError as exc:
        return {"signal": "task log unreadable", "detail": str(exc)[:120]}
    now = time.time()
    for task in tasks:
        if not isinstance(task, dict):
            continue
        if str(task.get("type")) in STOP_TASK_TYPES:
            continue
        try:
            started = int(task.get("starttime") or 0)
        except (TypeError, ValueError):
            continue
        # A task timestamped in the future means the clocks disagree; treat
        # it as current rather than as ancient.
        if started and now - started < within:
            return {
                "signal": str(task.get("type")),
                "seconds_ago": max(0, int(now - started)),
                **load,
            }
    return None


def reclaim_orphans(lab: Any, api: Any, *,
                    include_active: bool = False) -> dict[str, Any]:
    """Stop -- never delete -- guests no lease record vouches for.

    Stopping is reversible and unblocks host power-off, which is the whole
    point: one abandoned guest otherwise keeps the machine on for ever.
    Deleting is not reversible, and the controller by definition cannot vouch
    for what is on a disk it has lost the record of, so that stays manual.

    A guest that shows recent activity is left alone unless `include_active`:
    "no record here" is not the same as "nobody is using it", and stopping
    somebody else's running work is the one outcome this command must not
    have by default.
    """
    result: dict[str, Any] = {
        "stopped": [], "failed": {}, "already_stopped": [], "left_active": {},
    }
    for guest in orphaned_guests(lab, api):
        kind, vmid = guest["kind"], int(guest["vmid"])
        if guest.get("status") != "running":
            result["already_stopped"].append(vmid)
            continue
        activity = (
            None if include_active
            else recent_guest_activity(lab, api, kind, vmid,
                                       record=guest.get("load"))
        )
        if activity:
            result["left_active"][str(vmid)] = activity
            lab.audit(
                "orphan-guest-left-running",
                kind=kind,
                vmid=vmid,
                lease_tag=guest.get("lease_tag"),
                signal=activity.get("signal"),
            )
            continue
        try:
            stop_guest(lab, api, kind, vmid)
            result["stopped"].append(vmid)
            lab.audit(
                "orphan-guest-stopped",
                kind=kind,
                vmid=vmid,
                lease_tag=guest.get("lease_tag"),
                reason="no lease record",
                **guest_load(guest.get("load")),
            )
        except LabError as exc:
            result["failed"][str(vmid)] = str(exc)[:300]
            lab.audit(
                "orphan-guest-stop-failed", kind=kind, vmid=vmid,
                error=str(exc)[:300],
            )
    return result



def _is_template_guest(lab: Any, kind: str, vmid: int) -> bool:
    """Does the host say this guest is a template? Unreadable config says no.

    Only a *positive* ``template: 1`` from the host counts. If the config
    cannot be read we have no proof, and skipping a destroy we could have
    performed is the safe direction: the record stays and a later sweep
    retries, rather than a template being destroyed on a guess.
    """
    try:
        cfg = guest_module._guest_config(_make_proxmox(lab.CONFIG), kind, vmid)
    except Exception:  # noqa: BLE001 - an unreadable config is not a template
        return False
    return cfg is not None and guest_module._is_template(cfg)

def finalize_lease(lab: Any, api: Any, lease: dict[str, Any]) -> list[str]:
    """Tear down what one lease owned. Returns the failure lines.

    Per resource, in registration order reversed: skip what is already
    destroyed (idempotent), leave what another live lease claims
    (``left_to_another_lease``), leave ``retain`` guests untouched, and
    otherwise run the teardown chain -- graceful shutdown, hard stop if it
    will not go, then destroy. An already-gone guest is success.

    On any failure the lease lands in ``cleanup_failed`` with ``last_error``
    so every later sweep retries; without failures it ends here.
    """
    lease_root = _lease_root(lab)
    lease_id = str(lease.get("id"))
    failures: list[str] = []
    transferred: list[str] = []
    now = time.time()
    for resource in reversed(lease.get("resources", [])):
        kind = str(resource.get("kind"))
        vmid = int(resource.get("vmid"))
        if resource.get("destroyed_at"):
            continue
        owner = leases_module.resource_owner_elsewhere(
            lease_root, lease_id, kind, vmid, now=now
        )
        if owner:
            # A newer, still-live lease claims this guest. Stopping or
            # deleting it here would destroy a machine that lease is using --
            # the one failure mode expiry cleanup must never have.
            transferred.append(f"{kind}/{vmid}")
            lab.audit(
                "lease-resource-owned-by-another-lease",
                lease=lease_id, kind=kind, vmid=vmid, owner=owner,
            )
            continue
        if resource.get("policy", "disposable") != "disposable":
            # Retained guests outlive the lease untouched; their metadata is
            # the GC's business, not this teardown's.
            continue
        if _is_template_guest(lab, kind, vmid):
            # `guest destroy` refuses a template because destroying one kills
            # the shared clone source; the sweep must not do what the
            # operator-facing command refuses. The registry row is a weaker
            # proof than the host config, so the config decides.
            transferred.append(f"{kind}/{vmid}")
            lab.audit(
                "lease-resource-is-a-template",
                lease=lease_id, kind=kind, vmid=vmid,
            )
            continue
        try:
            stop_guest(lab, api, kind, vmid)
            delete_guest(lab, api, kind, vmid)
            lab.audit(
                "lease-resource-finalized",
                lease=lease_id, kind=kind, vmid=vmid, policy="disposable",
            )
        except LabError as exc:
            # Only treat a teardown error as "the guest is gone" when the
            # host confirms it. The stderr text alone can describe a disk
            # that could not be removed while the guest itself is alive.
            if _guest_is_gone(lab, exc) and _confirm_absent(lab, kind, vmid):
                _forget_retained(lab, kind, vmid)
                continue
            failures.append(f"{kind}/{vmid}: {exc}")
            lab.audit(
                "lease-resource-finalize-failed",
                lease=lease_id, kind=kind, vmid=vmid, error=str(exc),
            )
    lease["state"] = "ended" if not failures else "cleanup_failed"
    lease["last_error"] = "; ".join(failures) if failures else ""
    lease["transferred_resources"] = transferred
    if not failures:
        lease["ended_at"] = store_module.utc_now()
    leases_module.save_lease(lease_root, lease)
    return failures


def shared_lease_resources(lab: Any, lease: dict[str, Any]) -> list[dict[str, Any]]:
    """Guests `lease` would destroy that another non-terminal lease registers.

    `finalize_lease` declines to touch a resource a still-*live* lease owns,
    and `lease-register` refuses to take one. Neither closes the window this
    looks at: both ask "is the other lease live?" and a lease can be `active`
    while expired -- one heartbeat away from live again. Reads lease records
    only, so it costs no host call. ``retain`` resources are excluded: this
    never destroys one.
    """
    lease_id = str(lease.get("id"))
    others = leases_module.active_leases(
        _lease_root(lab), excluding=lease_id
    )
    if not others:
        return []
    now = time.time()
    shared: list[dict[str, Any]] = []
    for resource in lease.get("resources", []):
        if resource.get("destroyed_at"):
            continue
        if resource.get("policy", "disposable") != "disposable":
            continue
        try:
            kind = str(resource["kind"])
            vmid = int(resource["vmid"])
        except (KeyError, TypeError, ValueError):
            continue
        for other in others:
            if not leases_module.lease_claims(other, kind, vmid):
                continue
            shared.append({
                "resource": f"{kind}/{vmid}",
                "kind": kind,
                "vmid": vmid,
                "lease": str(other.get("id")),
                "lease_kind": str(other.get("kind") or leases_module.ORDINARY),
                "lease_live": leases_module.lease_is_live(other, now),
            })
    return shared


def describe_shared_resources(shared: list[dict[str, Any]]) -> str:
    return ", ".join(
        f"{item['resource']} (lease {item['lease']}"
        + ("" if item["lease_live"] else ", expired but still active")
        + ")"
        for item in shared
    )


def _release_claim(lab: Any, lease_id: str, prior_state: str) -> None:
    leases_module.claim_lease(
        _lease_root(lab), lease_id, from_state="ending", to_state=prior_state
    )


def cmd_lease_end(lab: Any, args: argparse.Namespace) -> None:
    config = lab.CONFIG
    lease_root = _lease_root(lab)
    seam = _make_proxmox(config)
    lease = leases_module.load_lease(lease_root, args.lease, active=False)
    prior_state = str(lease.get("state"))
    if prior_state == "ending":
        # Another actor (a concurrent cleanup-expired sweep) claimed it first.
        print(json.dumps({
            "lease": args.lease,
            "state": "ending",
            "note": "another actor is already finalizing this lease; "
                    "nothing was done",
        }, indent=2, sort_keys=True))
        return
    if prior_state not in ("active", "cleanup_failed"):
        raise LabError(
            f"Lease {args.lease} cannot be finalized from state {prior_state}"
        )
    if leases_module.is_long_term(lease):
        raise LabError(
            f"Lease {args.lease} is long-term: its guests are meant to "
            "survive. Use 'proxmox-lab lease-destroy --lease "
            f"{args.lease} --confirm' to remove it and its machines "
            "for good."
        )
    if not leases_module.claim_lease(
        lease_root, args.lease, from_state=prior_state, to_state="ending"
    ):
        print(json.dumps({
            "lease": args.lease,
            "state": "ending",
            "note": "another actor is already finalizing this lease; "
                    "nothing was done",
        }, indent=2, sort_keys=True))
        return
    # Strictly before anything is stopped or deleted. A warning that arrives
    # next to an already-destroyed guest is worthless.
    shared = shared_lease_resources(lab, lease)
    if shared and not getattr(args, "shared_guests_authorized", False):
        _release_claim(lab, args.lease, prior_state)
        lab.audit(
            "lease-end-refused-shared-guest",
            lease=args.lease,
            shared_with_other_leases=shared,
        )
        raise LabError(
            f"Lease {args.lease} would destroy guest(s) that another "
            "active lease still registers: "
            + describe_shared_resources(shared)
            + ". Deleting one of those stops somebody else's work and is "
            "not recoverable. End or abandon the other lease first, or "
            "re-run with --shared-guests-authorized to destroy them "
            "anyway."
        )
    if shared:
        print(
            "warning: destroying guest(s) another active lease registers, "
            "because --shared-guests-authorized was given: "
            + describe_shared_resources(shared),
            file=sys.stderr,
        )
    failures = finalize_lease(lab, seam, lease)
    others = leases_module.active_leases(lease_root, excluding=args.lease)
    host_powered_off = False
    if not others:
        # After every lease is closed -- and only then -- the host may go.
        host_powered_off = shutdown_host(lab, seam)
    lab.audit(
        "lease-end",
        lease=args.lease,
        failures=failures,
        remaining_active_leases=[x["id"] for x in others],
        host_powered_off=host_powered_off,
        shared_with_other_leases=shared,
    )
    result: dict[str, Any] = {
        "lease": args.lease,
        "failures": failures,
        "remaining_active_leases": [x["id"] for x in others],
        "host_powered_off": host_powered_off,
    }
    if lease.get("transferred_resources"):
        result["left_to_another_lease"] = lease["transferred_resources"]
    if shared:
        result["shared_with_other_leases"] = shared
        result["warning"] = (
            "--shared-guests-authorized was given, so this lease-end acted on "
            "guest(s) another active lease also registers: "
            + describe_shared_resources(shared)
        )
    persistent = [x for x in others if leases_module.is_long_term(x)]
    if persistent:
        # Say this loudly. A machine left running is the surprise nobody
        # wants on their electricity bill.
        result["host_left_running"] = True
        result["reason"] = (
            f"{len(persistent)} long-term lease(s) keep this machine on: "
            + ", ".join(x["id"] for x in persistent)
        )
        result["to_power_off"] = "destroy them with 'lease-destroy', or "\
            "stop the host yourself"
    elif not others and not host_powered_off:
        try:
            running = running_guest_vmids(lab, None)
        except LabError:
            running = []
        result["host_left_running"] = True
        if running:
            result["reason"] = (
                "guest(s) still running outside any tracked lease: "
                + ", ".join(str(vmid) for vmid in running)
            )
            result["to_power_off"] = (
                "stop or register those guests, or stop the host yourself"
            )
        else:
            result["reason"] = "host power-off could not be verified"
            result["to_power_off"] = "check the host and stop it yourself"
    print(json.dumps(result, indent=2, sort_keys=True))
    created_at = lease.get("created_at")
    if created_at:
        try:
            lifetime = time.time() - leases_module.parse_expiry(
                str(created_at)
            ).timestamp()
        except ValueError:
            lifetime = None
        if lifetime is not None and lifetime < 300:
            print(
                f"hint: lease {args.lease} ended after {int(lifetime)}s. For "
                "a work session, prefer ONE lease kept alive with "
                "lease-heartbeat every <=20 min; each begin/end cycle costs "
                "a host boot and provisioning.",
                file=sys.stderr,
            )
    if failures or (not others and not host_powered_off):
        raise LabError("Lease cleanup or host power-off did not complete")


def cmd_lease_destroy(lab: Any, args: argparse.Namespace) -> None:
    """Tear down a long-term lease: lift its protection, delete, allow power-off."""
    config = lab.CONFIG
    lease_root = _lease_root(lab)
    seam = _make_proxmox(config)
    lease = leases_module.load_lease(lease_root, args.lease, active=False)
    if not leases_module.is_long_term(lease):
        raise LabError(
            f"{args.lease} is an ordinary lease; end it with 'lease-end'"
        )
    guests = [
        f"{r['kind']}/{r['vmid']} ({r.get('name') or 'unnamed'})"
        for r in lease.get("resources", [])
    ]
    if not getattr(args, "confirm", False):
        raise LabError(
            "This permanently destroys a long-term lease and everything in "
            f"it:\n  " + ("\n  ".join(guests) or "(no registered guests)")
            + "\nRe-run with --confirm if that is what you want."
        )
    prior_state = str(lease.get("state"))
    if prior_state not in ("active", "cleanup_failed"):
        raise LabError(
            f"Lease {args.lease} cannot be destroyed from state {prior_state}"
        )
    if not leases_module.claim_lease(
        lease_root, args.lease, from_state=prior_state, to_state="ending"
    ):
        raise LabError(
            f"Lease {args.lease} is already being finalized; nothing was done"
        )
    # Lift the long-term protection first: metadata pxl-expiry -> the past,
    # so a teardown that half-fails is reaped by the host-side GC instead of
    # pinning the machine on for ever. There is no PVE protect flag to lift.
    past = int(time.time()) - 1
    for resource in lease.get("resources", []):
        if resource.get("destroyed_at"):
            continue
        try:
            seam.set_metadata(
                str(resource["kind"]), int(resource["vmid"]),
                description=leases_module.metadata_description(
                    args.lease, past
                ),
            )
        except LabError as exc:
            lab.audit("long-term-unprotect-failed", lease=args.lease,
                      vmid=resource.get("vmid"), error=str(exc))
    failures = finalize_lease(lab, seam, lease)
    if not failures:
        lease["state"] = "destroyed"
        leases_module.save_lease(lease_root, lease)
    others = leases_module.active_leases(lease_root, excluding=args.lease)
    host_powered_off = False
    if not others:
        host_powered_off = shutdown_host(lab, seam)
    lab.audit("long-term-destroyed", lease=args.lease, failures=failures,
              host_powered_off=host_powered_off)
    print(json.dumps({
        "lease": args.lease,
        "destroyed_guests": guests,
        "failures": failures,
        "host_powered_off": host_powered_off,
        **host_power_policy(lab),
        "remaining_active_leases": [x["id"] for x in others],
    }, indent=2, sort_keys=True))
    if failures:
        raise LabError("some guests could not be destroyed")


def cmd_lease_abandon(lab: Any, args: argparse.Namespace) -> None:
    """Close a stopped lease without touching its guests or the host."""
    lease_root = _lease_root(lab)
    lease = leases_module.load_lease(lease_root, args.lease, active=False)
    prior_state = str(lease.get("state"))
    if prior_state not in ("active", "cleanup_failed"):
        raise LabError(
            f"Lease {args.lease} cannot be abandoned from state {prior_state}"
        )
    if leases_module.is_long_term(lease):
        raise LabError(
            f"Lease {args.lease} is long-term and cannot be abandoned. "
            "Use 'proxmox-lab lease-destroy --lease "
            f"{args.lease} --confirm' to remove it and its machines."
        )
    if not args.confirm:
        raise LabError(
            "lease-abandon leaves every registered guest and the host "
            "untouched. Re-run with --confirm after they are stopped."
        )
    seam = _make_proxmox(lab.CONFIG)
    stopped: list[str] = []
    missing: list[str] = []
    for resource in lease.get("resources", []):
        kind = str(resource["kind"])
        vmid = int(resource["vmid"])
        resource_id = f"{kind}/{vmid}"
        try:
            status = guest_status(lab, seam, kind, vmid)
        except LabError as exc:
            if _guest_is_gone(lab, exc):
                missing.append(resource_id)
                continue
            raise LabError(
                f"Cannot safely abandon lease {args.lease}: could not "
                f"verify {resource_id} is stopped: {exc}"
            ) from None
        if status != "stopped":
            raise LabError(
                f"Cannot safely abandon lease {args.lease}: {resource_id} "
                f"is {status}, not stopped"
            )
        stopped.append(resource_id)
    if not leases_module.claim_lease(
        lease_root, args.lease, from_state=prior_state, to_state="abandoned"
    ):
        raise LabError(
            f"Lease {args.lease} changed state concurrently; re-run"
        )
    lease["state"] = "abandoned"
    lease["ended_at"] = store_module.utc_now()
    lease["last_error"] = ""
    leases_module.save_lease(lease_root, lease)
    audit_error: str | None = None
    try:
        lab.audit(
            "lease-abandon",
            lease=args.lease,
            stopped=stopped,
            missing=missing,
            reason="registered guests verified stopped; no guest or host "
                   "mutation",
        )
    except (LabError, OSError, ValueError) as exc:
        audit_error = str(exc)
        print(
            "warning: lease was closed but its audit event could not be "
            f"recorded: {audit_error}",
            file=sys.stderr,
        )
    result: dict[str, Any] = {
        "lease": args.lease,
        "state": "abandoned",
        "guests_verified_stopped": stopped,
        "guests_already_missing": missing,
        "guest_mutation": False,
        "host_mutation": False,
        "audit_recorded": audit_error is None,
    }
    if audit_error is not None:
        result["audit_error"] = audit_error
    print(json.dumps(result, indent=2, sort_keys=True))


def cmd_reclaim_orphans_only(lab: Any, args: argparse.Namespace) -> dict[str, Any]:
    """Stop orphaned guests and do nothing else.

    "Stop the guests nothing owns" is a much smaller intention than a full
    expiry sweep -- wanting one is not consenting to the other -- so it gets
    its own path: no lease is finalized, and the host is left exactly as it
    was.
    """
    if not args.host_change_authorized:
        raise LabError(
            "--orphans-only stops guests this controller has no record of. "
            "Re-run with --host-change-authorized once the user has asked for "
            "that. 'guest inventory --orphaned-only' lists them first."
        )
    seam = _make_proxmox(lab.CONFIG)
    if not _ssh_of(seam).probe():
        raise LabError(
            "the host is not reachable, so there is nothing running to reclaim"
        )
    reclaimed = reclaim_orphans(
        lab, seam, include_active=getattr(args, "include_active", False)
    )
    if reclaimed["stopped"]:
        lab.audit(
            "orphans-reclaimed",
            stopped=reclaimed["stopped"],
            already_stopped=reclaimed["already_stopped"],
            failed=sorted(reclaimed["failed"]),
        )
    result: dict[str, Any] = {
        "reclaimed_orphans": reclaimed,
        "leases_swept": [],
        "host_powered_off": False,
        "note": (
            "Only orphaned guests were touched. No lease was finalized and the "
            "host was left on; a normal 'cleanup-expired' run makes those "
            "decisions."
        ),
    }
    if reclaimed["left_active"]:
        result["left_active_note"] = (
            "These are running and were touched recently, so something is "
            "using them even though this controller has no record of them -- "
            "another controller drives guests through the same host. Pass "
            "--include-active to stop them anyway."
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    if reclaimed["failed"]:
        raise LabError("One or more orphaned guests could not be stopped")
    return result


def cmd_cleanup_expired(lab: Any, args: argparse.Namespace) -> None:
    if getattr(args, "orphans_only", False):
        cmd_reclaim_orphans_only(lab, args)
        return
    config = lab.CONFIG
    lease_root = _lease_root(lab)
    state_root = Path(lab.STATE_ROOT)
    seam = _make_proxmox(config)
    cleaned: list[str] = []
    retried: list[str] = []
    failed: dict[str, list[str]] = {}
    transferred: dict[str, list[str]] = {}
    now_epoch = int(time.time())
    for lease in leases_module.cleanup_candidate_leases(lease_root):
        if leases_module.is_long_term(lease):
            continue                 # never expires (and pxl-expiry=0 agrees)
        if lease.get("state") == "cleanup_failed":
            # Already past its end and known incomplete: a retry is exactly
            # what it needs, whatever its expiry says.
            retried.append(lease["id"])
        elif not getattr(args, "all", False) and int(
            lease.get("expires_at") or 0
        ) > now_epoch:
            continue                 # 0 = never expires
        if not leases_module.claim_lease(
            lease_root, lease["id"],
            from_state=str(lease.get("state")), to_state="ending",
        ):
            continue                 # a concurrent finalizer won
        failures = finalize_lease(lab, seam, lease)
        if lease.get("transferred_resources"):
            transferred[lease["id"]] = lease["transferred_resources"]
        if failures:
            failed[lease["id"]] = failures
        else:
            cleaned.append(lease["id"])
    reclaimed: dict[str, Any] | None = None
    if getattr(args, "reclaim_orphans", False):
        if not getattr(args, "host_change_authorized", False):
            raise LabError(
                "--reclaim-orphans stops guests this controller has no "
                "record of. Re-run with --host-change-authorized once the "
                "user has asked for that. 'status' lists them first."
            )
        reclaimed = reclaim_orphans(
            lab, seam, include_active=getattr(args, "include_active", False)
        )
    remaining = leases_module.active_leases(lease_root)
    host_powered_off = False
    idle_shutdown_triggered = False
    idle_seconds = leases_module.mcp_idle_elapsed(state_root)
    threshold_seconds = int(config.lease.idle_shutdown_seconds)
    if not remaining and (cleaned or getattr(args, "all", False)):
        host_powered_off = shutdown_host(lab, seam)
    elif leases_module.mcp_idle_shutdown_due(
        state_root, idle_shutdown_seconds=threshold_seconds
    ):
        idle_shutdown_triggered = True
        lab.audit(
            "mcp-idle-shutdown-triggered",
            idle_seconds=idle_seconds,
            threshold_seconds=threshold_seconds,
        )
        host_powered_off = shutdown_host(lab, seam)
    # The watchdog runs every five minutes. Recording a no-op sweep would
    # bury real events under thousands of identical entries, so stay silent
    # unless the sweep actually did or failed something.
    if (cleaned or failed or idle_shutdown_triggered or host_powered_off
            or (reclaimed and reclaimed["stopped"])):
        lab.audit(
            "cleanup-expired",
            cleaned=cleaned,
            retried=retried,
            failed=failed,
            transferred=transferred,
            remaining=[x["id"] for x in remaining],
            idle_seconds=idle_seconds,
            idle_shutdown_triggered=idle_shutdown_triggered,
            host_powered_off=host_powered_off,
        )
    print(
        json.dumps(
            {
                "cleaned": cleaned,
                "retried": retried,
                "failed": failed,
                "left_to_another_lease": transferred,
                "remaining": [x["id"] for x in remaining],
                "mcp_idle_seconds": idle_seconds,
                "mcp_idle_shutdown_after_seconds": threshold_seconds,
                "idle_shutdown_triggered": idle_shutdown_triggered,
                "host_powered_off": host_powered_off,
                **host_power_policy(lab),
                **({"reclaimed_orphans": reclaimed} if reclaimed else {}),
            },
            indent=2,
            sort_keys=True,
        )
    )
    if failed:
        raise LabError("One or more expired leases could not be cleaned")
    if reclaimed and reclaimed["failed"]:
        raise LabError("One or more orphaned guests could not be stopped")
