"""Small, read-only runbooks for models that need deterministic lab steps."""
from __future__ import annotations

import json
from typing import Any


COMMON_RULES = [
    "Run lease-begin first; never use standalone power-on.",
    "Create one lease-owned QEMU guest before using any console command.",
    "Drive one installer action per console checkpoint; use inspect for graphical decisions and never bypass calibrated clicks.",
    "Boot from the installed disk and observe the requested filesystem and desktop or login prompt before claiming success.",
    "Always lease-end and require failures=[] and host_powered_off=true.",
]

COMMON_PHASES = [
    "lease-begin",
    "download-verified-install-media",
    "api-create-qemu-and-register",
    "api-attach-disk-and-iso-then-start",
    "console-screenshot-or-inspect",
    "console-keys-or-calibrated-click",
    "boot-installed-disk-and-verify",
    "lease-end",
]

COMMON_INVALID = [
    "Do not use console exec: an installer has no guest agent or shell command.",
    "Do not use console commands until a lease-owned VMID exists.",
    "Quote every --data value containing a semicolon, especially boot=order=...;ide2.",
    "For a POST with no fields, omit --data entirely.",
]

REACTOS = {
    "name": "reactos",
    "release": "0.4.15",
    "verified_at": "2026-08-09",
    "source": "https://reactos.org/download/",
    "archive": {
        "url": "https://sourceforge.net/projects/reactos/files/ReactOS/0.4.15/ReactOS-0.4.15-release-1-gdbb43bbaeb2-x86-iso.zip/download",
        "filename": "ReactOS-0.4.15-release-1-gdbb43bbaeb2-x86-iso.zip",
        "sha256": "2be97d87fd43c93185aa841c1742fe9265c58aba0e517e20122df19d9add1935",
        "member": "ReactOS-0.4.15-release-1-gdbb43bbaeb2-x86.iso",
    },
    "rules": COMMON_RULES[:1] + [
        "Download without printing it, verify SHA-256, extract only the named ISO, then upload it as ISO content.",
        "Create one lease-owned QEMU guest with SeaBIOS, i440fx, IDE disk and CD-ROM, std VGA, and rtl8139 or e1000.",
    ] + COMMON_RULES[2:],
    "phase_order": COMMON_PHASES[:1] + [
        "download-verify-extract-locally",
        "upload-extracted-iso",
    ] + COMMON_PHASES[2:],
    "invalid_shortcuts": COMMON_INVALID + [
        "Do not upload the ZIP as ISO content; upload only the extracted .iso member.",
    ],
}

DRAGONFLY = {
    "name": "dragonfly",
    "release": "6.4.2",
    "verified_at": "2026-08-09",
    "source": "https://www.dragonflybsd.org/release64/",
    "image": {
        "url": "https://mirror-master.dragonflybsd.org/iso-images/dfly-x86_64-6.4.2_REL.iso",
        "filename": "dfly-x86_64-6.4.2_REL.iso",
        "checksum_algorithm": "md5",
        "checksum": "fdab09ac37cf427b8c01de5d54bfd1d7",
    },
    "filesystem": "HAMMER",
    "qemu": {
        "disk_storage": "local-lvm",
        "iso_storage": "local",
        "disk": "scsi0",
        "cdrom": "ide2",
        "boot": "order=scsi0;ide2",
        "network_model": "e1000",
    },
    "rules": COMMON_RULES,
    "phase_order": COMMON_PHASES,
    "invalid_shortcuts": COMMON_INVALID,
}

HAIKU = {
    "name": "haiku",
    "release": "R1/beta5",
    "verified_at": "2026-08-09",
    "source": "https://www.haiku-os.org/get-haiku/r1beta5/",
    "image": {
        "url": "https://ftp.osuosl.org/pub/haiku/r1beta5/haiku-r1beta5-x86_64-anyboot.iso",
        "filename": "haiku-r1beta5-x86_64-anyboot.iso",
        "checksum_algorithm": "sha256",
        "checksum": "22ae312a38e98083718b6984186e753d15806bd6ea44542144fdcef42c4dcb69",
    },
    "filesystem": "BFS",
    "rules": COMMON_RULES,
    "phase_order": COMMON_PHASES,
    "invalid_shortcuts": COMMON_INVALID,
}

OPENBSD = {
    "name": "openbsd",
    "release": "7.9",
    "verified_at": "2026-08-09",
    "source": "https://www.openbsd.org/79.html",
    "image": {
        "url": "https://cdn.openbsd.org/pub/OpenBSD/7.9/amd64/install79.iso",
        "filename": "install79.iso",
        "content": "iso",
        "storage": "local",
        "checksum_algorithm": "sha256",
        "checksum": "7a4a92e953618035097c796a90b54424a0f3ae775552e1e7d102cf8a5130449f",
    },
    "qemu": {
        "create_path": "/nodes/<node>/qemu",
        "vmid_field": "vmid",
        "disk": "scsi0=local-lvm:32",
        "cdrom": "ide2=local:iso/install79.iso,media=cdrom",
        "network": "virtio,bridge=vmbr0",
        "serial": "socket",
    },
    "rules": COMMON_RULES,
    "phase_order": COMMON_PHASES,
    "invalid_shortcuts": COMMON_INVALID + [
        "Pass --content iso to storage download-url; its default is import.",
        "For QEMU creation POST to /nodes/<node>/qemu with --data vmid=<id>; newid is only for cloning.",
        "Use console text for the serial installer instead of screenshot OCR or external OCR.",
    ],
}

WINDOWS_ME = {
    "name": "windows-me",
    "release": "retail media supplied by the user",
    "media": {
        "user_supplied": True,
        "requirements": "A lawfully licensed bootable Windows ME ISO and any required product key.",
        "download": None,
    },
    "qemu": {
        "firmware": "SeaBIOS",
        "machine": "pc-i440fx",
        "memory_mib": 512,
        "disk": "ide0=local-lvm:8",
        "cdrom": "ide2=<uploaded-iso>,media=cdrom",
        "vga": "std",
        "network_model": "rtl8139",
        "guest_agent": False,
    },
    "rules": COMMON_RULES[:1] + [
        "Do not use windows install; that helper targets supported Windows Server templates.",
        "Upload only user-supplied licensed media, then create a lease-owned legacy QEMU guest through api.",
    ] + COMMON_RULES[2:],
    "phase_order": [
        "lease-begin",
        "verify-and-upload-user-supplied-media",
    ] + COMMON_PHASES[2:],
    "invalid_shortcuts": COMMON_INVALID + [
        "Do not download proprietary Windows ME media or bypass product activation or licensing.",
        "Do not use UEFI, Q35, VirtIO disks, or the Server-only windows install helper.",
    ],
}

RECIPES = {
    item["name"]: item
    for item in (REACTOS, DRAGONFLY, HAIKU, OPENBSD, WINDOWS_ME)
}


def cmd_show(_lab: Any, args: Any) -> None:
    print(json.dumps(RECIPES[args.recipe_name], indent=2, sort_keys=True))


def register(sub: Any, lab: Any) -> None:
    recipe = sub.add_parser("recipe", help="print a deterministic guest runbook")
    recipe_sub = recipe.add_subparsers(dest="recipe_name", required=True)
    for name in sorted(RECIPES):
        item = recipe_sub.add_parser(name, help=f"{name} install facts and guards")
        item.set_defaults(func=lambda args: cmd_show(lab, args))
