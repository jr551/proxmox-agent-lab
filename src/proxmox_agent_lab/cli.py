#!/usr/bin/env python3
"""Guarded Proxmox lab controller.

Every mutation belongs to a lease, created resources are registered to it,
and finalising the last lease powers the machine off. Site-specific values
come from the config file; host access is over SSH keys alone.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import sys
import types
from typing import Any

from . import __version__
from . import config as config_module
from . import power as power_module
from . import journal as journal_module
from . import audit as audit_module
from . import cleanup as cleanup_module
from . import diagnostics as diagnostics_module
from . import leases as leases_module
from . import ssh as ssh_module
from .config import ConfigError
from .errors import LabError
from .audit import redact
from .leases import (
    is_long_term, lease_claims, lease_is_live, lease_requires_cleanup,
    new_expiry, parse_expiry, require_lease_resource,
)


# Importing must never fail, however broken the config is -- otherwise the
# very commands that diagnose and repair it (`init`, `doctor`) cannot run.
# A load failure is remembered and reported instead.
try:
    config_module.load()          # surfaces a broken file as an error...
    CONFIG_ERROR: str | None = None
except ConfigError as _exc:
    CONFIG_ERROR = str(_exc)
CONFIG = config_module.get()      # ...but every module shares this instance

DEFAULT_TTL_SECONDS = int(CONFIG.lease.ttl_seconds)
MCP_IDLE_SHUTDOWN_SECONDS = int(CONFIG.lease.idle_shutdown_seconds)
MIN_COLD_BOOT_TIMEOUT_SECONDS = 90
STATE_ROOT = config_module.state_dir()
LEASE_ROOT = STATE_ROOT / "leases"

# The single control channel. Feature modules reach the host through
# `lab.ssh`; constructing it never talks to anything, so `init`/`doctor`
# still work on an unconfigured install -- probe() simply reports False
# until a real [ssh] target exists.
ssh = ssh_module.SSH(str(CONFIG.ssh.target))


def _bind(lab: Any, fn: Any) -> Any:
    """Bind a ``lab`` instance to a command handler for argparse."""
    return lambda args: fn(lab, args)


# ---------------------------------------------------------------------------
# Facade: the lifecycle, audit and diagnostics implementations live in
# leases, cleanup, diagnostics, audit, journal and power. These wrappers
# keep the same names on this module so feature modules (which receive it
# as `lab`), the MCP server and the tests keep working, and so patched
# configuration values are read at call time rather than captured at
# import.


def audit(event: str, **fields: Any) -> None:
    audit_module.audit(event, **fields)

def lease_path(lease_id: str) -> Path:
    return leases_module.lease_path(LEASE_ROOT, lease_id)


def load_lease(lease_id: str, *, active: bool = True) -> dict[str, Any]:
    return leases_module.load_lease(LEASE_ROOT, lease_id, active=active)


def save_lease(lease: dict[str, Any]) -> None:
    leases_module.save_lease(LEASE_ROOT, lease)


def mcp_activity_path() -> Path:
    return leases_module.mcp_activity_path(STATE_ROOT)


def record_mcp_activity(tool_name: str) -> None:
    leases_module.record_mcp_activity(STATE_ROOT, tool_name, audit=audit)


def mcp_idle_elapsed(now: dt.datetime | None = None) -> float:
    return leases_module.mcp_idle_elapsed(STATE_ROOT, now)


def mcp_idle_shutdown_due(now: dt.datetime | None = None) -> bool:
    return leases_module.mcp_idle_shutdown_due(
        STATE_ROOT, idle_shutdown_seconds=MCP_IDLE_SHUTDOWN_SECONDS, now=now)


def idle_shutdown_due(*, reachable: bool, active_lease_count: int,
                      has_failures: bool, idle_seconds: float) -> bool:
    return leases_module.idle_shutdown_due(
        reachable=reachable,
        active_lease_count=active_lease_count,
        has_failures=has_failures,
        idle_seconds=idle_seconds,
        threshold_seconds=MCP_IDLE_SHUTDOWN_SECONDS)


def _leases_in_states(states: tuple[str, ...],
                    excluding: str | None = None) -> list[dict[str, Any]]:
    return leases_module.leases_in_states(LEASE_ROOT, states, excluding)


def active_leases(excluding: str | None = None) -> list[dict[str, Any]]:
    return leases_module.active_leases(LEASE_ROOT, excluding)


def cleanup_candidate_leases() -> list[dict[str, Any]]:
    return leases_module.cleanup_candidate_leases(LEASE_ROOT)


def all_lease_ids() -> set[str]:
    return leases_module.all_lease_ids(LEASE_ROOT)


def long_term_leases() -> list[dict[str, Any]]:
    return leases_module.long_term_leases(LEASE_ROOT)


def resource_owner_elsewhere(lease_id: str, kind: str, vmid: int, *,
                             now: dt.datetime | None = None) -> str | None:
    return leases_module.resource_owner_elsewhere(
        LEASE_ROOT, lease_id, kind, vmid, now=now)


def register_resource(lease: dict[str, Any], kind: str, vmid: int,
                      policy: str, name: str | None = None) -> None:
    leases_module.register_resource(
        lease, kind, vmid, policy, name,
        lease_root=LEASE_ROOT, state_root=STATE_ROOT,
        default_ttl=DEFAULT_TTL_SECONDS)


def ensure_on(api: Any, timeout: int | None = None) -> bool:
    return leases_module.ensure_on(_module(), api, timeout)


def node_guests(api: Any) -> list[dict[str, Any]]:
    return cleanup_module.node_guests(_module(), api)


def describe_guests(api: Any) -> list[dict[str, Any]]:
    return cleanup_module.describe_guests(_module(), api)


def orphaned_guests(api: Any) -> list[dict[str, Any]]:
    return cleanup_module.orphaned_guests(_module(), api)


def running_guest_vmids(api: Any) -> list[int]:
    return cleanup_module.running_guest_vmids(_module(), api)


def host_power_policy() -> dict[str, Any]:
    return cleanup_module.host_power_policy(_module())


def shutdown_host(api: Any = None, *, requested: bool = False) -> bool:
    return cleanup_module.shutdown_host(_module(), api, requested=requested)


def guest_status(api: Any, kind: str, vmid: int) -> str:
    return cleanup_module.guest_status(_module(), api, kind, vmid)


def stop_guest(api: Any, kind: str, vmid: int) -> None:
    cleanup_module.stop_guest(_module(), api, kind, vmid)


def delete_guest(api: Any, kind: str, vmid: int) -> None:
    cleanup_module.delete_guest(_module(), api, kind, vmid)


def guest_load(record: dict[str, Any]) -> dict[str, Any]:
    return cleanup_module.guest_load(record)


def recent_guest_activity(api: Any, kind: str, vmid: int, *,
                          within: int = 1800,
                          record: dict[str, Any] | None = None) -> Any:
    return cleanup_module.recent_guest_activity(
        _module(), api, kind, vmid, within=within, record=record)


def reclaim_orphans(api: Any, *,
                    include_active: bool = False) -> dict[str, Any]:
    return cleanup_module.reclaim_orphans(
        _module(), api, include_active=include_active)


def finalize_lease(api: Any, lease: dict[str, Any]) -> list[str]:
    return cleanup_module.finalize_lease(_module(), api, lease)


def shared_lease_resources(lease: dict[str, Any]) -> Any:
    return cleanup_module.shared_lease_resources(_module(), lease)


def describe_shared_resources(shared: Any) -> str:
    return cleanup_module.describe_shared_resources(shared)


def power_status() -> dict[str, Any]:
    """The lab host's power posture (the ``power status``/``power_status``
    report).

    Reachability over the ssh channel is the honest "is it on" signal; the
    last power-related audit event, the active lease count and the running
    guests complete the picture. Guests can only be enumerated while the
    host answers, so they are ``None`` -- unknown, not zero -- when it does
    not.
    """
    reachable = ssh.probe()
    last_power_event = None
    for row in journal_module.query_events(limit=200):
        event = str(row.get("event") or "")
        if event.startswith(("lab-power-", "lab-graceful-shutdown-")):
            last_power_event = {
                "event": event,
                "timestamp": row.get("timestamp"),
            }
            break
    return {
        "reachable": reachable,
        "powered_on": reachable,
        "last_power_event": last_power_event,
        "active_leases": len(active_leases()),
        "running_guests": running_guest_vmids(None) if reachable else None,
    }


def cmd_power_on(args: argparse.Namespace) -> None:
    leases_module.cmd_power_on(_module(), args)


def cmd_power_status(args: argparse.Namespace) -> None:
    print(json.dumps(power_status(), indent=2, sort_keys=True))


def cmd_power_shutdown(args: argparse.Namespace) -> None:
    if not args.standalone_authorized:
        raise LabError(
            "Standalone power-off is refused by default: with no lease left "
            "there is no finalizer to verify the host actually went off. "
            "Automatic power-off is [power] auto_shutdown; pass "
            "--standalone-authorized only when a person owns the host."
        )
    if not shutdown_host(None, requested=True):
        raise LabError(
            "the host did not power off: it may still be reachable, guests "
            "may still be running, or the shutdown could not be verified. "
            "See the journal for the recorded reason."
        )
    print(json.dumps({"host_powered_off": True}, indent=2, sort_keys=True))


def cmd_lease_begin(args: argparse.Namespace) -> None:
    leases_module.cmd_lease_begin(_module(), args)


def cmd_lease_heartbeat(args: argparse.Namespace) -> None:
    leases_module.cmd_lease_heartbeat(_module(), args)


def cmd_lease_register(args: argparse.Namespace) -> None:
    leases_module.cmd_lease_register(_module(), args)


def cmd_lease_end(args: argparse.Namespace) -> None:
    cleanup_module.cmd_lease_end(_module(), args)


def cmd_lease_abandon(args: argparse.Namespace) -> None:
    cleanup_module.cmd_lease_abandon(_module(), args)


def cmd_reclaim_orphans_only(args: argparse.Namespace) -> None:
    cleanup_module.cmd_reclaim_orphans_only(_module(), args)


def cmd_cleanup_expired(args: argparse.Namespace) -> None:
    cleanup_module.cmd_cleanup_expired(_module(), args)


def cmd_init(args: argparse.Namespace) -> None:
    diagnostics_module.cmd_init(_module(), args)


def cmd_doctor(args: argparse.Namespace) -> None:
    diagnostics_module.cmd_doctor(_module(), args)


def cmd_journal(args: argparse.Namespace) -> None:
    diagnostics_module.cmd_journal(_module(), args)


def cmd_status(args: argparse.Namespace) -> None:
    diagnostics_module.cmd_status(_module(), args)


INFRA_TAG = cleanup_module.INFRA_TAG


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="proxmox-lab",
        description="Proxmox skill and MCP server for AI agents to operate "
                    "VMs and containers over SSH.",
    )
    root.add_argument("--version", action="version", version=__version__)
    sub = root.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="write a starter config file")
    init.add_argument("--path")
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_init)

    doctor = sub.add_parser("doctor", help="check config, ssh access and store")
    doctor.set_defaults(func=cmd_doctor)

    journal = sub.add_parser("journal", help="read the local event journal")
    journal.add_argument("--limit", type=int, default=50)
    journal.add_argument("--lease")
    journal.add_argument("--since", help="ISO timestamp lower bound")
    journal.set_defaults(func=cmd_journal)

    status = sub.add_parser("status", help="host and lease overview")
    status.set_defaults(func=cmd_status)

    power = sub.add_parser(
        "power",
        help="host power: wake it, check it, or verify a shutdown",
    )
    power_sub = power.add_subparsers(dest="power_command", required=True)

    wake = power_sub.add_parser(
        "wake",
        help="wake without a lease (manual operations only; authorization "
             "required)",
    )
    wake.add_argument(
        "--timeout", type=int,
        help="cold-boot wait in seconds (default: 300; minimum: 90)",
    )
    wake.add_argument(
        "--standalone-authorized", action="store_true",
        help="confirm that a person, not the lease finalizer, owns shutdown",
    )
    wake.set_defaults(func=cmd_power_on)

    pstatus = power_sub.add_parser(
        "status", help="whether the host is answering and what pins it on")
    pstatus.set_defaults(func=cmd_power_status)

    poff = power_sub.add_parser(
        "shutdown",
        help="power the host off, verified by repeated probe failure "
             "(authorization required)",
    )
    poff.add_argument(
        "--standalone-authorized", action="store_true",
        help="confirm that a person, not the lease finalizer, owns shutdown",
    )
    poff.set_defaults(func=cmd_power_shutdown)

    begin = sub.add_parser("lease-begin")
    begin.add_argument("--purpose", required=True)
    begin.add_argument(
        "--long-term", action="store_true",
        help="keep these machines (and the host powered on) until destroyed",
    )
    begin.add_argument("--ttl", type=int, default=DEFAULT_TTL_SECONDS)
    begin.set_defaults(func=cmd_lease_begin)

    heartbeat = sub.add_parser("lease-heartbeat")
    heartbeat.add_argument("--lease", required=True)
    heartbeat.add_argument("--ttl", type=int, default=DEFAULT_TTL_SECONDS)
    heartbeat.set_defaults(func=cmd_lease_heartbeat)

    register = sub.add_parser("lease-register")
    register.add_argument("--lease", required=True)
    register.add_argument("--kind", choices=("qemu", "lxc"), required=True)
    register.add_argument("--vmid", type=int, required=True)
    register.add_argument("--policy", choices=("delete", "retain"), default="delete")
    register.add_argument("--name")
    register.set_defaults(func=cmd_lease_register)

    end = sub.add_parser("lease-end")
    end.add_argument("--lease", required=True)
    end.add_argument(
        "--shared-guests-authorized", action="store_true",
        help="destroy a registered guest even though another active lease "
             "also registers it. Refused by default: that lease may be "
             "mid-run, and a deleted guest does not come back",
    )
    end.set_defaults(func=cmd_lease_end)

    abandon = sub.add_parser(
        "lease-abandon",
        help="close a stopped ordinary lease without mutating guests or host",
    )
    abandon.add_argument("--lease", required=True)
    abandon.add_argument("--confirm", action="store_true")
    abandon.set_defaults(func=cmd_lease_abandon)

    listing = sub.add_parser("lease-list", help="show active leases")
    listing.set_defaults(func=_bind(_module(), leases_module.cmd_lease_list))

    destroy = sub.add_parser(
        "lease-destroy",
        help="permanently destroy a lease and its machines (long-term: the "
             "only way out)",
    )
    destroy.add_argument("--lease", required=True)
    destroy.add_argument(
        "--confirm", action="store_true",
        help="required: this deletes registered machines",
    )
    destroy.set_defaults(func=_bind(_module(), cleanup_module.cmd_lease_destroy))

    cleanup = sub.add_parser("cleanup-expired")
    cleanup.add_argument("--all", action="store_true")
    cleanup.add_argument(
        "--reclaim-orphans", action="store_true",
        help="stop (never delete) guests tagged with a lease this controller "
             "has no record of; they are invisible to normal cleanup and a "
             "running one blocks host power-off. Requires "
             "--host-change-authorized",
    )
    cleanup.add_argument(
        "--orphans-only", action="store_true",
        help="reclaim orphaned guests and nothing else: no lease is "
             "finalized and the host is left on. Requires "
             "--host-change-authorized",
    )
    cleanup.add_argument(
        "--include-active", action="store_true",
        help="also stop an orphaned guest that was touched in the last 30 "
             "minutes. Skipped by default: another controller may be driving "
             "guests over the same ssh channel, and its lease records are "
             "not here",
    )
    cleanup.add_argument("--host-change-authorized", action="store_true",
                         help="required by --reclaim-orphans")
    cleanup.set_defaults(func=cmd_cleanup_expired)

    from . import console
    from . import gc
    from . import guest
    from . import mcp
    from . import memflow
    from . import netcap
    from . import network
    from . import storage
    from . import transfer

    console.register(sub, _module())
    gc.register(sub, _module())
    guest.register(sub, _module())
    mcp.register(sub, _module())
    memflow.register(sub, _module())
    netcap.register(sub, _module())
    network.register(sub, _module())
    storage.register(sub, _module())
    transfer.register(sub, _module())
    return root


def _module() -> Any:
    """This module as an object, for helper modules that call back into it.

    When imported normally ``__name__`` is ``proxmox_agent_lab.cli`` and the
    module is already in ``sys.modules``. The fallback handles path-loaded
    execution (e.g. ``importlib.util.spec_from_file_location``) where the
    loader does not register the module: it builds a minimal
    ``proxmox_lab`` compatibility shim — historic top-level name for the same
    code now shipped as ``proxmox_agent_lab`` (see 03db912) — and registers
    it in ``sys.modules`` so ``import proxmox_lab`` resolves.
    """
    module = sys.modules.get(__name__)
    if module is None:  # loaded by path without sys.modules registration
        module = types.ModuleType("proxmox_lab")
        module.__dict__.update(globals())
        sys.modules["proxmox_lab"] = module
        sys.modules[__name__] = module
    return module


def _expected_errors() -> tuple[type[BaseException], ...]:
    """Every error the package raises on purpose.

    Collected once, from the modules themselves, so adding a new subsystem
    cannot reintroduce a raw traceback for a routine failure like an
    unreachable host.
    """
    errors: list[type[BaseException]] = [
        LabError, ConfigError, power_module.PowerError, ValueError,
        json.JSONDecodeError,
    ]
    for name in (
        "console", "guest", "mcp",
    ):
        try:
            module = __import__(f"{__package__}.{name}", fromlist=[name])
        except ImportError:  # pragma: no cover
            continue
        for attribute in vars(module).values():
            if (isinstance(attribute, type)
                    and issubclass(attribute, Exception)
                    and attribute.__module__ == module.__name__):
                errors.append(attribute)
    return tuple(dict.fromkeys(errors))


_EXPECTED_ERRORS = _expected_errors()


def main() -> int:
    try:
        args = parser().parse_args()
        args.func(args)
        return 0
    except _EXPECTED_ERRORS as exc:
        # Anything the tool raises on purpose is a message, not a traceback.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # python3 -m proxmox_agent_lab.cli
    sys.exit(main())
