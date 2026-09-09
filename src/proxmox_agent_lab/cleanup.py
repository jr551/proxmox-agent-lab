"""Reclamation: guest teardown, orphan recovery and host power-off.

Orchestration functions take `lab`, the CLI facade: they reach lease
data and state services through it rather than importing the
orchestration side of `leases`, which keeps the dependency direction
one way -- handlers toward transports and state services.
"""

from __future__ import annotations

from . import inventory as inventory_module
from . import power as power_module
from .api import ProxmoxAPI
from .errors import LabError
from .state import iso_now, json_dump, utc_now
from typing import Any
import argparse
import contextlib
import io
import json
import sys
import time

INFRA_TAG = "codex-lab-infra"


ORPHAN_ACTIVITY_WINDOW_SECONDS = 1800


STOP_TASK_TYPES = frozenset(
    {"qmstop", "qmshutdown", "qmreset", "vzstop", "vzshutdown"}
)


BUSY_CPU_FRACTION = 0.10


def _is_lab_infrastructure(resource: dict[str, Any]) -> bool:
    tags = str(resource.get("tags") or "").replace(",", ";")
    return INFRA_TAG in [tag.strip() for tag in tags.split(";")]


def node_guests(lab: Any, api: ProxmoxAPI) -> list[dict[str, Any]]:
    return [
        item for item in (api.call("GET", "/cluster/resources", {"type": "vm"}) or [])
        if isinstance(item, dict) and "vmid" in item
    ]


def describe_guests(lab: Any, api: ProxmoxAPI) -> list[dict[str, Any]]:
    """Every guest on the node, with what the controller can prove about it."""
    return inventory_module.classify(
        lab.node_guests(api),
        known_leases=lab.all_lease_ids(),
        retained=inventory_module.entries(lab.STATE_ROOT),
    )


def orphaned_guests(lab: Any, api: ProxmoxAPI) -> list[dict[str, Any]]:
    """Guests this tool created that no lease record or registry vouches for.

    Cleanup only ever finalizes resources listed in a lease, so a guest whose
    lease record is gone is invisible to it for ever -- and while such a guest
    runs, `shutdown_host()` refuses to power the machine off (by design). One
    of these can therefore keep the lab on indefinitely.
    """
    return inventory_module.orphans(lab.describe_guests(api))


def running_guest_vmids(lab: Any, api: ProxmoxAPI) -> list[int]:
    """VMIDs currently running on the node, lease or no lease.

    A guest can exist outside any lease's tracked resources -- a persistent
    builder kept alive on purpose across sessions (see 'guest template' /
    'guest clone'), or one a caller drove directly by VMID. The decision to
    power off the host must not rely on lease bookkeeping alone, or a guest
    like that gets the host pulled out from under it.
    """
    resources = api.call("GET", "/cluster/resources", {"type": "vm"}) or []
    return sorted(
        int(item["vmid"]) for item in resources
        if isinstance(item, dict) and item.get("status") == "running"
        and not lab._is_lab_infrastructure(item)
    )


def host_power_policy(lab: Any) -> dict[str, Any]:
    from .host_policy import lxc_only
    if lxc_only(lab.CONFIG):
        return {"host_left_running": True, "reason": "LXC-only VPS stays powered on by policy"}
    return {}


