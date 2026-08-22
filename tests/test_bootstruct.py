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


ISO_SECTORS = 64          # size of a synthesized image, in 2048-byte sectors
ROOT_DIR_LBA = 19         # root directory extent
CATALOG_LBA = 20          # El Torito boot catalog
BIOS_IMAGE_LBA = 22       # the BIOS boot image the catalog points at
UEFI_IMAGE_LBA = 24       # the UEFI boot image
SUBDIR_LBA = 30           # first subdirectory extent

# A tree that only exists on media somebody meant to boot, and one that does
# not: the difference decides whether a missing boot record is a defect or the
# intended shape of a data ISO.
BOOT_TREE = {"LOADER": ["ISOBTRT.BIN", "SETUPLDR.SYS"],
             "REACTOS": ["NTOSKRNL.EXE"]}
DATA_TREE = {"DOCS": ["README.TXT"], "ARCHIVE": []}


def _dir_record(name: str, is_dir: bool, lba: int, size: int) -> bytes:
    """One ISO 9660 directory record, padded to an even length."""
    raw = name.encode("ascii")
    length = 33 + len(raw)
    length += length % 2
    rec = bytearray(length)
    rec[0] = length
    struct.pack_into("<I", rec, 2, lba)
    struct.pack_into(">I", rec, 6, lba)
    struct.pack_into("<I", rec, 10, size)
    struct.pack_into(">I", rec, 14, size)
    rec[25] = 0x02 if is_dir else 0x00
    struct.pack_into("<H", rec, 28, 1)
    struct.pack_into(">H", rec, 30, 1)
    rec[32] = len(raw)
    rec[33:33 + len(raw)] = raw
    return bytes(rec)


def _validation_entry(platform=0x00, header_id=0x01, checksum=True,
                      key_bytes=True) -> bytes:
    """El Torito validation entry; the firmware checks can be broken here."""
    entry = bytearray(32)
    entry[0] = header_id
    entry[1] = platform
    entry[4:4 + 8] = b"PXLABTST"
    if key_bytes:
        entry[30], entry[31] = 0x55, 0xAA
    if checksum:
        total = sum(struct.unpack_from("<16H", entry, 0)) & 0xFFFF
        struct.pack_into("<H", entry, 28, (-total) & 0xFFFF)
    return bytes(entry)


def _boot_entry(bootable=True, media_type=0x00, load_segment=0,
                load_sectors=4, load_rba=BIOS_IMAGE_LBA) -> bytes:
    """El Torito initial/default or section entry."""
    entry = bytearray(32)
    entry[0] = 0x88 if bootable else 0x00
    entry[1] = media_type
    struct.pack_into("<H", entry, 2, load_segment)
    struct.pack_into("<H", entry, 6, load_sectors)
    struct.pack_into("<I", entry, 8, load_rba)
    return bytes(entry)


