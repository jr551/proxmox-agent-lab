"""Long-term leases: machines that are meant to stay.

A normal lease is a promise that everything disappears. A long-term lease is
the opposite promise, and it changes three things:

* **The host stays on.** While any long-term lease is active, nothing powers
  the machine down -- not `lease-end`, not the idle timer, not the watchdog.
* **Its guests are protected.** They get Proxmox's `protection` flag, so even
  a direct delete is refused until the lease is destroyed.
* **They are backed up weekly** to the slowest, largest storage available,
  because a machine you keep is a machine whose loss would hurt.

Nothing here expires. The only way out is `lease-destroy --confirm`, which
lifts the protection, deletes the guests, and lets the host power down again.
`lease-release --confirm` is the distinct preservation path: it closes the
lease, retains its stopped guests, and also lets the host power down.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from . import config as _config

_CONFIG = _config.get()
BACKUP_INTERVAL_DAYS = 7


def backup_storage(lab: Any) -> str:
    """Where long-term backups go: the slowest, roomiest storage configured."""
    configured = _CONFIG.lease.get("long_term_backup_storage") or ""
    return configured or _CONFIG.storage.bulk_storage


def set_protection(lab: Any, api: Any, kind: str, vmid: int,
                   protected: bool) -> None:
    """Proxmox refuses to delete a guest with `protection` set."""
    api.call(
        "PUT", f"/nodes/{lab.NODE}/{kind}/{vmid}/config",
        {"protection": 1 if protected else 0},
    )


def _due(last: str | None, now: dt.datetime, interval_days: int) -> bool:
    if not last:
        return True
    try:
        when = dt.datetime.fromisoformat(last.replace("Z", "+00:00"))
    except ValueError:
        return True
    return (now - when).total_seconds() >= interval_days * 86400


def backup_guest(lab: Any, api: Any, vmid: int, *, storage: str, keep: int,
                 timeout: int, notes: str) -> dict[str, Any]:
    """One vzdump, snapshot mode so a running guest stays up."""
    try:
        upid = api.call(
            "POST", f"/nodes/{lab.NODE}/vzdump",
            {
                "vmid": int(vmid),
                "storage": storage,
                "mode": "snapshot",       # keep the guest running
                "compress": "zstd",
                "remove": 0,
                "prune-backups": f"keep-last={keep}",
                "notes-template": notes,
            },
        )
        status = lab.wait_task(api, upid, timeout=timeout)
        return {"ok": True, "task": upid, "status": status.get("exitstatus")}
    except lab.LabError as exc:
        return {"ok": False, "error": str(exc)[:300]}


def backup_lease(lab: Any, api: Any, lease: dict[str, Any], *,
                 storage: str, keep: int, timeout: int) -> dict[str, Any]:
    """Back up every guest in one lease. Returns a per-guest report."""
    results: dict[str, Any] = {}
    for resource in lease.get("resources", []):
        vmid = int(resource["vmid"])
        results[str(vmid)] = backup_guest(
            lab, api, vmid, storage=storage, keep=keep, timeout=timeout,
            notes=f"long-term lease {lease['id']}",
        )
    return results


def retained_backup_enabled() -> bool:
    """Off by default, and deliberately so.

    Turning this on starts writing vzdump archives of every retained guest to
    the bulk store on a schedule. That is the right protection for templates
    that cost hours to rebuild, but it is also gigabytes of writes and hours of
    wall clock on a slow disk, so it is the operator's decision rather than a
    default this tool assumes. `doctor` reports the coverage gap either way.
    """
    return bool(_CONFIG.lease.get("retained_backup", False))


def retained_due(lab: Any, *, now: dt.datetime, interval_days: int,
                 force: bool = False) -> list[dict[str, Any]]:
    """Retained-registry guests whose last backup is older than the interval."""
    from . import inventory as inventory_module

    due: list[dict[str, Any]] = []
    for item in inventory_module.entries(lab.STATE_ROOT).values():
        if force or _due(item.get("last_backup_at"), now, interval_days):
            due.append(item)
    return sorted(due, key=lambda item: int(item.get("vmid", 0)))


def backup_retained(lab: Any, api: Any, *, storage: str, keep: int,
                    timeout: int, interval_days: int, force: bool = False,
                    ) -> dict[str, Any]:
    """Back up guests that outlive their lease.

    Lease-driven backups only ever covered guests of an *active long-term
    lease*, so templates and persistent workers -- the guests whose loss costs
    the most rebuild time -- had no coverage at all once their lease ended.
    """
    from . import inventory as inventory_module

    now = lab.utc_now()
    report: dict[str, Any] = {"guests": {}}
    for item in retained_due(lab, now=now, interval_days=interval_days,
                             force=force):
        kind, vmid = str(item.get("kind", "qemu")), int(item["vmid"])
        outcome = backup_guest(
            lab, api, vmid, storage=storage, keep=keep, timeout=timeout,
            notes=f"retained guest {kind}/{vmid}",
        )
        if outcome["ok"]:
            inventory_module.mark_backup(
                lab.STATE_ROOT, kind, vmid, lab.iso_now()
            )
        report["guests"][f"{kind}/{vmid}"] = outcome
        lab.audit("retained-backup", kind=kind, vmid=vmid, storage=storage,
                  ok=outcome["ok"])
    report["backed_up"] = sum(
        1 for x in report["guests"].values() if x.get("ok")
    )
    report["failed"] = sum(
        1 for x in report["guests"].values() if not x.get("ok")
    )
    return report


def cmd_backup(lab: Any, args: Any) -> None:
    """Run weekly backups for long-term leases, and optionally retained guests."""
    api = lab.ProxmoxAPI()
    if getattr(args, "retained", False):
        if not (retained_backup_enabled() or args.force):
            print(json.dumps({
                "skipped": "retained backups are off; set [lease] "
                           "retained_backup = true, or pass --force",
            }, indent=2, sort_keys=True))
            return
        if not api.reachable():
            lab.ensure_on(api)
        result = backup_retained(
            lab, api,
            storage=args.storage or backup_storage(lab),
            keep=int(args.keep or _CONFIG.lease.get("long_term_backup_keep", 2)),
            timeout=args.timeout,
            interval_days=args.interval_days,
            force=args.force,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        if result["failed"]:
            raise lab.LabError(
                f"{result['failed']} retained guest backup(s) failed"
            )
        return
    leases = lab.long_term_leases()
    if not leases:
        print(json.dumps({"long_term_leases": 0, "backed_up": []}, indent=2))
        return
    if not _CONFIG.lease.get("long_term_backup", True) and not args.force:
        print(json.dumps(
            {"skipped": "backups disabled by [lease] long_term_backup"},
            indent=2))
        return

    storage = args.storage or backup_storage(lab)
    keep = int(args.keep or _CONFIG.lease.get("long_term_backup_keep", 2))
    now = lab.utc_now()
    report: dict[str, Any] = {"storage": storage, "keep": keep, "leases": {}}

    for lease in leases:
        if not args.force and not _due(lease.get("last_backup_at"), now,
                                       args.interval_days):
            report["leases"][lease["id"]] = {"skipped": "not due yet",
                                             "last": lease.get("last_backup_at")}
            continue
        if not api.reachable():
            lab.ensure_on(api)
        results = backup_lease(lab, api, lease, storage=storage, keep=keep,
                               timeout=args.timeout)
        succeeded = all(entry.get("ok") for entry in results.values())
        if succeeded and results:
            lease["last_backup_at"] = lab.iso_now()
            lab.save_lease(lease)
        report["leases"][lease["id"]] = results
        lab.audit("long-term-backup", lease=lease["id"], storage=storage,
                  guests=len(results), all_succeeded=succeeded)

    print(json.dumps(report, indent=2, sort_keys=True))


def cmd_destroy(lab: Any, args: Any) -> None:
    """Tear down a long-term lease: unprotect, delete, allow power-off."""
    api = lab.ProxmoxAPI()
    lease = lab.load_lease(args.lease, active=False)
    if not lab.is_long_term(lease):
        raise lab.LabError(
            f"{args.lease} is an ordinary lease; end it with 'lease-end'"
        )
    guests = [
        f"{r['kind']}/{r['vmid']} ({r.get('name') or 'unnamed'})"
        for r in lease.get("resources", [])
    ]
    if not args.confirm:
        raise lab.LabError(
            "This permanently destroys a long-term lease and everything in "
            f"it:\n  " + ("\n  ".join(guests) or "(no registered guests)")
            + "\n\nBacked up so far: "
            + (lease.get("last_backup_at") or "never")
            + "\nRe-run with --confirm if that is what you want."
        )
    if not api.reachable():
        lab.ensure_on(api)

    # Lift protection first, or the deletes are refused.
    for resource in lease.get("resources", []):
        try:
            set_protection(lab, api, resource["kind"], int(resource["vmid"]),
                           False)
        except lab.LabError as exc:
            lab.audit("long-term-unprotect-failed", lease=lease["id"],
                      vmid=resource["vmid"], error=str(exc))
    for resource in lease.get("resources", []):
        resource["policy"] = "delete"

    lease["kind"] = "session"      # so the ordinary finaliser will act on it
    lab.save_lease(lease)
    failures = lab.finalize_lease(api, lease)

    others = lab.active_leases(excluding=args.lease)
    host_powered_off = False
    if not others:
        host_powered_off = lab.shutdown_host(api)
    lab.audit("long-term-destroyed", lease=args.lease, failures=failures,
              host_powered_off=host_powered_off)
    print(json.dumps({
        "lease": args.lease,
        "destroyed_guests": guests,
        "failures": failures,
        "host_powered_off": host_powered_off,
        "remaining_active_leases": [x["id"] for x in others],
    }, indent=2, sort_keys=True))
    if failures:
        raise lab.LabError("some guests could not be destroyed")


def cmd_release(lab: Any, args: Any) -> None:
    """Close a long-term lease while preserving every registered guest."""
    api = lab.ProxmoxAPI()
    lease = lab.load_lease(args.lease, active=False)
    if not lab.is_long_term(lease):
        raise lab.LabError(
            f"{args.lease} is an ordinary lease; end it with 'lease-end'"
        )
    guests = [
        f"{r['kind']}/{r['vmid']} ({r.get('name') or 'unnamed'})"
        for r in lease.get("resources", [])
    ]
    if not args.confirm:
        raise lab.LabError(
            "This closes the long-term lease and leaves its stopped guests "
            "outside lease ownership:\n  "
            + ("\n  ".join(guests) or "(no registered guests)")
            + "\nRe-run with --confirm if that is what you want."
        )
    if not api.reachable():
        lab.ensure_on(api)
    for resource in lease.get("resources", []):
        resource["policy"] = "retain"
        try:
            set_protection(
                lab, api, resource["kind"], int(resource["vmid"]), False
            )
        except lab.LabError as exc:
            lab.audit(
                "long-term-unprotect-failed", lease=lease["id"],
                vmid=resource["vmid"], error=str(exc),
            )
    lease["kind"] = "session"
    lab.save_lease(lease)
    failures = lab.finalize_lease(api, lease)
    others = lab.active_leases(excluding=args.lease)
    host_powered_off = False
    if not others:
        host_powered_off = lab.shutdown_host(api)
    lab.audit(
        "long-term-released", lease=args.lease, failures=failures,
        retained=len(guests), host_powered_off=host_powered_off,
    )
    print(json.dumps({
        "lease": args.lease,
        "retained_guests": guests,
        "failures": failures,
        "host_powered_off": host_powered_off,
        "remaining_active_leases": [x["id"] for x in others],
    }, indent=2, sort_keys=True))
    if failures:
        raise lab.LabError("some retained guests could not be finalized")


def cmd_list(lab: Any, args: Any) -> None:
    """All active leases, and whether the machine is pinned on."""
    leases = lab.active_leases()
    persistent = [x for x in leases if lab.is_long_term(x)]
    print(json.dumps({
        "active": [
            {
                "id": x["id"],
                "kind": x.get("kind", "session"),
                "purpose": x.get("purpose"),
                "expires_at": x.get("expires_at"),
                "guests": [r["vmid"] for r in x.get("resources", [])],
                "last_backup_at": x.get("last_backup_at"),
            }
            for x in leases
        ],
        "host_pinned_on": bool(persistent),
        "pinned_by": [x["id"] for x in persistent],
    }, indent=2, sort_keys=True))


def register(sub: Any, lab: Any) -> None:
    from .cli import _bind


    listing = sub.add_parser("lease-list", help="show active leases")
    listing.set_defaults(func=_bind(lab, cmd_list))

    destroy = sub.add_parser(
        "lease-destroy",
        help="permanently destroy a long-term lease and its machines",
    )
    destroy.add_argument("--lease", required=True)
    destroy.add_argument("--confirm", action="store_true",
                         help="required: this deletes protected machines")
    destroy.set_defaults(func=_bind(lab, cmd_destroy))

    release = sub.add_parser(
        "lease-release",
        help="close a long-term lease but retain its stopped machines",
    )
    release.add_argument("--lease", required=True)
    release.add_argument(
        "--confirm", action="store_true",
        help="required: retained machines become independent of the lease",
    )
    release.set_defaults(func=_bind(lab, cmd_release))

    backup = sub.add_parser(
        "backup", help="run weekly backups for long-term leases that are due"
    )
    backup.add_argument(
        "--retained", action="store_true",
        help="back up guests in the retained registry (templates, persistent "
             "workers) instead of long-term leases. Off unless [lease] "
             "retained_backup = true, or --force",
    )
    backup.add_argument("--storage", help="default: the bulk storage")
    backup.add_argument("--keep", type=int)
    backup.add_argument("--interval-days", type=int,
                        default=BACKUP_INTERVAL_DAYS)
    backup.add_argument("--force", action="store_true",
                        help="back up now even if not due")
    backup.add_argument("--timeout", type=int, default=7200)
    backup.set_defaults(func=_bind(lab, cmd_backup))
