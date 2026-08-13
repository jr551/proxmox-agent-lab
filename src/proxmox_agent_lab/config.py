"""Site configuration.

Everything that differs between one person's lab and another's lives here,
loaded from a TOML file rather than baked into the code. Nothing in this
module raises on import: an unconfigured install must still be able to run
`proxmox-lab init` and `--help`. Features complain only when actually used,
through `require()`.

Search order for the config file:

1. `$PROXMOX_AGENT_LAB_CONFIG`
2. `./proxmox-agent-lab.toml` (handy for a checkout)
3. `$XDG_CONFIG_HOME/proxmox-agent-lab/config.toml`
4. `~/.config/proxmox-agent-lab/config.toml`
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
    "proxmox": {
        "host": "",
        "port": 8006,
        "node": "",
        "token_user": "",
        "token_name": "",
        "verify_tls": False,
    },
    "lease": {
        "default_ttl_seconds": 2 * 60 * 60,
        "idle_shutdown_seconds": 8 * 60 * 60,
        # Long-term leases keep the machine on and their guests alive.
        "long_term_backup": True,
        "long_term_backup_storage": "",   # defaults to storage.bulk_storage
        "long_term_backup_keep": 2,
    },
    "power": {
        # how to switch the lab machine on: wake-on-lan | home-assistant
        # | command | none
        "mode": "wake-on-lan",
        "mac": "",
        "broadcast": "255.255.255.255",
        "wol_port": 9,
        "boot_timeout_seconds": 300,
        # home-assistant mode
        "home_assistant_url": "",
        "entity_on": "",
        "entity_off": "",
        # command mode
        "on_command": "",
        "off_command": "",
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
        # Disposable, pre-authenticated links to a guest console.
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
        # Advanced, opt-in: agentless guest introspection with memflow. Reads
        # a running guest's memory from the hypervisor over SSH (not the API
        # token), so it is a separate trust boundary and stays off until both
        # enabled and ssh_host are set. Needs no patched kernel. See
        # docs/memflow.md.
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
        # Emulated phones. x86_64 uses nested KVM and is usable; arm64-v8a is
        # real ARM but fully emulated and very slow on an x86 host.
        "api_level": 33,
        "abi": "x86_64",
    },
    "windows": {
        # Retained installer templates are site inventory, never universal
        # constants. A command-line override is also available for one-off
        # runs against a different template.
        "template_2025_vmid": 0,
        "template_2022_vmid": 0,
    },
    "secrets": {
        # keychain (macOS) | secret-tool (Linux) | env | file
        "backend": "auto",
        "file_path": "",
    },
    "audit": {
        "backend": "sqlite",   # sqlite | jsonl | pocketbase
        "journal_dir": "",     # defaults to <state>/journal
        "git_sync": False,     # copy redacted events to a private git repo
        "git_repo": "",
        "git_branch": "logs",
        "controller_id": "",
        "pocketbase_url": "",
        "pocketbase_collection": "proxmox_lab_events",
        "pocketbase_token_secret": "audit-token",
        "pocketbase_timeout_seconds": 10,
    },
}


class ConfigError(RuntimeError):
    pass


def state_dir() -> Path:
    override = os.environ.get(ENV_STATE)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "state" / APP_NAME


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
    return Config(_merge(DEFAULTS, {}), None)


_CACHED: "Config | None" = None


def get() -> "Config":
    """The process-wide configuration.

    Every module shares one instance. Loading per module would mean several
    reads of the same file and, worse, the possibility of two modules
    disagreeing about the same setting.
    """
    global _CACHED
    if _CACHED is None:
        try:
            _CACHED = load()
        except ConfigError:
            # Never fail at import; `doctor` reports the problem instead.
            _CACHED = defaults()
    return _CACHED


def reset_cache() -> None:
    """Forget the cached config. For tests, and after `init` writes one."""
    global _CACHED
    _CACHED = None


def _merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = {key: dict(value) if isinstance(value, dict) else value
           for key, value in base.items()}
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


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
    """Load configuration, falling back to defaults when absent."""
    chosen = path if path is not None else config_path()
    # A config file that does not exist yet is not an error: `init` has to be
    # able to run, and it is the command that creates it.
    if chosen is None or not chosen.is_file():
        return Config(_merge(DEFAULTS, {}), None, intended=chosen)
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
    unknown = sorted(set(loaded) - set(DEFAULTS))
    for name in unknown:
        loaded.pop(name, None)
    config = Config(_merge(DEFAULTS, loaded), chosen)
    config.unknown_sections = unknown
    return config


TEMPLATE = """\
# proxmox-agent-lab configuration
#
# Copy to ~/.config/proxmox-agent-lab/config.toml and edit. Every value here
# is site-specific; nothing secret belongs in this file. Secrets live in your
# OS keyring -- see 'proxmox-lab secrets --help'.

