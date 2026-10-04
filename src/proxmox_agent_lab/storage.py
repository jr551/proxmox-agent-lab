"""Read-only view of the node's storage.

The old storage command also formatted disks, changed content types and
deleted unreferenced volumes. Those change the host. This module only
reports what ``pvesh get`` already returns: free space, and the ISO and
template volids a create or ``guest media`` call can name.

Listing those volids (instead of guessing a path) is an idea from
ProxmoxMCP-Plus (MIT). The calls are ``pvesh get`` on this seam.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .errors import LabError
from . import guest as guest_module
from . import proxmox as proxmox_module

_FIELDS = ("storage", "type", "active", "enabled", "used", "avail", "total", "content")
_MEDIA = frozenset({"iso", "vztmpl"})
_STORAGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def _emit(payload: dict) -> dict:
    print(json.dumps(payload, indent=2, sort_keys=True))
    return payload


def cmd_status(lab: Any, args: Any) -> dict:
    """Free space and content flags for each store on the node. Read-only."""
    del args
    prox = guest_module._make_proxmox(lab.CONFIG)
    stores = []
    for item in prox.storage_status():
        if not isinstance(item, dict):
            continue
        stores.append({key: item.get(key) for key in _FIELDS})
    return _emit({"node": prox._node, "storage": stores})


def _content_kinds(raw: Any) -> set[str]:
    if isinstance(raw, str):
        return {part.strip() for part in raw.split(",") if part.strip()}
    if isinstance(raw, list):
        return {str(part).strip() for part in raw if str(part).strip()}
    return set()


def cmd_content(lab: Any, args: Any) -> dict:
    """ISO and template volids. Read-only.

    ``storage status`` stays the free-space view. This prints the ``volid``
    to pass to ``guest create --iso``, ``--ostemplate``, and ``guest media``.
    """
    wanted = getattr(args, "storage", None) or None
    if wanted is not None and _STORAGE_ID.fullmatch(str(wanted)) is None:
        raise LabError("--storage must be a storage name like local or local-lvm")
    prox = guest_module._make_proxmox(lab.CONFIG)
    stores = [
        item for item in prox.storage_status() if isinstance(item, dict)
    ]
    names = {
        str(item.get("storage"))
        for item in stores
        if isinstance(item.get("storage"), str)
    }
    if wanted is not None and wanted not in names:
        raise LabError(
            f"storage {wanted!r} is not on this node. "
            "'proxmox-lab storage status' lists the stores."
        )
    chosen: list[tuple[str, set[str]]] = []
    for item in stores:
        name = item.get("storage")
        if not isinstance(name, str):
            continue
        if wanted is not None and name != wanted:
            continue
        kinds = _content_kinds(item.get("content")) & _MEDIA
        if wanted is not None and not kinds:
            kinds = set(_MEDIA)
        if kinds:
            chosen.append((name, kinds))
    volumes = []
    for name, kinds in chosen:
        for kind in sorted(kinds):
            try:
                items = prox.storage_content(name, kind)
            except proxmox_module.ProxmoxError:
                if wanted is not None:
                    raise
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                if str(item.get("content") or kind) not in _MEDIA:
                    continue
                volumes.append({
                    "volid": item.get("volid"),
                    "content": item.get("content") or kind,
                    "storage": name,
                    "format": item.get("format"),
                    "size": item.get("size"),
                })
    volumes.sort(key=lambda row: str(row.get("volid") or ""))
    return _emit({"node": prox._node, "volumes": volumes})


def register(sub: Any, lab: Any) -> None:
    from .cli import _bind

    storage = sub.add_parser("storage", help="read-only view of node storage")
    commands = storage.add_subparsers(dest="storage_command", required=True)
    status = commands.add_parser(
        "status", help="free space and content flags for each store"
    )
    status.set_defaults(func=_bind(lab, cmd_status))
    content = commands.add_parser(
        "content",
        help="ISO and template volids for guest create and guest media",
    )
    content.add_argument(
        "--storage",
        help="one store (default: every store that holds iso or vztmpl)",
    )
    content.set_defaults(func=_bind(lab, cmd_content))
