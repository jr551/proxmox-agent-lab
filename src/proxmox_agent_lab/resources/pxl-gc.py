#!/usr/bin/env python3
"""Host-side garbage collector for pxl lab guests.

Copied to /usr/local/sbin/pxl-gc on the Proxmox host and run from root's
crontab (see docs/rework-plan.md section F). Two duties:

1. Destroy guests whose lease expired, using ONLY guest metadata:
   tags contain the token `proxmoxagentlab` (or the pre-rename `pxl`),
   description carries a parseable
   `pxl-lease=<id> pxl-expiry=<unix epoch>` line, `pxl-expiry=0` = long-term.
   Anything that does not parse is warn-skipped, never deleted.
2. Power the host off when idle: zero running guests of any kind AND zero
   unexpired pxl guests, observed on two consecutive runs at least 10 minutes
   apart (host-local stamp file).

This script is standalone: stdlib only, no proxmox_agent_lab imports, no
controller database. It never touches a guest without pxl metadata, never
powers off while any guest runs, and exits 0 on success (exit 2 only for a
usage error).

Environment overrides (used by tests; defaults suit the host):
  PXL_GC_NOW            injected clock, unix seconds (default: time.time())
  PXL_GC_STATE_DIR      stamp directory (default: /var/lib/pxl-gc)
  PXL_GC_LOCK_DIR       per-vmid flock directory (default: /var/lock)
  PXL_GC_POWEROFF       poweroff argv (default: "shutdown -h now")
  PXL_GC_STOP_TIMEOUT   seconds to await graceful stop (default: 120)
"""

import fcntl
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

STAMP_NAME = "clear-stamp"
CLEAR_SECONDS = 600
DEFAULT_STATE_DIR = "/var/lib/pxl-gc"
DEFAULT_LOCK_DIR = "/var/lock"
DEFAULT_POWEROFF = "shutdown -h now"
DEFAULT_STOP_TIMEOUT = 120

LEASE_RE = re.compile(r"(?:^|\s)pxl-lease=(\S+)")
EXPIRY_RE = re.compile(r"(?:^|\s)pxl-expiry=(\S+)")

#: Tag tokens that mark a guest as ours. `pxl` is the pre-rename token,
#: still honoured so guests stamped before the rename are never orphaned.
OWNERSHIP_TAGS = frozenset({"proxmoxagentlab", "pxl"})


def log(message):
    print("pxl-gc: " + message, flush=True)


def env_number(name, default):
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def fmt_epoch(value):
    return str(int(value)) if float(value) == int(value) else str(value)


def capture(binary, arglist, timeout=60):
    """Run one host command. Returns (returncode, stdout, error tail)."""
    argv = [binary] + list(arglist)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, "", argv[0] + " not found"
    except subprocess.TimeoutExpired:
        return 124, "", argv[0] + " timed out after " + str(timeout) + "s"
    except OSError as exc:
        return 125, "", str(exc)
    tail = ""
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")[-200:]
    return proc.returncode, proc.stdout, tail


def run_command(argv, timeout):
    """Run a side-effect command. Returns (returncode, error tail)."""
    rc, _out, err = capture(argv[0], argv[1:], timeout=timeout)
    return rc, err


# Cron’s PATH is `/usr/bin:/bin`. `qm` and `pct` live in `/usr/sbin`, so a
# name lookup that stops at PATH reports a healthy host as unlistable and
# the collector can neither reap nor prove it is safe to power off.
DEFAULT_BIN_DIRS = ("/usr/sbin", "/sbin")


def binary_dirs():
    """Extra directories searched after PATH. Tests set `PXL_GC_BIN_DIRS`."""
    raw = os.environ.get("PXL_GC_BIN_DIRS")
    if raw is None:
        return DEFAULT_BIN_DIRS
    return tuple(part for part in raw.split(os.pathsep) if part)


def resolve_binary(name):
    """Absolute path of a host tool, or None when it cannot be found."""
    found = shutil.which(name)
    if found:
        return found
    for directory in binary_dirs():
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def enumerate_guests(binary, kind):
    """(guests, ok) from `qm list` / `pct list`.

    `ok` is False when the listing itself failed -- the binary missing from
    PATH and the usual sbin directories, or the command erroring. Callers
    must NOT read an empty list as "no guests exist": an unknown state is
    what keeps the power-off duty from firing (fail closed), while the
    deletion duty simply has nothing it can safely act on.
    """
    resolved = resolve_binary(binary)
    if resolved is None:
        log(binary + " not found on PATH; cannot enumerate " + kind + " guests")
        return [], False, None
    rc, out, err = capture(resolved, ["list"])
    if rc != 0:
        log(binary + " list failed (" + str(rc) + "): " + err + "; cannot enumerate")
        return [], False, None
    guests = []
    lines = out.splitlines()
    for line in lines[1:]:  # first line is the column header
        parts = line.split()
        if not parts:
            continue
        if not parts[0].isdigit():
            log("unparseable " + binary + " list line, skipping: " + line.strip()[:80])
            continue
        status_index = 2 if kind == "qemu" else 1
        status = parts[status_index] if len(parts) > status_index else ""
        if status not in ("running", "stopped"):
            log("unparseable status for " + parts[0] + ", treating as running")
            status = "running"
        guests.append((parts[0], status))
    return guests, True, resolved


