"""Opt-in SSH transport for reaching the Proxmox host itself.

Everything else in this package talks to Proxmox over the HTTPS API token.
The subsystems that need host-side tooling -- memflow introspection, USB and
network capture, host-side disk reads, the monitor-screendump fallback --
share this one channel instead of each opening a host trust boundary of their
own. It stays **off** until the operator opts in.

The channel is configured by the ``[memflow]`` config section: it grew out of
memory introspection, and the key names stay so existing configuration keeps
working. ``enabled`` and ``ssh_host`` must both be set before any function
here will touch the network.

This module is a transport boundary: it must not import feature modules
(memflow, netcap, usb, disk, console), which keeps the dependency direction
one way -- features depend on the transport, never the reverse.
"""

from __future__ import annotations

import base64
import os
import shlex
import subprocess
from typing import Any

from . import config as _config

_CONFIG = _config.get()
_CHANNEL = _CONFIG.memflow  # the [memflow] section configures this channel

ENABLED = bool(_CHANNEL.get("enabled"))
SSH_HOST = _CHANNEL.get("ssh_host", "")
SSH_USER = _CHANNEL.get("ssh_user", "root") or "root"
SSH_PORT = int(_CHANNEL.get("ssh_port", 22) or 22)
SSH_KEY = _CHANNEL.get("ssh_key", "")
SSH_OPTIONS = _CHANNEL.get("ssh_options", "")
CONNECT_TIMEOUT = int(_CHANNEL.get("connect_timeout", 10) or 10)

# ssh's own failure vs a POSIX "command not found": one means the connection
# never carried a command, the other means the remote program is missing.
_SSH_FAILURE = 255

NOT_ENABLED = (
    "the opt-in host SSH channel is off. It reaches the Proxmox host over "
    "SSH -- a separate trust boundary from the API token -- so it stays "
    "disabled until you opt in. Set [memflow] enabled = true and ssh_host, "
    "then re-run. See docs/memflow.md for the channel's setup."
)


def _ssh_argv(remote: str) -> list[str]:
    argv = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={CONNECT_TIMEOUT}",
        "-o", "StrictHostKeyChecking=accept-new",
    ]
    if SSH_PORT != 22:
        argv += ["-p", str(SSH_PORT)]
    if SSH_KEY:
        argv += ["-i", os.path.expanduser(SSH_KEY)]
    if SSH_OPTIONS:
        argv += shlex.split(SSH_OPTIONS)
    argv.append(f"{SSH_USER}@{SSH_HOST}")
    argv.append("--")
    argv.append(remote)
    return argv


def run(lab: Any, remote_argv: list[str], *, timeout: int = 60,
        stdin: str | None = None) -> subprocess.CompletedProcess:
    """Run one command on the host over SSH and return the completed process.

    The argv is joined with shlex, so no caller can inject through an
    argument. This is the ungated primitive: callers are responsible for the
    authorization gate that applies to their operation -- read-only
    inspection goes through :func:`host_run`, anything that changes the host
    keeps its own documented gate before calling this.
    """
    remote = shlex.join(remote_argv)
    try:
        proc = subprocess.run(
            _ssh_argv(remote), capture_output=True, text=True,
            timeout=timeout, input=stdin, check=False,
        )
    except FileNotFoundError:
        raise lab.LabError(
            "ssh was not found on this machine; it is required to reach the "
            "Proxmox host"
        ) from None
    except subprocess.TimeoutExpired:
        raise lab.LabError(
            f"host SSH: no response from {SSH_HOST} within {timeout}s"
        ) from None
    if proc.returncode == _SSH_FAILURE:
        raise lab.LabError(
            f"host SSH: cannot SSH to {SSH_USER}@{SSH_HOST}: "
            f"{(proc.stderr or '').strip()[:300] or 'connection failed'}. "
            "Check [memflow] ssh_host/ssh_user/ssh_key and that the key is "
            "authorised on the host."
        )
    return proc


def host_ssh_enabled() -> bool:
    """True when the opt-in host SSH channel is configured."""
    return bool(ENABLED and SSH_HOST)


