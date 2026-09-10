"""Portable controller configuration and explicit credential handoff."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import secrets
import shutil
import sys
from typing import Any

from . import __version__, config, secrets_store
from .errors import LabError

MAX_BUNDLE_BYTES = 2 * 1024 * 1024


def _toml(values: dict[str, Any]) -> str:
    def literal(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (str, int, float, list)):
            return json.dumps(value, ensure_ascii=False, allow_nan=False)
        raise LabError("Unsupported value in connection configuration")
    lines = []
    for key, value in values.items():
        if isinstance(value, dict):
            lines.append(f"[{json.dumps(key)}]")
            lines.extend(f"{json.dumps(k)} = {literal(v)}" for k, v in value.items())
        else:
            lines.append(f"{json.dumps(key)} = {literal(value)}")
    return "\n".join(lines) + "\n"


def export_bundle(settings: config.Config, *, include_ssh_key: bool = False) -> dict:
    for key in ("host", "node", "token_user", "token_name"):
        settings.require("proxmox." + key)
    values = copy.deepcopy(settings.as_dict())
    # Each controller owns its own local spool, identity, paths and leases.
    values["audit"]["controller_id"] = ""
    values["audit"]["journal_dir"] = ""
    values["secrets"] = {"backend": "file", "file_path": ""}
    attachments = {}
    ca = values["proxmox"].get("ca_file")
    if ca:
        attachments["ca.pem"] = Path(ca).expanduser().read_text(encoding="utf-8")
    values["proxmox"]["ca_file"] = ""
    notes = []
    ssh = values["memflow"]
    if include_ssh_key:
        if not ssh.get("enabled") or not ssh.get("ssh_key"):
            raise LabError("--include-ssh-key needs an enabled host SSH channel with ssh_key configured")
        attachments["host-ssh-key"] = Path(ssh["ssh_key"]).expanduser().read_text(encoding="utf-8")
    elif ssh.get("enabled"):
        ssh["enabled"] = False
        notes.append("Host SSH is disabled on the recipient; export with --include-ssh-key to carry the configured key.")
    ssh["ssh_key"] = ""
    ssh["ssh_options"] = ""
    names = {"proxmox-token", str(values["audit"].get("password_secret") or "mariadb-password")}
    if "home-assistant" in values["power"]["mode"]:
        names.add("home-assistant-token")
    if values["s3"]["enabled"]:
        names.update(("s3-key-id", "s3-secret-key"))
    if values["vpn"]["enabled"]:
        names.update(("wg-private-key", "wg-peer-public-key", "wg-preshared-key"))
    if values["share"]["enabled"] and values["share"]["tunnel"] == "ngrok":
        names.add("ngrok-authtoken")
    credentials = {}
    for name in sorted(names):
        value = secrets_store.get(settings, name, required=name == "proxmox-token")
        if value:
            credentials[name] = value
        elif name != "wg-preshared-key":
            notes.append(f"{name} was unavailable; configure it on the recipient if needed.")
    if values["power"]["mode"] == "command":
        notes.append("Custom power commands must also exist on the recipient machine.")
    return {"format": "proxmox-agent-lab-connection", "version": 1,
            "config": values, "secrets": credentials, "files": attachments, "notes": notes}


def _validate(bundle: Any) -> dict:
    # Do not echo malformed payloads: they contain credentials.
    if not isinstance(bundle, dict) or bundle.get("format") != "proxmox-agent-lab-connection" or bundle.get("version") != 1:
        raise LabError("Unsupported connection bundle")
    values = bundle.get("config")
    if not isinstance(values, dict) or set(values) != set(config.DEFAULTS):
        raise LabError("Connection bundle has invalid configuration sections")
    for section, defaults in config.DEFAULTS.items():
        table = values[section]
        if not isinstance(table, dict) or set(table) != set(defaults):
            raise LabError("Connection bundle has unsupported configuration fields")
        for key, value in table.items():
            if type(value) is not type(defaults[key]):
                raise LabError("Connection bundle has an invalid configuration value")
            if isinstance(value, list) and any(not isinstance(x, str) for x in value):
                raise LabError("Connection bundle has an invalid list value")
    for key in ("host", "node", "token_user", "token_name"):
        if not values["proxmox"].get(key):
            raise LabError("Connection bundle is missing Proxmox connection settings")
    credentials = bundle.get("secrets")
    allowed = set(secrets_store.KNOWN_SECRETS) | {values["audit"].get("password_secret", "mariadb-password")}
    if not isinstance(credentials, dict) or set(credentials) - allowed or any(not isinstance(v, str) for v in credentials.values()):
        raise LabError("Connection bundle has invalid credentials")
    if not credentials.get("proxmox-token"):
        raise LabError("Connection bundle is missing the API token")
    files = bundle.get("files")
    if not isinstance(files, dict) or set(files) - {"ca.pem", "host-ssh-key"} or any(not isinstance(v, str) for v in files.values()):
        raise LabError("Connection bundle has invalid attachments")
    return copy.deepcopy(bundle)


def _private_write(path: Path, body: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(body)


def import_bundle(bundle: Any, directory: Path) -> Path:
    bundle = _validate(bundle)
    directory = directory.expanduser().absolute()
    values = bundle["config"]
    values["secrets"] = {"backend": "file", "file_path": str(directory / "secrets.toml")}
    values["audit"]["controller_id"] = ""
    values["audit"]["journal_dir"] = ""
    values["proxmox"]["ca_file"] = str(directory / "ca.pem") if "ca.pem" in bundle["files"] else ""
    values["memflow"]["ssh_key"] = str(directory / "host-ssh-key") if "host-ssh-key" in bundle["files"] else ""
    values["memflow"]["ssh_options"] = ""
    if "host-ssh-key" not in bundle["files"]:
        values["memflow"]["enabled"] = False
    config_body, secret_body = _toml(values), _toml(bundle["secrets"])
    directory.parent.mkdir(parents=True, exist_ok=True)
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        raise LabError("Destination already exists; use --directory with a new directory") from None
    try:
        _private_write(directory / "secrets.toml", secret_body)
        for name, body in bundle["files"].items():
            _private_write(directory / name, body)
        _private_write(directory / "config.toml", config_body)
    except BaseException:
        shutil.rmtree(directory)
        raise
    return directory / "config.toml"


def shell_handoff(bundle: dict) -> str:
    body = json.dumps(bundle, ensure_ascii=False, indent=2)
    delimiter = "PXL_CONNECTION_" + secrets.token_hex(12)
    while delimiter in body:
        delimiter = "PXL_CONNECTION_" + secrets.token_hex(12)
    # All private data goes to the importer on stdin, never argv or pip.
    return f'''# Private connection handoff: contains credentials. Paste into bash/zsh on the recipient.
(
set -eu
pxl_python=""
for candidate in python3.14 python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
        pxl_python="$candidate"; break
    fi
done
[ -n "$pxl_python" ] || {{ echo "Python 3.11+ is required" >&2; exit 1; }}
pxl_env="$HOME/.local/share/proxmox-agent-lab/connection-env-{__version__}"
"$pxl_python" -m venv "$pxl_env"
"$pxl_env/bin/python" -m pip install --quiet --disable-pip-version-check --timeout 30 --retries 2 'https://github.com/jr551/proxmox-agent-lab/releases/download/v{__version__}/proxmox_agent_lab-{__version__}-py3-none-any.whl'
"$pxl_env/bin/proxmox-lab" connection import <<'{delimiter}'
{body}
{delimiter}
printf 'Run: "%s/bin/proxmox-lab" doctor\\n' "$pxl_env"
)
'''


def cmd_export(lab: Any, args: Any) -> None:
    try:
        bundle = export_bundle(lab.CONFIG, include_ssh_key=args.include_ssh_key)
    except OSError as exc:
        raise LabError(f"Could not read a connection attachment: {exc}") from None
    output = shell_handoff(bundle) if args.format == "shell" else json.dumps(bundle, indent=2) + "\n"
    if len(output.encode("utf-8")) > MAX_BUNDLE_BYTES:
        raise LabError("Connection bundle is too large")
    if args.out:
        try:
            _private_write(Path(args.out).expanduser(), output)
        except FileExistsError:
            raise LabError("Output already exists; choose a new handoff filename") from None
        except OSError as exc:
            raise LabError(f"Could not write the private handoff: {exc}") from None
        print(json.dumps({"written": str(Path(args.out).expanduser()), "contains_secrets": True}))
    else:
        print(output, end="")
    for note in bundle["notes"]:
        print(note, file=sys.stderr)


def cmd_import(lab: Any, args: Any) -> None:
    raw = sys.stdin.read(MAX_BUNDLE_BYTES + 1)
    if len(raw.encode("utf-8")) > MAX_BUNDLE_BYTES:
        raise LabError("Connection bundle is too large")
    try:
        bundle = json.loads(raw)
    except (ValueError, RecursionError):
        raise LabError("Invalid connection bundle JSON") from None
    try:
        target = import_bundle(bundle, Path(args.directory) if args.directory else config.default_config_path().parent)
    except OSError as exc:
        raise LabError(f"Could not save the connection: {exc}") from None
    print(json.dumps({"config": str(target), "secrets_backend": "file",
                      "next": "proxmox-lab doctor", "config_env": {config.ENV_CONFIG: str(target)}}, indent=2))


def register(sub: Any, lab: Any) -> None:
    from .cli import _bind
    parser = sub.add_parser("connection", help="share connection settings and credentials with another controller")
    commands = parser.add_subparsers(dest="connection_command", required=True)
    export = commands.add_parser("export", help="generate a private pasteable setup block including secrets")
    export.add_argument("--format", choices=("shell", "json"), default="shell")
    export.add_argument("--out", help="create a private file instead of printing credentials")
    export.add_argument("--include-ssh-key", action="store_true", help="also transfer the configured host SSH private key")
    export.set_defaults(func=_bind(lab, cmd_export))
    incoming = commands.add_parser("import", help="read a private JSON connection bundle from stdin")
    incoming.add_argument("--directory", help="new configuration directory (default: the user config directory)")
    incoming.set_defaults(func=_bind(lab, cmd_import))
