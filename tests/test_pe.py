"""Offline tests for the pe (user-supplied Windows PE ISO) command module.

Everything external is faked: ``shutil.which`` decides which tools exist and
``subprocess.run`` is a recording stub, so the suite is deterministic on any
host and never touches a real ISO toolchain or the Proxmox API.
"""

from __future__ import annotations

import os
from pathlib import Path

import sys  # noqa: E402

# Shared bootstrap: fixture configuration plus a per-process state directory,
# applied before any proxmox_agent_lab import. `support` sits beside this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import bootstrap  # noqa: E402,F401
import shutil
import tempfile

import argparse  # noqa: E402
import contextlib  # noqa: E402
import hashlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import unittest  # noqa: E402
from unittest import mock  # noqa: E402

SRC = Path(__file__).parents[1] / "src"
sys.path.insert(0, str(SRC))

from proxmox_agent_lab import cli as LAB  # noqa: E402
from proxmox_agent_lab import pe  # noqa: E402

# Reuse the structure synthesizers from the parser test module.
sys.path.insert(0, str(Path(__file__).parent))
from test_bootstruct import _iso  # noqa: E402


def _write_iso(directory: Path, name: str = "pe.iso", **kw) -> Path:
    path = directory / name
    path.write_bytes(_iso(**kw))
    return path