def require_host_ssh(lab: Any, message: str | None = None) -> None:
    """Raise unless the host SSH channel is enabled and configured.

    ``message`` lets a subsystem that shares this channel (netcap, usb, disk)
    raise its own guidance pointing at its own doc, rather than the generic
    opt-in text.
    """
    if not ENABLED or not SSH_HOST:
        raise lab.LabError(NOT_ENABLED if message is None else message)


def host_run(lab: Any, argv: list[str], *, timeout: int = 60
             ) -> subprocess.CompletedProcess:
    """Run one command on the host and return the completed process.

    The argv is joined with shlex, so no caller can inject through an
    argument. Reserved for read-only host inspection -- anything that changes
    the host keeps its own authorization gate.
    """
    require_host_ssh(lab)
    return run(lab, argv, timeout=timeout)


def host_mkdir(lab: Any, directory: str, *, timeout: int = 30) -> None:
    """Create one private directory on the host."""
    require_host_ssh(lab)
    proc = run(lab, ["mkdir", "-p", "-m", "700", "--", directory],
               timeout=timeout)
    if proc.returncode:
        raise lab.LabError(
            f"could not create {directory} on {SSH_HOST}: "
            f"{(proc.stderr or '').strip()[:200] or proc.returncode}"
        )


def host_read_bytes(lab: Any, path: str, *, timeout: int = 120) -> bytes:
    """Read one file from the host verbatim, without decoding it as text."""
    require_host_ssh(lab)
    remote = shlex.join(["cat", "--", path])
    try:
        proc = subprocess.run(
            _ssh_argv(remote), capture_output=True, timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        raise lab.LabError(
            "ssh was not found on this machine; it is required to reach the "
            "Proxmox host"
        ) from None
    except subprocess.TimeoutExpired:
        raise lab.LabError(
            f"no response from {SSH_HOST} within {timeout}s while reading a "
            "host file"
        ) from None
    if proc.returncode:
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip()[:200]
        raise lab.LabError(
            f"could not read {path} from {SSH_HOST}: "
            f"{detail or proc.returncode}"
        )
    return proc.stdout


def host_remove_empty_dir(lab: Any, directory: str, *, timeout: int = 30) -> bool:
    """Remove one directory on the host, only if it is empty.

    `rmdir` refuses a non-empty directory, so a concurrent capture in the same
    lease cannot lose its file to this cleanup.
    """
    if not host_ssh_enabled():
        return False
    try:
        proc = run(lab, ["rmdir", "--", directory], timeout=timeout)
    except (OSError, lab.LabError):
        return False
    return proc.returncode == 0


def host_remove_file(lab: Any, path: str, *, timeout: int = 30) -> bool:
    """Delete one file on the host. Best effort: reports, never raises."""
    if not host_ssh_enabled():
        return False
    try:
        proc = run(lab, ["rm", "-f", "--", path], timeout=timeout)
    except (OSError, lab.LabError):
        # Cleanup runs in a finally path: it reports failure to the caller,
        # which records it, rather than masking the original error.
        return False
    return proc.returncode == 0


def run_remote_capture(lab: Any, script: str, *, timeout: int = 60
                       ) -> tuple[int, bytes]:
    """Run a host-side capture script and return (packet_count, pcap_bytes).

    The script must print ``PKTS=N`` on its own line and base64-encode the
    pcap on the remaining lines, as netcap/usb do. Failures raise LabError
    with the host's stderr (if any).
    """
    proc = host_run(lab, ["bash", "-c", script], timeout=timeout)
    if proc.returncode not in (0, None):
        raise lab.LabError(
            f"capture failed on the host: {(proc.stderr or '').strip()[:300]}"
        )
    return decode_capture_output(proc.stdout or "")


def decode_capture_output(stdout: str) -> tuple[int, bytes]:
    """Parse PKTS= and base64 pcap from a capture script's stdout."""
    pkts = 0
    b64: list[str] = []
    for line in stdout.splitlines():
        if line.startswith("PKTS="):
            try:
                pkts = int(line.split("=", 1)[1] or 0)
            except ValueError:
                pkts = 0
        else:
            b64.append(line)
    data = base64.b64decode("".join(b64) or "")
    return pkts, data