def _iso(volume_id="TESTISO", el_torito=True, uefi=True, hybrid=False,
         tree=None, catalog_lba=CATALOG_LBA, bios_lba=BIOS_IMAGE_LBA,
         uefi_lba=UEFI_IMAGE_LBA, bios_sectors=4, uefi_sectors=8,
         media_type=0x00, load_segment=0, header_id=0x01, checksum=True,
         key_bytes=True) -> bytes:
    total = ISO_SECTOR * ISO_SECTORS
    data = bytearray(total)
    # PVD at sector 16, volume descriptor set terminator at 18.
    pvd = 16 * ISO_SECTOR
    data[pvd] = 0x01
    data[pvd + 1:pvd + 6] = b"CD001"
    data[pvd + 6] = 0x01
    data[pvd + 8:pvd + 40] = b"LINUX".ljust(32)
    data[pvd + 40:pvd + 72] = volume_id.encode().ljust(32)
    data[18 * ISO_SECTOR] = 0xFF
    data[18 * ISO_SECTOR + 1:18 * ISO_SECTOR + 6] = b"CD001"

    # Root directory record inside the PVD, then the tree it points at.
    data[pvd + 156:pvd + 190] = _dir_record(
        "\x00", True, ROOT_DIR_LBA, ISO_SECTOR)
    root = bytearray()
    root += _dir_record("\x00", True, ROOT_DIR_LBA, ISO_SECTOR)
    root += _dir_record("\x01", True, ROOT_DIR_LBA, ISO_SECTOR)
    sub_lba = SUBDIR_LBA
    for name, children in (BOOT_TREE if tree is None else tree).items():
        root += _dir_record(name, True, sub_lba, ISO_SECTOR)
        sub = bytearray()
        sub += _dir_record("\x00", True, sub_lba, ISO_SECTOR)
        sub += _dir_record("\x01", True, ROOT_DIR_LBA, ISO_SECTOR)
        for child in children:
            sub += _dir_record(f"{child};1", False, 50, 512)
        data[sub_lba * ISO_SECTOR:sub_lba * ISO_SECTOR + len(sub)] = sub
        sub_lba += 1
    data[ROOT_DIR_LBA * ISO_SECTOR:
         ROOT_DIR_LBA * ISO_SECTOR + len(root)] = root

    if el_torito:
        brvd = 17 * ISO_SECTOR
        data[brvd] = 0x00
        data[brvd + 1:brvd + 6] = b"CD001"
        data[brvd + 6] = 0x01
        data[brvd + 7:brvd + 7 + 23] = b"EL TORITO SPECIFICATION"
        struct.pack_into("<I", data, brvd + 71, catalog_lba)
        cat = catalog_lba * ISO_SECTOR
        if cat + 128 <= total:
            data[cat:cat + 32] = _validation_entry(
                0x00, header_id=header_id, checksum=checksum,
                key_bytes=key_bytes)
            data[cat + 32:cat + 64] = _boot_entry(
                media_type=media_type, load_segment=load_segment,
                load_sectors=bios_sectors, load_rba=bios_lba)
            if uefi:
                # Final section header (0x91), platform UEFI, 1 entry.
                sh = cat + 64
                data[sh] = 0x91
                data[sh + 1] = 0xEF
                struct.pack_into("<H", data, sh + 2, 1)
                data[sh + 32:sh + 64] = _boot_entry(
                    load_sectors=uefi_sectors, load_rba=uefi_lba)
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

    def test_validation_entry_is_decoded_and_verified(self) -> None:
        et = bs.parse_iso(_iso())["el_torito"]
        self.assertTrue(et["catalog_usable"])
        self.assertEqual(et["catalog_lba"], CATALOG_LBA)
        self.assertEqual(et["validation"]["header_id"], "0x01")
        self.assertEqual(et["validation"]["platform"], "x86-BIOS")
        self.assertEqual(et["validation"]["id_string"], "PXLABTST")
        self.assertTrue(et["validation"]["checksum_ok"])
        self.assertTrue(et["validation"]["key_bytes_ok"])
        self.assertEqual(et["errors"], [])

    def test_entry_geometry_is_reported(self) -> None:
        et = bs.parse_iso(_iso(uefi=False, media_type=0x04,
                               load_segment=0x2000, bios_sectors=63))
        et = et["el_torito"]
        entry = et["entries"][0]
        self.assertEqual(entry["kind"], "initial/default")
        self.assertEqual(entry["platform_id"], "0x00")
        self.assertEqual(entry["media_type_name"], "hard-disk-emulation")
        self.assertEqual(entry["load_segment"], "0x2000")
        self.assertFalse(entry["load_segment_default"])
        self.assertEqual(entry["load_sectors"], 63)
        self.assertEqual(entry["load_rba"], BIOS_IMAGE_LBA)
        self.assertEqual(entry["image_offset"], BIOS_IMAGE_LBA * ISO_SECTOR)
        self.assertTrue(entry["image_in_range"])

    def test_default_load_segment_reads_as_0x7c0(self) -> None:
        entry = bs.parse_iso(_iso())["el_torito"]["entries"][0]
        self.assertEqual(entry["load_segment"], "0x07C0")
        self.assertTrue(entry["load_segment_default"])
        self.assertEqual(entry["media_type_name"], "no-emulation")

    def test_checksum_failure_makes_the_catalog_unusable(self) -> None:
        et = bs.parse_iso(_iso(checksum=False))["el_torito"]
        self.assertTrue(et["present"])
        self.assertFalse(et["catalog_usable"])
        self.assertFalse(et["validation"]["checksum_ok"])
        self.assertEqual(et["entries"], [])
        self.assertTrue(any("checksum" in e for e in et["errors"]))

    def test_missing_key_bytes_make_the_catalog_unusable(self) -> None:
        et = bs.parse_iso(_iso(key_bytes=False))["el_torito"]
        self.assertFalse(et["catalog_usable"])
        self.assertFalse(et["validation"]["key_bytes_ok"])
        self.assertTrue(any("0x55 0xAA" in e for e in et["errors"]))

    def test_wrong_validation_header_id_is_named(self) -> None:
        et = bs.parse_iso(_iso(header_id=0x02))["el_torito"]
        self.assertFalse(et["catalog_usable"])
        self.assertTrue(any("header id 0x02" in e for e in et["errors"]))

    def test_catalog_lba_past_the_end_of_the_image(self) -> None:
        et = bs.parse_iso(_iso(catalog_lba=90000))["el_torito"]
        self.assertTrue(et["present"])
        self.assertFalse(et["catalog_usable"])
        self.assertEqual(et["catalog_lba"], 90000)
        self.assertTrue(any("past the end" in e for e in et["errors"]))

    def test_catalog_lba_zero_is_rejected(self) -> None:
        et = bs.parse_iso(_iso(catalog_lba=0))["el_torito"]
        self.assertFalse(et["catalog_usable"])
        self.assertTrue(any("LBA 0" in e for e in et["errors"]))

    def test_catalog_beyond_the_bytes_read_is_distinguished(self) -> None:
        image = _iso()
        # Hand the parser a short slice but the real image size: the catalog
        # exists, it just was not read.
        et = bs.parse_iso(image[:19 * ISO_SECTOR], len(image))["el_torito"]
        self.assertTrue(et["present"])
        self.assertFalse(et["catalog_usable"])
        self.assertTrue(any("beyond the bytes read" in e for e in et["errors"]))

    def test_boot_image_past_the_end_of_the_image(self) -> None:
        et = bs.parse_iso(_iso(uefi=False, bios_lba=90000))["el_torito"]
        self.assertTrue(et["catalog_usable"])
        self.assertFalse(et["entries"][0]["image_in_range"])

    def test_bootloader_tree_is_recognized(self) -> None:
        tree = bs.parse_iso(_iso())["tree"]
        self.assertTrue(tree["looks_bootable"])
        self.assertIn("LOADER", tree["boot_markers"])
        self.assertIn("LOADER/ISOBTRT.BIN", tree["boot_markers"])
        self.assertIn("REACTOS", tree["boot_markers"])
        self.assertIn("LOADER/", tree["root_entries"])

    def test_data_tree_is_not_mistaken_for_boot_media(self) -> None:
        tree = bs.parse_iso(_iso(tree=DATA_TREE))["tree"]
        self.assertFalse(tree["looks_bootable"])
        self.assertEqual(tree["boot_markers"], [])
        self.assertIn("DOCS/", tree["root_entries"])


if __name__ == "__main__":
    unittest.main()
