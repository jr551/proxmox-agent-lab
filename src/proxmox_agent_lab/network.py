"""Read-only list of host bridges.

``pvesh get /nodes/<node>/network --type any_bridge`` is the Proxmox
filter for Linux bridges (``vmbr*``) and Open vSwitch bridges. Asking
the node, so ``guest create`` is not stuck guessing ``vmbr0``, is an
idea from ProxmoxMCP-Plus (MIT). The argv is ours.
"""

from __future__ import annotations

import json
from typing import Any

from . import guest as guest_module

_FIELDS = ("iface", "type", "active", "address", "cidr", "bridge_ports")
_BRIDGE_TYPES = frozenset({"bridge", "OVSBridge", "any_bridge"})


def _emit(payload: dict) -> dict:
    print(json.dumps(payload, indent=2, sort_keys=True))
    return payload


def _is_bridge(item: dict) -> bool:
    kind = str(item.get("type") or "")
    iface = str(item.get("iface") or "")
    return kind in _BRIDGE_TYPES or iface.startswith("vmbr")


def cmd_bridges(lab: Any, args: Any) -> dict:
    """Host bridges a fresh qemu guest can attach to. Read-only."""
    del args
    prox = guest_module._make_proxmox(lab.CONFIG)
    bridges = []
    for item in prox.network_bridges():
        if not isinstance(item, dict) or not _is_bridge(item):
            continue
        bridges.append({key: item.get(key) for key in _FIELDS})
    bridges.sort(key=lambda row: str(row.get("iface") or ""))
    return _emit({"node": prox._node, "bridges": bridges})


def register(sub: Any, lab: Any) -> None:
    from .cli import _bind

    network = sub.add_parser("network", help="read-only view of host bridges")
    commands = network.add_subparsers(dest="network_command", required=True)
    bridges = commands.add_parser(
        "bridges", help="bridges a guest can use (vmbr and any_bridge)"
    )
    bridges.set_defaults(func=_bind(lab, cmd_bridges))
