"""Offline tests for the iso and disk command modules."""

from __future__ import annotations

import os
from pathlib import Path

os.environ["PROXMOX_AGENT_LAB_CONFIG"] = str(
    Path(__file__).parent / "fixtures" / "config.toml"
)

import argparse  # noqa: E402
import base64  # noqa: E402
import json  # noqa: E402
import struct  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402
from unittest import mock  # noqa: E402

SRC = Path(__file__).parents[1] / "src"
sys.path.insert(0, str(SRC))

from proxmox_agent_lab import disk as lab_disk  # noqa: E402
from proxmox_agent_lab import isoinspect  # noqa: E402
from proxmox_agent_lab import bootstruct as bs  # noqa: E402

# Reuse the structure synthesizers from the parser test module.
sys.path.insert(0, str(Path(__file__).parent))
from test_bootstruct import (  # noqa: E402
    _iso, _mbr, _gpt, BIOS_IMAGE_LBA, DATA_TREE, ESP_GUID, LINUX_GUID,
)


class _Lab:
    LabError = RuntimeError
    NODE = "aipve"

    def __init__(self, api: mock.Mock | None = None) -> None:
        self._api = api or mock.Mock()
        self.audits: list[str] = []

    def ProxmoxAPI(self) -> mock.Mock:
        return self._api

    def load_lease(self, lease_id: str) -> dict:
        return {"id": lease_id}

    def audit(self, event: str, *, sync: bool = True, **f: object) -> None:
        self.audits.append(event)


