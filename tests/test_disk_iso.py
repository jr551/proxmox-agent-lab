"""Offline tests for the iso and disk command modules."""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock

SRC = Path(__file__).parents[1] / "src"
sys.path.insert(0, str(SRC))

from proxmox_agent_lab import disk as lab_disk  # noqa: E402
from proxmox_agent_lab import isoinspect  # noqa: E402
from proxmox_agent_lab import bootstruct as bs  # noqa: E402

# Reuse the structure synthesizers from the parser test module.
sys.path.insert(0, str(Path(__file__).parent))
from test_bootstruct import _iso, _mbr, _gpt, ESP_GUID, LINUX_GUID  # noqa: E402


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

    def test_bios_and_uefi_iso_is_ok(self) -> None:
        lab = _Lab()
        with tempfile.TemporaryDirectory() as tmp:
            iso = self._write_iso(tmp, volume_id="DEBIAN", uefi=True, hybrid=True)
            args = _args(lab, isoinspect.register,
                         "iso", "diagnose", "--path", iso)
            with mock.patch("builtins.print") as printed:
                isoinspect.cmd_diagnose(lab, args)
            payload = json.loads(printed.call_args[0][0])
        self.assertEqual(payload["volume_id"], "DEBIAN")
        self.assertTrue(payload["bootable_bios"])
        self.assertTrue(payload["bootable_uefi"])

    def test_bios_only_iso_warns_about_uefi(self) -> None:
        lab = _Lab()
        with tempfile.TemporaryDirectory() as tmp:
            iso = self._write_iso(tmp, uefi=False)
            args = _args(lab, isoinspect.register,
                         "iso", "diagnose", "--path", iso)
            with mock.patch("builtins.print") as printed:
                isoinspect.cmd_diagnose(lab, args)
            payload = json.loads(printed.call_args[0][0])
        self.assertFalse(payload["bootable_uefi"])
        self.assertTrue(any("UEFI" in w for w in payload["warnings"]))

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
