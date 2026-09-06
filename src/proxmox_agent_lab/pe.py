"""Windows PE (Hiren's BootCD PE) inspection, extraction, boot and rebuild.

This module works with a *user-supplied* Windows PE ISO.  It does **not**
contain, download, or redistribute any copyrighted material.  The operator is
solely responsible for lawfully obtaining the source ISO and for any use of the
tools it contains.

Commands:

* ``pe catalog``  -- report the ISO's boot record, WinPE WIM, and tool tree.
* ``pe extract``  -- extract the ISO contents and (optionally) the boot.wim.
* ``pe build``    -- build a customised PE ISO by filtering/remapping files.
* ``pe boot``     -- upload the ISO to Proxmox and boot a lease-owned VM.

External tools are used when they are present.  ``xorriso``, ``7z`` and
``wimlib`` are not bundled; the commands fail clearly when a required tool is
missing and print the equivalent manual command line.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from . import bootstruct
from . import isoinspect
from . import windows as windows_module

LEGAL_NOTICE = (
    "This command operates on a user-supplied Windows PE image. The operator "
    "is solely responsible for complying with the license terms of the source "
    "media and any bundled tools. No copyrighted material from the ISO is "
    "stored in or distributed by this repository."
)

# Known root/top-level directories that identify a PE/rescue ISO.  These are
# used to orient the operator; the code does not depend on any of them being
# present.
PE_FOLDERS = frozenset({
    "HBCD", "PROGRAMS", "SOURCES", "BOOT", "EFI", "CUSTOM", "DRIVERS",
})

# Paths to boot files that have been observed on WinPE/Hiren ISOs.  They are
# candidates for the El Torito boot image when a build tool has to rebuild an
# ISO from a staging tree.
BIOS_BOOT_FILES = ("BOOT/ETFSBOOT.COM", "ETFSBOOT.COM")
UEFI_BOOT_FILES = (
    "EFI/MICROSOFT/BOOT/EFISYS.BIN",
    "EFI/BOOT/EFISYS.BIN",
    "EFISYS.BIN",
    "EFI/BOOT/BOOTX64.EFI",
)

# The catalog command only ever reads this much of the ISO; the El Torito
# structures and the first tree levels live inside the first MiB or two.
_READ_BYTES = 2 * 1024 * 1024


def _find_tool(name: str) -> str | None:
    """Return the absolute path of an executable, or None."""
    return shutil.which(name)


def _run(argv: list[str], *, timeout: int = 300, check: bool = True,
         cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run an external tool, returning its CompletedProcess."""
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
        cwd=cwd,
    )


def _sha256(path: Path) -> str:
    """SHA-256 of a file, streamed so a 3 GiB ISO does not exhaust memory."""
    with open(path, "rb") as fh:
        digest = hashlib.file_digest(fh, "sha256")
    return digest.hexdigest()


def _require_file(lab: Any, path: Path, what: str = "ISO file") -> None:
    if not path.is_file():
        raise lab.LabError(f"no such {what}: {path}")


def _require_legal(lab: Any, args: Any) -> None:
    """Customization/boot commands act on licensed media; require the opt-in."""
    if not getattr(args, "legal_accepted", False):
        raise lab.LabError(
            f"{LEGAL_NOTICE} Re-run with --legal-accepted to confirm you "
            "accept responsibility for the license terms of the supplied "
            "media."
        )


def _parse_iso(path: Path, read_bytes: int = _READ_BYTES) -> dict[str, Any]:
    """Use the shared ISO parser to report bootability and tree."""
    size = path.stat().st_size
    read = min(max(read_bytes, 64 * 1024), _READ_BYTES)
    with open(path, "rb") as fh:
        data = fh.read(read)
    return bootstruct.parse_iso(data, size)


def _find_wim_in_tree(tree: dict[str, Any]) -> str | None:
    """Return the ISO path of a boot.wim if one is visible in the tree.

    The tree parser upper-cases every name and only walks the first two
    levels, so a ``SOURCES/BOOT.WIM`` marker shows the exact on-disc name
    while a bare ``SOURCES/`` root entry tells us to look for the canonical
    WinPE path.
    """
    if not tree:
        return None
    for marker in tree.get("boot_markers") or []:
        normalized = marker.replace("\\", "/")
        if normalized.rsplit("/", 1)[-1] == "BOOT.WIM":
            return normalized
    for entry in tree.get("root_entries") or []:
        if entry.rstrip("/") == "SOURCES":
            return "sources/boot.wim"
    return None


