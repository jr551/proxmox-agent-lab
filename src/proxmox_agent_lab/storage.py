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


def _validate_wipe_target(lab: Any, api: Any, args: Any) -> dict[str, Any]:
    """Lookup the device and enforce OS-disk / used / serial / size guards."""
    disks = _disks(lab, api)
    match = next((d for d in disks if d.get("devpath") == args.device), None)
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
    return described


def _set_content_with_fallback(lab: Any, api: Any, name: str,
                               content: str) -> tuple[bool, str]:
    """Try to set storage content types; returns (ok, note)."""
    try:
        api.call("PUT", f"/storage/{name}", {"content": content})
        return True, ""
    except lab.LabError as exc:
        return False, str(exc)


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
    described = _validate_wipe_target(lab, api, args)
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
    content_set, note = _set_content_with_fallback(lab, api, args.name, args.content)
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
        content_configured=content_set,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    if not content_set:
        # The command's contract is a registered storage set to hold the
        # requested content types. Half of that is not success: a caller that
        # sees exit 0 goes on to upload an ISO the storage will not accept.
        # The result above is printed first so the recovery details survive.
        raise lab.LabError(
            f"storage {args.name} was created from {args.device} but its "
            f"content types were not set: {note[:300]}. Finish with "
            f"'proxmox-lab storage set-content --name {args.name} --content "
            f"{args.content} --host-change-authorized' (the disk is already "
            "formatted; re-running add-disk would erase it again)."
        )

# --- unreferenced image garbage collection --------------------------------
#
# A failed create, or a guest destroyed outside a lease (or before the
# unreferenced-disk retry in delete_guest existed), leaves a disk image behind
# with no config pointing at it. Deleting one is irreversible, so this finds
# candidates and reports them; deletion needs a second, explicit run.

def _image_stores(lab: Any, api: Any) -> list[str]:
    """Storage ids on this node that may hold guest images."""
    entries = api.call("GET", f"/nodes/{lab.NODE}/storage") or []
    return sorted(
        str(item.get("storage"))
        for item in entries
        if isinstance(item, dict) and item.get("storage")
        and "images" in str(item.get("content") or "")
    )


def _volumes(lab: Any, api: Any, store: str) -> list[dict[str, Any]]:
    listing = api.call(
        "GET", f"/nodes/{lab.NODE}/storage/{store}/content", {"content": "images"}
    ) or []
    return [item for item in listing if isinstance(item, dict) and item.get("volid")]


def _referenced_volids(lab: Any, api: Any) -> set[str]:
    """Every volid mentioned by any guest config on the node.

    Deliberately blunt: every string value of every config key is kept, because
    a false "orphan" here would authorise deleting a disk that is genuinely in
    use. Missing a real orphan only means it is reported next time.

    Snapshots are read as well as the live config, and that is not optional. A
    snapshot's `vmstate` volume (`vm-<id>-state-<name>`) is listed by the
    storage as ordinary `images` content but appears *only* in the snapshot's
    own config -- so a live-config-only scan would have called it unreferenced
    and offered to delete the thing a rollback needs.
    """
    referenced: set[str] = set()

    def keep(config: Any) -> None:
        if isinstance(config, dict):
            referenced.update(
                value for value in config.values() if isinstance(value, str)
            )

    def read(path: str, what: str) -> Any:
        try:
            return api.call("GET", path)
        except lab.LabError as exc:
            # Anything unreadable is something we cannot clear volumes for.
            raise lab.LabError(
                f"could not read {what}: {exc}. Refusing to classify any "
                "volume as unreferenced"
            ) from None

    for guest in api.call("GET", "/cluster/resources", {"type": "vm"}) or []:
        if not isinstance(guest, dict) or "vmid" not in guest:
            continue
        kind = str(guest.get("type") or "qemu")
        vmid = int(guest["vmid"])
        base = f"/nodes/{lab.NODE}/{kind}/{vmid}"
        keep(read(f"{base}/config", f"the config of {kind}/{vmid}"))
        snapshots = read(f"{base}/snapshot", f"the snapshots of {kind}/{vmid}")
        for snapshot in snapshots or []:
            name = isinstance(snapshot, dict) and snapshot.get("name")
            if not name or name == "current":
                continue
            keep(read(
                f"{base}/snapshot/{name}/config",
                f"snapshot {name} of {kind}/{vmid}",
            ))
    return referenced


def _is_referenced(volid: str, referenced: set[str]) -> bool:
    if volid in referenced:
        return True
    # Config values carry options: "usb-bulk:9231/vm-9231-disk-0.raw,size=100G".
    return any(volid in value for value in referenced)


