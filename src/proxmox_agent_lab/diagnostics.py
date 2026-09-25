"""Diagnostics: init, doctor, status, secrets, journal.

These are the repair surface: they must work on a broken install, which
is why they read configuration and errors through the `lab` facade
instead of holding import-time snapshots.
"""

from __future__ import annotations

from . import config as config_module
from . import inventory as inventory_module
from . import journal as journal_module
from . import secrets_store
from pathlib import Path
from typing import Any
import argparse
import json
import sys

def cmd_init(lab: Any, args: argparse.Namespace) -> None:
    """Write a starter config file."""
    target = Path(args.path).expanduser() if args.path else lab.CONFIG.intended
    if target.exists() and not args.force:
        raise lab.LabError(f"{target} already exists; pass --force to overwrite")
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


def cmd_secrets(lab: Any, args: argparse.Namespace) -> None:
    if args.secrets_command == "list":
        print(json.dumps(secrets_store.status(lab.CONFIG), indent=2, sort_keys=True))
        return
    if args.secrets_command == "set":
        if args.name not in secrets_store.KNOWN_SECRETS and not args.allow_unknown:
            raise lab.LabError(
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
            raise lab.LabError("refusing to store an empty secret")
        try:
            backend = secrets_store.store(lab.CONFIG, args.name, value)
        except secrets_store.SecretError as exc:
            raise lab.LabError(str(exc)) from None
        print(json.dumps({"stored": args.name, "backend": backend}))


def retained_backup_coverage(lab: Any) -> dict[str, Any]:
    """How stale each retained guest's last backup is.

    Backups used to happen only as a side effect of an active long-term lease,
    so the keep-forever guests -- templates, persistent workers -- could have
    none at all. Whether or not the sweep is enabled, the drift is reported.
    """
    entries = inventory_module.entries(lab.STATE_ROOT)
    if not entries:
        return {}
    now = lab.utc_now()
    never: list[int] = []
    oldest_days: float | None = None
    for item in entries.values():
        last = item.get("last_backup_at")
        if not last:
            never.append(int(item.get("vmid", 0)))
            continue
        try:
            age = (now - lab.parse_expiry(str(last))).total_seconds() / 86400
        except (TypeError, ValueError):
            never.append(int(item.get("vmid", 0)))
            continue
        oldest_days = age if oldest_days is None else max(oldest_days, age)
    coverage: dict[str, Any] = {
        "retained_guests": len(entries),
        "sweep_enabled": bool(lab.CONFIG.lease.get("retained_backup", False)),
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


def host_update_report(lab: Any) -> dict[str, Any]:
    """Pending package updates and reboot state on the lab node.

    The node is an ordinary Debian/PVE host that needs patching, but the whole
    workflow is lease-in, work, power-off -- so nobody ever sees `apt` drift.
    This is advisory only: a pending security update is a thing to schedule
    between leases, not a reason for `doctor` to fail. Reached over the opt-in
    `[memflow]` host SSH channel, because there is no API for it.
    """
    from . import host_transport

    if not host_transport.host_ssh_enabled():
        return {
            "checked": False,
            "reason": "needs the opt-in [memflow] host SSH channel",
        }
    report: dict[str, Any] = {"checked": True}
    try:
        simulated = host_transport.host_run(
            lab._module(),
            ["sh", "-c", "LC_ALL=C apt-get -s -o Debug::NoLocking=1 upgrade"],
            timeout=120,
        )
    except lab.LabError as exc:
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
        reboot = host_transport.host_run(
            lab._module(),
            ["sh", "-c", "test -e /var/run/reboot-required && echo yes || echo no"],
            timeout=30,
        )
        report["reboot_required"] = (reboot.stdout or "").strip() == "yes"
    except lab.LabError:
        report["reboot_required"] = None
    report["remediation"] = (
        "Patch and reboot between leases, never during one. Updating the node "
        "is a host change and is deliberately not automated here."
    )
    return report


def cmd_doctor(lab: Any, args: argparse.Namespace) -> None:
    """Check the install end to end and say exactly what is missing."""
    problems: list[str] = []
    if lab.CONFIG_ERROR:
        problems.append(f"config could not be read: {lab.CONFIG_ERROR}")
    report: dict[str, Any] = {
        "controller_version": lab.__version__,
        "config_file": str(lab.CONFIG.source) if lab.CONFIG.source else None,
        "config_expected_at": str(lab.CONFIG.intended),
        "state_dir": str(lab.STATE_ROOT),
    }
    if lab.CONFIG.unknown_sections:
        report["unknown_sections"] = lab.CONFIG.unknown_sections
        problems.append(
            "config has section(s) this version does not know, and they were "
            f"ignored: {', '.join(lab.CONFIG.unknown_sections)}"
        )
    if not lab.CONFIG.configured and not lab.CONFIG_ERROR:
        problems.append(
            f"no config file at {lab.CONFIG.intended}; run 'proxmox-lab init'"
        )
    for key in ("host", "node", "token_user", "token_name"):
        if not getattr(lab.CONFIG.proxmox, key):
            problems.append(f"[proxmox] {key} is not set")
    report["proxmox"] = {
        "host": lab.HOST or None, "node": lab.NODE or None,
        "token": f"{lab.TOKEN_USER}!{lab.TOKEN_NAME}" if lab.TOKEN_USER else None,
        "verify_tls": lab.VERIFY_TLS,
        "guest_mode": lab.CONFIG.proxmox.get("guest_mode", "all"),
    }

    backend = secrets_store.detect_backend() \
        if lab.CONFIG.secrets.get("backend", "auto") in ("", "auto") \
        else lab.CONFIG.secrets.get("backend")
    report["secrets_backend"] = backend
    try:
        secrets_store.get(lab.CONFIG, "proxmox-token")
        report["proxmox_token_stored"] = True
    except secrets_store.SecretError:
        report["proxmox_token_stored"] = False
        problems.append("Proxmox API token not stored; run "
                        "'proxmox-lab secrets set proxmox-token'")

    mode = lab.CONFIG.power.get("mode")
    report["power"] = {
        "mode": mode,
        "boot_timeout_seconds": int(
            lab.CONFIG.power.get("boot_timeout_seconds", 300)
        ),
        "minimum_cold_boot_timeout_seconds": lab.MIN_COLD_BOOT_TIMEOUT_SECONDS,
    }
    if mode == "wake-on-lan" and not lab.CONFIG.power.get("mac"):
        problems.append("[power] mac is not set (needed for wake-on-lan)")
    if mode == "none":
        report["power"]["note"] = "no remote power-on; start the machine yourself"

    if lab.HOST and lab.NODE and report.get("proxmox_token_stored"):
        api = lab.ProxmoxAPI()
        reachable = api.reachable()
        report["proxmox_reachable"] = reachable
        if reachable:
            try:
                permissions = api.call("GET", "/access/permissions") or {}
                scope: dict[str, Any] = {}
                for path in (f"/nodes/{lab.NODE}", "/vms", "/"):
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
            except lab.LabError as exc:
                problems.append(f"could not read permissions: {exc}")
        else:
            report["proxmox_reachable"] = False
            report["note"] = ("host unreachable -- expected when the machine "
                              "is powered off")

    if lab.HOST and lab.NODE and report.get("proxmox_token_stored") \
            and report.get("proxmox_reachable"):
        api = lab.ProxmoxAPI()
        try:
            described = lab.describe_guests(api)
        except lab.LabError as exc:
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
            signal = lab.recent_guest_activity(
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
                str(x["vmid"]): lab.guest_load(x.get("load")) for x in idle
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
        coverage = lab.retained_backup_coverage()
        if coverage:
            report["retained_backup"] = coverage
    if getattr(args, "host_checks", False):
        report["host"] = lab.host_update_report()
    report["problems"] = problems
    report["ok"] = not problems
    print(json.dumps(report, indent=2, sort_keys=True))
    if problems:
        raise lab.LabError(f"{len(problems)} problem(s) found")


#: Flags the MariaDB ledger took with it: the command surface is
#: ``--lease``/``--since``/``--limit`` over the local store and nothing else.
_REMOVED_JOURNAL_FLAGS = (
    "host_setup",
    "flush_spool",
    "migrate",
    "migrations",
    "summary",
    "event",
    "controller",
)


def cmd_journal(lab: Any, args: argparse.Namespace) -> None:
    """Read the audit journal: the events in the local lab store."""
    removed = [
        name for name in _REMOVED_JOURNAL_FLAGS if getattr(args, name, None)
    ]
    if removed:
        raise lab.LabError(
            f"journal {'/'.join('--' + name.replace('_', '-') for name in removed)}"
            " is gone with the shared ledger: every event is a row in the "
            "local lab store, so there is nothing to upload, carry over, or "
            "summarise. Filter with --lease/--since/--limit."
        )
    rows = journal_module.query_events(
        lease=args.lease, since=args.since, limit=args.limit,
    )
    print(journal_module.format_events(rows))


def cmd_status(lab: Any, args: argparse.Namespace) -> None:
    api = lab.ProxmoxAPI()
    idle_seconds = int(lab.mcp_idle_elapsed())
    if not api.reachable():
        print(
            json.dumps(
                {
                    "reachable": False,
                    "host": lab.HOST,
                    "node": lab.NODE,
                    "mcp_idle_seconds": idle_seconds,
                    "mcp_idle_shutdown_after_seconds": lab.MCP_IDLE_SHUTDOWN_SECONDS,
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
        "host": lab.HOST,
        "node": lab.NODE,
        "version": version,
        "nodes": nodes,
        "guests": guests,
        "mcp_idle_seconds": idle_seconds,
        "mcp_idle_shutdown_after_seconds": lab.MCP_IDLE_SHUTDOWN_SECONDS,
        "active_leases": [
            {"id": x["id"], "purpose": x["purpose"], "expires_at": x["expires_at"]}
            for x in lab.active_leases()
        ],
    }
    described = inventory_module.classify(
        [x for x in guests if isinstance(x, dict) and "vmid" in x],
        known_leases=lab.all_lease_ids(),
        retained=inventory_module.entries(lab.STATE_ROOT),
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
    print(json.dumps(lab.redact(output), indent=2, sort_keys=True))
