"""Diagnostics and provisioning: init, doctor, status, secrets, journal.

These are the repair surface: they must work on a broken install, which
is why they read configuration and errors through the `lab` facade
instead of holding import-time snapshots.
"""

from __future__ import annotations

from . import audit as audit_module
from . import config as config_module
from . import inventory as inventory_module
from . import journal as journal_module
from . import mariadb as mariadb_module
from . import power as power_module
from . import secrets_store
from .errors import LabError
from .state import iso_now, json_dump, utc_now
from pathlib import Path
from typing import Any
import argparse
import json
import re
import secrets
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
        "journal_dir": str(lab.JOURNAL_ROOT),
        "audit": {
            "ledger": lab.ledger().describe() if lab.ledger() else None,
        },
    }
    if lab.ledger() is None:
        problems.append(
            "[audit] no ledger configured. Run 'proxmox-lab journal "
            "host-setup' to provision MariaDB on the Proxmox host."
        )
    spool = journal_module.spool_path(lab.JOURNAL_ROOT)
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

    settings = lab.ledger()
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
    mode = lab.CONFIG.power.get("mode")
    report["power"] = {
        "mode": mode,
        "can_force_off": power_module.can_force_off(lab.CONFIG),
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


def _provision_ledger(lab: Any, args: argparse.Namespace) -> dict[str, Any]:
    """Provision MariaDB on the Proxmox host and seed the shared secrets.

    A persistent, unprivileged container marked onboot, published on the
    hypervisor's own address. Deliberately not lease-owned: the ledger has to
    outlive the leases it records, so lease-end must never destroy it.
    """
    if not args.host_change_authorized:
        raise lab.LabError(
            "provisioning the audit ledger creates a container and a NAT rule "
            "on the Proxmox host. Re-run with --host-change-authorized only "
            "when the user asked for it."
        )
    from . import hostguard as hostguard_module
    from . import host_transport as host_transport_module

    ctid = args.ctid or 9310
    storage = args.storage or str(lab.CONFIG.storage.get("bulk_storage") or "local-lvm")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", storage):
        raise lab.LabError(f"invalid --storage value: {storage!r}")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", args.bridge):
        raise lab.LabError(f"invalid --bridge value: {args.bridge!r}")
    database = str(lab.CONFIG.audit.get("database") or "proxmox_lab")
    user = str(lab.CONFIG.audit.get("user") or "proxmox_lab")
    if not re.fullmatch(r"[A-Za-z0-9_]+", database):
        raise lab.LabError(f"invalid [audit] database name: {database!r}")
    if not re.fullmatch(r"[A-Za-z0-9_]+", user):
        raise lab.LabError(f"invalid [audit] user name: {user!r}")
    existing = secrets_store.get(
        lab.CONFIG, secrets_store.BOOTSTRAP_SECRET, required=False
    )
    password = existing or secrets.token_urlsafe(24)
    if any(c in password for c in "'`"):
        raise lab.LabError("audit ledger password may not contain single quotes or backticks")
    script = (
        mariadb_module.HOST_SETUP_SCRIPT
        .replace("__CTID__", str(ctid))
        .replace("__STORAGE__", storage)
        .replace("__BRIDGE__", str(args.bridge))
        .replace("__DBNAME__", database)
        .replace("__DBUSER__", user)
        .replace("__DBPASS__", password)
        .replace("__GUARD_INSTALL__", lab._guard_install_block(hostguard_module))
    )
    host_transport_module.require_host_ssh(lab._module())
    proc = host_transport_module.run(
        lab._module(), ["bash", "-s"], timeout=args.timeout, stdin=script
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode not in (0, None) or "ledger-ready" not in output:
        raise lab.LabError(
            "audit ledger provisioning failed: " + output.strip()[-800:]
        )

    settings = journal_module.settings_from_config(lab.CONFIG, password)
    if settings is None:
        raise lab.LabError("ledger provisioned but [audit] host is not resolvable")
    mariadb_module.ensure_schema(settings)

    # Seed the shared store so a second controller needs only this password.
    seeded = lab._seed_shared_secrets(settings)
    env_var = secrets_store._env_name(secrets_store.BOOTSTRAP_SECRET)
    audit_module.prime_ledger_cache(settings)
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


def _seed_shared_secrets(lab: Any, settings: Any) -> list[str]:
    """Copy this controller's secrets into the shared store, once.

    This is what makes adding a machine a one-liner. Only secrets this
    controller can actually read are copied, and an existing shared value is
    never overwritten -- the first controller to set one wins.
    """
    now = lab.utc_now().isoformat().replace("+00:00", "Z")
    existing = {row["name"] for row in mariadb_module.list_secrets(settings)}
    seeded: list[str] = []
    for name in secrets_store.KNOWN_SECRETS:
        if name == secrets_store.BOOTSTRAP_SECRET or name in existing:
            continue
        try:
            value = secrets_store.get(lab.CONFIG, name, required=False)
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
            updated_by=lab._controller_id(), updated_at=now,
        )
        seeded.append(name)
    return seeded


def cmd_journal(lab: Any, args: argparse.Namespace) -> None:
    """Read the shared audit ledger, or carry an old local one into it."""
    if args.host_setup:
        result = lab._provision_ledger(args)
        print(json.dumps(result, indent=2, sort_keys=True))
        return

    settings = lab.ledger()
    if settings is None:
        raise lab.LabError(
            "no audit ledger configured. Run 'proxmox-lab journal host-setup' "
            "to provision MariaDB on the Proxmox host."
        )

    if args.flush_spool:
        with lab.controller_lock():
            result = journal_module.flush_spool(
                settings, lab.JOURNAL_ROOT, controller=lab._controller_id()
            )
        print(json.dumps(result, indent=2, sort_keys=True))
        return

    if args.migrate:
        with lab.controller_lock():
            result = journal_module.migrate_legacy(
                settings, lab.JOURNAL_ROOT, controller=lab._controller_id()
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
