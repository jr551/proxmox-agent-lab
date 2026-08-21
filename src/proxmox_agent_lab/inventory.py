"""The retained-guest registry, and what node tags actually prove.

Why this exists
---------------
Every guest this tool creates is tagged `codex-lab;lease-<id>`. Those tags
live on the node for ever; the lease records that explain them do not -- they
are pruned, or simply absent on a rebuilt controller. So a tag is evidence
that *some* lease created a guest, and nothing more: on a fresh controller
almost no tag resolves to a local record, and any ownership check that
resolved `tag -> lease file` would call nearly every retained guest unowned.

Tags are therefore **informational only**. Ownership comes from two places
that the controller actually keeps:

* the lease record, for the life of the lease; and
* this registry, for guests that outlive their lease on purpose.

A guest registered with `policy = retain` -- a template, a persistent gateway,
a long-term lease's machine -- is written here at register time and is never
pruned automatically. That gives the keep-forever subset a durable owner, a
purpose, and a place to record backup coverage, none of which a tag can carry.

Everything here is a pure function over the state directory, in the style of
`journal`: the caller passes the root, so tests need no patching of module
state.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

REGISTRY_VERSION = 1
LEASE_TAG = re.compile(r"\Alease-(?P<lease>[A-Za-z0-9._-]{1,120})\Z")
LAB_TAG = "codex-lab"


def registry_path(state_root: Path) -> Path:
    return Path(state_root) / "retained.json"


def load(state_root: Path) -> dict[str, Any]:
    """The registry, or an empty one. A damaged file never breaks a command."""
    path = registry_path(state_root)
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"version": REGISTRY_VERSION, "guests": {}}
    if not isinstance(data, dict) or not isinstance(data.get("guests"), dict):
        return {"version": REGISTRY_VERSION, "guests": {}}
    return data


def _save(state_root: Path, data: dict[str, Any]) -> None:
    path = registry_path(state_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic: a half-written registry would lose the record of every retained
    # guest, which is exactly the thing this file exists to prevent.
    handle = tempfile.NamedTemporaryFile(
        "w", dir=str(path.parent), prefix=".retained-", delete=False
    )
    try:
        with handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise


def key(kind: str, vmid: int) -> str:
    return f"{kind}/{int(vmid)}"


def entries(state_root: Path) -> dict[str, dict[str, Any]]:
    guests = load(state_root)["guests"]
    return {name: item for name, item in guests.items() if isinstance(item, dict)}


def record(
    state_root: Path,
    *,
    kind: str,
    vmid: int,
    lease: str,
    now: str,
    purpose: str = "",
    name: str | None = None,
) -> dict[str, Any]:
    """Note a guest that is meant to outlive its lease.

    Idempotent, and it never overwrites the first lease that claimed the
    guest: that is the provenance worth keeping. Re-registration refreshes the
    name and purpose and records the latest lease to touch it.
    """
    data = load(state_root)
    guests = data.setdefault("guests", {})
    item = guests.get(key(kind, vmid))
    if not isinstance(item, dict):
        item = {
            "kind": kind,
            "vmid": int(vmid),
            "created_by_lease": lease,
            "recorded_at": now,
            "last_backup_at": None,
        }
        guests[key(kind, vmid)] = item
    item["last_lease"] = lease
    item["updated_at"] = now
    if name:
        item["name"] = name
    if purpose:
        item["purpose"] = purpose[:240]
    data["version"] = REGISTRY_VERSION
    _save(state_root, data)
    return dict(item)


def forget(state_root: Path, kind: str, vmid: int) -> bool:
    """Drop a guest from the registry, e.g. once it is actually deleted."""
    data = load(state_root)
    guests = data.setdefault("guests", {})
    if guests.pop(key(kind, vmid), None) is None:
        return False
    _save(state_root, data)
    return True


def mark_backup(state_root: Path, kind: str, vmid: int, when: str) -> None:
    """Record a successful backup, so coverage drift is measurable."""
    data = load(state_root)
    item = data.setdefault("guests", {}).get(key(kind, vmid))
    if not isinstance(item, dict):
        return
    item["last_backup_at"] = when
    _save(state_root, data)


def lease_of_tags(tags: Any) -> str | None:
    """The lease id a guest's `tags` field claims, if any."""
    if not isinstance(tags, str):
        return None
    for tag in tags.split(";"):
        matched = LEASE_TAG.fullmatch(tag.strip())
        if matched:
            return matched.group("lease")
    return None


def is_lab_guest(tags: Any) -> bool:
    if not isinstance(tags, str):
        return False
    return LAB_TAG in {tag.strip() for tag in tags.split(";")}


def classify(
    guests: list[dict[str, Any]],
    *,
    known_leases: set[str],
    retained: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Describe each node guest's ownership, and say what is unaccounted for.

    `lease_known` is whether the tag resolves to a lease record the controller
    still holds; `retained` is whether the registry vouches for the guest.
    A guest this tool created that neither vouches for is `orphaned`: nothing
    will ever clean it up, and while it runs it blocks host power-off.
    """
    out: list[dict[str, Any]] = []
    for guest in guests:
        if not isinstance(guest, dict) or "vmid" not in guest:
            continue
        try:
            vmid = int(guest["vmid"])
        except (TypeError, ValueError):
            continue
        kind = str(guest.get("type") or "qemu")
        tags = guest.get("tags")
        lease = lease_of_tags(tags)
        registry = retained.get(key(kind, vmid))
        lease_known = bool(lease and lease in known_leases)
        described = {
            "kind": kind,
            "vmid": vmid,
            "name": guest.get("name"),
            "status": guest.get("status"),
            "template": bool(guest.get("template")),
            "lab_guest": is_lab_guest(tags),
            "lease_tag": lease,
            "lease_known": lease_known,
            "retained": registry is not None,
            "orphaned": bool(
                lease and not lease_known and registry is None
            ),
        }
        if registry is not None:
            described["retained_purpose"] = registry.get("purpose")
            described["last_backup_at"] = registry.get("last_backup_at")
        out.append(described)
    return sorted(out, key=lambda item: item["vmid"])


def orphans(described: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in described if item.get("orphaned")]
