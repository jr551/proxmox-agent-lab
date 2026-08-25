"""Android devices as lab guests: a Galaxy S20, a Pixel, a tablet.

The architecture problem, stated plainly
----------------------------------------
Proxmox is x86-64 KVM and cannot accelerate a guest of another architecture.
An **arm64** Android image therefore runs under full software emulation with
no KVM at all -- on a mid-range desktop that is not merely slow, it is close
to unusable for anything with a screen.

So `abi = "x86_64"` is the default. The device *profile* still matches the
phone -- an S20's 1440x3200 at 560 dpi, its RAM, its Android version -- and
Google's x86_64 images from API 30 carry ARM-to-x86
translation, so most apps with arm64-only native libraries still run. Pass
`--abi arm64-v8a` if you specifically need real ARM and can wait.

How it fits the rest of the tool
--------------------------------
The emulator runs on the guest VM's own console, so everything already here
composes with it: `console screenshot` shows the phone, `console click` and
`console type` drive it, and `share create` sends someone a link to it. No
Android-specific viewer.

`adb` is exposed on the VM so you can install and drive apps normally.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from . import config as _config
from . import console

_CONFIG = _config.get()

SDK_ROOT = "/opt/android-sdk"
# Nested virtualisation is what makes this usable; without it the emulator
# falls back to software and behaves like the arm64 case.
DEFAULT_API = int(_CONFIG.android.get("api_level", 33))
DEFAULT_ABI = _CONFIG.android.get("abi", "x86_64")


class AndroidError(RuntimeError):
    pass


# Real device geometry. Resolution, density, RAM, storage and API level are
# faithfully applied and verifiable from inside the guest.
#
# `model` is a best effort and measured *not* to stick: ro.product.model is
# baked into the system image's build.prop, and a read-only property that is
# already set cannot be overridden with `emulator -prop`. A stock image keeps
# reporting sdk_gphone_x86_64. Spoofing it properly means shipping a modified
# system image, which this does not do -- so treat the profile as the phone's
# *shape*, not its identity, and check with `android status`.
PROFILES: dict[str, dict[str, Any]] = {
    "galaxy-s20": {
        "label": "Samsung Galaxy S20",
        "width": 1440, "height": 3200, "density": 560,
        "ram_mb": 8192, "heap_mb": 512, "storage_mb": 8192,
        "api": 33, "model": "SM-G980F", "manufacturer": "samsung",
        "brand": "samsung", "device": "x1s",
    },
    "galaxy-s20-ultra": {
        "label": "Samsung Galaxy S20 Ultra",
        "width": 1440, "height": 3200, "density": 560,
        "ram_mb": 12288, "heap_mb": 512, "storage_mb": 8192,
        "api": 33, "model": "SM-G988B", "manufacturer": "samsung",
        "brand": "samsung", "device": "z3s",
    },
    "galaxy-s10": {
        "label": "Samsung Galaxy S10",
        "width": 1440, "height": 3040, "density": 550,
        "ram_mb": 8192, "heap_mb": 512, "storage_mb": 8192,
        "api": 31, "model": "SM-G973F", "manufacturer": "samsung",
        "brand": "samsung", "device": "beyond1",
    },
    "pixel-6": {
        "label": "Google Pixel 6",
        "width": 1080, "height": 2400, "density": 420,
        "ram_mb": 8192, "heap_mb": 512, "storage_mb": 8192,
        "api": 33, "model": "Pixel 6", "manufacturer": "Google",
        "brand": "google", "device": "oriole",
    },
    "small": {
        "label": "small phone, for speed",
        "width": 720, "height": 1280, "density": 320,
        "ram_mb": 2048, "heap_mb": 256, "storage_mb": 4096,
        "api": 30, "model": "Android SDK", "manufacturer": "Google",
        "brand": "google", "device": "generic",
    },
    "minimal": {
        # The smallest thing that is still a usable Android: no Play
        # services, modest screen, 1.5 GB. Fits on a busy host and boots
        # quickly, which makes it the right default for a first try.
        "label": "minimal phone (lowest RAM)",
        "width": 720, "height": 1600, "density": 320,
        "ram_mb": 1536, "heap_mb": 228, "storage_mb": 3072,
        "api": 30, "model": "Android SDK", "manufacturer": "Google",
        "brand": "google", "device": "generic",
        # google_apis rather than the AOSP "default" image: it is the
        # well-supported one, and from API 30 it carries ARM-to-x86
        # translation, which is most of the point of x86_64 here.
        "image_type": "google_apis", "vm_overhead_mb": 3584,
    },
}


def profile(name: str) -> dict[str, Any]:
    try:
        return PROFILES[name]
    except KeyError:
        raise AndroidError(
            f"unknown profile {name!r}. Known: {', '.join(sorted(PROFILES))}"
        ) from None


SCRIPTS = Path(__file__).parent / "android_scripts"


def _script(name: str, **values: Any) -> str:
    """Load a provisioning script and fill its placeholders.

    They live as real files rather than embedded strings so they can be read,
    diffed and run by hand -- which matters, because when an emulator will
    not boot the first thing anyone wants is the exact script that built it.
    """
    body = (SCRIPTS / name).read_text()
    for key, value in values.items():
        body = body.replace(f"__{key}__", str(value))
    missing = set(re.findall(r"__[A-Z_]+__", body))
    if missing:
        raise AndroidError(f"{name} still has placeholders: {sorted(missing)}")
    return body


def setup_script(api: int, abi: str, image_type: str = "google_apis") -> str:
    return _script("01-install-sdk.sh", SDK=SDK_ROOT, API=api, ABI=abi,
                   IMAGE_TYPE=image_type)


def avd_script(name: str, spec: dict[str, Any], api: int, abi: str) -> str:
    return _script(
        "02-create-avd.sh", SDK=SDK_ROOT, NAME=name,
        PACKAGE=f"system-images;android-{api};"
                f"{spec.get('image_type', 'google_apis')};{abi}",
        WIDTH=spec["width"], HEIGHT=spec["height"], DENSITY=spec["density"],
        RAM=spec["ram_mb"], HEAP=spec["heap_mb"], STORAGE=spec["storage_mb"],
    )


def launch_script(name: str, spec: dict[str, Any], adb_port: int) -> str:
    return _script(
        "03-launch-emulator.sh", SDK=SDK_ROOT, NAME=name,
        MODEL=spec["model"], MANUFACTURER=spec["manufacturer"],
        BRAND=spec["brand"], DEVICE=spec["device"], ADB_PORT=adb_port,
    )


# --- commands -------------------------------------------------------------


def _exec(lab: Any, api: Any, vmid: int, script: str,
          timeout: int = 1800) -> dict[str, Any]:
    console.write_guest_file(lab, api, vmid, "/tmp/pxl-android-step.sh", script)
    # bash, not sh: these steps open with `set -euo pipefail`, which dash
    # (Debian's /bin/sh) rejects outright.
    return console.exec_guest(lab, api, vmid,
                              ["/bin/bash", "/tmp/pxl-android-step.sh"],
                              timeout=timeout)


def cmd_profiles(lab: Any, args: Any) -> None:
    print(json.dumps({
        "_note": ("screen, ram_mb and android_api are applied and verifiable "
                  "in the guest; model_requested is not -- a stock system "
                  "image keeps reporting sdk_gphone_x86_64"),
        "profiles": {
            name: {"label": spec["label"],
                   "screen": f"{spec['width']}x{spec['height']} @{spec['density']}dpi",
                   "ram_mb": spec["ram_mb"], "android_api": spec["api"],
                   "model_requested": spec["model"]}
            for name, spec in sorted(PROFILES.items())},
    }, indent=2, sort_keys=True))


def _create_device_vm(lab: Any, api: Any, lease: dict[str, Any], args: Any,
                      spec: dict[str, Any], template: int) -> tuple[str, int, str]:
    """Clone, register and cloud-init the Android VM. Returns (name, memory, api_level)."""
    name = args.name or f"android-{args.profile}-{args.vmid}"
    upid = api.call("POST", f"/nodes/{lab.NODE}/qemu/{template}/clone",
                    {"newid": args.vmid, "name": name, "full": 1,
                     "target": lab.NODE})
    lab.wait_task(api, upid, timeout=args.clone_timeout)
    lab.register_resource(lease, "qemu", args.vmid, args.policy, name)
    memory = args.memory or (spec["ram_mb"] + int(spec.get("vm_overhead_mb", 3072)))
    console.prepare_cloudinit_worker(
        lab, api, args.vmid, template,
        {
            "cores": args.cores,
            "memory": memory,
            "cpu": "host",
            "vga": "std",
            "ipconfig0": "ip=dhcp",
            "tags": f"codex-lab;lease-{args.lease};android",
        },
        agent_timeout=120,
    )
    return name, memory, spec["api"]


def _run_android_steps(lab: Any, api: Any, args: Any, spec: dict[str, Any],
                       api_level: int, abi: str) -> None:
    """Run the SDK/AVD/launch steps; raises AndroidError on failure."""
    steps = (
        ("sdk", setup_script(api_level, abi,
                             spec.get("image_type", "google_apis")),
         args.setup_timeout),
        ("avd", avd_script(args.profile, spec, api_level, abi), 600),
        ("launch", launch_script(args.profile, spec, args.adb_port), 300),
    )
    for label, script, timeout in steps:
        result = _exec(lab, api, args.vmid, script, timeout=timeout)
        if result["exitcode"] not in (0, None):
            raise AndroidError(
                f"{label} step failed: "
                + (result["stderr"] or result["stdout"])[-600:])


def cmd_create(lab: Any, args: Any) -> None:
    """Build an Android device: a VM, the SDK, and an AVD on its console."""
    api = lab.ProxmoxAPI()
    lease = lab.load_lease(args.lease)
    spec = profile(args.profile)
    api_level = args.api or spec["api"]
    abi = args.abi
    if args.vmid in lease["initial_vmids"]:
        raise AndroidError(f"VMID {args.vmid} existed before this lease")
    template = args.template or int(_CONFIG.network.get("gateway_template_vmid") or 0)
    if not template:
        raise AndroidError("pass --template <vmid> of a cloud-init image")
    name, _, _ = _create_device_vm(lab, api, lease, args, spec, template)
    _run_android_steps(lab, api, args, spec, api_level, abi)
    console.clear_bootstrap_password(lab, api, args.vmid)
    templated = False
    if args.as_template:
        _make_template(lab, api, lease, args.vmid)
        templated = True
    lab.audit("android-created", lease=args.lease, vmid=args.vmid,
              profile=args.profile, api_level=api_level, abi=abi,
              template=templated)
    print(json.dumps({
        "vmid": args.vmid, "name": name, "profile": args.profile,
        "device": spec["label"], "model_requested": spec["model"],
        "screen": f"{spec['width']}x{spec['height']} @{spec['density']}dpi",
        "android_api": api_level, "abi": abi,
        "template": templated,
        "model_note": ("the device will report the stock image's model, not "
                       "the requested one; screen and RAM do apply"),
        "note": ("first boot takes several minutes while Android starts"
                 if abi == "x86_64" else
                 "arm64 runs without KVM and will be very slow"),
        "next": [
            f"watch it boot: proxmox-lab console screenshot --vmid {args.vmid}",
            f"drive it:      proxmox-lab console click --lease <id> "
            f"--vmid {args.vmid} --x .. --y ..",
            f"show someone:  proxmox-lab share create --lease <id> "
            f"--vmid {args.vmid}",
        ] if not templated else [
            f"template {args.vmid} is ready; clone it for an instant device:",
            f"  proxmox-lab api --lease <id> --method POST --path "
            f"/nodes/{lab.NODE}/qemu/{args.vmid}/clone --data newid=<new> "
            f"--wait-task",
        ],
    }, indent=2, sort_keys=True))

def _make_template(lab: Any, api: Any, lease: dict, vmid: int) -> None:
    """Shut a built device down cleanly and convert it to a template."""
    # Stop first: Proxmox refuses to template a running guest, and a clean
    # shutdown means clones start from a settled filesystem.
    status = api.call("GET", f"/nodes/{lab.NODE}/qemu/{vmid}/status/current")
    if status.get("template"):
        raise AndroidError(f"{vmid} is already a template")
    if status.get("status") == "running":
        lab.wait_task(api, api.call(
            "POST", f"/nodes/{lab.NODE}/qemu/{vmid}/status/shutdown"),
            timeout=300)
    api.call("POST", f"/nodes/{lab.NODE}/qemu/{vmid}/template")
    # A template is worth keeping, so ordinary cleanup must leave it alone.
    for resource in lease.get("resources", []):
        if int(resource["vmid"]) == vmid:
            resource["policy"] = "retain"
    lab.save_lease(lease)


def cmd_template(lab: Any, args: Any) -> None:
    """Template a device that is already built, without rebuilding it."""
    lease = lab.load_lease(args.lease)
    api = lab.ProxmoxAPI()
    _make_template(lab, api, lease, args.vmid)
    lab.audit("android-templated", lease=args.lease, vmid=args.vmid)
    print(json.dumps({
        "vmid": args.vmid,
        "template": True,
        "policy": "retain",
        "next": [
            f"clone it for an instant device:",
            f"  proxmox-lab api --lease <id> --method POST --path "
            f"/nodes/{lab.NODE}/qemu/{args.vmid}/clone --data newid=<new> "
            f"--wait-task",
        ],
    }, indent=2, sort_keys=True))


def cmd_status(lab: Any, args: Any) -> None:
    api = lab.ProxmoxAPI()
    result = console.agent_exec(
        lab, api, args.vmid,
        ["/bin/bash", "-c",
         f"systemctl is-active pxl-android || true; "
         f"{SDK_ROOT}/platform-tools/adb devices 2>/dev/null | tail -n +2; "
         f"{SDK_ROOT}/platform-tools/adb shell getprop sys.boot_completed "
         f"2>/dev/null || true"],
        timeout=120)
    lines = [line for line in result["stdout"].splitlines() if line.strip()]
    report = {
        "vmid": args.vmid,
        "service": lines[0] if lines else "unknown",
        "adb": [line for line in lines[1:] if "device" in line or "offline" in line],
        "boot_completed": "1" in lines[-1] if lines else False,
        "raw": lines[:8],
    }
    if report["boot_completed"]:
        # Report what the device actually says it is, rather than what the
        # profile asked for: the two differ, and the gap should be visible.
        actual = console.agent_exec(
            lab, api, args.vmid,
            ["/bin/bash", "-c",
             f"{SDK_ROOT}/platform-tools/adb shell getprop ro.product.model; "
             f"{SDK_ROOT}/platform-tools/adb shell wm size"],
            timeout=90)
        detail = [x.strip() for x in actual["stdout"].splitlines() if x.strip()]
        if detail:
            report["reported_model"] = detail[0]
            report["reported_screen"] = detail[-1].replace("Physical size: ", "")
            report["note"] = ("screen and RAM follow the profile; the model "
                              "string is the stock image's and cannot be "
                              "overridden without a modified system image")
    print(json.dumps(report, indent=2, sort_keys=True))


def cmd_adb(lab: Any, args: Any) -> None:
    """Run an adb command against the emulator."""
    api = lab.ProxmoxAPI()
    lab.load_lease(args.lease)
    command = " ".join(args.command)
    result = console.agent_exec(
        lab, api, args.vmid,
        ["/bin/bash", "-c", f"{SDK_ROOT}/platform-tools/adb {command}"],
        timeout=args.timeout)
    lab.audit("android-adb", lease=args.lease, vmid=args.vmid,
              argv0=args.command[0], exitcode=result["exitcode"])
    print(json.dumps({"exit_code": result["exitcode"],
                      "stdout": result["stdout"][-4000:],
                      "stderr": result["stderr"][-1000:]},
                     indent=2, sort_keys=True))


def register(sub: Any, lab: Any) -> None:
    from .cli import _bind


    android = sub.add_parser(
        "android", help="Android devices as lab guests (emulated phones)")
    android_sub = android.add_subparsers(dest="android_command", required=True)

    android_sub.add_parser("profiles", help="known device profiles"
                           ).set_defaults(func=_bind(lab, cmd_profiles))

    create = android_sub.add_parser("create", help="build an Android device")
    create.add_argument("--lease", required=True)
    create.add_argument("--vmid", type=int, required=True)
    create.add_argument("--profile", default="galaxy-s20",
                        choices=sorted(PROFILES))
    create.add_argument("--name")
    create.add_argument("--template", type=int)
    create.add_argument("--api", type=int, help="Android API level")
    create.add_argument(
        "--abi", default=DEFAULT_ABI, choices=("x86_64", "arm64-v8a"),
        help="x86_64 uses nested KVM and is usable; arm64-v8a is真 ARM but "
             "fully emulated and very slow")
    create.add_argument("--cores", type=int, default=4)
    create.add_argument("--memory", type=int,
                        help="VM RAM in MB; defaults to the phone's RAM + 3 GB")
    create.add_argument("--adb-port", type=int, default=5037)
    create.add_argument("--policy", choices=("delete", "retain"),
                        default="delete")
    create.add_argument("--as-template", action="store_true",
                        help="convert to a Proxmox template when built, so "
                             "future devices clone in seconds")
    create.add_argument("--clone-timeout", type=int, default=1800)
    create.add_argument("--setup-timeout", type=int, default=2400)
    create.set_defaults(func=_bind(lab, cmd_create))

    status = android_sub.add_parser("status", help="is the device up?")
    status.add_argument("--vmid", type=int, required=True)
    status.set_defaults(func=_bind(lab, cmd_status))

    template = android_sub.add_parser(
        "template", help="convert an already-built device to a template")
    template.add_argument("--lease", required=True)
    template.add_argument("--vmid", type=int, required=True)
    template.set_defaults(func=_bind(lab, cmd_template))

    adb = android_sub.add_parser("adb", help="run an adb command")
    adb.add_argument("--lease", required=True)
    adb.add_argument("--vmid", type=int, required=True)
    adb.add_argument("--timeout", type=int, default=300)
    adb.add_argument("command", nargs="+")
    adb.set_defaults(func=_bind(lab, cmd_adb))