def shutdown_host(lab: Any, api: ProxmoxAPI) -> bool:
    """Shut the lab machine down and confirm it actually went off."""
    from .host_policy import lxc_only
    if lxc_only(lab.CONFIG):
        lab.audit("lab-power-off-disabled", reason="LXC-only VPS stays powered on")
        return False
    if not api.reachable():
        lab.audit("lab-power-off-already-verified", host=lab.HOST, node=lab.NODE)
        return True
    running = lab.running_guest_vmids(api)
    if running:
        lab.audit("lab-power-off-blocked-by-running-guest", host=lab.HOST, node=lab.NODE,
              vmids=running)
        return False
    try:
        task = api.call("POST", f"/nodes/{lab.NODE}/status", {"command": "shutdown"})
        lab.audit("lab-graceful-shutdown-requested", node=lab.NODE, task_id=task)
    except lab.LabError as exc:
        lab.audit("lab-graceful-shutdown-request-failed", error=str(exc))
    deadline = time.monotonic() + 240
    down_count = 0
    while time.monotonic() < deadline:
        if api.reachable():
            down_count = 0
        else:
            # Two consecutive failures, so a momentary blip is not mistaken
            # for a machine that has finished powering down.
            down_count += 1
            if down_count >= 2:
                lab.audit("lab-power-off-verified", host=lab.HOST, node=lab.NODE)
                return True
        time.sleep(5)

    # Graceful shutdown did not finish. Force-off is a last resort and is only
    # available for power modes that can actually cut power.
    if not power_module.can_force_off(lab.CONFIG):
        lab.audit("lab-power-off-unverified", host=lab.HOST, node=lab.NODE,
              reason="graceful shutdown timed out and no force-off is configured")
        return False
    try:
        detail = power_module.force_off(lab.CONFIG)
        lab.audit("lab-emergency-force-off-requested", **detail)
    except (power_module.PowerError, lab.ConfigError) as exc:
        lab.audit("lab-power-off-unverified", host=lab.HOST, node=lab.NODE, error=str(exc))
        return False
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if not api.reachable():
            time.sleep(5)
            if not api.reachable():
                lab.audit("lab-emergency-force-off-verified", host=lab.HOST, node=lab.NODE)
                return True
        time.sleep(5)
    lab.audit("lab-power-off-unverified", host=lab.HOST, node=lab.NODE)
    return False


def guest_status(lab: Any, api: ProxmoxAPI, kind: str, vmid: int) -> str:
    status = api.call("GET", f"/nodes/{lab.NODE}/{kind}/{vmid}/status/current")
    return status.get("status", "unknown")


def stop_guest(lab: Any, api: ProxmoxAPI, kind: str, vmid: int) -> None:
    try:
        if lab.guest_status(api, kind, vmid) == "stopped":
            return
    except lab.LabError as exc:
        if "HTTP 500" in str(exc) or "HTTP 404" in str(exc):
            return
        raise
    upid = api.call("POST", f"/nodes/{lab.NODE}/{kind}/{vmid}/status/shutdown")
    try:
        lab.wait_task(api, upid, timeout=130)
    except lab.LabError:
        lab.audit(
            "guest-graceful-shutdown-timeout",
            vmid=vmid,
            kind=kind,
            task_id=upid,
        )
        hard_upid = api.call("POST", f"/nodes/{lab.NODE}/{kind}/{vmid}/status/stop")
        lab.wait_task(api, hard_upid, timeout=60)


def _guest_is_gone(lab: Any, error: LabError) -> bool:
    message = str(error)
    # A prior finalizer run (or manual removal) beat us to it. Proxmox reports
    # this as a 404, or as a 500 whose body says the config file is absent.
    return "HTTP 404" in message or (
        "HTTP 500" in message and "does not exist" in message
    )


def _storage_io_error(lab: Any, error: LabError) -> bool:
    message = str(error).lower()
    return "input/output error" in message or "i/o error" in message


def _delete_guest(lab: Any, 
    api: ProxmoxAPI, kind: str, vmid: int, *, destroy_unreferenced_disks: bool
) -> None:
    data: dict[str, int] = {"purge": 1}
    if destroy_unreferenced_disks:
        data["destroy-unreferenced-disks"] = 1
    upid = api.call("DELETE", f"/nodes/{lab.NODE}/{kind}/{vmid}", data)
    lab.wait_task(api, upid, timeout=180)


