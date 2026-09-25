"""Site configuration.

Everything that differs between one person's lab and another's lives here,
loaded from a TOML file rather than baked into the code. Nothing in this
module raises on import: an unconfigured install must still be able to run
`proxmox-lab init` and `--help`. Features complain only when actually used,
through `require()`.

The canonical schema is the small one (docs/rework-plan.md §G)::

    [ssh]    target                      ssh alias/host reached as root
    [pve]    node, template_vmid
    [power]  mac, broadcast, port
    [state]  dir                         lab.db lives here
    [lease]  ttl_seconds, idle_shutdown_seconds

Note: ``[ssh] target`` replaces the old ``[memflow] ssh_host`` as the ssh
gate. The ssh transport is the whole control plane and is gated on
``[ssh] target`` alone; nothing gates on ``[memflow]`` any more.

TRANSITIONAL: the old attribute namespaces (``proxmox``, ``audit``,
``memflow``, ``storage``, ``lease.default_ttl_seconds``, ...) still resolve --
from defaults, and from the file when present -- so the modules that die in a
later wave keep importing. They are deleted with those modules; nothing new
should read them. ``lease.default_ttl_seconds`` is kept equal to the canonical
``lease.ttl_seconds``.

Search order for the config file:

1. `$PROXMOX_AGENT_LAB_CONFIG`
2. `./proxmox-agent-lab.toml` (handy for a checkout)
3. `$XDG_CONFIG_HOME/proxmox-agent-lab/config.toml`
4. `~/.config/proxmox-agent-lab/config.toml`

A missing or unparseable config never raises at import: `get()` records
`CONFIG_ERROR` and returns a defaulted config, while `load()` raises
`ConfigError` only when called explicitly -- so `doctor` can report the
broken file on an install that must first survive it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - only on older interpreters
    tomllib = None  # type: ignore[assignment]

APP_NAME = "proxmox-agent-lab"
ENV_CONFIG = "PROXMOX_AGENT_LAB_CONFIG"
ENV_STATE = "PROXMOX_AGENT_LAB_STATE"

DEFAULTS: dict[str, Any] = {
    # -- canonical schema (docs/rework-plan.md §G) --------------------------
    "ssh": {
        # The ssh transport is the whole control plane; this one setting is
        # the gate. Replaces the old [memflow] ssh_host.
        "target": "",
    },
    "pve": {
        "node": "pve",
        "template_vmid": 0,
    },
    "power": {
        "mac": "",
        "broadcast": "255.255.255.255",
        "port": 9,
        # Transitional power-mode knobs, read only by the dying power/doctor
        # code. Nothing new reads them.
        "mode": "wake-on-lan",
        "wowlan_mac": "",
        "wol_port": 9,
        "boot_timeout_seconds": 300,
        "home_assistant_url": "",
        "entity_on": "",
        "entity_off": "",
        "on_command": "",
        "off_command": "",
    },
    "state": {
        # lab.db lives here.
        "dir": "~/.local/share/proxmox-agent-lab",
    },
    "lease": {
        "ttl_seconds": 2 * 60 * 60,
        "idle_shutdown_seconds": 8 * 60 * 60,
        # Transitional: an alias of ttl_seconds, kept equal by _reconcile so
        # the old readers keep working. Deleted with them.
        "default_ttl_seconds": 2 * 60 * 60,
        # Transitional backup knobs for the dying backup code.
        # Long-term leases keep the machine on and their guests alive.
        "long_term_backup": True,
        "long_term_backup_storage": "",   # defaults to storage.bulk_storage
        "long_term_backup_keep": 2,
        "retained_backup": False,
        "retained_backup_interval_days": 7,
    },
    # -- transitional namespaces --------------------------------------------
    # Read only by modules that die in a later wave; resolved here (defaults,
    # plus whatever the file supplies) so those imports cannot crash. Deleted
    # with those modules.
    "proxmox": {
        "host": "",
        "port": 8006,
        "node": "",
        "token_user": "",
        "token_name": "",
        "verify_tls": False,
        "ca_file": "",
        "guest_mode": "all",  # all | lxc-only (VPS, no host power-off)
    },
    "storage": {
        "upload_storages": ["local"],
        "bulk_storage": "local",
    },
    "network": {
        "lab_bridge": "vmbr1",
        "lab_network": "10.66.0.0/24",
        "lab_gateway_ip": "10.66.0.1",
        "dhcp_start": "10.66.0.50",
        "dhcp_end": "10.66.0.200",
        "gateway_template_vmid": 0,
    },
    "vpn": {
        "enabled": False,
        "address": "",
        "dns": "",
        "endpoint": "",
        "keepalive": 25,
    },
    "s3": {
        "enabled": False,
        "endpoint": "",
        "bucket": "",
        "region": "us-east-1",
    },
    "share": {
        "enabled": False,
        "worker_vmid": 0,
        "port": 8900,
        "default_minutes": 30,
        "max_minutes": 480,
        "tunnel": "cloudflared",   # cloudflared | ngrok | none
        "novnc_version": "1.6.0",
        "ngrok_region": "",
    },
    "memflow": {
        # Transitionally still resolvable. ssh_host defaults to "" and is no
        # longer the ssh gate -- [ssh] target is.
        "enabled": False,
        "ssh_host": "",
        "ssh_user": "root",
        "ssh_port": 22,
        "ssh_key": "",
        "ssh_options": "",
        "helper": "pxl-memflow-run",
        "connect_timeout": 10,
    },
    "android": {
        "api_level": 33,
        "abi": "x86_64",
    },
    "windows": {
        "template_2025_vmid": 0,
        "template_2022_vmid": 0,
    },
    "secrets": {
        "backend": "auto",
        "file_path": "",
    },
    "audit": {
        "host": "",            # defaults to the [proxmox] host
        "port": 3306,
        "database": "proxmox_lab",
        "user": "proxmox_lab",
        "password_secret": "mariadb-password",
        "timeout_seconds": 10,
        "journal_dir": "",     # local spool only
        "controller_id": "",
    },
}


class ConfigError(RuntimeError):
    pass


def state_dir() -> Path:
    """Where runtime state lives; `<state dir>/lab.db` and friends.

    `$PROXMOX_AGENT_LAB_STATE` overrides (the test bootstrap points it at a
    per-process temp directory); otherwise the expanded `[state] dir` setting.
    """
    override = os.environ.get(ENV_STATE)
    if override:
        return Path(override).expanduser()
    return Path(str(get().state.dir)).expanduser()


def default_config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / APP_NAME / "config.toml"


def config_path() -> Path | None:
    """Where the config lives, whether or not the file exists yet."""
    override = os.environ.get(ENV_CONFIG)
    if override:
        return Path(override).expanduser()
    local = Path.cwd() / f"{APP_NAME}.toml"
    if local.is_file():
        return local
    return default_config_path()


def defaults() -> "Config":
    """A fully-defaulted config, for when the real one cannot be read."""
    return Config(_reconcile(_merge(DEFAULTS, {})), None)


# The last error hit while reading the config, if any. Recorded, never raised,
# by `get()` so a broken install can still run `init`/`doctor`; `load()`
# surfaces the same problem as a `ConfigError` when called explicitly.
CONFIG_ERROR: str | None = None

_CACHED: "Config | None" = None


def get() -> "Config":
    """The process-wide configuration.

    Every module shares one instance. Loading per module would mean several
    reads of the same file and, worse, the possibility of two modules
    disagreeing about the same setting. Never raises: a broken file records
    `CONFIG_ERROR` and yields the defaults instead.
    """
    global _CACHED, CONFIG_ERROR
    if _CACHED is None:
        try:
            _CACHED = load()
        except ConfigError as exc:
            # Never fail at import; `doctor` reports the problem instead.
            CONFIG_ERROR = str(exc)
            _CACHED = defaults()
    return _CACHED


def reset_cache() -> None:
    """Forget the cached config. For tests, and after `init` writes one."""
    global _CACHED, CONFIG_ERROR
    _CACHED = None
    CONFIG_ERROR = None


def _merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = {key: dict(value) if isinstance(value, dict) else value
           for key, value in base.items()}
    for key, value in overlay.items():
        existing = out.get(key)
        if isinstance(value, dict) and isinstance(existing, dict):
            out[key] = _merge(out[key], value)
        elif isinstance(value, dict) != isinstance(existing, dict):
            if isinstance(existing, dict):
                raise ConfigError(
                    f"section [{key}] must be a table, not a {type(value).__name__}"
                )
            raise ConfigError(
                f"setting [{key}] must be a scalar, not a {type(value).__name__}"
            )
        else:
            out[key] = value
    return out


def _bridge_legacy(loaded: dict[str, Any]) -> dict[str, Any]:
    """Accept the pre-§G spelling of the lease ttl. Canonical wins."""
    lease = loaded.get("lease")
    if (isinstance(lease, dict) and "ttl_seconds" not in lease
            and "default_ttl_seconds" in lease):
        lease["ttl_seconds"] = lease["default_ttl_seconds"]
    return loaded


def _reconcile(values: dict[str, Any]) -> dict[str, Any]:
    """Keep the transitional aliases equal to their canonical settings."""
    values["lease"]["default_ttl_seconds"] = values["lease"]["ttl_seconds"]
    return values


class Section:
    """Attribute access over one config table, with a helpful failure mode."""

    def __init__(self, name: str, values: dict[str, Any]) -> None:
        self._name = name
        self._values = values

    def __getattr__(self, key: str) -> Any:
        try:
            return self._values[key]
        except KeyError:
            raise AttributeError(
                f"unknown setting [{self._name}] {key}"
            ) from None

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def get(self, key: str, fallback: Any = None) -> Any:
        return self._values.get(key, fallback)

    def as_dict(self) -> dict[str, Any]:
        return dict(self._values)


class Config:
    def __init__(self, values: dict[str, Any], source: Path | None,
                 intended: Path | None = None) -> None:
        self._values = values
        self.source = source
        # Where the config *would* live, even when it does not exist yet, so
        # `init` writes to the path the user asked for and `doctor` can say
        # which file it looked for.
        self.intended = intended or source or default_config_path()
        self.unknown_sections: list[str] = []
        for name, table in values.items():
            if isinstance(table, dict):
                setattr(self, name, Section(name, table))

    @property
    def configured(self) -> bool:
        return self.source is not None

    def require(self, dotted: str, hint: str = "") -> Any:
        """Return a setting, or explain precisely what to configure."""
        section, _, key = dotted.partition(".")
        value = self._values.get(section, {}).get(key)
        if value in (None, "", [], 0) and not isinstance(value, bool):
            where = self.source or default_config_path()
            raise ConfigError(
                f"[{section}] {key} is not set. Add it to {where}"
                + (f" -- {hint}" if hint else "")
                + ("\nRun 'proxmox-lab init' to create a starter config."
                   if not self.configured else "")
            )
        return value

    def as_dict(self) -> dict[str, Any]:
        return {name: dict(table) if isinstance(table, dict) else table
                for name, table in self._values.items()}


def load(path: Path | None = None) -> Config:
    """Load configuration, falling back to defaults when absent.

    Raises `ConfigError` for a file that exists but cannot be read or parsed:
    this is the explicit-use path (`doctor` reports it); `get()` is the
    import-time path and swallows the same failure.
    """
    chosen = path if path is not None else config_path()
    # A config file that does not exist yet is not an error: `init` has to be
    # able to run, and it is the command that creates it.
    if chosen is None or not chosen.is_file():
        return Config(_reconcile(_merge(DEFAULTS, {})), None, intended=chosen)
    if tomllib is None:
        raise ConfigError("Python 3.11 or newer is required to read the config")
    try:
        with chosen.open("rb") as handle:
            loaded = tomllib.load(handle)
    except OSError as exc:
        raise ConfigError(f"cannot read {chosen}: {exc}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{chosen} is not valid TOML: {exc}") from None
    # An unknown section is a warning, never fatal. A section left behind by
    # another version -- or by a feature that has since been removed -- must
    # not discard the whole config and silently fall back to defaults, which
    # presents as "host is not set" and sends people hunting in the wrong
    # place entirely.
    loaded = _bridge_legacy(loaded)
    unknown = sorted(set(loaded) - set(DEFAULTS))
    for name in unknown:
        loaded.pop(name, None)
    config = Config(_reconcile(_merge(DEFAULTS, loaded)), chosen)
    config.unknown_sections = unknown
    return config


TEMPLATE = """\
# proxmox-agent-lab configuration
#
# Written by 'proxmox-lab init'. Copy to ~/.config/proxmox-agent-lab/config.toml
# and edit. Every value here is site-specific; nothing secret belongs in this
# file -- the transport is ssh with your agent/keys only.

[ssh]
target = "proxmox"           # ssh alias/host reached as root

[pve]
node = "pve"                 # node name used in pvesh paths
template_vmid = 100          # default template for guest clone/create

[power]
mac = ""                     # wired NIC MAC for WoL (filled by 'proxmox-lab init')
broadcast = "255.255.255.255"
port = 9

[state]
dir = "~/.local/share/proxmox-agent-lab"   # lab.db lives here

[lease]
ttl_seconds = 7200           # work is cleaned up if a lease is not renewed
idle_shutdown_seconds = 28800

# --- transitional: read only by the modules that die in a later wave (and by
# install.sh's config substitution, which dies with them). Nothing new reads
# these; they are deleted alongside those modules. --------------------------

[proxmox]
host = ""                    # address of the Proxmox host
port = 8006
node = "pve"                 # node name, as shown in the Proxmox UI
token_user = ""              # API token owner (transitional)
token_name = ""              # API token id (transitional)
verify_tls = false
ca_file = ""
guest_mode = "all"           # all | lxc-only (VPS, no host shutdown)

[secrets]
backend = "auto"             # transitional secret-backend knob (dying)
file_path = ""

[s3]
enabled = false
endpoint = ""
bucket = ""
region = "us-east-1"
"""
