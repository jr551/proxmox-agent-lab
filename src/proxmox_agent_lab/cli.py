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
import fcntl
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
from . import power as power_module
from . import journal as journal_module
from . import pocketbase as pocketbase_module
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
AUDIT_BACKEND = CONFIG.audit.get("backend", "sqlite")
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
UPLOAD_STORAGES = tuple(CONFIG.storage.upload_storages)
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
    if isinstance(last, (int, float)) and checked_at - last < UPDATE_CHECK_INTERVAL_SECONDS:
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


@contextlib.contextmanager
def controller_lock() -> Any:
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


def sync_repo(record: dict[str, Any], suffix: str) -> None:
    """Optionally copy one audit record to a private git log repository.

    Off by default: most people do not want their lab's audit trail pushed
    anywhere. Enable with [audit] git_sync = true and git_repo = "<path>".
    """
    if not CONFIG.audit.get("git_sync"):
        return
    configured = CONFIG.audit.get("git_repo")
    if not configured:
        print(
            "warning: [audit] git_sync is on but git_repo is empty; "
            "journal not pushed",
            file=sys.stderr,
        )
        return
    try:
        journal_module.sync_git(
            Path(configured),
            record,
            suffix,
            str(CONFIG.audit.get("git_branch") or "logs"),
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"warning: journal sync failed: {str(exc)[:300]}", file=sys.stderr)




def pocketbase_client() -> pocketbase_module.Client:
    url = str(CONFIG.audit.get("pocketbase_url") or "").strip()
    collection = str(
        CONFIG.audit.get("pocketbase_collection") or "proxmox_lab_events"
    ).strip()
    secret_name = str(
        CONFIG.audit.get("pocketbase_token_secret") or "audit-token"
    ).strip()
    try:
        timeout = float(CONFIG.audit.get("pocketbase_timeout_seconds", 10))
    except (TypeError, ValueError):
        raise LabError(
            "[audit] pocketbase_timeout_seconds must be a positive number"
        ) from None
    if not url:
        raise LabError("[audit] pocketbase_url is not set")
    try:
        token = secrets_store.get(CONFIG, secret_name)
    except secrets_store.SecretError as exc:
        raise LabError(str(exc)) from None
    try:
        return pocketbase_module.Client(
            url, token, collection, timeout=timeout
        )
    except ValueError as exc:
        raise LabError(str(exc)) from None
def audit(event: str, *, sync: bool = True, **fields: Any) -> None:
    now = utc_now()
    record = {
        "timestamp": now.isoformat().replace("+00:00", "Z"),
        "event": event,
        **redact(fields),
    }
    client: pocketbase_module.Client | None = None
    if AUDIT_BACKEND == "pocketbase":
        record["event_id"] = uuid.uuid4().hex
        record["controller"] = str(
            CONFIG.audit.get("controller_id") or socket.gethostname()
        )
        client = pocketbase_client()
    journal_module.append(
        JOURNAL_ROOT, AUDIT_BACKEND, record, pocketbase=client
    )
    if sync:
        sync_repo(record, event)


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


_AUDIT_THROUGH_BOOT_RETRY_SECONDS = 30


def _audit_through_boot(event: str, **fields: Any) -> None:
    """audit(), tolerant of a just-woken host's own audit backend still
    starting up.

    The audit backend can itself be hosted on this same lab host (see
    pocketbase-host-setup.sh) -- an onboot LXC that starts alongside Proxmox
    but is not necessarily answering the instant the API is. A short bounded
    retry covers that normal startup race; if the backend is still
    unreachable after it, the event is dropped with a loud warning rather
    than failing the power-on itself, since losing one audit line is
    recoverable and failing to confirm the host is up is not.
    """
    deadline = time.monotonic() + _AUDIT_THROUGH_BOOT_RETRY_SECONDS
    while True:
        try:
            audit(event, **fields)
            return
        except pocketbase_module.PocketBaseError as exc:
            if time.monotonic() >= deadline:
                print(
                    f"warning: '{event}' was not recorded to the audit "
                    f"backend (still unreachable {_AUDIT_THROUGH_BOOT_RETRY_SECONDS}s "
                    f"after power-on): {exc}",
                    file=sys.stderr,
                )
                return
            time.sleep(3)


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
            sync=False,
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


