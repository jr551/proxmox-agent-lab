#!/usr/bin/env python3
"""Guarded Proxmox lab controller.

Every mutation belongs to a lease, created resources are registered to it,
and finalising the last lease powers the machine off. Site-specific values
come from the config file; secrets come from the OS keyring.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
try:
    import fcntl  # POSIX advisory locks; absent on Windows
except ImportError:  # pragma: no cover - Windows only
    fcntl = None  # type: ignore[assignment]
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
from .config import ConfigError


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
API_ROOT = f"https://{HOST}:{PORT}/api2/json"
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
SENSITIVE_KEY = re.compile(
    r"(pass(word)?|token|secret|authorization|private.?key|cipassword|ssh.?keys?)",
    re.IGNORECASE,
)
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
UPDATE_CHECK_URL = (
    "https://api.github.com/repos/jr551/proxmox-agent-lab/releases/latest"
)
UPDATE_CHECK_INTERVAL_SECONDS = 86400


class LabError(RuntimeError):
    pass


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


def json_dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def check_for_updates(*, now: float | None = None) -> dict[str, Any]:
    """Check GitHub at most daily; network failure must never block the lab."""
    checked_at = time.time() if now is None else now
    cache = STATE_ROOT / "github-update-check.json"
    try:
        previous = json.loads(cache.read_text())
    except (OSError, ValueError, TypeError):
        previous = {}
    last = previous.get("checked_at", 0)
    if (
        previous.get("current") == __version__
        and isinstance(last, (int, float))
        and checked_at - last < UPDATE_CHECK_INTERVAL_SECONDS
    ):
        return {**previous, "cached": True}

    result: dict[str, Any] = {
        "checked_at": checked_at,
        "current": __version__,
        "latest": None,
        "update_available": False,
        "cached": False,
    }
    try:
        req = request.Request(
            UPDATE_CHECK_URL,
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": f"proxmox-agent-lab/{__version__}"},
        )
        with request.urlopen(req, timeout=3) as response:
            payload = json.load(response)
        tag = str(payload.get("tag_name", "")).strip()
        latest = tag.removeprefix("v")
        if re.fullmatch(r"\d+(?:\.\d+){1,3}", latest):
            result["latest"] = latest
            current_parts = tuple(int(x) for x in __version__.split("."))
            latest_parts = tuple(int(x) for x in latest.split("."))
            result["update_available"] = latest_parts > current_parts
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        result["error"] = "github update check unavailable"
    try:
        json_dump(cache, result)
    except OSError:
        pass
    return result


def update_notice() -> None:
    result = check_for_updates()
    if result.get("update_available"):
        print(
            f"notice: proxmox-agent-lab {result['latest']} is available on "
            "GitHub; update before starting new work when practical",
            file=sys.stderr,
        )


def redact(value: Any, key: str = "") -> Any:
    if SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        if "PVEAPIToken=" in value or "Bearer " in value:
            return "[REDACTED]"
        return value[:1000]
    return value


def _bind(lab: Any, fn: Any) -> Any:
    """Bind a ``lab`` instance to a command handler for argparse."""
    return lambda args: fn(lab, args)


def _lock_file(handle: Any) -> None:
    """Take an exclusive advisory lock, blocking until it is ours.

    POSIX gets flock. Windows has no equivalent that blocks the same way, so
    it takes the non-blocking one and continues either way: the lock exists to
    stop two controllers on *one* machine interleaving, and a Windows box
    without it is no worse off than it was before Windows was supported.
    """
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return
    try:  # pragma: no cover - Windows only
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    except (ImportError, OSError):
        pass


def _try_lock_file(handle: Any) -> bool:
    """Take the lock if it is free. False when someone else holds it."""
    if fcntl is not None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False
    try:  # pragma: no cover - Windows only
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False
    except ImportError:
        # Neither flock nor msvcrt: not a platform that exists today. Proceed
        # rather than report the lock permanently held -- this gate guards the
        # backup sweep, and silently never running it is worse than the
        # theoretical double-run it prevents.
        return True


@contextlib.contextmanager
def controller_lock() -> Any:
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+") as handle:
        _lock_file(handle)
        yield


@contextlib.contextmanager
def sweep_lock(name: str) -> Any:
    """A non-blocking lock for work that runs long and must not stack up.

    A backup can run for hours. It must not hold the controller lock, or every
    lease operation queues behind it, and a watchdog firing every five minutes
    must not start a second copy of the same vzdump. So this is separate and
    non-blocking: yields False when a previous sweep still holds it.
    """
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    with (STATE_ROOT / f"{name}.lock").open("a+") as handle:
        if not _try_lock_file(handle):
            yield False
            return
        yield True


def _controller_id() -> str:
    """This machine's name in the shared ledger."""
    return str(CONFIG.audit.get("controller_id") or socket.gethostname())


_LEDGER_CACHE: Any = False


def ledger() -> Any:
    """Settings for the shared MariaDB ledger, or None if not configured yet.

    Cached for the life of the process: this is consulted on every audited
    action, and rebuilding it each time would re-read the bootstrap secret.
    """
    global _LEDGER_CACHE
    if _LEDGER_CACHE is False:
        try:
            secret = secrets_store.get(
                CONFIG, secrets_store.BOOTSTRAP_SECRET, required=False
            )
        except secrets_store.SecretError:
            secret = ""
        _LEDGER_CACHE = journal_module.settings_from_config(CONFIG, secret)
    return _LEDGER_CACHE


_AUTO_MIGRATED = False


def _auto_migrate_once() -> None:
    """Carry a controller upgraded from an older release into the shared ledger.

    Runs at most once per process, and at most once per machine (a marker file
    records it). Silent and non-fatal: an upgrade must not turn the first
    command after it into a failure.
    """
    global _AUTO_MIGRATED
    if _AUTO_MIGRATED:
        return
    _AUTO_MIGRATED = True
    settings = ledger()
    if settings is None or journal_module.migration_done(JOURNAL_ROOT):
        return
    detail = journal_module.auto_migrate(
        settings, JOURNAL_ROOT, controller=_controller_id()
    )
    if detail and detail.get("uploaded"):
        print(
            f"notice: carried {detail['uploaded']} event(s) from this "
            "controller's previous local ledger into the shared MariaDB "
            "ledger. The old files were left in place.",
            file=sys.stderr,
        )


def audit(event: str, **fields: Any) -> None:
    """Append one redacted event to the shared ledger.

    Never fails the action being audited. The lab host is powered off between
    leases by design, so an unreachable ledger spools locally and is uploaded
    later by 'proxmox-lab journal --flush-spool'.
    """
    now = utc_now()
    record = {
        "timestamp": now.isoformat().replace("+00:00", "Z"),
        "event": event,
        "event_id": uuid.uuid4().hex,
        "controller": _controller_id(),
        **redact(fields),
    }
    _auto_migrate_once()
    outcome = journal_module.record(
        ledger(), JOURNAL_ROOT, record, controller=_controller_id()
    )
    if outcome == "spooled" and not _SPOOL_NOTICE_SHOWN:
        _note_spooling()


_SPOOL_NOTICE_SHOWN = False


def _note_spooling() -> None:
    """Say once per run that the ledger is unreachable and events are queued."""
    global _SPOOL_NOTICE_SHOWN
    _SPOOL_NOTICE_SHOWN = True
    print(
        "notice: the audit ledger is unreachable; events are being spooled to "
        f"{journal_module.spool_path(JOURNAL_ROOT)}. Upload them with "
        "'proxmox-lab journal --flush-spool' once the lab host is up.",
        file=sys.stderr,
    )


_TOKEN_CACHE: str | None = None


def keychain_secret() -> str:
    """The Proxmox API token secret, from whichever keyring is configured.

    Cached for the life of the process. Reading the keyring spawns a
    subprocess, and this is called on every single API request -- building a
    VM makes hundreds, and paying a process spawn for each was pure waste.
    """
    global _TOKEN_CACHE
    if _TOKEN_CACHE is not None:
        return _TOKEN_CACHE
    if not HOST or not NODE:
        raise LabError(
            "This install is not configured yet. Run 'proxmox-lab init' to "
            "create a config file, then fill in [proxmox] host and node."
        )
    try:
        _TOKEN_CACHE = secrets_store.get(CONFIG, "proxmox-token")
    except secrets_store.SecretError as exc:
        raise LabError(str(exc)) from None
    return _TOKEN_CACHE


