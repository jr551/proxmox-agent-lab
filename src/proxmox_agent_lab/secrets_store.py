"""Secret storage, abstracted over the platform's keyring.

Secrets are never written to the config file, passed on a command line, or
recorded in the audit journal. They are fetched at the moment of use and kept
only in memory.

Secrets live in the environment, and are shared between controllers by the
audit ledger. One credential is handed to a machine directly -- the ledger
password -- and every other secret is read from the shared store behind it, so
adding a controller is one export rather than a repeat of the whole setup.

Lookup order, first hit wins:

1. the configured backend (`env` by default)
2. the matching `PROXMOX_AGENT_LAB_*` environment variable, so any secret can
   be overridden locally without touching the shared store
3. the shared store in the audit ledger
An OS keystore (`keychain`, `secret-tool`) can still be named explicitly as
the backend, and a controller upgrading from one has its secrets copied into
the shared store when the ledger is provisioned.

Backends that may be set explicitly: `env`, `file` (a 0600 TOML file),
`keychain` (macOS) and `secret-tool` (libsecret). The last two are legacy.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

from .config import APP_NAME, Config

# The names this tool asks for. Documented so `secrets list` can show them.
KNOWN_SECRETS = {
    "mariadb-password": "Audit ledger password -- the one secret a new controller needs",
    "proxmox-token": "Proxmox API token secret (the UUID shown once on creation)",
    "home-assistant-token": "Home Assistant long-lived token (power mode only)",
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
    """The environment, everywhere.

    Secrets travel as environment variables so a controller is reproducible
    and portable -- the same config works on a laptop, in CI and on a box with
    no desktop keyring at all. An OS keystore is still read as a fallback (see
    `get`) so a controller that predates this keeps working untouched.
    """
    return "env"


def legacy_keystore() -> str:
    """The OS keystore this machine would have used before secrets moved to
    the environment. Read-only fallback; nothing new is written here.

    ``os.uname`` does not exist on Windows, so the platform check goes through
    ``platform.system()`` there rather than raising AttributeError on import
    of a controller that never had a keystore to begin with.
    """
    if _is_darwin() and shutil.which("security"):
        return "keychain"
    if shutil.which("secret-tool"):
        return "secret-tool"
    if _is_windows() and shutil.which("cmdkey"):
        return "wincred"
    return ""


def _is_windows() -> bool:
    return os.name == "nt"


def _is_darwin() -> bool:
    try:
        return os.uname().sysname == "Darwin"  # type: ignore[attr-defined]
    except AttributeError:  # pragma: no cover - Windows only
        import platform

        return platform.system() == "Darwin"


def read_legacy(backend: str, name: str) -> str | None:
    """Read one secret from the pre-environment OS keystore.

    Explicit, never an implicit fallback inside `get`. An automatic fallback
    reached past the configured backend into whatever the desktop keyring
    happened to hold -- including, in tests, the developer's real secrets --
    which made a "missing" secret unpredictable. Seeding the shared store
    (`journal host-setup`) reads through here on purpose instead.
    """
    if backend == "keychain":
        argv = ["security", "find-generic-password", "-a", name,
                "-s", APP_NAME, "-w"]
    elif backend == "wincred":  # pragma: no cover - Windows only
        # cmdkey cannot print a secret back, by design. A Windows controller
        # that stored secrets there has to move them into the environment.
        return None
    elif backend == "secret-tool":
        argv = ["secret-tool", "lookup", "service", APP_NAME, "account", name]
    else:
        return None
    result = subprocess.run(argv, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


# The one credential a controller must be given directly. Everything else is
# handed out by the shared store once this one works.
BOOTSTRAP_SECRET = "mariadb-password"


def _read_shared(config: Config, name: str) -> str | None:
    """Look one secret up in the shared store on the lab host.

    Best effort by design: the lab host is powered off between leases, and a
    missing shared store must fall through to the local ones rather than
    turning every command into an error.
    """
    # No bootstrap credential means nothing to authenticate with, so there is
    # nothing to ask. Checked first because it is a dict lookup, where trying
    # the connection anyway would block until the socket timed out on every
    # secret read -- including in tests and on a laptop away from the lab.
    if not os.environ.get(_env_name(BOOTSTRAP_SECRET)):
        return None
    try:
        from . import journal as _journal

        settings = _journal.settings_from_config(config)
        if settings is None:
            return None
        from . import mariadb as _mariadb

        return _mariadb.get_secret(settings, name)
    except Exception:
        return None


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
    # Windows has no POSIX mode bits to check; NTFS ACLs are the equivalent
    # and are not expressible here, so the check is skipped rather than
    # refusing to read a perfectly ordinary file.
    if not _is_windows():
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
    # The shared store on the lab host. This is what makes a second machine a
    # one-liner: give it the bootstrap password and it inherits every other
    # secret the first controller set up. Skipped for the bootstrap password
    # itself, which would otherwise need itself to be read.
    if name != BOOTSTRAP_SECRET:
        shared = _read_shared(config, name)
        if shared:
            return shared
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
        # `-w` with no value prompts twice ("password data" + "retype"),
        # reading both from stdin; passing the secret as an argument would
        # leave it visible in `ps` output for the life of the process.
        # `-U` must come before `-w`, or it is swallowed as the password.
        result = subprocess.run(
            ["security", "add-generic-password", "-a", name, "-s", APP_NAME,
             "-U", "-w"],
            input=f"{value}\n{value}\n",
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
        # json.dumps yields a valid TOML basic string (escapes " and \), so
        # a secret containing quotes can no longer corrupt the whole store
        # (audit 2026-08-24).
        body = "".join(
            f'{key} = {json.dumps(val)}\n'
            for key, val in sorted(existing.items())
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
