"""Diagnose why a boot/install ISO won't boot -- entirely locally.

An agent in this lab builds or downloads install media constantly, and the
commonest silent failure is an ISO that only boots one firmware type: it has a
BIOS El Torito entry but no UEFI one (or vice versa), or it was never made
hybrid so it cannot boot off a USB/disk at all. `iso diagnose` reads the ISO
file on this machine and reports its identity, its El Torito BIOS/UEFI boot
entries, and whether it carries an MBR/GPT for USB booting -- no host, no VM.
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
    with open(path, "rb") as fh:
        data = fh.read(read)
    info = bootstruct.parse_iso(data)

    et = info.get("el_torito") or {}
    warnings: list[str] = []
    if not info["is_iso9660"]:
        warnings.append("not an ISO 9660 image; it will not boot as an optical "
                        "image")
    elif not et.get("present"):
        warnings.append("no El Torito boot catalog: this ISO is not bootable as "
                        "installed media (data-only)")
    else:
        if not et.get("has_bios_boot"):
            warnings.append("no bootable BIOS/legacy entry: a SeaBIOS/CSM guest "
                            "will not boot this ISO")
        if not et.get("has_uefi_boot"):
            warnings.append("no bootable UEFI entry: an OVMF/UEFI guest will not "
                            "boot this ISO")
    if info["is_iso9660"] and not info.get("hybrid"):
        warnings.append("no hybrid MBR/GPT: bootable as an optical image but not "
                        "by dd'ing to a USB/disk")

    print(json.dumps({
        "path": path,
        "bytes_read": len(data),
        **info,
        "bootable_bios": bool(et.get("has_bios_boot")),
        "bootable_uefi": bool(et.get("has_uefi_boot")),
        "warnings": warnings,
        "ok": info["is_iso9660"] and not warnings,
    }, indent=2, sort_keys=True))


def register(sub: Any, lab: Any) -> None:
    iso = sub.add_parser(
        "iso", help="diagnose and debug boot/install ISO images (local)"
    )
    iso_sub = iso.add_subparsers(dest="iso_command", required=True)

    diag = iso_sub.add_parser(
        "diagnose",
        help="report an ISO's identity and BIOS/UEFI bootability",
    )
    diag.add_argument("--path", required=True, help="path to the .iso file")
    diag.add_argument("--read-bytes", type=int, default=_READ_BYTES,
                      help="how much of the ISO to read (default: 2 MiB)")
    diag.set_defaults(func=lambda args: cmd_diagnose(lab, args))
