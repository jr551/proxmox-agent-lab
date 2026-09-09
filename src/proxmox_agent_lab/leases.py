"""Lease lifecycle: state files, expiry, registration and power-on.

The data-layer functions take the state paths they need explicitly so
the module carries no configuration-derived globals of its own. The
command handlers and the power-on helper take `lab`, the CLI facade
every feature module already receives, which keeps the patch surface
used by the tests (and by any third-party registration callers)
unchanged.
"""

from __future__ import annotations

from . import inventory as inventory_module
from . import power as power_module
from .errors import LabError
from .state import iso_now, json_dump, utc_now
from typing import Any
import argparse
import datetime as dt
import json
import re
import secrets
import sys
import time


def lease_path(lease_root: Path, lease_id: str) -> Path:
    if not re.fullmatch(r"[a-z0-9-]{8,80}", lease_id):
        raise LabError("Invalid lease ID")
    return lease_root / f"{lease_id}.json"


def load_lease(lease_root: Path, lease_id: str, *,
               active: bool = True) -> dict[str, Any]:
    path = lease_path(lease_root, lease_id)
    if not path.exists():
        raise LabError(f"Unknown lease: {lease_id}")
    lease = json.loads(path.read_text())
    if active and lease.get("state") != "active":
        raise LabError(f"Lease {lease_id} is not active")
    return lease


def save_lease(lease_root: Path, lease: dict[str, Any]) -> None:
    json_dump(lease_path(lease_root, lease["id"]), lease)


def new_expiry(ttl: int) -> str:
    return (utc_now() + dt.timedelta(seconds=ttl)).isoformat().replace(
        "+00:00", "Z")