def _find_extracted_wim(root: Path) -> Path | None:
    """Locate ``sources/boot.wim`` in an extracted tree, case-insensitively."""
    for dirname in ("sources", "SOURCES"):
        folder = root / dirname
        if not folder.is_dir():
            continue
        for name in ("boot.wim", "BOOT.WIM"):
            candidate = folder / name
            if candidate.is_file():
                return candidate
        try:
            for item in folder.iterdir():
                if item.is_file() and item.name.lower() == "boot.wim":
                    return item
        except OSError:
            continue
    return None


def _wim_info(wim_path: Path) -> dict[str, Any] | None:
    """Ask wimlib-imagex for the images inside a WIM, if the tool is present."""
    tool = _find_tool("wimlib-imagex")
    if tool is None:
        return None
    try:
        result = _run([tool, "info", str(wim_path)])
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        return {"error": str(exc)}
    images: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line in result.stdout.splitlines():
        line = line.rstrip()
        if line.startswith("Index:"):
            if current:
                images.append(current)
            current = {"index": int(line.split(":", 1)[1].strip())}
        elif ":" in line and current:
            key, value = line.split(":", 1)
            current[key.strip().lower().replace(" ", "_")] = value.strip()
    if current:
        images.append(current)
    return {"images": images, "tool": tool}


def _extract_wim_from_iso(lab: Any, iso: Path, iso_path: str,
                          dest: Path) -> bool:
    """Pull one file out of an ISO with 7z or xorriso. Best-effort."""
    tool_7z = _find_tool("7z")
    if tool_7z:
        try:
            _run([
                tool_7z, "e", "-y", f"-o{dest.parent}", str(iso), iso_path,
            ])
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass
        # 7z 'e' flattens paths, so the file lands directly under -o.
        extracted = dest.parent / Path(iso_path).name
        if extracted.is_file():
            if extracted != dest:
                extracted.replace(dest)
            return True
    xorriso = _find_tool("xorriso")
    if xorriso:
        for candidate in {iso_path, iso_path.upper(), iso_path.lower()}:
            try:
                _run([
                    xorriso, "-osirrox", "on", "-indev", str(iso),
                    "-extract", "/" + candidate.lstrip("/"), str(dest),
                ])
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                continue
            if dest.is_file():
                return True
    return False


def _wim_apply(lab: Any, wim: Path, index: int, target: Path) -> str:
    """Apply one WIM image to a directory; return the tool that did it."""
    tool = _find_tool("wimlib-imagex")
    if tool:
        _run([tool, "apply", str(wim), str(index), str(target)], timeout=1800)
        return "wimlib-imagex"
    dism = _find_tool("dism")
    if dism:
        _run([
            dism, "/Apply-Image", f"/ImageFile:{wim}",
            f"/Index:{index}", f"/ApplyDir:{target}",
        ], timeout=1800)
        return "dism"
    raise lab.LabError(
        "applying a WIM image needs wimlib-imagex (Linux) or dism.exe "
        "(Windows); neither was found. Manual equivalent: "
        f"wimlib-imagex apply {wim} {index} {target}"
    )


def _wim_mount_rw(lab: Any, wim: Path, index: int, mount: Path) -> str:
    """Mount a WIM read-write; return the tool used so unmount matches."""
    tool = _find_tool("wimlib-imagex")
    if tool:
        _run([tool, "mountrw", str(wim), str(index), str(mount)],
             timeout=900)
        return "wimlib-imagex"
    dism = _find_tool("dism")
    if dism:
        _run([
            dism, "/Mount-Wim", f"/WimFile:{wim}",
            f"/Index:{index}", f"/MountDir:{mount}",
        ], timeout=900)
        return "dism"
    raise lab.LabError(
        "modifying a WIM image needs wimlib-imagex (Linux) or dism.exe "
        "(Windows); neither was found. Manual equivalent: "
        f"wimlib-imagex mountrw {wim} {index} <mount-dir>"
    )


