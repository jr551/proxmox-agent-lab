"""Guest file transfer: push/pull over chunked base64 guest exec.

The only transport is ``proxmox.push_bytes``/``proxmox.pull_bytes`` (chunked
base64 fed through guest exec, rework plan §D). The S3 scratch-bucket path
this module used to mediate -- uploads, presigned URLs, chunk tables -- is
cut entirely.

Ownership first: every command starts with ``guest.require_owned`` against the
lease registry (``kind=None``: whichever kind the lease registers) before any
host call. The guest kind is taken from the registry row, the same convention
``guest.py`` uses -- no ``qm``/``pct`` probing and never a seam call ahead of
the gate.

Digest policy. ``--sha256`` names the payload digest the caller expects. Push
refuses a mismatching local file before the guest is touched and reports the
guest-verified digest ``push_bytes`` recomputes in the guest; pull removes the
written file and fails when the pulled bytes do not match ``--sha256``.
``pull_bytes`` independently compares the reassembled bytes against the
guest's own sha256 before returning them, so a digest mismatch never reaches
disk.

``--timeout`` (default 300 s, bounded) is the transfer budget: ``push_bytes``
and ``pull_bytes`` own their guest execs and take no timeout of their own, so
the budget is enforced around the seam call here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any, Callable

from . import guest as guest_module
from . import proxmox as proxmox_module
from .errors import LabError

DEFAULT_TIMEOUT = 300
MAX_TIMEOUT = 86400


def _make_proxmox(config: Any) -> proxmox_module.Proxmox:
    """The proxmox seam for this configuration (tests substitute a double)."""
    return proxmox_module.from_config(config)


def _timeout_seconds(value: str) -> int:
    """argparse type: a bounded transfer budget in seconds."""
    try:
        seconds = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"not a whole number of seconds: {value!r}"
        ) from exc
    if not 1 <= seconds <= MAX_TIMEOUT:
        raise argparse.ArgumentTypeError(
            f"timeout must be between 1 and {MAX_TIMEOUT} seconds"
        )
    return seconds


def _bounded(call: Callable[[], Any], timeout: float, what: str) -> Any:
    """Run one seam transfer under the ``--timeout`` budget.

    The seam call is blocking and owns its own guest execs, so the budget is
    enforced here: the call runs on a daemon worker and the command fails
    (with nothing partial kept) when it overruns.
    """
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            outcome["value"] = call()
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            outcome["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise LabError(f"{what} did not finish within {timeout:g}s")
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")


def _require_guest(lab: Any, args: Any) -> str:
    """Gate on lease ownership first; return the registered guest kind.

    ``require_owned`` reads only the registry (no seam call), so the gate
    always precedes the first host command.
    """
    row = guest_module.require_owned(lab, args.lease, None, args.vmid)
    return str(row["kind"])


def _write_private(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` with mode 0600, never leaving a partial file."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    except OSError as exc:
        raise LabError(f"cannot write {path}: {exc}") from exc
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(path, 0o600)  # an existing file keeps its old mode otherwise
    except OSError as exc:
        path.unlink(missing_ok=True)  # a file we opened is ours to remove
        raise LabError(f"cannot write {path}: {exc}") from exc


def cmd_push(lab: Any, args: Any) -> None:
    """Copy a local file into a guest via chunked base64 guest exec."""
    kind = _require_guest(lab, args)
    source = Path(args.file).expanduser().resolve()
    if not source.is_file():
        raise LabError(f"not a regular file: {source}")
    data = source.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    expected = getattr(args, "sha256", None)
    if expected and digest != expected:
        # Refused before the guest is touched: nothing happened to audit.
        raise LabError(f"sha256 mismatch: {source} is {digest}, expected {expected}")
    dest = str(args.dest)
    timeout = getattr(args, "timeout", None) or DEFAULT_TIMEOUT
    prox = _make_proxmox(lab.CONFIG)
    guest_sha = _bounded(
        lambda: prox.push_bytes(kind, args.vmid, dest, data),
        timeout,
        f"push to {dest}",
    )
    lab.audit(
        "guest-push",
        lease=args.lease,
        vmid=args.vmid,
        remote_path=dest,
        bytes=len(data),
        sha256=guest_sha,
    )
    print(json.dumps({
        "lease_id": args.lease,
        "vmid": args.vmid,
        "local_path": str(source),
        "remote_path": dest,
        "bytes": len(data),
        "sha256": guest_sha,
    }, indent=2, sort_keys=True))


def cmd_pull(lab: Any, args: Any) -> None:
    """Copy a file out of a guest via chunked base64 guest exec."""
    kind = _require_guest(lab, args)
    remote = str(args.remote)
    timeout = getattr(args, "timeout", None) or DEFAULT_TIMEOUT
    prox = _make_proxmox(lab.CONFIG)
    # pull_bytes verifies the reassembled bytes against the guest's sha256
    # before returning them; a mismatch raises and nothing is written.
    data = _bounded(
        lambda: prox.pull_bytes(kind, args.vmid, remote),
        timeout,
        f"pull from {remote}",
    )
    digest = hashlib.sha256(data).hexdigest()
    out = Path(args.out).expanduser()
    _write_private(out, data)
    expected = getattr(args, "sha256", None)
    if expected and digest != expected:
        out.unlink(missing_ok=True)  # never leave a bad pull behind
        raise LabError(f"sha256 mismatch: {remote} is {digest}, expected {expected}")
    lab.audit(
        "guest-pull",
        lease=args.lease,
        vmid=args.vmid,
        remote_path=remote,
        bytes=len(data),
        sha256=digest,
    )
    print(json.dumps({
        "lease_id": args.lease,
        "vmid": args.vmid,
        "local_path": str(out),
        "remote_path": remote,
        "bytes": len(data),
        "sha256": digest,
    }, indent=2, sort_keys=True))


def register(sub: Any, lab: Any) -> None:
    """The ``push`` and ``pull`` parsers (transfer owns both)."""
    from .cli import _bind

    push = sub.add_parser("push", help="copy a local file into a guest")
    push.add_argument("--lease", required=True)
    push.add_argument("--vmid", type=int, required=True)
    push.add_argument("--file", required=True)
    push.add_argument("--dest", required=True)
    push.add_argument(
        "--sha256",
        help="expected SHA-256 of the file; verified before it is sent",
    )
    push.add_argument(
        "--timeout",
        type=_timeout_seconds,
        default=DEFAULT_TIMEOUT,
        help=f"transfer budget in seconds (default {DEFAULT_TIMEOUT})",
    )
    push.set_defaults(func=_bind(lab, cmd_push))

    pull = sub.add_parser("pull", help="copy a file out of a guest")
    pull.add_argument("--lease", required=True)
    pull.add_argument("--vmid", type=int, required=True)
    pull.add_argument("--remote", required=True)
    pull.add_argument("--out", required=True)
    pull.add_argument(
        "--sha256",
        help="expected SHA-256 of the pulled file; a mismatch removes it and fails",
    )
    pull.add_argument(
        "--timeout",
        type=_timeout_seconds,
        default=DEFAULT_TIMEOUT,
        help=f"transfer budget in seconds (default {DEFAULT_TIMEOUT})",
    )
    pull.set_defaults(func=_bind(lab, cmd_pull))
