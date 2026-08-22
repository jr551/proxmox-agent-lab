"""Diagnose why a boot/install ISO won't boot -- entirely locally.

An agent in this lab builds or downloads install media constantly, and the
commonest silent failure is an ISO that only boots one firmware type: it has a
BIOS El Torito entry but no UEFI one (or vice versa), or it was never made
hybrid so it cannot boot off a USB/disk at all. `iso diagnose` reads the ISO
file on this machine and reports its identity, its El Torito BIOS/UEFI boot
entries, and whether it carries an MBR/GPT for USB booting -- no host, no VM.

The nastier variant is an image that was reassembled by hand. Re-running
mkisofs without its El Torito options rebuilds the whole file tree and drops
the boot record, and nothing complains: the ISO attaches, the guest powers on,
and SeaBIOS shows a black screen with no keyboard and no error. So the catalog
is decoded properly here -- validation entry, per-entry platform, emulation
type, load segment, boot-load-size and image extent -- and each way it can be
broken is reported by name, before the image is ever attached to a guest. See
docs/reactos.md for the build workflow that produces such an image.
"""

from __future__ import annotations

import json
import os
from typing import Any

from . import bootstruct

# Enough of the image to cover the volume descriptors and a nearby boot
# catalog; El Torito catalogs on real installers sit within the first MiB.
_READ_BYTES = 2 * 1024 * 1024


def cmd_diagnose(lab: Any, args: Any) -> None:
    path = os.path.expanduser(args.path)
    if not os.path.isfile(path):
        raise lab.LabError(f"no such ISO file: {path}")
    read = min(_READ_BYTES, max(args.read_bytes, 64 * 1024))
    file_bytes = os.path.getsize(path)
    with open(path, "rb") as fh:
        data = fh.read(read)
    info = bootstruct.parse_iso(data, file_bytes)

    et = info.get("el_torito") or {}
    tree = info.get("tree") or {}
    warnings: list[str] = []
    if not info["is_iso9660"]:
        warnings.append("not an ISO 9660 image; it will not boot as an optical "
                        "image")
    elif not et.get("present"):
        warnings.extend(_no_boot_record_warnings(tree))
    elif not et.get("catalog_usable"):
        warnings.append(
            "the boot record is present but the catalog is unusable ("
            + "; ".join(et.get("errors") or ["reason unknown"])
            + "): firmware that reads this image finds nothing to boot. "
            "Rebuild the ISO rather than patching it, and keep the El Torito "
            f"options: {bootstruct.ELTORITO_MKISOFS_FLAGS} (the boot image "
            "path is build-specific)")
    else:
        if not et.get("has_bios_boot"):
            warnings.append("no bootable BIOS/legacy entry: a SeaBIOS/CSM guest "
                            "will not boot this ISO")
        if not et.get("has_uefi_boot"):
            warnings.append("no bootable UEFI entry: an OVMF/UEFI guest will not "
                            "boot this ISO")
        warnings.extend(_entry_warnings(et, file_bytes))
    if info["is_iso9660"] and not info.get("hybrid"):
        warnings.append("no hybrid MBR/GPT: bootable as an optical image but not "
                        "by dd'ing to a USB/disk")

    print(json.dumps({
        "path": path,
        "bytes_read": len(data),
        "file_bytes": file_bytes,
        **info,
        "bootable_bios": bool(et.get("has_bios_boot")),
        "bootable_uefi": bool(et.get("has_uefi_boot")),
        "el_torito_ok": _el_torito_ok(et),
        "warnings": warnings,
        "ok": info["is_iso9660"] and not warnings,
    }, indent=2, sort_keys=True))


def _no_boot_record_warnings(tree: dict[str, Any]) -> list[str]:
    """Explain a missing El Torito structure in terms of how it went missing.

    A tree full of bootloader files with no boot record over it is not a data
    ISO -- it is the output of a plain `mkisofs` re-run over an installer's
    file tree, which is exactly the repair an operator reaches for when the
    build's own ISO step fails on a missing file. That image boots to a black
    screen and reports nothing, so name the remedy here.
    """
    if not tree.get("looks_bootable"):
        return ["no El Torito boot catalog: this ISO is not bootable as "
                "installed media (data-only)"]
    found = ", ".join(tree.get("boot_markers") or [])
    return [
        "no El Torito boot record at all, but the file tree is bootloader-"
        f"shaped ({found}): this is what plain mkisofs produces when the boot "
        "options are omitted, and the guest will sit at a black screen with "
        "no keyboard and no error. Reassemble with the mandatory options: "
        f"{bootstruct.ELTORITO_MKISOFS_FLAGS} (the boot image path is "
        "build-specific)",
    ]


def _entry_warnings(et: dict[str, Any], file_bytes: int) -> list[str]:
    """Flag catalog entries that name a boot image the firmware cannot load."""
    out: list[str] = []
    for entry in et.get("entries") or []:
        if not entry.get("bootable"):
            continue
        label = f"the {entry.get('kind')} {entry.get('platform')} entry"
        if not entry.get("image_in_range", True):
            out.append(
                f"{label} points at boot image LBA {entry.get('load_rba')} "
                f"(byte {entry.get('image_offset')}), which is past the end of "
                f"the {file_bytes}-byte image: there is nothing there to load")
        elif not entry.get("load_sectors"):
            out.append(
                f"{label} has a boot-load-size of 0 sectors, so the firmware "
                "loads nothing from it (-boot-load-size 4 is the usual value "
                "for a no-emulation boot image)")
    return out


def _el_torito_ok(et: dict[str, Any]) -> bool:
    """True only when some entry would actually hand control to a bootloader."""
    if not et.get("present") or not et.get("catalog_usable"):
        return False
    return any(
        e.get("bootable") and e.get("image_in_range") and e.get("load_sectors")
        for e in et.get("entries") or []
    )


def register(sub: Any, lab: Any) -> None:
    iso = sub.add_parser(
        "iso", help="diagnose and debug boot/install ISO images (local)"
    )
    iso_sub = iso.add_subparsers(dest="iso_command", required=True)

    diag = iso_sub.add_parser(
        "diagnose",
        help="report an ISO's identity, El Torito boot catalog and "
             "BIOS/UEFI bootability",
    )
    diag.add_argument("--path", required=True, help="path to the .iso file")
    diag.add_argument("--read-bytes", type=int, default=_READ_BYTES,
                      help="how much of the ISO to read (default: 2 MiB)")
    diag.set_defaults(func=lambda args: cmd_diagnose(lab, args))