def _wim_unmount(lab: Any, mount: Path, tool: str, *, commit: bool) -> None:
    """Unmount a WIM, committing or discarding the changes."""
    if tool == "wimlib-imagex":
        argv = ["wimlib-imagex", "unmount", str(mount)]
        if commit:
            argv.append("--commit")
        resolved = _find_tool("wimlib-imagex") or argv[0]
        _run([resolved, *argv[1:]], timeout=900)
        return
    argv = [
        _find_tool("dism") or "dism",
        "/Unmount-Wim", f"/MountDir:{mount}",
        "/Commit" if commit else "/Discard",
    ]
    _run(argv, timeout=900)


def _script_argv(script: Path) -> list[str]:
    """Run a customization script through the platform shell."""
    if os.name == "nt":
        return ["cmd", "/c", str(script)]
    return ["sh", str(script)]


def _list_programs(root: Path) -> dict[str, Any]:
    """Walk an extracted ISO tree and report likely tool categories."""
    programs: dict[str, list[str]] = {}
    for marker in ("HBCD", "Programs", "PROGRAMS", "Program Files"):
        folder = root / marker
        if folder.is_dir():
            programs[marker] = sorted(
                p.name for p in folder.iterdir() if p.is_dir()
            )
    return {
        "root_files": sorted(
            p.name for p in root.iterdir() if p.is_file()
        ),
        "root_dirs": sorted(
            p.name for p in root.iterdir() if p.is_dir()
        ),
        "tool_categories": programs,
    }


# --------------------------------------------------------------------------- #
# catalog
# --------------------------------------------------------------------------- #

def cmd_catalog(lab: Any, args: Any) -> None:
    """Report the boot record, WinPE WIM, and visible tool tree of a PE ISO."""
    path = Path(os.path.expanduser(args.iso)).resolve()
    _require_file(lab, path)

    parsed = _parse_iso(path, args.read_bytes)
    summary = isoinspect.summarize(parsed, path.stat().st_size)
    wim_path = _find_wim_in_tree(parsed.get("tree") or {})
    wim: dict[str, Any] | None = None
    if wim_path and parsed.get("is_iso9660"):
        # To inspect the WIM we need to extract it from the ISO.  7-Zip can do
        # this from a solid archive; if it is missing we just report the path.
        with tempfile.TemporaryDirectory() as tmp:
            staged = Path(tmp) / "boot.wim"
            if _extract_wim_from_iso(lab, path, wim_path, staged):
                wim = {
                    "iso_path": wim_path,
                    "bytes": staged.stat().st_size,
                    **(_wim_info(staged) or {}),
                }
            if wim is None:
                wim = {
                    "iso_path": wim_path,
                    "note": (
                        "boot.wim is present but 7z/xorriso and wimlib are "
                        "not available to extract and inspect it"
                    ),
                }

    result = {
        "iso": {
            "path": str(path),
            "sha256": _sha256(path),
            "bytes": path.stat().st_size,
        },
        "boot": {
            "iso9660": parsed.get("is_iso9660"),
            "bootable_bios": summary["bootable_bios"],
            "bootable_uefi": summary["bootable_uefi"],
            "hybrid": bool(parsed.get("hybrid")),
            "el_torito_ok": summary["el_torito_ok"],
            "warnings": summary["warnings"],
        },
        "wim": wim,
        "tree": parsed.get("tree"),
        "legal_notice": LEGAL_NOTICE,
    }
    print(json.dumps(result, sort_keys=True))


# --------------------------------------------------------------------------- #
# extract
# --------------------------------------------------------------------------- #

def _extract_iso(lab: Any, iso: Path, out: Path) -> str:
    """Extract a whole ISO into ``out``; return the tool that did it."""
    tool_7z = _find_tool("7z")
    if tool_7z:
        _run([tool_7z, "x", "-y", f"-o{out}", str(iso)], timeout=1800)
        return "7z"
    xorriso = _find_tool("xorriso")
    if xorriso:
        _run([
            xorriso, "-osirrox", "on", "-indev", str(iso),
            "-extract", "/", str(out),
        ], timeout=1800)
        return "xorriso"
    raise lab.LabError(
        "extracting an ISO needs 7z or xorriso; neither was found. Manual "
        "equivalent: '7z x -o<out> <iso>' or "
        "'xorriso -osirrox on -indev <iso> -extract / <out>'"
    )