def _forget_retained(lab: Any, kind: str, vmid: int) -> None:
    """Keep the registry honest: a guest that is gone is not retained."""
    try:
        inventory_module.forget(lab.STATE_ROOT, kind, vmid)
    except OSError:
        pass


def delete_guest(lab: Any, api: ProxmoxAPI, kind: str, vmid: int) -> None:
    try:
        lab._delete_guest(api, kind, vmid, destroy_unreferenced_disks=True)
        lab._forget_retained(kind, vmid)
    except lab.LabError as exc:
        if lab._guest_is_gone(exc):
            lab._forget_retained(kind, vmid)
            return
        if not lab._storage_io_error(exc):
            raise
        try:
            # Destroying unreferenced disks makes Proxmox inspect every
            # configured storage. An unrelated failed device must not strand
            # a lease whose guest can otherwise be deleted.
            lab._delete_guest(api, kind, vmid, destroy_unreferenced_disks=False)
            lab._forget_retained(kind, vmid)
        except lab.LabError as retry_exc:
            if lab._guest_is_gone(retry_exc):
                lab._forget_retained(kind, vmid)
                return
            raise lab.LabError(
                f"Could not delete {kind}/{vmid} after retrying without "
                f"unreferenced-disk cleanup: {retry_exc}; initial storage "
                f"error: {exc}"
            ) from retry_exc


def guest_load(record: dict[str, Any] | None) -> dict[str, Any]:
    """The activity numbers Proxmox already reports for a guest.

    Returned with every orphan, busy or not, so the reader can disagree with
    the threshold instead of having to trust it.

    `disk_written_bytes` is **advisory and reported only**. Proxmox's
    `diskwrite` has been observed reading 0 for an entire session on a qcow2
    guest over directory-backed storage that was demonstrably writing, so it
    is not a signal anything here decides on -- in either direction. It is
    also cumulative, so a non-zero value says the guest wrote at some point
    since boot, not that it is writing now. For an answer that can be relied
    on, measure the change over an interval with 'guest disk-activity
    --ground-truth', which cross-checks it against QEMU's own block counters
    and the allocated size of the image file on the host.
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


def recent_guest_activity(lab: Any, 
    api: ProxmoxAPI, kind: str, vmid: int, *,
    within: int = ORPHAN_ACTIVITY_WINDOW_SECONDS,
    record: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Evidence that something is still using this guest, or None.

    "Orphaned" means *this* controller has no record of the guest. It does not
    mean nobody is using it: a second controller, or one whose state lives
    elsewhere, drives guests through the same API token and its lease records
    are not here. Reclamation stopped a live ReactOS benchmark that way -- the
    other session had been taking a console screenshot every 45 seconds, and
    restarted the guest 90 seconds later.

    Three independent signals, because each covers the others' blind spots:
    recent tasks (console, start, agent) show someone driving it from outside;
    a short uptime shows it was started recently even if the task log has
    rolled; and measurable CPU shows work happening *inside* it, which the
    other two cannot see at all -- a three-hour build in an unmanaged container
    generates no Proxmox task and does not reset the uptime. An unreadable task
    log counts as activity: leaving a guest running is a much smaller mistake
    than stopping somebody's work.

    A guest below the CPU floor is not proven idle, only not proven busy, so
    its measured load is reported either way.

    The disk counter is deliberately **not** one of the signals. `diskwrite`
    can read 0 on a guest that is writing hard (qcow2 over directory-backed
    storage is the known case), so reading a zero as "idle" would stop live
    work; and it is cumulative, so reading a non-zero as "busy" would keep a
    long-abandoned guest running for ever on one write it did at boot. It
    travels in `load` for the reader's benefit only, and no branch below
    consults it. Anything that wants a real answer has to measure the delta,
    which is what 'guest disk-activity' is for -- and that must never be
    called from here: it costs a monitor round trip and, for the host-side
    signal, the opt-in SSH boundary, neither of which belongs on the path
    that decides whether to leave somebody's guest alone.
    """
    load = lab.guest_load(record)
    if load.get("cpu_percent", 0) >= BUSY_CPU_FRACTION * 100:
        return {"signal": "busy", "cpu_percent": load["cpu_percent"], **load}
    try:
        current = api.call(
            "GET", f"/nodes/{lab.NODE}/{kind}/{vmid}/status/current"
        ) or {}
        uptime = int(current.get("uptime") or 0)
    except (lab.LabError, TypeError, ValueError):
        uptime = 0
    if 0 < uptime < within:
        return {"signal": "started recently", "seconds_ago": uptime}
    try:
        tasks = api.call(
            "GET", f"/nodes/{lab.NODE}/tasks", {"vmid": int(vmid), "limit": 20}
        ) or []
    except lab.LabError as exc:
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
        # A task timestamped in the future means the clocks disagree; treat it
        # as current rather than as ancient.
        if started and now - started < within:
            return {
                "signal": str(task.get("type")),
                "seconds_ago": max(0, int(now - started)),
                **load,
            }
    return None


