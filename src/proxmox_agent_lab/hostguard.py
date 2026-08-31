"""The host-side lease guard.

A controller finalises its own leases. The failure this covers is the one that
actually happens: the controller goes away -- laptop closed, process killed,
someone on holiday -- and its guests keep running. This project's own lab had
a lease sit `active` for eight days that way, holding the host awake the whole
time, because nothing on the host itself was watching.

So a small script runs on the Proxmox host under root, on a timer, and reads
the shared ledger the controllers write to. Guests tagged with a lease whose
last lifecycle event says it is over, or whose heartbeat has gone quiet past
the grace window, are stopped.

Stopped, never destroyed. A guest the guard stops can be started again and its
disk inspected; a guest it deleted is gone, and the guard is the component
running unattended with the least context about what the work was worth.
Destroying stays with the controller, which knows the lease's policy.

It then powers the host off, because a lab that cleans up but stays awake has
only solved half the problem -- the eight-day lease also meant eight days of
electricity. The condition for that is deliberately blunt: **no guest running
at all**, infrastructure aside. Not "no lease the ledger knows about" -- a
controller that has not been upgraded yet writes nowhere this guard can read,
and its work would look like an idle host. If something is running, somebody
may be using it.

The power-off does make one ledger consult: the long-term pin. A long-term
lease promises the host stays on even with every guest stopped, and only a
controller new enough to have the feature can create one -- so trusting the
ledger there cannot pull the power out from under an un-upgraded controller.
The same field answers the stop question: a long-term lease never heartbeats
by design, so its silence must not read as abandonment. Only a recorded end
(`lease-end`, `long-term-destroyed`, `long-term-released`) ends one.

Installed by `proxmox-lab journal host-setup` alongside the ledger container.
"""

from __future__ import annotations