def parse_expiry(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def mcp_activity_path(state_root: Path) -> Path:
    return state_root / "mcp-activity.json"


def record_mcp_activity(state_root: Path, tool_name: str, *,
                        audit: Any) -> None:
    recorded_at = iso_now()
    json_dump(
        mcp_activity_path(state_root),
        {
            "last_command_at": recorded_at,
            "tool": tool_name[:160],
        },
    )
    audit("mcp-command", tool=tool_name[:160], command_at=recorded_at)


def mcp_idle_elapsed(state_root: Path,
                     now: dt.datetime | None = None) -> float:
    path = mcp_activity_path(state_root)
    if not path.exists():
        json_dump(
            path,
            {
                "last_command_at": iso_now(),
                "tool": "[idle-baseline]",
            },
        )
        return 0.0
    activity = json.loads(path.read_text())
    last_command = parse_expiry(activity["last_command_at"])
    return max(0.0, ((now or utc_now()) - last_command).total_seconds())


def idle_shutdown_due(
    *,
    reachable: bool,
    active_lease_count: int,
    has_failures: bool,
    idle_seconds: float,
    threshold_seconds: float,
) -> bool:
    return (
        reachable
        and active_lease_count == 0
        and not has_failures
        and idle_seconds >= threshold_seconds
    )


def _leases_in_states(
    lease_root: Path, states: tuple[str, ...], excluding: str | None = None
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for path in sorted(lease_root.glob("*.json")) if lease_root.exists() else []:
        try:
            lease = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if lease.get("state") in states and lease.get("id") != excluding:
            result.append(lease)
    return result


def active_leases(lease_root: Path,
                  excluding: str | None = None) -> list[dict[str, Any]]:
    return _leases_in_states(lease_root, ("active",), excluding)


def cleanup_candidate_leases(lease_root: Path) -> list[dict[str, Any]]:
    """Leases a sweep is allowed to finalize.

    `cleanup_failed` is included on purpose. A transient QEMU lock while
    stopping one guest used to take a lease out of every later sweep, leaving
    its guests -- and so the host -- running until somebody reran `lease-end`
    by hand with the exact lease id. Finalizing is idempotent, so retrying an
    already-cleaned resource costs nothing and the fail-closed guarantee holds.
    """
    return _leases_in_states(lease_root, ("active", "cleanup_failed"))


def lease_claims(lease: dict[str, Any], kind: str, vmid: int) -> bool:
    """True when `lease` lists (kind, vmid) among its resources."""
    for item in lease.get("resources", []):
        try:
            if item.get("kind") == kind and int(item.get("vmid")) == int(vmid):
                return True
        except (TypeError, ValueError):
            continue
    return False


def lease_is_live(lease: dict[str, Any],
                  now: dt.datetime | None = None) -> bool:
    """True while a lease still holds a claim on its resources.

    A long-term lease always does. An ordinary one does until it expires:
    after that the watchdog may clean it up, so it must not simultaneously be
    able to shield a resource from cleanup for ever.
    """
    if lease.get("state") != "active":
        return False
    if is_long_term(lease):
        return True
    expires = lease.get("expires_at")
    if not expires:
        return True
    try:
        return parse_expiry(str(expires)) > (now or utc_now())
    except (TypeError, ValueError):
        return True


def resource_owner_elsewhere(
    lease_root: Path, lease_id: str, kind: str, vmid: int, *,
    now: dt.datetime | None = None
) -> str | None:
    """The id of another *live* lease that also owns (kind, vmid), if any.

    Registration does not stop two leases from listing the same guest -- a
    VMID can be handed to a newer lease while an older one still names it --
    so ownership is resolved at cleanup time, before anything destructive.
    """
    for other in active_leases(lease_root, excluding=lease_id):
        if lease_claims(other, kind, vmid) and lease_is_live(other, now):
            return str(other.get("id"))
    return None


def is_long_term(lease: dict[str, Any]) -> bool:
    return lease.get("kind") == "long-term"


def long_term_leases(lease_root: Path) -> list[dict[str, Any]]:
    """Active long-term leases. While any exists, the host stays powered on."""
    return [lease for lease in active_leases(lease_root)
            if is_long_term(lease)]


def lease_requires_cleanup(lease: dict[str, Any]) -> bool:
    return any(
        resource.get("policy", "delete") == "delete"
        for resource in lease.get("resources", [])
    )


def all_lease_ids(lease_root: Path) -> set[str]:
    """Every lease id the controller still holds a record for, any state."""
    ids: set[str] = set()
    for path in sorted(lease_root.glob("*.json")) if lease_root.exists() else []:
        try:
            lease = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if lease.get("id"):
            ids.add(str(lease["id"]))
    return ids


def register_resource(
    lease: dict[str, Any],
    kind: str,
    vmid: int,
    policy: str,
    name: str | None = None,
    *,
    lease_root: Path,
    state_root: Path,
    default_ttl: int,
) -> None:
    existing = next(
        (item for item in lease["resources"] if int(item["vmid"]) == vmid), None
    )
    if existing:
        existing.update({"kind": kind, "policy": policy, "name": name})
    else:
        lease["resources"].append(
            {"kind": kind, "vmid": vmid, "policy": policy, "name": name}
        )
    lease["updated_at"] = iso_now()
    # Registering a guest extends an ordinary lease, but must never give a
    # long-term one an expiry: not expiring is the whole point of it.
    if not is_long_term(lease):
        lease["expires_at"] = new_expiry(default_ttl)
    save_lease(lease_root, lease)
    if policy == "retain":
        # This guest is meant to outlive the lease, and the lease record will
        # not be here for ever. Its node tag proves only that some lease made
        # it, so the durable owner is recorded now or never.
        try:
            inventory_module.record(
                state_root, kind=kind, vmid=vmid, lease=str(lease.get("id")),
                now=iso_now(), purpose=str(lease.get("purpose") or ""),
                name=name,
            )
        except OSError as exc:
            print(f"warning: could not record retained guest {kind}/{vmid}: "
                  f"{exc}", file=sys.stderr)


def require_owned_qemu(lab: Any, lease_id: str, vmid: int) -> None:
    lab.require_lease_resource(lab.load_lease(lease_id), "qemu", vmid)


def require_lease_resource(
    lease: dict[str, Any], kind: str, vmid: int
) -> None:
    """Refuse guest mutation unless the active lease registered that guest."""
    if not any(
        item.get("kind") == kind and int(item.get("vmid", -1)) == vmid
        for item in lease.get("resources", [])
    ):
        # Name the remedy. A refusal that only states the rule sends the
        # operator looking for a broken guest instead of an unregistered one.
        lease_id = str(lease.get("id") or "<id>")
        pre_existing = vmid in lease.get("initial_vmids", [])
        reason = (
            f"VMID {vmid} existed before this lease"
            if pre_existing
            else f"VMID {vmid} is not a {kind} guest registered to this lease"
        )
        register = (
            f"proxmox-lab lease-register --lease {lease_id} --kind {kind} "
            f"--vmid {vmid}" + (" --allow-existing" if pre_existing else "")
        )
        raise LabError(
            f"{reason}; register it with '{register}' if you intend to drive it"
        )


def ensure_on(lab: Any, api: lab.ProxmoxAPI, timeout: int | None = None) -> bool:
    """Switch the lab machine on if it is not already up. Returns True if we
    had to wake it."""
    if api.reachable():
        return False
    if timeout is None:
        timeout = int(lab.CONFIG.power.get("boot_timeout_seconds", 300))
    if timeout < lab.MIN_COLD_BOOT_TIMEOUT_SECONDS:
        raise lab.LabError(
            f"cold-boot timeout must be at least {lab.MIN_COLD_BOOT_TIMEOUT_SECONDS}s; "
            "the lab host commonly needs a minute or two before its API answers"
        )
    try:
        detail = power_module.power_on(lab.CONFIG)
    except (power_module.PowerError, lab.ConfigError) as exc:
        raise lab.LabError(f"cannot switch the lab machine on: {exc}") from None
    lab._audit_through_boot("lab-power-on-requested", **detail)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if api.reachable():
            lab._audit_through_boot("lab-power-on-verified", host=lab.HOST, node=lab.NODE)
            return True
        time.sleep(5)
    raise lab.LabError(
        f"power-on was requested via {detail.get('mode')} but Proxmox at "
        f"{lab.HOST}:{lab.PORT} did not respond within {timeout}s. Check that the "
        "machine booted and that Proxmox starts on boot."
    )


def cmd_power_on(lab: Any, args: argparse.Namespace) -> None:
    if not args.standalone_authorized:
        raise lab.LabError(
            "Standalone power-on has no lease finalizer and is refused by default. "
            "Use lease-begin for normal work, or pass --standalone-authorized "
            "only when a person will manage shutdown."
        )
    changed = lab.ensure_on(lab.ProxmoxAPI(), timeout=args.timeout)
    print(json.dumps({"reachable": True, "power_on_requested": changed}))


def cmd_lease_begin(lab: Any, args: argparse.Namespace) -> None:
    api = lab.ProxmoxAPI()
    with lab.controller_lock():
        powered_on = lab.ensure_on(api, timeout=args.timeout)
        lease_id = f"{lab.utc_now().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(4)}"
        existing = api.call("GET", "/cluster/resources", {"type": "vm"})
        long_term = bool(getattr(args, "long_term", False))
        lease = {
            "id": lease_id,
            "purpose": args.purpose[:240],
            "kind": "long-term" if long_term else "session",
            "created_at": lab.iso_now(),
            "updated_at": lab.iso_now(),
            # A long-term lease never expires; that is the point. The watchdog
            # skips it, and its guests survive until explicitly destroyed.
            "expires_at": None if long_term else lab.new_expiry(args.ttl),
            "state": "active",
            "host_was_powered_on": powered_on,
            "initial_vmids": sorted(
                int(item["vmid"]) for item in existing if "vmid" in item
            ),
            "resources": [],
        }
        lab.save_lease(lease)
        try:
            lab.audit(
                "lease-begin",
                lease=lease_id,
                kind=lease["kind"],
                purpose=lease["purpose"],
                expires_at=lease["expires_at"],
                initial_vmids=lease["initial_vmids"],
            )
            output = dict(lease)
            if long_term:
                output["warning"] = (
                    "This is a long-term lease: the lab machine will stay powered on "
                    "until it is destroyed with 'lease-destroy'. Its guests are "
                    "protected from deletion and backed up weekly."
                )
            print(json.dumps(output, indent=2, sort_keys=True))
        except BaseException:
            lab.lease_path(lease_id).unlink(missing_ok=True)
            raise


def cmd_lease_heartbeat(lab: Any, args: argparse.Namespace) -> None:
    with lab.controller_lock():
        lease = lab.load_lease(args.lease)
        if lab.is_long_term(lease):
            print(json.dumps({
                "lease": args.lease, "kind": "long-term", "expires_at": None,
                "note": "long-term leases do not expire; no heartbeat needed",
            }, indent=2))
            return
        lease["updated_at"] = lab.iso_now()
        lease["expires_at"] = lab.new_expiry(args.ttl)
        lab.save_lease(lease)
        lab.audit(
            "lease-heartbeat",
            lease=args.lease,
            expires_at=lease["expires_at"],
        )
    print(json.dumps({"lease": args.lease, "expires_at": lease["expires_at"]}))


def cmd_lease_register(lab: Any, args: argparse.Namespace) -> None:
    with lab.controller_lock():
        lease = lab.load_lease(args.lease)
        if lab.is_long_term(lease):
            # Guests of a long-term lease are meant to survive, so they are
            # retained and given Proxmox's protection flag.
            args.policy = "retain"
        if args.vmid in lease["initial_vmids"] and not args.allow_existing:
            raise lab.LabError(
                f"VMID {args.vmid} existed before this lease; use --allow-existing "
                "only for an explicitly authorized retained resource"
            )
        owner = lab.resource_owner_elsewhere(args.lease, args.kind, args.vmid)
        if owner:
            raise lab.LabError(
                f"{args.kind}/{args.vmid} is already registered to live lease "
                f"{owner}. Two leases owning one guest is how a cleanup sweep "
                f"comes to delete a machine another lease is still using. "
                f"Work under {owner}, or end/abandon it first."
            )
        lab.register_resource(lease, args.kind, args.vmid, args.policy, args.name)
        lab.audit(
            "lease-register",
            lease=args.lease,
            kind=args.kind,
            vmid=args.vmid,
            policy=args.policy,
            name=args.name,
        )
    print(json.dumps({"registered": True, "lease": args.lease, "vmid": args.vmid}))
