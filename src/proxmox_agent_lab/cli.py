#!/usr/bin/env python3
"""Guarded Proxmox lab controller.

Every mutation belongs to a lease, created resources are registered to it,
and finalising the last lease powers the machine off. Site-specific values
come from the config file; secrets come from the configured secret
backend (see secrets_store).
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import io
import json
from pathlib import Path
import secrets
import socket
import uuid
import ssl
import sys
import time
import types
from typing import Any
from urllib import error, parse, request


from . import __version__
from . import config as config_module
from . import inventory as inventory_module
from . import power as power_module
from . import journal as journal_module
from . import mariadb as mariadb_module
from . import secrets_store
from . import audit as audit_module
from . import cleanup as cleanup_module
from . import diagnostics as diagnostics_module
from . import leases as leases_module
from . import state as state_module
from . import updates as updates_module
from .config import ConfigError
from .errors import LabError
from .state import iso_now, json_dump, utc_now
from .audit import redact
from .leases import (
    is_long_term, lease_claims, lease_is_live, lease_requires_cleanup,
    new_expiry, parse_expiry, require_lease_resource,
)

# Locking lives in state.py; this stays an alias so `lab.fcntl` still
# answers the platform question (None on Windows).
fcntl = state_module.fcntl


# Importing must never fail, however broken the config is -- otherwise the
# very commands that diagnose and repair it (`init`, `doctor`) cannot run.
# A load failure is remembered and reported instead.
try:
    config_module.load()          # surfaces a broken file as an error...
    CONFIG_ERROR: str | None = None
except ConfigError as _exc:
    CONFIG_ERROR = str(_exc)
CONFIG = config_module.get()      # ...but every module shares this instance

# Site values come from the config file. They stay module-level constants so
# the rest of the package can keep referring to `lab.NODE` and friends.
HOST = CONFIG.proxmox.host
PORT = int(CONFIG.proxmox.port)
NODE = CONFIG.proxmox.node
API_ROOT = f"https://{'[' + HOST + ']' if ':' in HOST else HOST}:{PORT}/api2/json"
TOKEN_USER = CONFIG.proxmox.token_user
TOKEN_NAME = CONFIG.proxmox.token_name
VERIFY_TLS = bool(CONFIG.proxmox.verify_tls)
DEFAULT_TTL_SECONDS = int(CONFIG.lease.default_ttl_seconds)
MCP_IDLE_SHUTDOWN_SECONDS = int(CONFIG.lease.idle_shutdown_seconds)
MIN_COLD_BOOT_TIMEOUT_SECONDS = 90
STATE_ROOT = config_module.state_dir()
LEASE_ROOT = STATE_ROOT / "leases"
LOCK_PATH = STATE_ROOT / "controller.lock"
# The journal lives with the rest of the runtime state, never inside the
# installed package -- site-packages is not writable, and an operator's audit
# trail is not part of the software.
JOURNAL_ROOT = Path(CONFIG.audit.get("journal_dir") or (STATE_ROOT / "journal"))
UPLOAD_STORAGES = tuple(CONFIG.storage.upload_storages)
# Big images belong on the bulk store, not on the hypervisor's root filesystem.
# Falls back to whatever is allowed if bulk is not one of the upload targets.
DEFAULT_UPLOAD_STORAGE = (
    str(CONFIG.storage.bulk_storage)
    if str(CONFIG.storage.bulk_storage) in UPLOAD_STORAGES
    else (UPLOAD_STORAGES[0] if UPLOAD_STORAGES else "local")
)
















def _bind(lab: Any, fn: Any) -> Any:
    """Bind a ``lab`` instance to a command handler for argparse."""
    return lambda args: fn(lab, args)
































def _audit_through_boot(event: str, **fields: Any) -> None:
    """audit(), for the moment right after the lab host wakes.

    The ledger runs on that same host, so it is routinely not answering yet
    when the Proxmox API already is. `audit` never raises -- it spools -- so
    this is simply audit() with a name that says why the call site cares.
    """
    audit(event, **fields)


































































# Infrastructure this tool runs on the host itself -- currently the audit
# ledger. It is onboot and outlives every lease on purpose, so counting it as
# an untracked guest would mean the host could never power itself off again,
# which is the whole point of the machine.




















# How recently a guest must have been touched to count as in use. Tasks that
# only ever mean "something stopped this guest" are excluded, or our own stop
# would make every later run think the guest is busy.
# Work happening *inside* a guest produces no Proxmox task and does not reset
# its uptime, so a long build in an unmanaged container looks idle to both of
# the other signals. This floor is set where a guest is unmistakably doing
# something: an idle Debian guest on the lab node sits near 1% and a genuinely
# idle container near 0.005%, so 10% is not a judgement call.































# ---------------------------------------------------------------------------
# Facade: the lifecycle, state, audit and diagnostics implementations
# now live in leases, cleanup, diagnostics, audit, state and updates.
# These wrappers keep the same names on this module so feature modules
# (which receive it as `lab`) and the tests keep working, and so patched
# configuration values are read at call time rather than captured at import.


def controller_lock() -> Any:
    return state_module.controller_lock(STATE_ROOT, LOCK_PATH)


def sweep_lock(name: str) -> Any:
    return state_module.sweep_lock(STATE_ROOT, name)


def _lock_file(handle: Any) -> None:
    state_module._lock_file(handle)


def _try_lock_file(handle: Any) -> bool:
    return state_module._try_lock_file(handle)


def audit(event: str, **fields: Any) -> None:
    audit_module.audit(event, **fields)


def ledger() -> Any:
    return audit_module.ledger(CONFIG)


def _controller_id() -> str:
    return audit_module.controller_id(CONFIG)


def _auto_migrate_once() -> None:
    audit_module.auto_migrate_once(CONFIG, JOURNAL_ROOT)


def check_for_updates(*, now: float | None = None) -> dict[str, Any]:
    return updates_module.check_for_updates(STATE_ROOT, __version__, now=now)


def update_notice() -> None:
    updates_module.update_notice(STATE_ROOT, __version__)


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


def idle_shutdown_due(*, reachable: bool, active_lease_count: int,
                      has_failures: bool, idle_seconds: float) -> bool:
    return leases_module.idle_shutdown_due(
        reachable=reachable, active_lease_count=active_lease_count,
        has_failures=has_failures, idle_seconds=idle_seconds,
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


def host_power_policy() -> str:
    return cleanup_module.host_power_policy(_module())


def shutdown_host(api: Any) -> dict[str, Any]:
    return cleanup_module.shutdown_host(_module(), api)


def guest_status(api: Any, kind: str, vmid: int) -> str:
    return cleanup_module.guest_status(_module(), api, kind, vmid)


def stop_guest(api: Any, kind: str, vmid: int) -> None:
    cleanup_module.stop_guest(_module(), api, kind, vmid)


def delete_guest(api: Any, kind: str, vmid: int) -> None:
    cleanup_module.delete_guest(_module(), api, kind, vmid)


def _delete_guest(api: Any, kind: str, vmid: int, *,
                  destroy_unreferenced_disks: bool) -> None:
    cleanup_module._delete_guest(
        _module(), api, kind, vmid,
        destroy_unreferenced_disks=destroy_unreferenced_disks)


def _forget_retained(kind: str, vmid: int) -> None:
    cleanup_module._forget_retained(_module(), kind, vmid)


def _guest_is_gone(error: Exception) -> bool:
    return cleanup_module._guest_is_gone(_module(), error)


def _storage_io_error(error: Exception) -> bool:
    return cleanup_module._storage_io_error(_module(), error)


def _is_lab_infrastructure(resource: dict[str, Any]) -> bool:
    return cleanup_module._is_lab_infrastructure(resource)


def guest_load(record: dict[str, Any]) -> float | None:
    return cleanup_module.guest_load(record)


def recent_guest_activity(api: Any, kind: str, vmid: int, *,
                          within: int = 1800,
                          record: dict[str, Any] | None = None) -> bool:
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


def retained_backup_coverage() -> dict[str, Any]:
    return diagnostics_module.retained_backup_coverage(_module())


def host_update_report() -> dict[str, Any]:
    return diagnostics_module.host_update_report(_module())


def _provision_ledger(args: argparse.Namespace) -> dict[str, Any]:
    return diagnostics_module._provision_ledger(_module(), args)


def _seed_shared_secrets(settings: Any) -> list[str]:
    return diagnostics_module._seed_shared_secrets(_module(), settings)


def _guard_install_block(hostguard_module: Any) -> str:
    return diagnostics_module._guard_install_block(hostguard_module)


def cmd_power_on(args: argparse.Namespace) -> None:
    leases_module.cmd_power_on(_module(), args)


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


def cmd_secrets(args: argparse.Namespace) -> None:
    diagnostics_module.cmd_secrets(_module(), args)


def cmd_doctor(args: argparse.Namespace) -> None:
    diagnostics_module.cmd_doctor(_module(), args)


def cmd_journal(args: argparse.Namespace) -> None:
    diagnostics_module.cmd_journal(_module(), args)


def cmd_status(args: argparse.Namespace) -> None:
    diagnostics_module.cmd_status(_module(), args)


SENSITIVE_KEY = audit_module.SENSITIVE_KEY
UPDATE_CHECK_URL = updates_module.UPDATE_CHECK_URL
UPDATE_CHECK_INTERVAL_SECONDS = updates_module.UPDATE_CHECK_INTERVAL_SECONDS
INFRA_TAG = cleanup_module.INFRA_TAG

def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="proxmox-lab",
        description="Lease-managed, fail-closed control of a Proxmox home lab "
                    "that powers itself on and off.",
    )
    root.add_argument("--version", action="version", version=__version__)
    sub = root.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="write a starter config file")
    init.add_argument("--path")
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_init)

    doctor = sub.add_parser("doctor", help="check config, secrets and access")
    doctor.add_argument(
        "--host-checks", action="store_true",
        help="also report the node's pending package updates and whether it "
             "needs a reboot (advisory; needs the opt-in [memflow] host SSH "
             "channel and adds a few seconds)",
    )
    doctor.set_defaults(func=cmd_doctor)

    store = sub.add_parser("secrets", help="store and inspect secrets")
    store_sub = store.add_subparsers(dest="secrets_command", required=True)
    store_sub.add_parser("list", help="which secrets are stored").set_defaults(
        func=cmd_secrets
    )
    setter = store_sub.add_parser("set", help="store one secret")
    setter.add_argument("name")
    setter.add_argument("--stdin", action="store_true",
                        help="read the value from stdin instead of prompting")
    setter.add_argument("--allow-unknown", action="store_true")
    setter.set_defaults(func=cmd_secrets)

    status = sub.add_parser("status", help="host and lease overview")
    status.set_defaults(func=cmd_status)

    ledger = sub.add_parser("journal", help="read the audit ledger")
    ledger.add_argument("--limit", type=int, default=50)
    ledger.add_argument("--lease")
    ledger.add_argument("--event", help="exact name, or a * wildcard")
    ledger.add_argument("--since", help="ISO timestamp lower bound")
    ledger.add_argument("--summary", action="store_true")
    ledger.add_argument("--controller", help="only this controller's events")
    ledger.add_argument(
        "--flush-spool",
        action="store_true",
        help="upload audit events spooled locally while the ledger was down",
    )
    ledger.add_argument(
        "--migrate",
        action="store_true",
        help="carry this controller's pre-MariaDB ledger into the shared one "
             "(runs automatically on upgrade; safe to re-run)",
    )
    ledger.add_argument(
        "--migrations",
        action="store_true",
        help="which controllers have already migrated their old ledger",
    )
    ledger.add_argument(
        "--host-setup",
        action="store_true",
        help="provision MariaDB on the Proxmox host (host change)",
    )
    ledger.add_argument("--host-change-authorized", action="store_true")
    ledger.add_argument("--ctid", type=int, help="container ID for the ledger")
    ledger.add_argument("--storage", help="storage for the ledger container")
    ledger.add_argument("--bridge", default="vmbr0")
    ledger.add_argument("--timeout", type=int, default=1800)
    ledger.set_defaults(func=cmd_journal)

    power = sub.add_parser(
        "power-on",
        help="wake without a lease (manual operations only; authorization required)",
    )
    power.add_argument(
        "--timeout", type=int,
        help="cold-boot wait (default: power.boot_timeout_seconds; minimum: 90)",
    )
    power.add_argument(
        "--standalone-authorized", action="store_true",
        help="confirm that a person, not the lease finalizer, owns shutdown",
    )
    power.set_defaults(func=cmd_power_on)

    begin = sub.add_parser("lease-begin")
    begin.add_argument("--purpose", required=True)
    begin.add_argument(
        "--long-term", action="store_true",
        help="keep these machines (and the host powered on) until destroyed",
    )
    begin.add_argument("--ttl", type=int, default=DEFAULT_TTL_SECONDS)
    begin.add_argument(
        "--timeout", type=int,
        help="cold-boot wait (default: power.boot_timeout_seconds; minimum: 90)",
    )
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
    register.add_argument("--allow-existing", action="store_true")
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
    cleanup.add_argument("--no-backup", action="store_true",
                         help="skip the long-term backup sweep")
    cleanup.add_argument(
        "--reclaim-orphans", action="store_true",
        help="stop (never delete) guests tagged with a lease this controller "
             "has no record of; they are invisible to normal cleanup and a "
             "running one blocks host power-off. Requires "
             "--host-change-authorized",
    )
    cleanup.add_argument(
        "--orphans-only", action="store_true",
        help="reclaim orphaned guests and nothing else: no lease is finalized, "
             "no backup runs, and the host is left on. Requires "
             "--host-change-authorized",
    )
    cleanup.add_argument(
        "--include-active", action="store_true",
        help="also stop an orphaned guest that was touched in the last 30 "
             "minutes. Skipped by default: another controller drives guests "
             "through the same token, and its lease records are not here",
    )
    cleanup.add_argument("--host-change-authorized", action="store_true",
                         help="required by --reclaim-orphans")
    cleanup.set_defaults(func=cmd_cleanup_expired)

    from . import console
    from . import connection
    from . import crash
    from . import disk
    from . import guest
    from . import hostinfo
    from . import ioworkload
    from . import isoinspect
    from . import oci
    from . import onboarding
    from . import recipes
    from . import storage
    from . import transfer
    from . import usb
    from . import virtio

    console.register(sub, _module())
    connection.register(sub, _module())
    crash.register(sub, _module())
    disk.register(sub, _module())
    guest.register(sub, _module())
    hostinfo.register(sub, _module())
    ioworkload.register(sub, _module())
    isoinspect.register(sub, _module())
    oci.register(sub, _module())
    onboarding.register(sub, _module())
    recipes.register(sub, _module())
    storage.register(sub, _module())
    transfer.register(sub, _module())
    usb.register(sub, _module())
    virtio.register(sub, _module())
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
    cannot reintroduce a raw traceback for a routine failure like a missing
    secret or an unreachable worker.
    """
    errors: list[type[BaseException]] = [
        LabError, ConfigError, secrets_store.SecretError,
        mariadb_module.MariaDBError, power_module.PowerError, ValueError,
        json.JSONDecodeError,
    ]
    for name in (
        "console", "guest", "s3",
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
        from .host_policy import check_command
        check_command(CONFIG, args.command)
        update_notice()
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
