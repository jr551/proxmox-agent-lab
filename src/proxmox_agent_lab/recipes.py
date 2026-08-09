"""Small, read-only runbooks for models that need deterministic lab steps."""
from __future__ import annotations

import json
from typing import Any


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
    "rules": [
        "Run lease-begin first; never use standalone power-on.",
        "Download without printing it, verify SHA-256, extract only the named ISO, then upload it as ISO content.",
        "Create one lease-owned QEMU guest with SeaBIOS, i440fx, IDE disk and CD-ROM, std VGA, and rtl8139 or e1000.",
        "Drive one installer action per console checkpoint; use inspect for graphical decisions and never bypass calibrated clicks.",
        "Boot from the installed disk and observe the desktop plus a bundled application before claiming success.",
        "Always lease-end and require failures=[] and host_powered_off=true.",
    ],
    "phase_order": [
        "lease-begin",
        "download-verify-extract-locally",
        "upload-extracted-iso",
        "api-create-qemu-and-register",
        "api-attach-disk-and-iso-then-start",
        "console-screenshot-or-inspect",
        "console-keys-or-calibrated-click",
        "boot-installed-disk-and-observe-desktop",
        "lease-end",
    ],
    "invalid_shortcuts": [
        "Do not use console exec: an installer has no guest agent or shell command.",
        "Do not use console commands until a lease-owned VMID exists.",
        "Do not upload the ZIP as ISO content; upload only the extracted .iso member.",
    ],
}


def cmd_show(_lab: Any, _args: Any) -> None:
    print(json.dumps(REACTOS, indent=2, sort_keys=True))


def register(sub: Any, lab: Any) -> None:
    recipe = sub.add_parser("recipe", help="print a deterministic guest runbook")
    recipe_sub = recipe.add_subparsers(dest="recipe_name", required=True)
    reactos = recipe_sub.add_parser("reactos", help="ReactOS install facts and guards")
    reactos.set_defaults(func=lambda args: cmd_show(lab, args))
