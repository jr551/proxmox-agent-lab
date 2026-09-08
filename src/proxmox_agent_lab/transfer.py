"""Guest file transfer: S3-mediated push/pull and the ``s3`` subcommands.

Files move through the configured S3 scratch bucket using presigned URLs; no
credential reaches the guest, argv or the audit ledger. The chunked path
splits large payloads so retries resume, and verifies a SHA-256 on both ends.
"""
from __future__ import annotations

from .guest_agent import agent_exec
from . import leases as leases_module
from . import s3
from pathlib import Path
from typing import Any
import json
import secrets
import shlex


SINGLE_OBJECT_MAX_MB = 32


CHUNK_DEFAULT_MB = 64


MAX_CHUNK_PARTS = 256


def _ps_quote(value: str) -> str:
    """Single-quote a value for a PowerShell string, doubling embedded quotes."""
    return "'" + value.replace("'", "''") + "'"


def _fetch_command(url: str, dest: str, windows: bool) -> list[str]:
    if windows:
        script = (
            "$ProgressPreference='SilentlyContinue'; "
            f"Invoke-WebRequest -UseBasicParsing -Uri {_ps_quote(url)} "
            f"-OutFile {_ps_quote(dest)}"
        )
        return ["powershell.exe", "-NoProfile", "-Command", script]
    return [
        "/bin/sh", "-c",
        f"curl -fsSL -A proxmox-agent-lab -o {shlex.quote(dest)} {shlex.quote(url)}",
    ]


def _upload_command(url: str, source: str, windows: bool) -> list[str]:
    if windows:
        script = (
            "$ProgressPreference='SilentlyContinue'; "
            f"Invoke-WebRequest -UseBasicParsing -Method Put -Uri {_ps_quote(url)} "
            f"-InFile {_ps_quote(source)}"
        )
        return ["powershell.exe", "-NoProfile", "-Command", script]
    return [
        "/bin/sh", "-c",
        f"curl -fsS -A proxmox-agent-lab -X PUT --data-binary "
        f"@{shlex.quote(source)} {shlex.quote(url)}",
    ]


def _fetch_parts_command(urls: list[str], dest: str) -> list[str]:
    """Download and reassemble chunk parts in the guest, printing the hash."""
    steps = " && ".join(
        f"curl -fsSL -A proxmox-agent-lab -o "
        f"{shlex.quote(f'/tmp/pp-{i:04d}')} {shlex.quote(url)}"
        for i, url in enumerate(urls)
    )
    script = (
        f"rm -f {shlex.quote(dest)} /tmp/pp-*; {steps} "
        f"&& cat /tmp/pp-* > {shlex.quote(dest)} && rm -f /tmp/pp-* "
        f"&& sha256sum {shlex.quote(dest)} | cut -d' ' -f1"
    )
    return ["/bin/sh", "-c", script]


def _upload_parts_command(urls: list[str], source: str, chunk: int) -> list[str]:
    """Split a guest file into chunks, upload each, then report its hash."""
    steps = " && ".join(
        f"curl -fsS -A proxmox-agent-lab -X PUT --data-binary "
        f"@{shlex.quote(f'/tmp/pp-{i:04d}')} {shlex.quote(url)}"
        for i, url in enumerate(urls)
    )
    script = (
        f"rm -f /tmp/pp-*; split -b {int(chunk)} -d -a 4 "
        f"{shlex.quote(source)} /tmp/pp- && {steps} "
        f"&& rm -f /tmp/pp-* && sha256sum {shlex.quote(source)} | cut -d' ' -f1"
    )
    return ["/bin/sh", "-c", script]


def _chunk_size_mb(args: Any) -> int:
    return max(1, getattr(args, "chunk_size", None) or CHUNK_DEFAULT_MB)