class ProxmoxAPI:
    def __init__(self) -> None:
        self._ssl = ssl.create_default_context()
        if not VERIFY_TLS:
            # A fresh Proxmox install has a self-signed certificate, so this
            # is off by default. Set [proxmox] verify_tls once you have put a
            # trusted certificate on the host.
            self._ssl.check_hostname = False
            self._ssl.verify_mode = ssl.CERT_NONE

    def call(
        self,
        method: str,
        path: str,
        data: dict[str, Any] | None = None,
        *,
        timeout: int = 30,
    ) -> Any:
        method = method.upper()
        if not path.startswith("/"):
            path = "/" + path
        url = API_ROOT + path
        payload: bytes | None = None
        if method in ("GET", "DELETE") and data:
            url += "?" + parse.urlencode(data, doseq=True)
        elif data:
            payload = parse.urlencode(data, doseq=True).encode()
        token = keychain_secret()
        req = request.Request(
            url,
            data=payload,
            method=method,
            headers={
                "Authorization": (
                    f"PVEAPIToken={TOKEN_USER}!{TOKEN_NAME}={token}"
                ),
                "Accept": "application/json",
            },
        )
        try:
            with request.urlopen(req, context=self._ssl, timeout=timeout) as response:
                body = json.load(response)
        except error.HTTPError as exc:
            if exc.code == 596:
                raise LabError(
                    f"Proxmox HTTP 596 for {method} {path}: guest agent is not "
                    "responding; the guest may be hung or its storage offline. "
                    "Try console screenshot or serial instead."
                ) from None
            detail = exc.read().decode(errors="replace")[:1000]
            raise LabError(f"Proxmox HTTP {exc.code} for {method} {path}: {detail}")
        except (error.URLError, TimeoutError, OSError) as exc:
            raise LabError(f"Proxmox unavailable for {method} {path}: {exc}")
        return body.get("data")

    def reachable(self) -> bool:
        try:
            self.call("GET", "/version", timeout=4)
            return True
        except LabError:
            return False


def _audit_through_boot(event: str, **fields: Any) -> None:
    """audit(), for the moment right after the lab host wakes.

    The ledger runs on that same host, so it is routinely not answering yet
    when the Proxmox API already is. `audit` never raises -- it spools -- so
    this is simply audit() with a name that says why the call site cares.
    """
    audit(event, **fields)


def ensure_on(api: ProxmoxAPI, timeout: int | None = None) -> bool:
    """Switch the lab machine on if it is not already up. Returns True if we
    had to wake it."""
    if api.reachable():
        return False
    if timeout is None:
        timeout = int(CONFIG.power.get("boot_timeout_seconds", 300))
    if timeout < MIN_COLD_BOOT_TIMEOUT_SECONDS:
        raise LabError(
            f"cold-boot timeout must be at least {MIN_COLD_BOOT_TIMEOUT_SECONDS}s; "
            "the lab host commonly needs a minute or two before its API answers"
        )
    try:
        detail = power_module.power_on(CONFIG)
    except (power_module.PowerError, ConfigError) as exc:
        raise LabError(f"cannot switch the lab machine on: {exc}") from None
    _audit_through_boot("lab-power-on-requested", **detail)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if api.reachable():
            _audit_through_boot("lab-power-on-verified", host=HOST, node=NODE)
            return True
        time.sleep(5)
    raise LabError(
        f"power-on was requested via {detail.get('mode')} but Proxmox at "
        f"{HOST}:{PORT} did not respond within {timeout}s. Check that the "
        "machine booted and that Proxmox starts on boot."
    )


def lease_path(lease_id: str) -> Path:
    if not re.fullmatch(r"[a-z0-9-]{8,80}", lease_id):
        raise LabError("Invalid lease ID")
    return LEASE_ROOT / f"{lease_id}.json"


def load_lease(lease_id: str, *, active: bool = True) -> dict[str, Any]:
    path = lease_path(lease_id)
    if not path.exists():
        raise LabError(f"Unknown lease: {lease_id}")
    lease = json.loads(path.read_text())
    if active and lease.get("state") != "active":
        raise LabError(f"Lease {lease_id} is not active")
    return lease


def save_lease(lease: dict[str, Any]) -> None:
    json_dump(lease_path(lease["id"]), lease)


def new_expiry(ttl: int = DEFAULT_TTL_SECONDS) -> str:
    return (utc_now() + dt.timedelta(seconds=ttl)).isoformat().replace("+00:00", "Z")


def parse_expiry(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def mcp_activity_path() -> Path:
    return STATE_ROOT / "mcp-activity.json"


def record_mcp_activity(tool_name: str) -> None:
    recorded_at = iso_now()
    json_dump(
        mcp_activity_path(),
        {
            "last_command_at": recorded_at,
            "tool": tool_name[:160],
        },
    )
    audit("mcp-command", tool=tool_name[:160], command_at=recorded_at)


def mcp_idle_elapsed(now: dt.datetime | None = None) -> float:
    path = mcp_activity_path()
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
) -> bool:
    return (
        reachable
        and active_lease_count == 0
        and not has_failures
        and idle_seconds >= MCP_IDLE_SHUTDOWN_SECONDS
    )


def wait_task(api: ProxmoxAPI, upid: str, timeout: int = 180) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    encoded = parse.quote(upid, safe="")
    while time.monotonic() < deadline:
        status = api.call("GET", f"/nodes/{NODE}/tasks/{encoded}/status")
        if status.get("status") == "stopped":
            if status.get("exitstatus") != "OK":
                raise LabError(
                    f"Proxmox task failed: {status.get('exitstatus', 'unknown')}"
                )
            return status
        time.sleep(2)
    raise LabError(f"Timed out waiting for Proxmox task {upid}")


def guest_status(api: ProxmoxAPI, kind: str, vmid: int) -> str:
    status = api.call("GET", f"/nodes/{NODE}/{kind}/{vmid}/status/current")
    return status.get("status", "unknown")


def stop_guest(api: ProxmoxAPI, kind: str, vmid: int) -> None:
    try:
        if guest_status(api, kind, vmid) == "stopped":
            return
    except LabError as exc:
        if "HTTP 500" in str(exc) or "HTTP 404" in str(exc):
            return
        raise
    upid = api.call("POST", f"/nodes/{NODE}/{kind}/{vmid}/status/shutdown")
    try:
        wait_task(api, upid, timeout=130)
    except LabError:
        audit(
            "guest-graceful-shutdown-timeout",
            vmid=vmid,
            kind=kind,
            task_id=upid,
        )
        hard_upid = api.call("POST", f"/nodes/{NODE}/{kind}/{vmid}/status/stop")
        wait_task(api, hard_upid, timeout=60)


def _guest_is_gone(error: LabError) -> bool:
    message = str(error)
    # A prior finalizer run (or manual removal) beat us to it. Proxmox reports
    # this as a 404, or as a 500 whose body says the config file is absent.
    return "HTTP 404" in message or (
        "HTTP 500" in message and "does not exist" in message
    )


def _storage_io_error(error: LabError) -> bool:
    message = str(error).lower()
    return "input/output error" in message or "i/o error" in message


def _delete_guest(
    api: ProxmoxAPI, kind: str, vmid: int, *, destroy_unreferenced_disks: bool
) -> None:
    data: dict[str, int] = {"purge": 1}
    if destroy_unreferenced_disks:
        data["destroy-unreferenced-disks"] = 1
    upid = api.call("DELETE", f"/nodes/{NODE}/{kind}/{vmid}", data)
    wait_task(api, upid, timeout=180)


def _forget_retained(kind: str, vmid: int) -> None:
    """Keep the registry honest: a guest that is gone is not retained."""
    try:
        inventory_module.forget(STATE_ROOT, kind, vmid)
    except OSError:
        pass


def delete_guest(api: ProxmoxAPI, kind: str, vmid: int) -> None:
    try:
        _delete_guest(api, kind, vmid, destroy_unreferenced_disks=True)
        _forget_retained(kind, vmid)
    except LabError as exc:
        if _guest_is_gone(exc):
            _forget_retained(kind, vmid)
            return
        if not _storage_io_error(exc):
            raise
        try:
            # Destroying unreferenced disks makes Proxmox inspect every
            # configured storage. An unrelated failed device must not strand
            # a lease whose guest can otherwise be deleted.
            _delete_guest(api, kind, vmid, destroy_unreferenced_disks=False)
            _forget_retained(kind, vmid)
        except LabError as retry_exc:
            if _guest_is_gone(retry_exc):
                _forget_retained(kind, vmid)
                return
            raise LabError(
                f"Could not delete {kind}/{vmid} after retrying without "
                f"unreferenced-disk cleanup: {retry_exc}; initial storage "
                f"error: {exc}"
            ) from retry_exc


def _leases_in_states(
    states: tuple[str, ...], excluding: str | None = None
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for path in sorted(LEASE_ROOT.glob("*.json")) if LEASE_ROOT.exists() else []:
        try:
            lease = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if lease.get("state") in states and lease.get("id") != excluding:
            result.append(lease)
    return result


def active_leases(excluding: str | None = None) -> list[dict[str, Any]]:
    return _leases_in_states(("active",), excluding)


def cleanup_candidate_leases() -> list[dict[str, Any]]:
    """Leases a sweep is allowed to finalize.

    `cleanup_failed` is included on purpose. A transient QEMU lock while
    stopping one guest used to take a lease out of every later sweep, leaving
    its guests -- and so the host -- running until somebody reran `lease-end`
    by hand with the exact lease id. Finalizing is idempotent, so retrying an
    already-cleaned resource costs nothing and the fail-closed guarantee holds.
    """
    return _leases_in_states(("active", "cleanup_failed"))


def lease_claims(lease: dict[str, Any], kind: str, vmid: int) -> bool:
    """True when `lease` lists (kind, vmid) among its resources."""
    for item in lease.get("resources", []):
        try:
            if item.get("kind") == kind and int(item.get("vmid")) == int(vmid):
                return True
        except (TypeError, ValueError):
            continue
    return False


def lease_is_live(lease: dict[str, Any], now: dt.datetime | None = None) -> bool:
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
    lease_id: str, kind: str, vmid: int, *, now: dt.datetime | None = None
) -> str | None:
    """The id of another *live* lease that also owns (kind, vmid), if any.

    Registration does not stop two leases from listing the same guest -- a
    VMID can be handed to a newer lease while an older one still names it --
    so ownership is resolved at cleanup time, before anything destructive.
    """
    for other in active_leases(excluding=lease_id):
        if lease_claims(other, kind, vmid) and lease_is_live(other, now):
            return str(other.get("id"))
    return None