def delete_guest(api: ProxmoxAPI, kind: str, vmid: int) -> None:
    try:
        _delete_guest(api, kind, vmid, destroy_unreferenced_disks=True)
    except LabError as exc:
        if _guest_is_gone(exc):
            return
        if not _storage_io_error(exc):
            raise
        try:
            # Destroying unreferenced disks makes Proxmox inspect every
            # configured storage. An unrelated failed device must not strand
            # a lease whose guest can otherwise be deleted.
            _delete_guest(api, kind, vmid, destroy_unreferenced_disks=False)
        except LabError as retry_exc:
            if _guest_is_gone(retry_exc):
                return
            raise LabError(
                f"Could not delete {kind}/{vmid} after retrying without "
                f"unreferenced-disk cleanup: {retry_exc}; initial storage "
                f"error: {exc}"
            ) from retry_exc


def active_leases(excluding: str | None = None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for path in sorted(LEASE_ROOT.glob("*.json")) if LEASE_ROOT.exists() else []:
        try:
            lease = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if lease.get("state") == "active" and lease.get("id") != excluding:
            result.append(lease)
    return result


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
    )


def shutdown_host(api: ProxmoxAPI) -> bool:
    """Shut the lab machine down and confirm it actually went off."""
    if not api.reachable():
        audit("lab-power-off-already-verified", host=HOST, node=NODE)
        return True
    running = running_guest_vmids(api)
    if running:
        audit("lab-power-off-blocked-by-running-guest", host=HOST, node=NODE,
              vmids=running, sync=False)
        return False
    try:
        task = api.call("POST", f"/nodes/{NODE}/status", {"command": "shutdown"})
        audit("lab-graceful-shutdown-requested", node=NODE, task_id=task,
              sync=False)
    except LabError as exc:
        audit("lab-graceful-shutdown-request-failed", error=str(exc), sync=False)
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
        audit("lab-emergency-force-off-requested", sync=False, **detail)
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
            raise LabError("--password-stdin received an empty password")
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
        if resource:
            kind, vmid = resource
            owned = any(
                item["kind"] == kind and int(item["vmid"]) == vmid
                for item in lease["resources"]
            )
            if method == "DELETE" and not owned:
                raise LabError(
                    f"Refusing deletion of unregistered {kind} VMID {vmid}"
                )
            power_action = re.fullmatch(
                rf"/nodes/{re.escape(NODE)}/(qemu|lxc)/\d+/status/"
                r"(?:start|stop|shutdown|reset|suspend)",
                args.path,
            )
            if power_action and not owned and vmid in lease["initial_vmids"]:
                raise LabError(f"VMID {vmid} existed before this lease")
        create_match = re.fullmatch(
            rf"/nodes/{re.escape(NODE)}/(qemu|lxc)/?", args.path
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
    result = api.call(method, args.path, data)
    task_status = None
    if args.wait_task and isinstance(result, str) and result.startswith("UPID:"):
        task_status = wait_task(api, result, timeout=args.task_timeout)
    if write and lease:
        create_match = re.fullmatch(
            rf"/nodes/{re.escape(NODE)}/(qemu|lxc)/?", args.path
        )
        if method == "POST" and create_match:
            kind_created = create_match.group(1)
            created_vmid = int(data["vmid"])
            policy = "retain" if is_long_term(lease) else args.policy
            # Reload inside the lock: the snapshot taken before api.call is
            # stale, and an unlocked save clobbers concurrent creations'
            # registrations. Every other lease mutator serializes here too.
            with controller_lock():
                fresh = load_lease(args.lease)
                register_resource(
                    fresh, kind_created, created_vmid, policy,
                    data.get("name") or data.get("hostname"),
                )
            if is_long_term(lease):
                from . import longterm
                try:
                    longterm.set_protection(
                        _module(), api, kind_created, created_vmid, True
                    )
                except LabError as exc:
                    print(f"warning: could not protect {created_vmid}: {exc}",
                          file=sys.stderr)
        audit(
            "proxmox-api-write",
            lease=args.lease,
            method=method,
            path=args.path,
            data=data,
            result=result,
            task_status=task_status,
        )
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
    print(
        json.dumps(
            redact({"data": result, "task_status": task_status}),
            indent=2,
            sort_keys=True,
        )
    )


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
            [
                "curl",
                "--config",
                config.name,
                "--insecure",
                "--request",
                "POST",
                "--form",
                f"content={args.content}",
                "--form",
                f"filename=@{source}",
                f"{API_ROOT}/nodes/{NODE}/storage/{args.storage}/upload",
            ],
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
    for resource in reversed(lease.get("resources", [])):
        kind = resource["kind"]
        vmid = int(resource["vmid"])
        try:
            stop_guest(api, kind, vmid)
            if resource.get("policy", "delete") == "delete":
                delete_guest(api, kind, vmid)
            audit(
                "lease-resource-finalized",
                lease=lease["id"],
                kind=kind,
                vmid=vmid,
                policy=resource.get("policy", "delete"),
                sync=False,
            )
        except LabError as exc:
            failures.append(f"{kind}/{vmid}: {exc}")
            audit(
                "lease-resource-finalize-failed",
                lease=lease["id"],
                kind=kind,
                vmid=vmid,
                error=str(exc),
                sync=False,
            )
    lease["state"] = "closed" if not failures else "cleanup_failed"
    lease["closed_at"] = iso_now()
    lease["failures"] = failures
    save_lease(lease)
    return failures


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
        )
    result: dict[str, Any] = {
        "lease": args.lease,
        "failures": failures,
        "remaining_active_leases": [x["id"] for x in others],
        "host_powered_off": host_powered_off,
    }
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


