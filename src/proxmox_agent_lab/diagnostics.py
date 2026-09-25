"""Diagnostics: init, doctor, status, journal.
These are the repair surface: they must work on a broken install, which is
why they read configuration and errors through the ``lab`` facade instead of
holding import-time snapshots (safety invariant 6). Every remote probe goes
through the one ssh seam (``lab.ssh``); the checklist is rework plan §G.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import tomllib
from typing import Any

from . import config as config_module
from . import gc as gc_module
from . import journal as journal_module
from . import proxmox as proxmox_module
from . import ssh as ssh_module
from . import store as store_module
from .errors import LabError


#: The starter config ``init`` writes: the §G schema, nothing else. The
#: transitional sections still present in ``config.TEMPLATE`` exist only for
#: modules that die in a later wave; a fresh install never needs them.
STARTER_CONFIG = """\
# proxmox-agent-lab configuration
#
# Written by 'proxmox-lab init'. Edit, then run 'proxmox-lab doctor'.
# Nothing secret belongs in this file -- the transport is ssh with your
# agent/keys only ('ssh-copy-id root@<target>').

[ssh]
target = "proxmox"           # ssh alias/host reached as root

[pve]
node = "pve"                 # node name used in pvesh paths
template_vmid = 100          # default template for guest clone/create

[power]
mac = ""                     # wired NIC MAC for WoL (discovered by init)
broadcast = "255.255.255.255"
port = 9

[state]
dir = "~/.local/share/proxmox-agent-lab"   # lab.db lives here

