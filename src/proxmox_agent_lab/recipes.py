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
    "qemu": {
        "firmware": "seabios",
        "machine": "pc-i440fx",
        "memory_mib": 1024,
        "disk_bus": "ide",
        "cdrom_bus": "ide2",
        "boot": "order=ide2;ide0",
        "vga": "std",
        "network_model": "e1000",
        "serial": "socket",
        "guest_agent": False,
    },
    "installation": {
        "media": "boot-cd",
        "architecture": "x86",
        "stages": [
            "text-mode-setup",
            "gui-setup",
            "first-desktop-boot",
        ],
        "final_check": [
            "Detach the ISO after setup and boot the installed IDE disk.",
            "Use VNC screenshot or inspect for GUI state; serial0 is for debug output when needed.",
        ],
    },
    "rules": COMMON_RULES[:1] + [
        "Download without printing it, verify SHA-256, extract only the named ISO, then upload it as ISO content.",
        "Create one lease-owned QEMU guest with legacy SeaBIOS, i440fx, IDE disk and CD-ROM, std VGA, e1000 networking, serial0, and no assumed guest agent.",
    ] + COMMON_RULES[2:],
    "phase_order": COMMON_PHASES[:1] + [
        "download-verify-extract-locally",
        "upload-extracted-iso",
    ] + COMMON_PHASES[2:4] + [
        "installer-text-mode",
        "installer-gui",
    ] + COMMON_PHASES[4:],
    "invalid_shortcuts": COMMON_INVALID + [
        "Do not upload the ZIP as ISO content; upload only the extracted .iso member.",
        "Do not use UEFI/OVMF, SATA, or VirtIO storage for the legacy compatibility path; keep SeaBIOS and IDE unless a separately verified experiment requires otherwise.",
        "Do not assume a ReactOS guest agent or shell channel; use VNC and, when configured, serial output.",
        "Do not treat a successful boot as stability evidence; ReactOS documents the 0.4.15 line as alpha software.",
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

ANDROID_X86 = {
    "name": "android-x86",
    "release": "9.0-r2",
    "verified_at": "2026-08-14",
    "source": "https://www.android-x86.org/",
    "note": "A real Android-x86 boot-to-metal OS install in a QEMU VM -- "
            "distinct from 'android create', which runs the Google SDK "
            "emulator inside a Debian VM. Use this when a genuine device "
            "network stack matters, e.g. driving Android's own proxy "
            "settings from an external tool.",
    "image": {
        "url": "https://sourceforge.net/projects/android-x86/files/Release%209.0/android-x86_64-9.0-r2-k49.iso/download",
        "filename": "android-x86_64-9.0-r2-k49.iso",
        "content": "iso",
        "storage": "local",
        "checksum_algorithm": "md5",
        "checksum": "024d05fea10cdbc896e72d53f259a367",
    },
    "qemu": {
        "firmware": "seabios",
        "memory_mib": 1536,
        "disk": "scsi0=local-lvm:16",
        "scsihw": "virtio-scsi-single",
        "cdrom": "ide2=local:iso/android-x86_64-9.0-r2-k49.iso,media=cdrom",
        "boot": "order=ide2;scsi0",
        "vga": "std",
        "network_model": "virtio",
        "network_bridge": "the bridge your OWN workstation or proxy tool can "
                           "reach directly -- usually the same bridge the "
                           "Proxmox host's management IP is on, e.g. vmbr0. "
                           "NOT the isolated lab-only bridge: verified live "
                           "that adb/HTTP from an external machine cannot "
                           "reach a guest placed there.",
        "guest_agent": False,
    },
    "installation": {
        "media": "boot-cd",
        "stages": [
            "grub-menu-choose-installation",
            "cfdisk-create-one-bootable-primary-partition-write",
            "format-ext4",
            "install-grub-yes",
            "system-partition-read-write-yes",
            "eject-iso-boot-from-disk",
            "setup-wizard",
        ],
        "setup_wizard": {
            "network": "the Wi-Fi picker lists the virtio NIC as an AP "
                       "named 'VirtWifi'; connect to it like any network",
            "known_quirk": "on 9.0-r2 the wizard can loop back to 'Copy "
                           "apps & data' after reaching Google Services "
                           "Accept, repeatedly, rather than completing. "
                           "Verified live. Do not keep retrying the wizard "
                           "UI -- switch to the on-device root shell "
                           "(Alt+F1 from the graphical console) and run: "
                           "settings put secure user_setup_complete 1 && "
                           "settings put global device_provisioned 1, then "
                           "power-cycle (stop/start, not just reset, to "
                           "reinitialize the virtio NIC cleanly) to land "
                           "directly on the home screen.",
            "keyboard_navigable": "every screen observed responded to tab/"
                                  "shift-tab/arrows plus enter for its "
                                  "focused control -- prefer this over "
                                  "console click on this OS. Its calibrated "
                                  "vision-click verification rejected small "
                                  "text links ('SKIP', 'ACCEPT') on this UI "
                                  "repeatedly across multiple providers; "
                                  "keyboard focus navigation was reliable "
                                  "every time it was tried instead.",
        },
        "final_check": [
            "adb connect <device-ip>:5555 from the machine that will run "
            "the proxy succeeds (enable first: setprop "
            "service.adb.tcp.port 5555 && stop adbd && start adbd, from "
            "the root shell or a prior adb session).",
        ],
    },
    "proxying": {
        "set_proxy": "adb shell settings put global http_proxy "
                     "<proxy-host>:<proxy-port> -- verified live with a "
                     "plain listener: the device's own browser traffic "
                     "(a CONNECT to a real HTTPS host) arrived at the "
                     "configured host:port. Any HTTP(S)-capable proxy "
                     "works; this recipe does not wire up or assume one.",
        "clear_proxy": "adb shell settings put global http_proxy :0",
        "install_ca_cert": "push the proxy's CA cert (in .0 hash form, "
                           "or any format Android's certificate installer "
                           "accepts) to the device with 'proxmox-lab push', "
                           "then Settings > Security > Encryption & "
                           "credentials > Install a certificate. Not "
                           "verified live in this pass -- the mechanism is "
                           "standard Android, not android-x86-specific.",
    },
    "rules": COMMON_RULES[:1] + [
        "Download without printing it, verify the checksum, then upload as "
        "ISO content; keep the guest disk on fast storage.",
        "Create one lease-owned QEMU guest with SeaBIOS, virtio-scsi disk, "
        "virtio network on a bridge reachable from wherever the proxy "
        "runs, std VGA, and no assumed guest agent.",
        "Drive the setup wizard with keyboard focus navigation "
        "(tab/shift-tab/arrows, enter), not console click.",
        "If the setup wizard loops after Google Services, use the "
        "on-device root shell (Alt+F1) to mark provisioning complete "
        "instead of retrying the UI.",
    ] + COMMON_RULES[2:],
    "phase_order": COMMON_PHASES[:1] + [
        "download-verify-extract-locally",
        "upload-verified-iso",
    ] + COMMON_PHASES[2:4] + [
        "installer-partition-format-grub",
        "boot-from-disk",
        "setup-wizard-or-root-shell-bypass",
        "configure-proxy",
    ] + COMMON_PHASES[7:],
    "invalid_shortcuts": COMMON_INVALID + [
        "Do not place the guest's network on an isolated/VPN-only lab "
        "bridge if an external proxy or adb client needs to reach it "
        "directly -- verified live that it is not routable from outside.",
        "Do not assume the in-guest 'Reboot' menu action actually power-"
        "cycled the VM; verify via the VM's uptime or force a reset/stop-"
        "start if the boot screen looks unchanged.",
        "Do not keep retrying a rejected console click more than the "
        "documented limit on this UI; switch to keyboard navigation or "
        "the root shell instead of manufacturing new click labels.",
        "Do not wire this recipe to any specific proxy tool (Burp or "
        "otherwise) or to this project's own netcap/MITM relay -- it only "
        "gets the device to a state where 'adb shell settings put global "
        "http_proxy' points it at whatever the user runs.",
    ],
}

RECIPES = {
    item["name"]: item
    for item in (REACTOS, DRAGONFLY, HAIKU, OPENBSD, WINDOWS_ME, ANDROID_X86)
}


def cmd_show(_lab: Any, args: Any) -> None:
    print(json.dumps(RECIPES[args.recipe_name], indent=2, sort_keys=True))


def register(sub: Any, lab: Any) -> None:
    recipe = sub.add_parser("recipe", help="print a deterministic guest runbook")
    recipe_sub = recipe.add_subparsers(dest="recipe_name", required=True)
    for name in sorted(RECIPES):
        item = recipe_sub.add_parser(name, help=f"{name} install facts and guards")
        item.set_defaults(func=lambda args: cmd_show(lab, args))