def cmd_cleanup_expired(args: argparse.Namespace) -> None:
    api = ProxmoxAPI()
    cleaned: list[str] = []
    failed: dict[str, list[str]] = {}
    with controller_lock():
        now = utc_now()
        for lease in active_leases():
            if is_long_term(lease):
                continue          # never expires, never swept
            if not args.all and parse_expiry(lease["expires_at"]) > now:
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
            if failures:
                failed[lease["id"]] = failures
            else:
                cleaned.append(lease["id"])
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
                sync=False,
            )
            host_powered_off = shutdown_host(api)
        # The watchdog runs every five minutes. Recording a no-op sweep would
        # append a journal line and a Forgejo commit each time, burying real
        # events under thousands of identical entries, so stay silent unless
        # the sweep actually did or failed something.
        if cleaned or failed or idle_shutdown_triggered or host_powered_off:
            audit(
                "cleanup-expired",
                cleaned=cleaned,
                failed=failed,
                remaining=[x["id"] for x in remaining],
                idle_seconds=idle_seconds,
                idle_shutdown_triggered=idle_shutdown_triggered,
                host_powered_off=host_powered_off,
            )
    print(
        json.dumps(
            {
                "cleaned": cleaned,
                "failed": failed,
                "remaining": [x["id"] for x in remaining],
                "mcp_idle_seconds": idle_seconds,
                "mcp_idle_shutdown_after_seconds": MCP_IDLE_SHUTDOWN_SECONDS,
                "idle_shutdown_triggered": idle_shutdown_triggered,
                "host_powered_off": host_powered_off,
            },
            indent=2,
            sort_keys=True,
        )
    )
    if failed:
        raise LabError("One or more expired leases could not be cleaned")


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


