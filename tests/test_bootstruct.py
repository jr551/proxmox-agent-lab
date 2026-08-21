"""Offline tests for MBR/GPT/ISO boot-structure parsers via synthesized data."""

from __future__ import annotations

from pathlib import Path
import struct
import sys
import unittest

SRC = Path(__file__).parents[1] / "src"
sys.path.insert(0, str(SRC))

from proxmox_agent_lab import bootstruct as bs  # noqa: E402

SECTOR = 512
ISO_SECTOR = 2048


def _mbr(partitions, signature=True) -> bytes:
    data = bytearray(SECTOR)
    for i, (boot, ptype, start, count) in enumerate(partitions):
        off = 446 + i * 16
        data[off] = boot
        data[off + 4] = ptype
        struct.pack_into("<II", data, off + 8, start, count)
    if signature:
        data[510:512] = b"\x55\xAA"
    return bytes(data)


def _guid_raw(s: str) -> bytes:
    hexpart = s.replace("-", "")
    d1 = int(hexpart[0:8], 16)
    d2 = int(hexpart[8:12], 16)
    d3 = int(hexpart[12:16], 16)
    d4 = bytes.fromhex(hexpart[16:20])
    d5 = bytes.fromhex(hexpart[20:32])
    return struct.pack("<IHH", d1, d2, d3) + d4 + d5


ESP_GUID = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"
LINUX_GUID = "0FC63DAF-8483-4772-8E79-3D69D8477DE4"


def _gpt(partitions) -> bytes:
    # LBA0 protective MBR, LBA1 header, LBA2 entries.
    data = bytearray(SECTOR * 40)
    data[0:SECTOR] = _mbr([(0x00, 0xEE, 1, 0xFFFFFFFF)])
    header = bytearray(92)
    header[0:8] = b"EFI PART"
    struct.pack_into("<IIII", header, 8, 0x00010000, 92, 0, 0)
    struct.pack_into("<QQ", header, 24, 1, 39)          # current/backup lba
    struct.pack_into("<QQ", header, 40, 34, 6)          # first/last usable
    header[56:72] = _guid_raw(LINUX_GUID)               # disk guid (any)
    struct.pack_into("<QIII", header, 72, 2, len(partitions), 128, 0)
    data[SECTOR:SECTOR + 92] = header
    base = 2 * SECTOR
    for i, (type_guid, first, last, name) in enumerate(partitions):
        off = base + i * 128
        data[off:off + 16] = _guid_raw(type_guid)
        data[off + 16:off + 32] = _guid_raw(LINUX_GUID)
        struct.pack_into("<QQ", data, off + 32, first, last)
        name_utf16 = name.encode("utf-16-le")
        data[off + 56:off + 56 + len(name_utf16)] = name_utf16
    return bytes(data)


class MbrTests(unittest.TestCase):
    def test_partitions_and_bootflag_are_parsed(self) -> None:
        raw = _mbr([(0x80, 0x83, 2048, 1000), (0x00, 0x82, 3048, 500)])
        out = bs.parse_mbr(raw)
        self.assertTrue(out["valid_signature"])
        self.assertFalse(out["protective"])
        self.assertEqual(len(out["partitions"]), 2)
        self.assertTrue(out["partitions"][0]["bootable"])
        self.assertEqual(out["partitions"][0]["type_name"], "Linux")
        self.assertEqual(out["partitions"][1]["type_name"], "Linux swap")

    def test_missing_signature_is_flagged(self) -> None:
        out = bs.parse_mbr(_mbr([], signature=False))
        self.assertFalse(out["valid_signature"])
        self.assertIn("signature", out["errors"][0])

    def test_protective_mbr_is_detected(self) -> None:
        out = bs.parse_mbr(_mbr([(0x00, 0xEE, 1, 0xFFFFFFFF)]))
        self.assertTrue(out["protective"])
        self.assertEqual(out["partitions"][0]["type_name"], "GPT protective")


