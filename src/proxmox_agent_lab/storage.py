"""Physical disk and storage management.

Formatting a disk destroys everything on it and cannot be undone, so every
guard here is deliberate:

* the caller must pass `--host-change-authorized`, like any host-level change;
* the target device must be named explicitly -- nothing is auto-selected;
* a disk Proxmox reports as in use, or as the OS disk, is refused outright;
* `--expect-serial` and `--expect-size-gb` let the caller pin the exact
  physical device, so a re-enumerated `/dev/sdX` cannot silently redirect the
  wipe at a different disk.

Requires `Sys.Modify` and `Sys.Audit` on the node, which the least-privilege
lab token does not hold by default.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Everything a directory storage can hold. A slow bulk disk is a good home for
# install media and cold images; keep fast guest disks on local-lvm.
from . import config as _config

_DEFAULT_BULK = _config.get().storage.bulk_storage
DEFAULT_CONTENT = "images,iso,vztmpl,import,backup,snippets"
FILESYSTEMS = ("ext4", "xfs")
CHECKSUM_RE = re.compile(r"^(sha256|sha512|sha1|md5)\s*[:=]\s*([0-9a-f]+)$", re.I)


def _normalise_checksum(value: str, algorithm: str | None) -> tuple[str, str]:
    """Accept a bare digest or the common ``algorithm:digest`` spelling."""
    match = CHECKSUM_RE.fullmatch(value.strip())
    if match:
        prefixed_algorithm = match.group(1).lower()
        if algorithm and algorithm != prefixed_algorithm:
            raise ValueError(
                f"checksum prefix says {prefixed_algorithm}, but "
                f"--checksum-algorithm says {algorithm}"
            )
        return match.group(2).lower(), prefixed_algorithm
    return value.strip().lower(), algorithm or "sha512"


def _disks(lab: Any, api: Any) -> list[dict[str, Any]]:
    try:
        return api.call("GET", f"/nodes/{lab.NODE}/disks/list") or []
    except lab.LabError as exc:
        raise lab.LabError(
            f"cannot read the disk list: {exc}. This needs Sys.Audit on "
            f"/nodes/{lab.NODE}, which the lab token does not hold by default."
        )


def _describe(disk: dict[str, Any]) -> dict[str, Any]:
    return {
        "device": disk.get("devpath"),
        "size_gb": round(int(disk.get("size", 0)) / 1_000_000_000, 1),
        "model": disk.get("model"),
        "serial": disk.get("serial"),
        "type": disk.get("type"),
        "used": disk.get("used") or None,
        "os_disk": bool(disk.get("osdisk")),
        "health": disk.get("health"),
        "wearout": disk.get("wearout"),
    }


def cmd_list_disks(lab: Any, args: Any) -> None:
    api = lab.ProxmoxAPI()
    disks = [_describe(disk) for disk in _disks(lab, api)]
    free = [
        disk for disk in disks if not disk["used"] and not disk["os_disk"]
    ]
    print(json.dumps(
        {
            "disks": disks,
            "unused_candidates": [disk["device"] for disk in free],
            "note": (
                "'used' means Proxmox already sees a filesystem, LVM member or "
                "partition table. Formatting such a disk is refused without "
                "--wipe-confirmed."
            ),
        },
        indent=2,
        sort_keys=True,
    ))


def cmd_add_disk(lab: Any, args: Any) -> None:
    """Format one physical disk and register it as directory storage."""
    if not args.host_change_authorized:
        raise lab.LabError(
            "Formatting a disk and adding node storage are host-level changes. "
            "Re-run with --host-change-authorized only when the user has "
            "explicitly asked for that exact change."
        )
    api = lab.ProxmoxAPI()
    lab.load_lease(args.lease)

    disks = _disks(lab, api)
    match = next(
        (d for d in disks if d.get("devpath") == args.device), None
    )
    if match is None:
        available = sorted(d.get("devpath", "?") for d in disks)
        raise lab.LabError(
            f"{args.device} is not a disk on {lab.NODE}. Present: {available}"
        )
    described = _describe(match)

    if described["os_disk"]:
        raise lab.LabError(
            f"refusing to format {args.device}: Proxmox reports it as the OS disk"
        )
    if described["used"] and not args.wipe_confirmed:
        raise lab.LabError(
            f"refusing to format {args.device}: it is already in use as "
            f"'{described['used']}'. Everything on it would be destroyed. "
            "Pass --wipe-confirmed only after the user has confirmed that "
            "this exact disk is the one to erase."
        )
    if args.expect_serial and described["serial"] != args.expect_serial:
        raise lab.LabError(
            f"serial mismatch for {args.device}: expected "
            f"{args.expect_serial!r}, found {described['serial']!r}. Device "
            "names can be reassigned across reboots; refusing to continue."
        )
    if args.expect_size_gb:
        actual = described["size_gb"]
        if abs(actual - args.expect_size_gb) > args.expect_size_gb * 0.1:
            raise lab.LabError(
                f"size mismatch for {args.device}: expected about "
                f"{args.expect_size_gb} GB, found {actual} GB"
            )

    lab.audit(
        "disk-format-requested",
        lease=args.lease,
        device=args.device,
        serial=described["serial"],
        size_gb=described["size_gb"],
        storage=args.name,
        filesystem=args.filesystem,
        previously_used=described["used"],
    )

    upid = api.call(
        "POST",
        f"/nodes/{lab.NODE}/disks/directory",
        {
            "name": args.name,
            "device": args.device,
            "filesystem": args.filesystem,
            "add_storage": 1,
        },
    )
    status = lab.wait_task(api, upid, timeout=args.timeout)

    content_set = False
    try:
        api.call("PUT", f"/storage/{args.name}", {"content": args.content})
        content_set = True
    except lab.LabError as exc:
        note = str(exc)

    result: dict[str, Any] = {
        "device": args.device,
        "serial": described["serial"],
        "size_gb": described["size_gb"],
        "storage": args.name,
        "filesystem": args.filesystem,
        "mounted_at": f"/mnt/pve/{args.name}",
        "task_status": status,
        "content_configured": content_set,
        "content": args.content if content_set else None,
    }
    if not content_set:
        result["content_warning"] = (
            f"storage created, but setting content types failed: {note[:300]}"
        )
    lab.audit(
        "disk-formatted",
        lease=args.lease,
        device=args.device,
        storage=args.name,
        content=args.content if content_set else None,
    )
    print(json.dumps(result, indent=2, sort_keys=True))


def cmd_set_content(lab: Any, args: Any) -> None:
    if not args.host_change_authorized:
        raise lab.LabError(
            "Changing a storage definition is a host-level change. Re-run with "
            "--host-change-authorized once the user has asked for it."
        )
    api = lab.ProxmoxAPI()
    lab.load_lease(args.lease)
    api.call("PUT", f"/storage/{args.name}", {"content": args.content})
    lab.audit("storage-content-changed", lease=args.lease, storage=args.name,
              content=args.content)
    print(json.dumps({"storage": args.name, "content": args.content}, indent=2))


def cmd_download(lab: Any, args: Any) -> None:
    """Have the node fetch an image straight into storage, checksum-verified.

    The download happens on the node, so a multi-gigabyte cloud image never
    crosses the controller's link. A checksum is required by default: an
    unverified image is a supply-chain problem, not a convenience.
    """
    api = lab.ProxmoxAPI()
    lab.load_lease(args.lease)
    if not args.checksum and not args.allow_unverified:
        raise lab.LabError(
            "refusing to download without --checksum. Pass the published "
            "digest, or --allow-unverified if the user accepts an unverified "
            "image."
        )
    payload: dict[str, Any] = {
        "url": args.url,
        "content": args.content,
        "filename": args.filename,
    }
    checksum = args.checksum
    checksum_algorithm = args.checksum_algorithm
    if checksum:
        try:
            checksum, checksum_algorithm = _normalise_checksum(
                checksum, checksum_algorithm
            )
        except ValueError as exc:
            raise lab.LabError(str(exc)) from exc
        payload["checksum"] = checksum
        payload["checksum-algorithm"] = checksum_algorithm
    upid = api.call(
        "POST", f"/nodes/{lab.NODE}/storage/{args.storage}/download-url", payload
    )
    status = lab.wait_task(api, upid, timeout=args.timeout)
    volume = f"{args.storage}:{args.content}/{args.filename}"
    lab.audit(
        "storage-image-downloaded",
        lease=args.lease,
        storage=args.storage,
        filename=args.filename,
        url=args.url,
        checksum=checksum,
        checksum_algorithm=checksum_algorithm if checksum else None,
        verified=bool(checksum),
    )
    print(json.dumps(
        {"volume": volume, "verified": bool(checksum), "status": status},
        indent=2,
        sort_keys=True,
    ))


def cmd_status(lab: Any, args: Any) -> None:
    api = lab.ProxmoxAPI()
    entries = api.call("GET", f"/nodes/{lab.NODE}/storage") or []
    print(json.dumps(
        [
            {
                "storage": item.get("storage"),
                "type": item.get("type"),
                "content": item.get("content"),
                "active": bool(item.get("active")),
                "total_gb": round(int(item.get("total", 0)) / 1_000_000_000, 1),
                "avail_gb": round(int(item.get("avail", 0)) / 1_000_000_000, 1),
            }
            for item in entries
        ],
        indent=2,
        sort_keys=True,
    ))


def register(sub: Any, lab: Any) -> None:
    def bind(handler: Any) -> Any:
        return lambda args: handler(lab, args)

    storage = sub.add_parser("storage", help="physical disks and node storage")
    storage_sub = storage.add_subparsers(dest="storage_command", required=True)

    storage_sub.add_parser(
        "list-disks", help="show physical disks and which are unused"
    ).set_defaults(func=bind(cmd_list_disks))

    storage_sub.add_parser(
        "status", help="show configured storage and free space"
    ).set_defaults(func=bind(cmd_status))

    add = storage_sub.add_parser(
        "add-disk", help="format a disk and add it as directory storage"
    )
    add.add_argument("--lease", required=True)
    add.add_argument("--device", required=True, help="exact path, e.g. /dev/sdb")
    add.add_argument("--name", required=True, help="storage id, e.g. usb-bulk")
    add.add_argument("--filesystem", choices=FILESYSTEMS, default="ext4")
    add.add_argument("--content", default=DEFAULT_CONTENT)
    add.add_argument("--expect-serial", help="pin the physical disk by serial")
    add.add_argument("--expect-size-gb", type=float,
                     help="pin the physical disk by approximate size")
    add.add_argument("--wipe-confirmed", action="store_true",
                     help="allow formatting a disk Proxmox reports as in use")
    add.add_argument("--host-change-authorized", action="store_true")
    add.add_argument("--timeout", type=int, default=1800)
    add.set_defaults(func=bind(cmd_add_disk))

    download = storage_sub.add_parser(
        "download-url", help="fetch an image into storage, checksum-verified"
    )
    download.add_argument("--lease", required=True)
    download.add_argument("--url", required=True)
    download.add_argument("--filename", required=True)
    download.add_argument("--storage", default=_DEFAULT_BULK)
    download.add_argument("--content", choices=("import", "iso", "vztmpl"),
                          default="import")
    download.add_argument(
        "--checksum",
        help="digest, optionally prefixed with its algorithm (for example sha256:abc...)",
    )
    download.add_argument("--checksum-algorithm",
                          choices=("sha256", "sha512", "sha1", "md5"))
    download.add_argument("--allow-unverified", action="store_true",
                          help="skip checksum verification (discouraged)")
    download.add_argument("--timeout", type=int, default=3600)
    download.set_defaults(func=bind(cmd_download))

    content = storage_sub.add_parser(
        "set-content", help="change what a storage may hold"
    )
    content.add_argument("--lease", required=True)
    content.add_argument("--name", required=True)
    content.add_argument("--content", default=DEFAULT_CONTENT)
    content.add_argument("--host-change-authorized", action="store_true")
    content.set_defaults(func=bind(cmd_set_content))