def cmd_doctor(args: argparse.Namespace) -> None:
    """Check the install end to end and say exactly what is missing."""
    problems: list[str] = []
    if CONFIG_ERROR:
        problems.append(f"config could not be read: {CONFIG_ERROR}")
    report: dict[str, Any] = {
        "config_file": str(CONFIG.source) if CONFIG.source else None,
        "config_expected_at": str(CONFIG.intended),
        "state_dir": str(STATE_ROOT),
        "journal_dir": str(JOURNAL_ROOT),
        "audit": {
            "local_backend": AUDIT_BACKEND,
            # Git sync is an independent, redacted JSONL export. It remains
            # valid when the richer local ledger is SQLite.
            "git_sync": bool(CONFIG.audit.get("git_sync")),
            "git_repo": (
                str(CONFIG.audit.get("git_repo") or "")
                if CONFIG.audit.get("git_sync") else None
            ),
            "git_branch": (
                str(CONFIG.audit.get("git_branch") or "logs")
                if CONFIG.audit.get("git_sync") else None
            ),
        },
    }
    if AUDIT_BACKEND not in {"sqlite", "jsonl", "pocketbase"}:
        problems.append(f"[audit] unsupported backend: {AUDIT_BACKEND}")
    if AUDIT_BACKEND == "pocketbase":
        report["audit"]["pocketbase"] = {
            "url": str(CONFIG.audit.get("pocketbase_url") or ""),
            "collection": str(
                CONFIG.audit.get("pocketbase_collection")
                or "proxmox_lab_events"
            ),
            "token_secret": str(
                CONFIG.audit.get("pocketbase_token_secret")
                or "audit-token"
            ),
        }
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

    if AUDIT_BACKEND == "pocketbase":
        try:
            secrets_store.get(
                CONFIG,
                str(
                    CONFIG.audit.get("pocketbase_token_secret")
                    or "audit-token"
                ),
            )
            report["pocketbase_token_stored"] = True
        except secrets_store.SecretError:
            report["pocketbase_token_stored"] = False
            problems.append(
                "PocketBase token not stored; run "
                "'proxmox-lab secrets set audit-token'"
            )
        if not CONFIG.audit.get("pocketbase_url"):
            problems.append("[audit] pocketbase_url is not set")
        elif report.get("pocketbase_token_stored"):
            try:
                client = pocketbase_client()
                client.get_collection()
                report["pocketbase_collection_reachable"] = True
            except (LabError, pocketbase_module.PocketBaseError) as exc:
                report["pocketbase_collection_reachable"] = False
                problems.append(
                    f"PocketBase audit collection check failed: {exc}"
                )
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

    report["problems"] = problems
    report["ok"] = not problems
    print(json.dumps(report, indent=2, sort_keys=True))
    if problems:
        raise LabError(f"{len(problems)} problem(s) found")