def cmd_extract(lab: Any, args: Any) -> None:
    """Extract a PE ISO's file tree and, optionally, its boot.wim."""
    path = Path(os.path.expanduser(args.iso)).resolve()
    _require_file(lab, path)
    out = Path(os.path.expanduser(args.out)).resolve()
    out.mkdir(parents=True, exist_ok=True)

    tool = _extract_iso(lab, path, out)

    wim_applied: Any = False
    if args.wim:
        wim = _find_extracted_wim(out)
        if wim is not None:
            target = out / "wim"
            target.mkdir(exist_ok=True)
            wim_tool = _wim_apply(lab, wim, args.wim_index, target)
            wim_applied = {
                "wim": str(wim),
                "index": args.wim_index,
                "target": str(target),
                "tool": wim_tool,
            }

    print(json.dumps({
        "extracted_to": str(out),
        "extract_tool": tool,
        "wim_applied": wim_applied,
        "programs": _list_programs(out),
        "legal_notice": LEGAL_NOTICE,
    }, sort_keys=True))


# --------------------------------------------------------------------------- #
# build
# --------------------------------------------------------------------------- #

def _manual_build_hint(from_iso: Path, out: Path) -> str:
    return (
        "xorriso -indev "
        f"{from_iso} -outdev {out} -boot_image any replay "
        "-compliance no_emul_toc -padding included"
    )


def cmd_build(lab: Any, args: Any) -> None:
    """Rebuild a PE ISO with files added, removed, or a patched boot.wim."""
    _require_legal(lab, args)
    source = Path(os.path.expanduser(args.from_iso)).resolve()
    _require_file(lab, source, "source ISO")
    out = Path(os.path.expanduser(args.out)).resolve()

    xorriso = _find_tool("xorriso")
    if xorriso is None:
        raise lab.LabError(
            "pe build requires xorriso, which is not installed. Manual "
            f"equivalent: {_manual_build_hint(source, out)}"
        )

    adds = [Path(os.path.expanduser(p)).resolve() for p in (args.add or [])]
    for added in adds:
        if not added.is_dir():
            raise lab.LabError(f"--add expects a directory: {added}")
    excludes = [str(p) for p in (args.exclude or [])]

    maps: list[tuple[Path, str]] = []
    modifications: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        parsed = _parse_iso(source)
        wim_iso_path = _find_wim_in_tree(parsed.get("tree") or {})
        staged_wim = tmp_path / "boot.wim"

        if args.wim_overlay or args.wim_script:
            if not wim_iso_path:
                raise lab.LabError(
                    "no sources/boot.wim is visible in this ISO, so a WIM "
                    "overlay or script cannot be applied"
                )
            if not _extract_wim_from_iso(lab, source, wim_iso_path,
                                         staged_wim):
                raise lab.LabError(
                    f"could not extract {wim_iso_path} from the ISO; install "
                    "7z or xorriso, or extract it manually with "
                    f"'7z e -o<dir> {source} {wim_iso_path}'"
                )
            mount = tmp_path / "wim-mount"
            mount.mkdir()
            tool = _wim_mount_rw(lab, staged_wim, args.wim_index, mount)
            try:
                if args.wim_overlay:
                    overlay = Path(os.path.expanduser(args.wim_overlay))
                    if not overlay.is_dir():
                        raise lab.LabError(
                            f"--wim-overlay expects a directory: {overlay}"
                        )
                    shutil.copytree(overlay, mount, dirs_exist_ok=True)
                    modifications.append(
                        f"wim-overlay {overlay} -> {wim_iso_path} "
                        f"(index {args.wim_index})"
                    )
                if args.wim_script:
                    script = Path(os.path.expanduser(args.wim_script))
                    _require_file(lab, script, "WIM script")
                    _run(_script_argv(script), cwd=mount, timeout=1800)
                    modifications.append(
                        f"wim-script {script} inside {wim_iso_path} "
                        f"(index {args.wim_index})"
                    )
            except BaseException:
                # Never leave a mounted WIM behind, and never commit a
                # half-applied overlay.
                try:
                    _wim_unmount(lab, mount, tool, commit=False)
                except Exception:
                    pass
                raise
            _wim_unmount(lab, mount, tool, commit=True)
            maps.append((staged_wim, "/" + wim_iso_path.lstrip("/")))

        for added in adds:
            if args.add_to and len(adds) == 1:
                target = "/" + args.add_to.strip("/")
            else:
                base = args.add_to.strip("/") if args.add_to else ""
                target = "/" + posixpath.join(base, added.name).lstrip("/")
            maps.append((added, target))
            modifications.append(f"map {added} -> {target}")
        for excluded in excludes:
            modifications.append(f"remove {excluded}")

        argv = [xorriso, "-indev", str(source), "-outdev", str(out)]
        for local, iso_path in maps:
            argv += ["-map", str(local), iso_path]
        for excluded in excludes:
            argv += ["-rm_r", "/" + excluded.strip("/")]
        argv += [
            "-boot_image", "any", "replay",
            "-compliance", "no_emul_toc",
            "-padding", "included",
        ]
        _run(argv, timeout=3600)

    print(json.dumps({
        "output": str(out),
        "modifications": modifications,
        "legal_notice": LEGAL_NOTICE,
    }, sort_keys=True))


