"""Lease lifecycle: begin/heartbeat/register/list over the SQLite store.

One lease row per work session in ``<state dir>/lab.db`` (rework plan §C); the
per-lease JSON files are gone. Long-term is not a separate subsystem any more:
it is ``kind='long_term'`` with ``expires_at=0`` and guest metadata
``pxl-expiry=0``, and its protection is that metadata alone -- no PVE
``protection`` flag.

Guest identity for the host-side GC is written when a guest joins the lease:
tags ``proxmoxagentlab;<controller-hostname>;lease-<id>`` and description
``pxl-lease=<id> pxl-expiry=<epoch>``
(``0`` for long-term). Every heartbeat rewrites that expiry on every
registered guest, because the GC is metadata-driven and stale metadata would
reap live work.

The data-layer functions keep their legacy call shapes -- the state root paths
they receive locate the store (``state_root = lease_root.parent``, with the
database at ``<state root>/lab.db``) -- and the ``api`` parameter surviving
callers still pass is unused: the proxmox seam built from the configuration
replaces the old HTTP client everywhere below.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import secrets
import socket
import time
from pathlib import Path
from typing import Any

from . import power as power_module
from . import proxmox as proxmox_module
from . import store as store_module
from .errors import LabError

#: The long-term lease kind. Its expiry is 0 in the store and in guest
#: metadata: nothing expires it but `lease-destroy --confirm`.
LONG_TERM = "long_term"
ORDINARY = "ordinary"

#: Lease states that still own their world. `ending` is transient (one
#: finalizer holds it); `cleanup_failed` keeps ownership on purpose so every
#: later sweep retries the teardown.
NON_TERMINAL_STATES = ("active", "ending", "cleanup_failed")

LEASE_ID_PATTERN = re.compile(r"[a-z0-9-]{8,80}")

MIN_COLD_BOOT_TIMEOUT_SECONDS = 90
DEFAULT_BOOT_TIMEOUT_SECONDS = 300

_LEASES_DIRNAME = "leases"


def _make_proxmox(config: Any) -> proxmox_module.Proxmox:
    """The proxmox seam for this configuration (tests substitute a double)."""
    return proxmox_module.from_config(config)


def _ssh_of(seam: Any) -> Any:
    """The ssh transport underneath a proxmox seam.

    Reachability probing lives on the ssh layer (``ssh.SSH.probe`` is safe to
    call from lease-begin and the verified-shutdown loop by design).
    """
    return getattr(seam, "_ssh", seam)


def lease_root_for(state_root: Path) -> Path:
    """Where lease paths are rooted; kept for the legacy call shapes."""
    return Path(state_root) / _LEASES_DIRNAME


def _open_store(root: Path) -> store_module.Store:
    """Open ``lab.db`` beneath either a lease root or a state root directory."""
    path = Path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    return store_module.Store(path)


def _store(lease_root: Path) -> store_module.Store:
    return _open_store(Path(lease_root).parent / "lab.db")


def _store_at(state_root: Path) -> store_module.Store:
    return _open_store(Path(state_root) / "lab.db")


def _lease_root(lab: Any) -> Path:
    return lease_root_for(Path(lab.STATE_ROOT))


def _epoch(when: Any) -> float:
    """A unix epoch from ``None`` (now), a datetime, or a number."""
    if when is None:
        return time.time()
    if isinstance(when, dt.datetime):
        return when.timestamp()
    return float(when)


def _ttl_seconds(config: Any, args: Any) -> int:
    value = getattr(args, "ttl", None)
    return int(value) if value else int(config.lease.ttl_seconds)


def lease_path(lease_root: Path, lease_id: str) -> Path:
    """The file holding this lease's record.

    Leases no longer live in per-lease JSON files: every row is in the single
    ``lab.db`` under the state root, so that is the path named here. The id
    validation the old path computation performed is kept -- callers treat
    this as the cheap well-formedness gate it always was.
    """
    if not LEASE_ID_PATTERN.fullmatch(lease_id):
        raise LabError("Invalid lease ID")
    return Path(lease_root).parent / "lab.db"


def load_lease(lease_root: Path, lease_id: str, *,
               active: bool = True) -> dict[str, Any]:
    """The lease row, with its registered resources attached.

    ``active=True`` demands state ``active`` exactly (the old gate). The
    returned dict carries the ``leases`` table columns (``expires_at`` is a
    unix epoch, ``0`` for long-term) plus ``resources``.
    """
    lease_path(lease_root, lease_id)          # id gate
    with _store(lease_root) as store:
        row = store.get_lease(lease_id)
        if row is None:
            raise LabError(f"Unknown lease: {lease_id}")
        if active and row["state"] != "active":
            raise LabError(f"Lease {lease_id} is not active")
        lease = dict(row)
        lease["resources"] = store.resources_for(lease_id)
        return lease


def save_lease(lease_root: Path, lease: dict[str, Any]) -> None:
    """Persist the closable fields of a lease dict.

    Legacy shape over the store: the fields the ``leases`` table holds are
    written (``state``, ``last_error``, ``ended_at``). Extending expiry is not
    a save -- it goes through :func:`register_resource` / the heartbeat.
    """
    lease_id = str(lease.get("id") or "")
    lease_path(lease_root, lease_id)          # id gate
    with _store(lease_root) as store:
        store.set_lease_state(
            lease_id,
            str(lease.get("state") or "active"),
            error=lease.get("last_error"),
            ended=bool(lease.get("ended_at")),
        )


def claim_lease(lease_root: Path, lease_id: str, *,
                from_state: str, to_state: str) -> bool:
    """Compare-and-swap a lease between states; ``True`` iff this caller won.

    This is what makes `lease-end` and `cleanup-expired` racing harmless: the
    winner tears down, the loser reports "already ending".
    """
    with _store(lease_root) as store:
        return store.claim_lease(
            lease_id, from_state=from_state, to_state=to_state
        )


def new_expiry(ttl: int) -> str:
    """An ISO-8601 expiry stamp (legacy helper; the store uses epochs)."""
    now = dt.datetime.now(dt.timezone.utc)
    return (now + dt.timedelta(seconds=ttl)).isoformat().replace("+00:00", "Z")


def parse_expiry(value: str) -> dt.datetime:
    """Parse an ISO-8601 stamp (legacy helper; the store uses epochs)."""
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def metadata_description(lease_id: str, expiry_epoch: int) -> str:
    """The GC metadata line for one guest (rework plan §F)."""
    return f"pxl-lease={lease_id} pxl-expiry={int(expiry_epoch)}"


#: The tag the host-side GC and the controller's own cross-checks key on.
#: Deliberately readable on the Proxmox GUI: a guest carrying it is ours.
OWNERSHIP_TAG = "proxmoxagentlab"

#: Pre-rename guests still carry ``pxl``; matchers accept it so a guest
#: stamped before the rename is never orphaned by it. New stamps never use it.
LEGACY_OWNERSHIP_TAG = "pxl"

_TAG_SAFE = re.compile(r"[^a-z0-9-]+")


def controller_tag() -> str:
    """This machine's short name as a Proxmox tag (``mac``, ``omp-box``...).

    PVE tag chars are restricted, so fold the hostname to lowercase,
    collapse anything outside ``[a-z0-9-]`` to ``-``, and trim; an empty or
    unguessable hostname degrades to ``controller`` rather than failing the
    stamp.
    """
    try:
        raw = socket.gethostname() or ""
    except OSError:
        raw = ""
    short = raw.split(".", 1)[0]                     # drop any domain part
    folded = _TAG_SAFE.sub("-", short.strip().lower()).strip("-")
    return folded or "controller"


def metadata_tags(lease_id: str, controller: str | None = None) -> str:
    """The GC tags for one guest: ownership, the machine that made it, lease.

    ``proxmoxagentlab`` is the sweep key; the controller hostname is human
    context (which machine created it); ``lease-<id>`` pins the owner.
    """
    host = controller if controller is not None else controller_tag()
    return f"{OWNERSHIP_TAG};{host};lease-{lease_id}"


def _stamp_guest(seam: Any, kind: str, vmid: int, lease_id: str,
                 expiry_epoch: int) -> None:
    seam.set_metadata(
        kind, vmid,
        tags=metadata_tags(lease_id),
        description=metadata_description(lease_id, expiry_epoch),
    )


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
    """Register a lease-owned guest and stamp its GC metadata.

    Registration extends an ordinary lease (and stamps the new expiry on the
    guest) but must never give a long-term one an expiry: not expiring is the
    whole point of it, and its guests are stamped ``pxl-expiry=0``.

    ``state_root`` is accepted for the legacy call shape (the retained-registry
    it once fed died with ``inventory.py``; the resource row is the durable
    owner record now). Re-registering the same guest is idempotent: the row is
    left as first registered and the metadata/expiry are refreshed.
    """
    lease_id = str(lease.get("id") or "")
    lease_path(lease_root, lease_id)          # id gate
    policy = "retain" if policy == "retain" else "disposable"
    vmid = int(vmid)
    long_term = is_long_term(lease)
    expiry_epoch = 0 if long_term else int(time.time()) + int(default_ttl)
    with _store(lease_root) as store:
        try:
            store.register_resource(
                lease_id, kind, vmid, name=name, policy=policy
            )
        except store_module.StoreError as exc:
            if "already registered" not in str(exc):
                raise
        if not long_term:
            store.heartbeat(lease_id, expires_at=expiry_epoch)
    # Keep the caller's dict in step, the way the JSON record used to.
    resources = lease.setdefault("resources", [])
    for resource in resources:
        if str(resource.get("kind")) == kind and int(resource.get("vmid", -1)) == vmid:
            resource.update({"policy": policy, "name": name})
            break
    else:
        resources.append({"kind": kind, "vmid": vmid, "policy": policy,
                          "name": name})
    seam = _make_proxmox(_process_config())
    _stamp_guest(seam, kind, vmid, lease_id, expiry_epoch)


def _process_config() -> Any:
    """The process configuration, read at call time (tests patch the seam)."""
    from . import config as config_module

    return config_module.get()


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
        register = (
            f"proxmox-lab lease-register --lease {lease_id} --kind {kind} "
            f"--vmid {vmid}"
        )
        raise LabError(
            f"VMID {vmid} is not a {kind} guest registered to this lease; "
            f"register it with '{register}' if you intend to drive it"
        )


def leases_in_states(
    lease_root: Path, states: tuple[str, ...], excluding: str | None = None
) -> list[dict[str, Any]]:
    """Every lease in one of ``states`` (resources attached), oldest first."""
    result: list[dict[str, Any]] = []
    with _store(lease_root) as store:
        for row in store.list_leases(include_ended=True):
            if row["state"] in states and row["id"] != excluding:
                lease = dict(row)
                lease["resources"] = store.resources_for(row["id"])
                result.append(lease)
    return result


def active_leases(lease_root: Path,
                  excluding: str | None = None) -> list[dict[str, Any]]:
    """Leases that still own their world: every non-terminal state.

    ``cleanup_failed`` counts on purpose -- its resources are still
    lease-owned (that is what makes every later sweep retry them), so a
    machine must not be powered out from under them.
    """
    return leases_in_states(lease_root, NON_TERMINAL_STATES, excluding)


def cleanup_candidate_leases(lease_root: Path) -> list[dict[str, Any]]:
    """Leases a sweep is allowed to finalize.

    ``cleanup_failed`` is included on purpose. A transient QEMU lock while
    stopping one guest used to take a lease out of every later sweep, leaving
    its guests -- and so the host -- running until somebody reran `lease-end`
    by hand with the exact lease id. Finalizing is idempotent, so retrying an
    already-cleaned resource costs nothing and the fail-closed guarantee holds.
    """
    return leases_in_states(lease_root, ("active", "cleanup_failed"))


def all_lease_ids(lease_root: Path) -> set[str]:
    """Every lease id the controller still holds a record for, any state."""
    with _store(lease_root) as store:
        return {str(row["id"]) for row in store.list_leases(include_ended=True)}


def long_term_leases(lease_root: Path) -> list[dict[str, Any]]:
    """Active long-term leases. While any exists, the host stays powered on."""
    return [lease for lease in active_leases(lease_root) if is_long_term(lease)]


def lease_claims(lease: dict[str, Any], kind: str, vmid: int) -> bool:
    """True when `lease` lists a live (kind, vmid) among its resources.

    A resource whose ``destroyed_at`` is set claims nothing: the guest is
    gone, so there is no machine left to collide over.
    """
    for item in lease.get("resources", []):
        if item.get("destroyed_at"):
            continue
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
    able to shield a resource from clean-up for ever. A terminal-state lease
    never does; ``cleanup_failed`` still does, because ownership has not
    ended.
    """
    if lease.get("state") in store_module.TERMINAL_STATES:
        return False
    if is_long_term(lease):
        return True
    expires = lease.get("expires_at")
    if not expires:
        return True
    try:
        return int(expires) > _epoch(now)
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
    An expired claim never shields a guest; a live one always does.
    """
    for other in active_leases(lease_root, excluding=lease_id):
        if lease_claims(other, kind, vmid) and lease_is_live(other, now):
            return str(other.get("id"))
    return None


def forget_resource(lease_root: Path, kind: str, vmid: int) -> int:
    """Record (kind, vmid) as destroyed on every lease that claimed it.

    A guest that is gone is not retained. The old retained-registry JSON died
    with ``inventory.py``; the resource row is the durable owner record now,
    and ``destroyed_at`` is its tombstone. Returns the rows stamped.
    """
    stamped = 0
    with _store(lease_root) as store:
        for row in store.list_leases(include_ended=True):
            if store.mark_destroyed(str(row["id"]), kind, int(vmid)):
                stamped += 1
    return stamped


def is_long_term(lease: dict[str, Any]) -> bool:
    return lease.get("kind") == LONG_TERM


def lease_requires_cleanup(lease: dict[str, Any]) -> bool:
    return any(
        not resource.get("destroyed_at")
        and resource.get("policy", "disposable") != "retain"
        for resource in lease.get("resources", [])
    )


def ensure_on(lab: Any, api: Any, timeout: int | None = None) -> bool:
    """Switch the lab machine on if it is not already up.

    Returns True if we had to wake it. ``api`` is unused: the proxmox seam
    built from the configuration replaces the old HTTP client.
    """
    config = lab.CONFIG
    seam = _make_proxmox(config)
    if _ssh_of(seam).probe():
        return False
    if timeout is None:
        timeout = DEFAULT_BOOT_TIMEOUT_SECONDS
    timeout = int(timeout)
    if timeout < MIN_COLD_BOOT_TIMEOUT_SECONDS:
        raise LabError(
            f"cold-boot timeout must be at least "
            f"{MIN_COLD_BOOT_TIMEOUT_SECONDS}s; the lab host commonly needs a "
            "minute or two before it answers"
        )
    detail = power_module.wake(config)
    lab.audit("lab-power-on-requested", **detail)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _ssh_of(seam).probe():
            lab.audit("lab-power-on-verified", node=config.pve.node)
            return True
        time.sleep(5)
    raise LabError(
        f"power-on was requested but the host did not answer within "
        f"{timeout}s. Check that the machine booted and that SSH starts on boot."
    )


# -- MCP idle clock --------------------------------------------------------

def mcp_activity_path(state_root: Path) -> Path:
    """The file recording MCP tool activity.

    The idle clock is one ``schema_meta`` row inside ``lab.db`` (rework plan
    §C), so the store file is the only path this can honestly name.
    """
    return Path(state_root) / "lab.db"


def record_mcp_activity(state_root: Path, tool_name: str, *,
                        audit: Any) -> None:
    """Refresh the MCP idle clock and note the tool call."""
    recorded_at = store_module.utc_now()
    with _store_at(state_root) as store:
        store.touch_mcp_activity()
    audit("mcp-command", tool=tool_name[:160], command_at=recorded_at)


def mcp_idle_elapsed(state_root: Path,
                     now: dt.datetime | None = None) -> float:
    """Seconds since the last MCP tool call (0 while none was recorded)."""
    with _store_at(state_root) as store:
        last = store.last_mcp_activity()
    if last is None:
        return 0.0
    return max(0.0, _epoch(now) - float(last))


def mcp_idle_shutdown_due(state_root: Path, *, idle_shutdown_seconds: float,
                          now: dt.datetime | None = None) -> bool:
    """True only when the lab is both idle and unowned.

    Both conditions, no others: the MCP clock must have been quiet for at
    least ``idle_shutdown_seconds`` AND every lease must have ended. An idle
    clock while somebody holds a lease is somebody thinking, not an empty lab.
    """
    idle = mcp_idle_elapsed(state_root, now)
    with _store_at(state_root) as store:
        active = store.active_leases()
    return idle >= float(idle_shutdown_seconds) and not active


def idle_shutdown_due(
    *,
    reachable: bool,
    active_lease_count: int,
    has_failures: bool,
    idle_seconds: float,
    threshold_seconds: float,
) -> bool:
    """The idle-shutdown predicate over already-known facts (legacy shape)."""
    return (
        reachable
        and active_lease_count == 0
        and not has_failures
        and idle_seconds >= threshold_seconds
    )


# -- command handlers ------------------------------------------------------

def cmd_power_on(lab: Any, args: argparse.Namespace) -> None:
    if not args.standalone_authorized:
        raise LabError(
            "Standalone power-on has no lease finalizer and is refused by "
            "default. Use lease-begin for normal work, or pass "
            "--standalone-authorized only when a person will manage shutdown."
        )
    changed = ensure_on(lab, None, timeout=args.timeout)
    print(json.dumps({"reachable": True, "power_on_requested": changed}))


def cmd_lease_begin(lab: Any, args: argparse.Namespace) -> None:
    config = lab.CONFIG
    seam = _make_proxmox(config)
    if not _ssh_of(seam).probe():
        raise LabError(
            f"the lab host ({config.ssh.target}) is not reachable, so "
            "lease-begin would open a lease that cannot be governed. Wake it "
            "with 'power wake' (or check the network) and re-run."
        )
    lease_root = _lease_root(lab)
    long_term = bool(getattr(args, "long_term", False))
    lease_id = (
        dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S")
        + "-"
        + secrets.token_hex(4)
    )
    purpose = str(args.purpose)[:240]
    # A long-term lease never expires: expires_at=0 is the store's spelling of
    # "nothing but lease-destroy ends this", mirrored in pxl-expiry=0.
    expires_at = 0 if long_term else int(time.time()) + _ttl_seconds(config, args)
    with _store(lease_root) as store:
        store.create_lease(
            lease_id,
            kind=LONG_TERM if long_term else ORDINARY,
            purpose=purpose,
            expires_at=expires_at,
        )
    try:
        lab.audit(
            "lease-begin",
            lease=lease_id,
            kind=LONG_TERM if long_term else ORDINARY,
            purpose=purpose,
            expires_at=expires_at,
        )
        lease = load_lease(lease_root, lease_id)
        output = dict(lease)
        if long_term:
            output["warning"] = (
                "This is a long-term lease: the lab machine will stay "
                "powered on until it is destroyed with 'lease-destroy'. Its "
                "guests carry pxl-expiry=0 and are never swept."
            )
        print(json.dumps(output, indent=2, sort_keys=True))
    except BaseException:
        # The row must not outlive a failed begin.
        with _store(lease_root) as store:
            store.set_lease_state(lease_id, "abandoned", ended=True)
        raise


def cmd_lease_heartbeat(lab: Any, args: argparse.Namespace) -> None:
    config = lab.CONFIG
    lease_root = _lease_root(lab)
    lease = load_lease(lease_root, args.lease)
    if is_long_term(lease):
        print(json.dumps({
            "lease": args.lease,
            "kind": LONG_TERM,
            "expires_at": 0,
            "note": "long-term leases do not expire; no heartbeat needed",
        }, indent=2))
        return
    expires_at = int(time.time()) + _ttl_seconds(config, args)
    with _store(lease_root) as store:
        if not store.heartbeat(args.lease, expires_at=expires_at):
            raise LabError(f"Lease {args.lease} is not active")
    # The GC is metadata-driven: stale expiry on any registered guest would
    # reap live work, so every one of them is rewritten, every heartbeat.
    seam = _make_proxmox(config)
    for resource in lease.get("resources", []):
        if resource.get("destroyed_at"):
            continue
        kind, vmid = str(resource["kind"]), int(resource["vmid"])
        try:
            seam.set_metadata(
                kind, vmid,
                description=metadata_description(args.lease, expires_at),
            )
        except LabError as exc:
            if "does not exist" in str(exc):
                continue                   # gone guest: nothing to keep fresh
            raise
    lab.audit("lease-heartbeat", lease=args.lease, expires_at=expires_at)
    print(json.dumps({"lease": args.lease, "expires_at": expires_at}))


def cmd_lease_register(lab: Any, args: argparse.Namespace) -> None:
    lease_root = _lease_root(lab)
    lease = load_lease(lease_root, args.lease)
    owner = resource_owner_elsewhere(
        lease_root, args.lease, args.kind, int(args.vmid)
    )
    if owner:
        raise LabError(
            f"{args.kind}/{args.vmid} is already registered to live lease "
            f"{owner}. Two leases owning one guest is how a cleanup sweep "
            f"comes to delete a machine another lease is still using. "
            f"Work under {owner}, or end/abandon it first."
        )
    policy = "retain" if args.policy == "retain" else "disposable"
    register_resource(
        lease, args.kind, int(args.vmid), args.policy, args.name,
        lease_root=lease_root,
        state_root=Path(lease_root).parent,
        default_ttl=_ttl_seconds(lab.CONFIG, args),
    )
    lab.audit(
        "lease-register",
        lease=args.lease,
        kind=args.kind,
        vmid=int(args.vmid),
        policy=policy,
        name=args.name,
    )
    print(json.dumps(
        {"registered": True, "lease": args.lease, "vmid": int(args.vmid)}
    ))


def cmd_lease_list(lab: Any, args: argparse.Namespace) -> None:
    """Every active lease, and whether the machine is pinned on."""
    leases = active_leases(_lease_root(lab))
    persistent = [lease for lease in leases if is_long_term(lease)]
    print(json.dumps({
        "active": [
            {
                "id": lease["id"],
                "kind": lease.get("kind", ORDINARY),
                "purpose": lease.get("purpose"),
                "expires_at": lease.get("expires_at"),
                "guests": [
                    resource["vmid"] for resource in lease.get("resources", [])
                ],
            }
            for lease in leases
        ],
        "host_pinned_on": bool(persistent),
        "pinned_by": [lease["id"] for lease in persistent],
    }, indent=2, sort_keys=True))
