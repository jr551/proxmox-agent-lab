"""Read-only view of the node's storage.

The old storage command also formatted disks, changed content types and
deleted unreferenced volumes. Those change the host. This module only
reports what ``pvesh get /nodes/<node>/storage`` already returns, so a
create can see free space before it fills a disk.
"""

from __future__ import annotations

import json
from typing import Any

from . import guest as guest_module

_FIELDS = ("storage", "type", "active", "enabled", "used", "avail", "total", "content")


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


def register(sub: Any, lab: Any) -> None:
    from .cli import _bind

    storage = sub.add_parser("storage", help="read-only view of node storage")
    commands = storage.add_subparsers(dest="storage_command", required=True)
    status = commands.add_parser(
        "status", help="free space and content flags for each store"
    )
    status.set_defaults(func=_bind(lab, cmd_status))