# Written to /usr/local/lib/pxl-hostguard.py on the Proxmox host. Standard
# library plus PyMySQL, which the ledger container's host already needs.
GUARD_SCRIPT = r'''#!/usr/bin/env python3
"""Stop guests whose lease is over. Runs on the Proxmox host, as root.

A long-term lease is the exception in both directions: it never heartbeats,
so its silence is not abandonment, and while one is active the host stays on
even with nothing running. Only a recorded end ends one."""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

CONFIG_PATH = "/etc/pxl-hostguard.json"
# How long a lease may go without a heartbeat before the guard treats it as
# abandoned. Generously wider than the controller's own heartbeat interval:
# stopping a live experiment because a laptop slept is worse than a guest
# running an extra hour.
DEFAULT_GRACE_MINUTES = 90
# Consecutive idle checks before powering off. One idle sweep is not enough:
# a controller between two commands, or a guest mid-reboot, both look idle for
# a moment.
DEFAULT_IDLE_CHECKS = 3
STATE_PATH = "/var/lib/pxl-hostguard-state.json"
# Every way a lease can be recorded as over. The long-term endings matter
# because long-term leases never write lease-end -- destroy and release close
# them under their own event names.
LEASE_END_EVENTS = ("lease-end", "lease-abandoned",
                    "long-term-destroyed", "long-term-released")


def log(message):
    print(f"pxl-hostguard: {message}", flush=True)


def load_config():
    with open(CONFIG_PATH) as handle:
        return json.load(handle)


def connect(cfg):
    import pymysql
    from pymysql.cursors import DictCursor

    return pymysql.connect(
        host=cfg.get("host", "127.0.0.1"), port=int(cfg.get("port", 3306)),
        user=cfg["user"], password=cfg["password"], database=cfg["database"],
        connect_timeout=10, charset="utf8mb4", cursorclass=DictCursor,
        autocommit=True,
    )


def lease_state(connection):
    """Last lifecycle event per lease, from the shared ledger."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT lease, event, MAX(timestamp) AS last_at FROM events "
            "WHERE lease IS NOT NULL AND event IN "
            "('lease-begin','lease-heartbeat','lease-end','lease-abandoned',"
            "'long-term-destroyed','long-term-released') "
            "GROUP BY lease, event"
        )
        rows = cursor.fetchall() or []
    leases = {}
    for row in rows:
        entry = leases.setdefault(row["lease"], {})
        entry[row["event"]] = row["last_at"]
    return leases


def lease_kinds(connection):
    """The kind ("session" or "long-term") of each lease, from its begin
    event's JSON payload. Leases whose begin event carries no kind, or an
    unparseable one, read as ordinary session leases."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT lease, data FROM events "
            "WHERE lease IS NOT NULL AND event = 'lease-begin'"
        )
        rows = cursor.fetchall() or []
    kinds = {}
    for row in rows:
        try:
            kind = json.loads(row["data"] or "{}").get("kind")
        except ValueError:
            continue
        if kind:
            kinds[row["lease"]] = str(kind)
    return kinds


def parse_ts(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def is_over(events, grace, long_term=False):
    """Has this lease ended, or gone quiet long enough to count as abandoned?

    A long-term lease does not heartbeat by design, so quiet is its normal
    state; only a recorded end ends one."""
    if any(events.get(name) for name in LEASE_END_EVENTS):
        return "ended"
    if long_term:
        return None
    seen = max(
        (t for t in (parse_ts(events.get("lease-heartbeat")),
                     parse_ts(events.get("lease-begin"))) if t),
        default=None,
    )
    if seen is None:
        return None
    if datetime.now(timezone.utc) - seen > timedelta(minutes=grace):
        return "abandoned"
    return None


def guests():
    """Every guest on this node, with its lease tag if it has one."""
    out = []
    for kind, argv in (("qemu", ["qm", "list"]), ("lxc", ["pct", "list"])):
        try:
            result = subprocess.run(argv, capture_output=True, text=True,
                                    timeout=60, check=False)
        except (OSError, subprocess.SubprocessError):
            continue
        for line in (result.stdout or "").splitlines()[1:]:
            parts = line.split()
            if len(parts) < 2 or not parts[0].isdigit():
                continue
            vmid = int(parts[0])
            status = parts[1] if kind == "lxc" else (
                parts[2] if len(parts) > 2 else "")
            cfg = subprocess.run(
                ["qm" if kind == "qemu" else "pct", "config", str(vmid)],
                capture_output=True, text=True, timeout=60, check=False)
            lease = None
            for cfg_line in (cfg.stdout or "").splitlines():
                if cfg_line.startswith("tags:"):
                    for tag in cfg_line.split(":", 1)[1].replace(",", ";").split(";"):
                        tag = tag.strip()
                        if tag.startswith("lease-"):
                            lease = tag[len("lease-"):]
            out.append({"kind": kind, "vmid": vmid, "status": status,
                        "lease": lease})
    return out


def stop(guest):
    argv = ["qm" if guest["kind"] == "qemu" else "pct", "shutdown",
            str(guest["vmid"]), "--timeout", "120"]
    result = subprocess.run(argv, capture_output=True, text=True,
                            timeout=200, check=False)
    if result.returncode != 0:
        argv = ["qm" if guest["kind"] == "qemu" else "pct", "stop",
                str(guest["vmid"])]
        result = subprocess.run(argv, capture_output=True, text=True,
                                timeout=200, check=False)
    return result.returncode == 0


def record(connection, controller, vmid, lease, reason):
    """Write what the guard did into the same ledger it read.

    vmid 0 with no lease is the host-level event (powering off), not a guest.
    """
    import hashlib

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    event = "hostguard-powered-off" if reason == "host-idle" else "hostguard-stopped"
    payload = {
        "timestamp": now, "event": event, "lease": lease,
        "vmid": vmid or None, "reason": reason, "controller": controller,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    payload["event_id"] = hashlib.sha256(canonical.encode()).hexdigest()[:36]
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT IGNORE INTO events (event_id, controller, timestamp, "
            "event, lease, vmid, data) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (payload["event_id"], controller, now, event,
             lease, vmid or None, json.dumps(payload, sort_keys=True)),
        )


def read_state():
    try:
        with open(STATE_PATH) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {"idle_checks": 0}


def write_state(state):
    """Persist the idle counter. Write-then-rename, so a kill mid-write can
    only ever leave the previous state or the new one, never a truncated
    file. Read failures already fail safe (counter reset to zero), but a
    torn write would silently defeat the consecutive-checks rule."""
    tmp = STATE_PATH + ".tmp"
    try:
        with open(tmp, "w") as handle:
            json.dump(state, handle)
        os.replace(tmp, STATE_PATH)
    except OSError:
        pass


def anything_running(all_guests, ledger_ctid):
    """Guests running right now, infrastructure aside.

    The test for "is this host idle" on purpose, rather than asking the ledger
    which leases are open. A controller that has not been upgraded writes
    nowhere this guard can read; its guests would look like nobody's work and
    the host would be powered off underneath it.
    """
    return [
        g for g in all_guests
        if g["status"] == "running" and str(g["vmid"]) != str(ledger_ctid)
    ]


def pinned_leases(leases, kinds):
    """Active long-term leases: the one thing that keeps the host on with
    nothing running.

    Ordinary leases are invisible to this decision on purpose -- an
    un-upgraded controller writes nowhere here, so trusting the ledger for
    "is anything running" would power off real work. A long-term lease is
    different in kind: only a controller new enough to have the feature can
    create one, and its whole promise is that the host stays on even when
    every guest is stopped."""
    return sorted(
        lease for lease, events in leases.items()
        if kinds.get(lease) == "long-term"
        and not any(events.get(name) for name in LEASE_END_EVENTS)
    )


def power_off():
    log("host idle; powering off")
    subprocess.run(["/sbin/shutdown", "-h", "now"], check=False, timeout=60)


def main():
    cfg = load_config()
    grace = int(cfg.get("grace_minutes", DEFAULT_GRACE_MINUTES))
    controller = cfg.get("controller_id", "pxl-hostguard")
    ledger_ctid = str(cfg.get("ledger_ctid", ""))
    try:
        connection = connect(cfg)
    except Exception as exc:
        log(f"ledger unreachable, nothing to enforce: {exc}")
        return 0
    try:
        leases = lease_state(connection)
        kinds = lease_kinds(connection)
        pinned = pinned_leases(leases, kinds)
        all_guests = guests()
        stopped = 0
        for guest in all_guests:
            if not guest["lease"] or guest["status"] != "running":
                continue
            # Never touch the ledger container itself: stopping it would take
            # away the very history this guard runs on.
            if str(guest["vmid"]) == ledger_ctid:
                continue
            events = leases.get(guest["lease"])
            if events is None:
                # A lease this ledger has never seen. Could be another
                # controller mid-first-write; leave it alone and say so.
                log(f"vmid {guest['vmid']}: lease {guest['lease']} unknown to "
                    "the ledger, leaving it running")
                continue
            reason = is_over(events, grace,
                             long_term=kinds.get(guest["lease"]) == "long-term")
            if not reason:
                continue
            if stop(guest):
                stopped += 1
                record(connection, controller, guest["vmid"], guest["lease"],
                       reason)
                log(f"stopped vmid {guest['vmid']} ({reason} lease "
                    f"{guest['lease']})")
            else:
                log(f"could not stop vmid {guest['vmid']}")

        # Powering off is the other half of cleaning up. Guarded by a plain
        # "is anything running", and by needing several idle sweeps in a row so
        # a controller pausing between two commands does not lose its host.
        state = read_state()
        if not cfg.get("shutdown_when_idle", True):
            log(f"done; {stopped} guest(s) stopped; shutdown disabled")
            write_state({"idle_checks": 0})
            return 0
        busy = anything_running(all_guests, ledger_ctid)
        if busy:
            if state.get("idle_checks"):
                log(f"{len(busy)} guest(s) running; idle counter reset")
            write_state({"idle_checks": 0})
            log(f"done; {stopped} guest(s) stopped; host stays up")
            return 0
        if pinned:
            write_state({"idle_checks": 0})
            log(f"done; {stopped} guest(s) stopped; long-term lease "
                f"{', '.join(pinned)} active; host stays up")
            return 0
        needed = int(cfg.get("idle_checks", DEFAULT_IDLE_CHECKS))
        seen = int(state.get("idle_checks", 0)) + 1
        write_state({"idle_checks": seen})
        if seen < needed:
            log(f"done; {stopped} stopped; host idle ({seen}/{needed} checks)")
            return 0
        write_state({"idle_checks": 0})
        record(connection, controller, 0, None, "host-idle")
        connection.close()
        power_off()
        return 0
    finally:
        try:
            connection.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

# systemd timer rather than a crontab line: it survives a reboot without an
# @reboot entry, logs to the journal, and will not stack runs if one is slow.
GUARD_UNITS = r'''
cat > /etc/systemd/system/pxl-hostguard.service <<'UNIT'
[Unit]
Description=proxmox-agent-lab lease guard
After=network-online.target pve-cluster.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /usr/local/lib/pxl-hostguard.py
UNIT

cat > /etc/systemd/system/pxl-hostguard.timer <<'UNIT'
[Unit]
Description=Run the proxmox-agent-lab lease guard every 10 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=10min
Persistent=true

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now pxl-hostguard.timer >/dev/null 2>&1 || true
'''