def fetch_config(binary, vmid):
    rc, out, _err = capture(binary, ["config", vmid])
    return None if rc != 0 else out


def config_value(config, key):
    prefix = key + ":"
    for line in config.splitlines():
        if line.startswith(prefix):
            value = line[len(prefix):].strip()
            if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
                value = value[1:-1]
            return value
    return None


def parse_metadata(description):
    """-> (lease id, expiry epoch); anything unparseable raises ValueError."""
    if description is None or not description.strip():
        raise ValueError("description has no pxl-lease/pxl-expiry line")
    lease = LEASE_RE.search(description)
    expiry = EXPIRY_RE.search(description)
    if lease is None:
        raise ValueError("description missing pxl-lease=")
    if expiry is None:
        raise ValueError("description missing pxl-expiry=")
    raw = expiry.group(1)
    if not raw.isdigit():
        raise ValueError("pxl-expiry=" + raw + " is not a number")
    return lease.group(1), int(raw)


def acquire_lock(lock_dir, vmid):
    """Non-blocking per-vmid flock. fd held, None if held elsewhere, -1 if unusable."""
    try:
        os.makedirs(lock_dir, exist_ok=True)
        fd = os.open(os.path.join(lock_dir, "pxl-gc-" + vmid + ".lock"),
                     os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return -1
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def release_lock(fd):
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    except OSError:
        pass


def wait_stopped(binary, vmid, timeout):
    """Poll guest status. -> 'stopped' | 'running' | 'gone' (probe failure = gone)."""
    end = time.monotonic() + max(timeout, 0)
    while True:
        rc, out, _err = capture(binary, ["status", vmid])
        if rc != 0:
            return "gone"
        status = ""
        for line in out.splitlines():
            if line.startswith("status:"):
                status = line.split(":", 1)[1].strip()
                break
        if status == "stopped":
            return "stopped"
        if status != "running":
            log(vmid + ": status not understood; treating as running")
            status = "running"
        remaining = end - time.monotonic()
        if remaining <= 0:
            return "running"
        time.sleep(min(1.0, remaining))


def reap_guest(binary, vmid, expiry, stop_timeout, dry, lock_dir, stopped):
    """Graceful stop, then hard stop if needed, then destroy one expired guest."""
    fd = acquire_lock(lock_dir, vmid)
    if fd is None:
        log("skip " + vmid + ": locked by another run")
        return
    if fd < 0:
        log("skip " + vmid + ": lock directory unavailable; not deleting")
        return
    try:
        if dry:
            log("would stop and destroy " + vmid + " (expired " + str(expiry) + ")")
            return
        log(vmid + ": graceful shutdown")
        run_command([binary, "shutdown", vmid, "--timeout", "120"], timeout=180)
        state = wait_stopped(binary, vmid, stop_timeout)
        if state == "gone":
            log(vmid + ": already gone (no-op)")
            stopped.add(vmid)
            return
        if state == "running":
            log(vmid + ": hard stop (grace period expired)")
            rc, err = run_command([binary, "stop", vmid], timeout=60)
            if rc != 0:
                log(vmid + ": hard stop failed (" + err + "); leaving it running")
                return
            if wait_stopped(binary, vmid, 0) != "stopped":
                log(vmid + ": not stopped after hard stop; not destroying")
                return
        stopped.add(vmid)
        log(vmid + ": destroy")
        argv = [binary, "destroy", vmid]
        if os.path.basename(binary) == "qm":
            argv += ["--purge", "1"]
        rc, err = run_command(argv, timeout=120)
        if rc == 0:
            log(vmid + ": destroyed")
        elif wait_stopped(binary, vmid, 0) == "gone":
            log(vmid + ": already gone (no-op)")
        else:
            log(vmid + ": destroy failed (" + err + ")")
    finally:
        release_lock(fd)


def stamp_path(state_dir):
    return os.path.join(state_dir, STAMP_NAME)


def read_stamp(state_dir):
    try:
        with open(stamp_path(state_dir)) as handle:
            raw = handle.read().strip()
    except FileNotFoundError:
        return None
    except OSError as exc:
        log("warn: cannot read clear stamp: " + str(exc))
        return None
    try:
        return float(raw)
    except ValueError:
        log("warn: clear stamp is not a number; treating as absent")
        return None


def write_stamp(state_dir, now):
    try:
        os.makedirs(state_dir, exist_ok=True)
        with open(stamp_path(state_dir), "w") as handle:
            handle.write(str(now) + "\n")
    except OSError as exc:
        log("warn: cannot write clear stamp: " + str(exc))
        return False
    return True


def run_poweroff():
    argv = shlex.split(os.environ.get("PXL_GC_POWEROFF", DEFAULT_POWEROFF))
    if not argv:
        log("poweroff command is empty; not powering off")
        return
    rc, err = run_command(argv, timeout=120)
    if rc != 0:
        log("poweroff failed (" + str(rc) + "): " + err)
    else:
        log("poweroff command sent: " + " ".join(argv))


def power_pass(enumerated, pinned, stopped, now, state_dir, dry, known):
    """Second duty: power the host off when idle across two clear runs >=10 min apart.

    `known` is False when any guest listing failed. "Could not enumerate"
    is NOT "no guests running" -- a listing failure under a running untracked
    guest would otherwise power the host off under live work, so the duty
    refuses to judge and drops any clear stamp (fail closed, §F).
    """
    if not known:
        log("not clear: guest enumeration failed; refusing to power off")
        try:
            os.unlink(stamp_path(state_dir))
            log("clear stamp removed")
        except FileNotFoundError:
            pass
        except OSError as exc:
            log("warn: cannot remove clear stamp: " + str(exc))
        return
    running = sum(1 for kind, vmid, status in enumerated
                  if status == "running" and (kind, vmid) not in stopped)
    if running or pinned:
        log("not clear: running=" + str(running) + " pinned=" + str(len(pinned)))
        try:
            os.unlink(stamp_path(state_dir))
            log("clear stamp removed")
        except FileNotFoundError:
            pass
        except OSError as exc:
            log("warn: cannot remove clear stamp: " + str(exc))
        return
    stamp = read_stamp(state_dir)
    if stamp is None:
        if dry:
            log("clear, first observation (would stamp)")
            return
        if write_stamp(state_dir, now):
            log("clear, first observation stamped at " + fmt_epoch(now))
        return
    age = now - stamp
    if age >= CLEAR_SECONDS:
        if dry:
            log("would power off: 0 running guests, 0 unexpired pxl guests, clear since "
                + fmt_epoch(stamp))
            return
        log("powering off: 0 running guests, 0 unexpired pxl guests, clear since "
            + fmt_epoch(stamp))
        run_poweroff()
    else:
        log("clear since " + fmt_epoch(stamp) + " (" + str(int(age)) + "s < "
            + str(CLEAR_SECONDS) + "s), waiting")


def main(argv):
    dry = False
    for arg in argv:
        if arg != "--dry-run":
            print("usage: pxl-gc [--dry-run]", file=sys.stderr)
            return 2
        dry = True
    state_dir = os.environ.get("PXL_GC_STATE_DIR") or DEFAULT_STATE_DIR
    lock_dir = os.environ.get("PXL_GC_LOCK_DIR") or DEFAULT_LOCK_DIR
    stop_timeout = int(env_number("PXL_GC_STOP_TIMEOUT", DEFAULT_STOP_TIMEOUT))
    if stop_timeout < 0:
        stop_timeout = 0
    now = env_number("PXL_GC_NOW", time.time())

    enumerated = []  # (kind, vmid, status)
    pinned = set()   # (kind, vmid): parseable pxl metadata, expiry 0 or future
    stopped = set()  # (kind, vmid): stopped or destroyed by this run
    known = True     # did every guest listing succeed? (power-off fails closed)
    tools = {}
    for kind, binary in (("qemu", "qm"), ("lxc", "pct")):
        guests, ok, resolved = enumerate_guests(binary, kind)
        tools[kind] = resolved
        if not ok:
            known = False
        for vmid, status in guests:
            enumerated.append((kind, vmid, status))

    for kind, vmid, status in enumerated:
        binary = tools[kind]
        try:
            config = fetch_config(binary, vmid)
            if config is None:
                log("warn " + vmid + ": config unreadable; not deleting")
                continue
            if config_value(config, "template") == "1":
                log("skip " + vmid + ": template")
                continue
            tags = config_value(config, "tags") or ""
            tokens = {token.strip() for token in tags.split(";") if token.strip()}
            if not (tokens & OWNERSHIP_TAGS):
                log("skip " + vmid + ": not ours (no ownership tag)")
                continue
            try:
                _lease, expiry = parse_metadata(config_value(config, "description"))
            except ValueError as exc:
                log("warn " + vmid + ": " + str(exc) + "; not deleting")
                continue
            if expiry == 0:
                log("skip " + vmid + ": long-term (pxl-expiry=0)")
                pinned.add((kind, vmid))
                continue
            if now < expiry:
                log("skip " + vmid + ": unexpired (until " + str(expiry) + ")")
                pinned.add((kind, vmid))
                continue
            reap_guest(binary, vmid, expiry, stop_timeout, dry, lock_dir, stopped)
        except Exception as exc:  # one bad guest never fails the run
            log("warn " + vmid + ": internal error (" + str(exc) + "); not deleting")

    power_pass(enumerated, pinned, stopped, now, state_dir, dry, known)
    return 0


if __name__ == "__main__":
    try:
        code = main(sys.argv[1:])
    except Exception:
        import traceback
        traceback.print_exc()
        code = 0  # exit 2 (usage) is the only non-zero exit this script has
    sys.exit(code)