def _push_chunked(lab: Any, api: Any, args: Any, source: Path,
                  payload: bytes, name: str) -> dict[str, Any]:
    chunk = _chunk_size_mb(args) * 1024 * 1024
    parts = [payload[i:i + chunk] for i in range(0, len(payload), chunk)]
    if len(parts) > MAX_CHUNK_PARTS:
        raise lab.LabError(f"{name} needs {len(parts)} parts (max {MAX_CHUNK_PARTS}); "
            "raise --chunk-size",
        )
    base = args.key or f"push/{secrets.token_hex(6)}/{name}"
    keys = [f"{base}/part-{i:04d}" for i in range(len(parts))]
    for key, part in zip(keys, parts):
        s3.put_bytes(key, part)
    urls = [s3.presign(key, expires=args.url_expiry) for key in keys]
    dest = args.dest or f"/tmp/{name}"
    run = agent_exec(
        lab, api, args.vmid, _fetch_parts_command(urls, dest),
        timeout=args.timeout,
    )
    if run["exitcode"] not in (0, None):
        raise lab.LabError(f"guest fetch failed: {run['stderr'][:400]}")
    guest_sha = run.get("stdout", "").strip()
    if args.sha256 and guest_sha != args.sha256:
        raise lab.LabError(f"sha256 mismatch on guest: {guest_sha} != {args.sha256}"
        )
    return {
        "vmid": args.vmid, "s3_key": base, "bytes": len(payload),
        "parts": len(parts), "dest": dest, "chunked": True,
        "guest_sha256": guest_sha or None,
    }


