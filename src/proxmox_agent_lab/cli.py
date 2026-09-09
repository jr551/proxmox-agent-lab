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
import os
from pathlib import Path
import re
import secrets
import socket
import uuid
import ssl
import subprocess
import sys
import tempfile
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
from . import api as api_module
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
SAFE_WRITE_PREFIXES = (
    f"/nodes/{NODE}/qemu",
    f"/nodes/{NODE}/lxc",
    f"/nodes/{NODE}/tasks",
    f"/nodes/{NODE}/status",
)
# The subset of the safe write surface that addresses an individual guest, and
# therefore must resolve to a (kind, vmid) the lease owns before it is sent.
GUEST_PATH_PREFIXES = (
    f"/nodes/{NODE}/qemu/",
    f"/nodes/{NODE}/lxc/",
)
UPLOAD_STORAGES = tuple(CONFIG.storage.upload_storages)
# Big images belong on the bulk store, not on the hypervisor's root filesystem.
# Falls back to whatever is allowed if bulk is not one of the upload targets.
DEFAULT_UPLOAD_STORAGE = (
    str(CONFIG.storage.bulk_storage)
    if str(CONFIG.storage.bulk_storage) in UPLOAD_STORAGES
    else (UPLOAD_STORAGES[0] if UPLOAD_STORAGES else "local")
)
HOST_CHANGE_MARKERS = (
    "/access",
    "/storage",
    "/cluster",
    "/network",
    "/sdn",
    "/firewall",
    "/disks",
    "/hardware",
    "/ceph",
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




















def parse_data(values: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for item in values:
        if "=" not in item:
            raise LabError(f"Expected key=value data, got: {item}")
        key, value = item.split("=", 1)
        if not key:
            raise LabError("Data key may not be empty")
        result[key] = value
    return result


def path_resource(path: str) -> tuple[str, int] | None:
    match = re.match(rf"^/nodes/{re.escape(NODE)}/(qemu|lxc)/(\d+)(?:/|$)", path)
    if not match:
        return None
    return match.group(1), int(match.group(2))




def _boot_order_devices(value: str) -> list[str]:
    """Normalized device list of a PVE `boot` value.

    PVE documents ``boot`` as ``[order=]dev;dev`` — the ``order=`` prefix is
    optional, so a bare ``boot=ide2;ide0`` is valid and must parse the same.
    """
    order = value.strip()
    if order.startswith("order="):
        order = order[len("order="):]
    return [
        device.strip().lower()
        for device in order.split(";")
        if device.strip()
    ]


DISK_CONFIG_KEY = re.compile(
    r"\A(?:scsi|virtio|ide|sata|efidisk|tpmstate|rootfs|mp|unused)\d*\Z"
)


def slow_storage_disks(data: dict[str, Any]) -> list[str]:
    """Disk specs in `data` that would place a guest disk on bulk storage.

    An ISO *mounted* from the bulk store is exactly what the docs recommend,
    so `media=cdrom` is excluded. A guest's own disk there is a different
    thing: the lab's USB directory store measured about 25 MB/s sequential
    write, which is slow enough that an I/O comparison run on it measures the
    cable rather than the guest.
    """
    bulk = str(CONFIG.storage.bulk_storage or "")
    if not bulk:
        return []
    found: list[str] = []
    for key, value in data.items():
        if not DISK_CONFIG_KEY.fullmatch(str(key)) or not isinstance(value, str):
            continue
        if "media=cdrom" in value:
            continue
        if value.split(":", 1)[0].strip() == bulk:
            found.append(f"{key}={value}")
    return sorted(found)


def cmd_api(args: argparse.Namespace) -> None:
    api = ProxmoxAPI()
    method = args.method.upper()
    data = parse_data(args.data)
    if args.password_stdin:
        key = args.password_key
        if method == "GET" or key in data:
            raise LabError(
                f"--password-stdin is only valid for a write without {key}=data"
            )
        password = sys.stdin.readline().rstrip("\r\n")
        if not password:
            # Deliberately stricter than the guest-console paths. There an
            # empty password is a fact about a guest that already has none;
            # here it would be *written* into a Proxmox object, creating a
            # blank credential nobody asked for. `guest run --password-stdin`
            # is the command that accepts an empty console password.
            raise LabError(
                f"--password-stdin received an empty password for {key}. A "
                "write would store a blank credential; to log into a guest "
                "that has no password, use 'guest run --password-stdin'."
            )
        data[key] = password
    write = method != "GET"
    lease: dict[str, Any] | None = None
    if write:
        if not args.lease:
            raise LabError("Every Proxmox write requires --lease")
        lease = load_lease(args.lease)
        if any(marker in args.path for marker in HOST_CHANGE_MARKERS):
            if not args.host_change_authorized:
                raise LabError(
                    "Host-level change refused without --host-change-authorized"
                )
        if not args.path.startswith(SAFE_WRITE_PREFIXES) and (
            not args.host_change_authorized
        ):
            raise LabError(f"Write path is outside the leased guest surface: {args.path}")
        resource = path_resource(args.path)
        create_match = re.fullmatch(
            rf"/nodes/{re.escape(NODE)}/(qemu|lxc)/?", args.path
        )
        if resource:
            require_lease_resource(lease, *resource)
        elif not create_match and args.path.startswith(GUEST_PATH_PREFIXES):
            # A guest path the resource regex cannot read is not a path whose
            # ownership can be checked. `/nodes/N/qemu//9246/sendkey` reaches
            # the same guest but parses as no guest at all, so accepting it
            # would mutate a guest with the ownership check skipped.
            raise LabError(
                f"Write path names no readable guest: {args.path}. Use "
                f"/nodes/{NODE}/<qemu|lxc>/<vmid>/... so the lease ownership "
                "check can run."
            )
        if method == "POST" and create_match:
            if "vmid" not in data:
                raise LabError("Guest creation requires an explicit vmid")
            vmid = int(data["vmid"])
            if vmid in lease["initial_vmids"]:
                raise LabError(f"VMID {vmid} existed before this lease")
            lease_tag = "lease-" + args.lease
            tags = [x for x in data.get("tags", "").split(";") if x]
            for tag in ("codex-lab", lease_tag):
                if tag not in tags:
                    tags.append(tag)
            data["tags"] = ";".join(tags)
            data.setdefault("onboot", "0")
    slow_disks: list[str] = []
    if write:
        slow_disks = slow_storage_disks(data)
        if slow_disks and not args.slow_storage_accepted:
            print(
                f"warning: {', '.join(slow_disks)} puts a guest disk on "
                f"'{CONFIG.storage.bulk_storage}', the configured bulk store. "
                "It is the right home for ISOs and cold images, not for a "
                "running or benchmarked guest -- 'storage status' reports "
                "class fast|bulk. Pass --slow-storage-accepted to silence "
                "this.",
                file=sys.stderr,
            )
        # The intent is durable before the external mutation. A failed ledger
        # must block the request rather than make a completed write look failed.
        audit(
            "proxmox-api-write-intent",
            lease=args.lease,
            method=method,
            path=args.path,
            data=data,
        )
    result = api.call(method, args.path, data)
    # Register the created guest BEFORE waiting on its task: if the wait
    # times out or errors, the guest already exists and must belong to this
    # lease, or lease-end leaves it behind as an orphan (audit 2026-08-24).
    registered_early = False
    if write and lease and method == "POST":
        create_match_early = re.fullmatch(
            rf"/nodes/{re.escape(NODE)}/(qemu|lxc)/?", args.path
        )
        if create_match_early and str(data.get("vmid", "")).isdigit():
            kind_created = create_match_early.group(1)
            policy = "retain" if is_long_term(lease) else args.policy
            with controller_lock():
                fresh = load_lease(args.lease)
                register_resource(
                    fresh, kind_created, int(data["vmid"]), policy,
                    data.get("name") or data.get("hostname"),
                )
            registered_early = True
    task_status = None
    if args.wait_task and isinstance(result, str) and result.startswith("UPID:"):
        task_status = wait_task(api, result, timeout=args.task_timeout)
    if write and lease and method == "POST" and registered_early:
        create_match = re.fullmatch(
            rf"/nodes/{re.escape(NODE)}/(qemu|lxc)/?", args.path
        )
        kind_created = create_match.group(1)
        created_vmid = int(data["vmid"])
        if is_long_term(lease):
            from . import longterm
            try:
                longterm.set_protection(
                    _module(), api, kind_created, created_vmid, True
                )
            except LabError as exc:
                print(f"warning: could not protect {created_vmid}: {exc}",
                      file=sys.stderr)
    report: dict[str, Any] = {"data": result, "task_status": task_status}
    try:
        audit(
            "proxmox-api-write",
            lease=args.lease,
            method=method,
            path=args.path,
            data=data,
            result=result,
            task_status=task_status,
        )
    except (LabError, OSError, journal_module.sqlite3.Error) as exc:
        report["operation_succeeded"] = True
        report["audit_recording_failed"] = str(exc)
    if write and method == "PUT" and "boot" in data:
        config_match = re.fullmatch(
            rf"/nodes/{re.escape(NODE)}/qemu/(\d+)/config", args.path
        )
        if config_match:
            vmid = config_match.group(1)
            requested = _boot_order_devices(data["boot"])
            try:
                persisted = _boot_order_devices(
                    api.call("GET", f"/nodes/{NODE}/qemu/{vmid}/config").get(
                        "boot", ""
                    )
                )
            except LabError:
                persisted = None
            if persisted is not None and requested and persisted != requested:
                persisted_text = ";".join(persisted) or "(none)"
                print(
                    f"warning: PVE persisted boot order '{persisted_text}' "
                    f"instead of requested '{';'.join(requested)}' — set ide2/disk "
                    "attach and boot order in separate calls",
                    file=sys.stderr,
                )
    if write:
        print(json.dumps(redact(report), indent=2, sort_keys=True))
        return
    print(
        json.dumps(
            redact({"data": result, "task_status": task_status}),
            indent=2,
            sort_keys=True,
        )
    )


def upload_curl_argv(
    config_path: str, source: Path, content: str, storage: str
) -> list[str]:
    """The curl argv for one storage upload.

    Certificate policy comes from the same [proxmox] verify_tls switch the API
    client uses. An operator who has put a trusted certificate on the node and
    turned verification on must not get an unverified upload channel, or a
    man-in-the-middle could swap the ISO while every REST call stays safe.
    The token is only ever in the 0600 curl config file, never in argv.
    """
    argv = ["curl", "--config", config_path]
    if not VERIFY_TLS:
        argv.append("--insecure")
    elif CONFIG.proxmox.get("ca_file"):
        argv.extend(["--cacert", str(CONFIG.proxmox.ca_file)])
    return [
        *argv,
        "--request", "POST",
        "--form", f"content={content}",
        "--form", f"filename=@{source}",
        f"{API_ROOT}/nodes/{NODE}/storage/{storage}/upload",
    ]


def cmd_upload(args: argparse.Namespace) -> None:
    if args.storage not in UPLOAD_STORAGES:
        raise LabError(
            f"Storage {args.storage!r} is not allowlisted for upload; "
            f"choose one of {', '.join(sorted(UPLOAD_STORAGES))}"
        )
    source = Path(args.file).expanduser().resolve()
    if not source.is_file():
        raise LabError(f"Upload source is not a regular file: {source}")
    lease = load_lease(args.lease)
    token = keychain_secret()
    config_text = (
        "silent\n"
        "show-error\n"
        "fail-with-body\n"
        f'header = "Authorization: PVEAPIToken={TOKEN_USER}!{TOKEN_NAME}={token}"\n'
    )
    with tempfile.NamedTemporaryFile(
        mode="w", prefix="proxmox-upload-", delete=False
    ) as config:
        os.chmod(config.name, 0o600)
        config.write(config_text)
        config.flush()
    try:
        result = subprocess.run(
            upload_curl_argv(config.name, source, args.content, args.storage),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=args.timeout,
            check=False,
        )
    finally:
        Path(config.name).unlink(missing_ok=True)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[:1000]
        raise LabError(f"Proxmox upload failed: {detail}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise LabError("Proxmox upload returned invalid JSON") from exc
    upid = payload.get("data")
    if not upid:
        raise LabError(f"Proxmox upload did not return a task ID: {payload}")
    status = wait_task(ProxmoxAPI(), upid, timeout=args.task_timeout)
    audit(
        "proxmox-storage-upload",
        lease=lease["id"],
        storage=args.storage,
        content=args.content,
        filename=source.name,
        size=source.stat().st_size,
        task_id=upid,
        status=status,
    )
    print(
        json.dumps(
            {"data": upid, "filename": source.name, "status": status},
            indent=2,
            sort_keys=True,
        )
    )












# How recently a guest must have been touched to count as in use. Tasks that
# only ever mean "something stopped this guest" are excluded, or our own stop
# would make every later run think the guest is busy.
# Work happening *inside* a guest produces no Proxmox task and does not reset
# its uptime, so a long build in an unmanaged container looks idle to both of
# the other signals. This floor is set where a guest is unmistakably doing
# something: an idle Debian guest on the lab node sits near 1% and a genuinely
# idle container near 0.005%, so 10% is not a judgement call.































# ---------------------------------------------------------------------------
# Facade: the lifecycle, state, audit, API and diagnostics implementations
# now live in leases, cleanup, diagnostics, audit, api, state and updates.
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


def keychain_secret() -> str:
    return api_module.token_secret(CONFIG, host=HOST, node=NODE)


class ProxmoxAPI(api_module.ProxmoxAPI):
    """The API client bound to this process's configuration."""

    def __init__(self) -> None:
        # The lambda, not keychain_secret itself: resolving the global at call
        # time is what lets tests patch LAB.keychain_secret after the client
        # is constructed.
        super().__init__(
            config=CONFIG, api_root=API_ROOT, token_user=TOKEN_USER,
            token_name=TOKEN_NAME, token_secret=lambda: keychain_secret(),
        )


def wait_task(api: ProxmoxAPI, upid: str,
              timeout: int = 180) -> dict[str, Any]:
    return api_module.wait_task(api, NODE, upid, timeout)


def audit(event: str, **fields: Any) -> None:
    audit_module.audit(CONFIG, JOURNAL_ROOT, event, **fields)


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
    return leases_module._leases_in_states(LEASE_ROOT, states, excluding)


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


def ensure_on(api: ProxmoxAPI, timeout: int | None = None) -> bool:
    return leases_module.ensure_on(_module(), api, timeout)


def node_guests(api: ProxmoxAPI) -> list[dict[str, Any]]:
    return cleanup_module.node_guests(_module(), api)


def describe_guests(api: ProxmoxAPI) -> list[dict[str, Any]]:
    return cleanup_module.describe_guests(_module(), api)


def orphaned_guests(api: ProxmoxAPI) -> list[dict[str, Any]]:
    return cleanup_module.orphaned_guests(_module(), api)


def running_guest_vmids(api: ProxmoxAPI) -> list[int]:
    return cleanup_module.running_guest_vmids(_module(), api)


def host_power_policy() -> str:
    return cleanup_module.host_power_policy(_module())


def shutdown_host(api: ProxmoxAPI) -> dict[str, Any]:
    return cleanup_module.shutdown_host(_module(), api)


def guest_status(api: ProxmoxAPI, kind: str, vmid: int) -> str:
    return cleanup_module.guest_status(_module(), api, kind, vmid)


def stop_guest(api: ProxmoxAPI, kind: str, vmid: int) -> None:
    cleanup_module.stop_guest(_module(), api, kind, vmid)


def delete_guest(api: ProxmoxAPI, kind: str, vmid: int) -> None:
    cleanup_module.delete_guest(_module(), api, kind, vmid)


def _delete_guest(api: ProxmoxAPI, kind: str, vmid: int, *,
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


def recent_guest_activity(api: ProxmoxAPI, kind: str, vmid: int, *,
                          within: int = 1800,
                          record: dict[str, Any] | None = None) -> bool:
    return cleanup_module.recent_guest_activity(
        _module(), api, kind, vmid, within=within, record=record)


def reclaim_orphans(api: ProxmoxAPI, *,
                    include_active: bool = False) -> dict[str, Any]:
    return cleanup_module.reclaim_orphans(
        _module(), api, include_active=include_active)


def finalize_lease(api: ProxmoxAPI, lease: dict[str, Any]) -> list[str]:
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

    api = sub.add_parser("api")
    api.add_argument("--lease")
    api.add_argument(
        "--method", type=str.upper,
        choices=("GET", "POST", "PUT", "DELETE"), required=True,
        help="HTTP method (case-insensitive)",
    )
    api.add_argument("--path", required=True)
    api.add_argument("--data", action="append", default=[])
    api.add_argument("--policy", choices=("delete", "retain"), default="delete")
    api.add_argument("--host-change-authorized", action="store_true")
    api.add_argument(
        "--slow-storage-accepted", action="store_true",
        help="acknowledge placing a guest disk on the configured bulk "
             "storage, which is slow enough to distort any I/O measurement",
    )
    api.add_argument(
        "--password-stdin",
        action="store_true",
        help="Read a password value from stdin without exposing it in argv. "
             "It must not be empty: a write stores the credential",
    )
    api.add_argument(
        "--password-key",
        default="password",
        help="Field the stdin password fills, e.g. cipassword for cloud-init",
    )
    api.add_argument("--wait-task", action="store_true")
    api.add_argument("--task-timeout", type=int, default=1800)
    api.set_defaults(func=cmd_api)

    upload = sub.add_parser("upload")
    upload.add_argument("--lease", required=True)
    # Bulk by default. ISOs are the biggest thing this tool writes, and the
    # Proxmox root filesystem is small: this lab's filled to 96% on ISOs alone,
    # which takes the hypervisor down with it long before it takes a lease down.
    # choices=None when nothing is configured, so the argument stays usable and
    # cmd_upload's own check reports the problem instead of argparse refusing
    # every value including the default.
    upload.add_argument("--storage", default=DEFAULT_UPLOAD_STORAGE,
                        choices=UPLOAD_STORAGES or None,
                        help="default: %(default)s"
                             + (" (the configured bulk store)"
                                if DEFAULT_UPLOAD_STORAGE ==
                                str(CONFIG.storage.bulk_storage) else ""))
    upload.add_argument("--content", choices=("import", "iso"), default="import")
    upload.add_argument("--file", required=True)
    upload.add_argument("--timeout", type=int, default=1800)
    upload.add_argument("--task-timeout", type=int, default=1800)
    upload.set_defaults(func=cmd_upload)

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

    from . import android
    from . import console
    from . import disk
    from . import guest
    from . import longterm
    from . import memflow
    from . import netcap
    from . import netgw
    from . import isoinspect
    from . import oci
    from . import onboarding
    from . import pe
    from . import recipes
    from . import share
    from . import storage
    from . import usb
    from . import virtio
    from . import windows

    android.register(sub, _module())
    console.register(sub, _module())
    disk.register(sub, _module())
    guest.register(sub, _module())
    longterm.register(sub, _module())
    memflow.register(sub, _module())
    netcap.register(sub, _module())
    netgw.register(sub, _module())
    isoinspect.register(sub, _module())
    oci.register(sub, _module())
    onboarding.register(sub, _module())
    pe.register(sub, _module())
    recipes.register(sub, _module())
    share.register(sub, _module())
    storage.register(sub, _module())
    usb.register(sub, _module())
    virtio.register(sub, _module())
    windows.register(sub, _module())
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
        "android", "console", "guest", "rfb", "s3", "netgw", "share",
        "vision", "ws",
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