class GptTests(unittest.TestCase):
    def test_header_and_named_partitions(self) -> None:
        raw = _gpt([
            (ESP_GUID, 2048, 206847, "EFI System Partition"),
            (LINUX_GUID, 206848, 2000000, "root"),
        ])
        out = bs.parse_gpt(raw)
        self.assertTrue(out["valid_header"])
        self.assertEqual(out["entry_count"], 2)
        self.assertEqual(out["partitions"][0]["type_name"], "EFI System (ESP)")
        self.assertEqual(out["partitions"][0]["name"], "EFI System Partition")
        self.assertEqual(out["partitions"][1]["type_name"], "Linux filesystem")

    def test_missing_efi_part_signature(self) -> None:
        out = bs.parse_gpt(bytes(SECTOR * 3))
        self.assertFalse(out["valid_header"])

    def test_parse_boot_sectors_routes_to_gpt(self) -> None:
        raw = _gpt([(ESP_GUID, 2048, 4096, "esp")])
        out = bs.parse_boot_sectors(raw)
        self.assertEqual(out["scheme"], "gpt")
        self.assertTrue(out["gpt"]["valid_header"])

    def test_parse_boot_sectors_routes_to_mbr(self) -> None:
        out = bs.parse_boot_sectors(_mbr([(0x80, 0x07, 2048, 1000)]))
        self.assertEqual(out["scheme"], "mbr")


def _iso(volume_id="TESTISO", el_torito=True, uefi=True, hybrid=False) -> bytes:
    total = ISO_SECTOR * 40
    data = bytearray(total)
    # PVD at sector 16.
    pvd = 16 * ISO_SECTOR
    data[pvd + 1:pvd + 6] = b"CD001"
    data[pvd + 8:pvd + 40] = b"LINUX".ljust(32)
    data[pvd + 40:pvd + 72] = volume_id.encode().ljust(32)
    if el_torito:
        brvd = 17 * ISO_SECTOR
        data[brvd] = 0x00
        data[brvd + 1:brvd + 6] = b"CD001"
        data[brvd + 7:brvd + 7 + 23] = b"EL TORITO SPECIFICATION"
        catalog_sector = 20
        struct.pack_into("<I", data, brvd + 71, catalog_sector)
        cat = catalog_sector * ISO_SECTOR
        # Validation entry: id 0x01, platform 0x00 (BIOS).
        data[cat] = 0x01
        data[cat + 1] = 0x00
        # Initial/default entry (BIOS), bootable 0x88.
        data[cat + 32] = 0x88
        data[cat + 32 + 1] = 0x00
        struct.pack_into("<H", data, cat + 32 + 6, 4)
        struct.pack_into("<I", data, cat + 32 + 8, 100)
        if uefi:
            # Final section header (0x91), platform UEFI, 1 entry.
            sh = cat + 64
            data[sh] = 0x91
            data[sh + 1] = 0xEF
            struct.pack_into("<H", data, sh + 2, 1)
            # Section entry: bootable UEFI.
            se = sh + 32
            data[se] = 0x88
            data[se + 1] = 0x00
            struct.pack_into("<I", data, se + 8, 300)
    if hybrid:
        data[0:SECTOR] = _mbr([(0x00, 0x00, 0, ISO_SECTOR // SECTOR)])
    return bytes(data)


class IsoTests(unittest.TestCase):
    def test_iso_identity_is_read(self) -> None:
        out = bs.parse_iso(_iso(volume_id="DEBIAN_12"))
        self.assertTrue(out["is_iso9660"])
        self.assertEqual(out["volume_id"], "DEBIAN_12")
        self.assertEqual(out["system_id"], "LINUX")

    def test_non_iso_is_reported(self) -> None:
        out = bs.parse_iso(bytes(ISO_SECTOR * 20))
        self.assertFalse(out["is_iso9660"])
        self.assertIn("CD001", out["errors"][0])

    def test_el_torito_bios_and_uefi_entries(self) -> None:
        out = bs.parse_iso(_iso(uefi=True))
        et = out["el_torito"]
        self.assertTrue(et["present"])
        self.assertTrue(et["has_bios_boot"])
        self.assertTrue(et["has_uefi_boot"])
        platforms = {e["platform"] for e in et["entries"]}
        self.assertEqual(platforms, {"x86-BIOS", "UEFI"})

    def test_bios_only_iso_lacks_uefi(self) -> None:
        out = bs.parse_iso(_iso(uefi=False))
        self.assertTrue(out["el_torito"]["has_bios_boot"])
        self.assertFalse(out["el_torito"]["has_uefi_boot"])

    def test_hybrid_iso_reports_usb_bootability(self) -> None:
        out = bs.parse_iso(_iso(hybrid=True))
        self.assertIsNotNone(out["hybrid"])
        self.assertTrue(out["hybrid"]["has_mbr"])


if __name__ == "__main__":
    unittest.main()