def _pull_chunked(lab: Any, api: Any, args: Any, name: str) -> dict[str, Any]:
    import hashlib
    import math

    chunk = _chunk_size_mb(args) * 1024 * 1024
    base = args.key or f"pull/{args.vmid}/{name}"
    out = Path(args.out).expanduser() if args.out else Path(name)
    if args.sha256 and out.is_file():
        if hashlib.sha256(out.read_bytes()).hexdigest() == args.sha256:
            return {
                "vmid": args.vmid, "path": str(out),
                "bytes": out.stat().st_size, "sha256": args.sha256,
                "s3_key": base, "already_verified": True, "chunked": True,
            }
    size_run = agent_exec(
        lab, api, args.vmid,
        ["/bin/sh", "-c", f"stat -c %s {shlex.quote(args.remote)}"],
        timeout=args.timeout,
    )
    try:
        size = int(size_run.get("stdout", "").strip())
    except ValueError:
        raise lab.LabError(f"cannot read size of {args.remote}: "
            f"{size_run.get('stderr', '')[:200]}",
        ) from None
    n_parts = max(1, math.ceil(size / chunk))
    if n_parts > MAX_CHUNK_PARTS:
        raise lab.LabError(f"{name} needs {n_parts} parts (max {MAX_CHUNK_PARTS}); "
            "raise --chunk-size",
        )
    keys = [f"{base}/part-{i:04d}" for i in range(n_parts)]
    # Drop stale parts from an earlier interrupted attempt with the same key.
    for obj in s3.list_objects(base):
        key = str(obj.get("key", ""))
        if key.startswith(base + "/"):
            s3.delete_object(key)
    urls = [s3.presign(key, method="PUT", expires=args.url_expiry)
            for key in keys]
    run = agent_exec(
        lab, api, args.vmid,
        _upload_parts_command(urls, args.remote, chunk),
        timeout=args.timeout,
    )
    if run["exitcode"] not in (0, None):
        raise lab.LabError(f"guest upload failed: {run['stderr'][:400]}")
    guest_sha = run.get("stdout", "").strip()
    payload = b"".join(s3.get_bytes(key) for key in keys)
    sha = hashlib.sha256(payload).hexdigest()
    if guest_sha and sha != guest_sha:
        raise lab.LabError(f"sha256 mismatch: assembled {sha} != guest {guest_sha}"
        )
    if args.sha256 and sha != args.sha256:
        raise lab.LabError(f"sha256 mismatch: {sha} != expected {args.sha256}"
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(payload)
    if not args.keep:
        for key in keys:
            s3.delete_object(key)
    return {
        "vmid": args.vmid, "path": str(out), "bytes": len(payload),
        "sha256": sha, "parts": n_parts, "s3_key": base, "chunked": True,
    }


def cmd_push(lab: Any, args: Any) -> None:
    """Copy a local file into a guest via the S3 scratch bucket."""
    api = lab.ProxmoxAPI()
    leases_module.require_owned_qemu(lab, args.lease, args.vmid)
    source = Path(args.file).expanduser().resolve()
    if not source.is_file():
        raise lab.LabError(f"not a regular file: {source}")
    payload = source.read_bytes()
    chunked = (
        not args.windows and not args.url_only
        and len(payload) > SINGLE_OBJECT_MAX_MB * 1024 * 1024
    )
    if chunked:
        result = _push_chunked(lab, api, args, source, payload, source.name)
        lab.audit("guest-push", lease=args.lease, vmid=args.vmid,
                  s3_key=result["s3_key"], bytes=len(payload),
                  parts=result["parts"], chunked=True)
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    key = args.key or f"push/{secrets.token_hex(6)}/{source.name}"
    s3.put_bytes(key, payload)
    url = s3.presign(key, expires=args.url_expiry)
    dest = args.dest or (
        f"C:\\Windows\\Temp\\{source.name}" if args.windows else f"/tmp/{source.name}"
    )
    result: dict[str, Any] = {
        "vmid": args.vmid,
        "s3_key": key,
        "bytes": len(payload),
        "dest": dest,
    }
    if args.url_only:
        result["fetch_url"] = url
        result["hint"] = "run the fetch inside the guest yourself"
    else:
        run = agent_exec(
            lab, api, args.vmid, _fetch_command(url, dest, args.windows),
            timeout=args.timeout,
        )
        result["guest"] = run
        if run["exitcode"] not in (0, None):
            raise lab.LabError(f"guest fetch failed: {run['stderr'][:400]}")
    lab.audit("guest-push", lease=args.lease, vmid=args.vmid, s3_key=key,
              bytes=len(payload), dest=dest)
    print(json.dumps(result, indent=2, sort_keys=True))


def cmd_pull(lab: Any, args: Any) -> None:
    """Copy a file out of a guest via the S3 scratch bucket."""
    import hashlib

    api = lab.ProxmoxAPI()
    leases_module.require_owned_qemu(lab, args.lease, args.vmid)
    name = Path(args.remote).name
    out = Path(args.out).expanduser() if args.out else Path(name)
    # Resume: when the local file already matches the expected hash there is
    # nothing to do, so make no guest or S3 traffic at all.
    if args.sha256 and out.is_file():
        if hashlib.sha256(out.read_bytes()).hexdigest() == args.sha256:
            lab.audit("guest-pull", lease=args.lease, vmid=args.vmid,
                      bytes=out.stat().st_size, sha256=args.sha256,
                      already_verified=True)
            print(json.dumps({
                "vmid": args.vmid, "path": str(out),
                "bytes": out.stat().st_size, "sha256": args.sha256,
                "already_verified": True,
            }, indent=2, sort_keys=True))
            return
    if not args.windows:
        probe = agent_exec(
            lab, api, args.vmid,
            ["/bin/sh", "-c", f"stat -c %s {shlex.quote(args.remote)}"],
            timeout=args.timeout,
        )
        try:
            remote_size = int(probe.get("stdout", "").strip())
        except ValueError:
            remote_size = 0
        if remote_size > SINGLE_OBJECT_MAX_MB * 1024 * 1024:
            result = _pull_chunked(lab, api, args, name)
            lab.audit("guest-pull", lease=args.lease, vmid=args.vmid,
                      s3_key=result.get("s3_key", ""), bytes=result["bytes"],
                      parts=result.get("parts"), chunked=True)
            print(json.dumps(result, indent=2, sort_keys=True))
            return
    key = args.key or f"pull/{secrets.token_hex(6)}/{Path(args.remote).name}"
    url = s3.presign(key, method="PUT", expires=args.url_expiry)
    run = agent_exec(
        lab, api, args.vmid, _upload_command(url, args.remote, args.windows),
        timeout=args.timeout,
    )
    if run["exitcode"] not in (0, None):
        raise lab.LabError(f"guest upload failed: {run['stderr'][:400]}")
    payload = s3.get_bytes(key)
    target = Path(args.out).expanduser() if args.out else Path(Path(args.remote).name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    if not args.keep:
        s3.delete_object(key)
    lab.audit("guest-pull", lease=args.lease, vmid=args.vmid, s3_key=key,
              bytes=len(payload))
    print(json.dumps(
        {"vmid": args.vmid, "path": str(target), "bytes": len(payload)}, indent=2
    ))


def cmd_s3(lab: Any, args: Any) -> None:
    if args.s3_command == "health":
        print(json.dumps(s3.health(), indent=2, sort_keys=True))
    elif args.s3_command == "list":
        print(json.dumps(s3.list_objects(args.prefix), indent=2, sort_keys=True))
    elif args.s3_command == "put":
        source = Path(args.file).expanduser().resolve()
        key = args.key or f"upload/{secrets.token_hex(6)}/{source.name}"
        s3.put_bytes(key, source.read_bytes())
        print(json.dumps({"key": key, "bytes": source.stat().st_size}, indent=2))
    elif args.s3_command == "get":
        payload = s3.get_bytes(args.key)
        target = Path(args.out).expanduser() if args.out else Path(Path(args.key).name)
        target.write_bytes(payload)
        print(json.dumps({"path": str(target), "bytes": len(payload)}, indent=2))
    elif args.s3_command == "presign":
        print(json.dumps(
            {"url": s3.presign(args.key, method=args.method,
                                   expires=args.expires)},
            indent=2,
        ))
    elif args.s3_command == "delete":
        s3.delete_object(args.key)
        print(json.dumps({"deleted": args.key}))


def _register_transfer(sub: Any, lab: Any) -> None:
    from .cli import _bind

    push = sub.add_parser("push", help="copy a local file into a guest")
    push.add_argument("--lease", required=True)
    push.add_argument("--vmid", type=int, required=True)
    push.add_argument("--file", required=True)
    push.add_argument("--dest")
    push.add_argument("--key", help="explicit S3 object key")
    push.add_argument("--windows", action="store_true")
    push.add_argument("--url-only", action="store_true",
                      help="print a presigned URL instead of using the guest agent")
    push.add_argument("--url-expiry", type=int, default=3600)
    push.add_argument("--timeout", type=int, default=600)
    push.add_argument("--chunk-size", type=int, metavar="MB", default=CHUNK_DEFAULT_MB,
                      help="part size for large-file transfers (default 64)")
    push.add_argument("--sha256",
                      help="expected SHA-256 of the file; verified on the guest")
    push.set_defaults(func=_bind(lab, cmd_push))

    pull = sub.add_parser("pull", help="copy a file out of a guest")
    pull.add_argument("--lease", required=True)
    pull.add_argument("--vmid", type=int, required=True)
    pull.add_argument("--remote", required=True)
    pull.add_argument("--out")
    pull.add_argument("--key")
    pull.add_argument("--keep", action="store_true",
                      help="keep the scratch object after download")
    pull.add_argument("--windows", action="store_true")
    pull.add_argument("--url-expiry", type=int, default=3600)
    pull.add_argument("--timeout", type=int, default=600)
    pull.add_argument("--chunk-size", type=int, metavar="MB", default=CHUNK_DEFAULT_MB,
                      help="part size for large-file transfers (default 64)")
    pull.add_argument("--sha256",
                      help="expected SHA-256; skips the transfer when the "
                           "local file already matches")
    pull.set_defaults(func=_bind(lab, cmd_pull))


def _register_s3(sub: Any, lab: Any) -> None:
    from .cli import _bind

    store = sub.add_parser("s3", help="scratch bucket operations")
    store_sub = store.add_subparsers(dest="s3_command", required=True)
    store_sub.add_parser("health").set_defaults(func=_bind(lab, cmd_s3))
    listing = store_sub.add_parser("list")
    listing.add_argument("--prefix", default="")
    listing.set_defaults(func=_bind(lab, cmd_s3))
    putter = store_sub.add_parser("put")
    putter.add_argument("--file", required=True)
    putter.add_argument("--key")
    putter.set_defaults(func=_bind(lab, cmd_s3))
    getter = store_sub.add_parser("get")
    getter.add_argument("--key", required=True)
    getter.add_argument("--out")
    getter.set_defaults(func=_bind(lab, cmd_s3))
    signer = store_sub.add_parser("presign")
    signer.add_argument("--key", required=True)
    signer.add_argument("--method", default="GET", choices=("GET", "PUT"))
    signer.add_argument("--expires", type=int, default=3600)
    signer.set_defaults(func=_bind(lab, cmd_s3))
    remover = store_sub.add_parser("delete")
    remover.add_argument("--key", required=True)
    remover.set_defaults(func=_bind(lab, cmd_s3))