[lease]
ttl_seconds = 7200           # work is cleaned up if a lease is not renewed
idle_shutdown_seconds = 28800
"""

_MAC = re.compile(r"\b([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})\b")
_PXL_LEASE = re.compile(r"pxl-lease=([a-z0-9-]{8,80})")
_PXL_EXPIRY = re.compile(r"pxl-expiry=(\d+)")
_CONFIG_LINE = re.compile(r"^([A-Za-z0-9_-]+):\s?(.*)$")
_VMID_LINE = re.compile(r"^\s*(\d+)\s")

#: Interfaces that are never the wired uplink WoL needs: loopback, the Proxmox
#: bridges, guest taps, firewall chains and anything clearly wireless/virtual.
_NON_WIRED = (
    "lo", "vmbr", "vnet", "tap", "fwbr", "fwln", "fwpr",
    "wlan", "wlp", "docker", "veth", "virbr", "zt", "tailscale",
)
#: Predictable (``eno*``/``enp*``/``ens*``) and legacy wired-NIC names.
_WIRED = ("en", "eth")

_REMOTE_CHECK_TIMEOUT = 15.0


def _text(raw: Any) -> str:
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw).decode("utf-8", "replace")
    return raw if isinstance(raw, str) else ""


def _setting(config: Any, section: str, key: str, default: Any = "") -> Any:
    """One config value, tolerating missing sections on a broken install."""
    return getattr(getattr(config, section, None), key, default)


def _lab_ssh(lab: Any) -> Any:
    """The ssh seam off the facade; ``None`` when it cannot be built."""
    try:
        return lab.ssh
    except (LabError, config_module.ConfigError, AttributeError):
        return None


def _make_ssh(target: str) -> Any:
    """The ssh seam for one target (tests substitute a ``FakeSSH``)."""
    return ssh_module.SSH(target)


# -- init -----------------------------------------------------------------

def _wired_mac(link_show: str) -> str | None:
    """The wired NIC's MAC out of ``ip -br link show`` output.

    Preference order: an UP ``en*``/``eth*`` interface, any ``en*``/``eth*``,
    then any other UP non-virtual interface, then the rest. Bridges, taps,
    wireless and loopback are never candidates.
    """
    candidates: list[tuple[bool, bool, str]] = []
    for line in link_show.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        name = fields[0].split("@", 1)[0]
        if name.startswith(_NON_WIRED):
            continue
        match = _MAC.search(" ".join(fields[2:]))
        if match is None or match.group(1) == "00:00:00:00:00:00":
            continue
        candidates.append(
            (name.startswith(_WIRED), "UP" in fields[1].upper(),
             match.group(1).lower())
        )
    for need_wired, need_up in (
        (True, True), (True, False), (False, True), (False, False)
    ):
        for wired, up, mac in candidates:
            if wired == need_wired and (up or not need_up):
                return mac
    return None


def _discover_wol_mac(target: str) -> str | None:
    """Best-effort wired-NIC MAC discovery over the ssh seam (§G init).

    ``None`` on any failure -- unreachable target, missing ssh binary, an
    allowlist refusal -- because ``init``'s job is to produce a config the
    operator finishes by hand, and a discovered MAC is a bonus.
    """
    seam = _make_ssh(target)
    try:
        if not seam.probe():
            return None
        result = seam.run(["ip", "-br", "link", "show"], timeout=10)
    except LabError:
        return None
    if not result.ok:
        return None
    return _wired_mac(_text(result.stdout))


def cmd_init(lab: Any, args: argparse.Namespace) -> None:
    """Write the §G starter config; discover the WoL MAC when reachable."""
    target = Path(args.path).expanduser() if args.path else lab.CONFIG.intended
    if target.exists() and not args.force:
        raise LabError(f"{target} already exists; pass --force to overwrite")
    target.parent.mkdir(parents=True, exist_ok=True)

    # The target to probe: the config being replaced when there is one (so
    # ``init --force`` re-discovers against the live host), otherwise the
    # starter's own target.
    probe_target = ""
    if target.is_file():
        try:
            probe_target = str(
                _setting(config_module.load(target), "ssh", "target")
            ).strip()
        except config_module.ConfigError:
            probe_target = ""
    if not probe_target:
        probe_target = str(
            tomllib.loads(STARTER_CONFIG)["ssh"]["target"]
        ).strip()

    discovered = _discover_wol_mac(probe_target) if probe_target else None
    text = STARTER_CONFIG
    if discovered is not None:
        text = text.replace('mac = ""', f'mac = "{discovered}"', 1)
    target.write_text(text)
    config_module.reset_cache()

    report: dict[str, Any] = {
        "config": str(target),
        "mac_discovered": discovered,
        "next": [
            f"edit {target} -- set [ssh] target and [pve] template_vmid",
            "proxmox-lab doctor",
        ],
    }
    if discovered is None:
        report["warning"] = (
            f"could not discover the wired NIC's MAC over ssh to "
            f"{probe_target!r}; set [power] mac by hand for 'power wake'"
        )
    print(json.dumps(report, indent=2, sort_keys=True))


# -- doctor ---------------------------------------------------------------

def _guest_configs(ssh: Any) -> list[dict]:
    """Every guest's ``qm``/``pct config`` as parsed key/values.

    Best-effort enumeration for the drift check: a kind whose ``list`` fails
    contributes nothing rather than failing the whole check.
    """
    guests: list[dict] = []
    for kind, tool in (("qemu", "qm"), ("lxc", "pct")):
        listing = ssh.run([tool, "list"], timeout=_REMOTE_CHECK_TIMEOUT)
        if not listing.ok:
            continue
        for line in _text(listing.stdout).splitlines():
            vmid = _VMID_LINE.match(line)
            if vmid is None:
                continue
            cfg = ssh.run(
                [tool, "config", vmid.group(1)], timeout=_REMOTE_CHECK_TIMEOUT
            )
            if not cfg.ok:
                continue
            parsed: dict[str, str] = {}
            for row in _text(cfg.stdout).splitlines():
                field = _CONFIG_LINE.match(row)
                if field is not None and field.group(1) not in parsed:
                    parsed[field.group(1)] = field.group(2)
            guests.append(
                {"kind": kind, "vmid": int(vmid.group(1)), "config": parsed}
            )
    return guests


def _drifted_guests(ssh: Any, state_root: Path) -> list[dict]:
    """pxl guests whose metadata disagrees with ``lab.db`` (§G check 10).

    A guest is ours when the ``pxl`` tag or a ``pxl-lease=`` line says so.
    Drift is an unparseable stamp, an unknown lease id, a guest the store
    does not register to that lease, or an expiry mismatch. Reported, never
    acted on -- the GC is the enforcement side.
    """
    with store_module.Store(Path(state_root) / "lab.db") as store:
        leases = {
            row["id"]: row for row in store.list_leases(include_ended=True)
        }
        owned = {
            (res["kind"], int(res["vmid"])): res["lease_id"]
            for lease_id in leases
            for res in store.resources_for(lease_id)
        }
    drifted: list[dict] = []
    for guest in _guest_configs(ssh):
        cfg = guest["config"]
        tags = {t.strip() for t in str(cfg.get("tags", "")).split(";")}
        lease_id = _PXL_LEASE.search(str(cfg.get("description", "")))
        if "pxl" not in tags and lease_id is None:
            continue
        entry = {"kind": guest["kind"], "vmid": guest["vmid"]}
        if lease_id is None:
            drifted.append({**entry, "detail": "pxl tag but unparseable metadata"})
            continue
        lease = leases.get(lease_id.group(1))
        if lease is None:
            drifted.append(
                {**entry, "detail": f"lease {lease_id.group(1)!r} not in lab.db"}
            )
            continue
        if owned.get((guest["kind"], guest["vmid"])) != lease["id"]:
            drifted.append(
                {**entry, "detail": f"not registered to lease {lease['id']!r}"}
            )
            continue
        expiry = _PXL_EXPIRY.search(str(cfg.get("description", "")))
        if expiry is None or int(expiry.group(1)) != int(lease["expires_at"]):
            drifted.append(
                {**entry, "detail": "pxl-expiry differs from lab.db"}
            )
    return drifted


def cmd_doctor(lab: Any, args: argparse.Namespace) -> None:
    """The §G checklist; exits non-zero when any check fails.

    Every check reports ``{name, ok, detail}``. Warns and skipped remote
    checks (host unreachable or unconfigured) do not fail the run; genuine
    failures do. ``args.host_checks`` is accepted and ignored: with the
    host-transport channel gone there is no optional deeper probe left.
    """
    checks: list[dict[str, Any]] = []
    problems: list[str] = []

    def emit(name: str, ok: bool, detail: str, *, skipped: bool = False) -> None:
        entry: dict[str, Any] = {"name": name, "ok": bool(ok), "detail": detail}
        if skipped:
            entry["skipped"] = True
        checks.append(entry)
        if not ok and not skipped:
            problems.append(f"{name}: {detail}")

    def remote(name: str, run: Any) -> None:
        """A remote check: skipped (never failed) while the host is dark."""
        if not reachable:
            emit(name, False, f"skipped: {skip_reason}", skipped=True)
            return
        try:
            run()
        except Exception as exc:
            emit(name, False, str(exc)[:300])

    config = lab.CONFIG
    state_root = Path(lab.STATE_ROOT).expanduser()

    emit(
        "python_version",
        sys.version_info >= (3, 11),
        f"python {sys.version.split()[0]} (needs >= 3.11)",
    )

    config_error = getattr(lab, "CONFIG_ERROR", None)
    if config_error:
        emit("config", False, f"could not be read: {config_error}")
    elif getattr(config, "configured", False):
        detail = str(config.source)
        unknown = getattr(config, "unknown_sections", None)
        if unknown:
            detail += f" (ignored unknown section(s): {', '.join(unknown)})"
        emit("config", True, detail)
    else:
        emit(
            "config",
            False,
            f"no config file at {config.intended}; run 'proxmox-lab init'",
        )

    target = str(_setting(config, "ssh", "target")).strip()
    emit(
        "ssh_target",
        bool(target),
        f"[ssh] target = {target!r}" if target else "[ssh] target is not set",
    )

    ssh = _lab_ssh(lab) if target else None
    reachable = bool(ssh is not None and ssh.probe())
    skip_reason = "no [ssh] target" if not target else "host unreachable"
    if not target:
        emit("ssh_connect", False, "skipped: no [ssh] target", skipped=True)
    elif reachable:
        emit("ssh_connect", True, f"ssh -o BatchMode=yes {target} answered")
    else:
        emit(
            "ssh_connect",
            False,
            f"ssh to {target} failed or timed out; "
            f"run 'ssh-copy-id root@{target}'",
        )

    def check_remote_tooling() -> None:
        """qm/pct/pvesh/pveversion present -- exercised, not just pathed."""
        broken = []
        for tool, argv in (
            ("qm", ["qm", "list"]),
            ("pct", ["pct", "list"]),
            ("pvesh", ["pvesh", "get", "/version", "--output-format", "json"]),
            ("pveversion", ["pveversion"]),
        ):
            if not ssh.run(argv, timeout=_REMOTE_CHECK_TIMEOUT).ok:
                broken.append(tool)
        if broken:
            emit("remote_tooling", False, f"missing or broken: {', '.join(broken)}")
        else:
            emit("remote_tooling", True, "qm, pct, pvesh, pveversion all respond")

    remote("remote_tooling", check_remote_tooling)

    def check_node_identity() -> None:
        result = ssh.run(["hostname", "-s"], timeout=_REMOTE_CHECK_TIMEOUT)
        host = _text(result.stdout).strip() if result.ok else ""
        node = str(_setting(config, "pve", "node")).strip()
        if not host:
            emit("node_identity", False, "'hostname -s' failed on the host")
        elif node == host:
            emit("node_identity", True, f"[pve] node matches hostname -s ({node})")
        else:
            emit(
                "node_identity",
                True,
                f"warning: [pve] node is {node or '<unset>'!r} but the host "
                f"reports {host!r}",
            )

    remote("node_identity", check_node_identity)

    try:
        state_root.mkdir(parents=True, exist_ok=True)
        with store_module.Store(state_root / "lab.db"):
            pass
        emit(
            "state_dir",
            True,
            f"{state_root / 'lab.db'} opens (schema v{store_module.SCHEMA_VERSION})",
        )
    except Exception as exc:
        emit("state_dir", False, f"{state_root} unusable: {exc}")

    def check_template() -> None:
        vmid = int(_setting(config, "pve", "template_vmid", 0) or 0)
        if not vmid:
            emit("template_vmid", True, "warning: [pve] template_vmid is not set")
            return
        for tool in ("qm", "pct"):
            result = ssh.run(
                [tool, "config", str(vmid)], timeout=_REMOTE_CHECK_TIMEOUT
            )
            if result.ok and _text(result.stdout).strip():
                emit("template_vmid", True, f"{tool} knows vmid {vmid}")
                return
        emit(
            "template_vmid",
            True,
            f"warning: no guest resolves [pve] template_vmid {vmid}",
        )

    remote("template_vmid", check_template)

    mac = str(_setting(config, "power", "mac")).strip()
    emit(
        "wol_mac",
        True,
        f"[power] mac set ({mac})"
        if mac
        else "warning: [power] mac is empty; 'power wake' needs it "
             "('proxmox-lab init' can discover it)",
    )

    def check_gc_cron() -> None:
        report = gc_module.status(ssh)
        if report.get("crontab") == "present":
            emit("gc_cron", True, f"installed ({report.get('path')})")
        else:
            emit(
                "gc_cron",
                True,
                "info: no pxl-gc crontab; install with "
                "'proxmox-lab gc install --host-change-authorized'",
            )

    remote("gc_cron", check_gc_cron)

    def check_drift() -> None:
        drifted = _drifted_guests(ssh, state_root)
        if not drifted:
            emit("drift", True, "no pxl-tagged guests disagree with lab.db")
            return
        shown = "; ".join(
            f"{item['kind']} {item['vmid']} ({item['detail']})"
            for item in drifted[:5]
        )
        emit(
            "drift",
            True,
            f"warning: {len(drifted)} drifted guest(s): {shown}",
        )

    remote("drift", check_drift)

    report = {
        "ok": not problems,
        "version": lab.__version__,
        "config_file": str(config.source) if getattr(config, "source", None) else None,
        "config_expected_at": str(getattr(config, "intended", "")),
        "state_dir": str(state_root),
        "checks": checks,
        "problems": problems,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    if problems:
        raise LabError(f"{len(problems)} problem(s) found")


# -- journal (over the local store) ----------------------------------------

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
        raise LabError(
            f"journal {'/'.join('--' + name.replace('_', '-') for name in removed)}"
            " is gone with the shared ledger: every event is a row in the "
            "local lab store, so there is nothing to upload, carry over, or "
            "summarise. Filter with --lease/--since/--limit."
        )
    rows = journal_module.query_events(
        lease=args.lease, since=args.since, limit=args.limit,
    )
    print(journal_module.format_events(rows))


# -- status ----------------------------------------------------------------

def cmd_status(lab: Any, args: argparse.Namespace) -> None:
    """Host + lease overview. Read-only; works while the host is off."""
    config = lab.CONFIG
    state_root = Path(lab.STATE_ROOT).expanduser()
    target = str(_setting(config, "ssh", "target")).strip()
    node = str(_setting(config, "pve", "node")).strip()

    output: dict[str, Any] = {
        "target": target or None,
        "node": node or None,
        "state_dir": str(state_root),
        "mcp_idle_seconds": int(lab.mcp_idle_elapsed()),
        "idle_shutdown_seconds": int(
            _setting(config, "lease", "idle_shutdown_seconds", 0) or 0
        ),
    }
    try:
        with store_module.Store(state_root / "lab.db") as store:
            output["leases"] = [
                {
                    "id": row["id"],
                    "kind": row["kind"],
                    "purpose": row["purpose"],
                    "state": row["state"],
                    "expires_at": row["expires_at"],
                    "guests": len(store.resources_for(row["id"])),
                }
                for row in store.list_leases(include_ended=True)
            ]
    except Exception as exc:
        output["leases"] = []
        output["leases_error"] = str(exc)[:200]

    ssh = _lab_ssh(lab)
    reachable = bool(target and ssh is not None and ssh.probe())
    output["reachable"] = reachable
    if not reachable:
        output["note"] = (
            "host unreachable -- expected while powered off; "
            "'proxmox-lab power wake' starts it"
            if target
            else "no [ssh] target configured; run 'proxmox-lab init'"
        )
        print(json.dumps(output, indent=2, sort_keys=True))
        return

    prox = proxmox_module.Proxmox(ssh, node or "pve")
    try:
        output["pveversion"] = prox.pveversion()
    except LabError as exc:
        output["pveversion_error"] = str(exc)[:200]
    if node:
        try:
            output["uptime_seconds"] = prox.node_status().get("uptime")
        except LabError:
            pass
    guests: dict[str, list[int]] = {"qemu": [], "lxc": []}
    for kind, tool in (("qemu", "qm"), ("lxc", "pct")):
        result = ssh.run([tool, "list"], timeout=_REMOTE_CHECK_TIMEOUT)
        if result.ok:
            guests[kind] = [
                int(match.group(1))
                for line in _text(result.stdout).splitlines()
                if (match := _VMID_LINE.match(line))
            ]
    output["guests"] = guests
    print(json.dumps(output, indent=2, sort_keys=True))