def reclaim_orphans(lab: Any, 
    api: ProxmoxAPI, *, include_active: bool = False
) -> dict[str, Any]:
    """Stop -- never delete -- guests no lease record or registry vouches for.

    Stopping is reversible and unblocks host power-off, which is the whole
    point: one abandoned guest otherwise keeps the machine on for ever.
    Deleting is not reversible, and the controller by definition cannot vouch
    for what is on a disk it has lost the record of, so that stays manual.

    A guest that shows recent activity is left alone unless `include_active`:
    "no record here" is not the same as "nobody is using it", and stopping
    somebody else's running work is the one outcome this command must not have
    by default.
    """
    result: dict[str, Any] = {
        "stopped": [], "failed": {}, "already_stopped": [], "left_active": {},
    }
    for guest in lab.orphaned_guests(api):
        kind, vmid = guest["kind"], int(guest["vmid"])
        if guest.get("status") != "running":
            result["already_stopped"].append(vmid)
            continue
        activity = (
            None if include_active
            else lab.recent_guest_activity(api, kind, vmid, record=guest.get("load"))
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
            lab.stop_guest(api, kind, vmid)
            result["stopped"].append(vmid)
            lab.audit(
                "orphan-guest-stopped",
                kind=kind,
                vmid=vmid,
                lease_tag=guest.get("lease_tag"),
                reason="no lease record or retained registry entry",
                **lab.guest_load(guest.get("load")),
            )
        except lab.LabError as exc:
            result["failed"][str(vmid)] = str(exc)[:300]
            lab.audit(
                "orphan-guest-stop-failed", kind=kind, vmid=vmid,
                error=str(exc)[:300],
            )
    return result


def finalize_lease(lab: Any, api: ProxmoxAPI, lease: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    transferred: list[str] = []
    now = lab.utc_now()
    for resource in reversed(lease.get("resources", [])):
        kind = resource["kind"]
        vmid = int(resource["vmid"])
        owner = lab.resource_owner_elsewhere(lease["id"], kind, vmid, now=now)
        if owner:
            # A newer, still-live lease claims this guest. Stopping or
            # deleting it here would destroy a machine that lease is using --
            # the one failure mode expiry cleanup must never have.
            transferred.append(f"{kind}/{vmid}")
            lab.audit(
                "lease-resource-owned-by-another-lease",
                lease=lease["id"],
                kind=kind,
                vmid=vmid,
                owner=owner,
            )
            continue
        try:
            # The unattended answer ISO holds the Administrator password in
            # plain text. An install abandoned before `windows finish` would
            # otherwise leave it on shared storage for good (audit 2026-08-24).
            if kind == "qemu" and resource.get("policy", "delete") == "delete":
                try:
                    from . import windows as windows_module
                    windows_module._shred_answer_iso(lab._module(), api, vmid)
                except Exception:  # noqa: BLE001 - best-effort, same as finish
                    pass
            lab.stop_guest(api, kind, vmid)
            if resource.get("policy", "delete") == "delete":
                lab.delete_guest(api, kind, vmid)
            lab.audit(
                "lease-resource-finalized",
                lease=lease["id"],
                kind=kind,
                vmid=vmid,
                policy=resource.get("policy", "delete"),
            )
        except lab.LabError as exc:
            failures.append(f"{kind}/{vmid}: {exc}")
            lab.audit(
                "lease-resource-finalize-failed",
                lease=lease["id"],
                kind=kind,
                vmid=vmid,
                error=str(exc),
            )
    lease["state"] = "closed" if not failures else "cleanup_failed"
    lease["closed_at"] = lab.iso_now()
    lease["failures"] = failures
    lease["transferred_resources"] = transferred
    lab.save_lease(lease)
    return failures


def shared_lease_resources(lab: Any, lease: dict[str, Any]) -> list[dict[str, Any]]:
    """Guests `lease` would delete that another active lease also registers.

    `finalize_lease` already declines to touch a resource a still-*live* lease
    owns, and `lease-register` refuses to take one. Neither closes the window
    this looks at, because both ask "is the other lease live?" and a lease can
    be `active` while expired -- one heartbeat away from live again. It also
    only takes one registration path that skips `lease-register` (a module
    that calls `register_resource` directly, such as an idempotent
    `memflow ghidra-setup --lxc N` re-run under a second lease) for two live
    leases to name one guest.

    Reads lease records only, so it costs no network call inside the
    controller lock. `retain` resources are excluded: this never deletes one,
    and `finalize_lease` keeps deciding what to do with them.
    """
    others = lab.active_leases(excluding=str(lease.get("id")))
    if not others:
        return []
    now = lab.utc_now()
    shared: list[dict[str, Any]] = []
    for resource in lease.get("resources", []):
        if resource.get("policy", "delete") != "delete":
            continue
        try:
            kind = str(resource["kind"])
            vmid = int(resource["vmid"])
        except (KeyError, TypeError, ValueError):
            continue
        for other in others:
            if not lab.lease_claims(other, kind, vmid):
                continue
            shared.append({
                "resource": f"{kind}/{vmid}",
                "kind": kind,
                "vmid": vmid,
                "lease": str(other.get("id")),
                "lease_kind": str(other.get("kind") or "session"),
                "lease_live": lab.lease_is_live(other, now),
            })
    return shared


def describe_shared_resources(shared: list[dict[str, Any]]) -> str:
    return ", ".join(
        f"{item['resource']} (lease {item['lease']}"
        + ("" if item["lease_live"] else ", expired but still active")
        + ")"
        for item in shared
    )


def cmd_lease_end(lab: Any, args: argparse.Namespace) -> None:
    api = lab.ProxmoxAPI()
    with lab.controller_lock():
        lease = lab.load_lease(args.lease, active=False)
        if lease.get("state") not in ("active", "cleanup_failed"):
            raise lab.LabError(
                f"Lease {args.lease} cannot be finalized from state "
                f"{lease.get('state')}"
            )
        if lab.is_long_term(lease):
            raise lab.LabError(
                f"Lease {args.lease} is long-term: its guests are meant to "
                "survive. Use 'proxmox-lab lease-destroy --lease "
                f"{args.lease} --confirm' to remove it and its machines "
                "for good."
            )
        # Strictly before anything is powered on, stopped or deleted. A
        # warning that arrives next to an already-destroyed guest is worthless.
        shared = lab.shared_lease_resources(lease)
        if shared and not getattr(args, "shared_guests_authorized", False):
            lab.audit(
                "lease-end-refused-shared-guest",
                lease=args.lease,
                shared_with_other_leases=shared,
            )
            raise lab.LabError(
                f"Lease {args.lease} would destroy guest(s) that another "
                "active lease still registers: "
                + lab.describe_shared_resources(shared)
                + ". Deleting one of those stops somebody else's work and is "
                "not recoverable. End or abandon the other lease first, or "
                "re-run with --shared-guests-authorized to destroy them "
                "anyway."
            )
        if shared:
            print(
                "warning: destroying guest(s) another active lease registers, "
                "because --shared-guests-authorized was given: "
                + lab.describe_shared_resources(shared),
                file=sys.stderr,
            )
        if not api.reachable() and lab.lease_requires_cleanup(lease):
            lab.ensure_on(api)
        if not api.reachable():
            lease["state"] = "closed"
            lease["closed_at"] = lab.iso_now()
            lease["failures"] = []
            lab.save_lease(lease)
            failures: list[str] = []
        else:
            failures = lab.finalize_lease(api, lease)
        others = lab.active_leases(excluding=args.lease)
        persistent = [x for x in others if lab.is_long_term(x)]
        host_powered_off = False
        if not others:
            host_powered_off = lab.shutdown_host(api)
        lab.audit(
            "lease-end",
            lease=args.lease,
            failures=failures,
            remaining_active_leases=[x["id"] for x in others],
            long_term_leases=[x["id"] for x in persistent],
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
            + lab.describe_shared_resources(shared)
        )
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
    elif lab.CONFIG.proxmox.get("guest_mode", "all") == "lxc-only":
        result["host_left_running"] = True
        result["reason"] = "LXC-only VPS stays powered on by policy"
    elif not others and not host_powered_off:
        running = lab.running_guest_vmids(api) if api.reachable() else []
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
    created_at = lease.get("created_at") or lease.get("created")
    if created_at:
        lifetime = (lab.utc_now() - lab.parse_expiry(created_at)).total_seconds()
        if lifetime < 300:
            print(
                f"hint: lease {args.lease} ended after {int(lifetime)}s. For "
                "a work session, prefer ONE lease kept alive with "
                "lease-heartbeat every <=20 min; each begin/end cycle costs "
                "a host boot and provisioning.",
                file=sys.stderr,
            )
    if failures or (not others and not host_powered_off):
        raise lab.LabError("Lease cleanup or host power-off did not complete")


def cmd_lease_abandon(lab: Any, args: argparse.Namespace) -> None:
    """Close a stopped ordinary lease without touching its guests or host."""
    with lab.controller_lock():
        lease = lab.load_lease(args.lease, active=False)
        if lease.get("state") not in ("active", "cleanup_failed"):
            raise lab.LabError(
                f"Lease {args.lease} cannot be abandoned from state "
                f"{lease.get('state')}"
            )
        if lab.is_long_term(lease):
            raise lab.LabError(
                f"Lease {args.lease} is long-term and cannot be abandoned. "
                "Use 'proxmox-lab lease-release --lease "
                f"{args.lease} --confirm' or 'proxmox-lab lease-destroy "
                f"--lease {args.lease} --confirm'."
            )
        if not args.confirm:
            raise lab.LabError(
                "lease-abandon leaves every registered guest and the host "
                "untouched. Re-run with --confirm after they are stopped."
            )
        api = lab.ProxmoxAPI()
        if not api.reachable():
            raise lab.LabError(
                "Cannot safely abandon this lease while Proxmox is "
                "unreachable; registered guests cannot be verified stopped."
            )
        stopped: list[str] = []
        missing: list[str] = []
        for resource in lease.get("resources", []):
            kind = resource["kind"]
            vmid = int(resource["vmid"])
            resource_id = f"{kind}/{vmid}"
            try:
                status = lab.guest_status(api, kind, vmid)
            except lab.LabError as exc:
                if "HTTP 404" in str(exc):
                    missing.append(resource_id)
                    continue
                raise lab.LabError(
                    f"Cannot safely abandon lease {args.lease}: could not "
                    f"verify {resource_id} is stopped: {exc}"
                ) from None
            if status != "stopped":
                raise lab.LabError(
                    f"Cannot safely abandon lease {args.lease}: {resource_id} "
                    f"is {status}, not stopped"
                )
            stopped.append(resource_id)
        lease["state"] = "closed"
        lease["closed_at"] = lab.iso_now()
        lease["abandoned_at"] = lease["closed_at"]
        lease["abandoned_reason"] = (
            "registered guests verified stopped; no guest or host mutation"
        )
        lab.save_lease(lease)
        audit_error: str | None = None
        try:
            lab.audit(
                "lease-abandon",
                lease=args.lease,
                stopped=stopped,
                missing=missing,
                reason=lease["abandoned_reason"],
            )
        except (lab.LabError, OSError, ValueError) as exc:
            audit_error = str(exc)
            print(
                "warning: lease was closed but its audit event could not be "
                f"recorded: {audit_error}",
                file=sys.stderr,
            )
    result: dict[str, Any] = {
        "lease": args.lease,
        "state": "closed",
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

    Reclamation was only reachable as part of a full expiry sweep, which in the
    same run finalizes every expired lease -- deleting their guests -- and then
    decides whether to power the host off. "Stop the guests nothing owns" is a
    much smaller intention than that, and wanting one is not consenting to the
    other, so it gets its own path: no lease is finalized, no backup runs, and
    the host is left exactly as it was.
    """
    if not args.host_change_authorized:
        raise lab.LabError(
            "--orphans-only stops guests this controller has no record of. "
            "Re-run with --host-change-authorized once the user has asked for "
            "that. 'guest inventory --orphaned-only' lists them first."
        )
    api = lab.ProxmoxAPI()
    if not api.reachable():
        raise lab.LabError(
            "the host is not reachable, so there is nothing running to reclaim"
        )
    with lab.controller_lock():
        reclaimed = lab.reclaim_orphans(
            api, include_active=getattr(args, "include_active", False)
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
            "another controller drives guests through the same token. Pass "
            "--include-active to stop them anyway."
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    if reclaimed["failed"]:
        raise lab.LabError("One or more orphaned guests could not be stopped")
    return result


def cmd_cleanup_expired(lab: Any, args: argparse.Namespace) -> None:
    if getattr(args, "orphans_only", False):
        lab.cmd_reclaim_orphans_only(args)
        return
    api = lab.ProxmoxAPI()
    cleaned: list[str] = []
    retried: list[str] = []
    failed: dict[str, list[str]] = {}
    transferred: dict[str, list[str]] = {}
    with lab.controller_lock():
        now = lab.utc_now()
        for lease in lab.cleanup_candidate_leases():
            if lab.is_long_term(lease):
                continue          # never expires, never swept
            if lease.get("state") == "cleanup_failed":
                # Already past its end and known incomplete: a retry is
                # exactly what it needs, whatever its expiry says.
                retried.append(lease["id"])
            elif not args.all and lab.parse_expiry(lease["expires_at"]) > now:
                continue
            if not api.reachable() and lab.lease_requires_cleanup(lease):
                lab.ensure_on(api)
            if api.reachable():
                failures = lab.finalize_lease(api, lease)
            else:
                failures = []
                lease["state"] = "closed"
                lease["closed_at"] = lab.iso_now()
                lab.save_lease(lease)
            if lease.get("transferred_resources"):
                transferred[lease["id"]] = lease["transferred_resources"]
            if failures:
                failed[lease["id"]] = failures
            else:
                cleaned.append(lease["id"])
        reclaimed: dict[str, Any] | None = None
        if getattr(args, "reclaim_orphans", False) and api.reachable():
            if not args.host_change_authorized:
                raise lab.LabError(
                    "--reclaim-orphans stops guests this controller has no "
                    "record of. Re-run with --host-change-authorized once the "
                    "user has asked for that. 'status' lists them first."
                )
            reclaimed = lab.reclaim_orphans(
                api, include_active=getattr(args, "include_active", False)
            )
        remaining = lab.active_leases()
        persistent = [x for x in remaining if lab.is_long_term(x)]
        if persistent and api.reachable() and not args.no_backup:
            # Weekly backups ride along with the watchdog, so a long-term
            # lease needs no separate schedule.
            try:
                from . import longterm
                backup_args = argparse.Namespace(
                    storage=None, keep=None,
                    interval_days=longterm.BACKUP_INTERVAL_DAYS,
                    force=False, timeout=7200,
                )
                with contextlib.redirect_stdout(io.StringIO()):
                    longterm.cmd_backup(lab._module(), backup_args)
            except (lab.LabError, OSError) as exc:
                print(f"warning: long-term backup sweep failed: {exc}",
                      file=sys.stderr)
        host_powered_off = False
        idle_seconds = int(lab.mcp_idle_elapsed())
        idle_shutdown_triggered = False
        if persistent or lab.CONFIG.proxmox.get("guest_mode", "all") == "lxc-only":
            # Long-term leases and VPS policy keep the host on.
            pass
        elif not remaining and (cleaned or args.all):
            host_powered_off = lab.shutdown_host(api)
        elif lab.idle_shutdown_due(
            reachable=api.reachable(),
            active_lease_count=len(remaining),
            has_failures=bool(failed),
            idle_seconds=idle_seconds,
        ):
            idle_shutdown_triggered = True
            lab.audit(
                "mcp-idle-shutdown-triggered",
                idle_seconds=idle_seconds,
                threshold_seconds=lab.MCP_IDLE_SHUTDOWN_SECONDS,
            )
            host_powered_off = lab.shutdown_host(api)
        # The watchdog runs every five minutes. Recording a no-op sweep would
        # append a journal line and a Forgejo commit each time, burying real
        # events under thousands of identical entries, so stay silent unless
        # the sweep actually did or failed something.
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
    # Deliberately after the controller lock is released: a vzdump can run for
    # hours, and nothing else may queue behind it.
    retained_backup: dict[str, Any] | None = None
    if not args.no_backup and not host_powered_off and api.reachable():
        from . import longterm as longterm_module

        if longterm_module.retained_backup_enabled():
            with lab.sweep_lock("retained-backup") as acquired:
                if not acquired:
                    retained_backup = {
                        "skipped": "a previous backup sweep is still running"
                    }
                else:
                    try:
                        retained_backup = longterm_module.backup_retained(
                            lab._module(), api,
                            storage=longterm_module.backup_storage(lab._module()),
                            keep=int(
                                lab.CONFIG.lease.get("long_term_backup_keep", 2)
                            ),
                            timeout=7200,
                            interval_days=int(lab.CONFIG.lease.get(
                                "retained_backup_interval_days", 7)),
                        )
                    except (lab.LabError, OSError) as exc:
                        retained_backup = {"error": str(exc)[:300]}
    print(
        json.dumps(
            {
                "cleaned": cleaned,
                "retried": retried,
                "failed": failed,
                "left_to_another_lease": transferred,
                "retained_backup": retained_backup,
                "remaining": [x["id"] for x in remaining],
                "mcp_idle_seconds": idle_seconds,
                "mcp_idle_shutdown_after_seconds": lab.MCP_IDLE_SHUTDOWN_SECONDS,
                "idle_shutdown_triggered": idle_shutdown_triggered,
                "host_powered_off": host_powered_off,
                **lab.host_power_policy(),
                **({"reclaimed_orphans": reclaimed} if reclaimed else {}),
            },
            indent=2,
            sort_keys=True,
        )
    )
    if failed:
        raise lab.LabError("One or more expired leases could not be cleaned")
    if reclaimed and reclaimed["failed"]:
        raise lab.LabError("One or more orphaned guests could not be stopped")