def cmd_gc(lab: Any, args: Any) -> None:
    """Find image volumes no guest config references. Reports by default."""
    api = lab.ProxmoxAPI()
    stores = [args.storage] if args.storage else _image_stores(lab, api)
    referenced = _referenced_volids(lab, api)
    orphaned: list[dict[str, Any]] = []
    kept = 0
    for store in stores:
        for volume in _volumes(lab, api, store):
            volid = str(volume["volid"])
            vmid = volume.get("vmid")
            if args.vmid is not None and str(vmid) != str(args.vmid):
                continue
            if _is_referenced(volid, referenced):
                kept += 1
                continue
            orphaned.append({
                "volid": volid,
                "storage": store,
                "vmid": int(vmid) if str(vmid or "").isdigit() else None,
                # Provisioned size and bytes actually on disk are very
                # different numbers for a thin volume or a sparse qcow2, and
                # only the second one comes back when the volume is deleted.
                # Reporting the first as if it were reclaimable space invites
                # an irreversible deletion for a gain that is not there.
                "size_gb": round(int(volume.get("size") or 0) / 1_000_000_000, 2),
                "used_gb": round(int(volume.get("used") or 0) / 1_000_000_000, 3),
                "format": volume.get("format"),
            })
    orphaned.sort(key=lambda item: item["volid"])
    result: dict[str, Any] = {
        "stores": stores,
        "referenced_volumes": kept,
        "orphaned_volumes": orphaned,
        "orphaned_provisioned_gb": round(sum(x["size_gb"] for x in orphaned), 2),
        "orphaned_on_disk_gb": round(sum(x["used_gb"] for x in orphaned), 3),
        "deleted": [],
    }
    if not args.delete:
        result["dry_run"] = True
        if orphaned:
            result["to_delete"] = (
                "re-run with --delete --host-change-authorized to remove "
                "exactly these volumes"
            )
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    if not args.host_change_authorized:
        print(json.dumps(result, indent=2, sort_keys=True))
        raise lab.LabError(
            "Deleting node storage content is a host-level change. Re-run "
            "with --host-change-authorized once the user has asked for it."
        )
    if args.lease:
        lab.load_lease(args.lease)
    failures: dict[str, str] = {}
    for item in orphaned:
        # Only volumes this same run classified as unreferenced are deleted;
        # nothing is taken from an earlier report.
        try:
            api.call(
                "DELETE",
                f"/nodes/{lab.NODE}/storage/{item['storage']}/content/"
                f"{item['volid']}",
            )
            lab.audit("storage-volume-deleted", lease=args.lease,
                      volid=item["volid"], size_gb=item["size_gb"],
                      used_gb=item["used_gb"], storage=item["storage"])
            result["deleted"].append(item["volid"])
        except lab.LabError as exc:
            failures[item["volid"]] = str(exc)[:300]
    if failures:
        result["failed"] = failures
    print(json.dumps(result, indent=2, sort_keys=True))
    if failures:
        raise lab.LabError(f"{len(failures)} volume(s) could not be deleted")


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


def storage_class(name: str) -> str:
    """`bulk` for the configured slow/roomy store, `fast` for anything else.

    Callers branch on this instead of hardcoding a site's storage id. The
    distinction is not cosmetic: a USB directory store measured ~25 MB/s
    sequential write on the lab node, so a guest disk placed there turns any
    I/O comparison into a measurement of the cable.
    """
    return "bulk" if name and name == _DEFAULT_BULK else "fast"


def cmd_status(lab: Any, args: Any) -> None:
    api = lab.ProxmoxAPI()
    entries = api.call("GET", f"/nodes/{lab.NODE}/storage") or []
    print(json.dumps(
        [
            {
                "storage": item.get("storage"),
                "type": item.get("type"),
                "class": storage_class(str(item.get("storage") or "")),
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
    from .cli import _bind


    storage = sub.add_parser("storage", help="physical disks and node storage")
    storage_sub = storage.add_subparsers(dest="storage_command", required=True)

    storage_sub.add_parser(
        "list-disks", help="show physical disks and which are unused"
    ).set_defaults(func=_bind(lab, cmd_list_disks))

    storage_sub.add_parser(
        "status", help="show configured storage and free space"
    ).set_defaults(func=_bind(lab, cmd_status))

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
    add.set_defaults(func=_bind(lab, cmd_add_disk))

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
    download.set_defaults(func=_bind(lab, cmd_download))

    gc = storage_sub.add_parser(
        "gc",
        help="find image volumes no guest config references (reports only "
             "unless --delete)",
        description="Lists every volume on the node's images-capable storage, "
                    "checks it against every guest config, and reports what "
                    "nothing references. Deletion is a separate, explicit run: "
                    "--delete --host-change-authorized, and only volumes that "
                    "same run classified as unreferenced.",
    )
    gc.add_argument("--storage", help="one storage id (default: all images stores)")
    gc.add_argument("--vmid", type=int, help="only volumes named for this VMID")
    gc.add_argument("--dry-run", action="store_true",
                    help="explicit no-op: reporting is already the default")
    gc.add_argument("--delete", action="store_true",
                    help="delete the volumes this run found unreferenced")
    gc.add_argument("--host-change-authorized", action="store_true")
    gc.add_argument("--lease", help="optional, recorded in the audit event")
    gc.set_defaults(func=_bind(lab, cmd_gc))

    content = storage_sub.add_parser(
        "set-content", help="change what a storage may hold"
    )
    content.add_argument("--lease", required=True)
    content.add_argument("--name", required=True)
    content.add_argument("--content", default=DEFAULT_CONTENT)
    content.add_argument("--host-change-authorized", action="store_true")
    content.set_defaults(func=_bind(lab, cmd_set_content))