def cmd_journal(args: argparse.Namespace) -> None:
    """Read, migrate, or provision the audit ledger."""
    needs_pocketbase = (
        AUDIT_BACKEND == "pocketbase"
        or args.provision_pocketbase
        or args.migrate_sqlite_to_pocketbase
    )
    client = pocketbase_client() if needs_pocketbase else None
    if args.provision_pocketbase:
        if client is None:
            raise LabError("PocketBase client is not configured")
        result = client.provision()
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    if args.migrate_sqlite_to_pocketbase:
        if client is None:
            raise LabError("PocketBase client is not configured")
        with controller_lock():
            provision = client.provision()
            source_dir = (
                Path(args.sqlite_journal_dir).expanduser()
                if args.sqlite_journal_dir
                else JOURNAL_ROOT
            )
            result = journal_module.migrate_sqlite_to_pocketbase(
                source_dir,
                client,
                controller=str(
                    CONFIG.audit.get("controller_id") or socket.gethostname()
                ),
            )
        result["collection"] = client.collection_name
        result["collection_created"] = provision["created"]
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    if args.summary:
        print(json.dumps(
            journal_module.summary(
                JOURNAL_ROOT, backend=AUDIT_BACKEND, pocketbase=client
            ),
            indent=2,
            sort_keys=True,
        ))
        return
    if args.import_jsonl:
        count = journal_module.import_jsonl(JOURNAL_ROOT)
        print(json.dumps({"imported": count}, indent=2))
        return
    events = journal_module.query(
        JOURNAL_ROOT, limit=args.limit, lease=args.lease,
        event=args.event, since=args.since,
        backend=AUDIT_BACKEND, pocketbase=client,
    )
    print(json.dumps(events, indent=2, sort_keys=True))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="proxmox-lab",
        description="Lease-managed, fail-closed control of a Proxmox home lab "
                    "that powers itself on and off.",
    )
    sub = root.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="write a starter config file")
    init.add_argument("--path")
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_init)

    doctor = sub.add_parser("doctor", help="check config, secrets and access")
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
    ledger.add_argument(
        "--provision-pocketbase",
        action="store_true",
        help="create or validate the configured PocketBase audit collection",
    )
    ledger.add_argument("--import-jsonl", action="store_true",
                        help="load legacy per-day JSONL files into the database")
    ledger.add_argument(
        "--migrate-sqlite-to-pocketbase",
        action="store_true",
        help="copy the SQLite audit ledger to the configured PocketBase collection",
    )
    ledger.add_argument(
        "--sqlite-journal-dir",
        help="source journal directory (default: configured journal_dir)",
    )
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
        "--password-stdin",
        action="store_true",
        help="Read a password value from stdin without exposing it in argv",
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
    upload.add_argument("--storage", default="local",
                        choices=UPLOAD_STORAGES)
    upload.add_argument("--content", choices=("import", "iso"), default="import")
    upload.add_argument("--file", required=True)
    upload.add_argument("--timeout", type=int, default=1800)
    upload.add_argument("--task-timeout", type=int, default=1800)
    upload.set_defaults(func=cmd_upload)

    end = sub.add_parser("lease-end")
    end.add_argument("--lease", required=True)
    end.set_defaults(func=cmd_lease_end)

    cleanup = sub.add_parser("cleanup-expired")
    cleanup.add_argument("--all", action="store_true")
    cleanup.add_argument("--no-backup", action="store_true",
                         help="skip the long-term backup sweep")
    cleanup.set_defaults(func=cmd_cleanup_expired)

    from . import android
    from . import console
    from . import guest
    from . import longterm
    from . import memflow
    from . import netcap
    from . import netgw
    from . import recipes
    from . import share
    from . import storage
    from . import usb
    from . import windows

    android.register(sub, _module())
    console.register(sub, _module())
    guest.register(sub, _module())
    longterm.register(sub, _module())
    memflow.register(sub, _module())
    netcap.register(sub, _module())
    netgw.register(sub, _module())
    recipes.register(sub, _module())
    share.register(sub, _module())
    storage.register(sub, _module())
    usb.register(sub, _module())
    windows.register(sub, _module())
    return root


def _module() -> Any:
    """This module as an object, for helper modules that call back into it."""
    module = sys.modules.get(__name__)
    if module is None:  # loaded by path without sys.modules registration
        module = types.ModuleType("proxmox_lab")
        module.__dict__.update(globals())
    return module


def _expected_errors() -> tuple[type[BaseException], ...]:
    """Every error the package raises on purpose.

    Collected once, from the modules themselves, so adding a new subsystem
    cannot reintroduce a raw traceback for a routine failure like a missing
    secret or an unreachable worker.
    """
    errors: list[type[BaseException]] = [
        LabError, ConfigError, secrets_store.SecretError,
        pocketbase_module.PocketBaseError, power_module.PowerError, ValueError,
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
