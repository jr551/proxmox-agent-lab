"""Secret storage, abstracted over the platform's keyring.

Secrets are never written to the config file, passed on a command line, or
recorded in the audit journal. They are fetched at the moment of use and kept
only in memory.

Backends, in order of preference when `backend = "auto"`:

* `keychain`    -- macOS `security`
* `secret-tool` -- Linux, libsecret (GNOME Keyring, KWallet)
* `env`         -- environment variables, for CI and containers
* `file`        -- a 0600 TOML file, for headless hosts with no keyring
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

from .config import APP_NAME, Config

# The names this tool asks for. Documented so `secrets list` can show them.
KNOWN_SECRETS = {
    "proxmox-token": "Proxmox API token secret (the UUID shown once on creation)",
    "home-assistant-token": "Home Assistant long-lived token (power mode only)",
    "audit-token": "PocketBase API token for audit storage",
    "pocketbase-superuser-email": "PocketBase superuser email for agent provisioning",
    "pocketbase-superuser-password": "PocketBase superuser password for agent provisioning",
    "pocketbase-agent-email": "PocketBase restricted audit agent email",
    "pocketbase-agent-password": "PocketBase restricted audit agent password",
    "s3-key-id": "S3 access key id (scratch bucket)",
    "s3-secret-key": "S3 secret access key (scratch bucket)",
    "wg-private-key": "WireGuard client private key",
    "wg-preshared-key": "WireGuard preshared key",
    "wg-peer-public-key": "WireGuard server public key",
    "ngrok-authtoken": "ngrok authtoken, for sharing a guest console",
    "nvidia-api-key": "NVIDIA API key for opt-in screenshot vision analysis",
    "openrouter-api-key": "OpenRouter API key for fallback vision analysis",
    "kilo-api-key": "Kilo Code gateway key for fallback vision analysis",
}


class SecretError(RuntimeError):
    pass


def _env_name(name: str) -> str:
    return "PROXMOX_AGENT_LAB_" + name.upper().replace("-", "_")


def detect_backend() -> str:
    if shutil.which("security") and os.uname().sysname == "Darwin":
        return "keychain"
    if shutil.which("secret-tool"):
        return "secret-tool"
    return "env"


def _resolve(config: Config) -> str:
    backend = config.secrets.get("backend", "auto")
    return detect_backend() if backend in ("", "auto") else backend


def _file_path(config: Config) -> Path:
    configured = config.secrets.get("file_path") or ""
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".config" / APP_NAME / "secrets.toml"


def _read_file_secret(config: Config, name: str) -> str | None:
    path = _file_path(config)
    if not path.is_file():
        return None
    mode = path.stat().st_mode & 0o077
    if mode:
        raise SecretError(
            f"{path} is readable by other users; run: chmod 600 {path}"
        )
    try:
        import tomllib
        with path.open("rb") as handle:
            return tomllib.load(handle).get(name)
    except Exception as exc:  # noqa: BLE001 - surfaced verbatim to the user
        raise SecretError(f"cannot read {path}: {exc}") from None


def get(config: Config, name: str, *, required: bool = True) -> str:
    """Fetch one secret. Returns "" when optional and absent."""
    backend = _resolve(config)
    value: str | None = None

    if backend == "keychain":
        result = subprocess.run(
            ["security", "find-generic-password", "-a", name,
             "-s", APP_NAME, "-w"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False,
        )
        value = result.stdout.strip() if result.returncode == 0 else None
    elif backend == "secret-tool":
        result = subprocess.run(
            ["secret-tool", "lookup", "service", APP_NAME, "account", name],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False,
        )
        value = result.stdout.strip() if result.returncode == 0 else None
    elif backend == "env":
        value = os.environ.get(_env_name(name))
    elif backend == "file":
        value = _read_file_secret(config, name)
    else:
        raise SecretError(f"unknown secrets backend: {backend!r}")

    if value:
        return value
    # An env var always wins as a fallback, so CI can override any backend.
    fallback = os.environ.get(_env_name(name))
    if fallback:
        return fallback
    if not required:
        return ""
    raise SecretError(
        f"secret {name!r} is not stored ({KNOWN_SECRETS.get(name, 'unknown secret')}).\n"
        f"Store it with:  proxmox-lab secrets set {name}\n"
        f"Backend in use: {backend}"
    )


def store(config: Config, name: str, value: str) -> str:
    """Save one secret; returns the backend that accepted it."""
    backend = _resolve(config)
    if backend == "keychain":
        subprocess.run(["security", "delete-generic-password", "-a", name,
                        "-s", APP_NAME], capture_output=True, check=False)
        result = subprocess.run(
            ["security", "add-generic-password", "-a", name, "-s", APP_NAME,
             "-w", value, "-U"],
            capture_output=True, text=True, check=False,
        )
        if result.returncode:
            raise SecretError(f"keychain rejected the secret: {result.stderr.strip()}")
    elif backend == "secret-tool":
        result = subprocess.run(
            ["secret-tool", "store", "--label", f"{APP_NAME} {name}",
             "service", APP_NAME, "account", name],
            input=value, text=True, capture_output=True, check=False,
        )
        if result.returncode:
            raise SecretError(f"secret-tool rejected the secret: {result.stderr.strip()}")
    elif backend == "file":
        path = _file_path(config)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing: dict[str, str] = {}
        if path.is_file():
            import tomllib
            with path.open("rb") as handle:
                existing = tomllib.load(handle)
        existing[name] = value
        body = "".join(
            f'{key} = "{val}"\n' for key, val in sorted(existing.items())
        )
        path.write_text("# proxmox-agent-lab secrets. Keep this file at 0600.\n" + body)
        path.chmod(0o600)
    elif backend == "env":
        raise SecretError(
            "the env backend is read-only; export "
            f"{_env_name(name)} instead, or set [secrets] backend to "
            '"keychain", "secret-tool" or "file"'
        )
    else:
        raise SecretError(f"unknown secrets backend: {backend!r}")
    return backend


def status(config: Config) -> dict[str, object]:
    backend = _resolve(config)
    present = {}
    for name in KNOWN_SECRETS:
        try:
            present[name] = bool(get(config, name, required=False))
        except SecretError:
            present[name] = False
    return {"backend": backend, "stored": present, "known": KNOWN_SECRETS}