def _completed(argv, stdout: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(argv, returncode, stdout, "")


def _which(available: dict[str, str]):
    """A shutil.which side-effect honouring the tools the test installed."""
    def find(name: str):
        return available.get(name)
    return find


def _args(lab, *argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    pe.register(parser.add_subparsers(), lab)
    return parser.parse_args(list(argv))


def _stdout(fn, *a, **kw) -> str:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        fn(*a, **kw)
    return buffer.getvalue()


class _Lab:
    """Minimal lab facade: config, lease and API are all fake."""

    LabError = RuntimeError
    NODE = "testnode"
    DEFAULT_UPLOAD_STORAGE = "bulk"
    CONFIG = LAB.CONFIG

    def __init__(self, api=None, lease=None) -> None:
        self._api = api if api is not None else mock.Mock()
        self._lease = lease or {
            "id": "L1", "state": "active",
            "initial_vmids": [], "resources": [],
        }
        self.uploads = []
        self.registered = []
        self.audits = []

    def ProxmoxAPI(self):
        return self._api

    def load_lease(self, lease_id: str):
        return self._lease

    def cmd_upload(self, namespace) -> None:
        self.uploads.append(namespace)

    def register_resource(self, lease, kind, vmid, policy, name=None) -> None:
        self.registered.append(
            {"kind": kind, "vmid": vmid, "policy": policy, "name": name}
        )

    def wait_task(self, api, upid, timeout=180):
        return {"status": "stopped", "exitstatus": "OK"}

    def audit(self, event: str, **fields) -> None:
        self.audits.append(event)


class ParserTests(unittest.TestCase):
    def test_pe_subcommands_are_reachable(self) -> None:
        lab = _Lab()
        for argv in (
            ("pe", "catalog", "--iso", "x.iso"),
            ("pe", "extract", "--iso", "x.iso", "--out", "o"),
            ("pe", "build", "--from-iso", "a.iso", "--out", "b.iso",
             "--legal-accepted"),
            ("pe", "boot", "--lease", "L1", "--vmid", "9700", "--iso",
             "x.iso", "--legal-accepted"),
        ):
            args = _args(lab, *argv)
            self.assertTrue(callable(args.func))

    def test_registered_in_the_real_cli_parser(self) -> None:
        args = LAB.parser().parse_args(
            ["pe", "catalog", "--iso", "x.iso"]
        )
        self.assertTrue(callable(args.func))

    def test_recipe_is_registered(self) -> None:
        from proxmox_agent_lab import recipes

        self.assertIn("pe", recipes.RECIPES)
        self.assertIn("pe boot", json.dumps(recipes.RECIPES["pe"]))


class CatalogTests(unittest.TestCase):
    def _catalog(self, iso_path: Path, available, run_side_effect=None):
        lab = _Lab()
        args = _args(lab, "pe", "catalog", "--iso", str(iso_path))
        with mock.patch("shutil.which", side_effect=_which(available)), \
             mock.patch("subprocess.run", side_effect=run_side_effect), \
             mock.patch("builtins.print") as printed:
            pe.cmd_catalog(lab, args)
        return json.loads(printed.call_args[0][0])

    def test_reports_boot_record_hash_and_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            iso = _write_iso(Path(tmp), tree={"SOURCES": ["BOOT.WIM"]},
                             uefi=True, hybrid=True)
            expected_sha = hashlib.sha256(iso.read_bytes()).hexdigest()
            payload = self._catalog(iso, {})  # no external tools at all
        self.assertEqual(payload["iso"]["sha256"], expected_sha)
        self.assertTrue(payload["boot"]["iso9660"])
        self.assertTrue(payload["boot"]["bootable_bios"])
        self.assertTrue(payload["boot"]["bootable_uefi"])
        self.assertTrue(payload["boot"]["hybrid"])
        self.assertTrue(payload["boot"]["el_torito_ok"])
        self.assertEqual(payload["wim"]["iso_path"], "sources/boot.wim")
        self.assertIn("not available", payload["wim"]["note"])
        self.assertIn("SOURCES/", payload["tree"]["root_entries"])
        self.assertEqual(payload["legal_notice"], pe.LEGAL_NOTICE)

    def test_inspects_boot_wim_when_7z_and_wimlib_exist(self) -> None:
        wim_info = (
            "WIM Information:\n"
            "Image Count: 1\n"
            "Index: 1\n"
            "Name: Windows PE\n"
            "Description: synthetic\n"
        )

        def fake_run(argv, **kw):
            if "7z" in argv[0]:
                out_dir = Path(
                    next(a[2:] for a in argv if a.startswith("-o"))
                )
                (out_dir / "boot.wim").write_bytes(b"synthetic-wim")
                return _completed(argv)
            return _completed(argv, stdout=wim_info)

        with tempfile.TemporaryDirectory() as tmp:
            iso = _write_iso(Path(tmp), tree={"SOURCES": ["BOOT.WIM"]})
            payload = self._catalog(
                iso,
                {"7z": "/usr/bin/7z", "wimlib-imagex": "/usr/bin/wimlib-imagex"},
                fake_run,
            )
        self.assertEqual(payload["wim"]["iso_path"], "sources/boot.wim")
        self.assertEqual(payload["wim"]["bytes"], len(b"synthetic-wim"))
        self.assertEqual(payload["wim"]["images"][0]["index"], 1)
        self.assertEqual(payload["wim"]["images"][0]["name"], "Windows PE")

    def test_missing_iso_is_a_lab_error(self) -> None:
        lab = _Lab()
        args = _args(lab, "pe", "catalog", "--iso", "/no/such/pe.iso")
        with self.assertRaisesRegex(RuntimeError, "no such ISO file"):
            pe.cmd_catalog(lab, args)


class ExtractTests(unittest.TestCase):
    def _extract(self, lab, iso: Path, out: Path, available, fake_run,
                 *extra: str):
        args = _args(lab, "pe", "extract", "--iso", str(iso),
                     "--out", str(out), *extra)
        with mock.patch("shutil.which", side_effect=_which(available)), \
             mock.patch("subprocess.run", side_effect=fake_run), \
             mock.patch("builtins.print") as printed:
            pe.cmd_extract(lab, args)
        return json.loads(printed.call_args[0][0])

    def test_extracts_with_7z_and_applies_the_wim(self) -> None:
        def fake_run(argv, **kw):
            if "7z" in argv[0]:
                out_dir = Path(
                    next(a[2:] for a in argv if a.startswith("-o"))
                )
                (out_dir / "sources").mkdir(parents=True, exist_ok=True)
                (out_dir / "sources" / "boot.wim").write_bytes(b"wim")
                (out_dir / "HBCD" / "Tools").mkdir(parents=True)
                return _completed(argv)
            if "wimlib" in argv[0] and argv[1] == "apply":
                Path(argv[-1]).mkdir(parents=True, exist_ok=True)
                (Path(argv[-1]) / "Windows").mkdir()
                return _completed(argv)
            return _completed(argv)

        lab = _Lab()
        with tempfile.TemporaryDirectory() as tmp:
            iso = _write_iso(Path(tmp))
            out = Path(tmp) / "tree"
            payload = self._extract(
                lab, iso, out,
                {"7z": "/usr/bin/7z",
                 "wimlib-imagex": "/usr/bin/wimlib-imagex"},
                fake_run, "--wim",
            )
        self.assertEqual(payload["extracted_to"], str(out.resolve()))
        self.assertEqual(payload["extract_tool"], "7z")
        self.assertTrue(payload["wim_applied"])
        self.assertEqual(payload["wim_applied"]["index"], 1)
        self.assertEqual(payload["wim_applied"]["tool"], "wimlib-imagex")
        self.assertIn("sources", payload["programs"]["root_dirs"])
        self.assertEqual(
            payload["programs"]["tool_categories"]["HBCD"], ["Tools"])
        self.assertEqual(payload["legal_notice"], pe.LEGAL_NOTICE)

    def test_falls_back_to_xorriso_when_7z_is_missing(self) -> None:
        calls = []

        def fake_run(argv, **kw):
            calls.append(list(argv))
            if "xorriso" in argv[0] and "-extract" in argv:
                out_dir = Path(argv[-1])
                out_dir.mkdir(parents=True, exist_ok=True)
            return _completed(argv)

        lab = _Lab()
        with tempfile.TemporaryDirectory() as tmp:
            iso = _write_iso(Path(tmp))
            out = Path(tmp) / "tree"
            payload = self._extract(
                lab, iso, out, {"xorriso": "/usr/bin/xorriso"}, fake_run)
        self.assertEqual(payload["extract_tool"], "xorriso")
        self.assertIn("-osirrox", calls[0])
        self.assertFalse(payload["wim_applied"])

    def test_no_extract_tool_is_a_clear_error(self) -> None:
        lab = _Lab()
        with tempfile.TemporaryDirectory() as tmp:
            iso = _write_iso(Path(tmp))
            out = Path(tmp) / "tree"
            args = _args(lab, "pe", "extract", "--iso", str(iso),
                         "--out", str(out))
            with mock.patch("shutil.which", return_value=None):
                with self.assertRaisesRegex(RuntimeError, "7z or xorriso"):
                    pe.cmd_extract(lab, args)

    def test_wim_apply_without_tools_is_a_clear_error(self) -> None:
        def fake_run(argv, **kw):
            out_dir = Path(next(a[2:] for a in argv if a.startswith("-o")))
            (out_dir / "sources").mkdir(parents=True, exist_ok=True)
            (out_dir / "sources" / "boot.wim").write_bytes(b"wim")
            return _completed(argv)

        lab = _Lab()
        with tempfile.TemporaryDirectory() as tmp:
            iso = _write_iso(Path(tmp))
            out = Path(tmp) / "tree"
            args = _args(lab, "pe", "extract", "--iso", str(iso),
                         "--out", str(out), "--wim")
            with mock.patch(
                "shutil.which",
                side_effect=_which({"7z": "/usr/bin/7z"}),
            ), mock.patch("subprocess.run", side_effect=fake_run):
                with self.assertRaisesRegex(
                    RuntimeError, "wimlib-imagex.*dism"
                ):
                    pe.cmd_extract(lab, args)


class BuildTests(unittest.TestCase):
    def _build(self, lab, available, fake_run, *argv: str):
        args = _args(lab, "pe", "build", *argv)
        with mock.patch("shutil.which", side_effect=_which(available)), \
             mock.patch("subprocess.run", side_effect=fake_run), \
             mock.patch("builtins.print") as printed:
            pe.cmd_build(lab, args)
        if printed.call_args is None:
            return None
        return json.loads(printed.call_args[0][0])

    def test_requires_legal_acceptance(self) -> None:
        lab = _Lab()
        with tempfile.TemporaryDirectory() as tmp:
            iso = _write_iso(Path(tmp))
            args = _args(lab, "pe", "build", "--from-iso", str(iso),
                         "--out", str(Path(tmp) / "out.iso"))
            with self.assertRaisesRegex(RuntimeError, "--legal-accepted"):
                pe.cmd_build(lab, args)

    def test_requires_xorriso_and_names_the_manual_command(self) -> None:
        lab = _Lab()
        with tempfile.TemporaryDirectory() as tmp:
            iso = _write_iso(Path(tmp))
            args = _args(lab, "pe", "build", "--from-iso", str(iso),
                         "--out", str(Path(tmp) / "out.iso"),
                         "--legal-accepted")
            with mock.patch("shutil.which", return_value=None):
                with self.assertRaisesRegex(RuntimeError, "xorriso") as caught:
                    pe.cmd_build(lab, args)
            self.assertIn("-boot_image any replay", str(caught.exception))

    def test_maps_adds_and_removes_and_replays_the_boot_record(self) -> None:
        calls = []

        def fake_run(argv, **kw):
            calls.append(list(argv))
            return _completed(argv)

        lab = _Lab()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            iso = _write_iso(root, tree={"SOURCES": ["BOOT.WIM"]})
            extra = root / "mytools"
            (extra / "tool.txt").parent.mkdir(parents=True)
            (extra / "tool.txt").write_text("x")
            out = root / "out.iso"
            payload = self._build(
                lab, {"xorriso": "/usr/bin/xorriso"}, fake_run,
                "--from-iso", str(iso), "--out", str(out),
                "--legal-accepted",
                "--add", str(extra), "--exclude", "/HBCD/Junk",
            )
        final = calls[-1]
        self.assertEqual(final[1:5],
                         ["-indev", str(iso.resolve()), "-outdev",
                          str(out.resolve())])
        self.assertIn("-map", final)
        idx = final.index("-map")
        self.assertEqual(final[idx + 2], "/mytools")
        self.assertIn("-rm_r", final)
        self.assertIn("/HBCD/Junk", final)
        for flag in ("-boot_image", "any", "replay", "-compliance",
                     "no_emul_toc", "-padding", "included"):
            self.assertIn(flag, final)
        self.assertEqual(payload["output"], str(out.resolve()))
        self.assertTrue(any("map" in m for m in payload["modifications"]))
        self.assertTrue(any("remove" in m for m in payload["modifications"]))

    def test_wim_overlay_mounts_copies_commits_and_remaps(self) -> None:
        calls = []
        mounted: dict[str, Path] = {}
        copied: dict[str, object] = {}
        real_copytree = shutil.copytree

        def spy_copytree(src, dst, *a, **kw):
            # copytree recurses through the patched module-level name, so
            # only the top-level call -- the one carrying dirs_exist_ok as a
            # keyword -- is the overlay copy we are checking. The mount point
            # lives in the command's own tempdir, which is gone by return;
            # verify the copy while it still exists.
            result = real_copytree(src, dst, *a, **kw)
            if kw.get("dirs_exist_ok"):
                copied["dst"] = Path(dst)
                copied["landed"] = (
                    Path(dst) / "Drivers" / "x.inf").is_file()
            return result

        def fake_run(argv, **kw):
            calls.append((list(argv), kw.get("cwd")))
            head = Path(argv[0]).name.lower()
            if head == "7z":
                out_dir = Path(
                    next(a[2:] for a in argv if a.startswith("-o"))
                )
                (out_dir / "boot.wim").write_bytes(b"wim")
            elif "wimlib" in head and argv[1] == "mountrw":
                mounted["dir"] = Path(argv[-1])
            return _completed(argv)

        lab = _Lab()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            iso = _write_iso(root, tree={"SOURCES": ["BOOT.WIM"]})
            overlay = root / "overlay"
            (overlay / "Drivers").mkdir(parents=True)
            (overlay / "Drivers" / "x.inf").write_text("x")
            out = root / "out.iso"
            args = _args(
                lab, "pe", "build", "--from-iso", str(iso),
                "--out", str(out), "--legal-accepted",
                "--wim-overlay", str(overlay),
            )
            with mock.patch(
                "shutil.which",
                side_effect=_which({
                    "xorriso": "/usr/bin/xorriso", "7z": "/usr/bin/7z",
                    "wimlib-imagex": "/usr/bin/wimlib-imagex",
                }),
            ), mock.patch("subprocess.run", side_effect=fake_run), \
                    mock.patch("shutil.copytree", side_effect=spy_copytree), \
                    mock.patch("builtins.print") as printed:
                pe.cmd_build(lab, args)
            payload = json.loads(printed.call_args[0][0])
        # The overlay really landed inside the mounted WIM's directory.
        self.assertEqual(copied["dst"], mounted["dir"])
        self.assertTrue(copied["landed"])
        verbs = [c[0][1] for c in calls if "wimlib" in Path(c[0][0]).name]
        self.assertEqual(verbs, ["mountrw", "unmount"])
        self.assertIn("--commit", calls[-2][0])
        final = calls[-1][0]
        idx = final.index("-map")
        self.assertEqual(final[idx + 2], "/sources/boot.wim")
        self.assertTrue(
            any("wim-overlay" in m for m in payload["modifications"]))

    def test_wim_work_falls_back_to_dism(self) -> None:
        calls = []

        def fake_run(argv, **kw):
            calls.append(list(argv))
            if "-extract" in argv:
                # xorriso -osirrox writes the ISO member to the disk path.
                Path(argv[-1]).write_bytes(b"wim")
            return _completed(argv)

        lab = _Lab()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            iso = _write_iso(root, tree={"SOURCES": ["BOOT.WIM"]})
            overlay = root / "overlay"
            overlay.mkdir()
            out = root / "out.iso"
            self._build(
                lab,
                {"xorriso": "/usr/bin/xorriso", "dism": "C:/Windows/dism.exe"},
                fake_run,
                "--from-iso", str(iso), "--out", str(out),
                "--legal-accepted", "--wim-overlay", str(overlay),
            )
        dism = [c for c in calls if "dism" in Path(c[0]).name.lower()]
        self.assertEqual(dism[0][1], "/Mount-Wim")
        self.assertEqual(dism[1][1], "/Unmount-Wim")
        self.assertIn("/Commit", dism[1])

    def test_wim_options_without_a_wim_are_refused(self) -> None:
        lab = _Lab()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # A data tree: no SOURCES directory at all.
            iso = _write_iso(root, tree={"DOCS": ["README.TXT"]})
            overlay = root / "overlay"
            overlay.mkdir()
            args = _args(
                lab, "pe", "build", "--from-iso", str(iso),
                "--out", str(root / "out.iso"), "--legal-accepted",
                "--wim-overlay", str(overlay),
            )
            with mock.patch(
                "shutil.which",
                side_effect=_which({"xorriso": "/usr/bin/xorriso"}),
            ), mock.patch(
                "subprocess.run", return_value=_completed([])
            ):
                with self.assertRaisesRegex(RuntimeError, "sources/boot.wim"):
                    pe.cmd_build(lab, args)


class BootTests(unittest.TestCase):
    def _boot_args(self, lab, iso: Path, *extra: str):
        return _args(
            lab, "pe", "boot", "--lease", "L1", "--vmid", "9700",
            "--iso", str(iso), "--legal-accepted", *extra,
        )

    def _boot(self, lab, iso: Path, *extra: str):
        args = self._boot_args(lab, iso, *extra)
        with mock.patch.object(
            pe.windows_module, "_tap_boot_prompt", return_value=3
        ) as taps, mock.patch("builtins.print") as printed:
            pe.cmd_boot(lab, args)
        return json.loads(printed.call_args[0][0]), taps

    def test_requires_legal_acceptance(self) -> None:
        lab = _Lab()
        with tempfile.TemporaryDirectory() as tmp:
            iso = _write_iso(Path(tmp))
            args = _args(lab, "pe", "boot", "--lease", "L1", "--vmid",
                         "9700", "--iso", str(iso))
            with self.assertRaisesRegex(RuntimeError, "--legal-accepted"):
                pe.cmd_boot(lab, args)

    def test_refuses_a_pre_existing_vmid(self) -> None:
        lab = _Lab(lease={
            "id": "L1", "state": "active",
            "initial_vmids": [9700], "resources": [],
        })
        with tempfile.TemporaryDirectory() as tmp:
            iso = _write_iso(Path(tmp))
            args = self._boot_args(lab, iso)
            with self.assertRaisesRegex(RuntimeError, "existed before"):
                pe.cmd_boot(lab, args)

    def test_uefi_iso_boots_ovmf_q35_with_efidisk(self) -> None:
        api = mock.Mock()
        api.call.return_value = "UPID:1"
        lab = _Lab(api=api)
        with tempfile.TemporaryDirectory() as tmp:
            iso = _write_iso(Path(tmp), uefi=True)
            payload, taps = self._boot(lab, iso)
        self.assertEqual(payload["firmware"], "ovmf")
        self.assertEqual(payload["machine"], "q35")
        self.assertEqual(payload["volume"], f"bulk:iso/{iso.name}")
        self.assertTrue(payload["started"])
        self.assertEqual(payload["boot_key_taps"], 3)
        taps.assert_called_once()

        # The ISO went through the normal upload path as iso content.
        self.assertEqual(lab.uploads[0].storage, "bulk")
        self.assertEqual(lab.uploads[0].content, "iso")
        self.assertEqual(lab.uploads[0].lease, "L1")

        create = api.call.call_args_list[0]
        self.assertEqual(create.args[:2],
                         ("POST", "/nodes/testnode/qemu"))
        vm = create.args[2]
        self.assertEqual(vm["vmid"], 9700)
        self.assertEqual(vm["bios"], "ovmf")
        self.assertEqual(vm["machine"], "q35")
        self.assertEqual(vm["efidisk0"], "local-lvm:1")
        self.assertEqual(vm["ide2"], f"bulk:iso/{iso.name},media=cdrom")
        self.assertEqual(vm["scsi0"], "local-lvm:8,ssd=1")
        self.assertEqual(vm["net0"], "e1000,bridge=vmbr1")
        self.assertEqual(vm["boot"], "order=ide2;scsi0")
        self.assertEqual(vm["tags"], "codex-lab;lease-L1;pe")
        self.assertEqual(vm["agent"], 0)
        self.assertEqual(vm["onboot"], 0)

        start = api.call.call_args_list[1]
        self.assertEqual(
            start.args[:2],
            ("POST", "/nodes/testnode/qemu/9700/status/start"))
        self.assertEqual(
            lab.registered[0],
            {"kind": "qemu", "vmid": 9700, "policy": "delete",
             "name": payload["name"]})
        self.assertIn("pe-boot", lab.audits)

    def test_bios_only_iso_boots_seabios_i440fx(self) -> None:
        api = mock.Mock()
        lab = _Lab(api=api)
        with tempfile.TemporaryDirectory() as tmp:
            iso = _write_iso(Path(tmp), uefi=False)
            payload, _ = self._boot(lab, iso, "--no-start")
        self.assertEqual(payload["firmware"], "seabios")
        self.assertEqual(payload["machine"], "pc-i440fx")
        self.assertFalse(payload["started"])
        vm = api.call.call_args_list[0].args[2]
        self.assertEqual(vm["bios"], "seabios")
        self.assertNotIn("efidisk0", vm)
        # --no-start: only the create POST happened.
        self.assertEqual(len(api.call.call_args_list), 1)

    def test_explicit_firmware_and_iso_storage_are_honoured(self) -> None:
        api = mock.Mock()
        lab = _Lab(api=api)
        with tempfile.TemporaryDirectory() as tmp:
            iso = _write_iso(Path(tmp), uefi=True)
            payload, _ = self._boot(
                lab, iso, "--firmware", "seabios", "--iso-storage", "local",
                "--name", "rescue", "--no-start", "--no-boot-key")
        self.assertEqual(payload["firmware"], "seabios")
        self.assertEqual(payload["name"], "rescue")
        self.assertEqual(payload["volume"], f"local:iso/{iso.name}")
        self.assertEqual(lab.uploads[0].storage, "local")


if __name__ == "__main__":
    unittest.main()