[proxmox]
host = "192.168.1.50"        # address of the Proxmox host
port = 8006
node = "pve"                 # node name, as shown in the Proxmox UI
token_user = "agent@pve"     # API token owner
token_name = "lab"           # API token id
verify_tls = false           # true once you trust the host certificate

[lease]
default_ttl_seconds = 7200   # work is cleaned up if a lease is not renewed
idle_shutdown_seconds = 28800
# Long-term leases (proxmox-lab lease-begin --long-term) keep their machines
# alive and the host powered on. These control their weekly backup.
long_term_backup = true
long_term_backup_storage = ""   # blank = [storage] bulk_storage
long_term_backup_keep = 2

[power]
# How to switch the machine on. Wake-on-LAN needs nothing but the NIC's MAC
# and works on almost any desktop; enable WoL in its BIOS first.
mode = "wake-on-lan"         # wake-on-lan | home-assistant | command | none
mac = "aa:bb:cc:dd:ee:ff"
broadcast = "192.168.1.255"
boot_timeout_seconds = 300

# mode = "home-assistant"
# home_assistant_url = "https://homeassistant.example"
# entity_on = "script.lab_power_on"
# entity_off = "script.lab_force_off"

# mode = "command"
# on_command = "/usr/local/bin/lab-power-on"
# off_command = "/usr/local/bin/lab-force-off"

[storage]
upload_storages = ["local"]  # storages this tool may upload into
bulk_storage = "local"       # where big images and ISOs go

[network]
# Only needed for the forced-VPN gateway.
lab_bridge = "vmbr1"
lab_network = "10.66.0.0/24"
lab_gateway_ip = "10.66.0.1"
dhcp_start = "10.66.0.50"
dhcp_end = "10.66.0.200"
gateway_template_vmid = 0    # VMID of a Debian/Ubuntu cloud-init template

[vpn]
enabled = false              # true to route all lab egress through WireGuard
address = ""                 # e.g. "10.100.0.2/32" from your provider
dns = ""                     # e.g. "10.100.0.1"
endpoint = ""                # e.g. "vpn.example.com:51820"
keepalive = 25

[s3]
enabled = false              # optional scratch bucket for guest file transfer
endpoint = ""
bucket = ""
region = "us-east-1"

[share]
# Optional: send someone a link to one VM's screen. Needs an ngrok authtoken
# (proxmox-lab secrets set ngrok-authtoken) and a worker built with
# 'proxmox-lab share setup'.
enabled = false
worker_vmid = 0
tunnel = "cloudflared"       # needs no account; "ngrok" or "none" also work
default_minutes = 30
max_minutes = 480

[memflow]
# Advanced, opt-in: agentless guest introspection with memflow. Reads a
# running guest's memory from the hypervisor over SSH (a separate trust
# boundary from the API token); needs no patched kernel. Prepare the host with
# 'proxmox-lab memflow host-setup'. See docs/memflow.md.
enabled = false
# ssh_host = "192.168.1.50"  # the Proxmox host; memflow runs resident there
# ssh_user = "root"          # needs to read /proc/<qemu-pid>/mem
# ssh_key = "~/.ssh/pxl_vmi" # path to a key file, never the key itself

[windows]
# Retained Windows installer templates. Leave at 0 until you have created the
# corresponding template; `windows install --template-vmid` overrides either.
template_2025_vmid = 0
template_2022_vmid = 0

[secrets]
backend = "auto"             # auto | keychain | secret-tool | env | file

[audit]
backend = "sqlite"           # sqlite | jsonl | pocketbase
git_sync = false             # copy redacted events to a private git repo
git_repo = ""                # dedicated private logging checkout
git_branch = "logs"
controller_id = ""           # defaults to the controller hostname
pocketbase_url = ""          # e.g. https://rowedb.example
pocketbase_collection = "proxmox_lab_events"
pocketbase_token_secret = "audit-token"
pocketbase_timeout_seconds = 10
"""