def _args(lab: _Lab, register, *argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    register(parser.add_subparsers(), lab)
    return parser.parse_args(list(argv))


class IsoDiagnoseTests(unittest.TestCase):
    def _write_iso(self, tmp: str, **kw) -> str:
        path = Path(tmp) / "test.iso"
        path.write_bytes(_iso(**kw))
        return str(path)

    def _diagnose(self, **kw) -> dict:
        lab = _Lab()
        with tempfile.TemporaryDirectory() as tmp:
            iso = self._write_iso(tmp, **kw)
            args = _args(lab, isoinspect.register,
                         "iso", "diagnose", "--path", iso)
            with mock.patch("builtins.print") as printed:
                isoinspect.cmd_diagnose(lab, args)
            return json.loads(printed.call_args[0][0])

    def test_bios_and_uefi_iso_is_ok(self) -> None:
        payload = self._diagnose(volume_id="DEBIAN", uefi=True, hybrid=True)
        self.assertEqual(payload["volume_id"], "DEBIAN")
        self.assertTrue(payload["bootable_bios"])
        self.assertTrue(payload["bootable_uefi"])
        self.assertEqual(payload["warnings"], [])
        self.assertTrue(payload["ok"])

    def test_bios_only_iso_warns_about_uefi(self) -> None:
        payload = self._diagnose(uefi=False)
        self.assertFalse(payload["bootable_uefi"])
        self.assertTrue(any("UEFI" in w for w in payload["warnings"]))

    # ---- El Torito diagnosis for hand-reassembled media (issue #94) ------- #

    def test_well_formed_iso_reports_its_el_torito_entries(self) -> None:
        payload = self._diagnose(hybrid=True, media_type=0x00,
                                 bios_sectors=4, uefi_sectors=8)
        et = payload["el_torito"]
        self.assertTrue(payload["el_torito_ok"])
        self.assertTrue(et["catalog_usable"])
        self.assertTrue(et["validation"]["checksum_ok"])
        self.assertTrue(et["validation"]["key_bytes_ok"])
        bios, uefi = et["entries"]
        self.assertEqual(bios["kind"], "initial/default")
        self.assertEqual((bios["platform"], bios["platform_id"]),
                         ("x86-BIOS", "0x00"))
        self.assertEqual(bios["media_type_name"], "no-emulation")
        self.assertEqual(bios["load_segment"], "0x07C0")
        self.assertEqual(bios["load_sectors"], 4)
        self.assertEqual(bios["load_rba"], BIOS_IMAGE_LBA)
        self.assertTrue(bios["image_in_range"])
        self.assertEqual((uefi["kind"], uefi["platform"], uefi["platform_id"]),
                         ("section", "UEFI", "0xEF"))
        self.assertEqual(uefi["load_sectors"], 8)

    def test_corrupt_catalog_is_reported_as_unusable(self) -> None:
        payload = self._diagnose(hybrid=True, checksum=False)
        self.assertFalse(payload["el_torito_ok"])
        self.assertFalse(payload["el_torito"]["catalog_usable"])
        joined = " ".join(payload["warnings"])
        self.assertIn("the boot record is present but the catalog is unusable",
                      joined)
        self.assertIn("checksum", joined)
        # A useless catalog must not also be reported as simply BIOS-less;
        # the operator needs the actual reason, not a symptom.
        self.assertEqual(len(payload["warnings"]), 1)

    def test_catalog_with_no_key_bytes_is_reported_as_unusable(self) -> None:
        payload = self._diagnose(hybrid=True, key_bytes=False)
        self.assertFalse(payload["el_torito_ok"])
        self.assertIn("0x55 0xAA", " ".join(payload["warnings"]))

    def test_boot_image_past_end_of_image_is_reported(self) -> None:
        payload = self._diagnose(hybrid=True, uefi=False, bios_lba=90000)
        self.assertFalse(payload["el_torito_ok"])
        joined = " ".join(payload["warnings"])
        self.assertIn("boot image LBA 90000", joined)
        self.assertIn("past the end", joined)

    def test_zero_boot_load_size_is_reported(self) -> None:
        payload = self._diagnose(hybrid=True, uefi=False, bios_sectors=0)
        self.assertFalse(payload["el_torito_ok"])
        joined = " ".join(payload["warnings"])
        self.assertIn("boot-load-size of 0 sectors", joined)
        self.assertIn("-boot-load-size 4", joined)

    def test_data_only_iso_with_boot_tree_names_the_mkisofs_flags(self) -> None:
        # Exactly the plain-mkisofs re-run: the installer's file tree is all
        # there, the boot record is not, and nothing said so.
        payload = self._diagnose(hybrid=True, el_torito=False)
        self.assertFalse(payload["el_torito_ok"])
        self.assertFalse(payload["bootable_bios"])
        joined = " ".join(payload["warnings"])
        self.assertIn("no El Torito boot record at all", joined)
        self.assertIn("LOADER/ISOBTRT.BIN", joined)
        self.assertIn(
            "-eltorito-platform x86 -eltorito-boot loader/isobtrt.bin "
            "-no-emul-boot -boot-load-size 4", joined)

    def test_genuine_data_iso_is_not_told_to_add_a_boot_record(self) -> None:
        payload = self._diagnose(hybrid=True, el_torito=False, tree=DATA_TREE)
        joined = " ".join(payload["warnings"])
        self.assertIn("data-only", joined)
        self.assertNotIn("mkisofs", joined)

    def test_missing_file_is_reported(self) -> None:
        lab = _Lab()
        args = _args(lab, isoinspect.register,
                     "iso", "diagnose", "--path", "/no/such.iso")
        with self.assertRaisesRegex(RuntimeError, "no such ISO"):
            isoinspect.cmd_diagnose(lab, args)


class DiskBootInfoTests(unittest.TestCase):
    def test_local_gpt_image_is_parsed(self) -> None:
        lab = _Lab()
        with tempfile.TemporaryDirectory() as tmp:
            img = Path(tmp) / "disk.img"
            img.write_bytes(_gpt([(ESP_GUID, 2048, 4096, "esp"),
                                  (LINUX_GUID, 4097, 90000, "root")]))
            args = _args(lab, lab_disk.register,
                         "disk", "boot-info", "--image", str(img))
            with mock.patch("builtins.print") as printed:
                lab_disk.cmd_boot_info(lab, args)
            payload = json.loads(printed.call_args[0][0])
        self.assertEqual(payload["scheme"], "gpt")
        self.assertEqual(payload["gpt"]["partitions"][0]["type_name"],
                         "EFI System (ESP)")

    def test_stopped_guest_disk_is_read_over_host(self) -> None:
        api = mock.Mock()
        api.call.return_value = {"status": "stopped"}
        lab = _Lab(api)
        sectors = base64.b64encode(_mbr([(0x80, 0x83, 2048, 5000)])).decode()
        args = _args(lab, lab_disk.register,
                     "disk", "boot-info", "--vmid", "9001", "--lease", "L")
        with mock.patch.object(lab_disk, "_run_host", return_value=sectors), \
             mock.patch("builtins.print") as printed:
            lab_disk.cmd_boot_info(lab, args)
            payload = json.loads(printed.call_args[0][0])
        self.assertEqual(payload["scheme"], "mbr")
        self.assertIn("disk-boot-info", lab.audits)

    def test_running_guest_is_refused(self) -> None:
        api = mock.Mock()
        api.call.return_value = {"status": "running"}
        lab = _Lab(api)
        args = _args(lab, lab_disk.register,
                     "disk", "boot-info", "--vmid", "9001", "--lease", "L")
        with self.assertRaisesRegex(RuntimeError, "not stopped"):
            lab_disk.cmd_boot_info(lab, args)


class DiskWriteGateTests(unittest.TestCase):
    def test_write_refuses_without_i_understand(self) -> None:
        lab = _Lab()
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "f"
            src.write_text("x")
            args = _args(lab, lab_disk.register, "disk", "write",
                         "--lease", "L", "--vmid", "9001",
                         "--src", str(src), "--dest", "/etc/f")
            with self.assertRaisesRegex(RuntimeError, "i-understand"):
                lab_disk.cmd_write(lab, args)

    def test_write_script_embeds_payload_in_quoted_heredoc(self) -> None:
        payload = base64.b64encode(b"hello world").decode()
        script = lab_disk._build_write_script(payload)
        self.assertIn("<<'PXLB64'", script)
        self.assertIn(payload, script)
        # vmid/mount/dest are positional, never interpolated into the script.
        self.assertIn('"${1:?vmid}"', script)
        self.assertIn("guestfish --rw", script)


if __name__ == "__main__":
    unittest.main()