def is_long_term(lease: dict[str, Any]) -> bool:
    return lease.get("kind") == "long-term"


def long_term_leases() -> list[dict[str, Any]]:
    """Active long-term leases. While any exists, the host stays powered on."""
    return [lease for lease in active_leases() if is_long_term(lease)]


def lease_requires_cleanup(lease: dict[str, Any]) -> bool:
    return any(
        resource.get("policy", "delete") == "delete"
        for resource in lease.get("resources", [])
    )


def all_lease_ids() -> set[str]:
    """Every lease id the controller still holds a record for, any state."""
    ids: set[str] = set()
    for path in sorted(LEASE_ROOT.glob("*.json")) if LEASE_ROOT.exists() else []:
        try:
            lease = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if lease.get("id"):
            ids.add(str(lease["id"]))
    return ids


def node_guests(api: ProxmoxAPI) -> list[dict[str, Any]]:
    return [
        item for item in (api.call("GET", "/cluster/resources", {"type": "vm"}) or [])
        if isinstance(item, dict) and "vmid" in item
    ]


def describe_guests(api: ProxmoxAPI) -> list[dict[str, Any]]:
    """Every guest on the node, with what the controller can prove about it."""
    return inventory_module.classify(
        node_guests(api),
        known_leases=all_lease_ids(),
        retained=inventory_module.entries(STATE_ROOT),
    )


def orphaned_guests(api: ProxmoxAPI) -> list[dict[str, Any]]:
    """Guests this tool created that no lease record or registry vouches for.

    Cleanup only ever finalizes resources listed in a lease, so a guest whose
    lease record is gone is invisible to it for ever -- and while such a guest
    runs, `shutdown_host()` refuses to power the machine off (by design). One
    of these can therefore keep the lab on indefinitely.
    """
    return inventory_module.orphans(describe_guests(api))


def running_guest_vmids(api: ProxmoxAPI) -> list[int]:
    """VMIDs currently running on the node, lease or no lease.

    A guest can exist outside any lease's tracked resources -- a persistent
    builder kept alive on purpose across sessions (see 'guest template' /
    'guest clone'), or one a caller drove directly by VMID. The decision to
    power off the host must not rely on lease bookkeeping alone, or a guest
    like that gets the host pulled out from under it.
    """
    resources = api.call("GET", "/cluster/resources", {"type": "vm"}) or []
    return sorted(
        int(item["vmid"]) for item in resources
        if isinstance(item, dict) and item.get("status") == "running"
        and not _is_lab_infrastructure(item)
    )


# Infrastructure this tool runs on the host itself -- currently the audit
# ledger. It is onboot and outlives every lease on purpose, so counting it as
# an untracked guest would mean the host could never power itself off again,
# which is the whole point of the machine.
INFRA_TAG = "codex-lab-infra"


def _is_lab_infrastructure(resource: dict[str, Any]) -> bool:
    tags = str(resource.get("tags") or "").replace(",", ";")
    return INFRA_TAG in [tag.strip() for tag in tags.split(";")]


def shutdown_host(api: ProxmoxAPI) -> bool:
    """Shut the lab machine down and confirm it actually went off."""
    if not api.reachable():
        audit("lab-power-off-already-verified", host=HOST, node=NODE)
        return True
    running = running_guest_vmids(api)
    if running:
        audit("lab-power-off-blocked-by-running-guest", host=HOST, node=NODE,
              vmids=running)
        return False
    try:
        task = api.call("POST", f"/nodes/{NODE}/status", {"command": "shutdown"})
        audit("lab-graceful-shutdown-requested", node=NODE, task_id=task)
    except LabError as exc:
        audit("lab-graceful-shutdown-request-failed", error=str(exc))
    deadline = time.monotonic() + 240
    down_count = 0
    while time.monotonic() < deadline:
        if api.reachable():
            down_count = 0
        else:
            # Two consecutive failures, so a momentary blip is not mistaken
            # for a machine that has finished powering down.
            down_count += 1
            if down_count >= 2:
                audit("lab-power-off-verified", host=HOST, node=NODE)
                return True
        time.sleep(5)

    # Graceful shutdown did not finish. Force-off is a last resort and is only
    # available for power modes that can actually cut power.
    if not power_module.can_force_off(CONFIG):
        audit("lab-power-off-unverified", host=HOST, node=NODE,
              reason="graceful shutdown timed out and no force-off is configured")
        return False
    try:
        detail = power_module.force_off(CONFIG)
        audit("lab-emergency-force-off-requested", **detail)
    except (power_module.PowerError, ConfigError) as exc:
        audit("lab-power-off-unverified", host=HOST, node=NODE, error=str(exc))
        return False
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if not api.reachable():
            time.sleep(5)
            if not api.reachable():
                audit("lab-emergency-force-off-verified", host=HOST, node=NODE)
                return True
        time.sleep(5)
    audit("lab-power-off-unverified", host=HOST, node=NODE)
    return False


def cmd_status(args: argparse.Namespace) -> None:
    api = ProxmoxAPI()
    idle_seconds = int(mcp_idle_elapsed())
    if not api.reachable():
        print(
            json.dumps(
                {
                    "reachable": False,
                    "host": HOST,
                    "node": NODE,
                    "mcp_idle_seconds": idle_seconds,
                    "mcp_idle_shutdown_after_seconds": MCP_IDLE_SHUTDOWN_SECONDS,
                },
                indent=2,
            )
        )
        return
    version = api.call("GET", "/version")
    nodes = api.call("GET", "/nodes")
    guests = api.call("GET", "/cluster/resources", {"type": "vm"})
    output = {
        "reachable": True,
        "host": HOST,
        "node": NODE,
        "version": version,
        "nodes": nodes,
        "guests": guests,
        "mcp_idle_seconds": idle_seconds,
        "mcp_idle_shutdown_after_seconds": MCP_IDLE_SHUTDOWN_SECONDS,
        "active_leases": [
            {"id": x["id"], "purpose": x["purpose"], "expires_at": x["expires_at"]}
            for x in active_leases()
        ],
    }
    described = inventory_module.classify(
        [x for x in guests if isinstance(x, dict) and "vmid" in x],
        known_leases=all_lease_ids(),
        retained=inventory_module.entries(STATE_ROOT),
    )
    orphans = inventory_module.orphans(described)
    output["retained_guests"] = [x for x in described if x["retained"]]
    output["orphaned_guests"] = orphans
    if orphans:
        running = [x["vmid"] for x in orphans if x["status"] == "running"]
        output["orphan_note"] = (
            f"{len(orphans)} guest(s) carry a lease tag this controller has no "
            "record of, so no sweep will ever clean them up"
            + (f"; {len(running)} still running, which blocks host power-off. "
               "Reclaim with 'cleanup-expired --reclaim-orphans "
               "--host-change-authorized'" if running else "")
        )
    print(json.dumps(redact(output), indent=2, sort_keys=True))


def cmd_power_on(args: argparse.Namespace) -> None:
    if not args.standalone_authorized:
        raise LabError(
            "Standalone power-on has no lease finalizer and is refused by default. "
            "Use lease-begin for normal work, or pass --standalone-authorized "
            "only when a person will manage shutdown."
        )
    changed = ensure_on(ProxmoxAPI(), timeout=args.timeout)
    print(json.dumps({"reachable": True, "power_on_requested": changed}))