# --------------------------------------------------------------------------- #
# boot
# --------------------------------------------------------------------------- #

def _upload_iso(lab: Any, lease_id: str, path: Path, storage: str) -> str:
    """Upload a user-supplied ISO and return its Proxmox volume ID."""
    namespace = argparse.Namespace(
        lease=lease_id,
        storage=storage,
        content="iso",
        file=str(path),
        timeout=600,
        task_timeout=600,
    )
    lab.cmd_upload(namespace)
    return f"{storage}:iso/{path.name}"


def cmd_boot(lab: Any, args: Any) -> None:
    """Upload a PE ISO and boot a lease-owned QEMU guest from it."""
    _require_legal(lab, args)
    path = Path(os.path.expanduser(args.iso)).resolve()
    _require_file(lab, path)

    api = lab.ProxmoxAPI()
    lease = lab.load_lease(args.lease)
    if args.vmid in lease["initial_vmids"]:
        raise lab.LabError(f"VMID {args.vmid} existed before this lease")
    name = args.name or f"pe-lab-{args.vmid}"

    volume = _upload_iso(lab, args.lease, path, args.iso_storage)

    firmware = args.firmware
    if firmware == "auto":
        parsed = _parse_iso(path)
        et = parsed.get("el_torito") or {}
        firmware = "ovmf" if et.get("has_uefi_boot") else "seabios"
    machine = "q35" if firmware == "ovmf" else "pc-i440fx"

    payload: dict[str, Any] = {
        "vmid": args.vmid,
        "name": name,
        "memory": args.memory,
        "cores": args.cores,
        "cpu": "host",
        "net0": (
            f"{args.network_model},bridge={lab.CONFIG.network.lab_bridge}"
        ),
        "scsi0": f"{args.storage}:{args.disk_size},ssd=1",
        "ide2": f"{volume},media=cdrom",
        "boot": "order=ide2;scsi0",
        "scsihw": "virtio-scsi-single",
        "bios": firmware,
        "machine": machine,
        "ostype": "win10",
        "onboot": 0,
        "agent": 0,
        "tags": f"codex-lab;lease-{args.lease};pe",
    }
    if firmware == "ovmf":
        payload["efidisk0"] = f"{args.storage}:1"
    create_upid = api.call("POST", f"/nodes/{lab.NODE}/qemu", payload)
    lab.wait_task(api, create_upid, timeout=300)
    lab.register_resource(lease, "qemu", args.vmid, "delete", name)

    result: dict[str, Any] = {
        "vmid": args.vmid,
        "name": name,
        "firmware": firmware,
        "machine": machine,
        "volume": volume,
        "iso": str(path),
        "started": False,
    }
    if args.start:
        start_upid = api.call(
            "POST", f"/nodes/{lab.NODE}/qemu/{args.vmid}/status/start"
        )
        lab.wait_task(api, start_upid, timeout=120)
        result["started"] = True
        if args.boot_key:
            # The UEFI "press any key to boot from CD" window is short; tap
            # Enter across it. Best-effort, bounded, never fails the boot.
            result["boot_key_taps"] = windows_module._tap_boot_prompt(
                lab, api, args.vmid
            )
    lab.audit(
        "pe-boot",
        lease=args.lease,
        vmid=args.vmid,
        iso=path.name,
        firmware=firmware,
    )
    result["next"] = [
        f"watch: proxmox-lab console screenshot --vmid {args.vmid}",
        "send the boot-menu key if it stalls: proxmox-lab console keys "
        f"--lease {args.lease} --vmid {args.vmid} enter",
        f"lease-end when finished: proxmox-lab lease-end --lease {args.lease}",
    ]
    print(json.dumps(result, indent=2, sort_keys=True))