def cmd_lease_begin(args: argparse.Namespace) -> None:
    api = ProxmoxAPI()
    with controller_lock():
        powered_on = ensure_on(api, timeout=args.timeout)
        lease_id = f"{utc_now().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(4)}"
        existing = api.call("GET", "/cluster/resources", {"type": "vm"})
        long_term = bool(getattr(args, "long_term", False))
        lease = {
            "id": lease_id,
            "purpose": args.purpose[:240],
            "kind": "long-term" if long_term else "session",
            "created_at": iso_now(),
            "updated_at": iso_now(),
            # A long-term lease never expires; that is the point. The watchdog
            # skips it, and its guests survive until explicitly destroyed.
            "expires_at": None if long_term else new_expiry(args.ttl),
            "state": "active",
            "host_was_powered_on": powered_on,
            "initial_vmids": sorted(
                int(item["vmid"]) for item in existing if "vmid" in item
            ),
            "resources": [],
        }
        save_lease(lease)
        try:
            audit(
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
            lease_path(lease_id).unlink(missing_ok=True)
            raise


def cmd_lease_heartbeat(args: argparse.Namespace) -> None:
    with controller_lock():
        lease = load_lease(args.lease)
        if is_long_term(lease):
            print(json.dumps({
                "lease": args.lease, "kind": "long-term", "expires_at": None,
                "note": "long-term leases do not expire; no heartbeat needed",
            }, indent=2))
            return
        lease["updated_at"] = iso_now()
        lease["expires_at"] = new_expiry(args.ttl)
        save_lease(lease)
        audit(
            "lease-heartbeat",
            lease=args.lease,
            expires_at=lease["expires_at"],
        )
    print(json.dumps({"lease": args.lease, "expires_at": lease["expires_at"]}))


def register_resource(
    lease: dict[str, Any],
    kind: str,
    vmid: int,
    policy: str,
    name: str | None = None,
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
        lease["expires_at"] = new_expiry()
    save_lease(lease)
    if policy == "retain":
        # This guest is meant to outlive the lease, and the lease record will
        # not be here for ever. Its node tag proves only that some lease made
        # it, so the durable owner is recorded now or never.
        try:
            inventory_module.record(
                STATE_ROOT, kind=kind, vmid=vmid, lease=str(lease.get("id")),
                now=iso_now(), purpose=str(lease.get("purpose") or ""),
                name=name,
            )
        except OSError as exc:
            print(f"warning: could not record retained guest {kind}/{vmid}: "
                  f"{exc}", file=sys.stderr)


def cmd_lease_register(args: argparse.Namespace) -> None:
    with controller_lock():
        lease = load_lease(args.lease)
        if is_long_term(lease):
            # Guests of a long-term lease are meant to survive, so they are
            # retained and given Proxmox's protection flag.
            args.policy = "retain"
        if args.vmid in lease["initial_vmids"] and not args.allow_existing:
            raise LabError(
                f"VMID {args.vmid} existed before this lease; use --allow-existing "
                "only for an explicitly authorized retained resource"
            )
        owner = resource_owner_elsewhere(args.lease, args.kind, args.vmid)
        if owner:
            raise LabError(
                f"{args.kind}/{args.vmid} is already registered to live lease "
                f"{owner}. Two leases owning one guest is how a cleanup sweep "
                f"comes to delete a machine another lease is still using. "
                f"Work under {owner}, or end/abandon it first."
            )
        register_resource(lease, args.kind, args.vmid, args.policy, args.name)
        audit(
            "lease-register",
            lease=args.lease,
            kind=args.kind,
            vmid=args.vmid,
            policy=args.policy,
            name=args.name,
        )
    print(json.dumps({"registered": True, "lease": args.lease, "vmid": args.vmid}))


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
        mode="w", prefix="proxmox-upload-", delete=True
    ) as config:
        os.chmod(config.name, 0o600)
        config.write(config_text)
        config.flush()
        result = subprocess.run(
            upload_curl_argv(config.name, source, args.content, args.storage),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=args.timeout,
            check=False,
        )
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


def finalize_lease(api: ProxmoxAPI, lease: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    transferred: list[str] = []
    now = utc_now()
    for resource in reversed(lease.get("resources", [])):
        kind = resource["kind"]
        vmid = int(resource["vmid"])
        owner = resource_owner_elsewhere(lease["id"], kind, vmid, now=now)
        if owner:
            # A newer, still-live lease claims this guest. Stopping or
            # deleting it here would destroy a machine that lease is using --
            # the one failure mode expiry cleanup must never have.
            transferred.append(f"{kind}/{vmid}")
            audit(
                "lease-resource-owned-by-another-lease",
                lease=lease["id"],
                kind=kind,
                vmid=vmid,
                owner=owner,
            )
            continue
        try:
            # The unattended answer ISO holds the Administrator password in
            # plain text. An install abandoned before `windows finish` would
            # otherwise leave it on shared storage for good (audit 2026-08-24).
            if kind == "qemu" and resource.get("policy", "delete") == "delete":
                try:
                    from . import windows as windows_module
                    windows_module._shred_answer_iso(_module(), api, vmid)
                except Exception:  # noqa: BLE001 - best-effort, same as finish
                    pass
            stop_guest(api, kind, vmid)
            if resource.get("policy", "delete") == "delete":
                delete_guest(api, kind, vmid)
            audit(
                "lease-resource-finalized",
                lease=lease["id"],
                kind=kind,
                vmid=vmid,
                policy=resource.get("policy", "delete"),
            )
        except LabError as exc:
            failures.append(f"{kind}/{vmid}: {exc}")
            audit(
                "lease-resource-finalize-failed",
                lease=lease["id"],
                kind=kind,
                vmid=vmid,
                error=str(exc),
            )
    lease["state"] = "closed" if not failures else "cleanup_failed"
    lease["closed_at"] = iso_now()
    lease["failures"] = failures
    lease["transferred_resources"] = transferred
    save_lease(lease)
    return failures


def shared_lease_resources(lease: dict[str, Any]) -> list[dict[str, Any]]:
    """Guests `lease` would delete that another active lease also registers.

    `finalize_lease` already declines to touch a resource a still-*live* lease
    owns, and `lease-register` refuses to take one. Neither closes the window
    this looks at, because both ask "is the other lease live?" and a lease can
    be `active` while expired -- one heartbeat away from live again. It also
    only takes one registration path that skips `lease-register` (a module
    that calls `register_resource` directly, such as an idempotent
    `memflow ghidra-setup --lxc N` re-run under a second lease) for two live
    leases to name one guest.

    Reads lease records only, so it costs no network call inside the
    controller lock. `retain` resources are excluded: this never deletes one,
    and `finalize_lease` keeps deciding what to do with them.
    """
    others = active_leases(excluding=str(lease.get("id")))
    if not others:
        return []
    now = utc_now()
    shared: list[dict[str, Any]] = []
    for resource in lease.get("resources", []):
        if resource.get("policy", "delete") != "delete":
            continue
        try:
            kind = str(resource["kind"])
            vmid = int(resource["vmid"])
        except (KeyError, TypeError, ValueError):
            continue
        for other in others:
            if not lease_claims(other, kind, vmid):
                continue
            shared.append({
                "resource": f"{kind}/{vmid}",
                "kind": kind,
                "vmid": vmid,
                "lease": str(other.get("id")),
                "lease_kind": str(other.get("kind") or "session"),
                "lease_live": lease_is_live(other, now),
            })
    return shared


def describe_shared_resources(shared: list[dict[str, Any]]) -> str:
    return ", ".join(
        f"{item['resource']} (lease {item['lease']}"
        + ("" if item["lease_live"] else ", expired but still active")
        + ")"
        for item in shared
    )


def cmd_lease_end(args: argparse.Namespace) -> None:
    api = ProxmoxAPI()
    with controller_lock():
        lease = load_lease(args.lease, active=False)
        if lease.get("state") not in ("active", "cleanup_failed"):
            raise LabError(
                f"Lease {args.lease} cannot be finalized from state "
                f"{lease.get('state')}"
            )
        if is_long_term(lease):
            raise LabError(
                f"Lease {args.lease} is long-term: its guests are meant to "
                "survive. Use 'proxmox-lab lease-destroy --lease "
                f"{args.lease} --confirm' to remove it and its machines "
                "for good."
            )
        # Strictly before anything is powered on, stopped or deleted. A
        # warning that arrives next to an already-destroyed guest is worthless.
        shared = shared_lease_resources(lease)
        if shared and not getattr(args, "shared_guests_authorized", False):
            audit(
                "lease-end-refused-shared-guest",
                lease=args.lease,
                shared_with_other_leases=shared,
            )
            raise LabError(
                f"Lease {args.lease} would destroy guest(s) that another "
                "active lease still registers: "
                + describe_shared_resources(shared)
                + ". Deleting one of those stops somebody else's work and is "
                "not recoverable. End or abandon the other lease first, or "
                "re-run with --shared-guests-authorized to destroy them "
                "anyway."
            )
        if shared:
            print(
                "warning: destroying guest(s) another active lease registers, "
                "because --shared-guests-authorized was given: "
                + describe_shared_resources(shared),
                file=sys.stderr,
            )
        if not api.reachable() and lease_requires_cleanup(lease):
            ensure_on(api)
        if not api.reachable():
            lease["state"] = "closed"
            lease["closed_at"] = iso_now()
            lease["failures"] = []
            save_lease(lease)
            failures: list[str] = []
        else:
            failures = finalize_lease(api, lease)
        others = active_leases(excluding=args.lease)
        persistent = [x for x in others if is_long_term(x)]
        host_powered_off = False
        if not others:
            host_powered_off = shutdown_host(api)
        audit(
            "lease-end",
            lease=args.lease,
            failures=failures,
            remaining_active_leases=[x["id"] for x in others],
            long_term_leases=[x["id"] for x in persistent],
            host_powered_off=host_powered_off,
            shared_with_other_leases=shared,
        )
    result: dict[str, Any] = {
        "lease": args.lease,
        "failures": failures,
        "remaining_active_leases": [x["id"] for x in others],
        "host_powered_off": host_powered_off,
    }
    if lease.get("transferred_resources"):
        result["left_to_another_lease"] = lease["transferred_resources"]
    if shared:
        result["shared_with_other_leases"] = shared
        result["warning"] = (
            "--shared-guests-authorized was given, so this lease-end acted on "
            "guest(s) another active lease also registers: "
            + describe_shared_resources(shared)
        )
    if persistent:
        # Say this loudly. A machine left running is the surprise nobody
        # wants on their electricity bill.
        result["host_left_running"] = True
        result["reason"] = (
            f"{len(persistent)} long-term lease(s) keep this machine on: "
            + ", ".join(x["id"] for x in persistent)
        )
        result["to_power_off"] = "destroy them with 'lease-destroy', or "\
            "stop the host yourself"
    elif not others and not host_powered_off:
        running = running_guest_vmids(api) if api.reachable() else []
        result["host_left_running"] = True
        if running:
            result["reason"] = (
                "guest(s) still running outside any tracked lease: "
                + ", ".join(str(vmid) for vmid in running)
            )
            result["to_power_off"] = (
                "stop or register those guests, or stop the host yourself"
            )
        else:
            result["reason"] = "host power-off could not be verified"
            result["to_power_off"] = "check the host and stop it yourself"
    print(json.dumps(result, indent=2, sort_keys=True))
    created_at = lease.get("created_at") or lease.get("created")
    if created_at:
        lifetime = (utc_now() - parse_expiry(created_at)).total_seconds()
        if lifetime < 300:
            print(
                f"hint: lease {args.lease} ended after {int(lifetime)}s. For "
                "a work session, prefer ONE lease kept alive with "
                "lease-heartbeat every <=20 min; each begin/end cycle costs "
                "a host boot and provisioning.",
                file=sys.stderr,
            )
    if failures or (not others and not host_powered_off):
        raise LabError("Lease cleanup or host power-off did not complete")


def cmd_lease_abandon(args: argparse.Namespace) -> None:
    """Close a stopped ordinary lease without touching its guests or host."""
    with controller_lock():
        lease = load_lease(args.lease, active=False)
        if lease.get("state") not in ("active", "cleanup_failed"):
            raise LabError(
                f"Lease {args.lease} cannot be abandoned from state "
                f"{lease.get('state')}"
            )
        if is_long_term(lease):
            raise LabError(
                f"Lease {args.lease} is long-term and cannot be abandoned. "
                "Use 'proxmox-lab lease-release --lease "
                f"{args.lease} --confirm' or 'proxmox-lab lease-destroy "
                f"--lease {args.lease} --confirm'."
            )
        if not args.confirm:
            raise LabError(
                "lease-abandon leaves every registered guest and the host "
                "untouched. Re-run with --confirm after they are stopped."
            )
        api = ProxmoxAPI()
        if not api.reachable():
            raise LabError(
                "Cannot safely abandon this lease while Proxmox is "
                "unreachable; registered guests cannot be verified stopped."
            )
        stopped: list[str] = []
        missing: list[str] = []
        for resource in lease.get("resources", []):
            kind = resource["kind"]
            vmid = int(resource["vmid"])
            resource_id = f"{kind}/{vmid}"
            try:
                status = guest_status(api, kind, vmid)
            except LabError as exc:
                if "HTTP 404" in str(exc):
                    missing.append(resource_id)
                    continue
                raise LabError(
                    f"Cannot safely abandon lease {args.lease}: could not "
                    f"verify {resource_id} is stopped: {exc}"
                ) from None
            if status != "stopped":
                raise LabError(
                    f"Cannot safely abandon lease {args.lease}: {resource_id} "
                    f"is {status}, not stopped"
                )
            stopped.append(resource_id)
        lease["state"] = "closed"
        lease["closed_at"] = iso_now()
        lease["abandoned_at"] = lease["closed_at"]
        lease["abandoned_reason"] = (
            "registered guests verified stopped; no guest or host mutation"
        )
        save_lease(lease)
        audit_error: str | None = None
        try:
            audit(
                "lease-abandon",
                lease=args.lease,
                stopped=stopped,
                missing=missing,
                reason=lease["abandoned_reason"],
            )
        except (LabError, OSError, ValueError) as exc:
            audit_error = str(exc)
            print(
                "warning: lease was closed but its audit event could not be "
                f"recorded: {audit_error}",
                file=sys.stderr,
            )
    result: dict[str, Any] = {
        "lease": args.lease,
        "state": "closed",
        "guests_verified_stopped": stopped,
        "guests_already_missing": missing,
        "guest_mutation": False,
        "host_mutation": False,
        "audit_recorded": audit_error is None,
    }
    if audit_error is not None:
        result["audit_error"] = audit_error
    print(json.dumps(result, indent=2, sort_keys=True))


# How recently a guest must have been touched to count as in use. Tasks that
# only ever mean "something stopped this guest" are excluded, or our own stop
# would make every later run think the guest is busy.
ORPHAN_ACTIVITY_WINDOW_SECONDS = 1800
STOP_TASK_TYPES = frozenset(
    {"qmstop", "qmshutdown", "qmreset", "vzstop", "vzshutdown"}
)
# Work happening *inside* a guest produces no Proxmox task and does not reset
# its uptime, so a long build in an unmanaged container looks idle to both of
# the other signals. This floor is set where a guest is unmistakably doing
# something: an idle Debian guest on the lab node sits near 1% and a genuinely
# idle container near 0.005%, so 10% is not a judgement call.
BUSY_CPU_FRACTION = 0.10


def guest_load(record: dict[str, Any] | None) -> dict[str, Any]:
    """The activity numbers Proxmox already reports for a guest.

    Returned with every orphan, busy or not, so the reader can disagree with
    the threshold instead of having to trust it.

    `disk_written_bytes` is **advisory and reported only**. Proxmox's
    `diskwrite` has been observed reading 0 for an entire session on a qcow2
    guest over directory-backed storage that was demonstrably writing, so it
    is not a signal anything here decides on -- in either direction. It is
    also cumulative, so a non-zero value says the guest wrote at some point
    since boot, not that it is writing now. For an answer that can be relied
    on, measure the change over an interval with 'guest disk-activity
    --ground-truth', which cross-checks it against QEMU's own block counters
    and the allocated size of the image file on the host.
    """
    if not isinstance(record, dict):
        return {}
    load: dict[str, Any] = {}
    try:
        load["cpu_percent"] = round(float(record.get("cpu") or 0.0) * 100, 3)
    except (TypeError, ValueError):
        pass
    for source, name in (("mem", "mem_bytes"), ("diskwrite", "disk_written_bytes"),
                         ("netin", "net_in_bytes")):
        try:
            load[name] = int(record.get(source) or 0)
        except (TypeError, ValueError):
            continue
    return load


def recent_guest_activity(
    api: ProxmoxAPI, kind: str, vmid: int, *,
    within: int = ORPHAN_ACTIVITY_WINDOW_SECONDS,
    record: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Evidence that something is still using this guest, or None.

    "Orphaned" means *this* controller has no record of the guest. It does not
    mean nobody is using it: a second controller, or one whose state lives
    elsewhere, drives guests through the same API token and its lease records
    are not here. Reclamation stopped a live ReactOS benchmark that way -- the
    other session had been taking a console screenshot every 45 seconds, and
    restarted the guest 90 seconds later.

    Three independent signals, because each covers the others' blind spots:
    recent tasks (console, start, agent) show someone driving it from outside;
    a short uptime shows it was started recently even if the task log has
    rolled; and measurable CPU shows work happening *inside* it, which the
    other two cannot see at all -- a three-hour build in an unmanaged container
    generates no Proxmox task and does not reset the uptime. An unreadable task
    log counts as activity: leaving a guest running is a much smaller mistake
    than stopping somebody's work.

    A guest below the CPU floor is not proven idle, only not proven busy, so
    its measured load is reported either way.

    The disk counter is deliberately **not** one of the signals. `diskwrite`
    can read 0 on a guest that is writing hard (qcow2 over directory-backed
    storage is the known case), so reading a zero as "idle" would stop live
    work; and it is cumulative, so reading a non-zero as "busy" would keep a
    long-abandoned guest running for ever on one write it did at boot. It
    travels in `load` for the reader's benefit only, and no branch below
    consults it. Anything that wants a real answer has to measure the delta,
    which is what 'guest disk-activity' is for -- and that must never be
    called from here: it costs a monitor round trip and, for the host-side
    signal, the opt-in SSH boundary, neither of which belongs on the path
    that decides whether to leave somebody's guest alone.
    """
    load = guest_load(record)
    if load.get("cpu_percent", 0) >= BUSY_CPU_FRACTION * 100:
        return {"signal": "busy", "cpu_percent": load["cpu_percent"], **load}
    try:
        current = api.call(
            "GET", f"/nodes/{NODE}/{kind}/{vmid}/status/current"
        ) or {}
        uptime = int(current.get("uptime") or 0)
    except (LabError, TypeError, ValueError):
        uptime = 0
    if 0 < uptime < within:
        return {"signal": "started recently", "seconds_ago": uptime}
    try:
        tasks = api.call(
            "GET", f"/nodes/{NODE}/tasks", {"vmid": int(vmid), "limit": 20}
        ) or []
    except LabError as exc:
        return {"signal": "task log unreadable", "detail": str(exc)[:120]}
    now = time.time()
    for task in tasks:
        if not isinstance(task, dict):
            continue
        if str(task.get("type")) in STOP_TASK_TYPES:
            continue
        try:
            started = int(task.get("starttime") or 0)
        except (TypeError, ValueError):
            continue
        # A task timestamped in the future means the clocks disagree; treat it
        # as current rather than as ancient.
        if started and now - started < within:
            return {
                "signal": str(task.get("type")),
                "seconds_ago": max(0, int(now - started)),
                **load,
            }
    return None


def reclaim_orphans(
    api: ProxmoxAPI, *, include_active: bool = False
) -> dict[str, Any]:
    """Stop -- never delete -- guests no lease record or registry vouches for.

    Stopping is reversible and unblocks host power-off, which is the whole
    point: one abandoned guest otherwise keeps the machine on for ever.
    Deleting is not reversible, and the controller by definition cannot vouch
    for what is on a disk it has lost the record of, so that stays manual.

    A guest that shows recent activity is left alone unless `include_active`:
    "no record here" is not the same as "nobody is using it", and stopping
    somebody else's running work is the one outcome this command must not have
    by default.
    """
    result: dict[str, Any] = {
        "stopped": [], "failed": {}, "already_stopped": [], "left_active": {},
    }
    for guest in orphaned_guests(api):
        kind, vmid = guest["kind"], int(guest["vmid"])
        if guest.get("status") != "running":
            result["already_stopped"].append(vmid)
            continue
        activity = (
            None if include_active
            else recent_guest_activity(api, kind, vmid, record=guest.get("load"))
        )
        if activity:
            result["left_active"][str(vmid)] = activity
            audit(
                "orphan-guest-left-running",
                kind=kind,
                vmid=vmid,
                lease_tag=guest.get("lease_tag"),
                signal=activity.get("signal"),
            )
            continue
        try:
            stop_guest(api, kind, vmid)
            result["stopped"].append(vmid)
            audit(
                "orphan-guest-stopped",
                kind=kind,
                vmid=vmid,
                lease_tag=guest.get("lease_tag"),
                reason="no lease record or retained registry entry",
                **guest_load(guest.get("load")),
            )
        except LabError as exc:
            result["failed"][str(vmid)] = str(exc)[:300]
            audit(
                "orphan-guest-stop-failed", kind=kind, vmid=vmid,
                error=str(exc)[:300],
            )
    return result


def cmd_reclaim_orphans_only(args: argparse.Namespace) -> dict[str, Any]:
    """Stop orphaned guests and do nothing else.

    Reclamation was only reachable as part of a full expiry sweep, which in the
    same run finalizes every expired lease -- deleting their guests -- and then
    decides whether to power the host off. "Stop the guests nothing owns" is a
    much smaller intention than that, and wanting one is not consenting to the
    other, so it gets its own path: no lease is finalized, no backup runs, and
    the host is left exactly as it was.
    """
    if not args.host_change_authorized:
        raise LabError(
            "--orphans-only stops guests this controller has no record of. "
            "Re-run with --host-change-authorized once the user has asked for "
            "that. 'guest inventory --orphaned-only' lists them first."
        )
    api = ProxmoxAPI()
    if not api.reachable():
        raise LabError(
            "the host is not reachable, so there is nothing running to reclaim"
        )
    with controller_lock():
        reclaimed = reclaim_orphans(
            api, include_active=getattr(args, "include_active", False)
        )
    if reclaimed["stopped"]:
        audit(
            "orphans-reclaimed",
            stopped=reclaimed["stopped"],
            already_stopped=reclaimed["already_stopped"],
            failed=sorted(reclaimed["failed"]),
        )
    result: dict[str, Any] = {
        "reclaimed_orphans": reclaimed,
        "leases_swept": [],
        "host_powered_off": False,
        "note": (
            "Only orphaned guests were touched. No lease was finalized and the "
            "host was left on; a normal 'cleanup-expired' run makes those "
            "decisions."
        ),
    }
    if reclaimed["left_active"]:
        result["left_active_note"] = (
            "These are running and were touched recently, so something is "
            "using them even though this controller has no record of them -- "
            "another controller drives guests through the same token. Pass "
            "--include-active to stop them anyway."
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    if reclaimed["failed"]:
        raise LabError("One or more orphaned guests could not be stopped")
    return result


def cmd_cleanup_expired(args: argparse.Namespace) -> None:
    if getattr(args, "orphans_only", False):
        cmd_reclaim_orphans_only(args)
        return
    api = ProxmoxAPI()
    cleaned: list[str] = []
    retried: list[str] = []
    failed: dict[str, list[str]] = {}
    transferred: dict[str, list[str]] = {}
    with controller_lock():
        now = utc_now()
        for lease in cleanup_candidate_leases():
            if is_long_term(lease):
                continue          # never expires, never swept
            if lease.get("state") == "cleanup_failed":
                # Already past its end and known incomplete: a retry is
                # exactly what it needs, whatever its expiry says.
                retried.append(lease["id"])
            elif not args.all and parse_expiry(lease["expires_at"]) > now:
                continue
            if not api.reachable() and lease_requires_cleanup(lease):
                ensure_on(api)
            if api.reachable():
                failures = finalize_lease(api, lease)
            else:
                failures = []
                lease["state"] = "closed"
                lease["closed_at"] = iso_now()
                save_lease(lease)
            if lease.get("transferred_resources"):
                transferred[lease["id"]] = lease["transferred_resources"]
            if failures:
                failed[lease["id"]] = failures
            else:
                cleaned.append(lease["id"])
        reclaimed: dict[str, Any] | None = None
        if getattr(args, "reclaim_orphans", False) and api.reachable():
            if not args.host_change_authorized:
                raise LabError(
                    "--reclaim-orphans stops guests this controller has no "
                    "record of. Re-run with --host-change-authorized once the "
                    "user has asked for that. 'status' lists them first."
                )
            reclaimed = reclaim_orphans(
                api, include_active=getattr(args, "include_active", False)
            )
        remaining = active_leases()
        persistent = [x for x in remaining if is_long_term(x)]
        if persistent and api.reachable() and not args.no_backup:
            # Weekly backups ride along with the watchdog, so a long-term
            # lease needs no separate schedule.
            try:
                from . import longterm
                backup_args = argparse.Namespace(
                    storage=None, keep=None,
                    interval_days=longterm.BACKUP_INTERVAL_DAYS,
                    force=False, timeout=7200,
                )
                with contextlib.redirect_stdout(io.StringIO()):
                    longterm.cmd_backup(_module(), backup_args)
            except (LabError, OSError) as exc:
                print(f"warning: long-term backup sweep failed: {exc}",
                      file=sys.stderr)
        host_powered_off = False
        idle_seconds = int(mcp_idle_elapsed())
        idle_shutdown_triggered = False
        if persistent:
            # The whole promise of a long-term lease: the machine stays up.
            pass
        elif not remaining and (cleaned or args.all):
            host_powered_off = shutdown_host(api)
        elif idle_shutdown_due(
            reachable=api.reachable(),
            active_lease_count=len(remaining),
            has_failures=bool(failed),
            idle_seconds=idle_seconds,
        ):
            idle_shutdown_triggered = True
            audit(
                "mcp-idle-shutdown-triggered",
                idle_seconds=idle_seconds,
                threshold_seconds=MCP_IDLE_SHUTDOWN_SECONDS,
            )
            host_powered_off = shutdown_host(api)
        # The watchdog runs every five minutes. Recording a no-op sweep would
        # append a journal line and a Forgejo commit each time, burying real
        # events under thousands of identical entries, so stay silent unless
        # the sweep actually did or failed something.
        if (cleaned or failed or idle_shutdown_triggered or host_powered_off
                or (reclaimed and reclaimed["stopped"])):
            audit(
                "cleanup-expired",
                cleaned=cleaned,
                retried=retried,
                failed=failed,
                transferred=transferred,
                remaining=[x["id"] for x in remaining],
                idle_seconds=idle_seconds,
                idle_shutdown_triggered=idle_shutdown_triggered,
                host_powered_off=host_powered_off,
            )
    # Deliberately after the controller lock is released: a vzdump can run for
    # hours, and nothing else may queue behind it.
    retained_backup: dict[str, Any] | None = None
    if not args.no_backup and not host_powered_off and api.reachable():
        from . import longterm as longterm_module

        if longterm_module.retained_backup_enabled():
            with sweep_lock("retained-backup") as acquired:
                if not acquired:
                    retained_backup = {
                        "skipped": "a previous backup sweep is still running"
                    }
                else:
                    try:
                        retained_backup = longterm_module.backup_retained(
                            _module(), api,
                            storage=longterm_module.backup_storage(_module()),
                            keep=int(
                                CONFIG.lease.get("long_term_backup_keep", 2)
                            ),
                            timeout=7200,
                            interval_days=int(CONFIG.lease.get(
                                "retained_backup_interval_days", 7)),
                        )
                    except (LabError, OSError) as exc:
                        retained_backup = {"error": str(exc)[:300]}
    print(
        json.dumps(
            {
                "cleaned": cleaned,
                "retried": retried,
                "failed": failed,
                "left_to_another_lease": transferred,
                "retained_backup": retained_backup,
                "remaining": [x["id"] for x in remaining],
                "mcp_idle_seconds": idle_seconds,
                "mcp_idle_shutdown_after_seconds": MCP_IDLE_SHUTDOWN_SECONDS,
                "idle_shutdown_triggered": idle_shutdown_triggered,
                "host_powered_off": host_powered_off,
                **({"reclaimed_orphans": reclaimed} if reclaimed else {}),
            },
            indent=2,
            sort_keys=True,
        )
    )
    if failed:
        raise LabError("One or more expired leases could not be cleaned")
    if reclaimed and reclaimed["failed"]:
        raise LabError("One or more orphaned guests could not be stopped")


def cmd_init(args: argparse.Namespace) -> None:
    """Write a starter config file."""
    target = Path(args.path).expanduser() if args.path else CONFIG.intended
    if target.exists() and not args.force:
        raise LabError(f"{target} already exists; pass --force to overwrite")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(config_module.TEMPLATE)
    print(json.dumps(
        {
            "config": str(target),
            "next": [
                f"edit {target} -- set [proxmox] host, node, token_user, "
                "token_name, and [power] mac",
                "proxmox-lab secrets set proxmox-token",
                "proxmox-lab doctor",
            ],
        },
        indent=2,
    ))


def cmd_secrets(args: argparse.Namespace) -> None:
    if args.secrets_command == "list":
        print(json.dumps(secrets_store.status(CONFIG), indent=2, sort_keys=True))
        return
    if args.secrets_command == "set":
        if args.name not in secrets_store.KNOWN_SECRETS and not args.allow_unknown:
            raise LabError(
                f"unknown secret {args.name!r}. Known: "
                f"{', '.join(sorted(secrets_store.KNOWN_SECRETS))}. "
                "Use --allow-unknown to store it anyway."
            )
        if args.stdin:
            value = sys.stdin.readline().rstrip("\r\n")
        else:
            import getpass
            value = getpass.getpass(f"{args.name}: ")
        if not value:
            raise LabError("refusing to store an empty secret")
        try:
            backend = secrets_store.store(CONFIG, args.name, value)
        except secrets_store.SecretError as exc:
            raise LabError(str(exc)) from None
        print(json.dumps({"stored": args.name, "backend": backend}))


def retained_backup_coverage() -> dict[str, Any]:
    """How stale each retained guest's last backup is.

    Backups used to happen only as a side effect of an active long-term lease,
    so the keep-forever guests -- templates, persistent workers -- could have
    none at all. Whether or not the sweep is enabled, the drift is reported.
    """
    entries = inventory_module.entries(STATE_ROOT)
    if not entries:
        return {}
    now = utc_now()
    never: list[int] = []
    oldest_days: float | None = None
    for item in entries.values():
        last = item.get("last_backup_at")
        if not last:
            never.append(int(item.get("vmid", 0)))
            continue
        try:
            age = (now - parse_expiry(str(last))).total_seconds() / 86400
        except (TypeError, ValueError):
            never.append(int(item.get("vmid", 0)))
            continue
        oldest_days = age if oldest_days is None else max(oldest_days, age)
    coverage: dict[str, Any] = {
        "retained_guests": len(entries),
        "sweep_enabled": bool(CONFIG.lease.get("retained_backup", False)),
        "never_backed_up": sorted(never),
        "oldest_backup_age_days": (
            round(oldest_days, 1) if oldest_days is not None else None
        ),
    }
    if never and not coverage["sweep_enabled"]:
        coverage["note"] = (
            "These guests are meant to be kept but have no backup. Enable "
            "[lease] retained_backup, run 'proxmox-lab backup --retained "
            "--force', or accept the risk deliberately."
        )
    return coverage


def host_update_report() -> dict[str, Any]:
    """Pending package updates and reboot state on the lab node.

    The node is an ordinary Debian/PVE host that needs patching, but the whole
    workflow is lease-in, work, power-off -- so nobody ever sees `apt` drift.
    This is advisory only: a pending security update is a thing to schedule
    between leases, not a reason for `doctor` to fail. Reached over the opt-in
    `[memflow]` host SSH channel, because there is no API for it.
    """
    from . import memflow

    if not memflow.host_ssh_enabled():
        return {
            "checked": False,
            "reason": "needs the opt-in [memflow] host SSH channel",
        }
    report: dict[str, Any] = {"checked": True}
    try:
        simulated = memflow.host_run(
            _module(),
            ["sh", "-c", "LC_ALL=C apt-get -s -o Debug::NoLocking=1 upgrade"],
            timeout=120,
        )
    except LabError as exc:
        return {"checked": False, "reason": str(exc)[:200]}
    if simulated.returncode:
        return {
            "checked": False,
            "reason": (simulated.stderr or simulated.stdout or "").strip()[:200],
        }
    lines = (simulated.stdout or "").splitlines()
    upgrades = [line for line in lines if line.startswith("Inst ")]
    report["updates_pending"] = len(upgrades)
    report["security_updates"] = any(
        "-security" in line or "Debian-Security" in line for line in upgrades
    )
    try:
        reboot = memflow.host_run(
            _module(),
            ["sh", "-c", "test -e /var/run/reboot-required && echo yes || echo no"],
            timeout=30,
        )
        report["reboot_required"] = (reboot.stdout or "").strip() == "yes"
    except LabError:
        report["reboot_required"] = None
    report["remediation"] = (
        "Patch and reboot between leases, never during one. Updating the node "
        "is a host change and is deliberately not automated here."
    )
    return report


def cmd_doctor(args: argparse.Namespace) -> None:
    """Check the install end to end and say exactly what is missing."""
    problems: list[str] = []
    if CONFIG_ERROR:
        problems.append(f"config could not be read: {CONFIG_ERROR}")
    report: dict[str, Any] = {
        "controller_version": __version__,
        "config_file": str(CONFIG.source) if CONFIG.source else None,
        "config_expected_at": str(CONFIG.intended),
        "state_dir": str(STATE_ROOT),
        "journal_dir": str(JOURNAL_ROOT),
        "audit": {
            "ledger": ledger().describe() if ledger() else None,
        },
    }
    if ledger() is None:
        problems.append(
            "[audit] no ledger configured. Run 'proxmox-lab journal "
            "host-setup' to provision MariaDB on the Proxmox host."
        )
    spool = journal_module.spool_path(JOURNAL_ROOT)
    try:
        spooled: int | None = sum(
            1 for line in spool.read_text().splitlines() if line.strip()
        ) if spool.exists() else 0
    except OSError as exc:
        spooled = None
        problems.append(f"audit spool at {spool} could not be read: {exc}")
    report["audit"]["spooled_records"] = spooled
    if spooled:
        problems.append(
            f"{spooled} audit record(s) are still spooled locally at {spool}: "
            "the ledger was unreachable. Upload the backlog with "
            "'proxmox-lab journal --flush-spool'"
        )
    if CONFIG.unknown_sections:
        report["unknown_sections"] = CONFIG.unknown_sections
        problems.append(
            "config has section(s) this version does not know, and they were "
            f"ignored: {', '.join(CONFIG.unknown_sections)}"
        )
    if not CONFIG.configured and not CONFIG_ERROR:
        problems.append(
            f"no config file at {CONFIG.intended}; run 'proxmox-lab init'"
        )
    for key in ("host", "node", "token_user", "token_name"):
        if not getattr(CONFIG.proxmox, key):
            problems.append(f"[proxmox] {key} is not set")
    report["proxmox"] = {
        "host": HOST or None, "node": NODE or None,
        "token": f"{TOKEN_USER}!{TOKEN_NAME}" if TOKEN_USER else None,
        "verify_tls": VERIFY_TLS,
    }

    backend = secrets_store.detect_backend() \
        if CONFIG.secrets.get("backend", "auto") in ("", "auto") \
        else CONFIG.secrets.get("backend")
    report["secrets_backend"] = backend
    try:
        secrets_store.get(CONFIG, "proxmox-token")
        report["proxmox_token_stored"] = True
    except secrets_store.SecretError:
        report["proxmox_token_stored"] = False
        problems.append("Proxmox API token not stored; run "
                        "'proxmox-lab secrets set proxmox-token'")

    settings = ledger()
    if settings is not None:
        report["ledger_reachable"] = mariadb_module.ping(settings)
        if not report["ledger_reachable"]:
            # Not a problem: the lab host is off between leases by design, and
            # events spool until it is back.
            report["ledger_note"] = (
                "ledger unreachable -- expected when the lab host is powered "
                "off; events spool locally until it returns"
            )
        # The spool backlog is reported once, below, where it is counted.
    mode = CONFIG.power.get("mode")
    report["power"] = {
        "mode": mode,
        "can_force_off": power_module.can_force_off(CONFIG),
        "boot_timeout_seconds": int(
            CONFIG.power.get("boot_timeout_seconds", 300)
        ),
        "minimum_cold_boot_timeout_seconds": MIN_COLD_BOOT_TIMEOUT_SECONDS,
    }
    if mode == "wake-on-lan" and not CONFIG.power.get("mac"):
        problems.append("[power] mac is not set (needed for wake-on-lan)")
    if mode == "none":
        report["power"]["note"] = "no remote power-on; start the machine yourself"

    if HOST and NODE and report.get("proxmox_token_stored"):
        api = ProxmoxAPI()
        reachable = api.reachable()
        report["proxmox_reachable"] = reachable
        if reachable:
            try:
                permissions = api.call("GET", "/access/permissions") or {}
                scope: dict[str, Any] = {}
                for path in (f"/nodes/{NODE}", "/vms", "/"):
                    scope.update(permissions.get(path, {}) or {})
                needed = ("VM.Allocate", "VM.Config.Disk", "VM.PowerMgmt",
                          "VM.Console", "VM.Audit")
                missing = [name for name in needed if not scope.get(name)]
                report["privileges_missing"] = missing
                if missing:
                    problems.append(
                        "API token lacks: " + ", ".join(missing)
                        + " (grant PVEVMAdmin on /vms)"
                    )
            except LabError as exc:
                problems.append(f"could not read permissions: {exc}")
        else:
            report["proxmox_reachable"] = False
            report["note"] = ("host unreachable -- expected when the machine "
                              "is powered off")

    if HOST and NODE and report.get("proxmox_token_stored") \
            and report.get("proxmox_reachable"):
        api = ProxmoxAPI()
        try:
            described = describe_guests(api)
        except LabError as exc:
            report["inventory_error"] = str(exc)[:200]
            described = []
        orphans = inventory_module.orphans(described)
        running_orphans = [x for x in orphans if x["status"] == "running"]
        report["guests"] = {
            "total": len(described),
            "retained": sum(1 for x in described if x["retained"]),
            "orphaned": len(orphans),
            "orphaned_running": [x["vmid"] for x in running_orphans],
        }
        active = {}
        idle = []
        for orphan in running_orphans:
            signal = recent_guest_activity(
                api, orphan["kind"], int(orphan["vmid"]),
                record=orphan.get("load"),
            )
            if signal:
                active[str(orphan["vmid"])] = signal
            else:
                idle.append(orphan)
        if active:
            # Running, unowned *here*, and being driven -- so another
            # controller owns it. Saying "nothing will clean this up" would be
            # a misdiagnosis, and acting on it would stop somebody's work.
            report["guests"]["orphaned_but_active"] = active
            report["guests"]["active_note"] = (
                "Running and touched recently, so something is using these "
                "even though this controller has no record of them -- most "
                "likely another controller sharing the API token. They keep "
                "the host on, which is correct while they are in use."
            )
        if idle:
            report["guests"]["orphaned_idle_load"] = {
                str(x["vmid"]): guest_load(x.get("load")) for x in idle
            }
            # This is the failure mode that keeps the machine on for days:
            # nothing owns the guest, so no sweep stops it, and shutdown_host
            # refuses while any guest runs.
            problems.append(
                f"{len(idle)} running guest(s) carry a lease tag this "
                "controller has no record of and show no recent activity, so "
                "no sweep will clean them up and the host cannot power off: "
                + ", ".join(str(x["vmid"]) for x in idle)
                + ". Reclaim with 'cleanup-expired --orphans-only "
                "--host-change-authorized'"
            )
        elif orphans and not active:
            report["guests"]["note"] = (
                f"{len(orphans)} stopped guest(s) are tagged with a lease this "
                "controller no longer has; see 'guest inventory --orphaned-only'"
            )
        coverage = retained_backup_coverage()
        if coverage:
            report["retained_backup"] = coverage
    if getattr(args, "host_checks", False):
        report["host"] = host_update_report()
    report["problems"] = problems
    report["ok"] = not problems
    print(json.dumps(report, indent=2, sort_keys=True))
    if problems:
        raise LabError(f"{len(problems)} problem(s) found")


def _guard_install_block(hostguard_module: Any) -> str:
    """Shell that writes the lease guard onto the host and starts its timer.

    The heredoc delimiter is quoted, so the shell expands nothing inside it and
    the script travels verbatim.
    """
    return (
        "cat > /usr/local/lib/pxl-hostguard.py <<'PXLGUARD'\n"
        + hostguard_module.GUARD_SCRIPT
        + "\nPXLGUARD\nchmod 755 /usr/local/lib/pxl-hostguard.py\n"
        + hostguard_module.GUARD_UNITS
    )


def _provision_ledger(args: argparse.Namespace) -> dict[str, Any]:
    """Provision MariaDB on the Proxmox host and seed the shared secrets.

    A persistent, unprivileged container marked onboot, published on the
    hypervisor's own address. Deliberately not lease-owned: the ledger has to
    outlive the leases it records, so lease-end must never destroy it.
    """
    if not args.host_change_authorized:
        raise LabError(
            "provisioning the audit ledger creates a container and a NAT rule "
            "on the Proxmox host. Re-run with --host-change-authorized only "
            "when the user asked for it."
        )
    from . import hostguard as hostguard_module
    from . import memflow as memflow_module

    ctid = args.ctid or 9310
    storage = args.storage or str(CONFIG.storage.get("bulk_storage") or "local-lvm")
    existing = secrets_store.get(
        CONFIG, secrets_store.BOOTSTRAP_SECRET, required=False
    )
    password = existing or secrets.token_urlsafe(24)
    script = (
        mariadb_module.HOST_SETUP_SCRIPT
        .replace("__CTID__", str(ctid))
        .replace("__STORAGE__", storage)
        .replace("__BRIDGE__", str(args.bridge))
        .replace("__DBNAME__", str(CONFIG.audit.get("database") or "proxmox_lab"))
        .replace("__DBUSER__", str(CONFIG.audit.get("user") or "proxmox_lab"))
        .replace("__DBPASS__", password)
        .replace("__GUARD_INSTALL__", _guard_install_block(hostguard_module))
    )
    memflow_module._require_enabled(_module())
    proc = memflow_module._ssh(
        _module(), ["bash", "-s"], timeout=args.timeout, stdin=script
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode not in (0, None) or "ledger-ready" not in output:
        raise LabError(
            "audit ledger provisioning failed: " + output.strip()[-800:]
        )

    settings = journal_module.settings_from_config(CONFIG, password)
    if settings is None:
        raise LabError("ledger provisioned but [audit] host is not resolvable")
    mariadb_module.ensure_schema(settings)

    # Seed the shared store so a second controller needs only this password.
    seeded = _seed_shared_secrets(settings)
    env_var = secrets_store._env_name(secrets_store.BOOTSTRAP_SECRET)
    global _LEDGER_CACHE
    _LEDGER_CACHE = settings
    return {
        "ctid": ctid,
        "ledger": settings.describe(),
        "reachable": mariadb_module.ping(settings),
        "shared_secrets_seeded": seeded,
        "bootstrap_env_var": env_var,
        # Printed once, here, because there is nowhere else to get it: MariaDB
        # keeps only a hash, and this is the credential every other controller
        # needs. Re-running host-setup with it already in the environment keeps
        # the same one rather than rotating it.
        "bootstrap_export": f"export {env_var}='{password}'",
        "bootstrap_password_was_generated": not existing,
        "next": [
            f"Put this in the environment of every controller:  export {env_var}=...",
            "proxmox-lab journal --migrate    # carry this machine's history over",
        ],
        "host_output": output.strip()[-400:],
    }


def _seed_shared_secrets(settings: Any) -> list[str]:
    """Copy this controller's secrets into the shared store, once.

    This is what makes adding a machine a one-liner. Only secrets this
    controller can actually read are copied, and an existing shared value is
    never overwritten -- the first controller to set one wins.
    """
    now = utc_now().isoformat().replace("+00:00", "Z")
    existing = {row["name"] for row in mariadb_module.list_secrets(settings)}
    seeded: list[str] = []
    for name in secrets_store.KNOWN_SECRETS:
        if name == secrets_store.BOOTSTRAP_SECRET or name in existing:
            continue
        try:
            value = secrets_store.get(CONFIG, name, required=False)
        except secrets_store.SecretError:
            value = ""
        if not value:
            # An upgraded controller may still hold this only in the OS
            # keystore it used before secrets moved to the environment.
            legacy = secrets_store.legacy_keystore()
            value = (legacy and secrets_store.read_legacy(legacy, name)) or ""
        if not value:
            continue
        mariadb_module.put_secret(
            settings, name, value,
            updated_by=_controller_id(), updated_at=now,
        )
        seeded.append(name)
    return seeded


def cmd_journal(args: argparse.Namespace) -> None:
    """Read the shared audit ledger, or carry an old local one into it."""
    if args.host_setup:
        result = _provision_ledger(args)
        print(json.dumps(result, indent=2, sort_keys=True))
        return

    settings = ledger()
    if settings is None:
        raise LabError(
            "no audit ledger configured. Run 'proxmox-lab journal host-setup' "
            "to provision MariaDB on the Proxmox host."
        )

    if args.flush_spool:
        with controller_lock():
            result = journal_module.flush_spool(
                settings, JOURNAL_ROOT, controller=_controller_id()
            )
        print(json.dumps(result, indent=2, sort_keys=True))
        return

    if args.migrate:
        with controller_lock():
            result = journal_module.migrate_legacy(
                settings, JOURNAL_ROOT, controller=_controller_id()
            )
        print(json.dumps(result, indent=2, sort_keys=True))
        return

    if args.migrations:
        print(json.dumps(
            {"migrations": mariadb_module.migrations(settings)},
            indent=2, sort_keys=True, default=str,
        ))
        return

    if args.summary:
        print(json.dumps(
            journal_module.summary(settings),
            indent=2, sort_keys=True, default=str,
        ))
        return

    events = journal_module.query(
        settings,
        limit=args.limit,
        lease=args.lease,
        event=args.event,
        since=args.since,
        controller=args.controller,
    )
    print(json.dumps(events, indent=2, sort_keys=True, default=str))


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