# --------------------------------------------------------------------------- #
# registration
# --------------------------------------------------------------------------- #

def register(sub: Any, lab: Any) -> None:
    pe = sub.add_parser(
        "pe",
        help="inspect, extract, rebuild and boot a user-supplied "
             "Windows PE ISO",
    )
    pe_sub = pe.add_subparsers(dest="pe_command", required=True)

    catalog = pe_sub.add_parser(
        "catalog",
        help="report a PE ISO's boot record, boot.wim and tool tree",
    )
    catalog.add_argument("--iso", required=True, help="path to the PE ISO")
    catalog.add_argument(
        "--read-bytes", type=int, default=_READ_BYTES,
        help="how much of the ISO to read for the boot record (default: "
             "2 MiB)",
    )
    catalog.set_defaults(func=lambda args: cmd_catalog(lab, args))

    extract = pe_sub.add_parser(
        "extract",
        help="extract a PE ISO's file tree and optionally apply its boot.wim",
    )
    extract.add_argument("--iso", required=True, help="path to the PE ISO")
    extract.add_argument("--out", required=True,
                         help="directory to extract into")
    extract.add_argument(
        "--wim", action="store_true",
        help="also apply sources/boot.wim into <out>/wim",
    )
    extract.add_argument("--wim-index", type=int, default=1)
    extract.set_defaults(func=lambda args: cmd_extract(lab, args))

    build = pe_sub.add_parser(
        "build",
        help="rebuild a PE ISO with files added, removed, or a patched "
             "boot.wim (xorriso required)",
    )
    build.add_argument("--from-iso", required=True,
                       help="source PE ISO (user-supplied)")
    build.add_argument("--out", required=True, help="output ISO path")
    build.add_argument(
        "--add", action="append", default=[], metavar="DIR",
        help="add a directory's contents to the ISO (repeatable)",
    )
    build.add_argument(
        "--add-to",
        help="ISO path for --add: with a single --add it is the exact "
             "target, otherwise each directory lands under it",
    )
    build.add_argument(
        "--exclude", action="append", default=[], metavar="ISO-PATH",
        help="remove a path from the ISO (repeatable)",
    )
    build.add_argument(
        "--wim-overlay",
        help="directory whose contents are copied into the mounted "
             "boot.wim before it is committed back",
    )
    build.add_argument(
        "--wim-script",
        help="script run inside the mounted boot.wim before committing",
    )
    build.add_argument("--wim-index", type=int, default=1)
    build.add_argument(
        "--legal-accepted", action="store_true",
        help="confirm you accept responsibility for the license terms of "
             "the supplied media",
    )
    build.set_defaults(func=lambda args: cmd_build(lab, args))

    boot = pe_sub.add_parser(
        "boot",
        help="upload a PE ISO and boot a lease-owned QEMU guest from it",
    )
    boot.add_argument("--lease", required=True)
    boot.add_argument("--vmid", type=int, required=True)
    boot.add_argument("--iso", required=True, help="path to the PE ISO")
    boot.add_argument("--name")
    boot.add_argument("--memory", type=int, default=4096)
    boot.add_argument("--cores", type=int, default=2)
    boot.add_argument("--disk-size", type=int, default=8,
                      help="scratch disk size in GiB")
    boot.add_argument("--storage", default="local-lvm",
                      help="storage for the scratch disk and EFI disk")
    boot.add_argument(
        "--iso-storage", default=lab.DEFAULT_UPLOAD_STORAGE,
        help="upload target storage (default: %(default)s)",
    )
    boot.add_argument("--network-model", default="e1000")
    boot.add_argument(
        "--firmware", choices=("auto", "seabios", "ovmf"), default="auto",
        help="auto picks OVMF when the ISO has a UEFI boot entry",
    )
    boot.add_argument("--no-start", dest="start", action="store_false")
    boot.add_argument(
        "--no-boot-key", dest="boot_key", action="store_false",
        help="do not auto-tap Enter at the UEFI boot-from-CD prompt",
    )
    boot.add_argument(
        "--legal-accepted", action="store_true",
        help="confirm you accept responsibility for the license terms of "
             "the supplied media",
    )
    boot.set_defaults(func=lambda args: cmd_boot(lab, args))
