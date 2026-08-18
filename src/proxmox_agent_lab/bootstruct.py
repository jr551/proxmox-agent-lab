"""Pure parsers for on-disk boot structures: MBR, GPT and ISO 9660/El Torito.

These are the structures a machine reads before any OS is running, and the
usual reason an install ISO or a freshly imaged disk "won't boot". Every
function here is a pure decoder of raw bytes -- no host, no I/O -- so an agent
can point them at a local image, an ISO it just built, or sectors read out of
a stopped guest, and get the same answer. Callers layer the transport on top.

Nothing here raises on malformed input beyond what the caller can see: parsers
return best-effort structured results with an ``errors`` list, because a
half-written boot sector is exactly the case worth diagnosing rather than
crashing on.
"""

from __future__ import annotations

import struct
from typing import Any

SECTOR = 512

# MBR partition type bytes worth naming; the long tail is reported as hex.
_MBR_TYPES: dict[int, str] = {
    0x00: "empty",
    0x05: "extended-chs",
    0x07: "NTFS/exFAT/HPFS",
    0x0B: "FAT32-CHS",
    0x0C: "FAT32-LBA",
    0x0E: "FAT16-LBA",
    0x0F: "extended-lba",
    0x82: "Linux swap",
    0x83: "Linux",
    0x8E: "Linux LVM",
    0xA5: "FreeBSD",
    0xA6: "OpenBSD",
    0xA9: "NetBSD",
    0xEE: "GPT protective",
    0xEF: "EFI System (ESP)",
    0xFD: "Linux RAID",
}

# GPT partition-type GUIDs worth naming.
_GPT_TYPES: dict[str, str] = {
    "00000000-0000-0000-0000-000000000000": "unused",
    "C12A7328-F81F-11D2-BA4B-00A0C93EC93B": "EFI System (ESP)",
    "21686148-6449-6E6F-744E-656564454649": "BIOS boot",
    "E3C9E316-0B5C-4DB8-817D-F92DF00215AE": "Microsoft reserved",
    "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7": "Microsoft basic data",
    "DE94BBA4-06D1-4D40-A16A-BFD50179D6AC": "Windows recovery",
    "0FC63DAF-8483-4772-8E79-3D69D8477DE4": "Linux filesystem",
    "0657FD6D-A4AB-43C4-84E5-0933C84B4F4F": "Linux swap",
    "E6D6D379-F507-44C2-A23C-238F2A3DF928": "Linux LVM",
    "44479540-F297-41B2-9AF7-D131D5F0458A": "Linux root (x86)",
    "4F68BCE3-E8CD-4DB1-96E7-FBCAF984B709": "Linux root (x86-64)",
    "933AC7E1-2EB4-4F13-B844-0E14E2AEF915": "Linux /home",
    "516E7CB4-6ECF-11D6-8FF8-00022D09712B": "FreeBSD data",
    "48465300-0000-11AA-AA11-00306543ECAC": "Apple HFS+",
    "7C3457EF-0000-11AA-AA11-00306543ECAC": "Apple APFS",
}


def _guid(raw: bytes) -> str:
    """Format a 16-byte mixed-endian GUID as its canonical string."""
    if len(raw) != 16:
        return ""
    d1, d2, d3 = struct.unpack_from("<IHH", raw, 0)
    d4 = raw[8:10]
    d5 = raw[10:16]
    return (
        f"{d1:08X}-{d2:04X}-{d3:04X}-"
        f"{d4[0]:02X}{d4[1]:02X}-"
        + "".join(f"{b:02X}" for b in d5)
    )


def parse_mbr(data: bytes) -> dict[str, Any]:
    """Parse a legacy MBR (first 512 bytes). Detects a GPT protective MBR."""
    result: dict[str, Any] = {
        "structure": "mbr",
        "valid_signature": False,
        "protective": False,
        "partitions": [],
        "errors": [],
    }
    if len(data) < SECTOR:
        result["errors"].append(f"need {SECTOR} bytes, got {len(data)}")
        return result
    result["valid_signature"] = data[510:512] == b"\x55\xAA"
    if not result["valid_signature"]:
        result["errors"].append("missing 0x55AA boot signature")
    for i in range(4):
        entry = data[446 + i * 16: 446 + (i + 1) * 16]
        boot, ptype = entry[0], entry[4]
        start_lba, sectors = struct.unpack_from("<II", entry, 8)
        if ptype == 0x00 and start_lba == 0 and sectors == 0:
            continue
        result["partitions"].append({
            "index": i + 1,
            "bootable": boot == 0x80,
            "type": f"0x{ptype:02X}",
            "type_name": _MBR_TYPES.get(ptype, "unknown"),
            "start_lba": start_lba,
            "sectors": sectors,
        })
        if ptype == 0xEE:
            result["protective"] = True
    return result


def parse_gpt(data: bytes, sector_size: int = SECTOR) -> dict[str, Any]:
    """Parse a GPT header + entries. ``data`` starts at LBA0 (the disk start).

    Needs the protective MBR (LBA0), the GPT header (LBA1) and the entry array
    (usually LBA2+). Pass at least the first ~34 sectors.
    """
    result: dict[str, Any] = {
        "structure": "gpt",
        "valid_header": False,
        "partitions": [],
        "errors": [],
    }
    header_off = sector_size
    if len(data) < header_off + 92:
        result["errors"].append("not enough data for a GPT header")
        return result
    header = data[header_off:header_off + 92]
    if header[0:8] != b"EFI PART":
        result["errors"].append("no 'EFI PART' signature at LBA1")
        return result
    result["valid_header"] = True
    (revision, header_size, _hdr_crc, _rsvd, current_lba, backup_lba,
     first_usable, last_usable) = struct.unpack_from("<IIIIQQQQ", header, 8)
    disk_guid = _guid(header[56:72])
    (entries_lba, num_entries, entry_size, _entries_crc) = struct.unpack_from(
        "<QIII", header, 72)
    result.update({
        "revision": f"{revision >> 16}.{revision & 0xFFFF}",
        "disk_guid": disk_guid,
        "first_usable_lba": first_usable,
        "last_usable_lba": last_usable,
        "backup_lba": backup_lba,
        "entry_count": num_entries,
        "entry_size": entry_size,
    })
    if entry_size < 128 or entry_size > 4096:
        result["errors"].append(f"implausible entry size {entry_size}")
        return result
    base = entries_lba * sector_size
    for i in range(num_entries):
        off = base + i * entry_size
        if off + entry_size > len(data):
            result["errors"].append(
                f"entry array truncated after {i} of {num_entries} entries")
            break
        entry = data[off:off + entry_size]
        type_guid = _guid(entry[0:16])
        if type_guid == "00000000-0000-0000-0000-000000000000":
            continue
        first_lba, last_lba = struct.unpack_from("<QQ", entry, 32)
        attributes = struct.unpack_from("<Q", entry, 48)[0]
        name = entry[56:128].decode("utf-16-le", "replace").split("\x00", 1)[0]
        result["partitions"].append({
            "index": i + 1,
            "type_guid": type_guid,
            "type_name": _GPT_TYPES.get(type_guid, "unknown"),
            "unique_guid": _guid(entry[16:32]),
            "first_lba": first_lba,
            "last_lba": last_lba,
            "attributes": f"0x{attributes:016X}",
            "name": name,
        })
    return result


def parse_boot_sectors(data: bytes, sector_size: int = SECTOR) -> dict[str, Any]:
    """Parse whichever partition scheme a disk's opening sectors carry."""
    mbr = parse_mbr(data)
    out: dict[str, Any] = {"mbr": mbr}
    if mbr.get("protective") or (
        len(data) >= sector_size + 8
        and data[sector_size:sector_size + 8] == b"EFI PART"
    ):
        out["gpt"] = parse_gpt(data, sector_size)
        out["scheme"] = "gpt"
    else:
        out["scheme"] = "mbr" if mbr["partitions"] else "none"
    return out


# --------------------------------------------------------------------------- #
# ISO 9660 + El Torito (bootable-CD/USB image diagnosis).
# --------------------------------------------------------------------------- #

_ISO_SECTOR = 2048


def parse_iso(data: bytes) -> dict[str, Any]:
    """Diagnose an ISO 9660 image: volume identity and El Torito boot catalog.

    ``data`` must include at least the system area and volume descriptors
    (the first ~64 KiB), and enough beyond that to reach the boot catalog for
    El Torito decoding. Reports BIOS vs UEFI boot entries -- the usual reason a
    burned or dd'd installer only boots one firmware type.
    """
    result: dict[str, Any] = {
        "structure": "iso9660",
        "is_iso9660": False,
        "volume_id": None,
        "system_id": None,
        "el_torito": None,
        "hybrid": None,
        "errors": [],
    }
    # Primary Volume Descriptor lives at sector 16; "CD001" identifies ISO 9660.
    pvd_off = 16 * _ISO_SECTOR
    if len(data) < pvd_off + _ISO_SECTOR:
        result["errors"].append("too short to contain the volume descriptors")
        return result
    if data[pvd_off + 1:pvd_off + 6] != b"CD001":
        result["errors"].append("no CD001 identifier at sector 16")
        return result
    result["is_iso9660"] = True
    result["system_id"] = data[pvd_off + 8:pvd_off + 40].decode(
        "ascii", "replace").strip() or None
    result["volume_id"] = data[pvd_off + 40:pvd_off + 72].decode(
        "ascii", "replace").strip() or None

    # A hybrid ISO also carries an MBR (and often a GPT) so it boots off USB.
    hybrid = parse_mbr(data[:SECTOR]) if len(data) >= SECTOR else None
    if hybrid and hybrid["valid_signature"] and hybrid["partitions"]:
        result["hybrid"] = {
            "has_mbr": True,
            "partitions": hybrid["partitions"],
            "has_gpt": len(data) >= SECTOR + 8
            and data[SECTOR:SECTOR + 8] == b"EFI PART",
        }

    result["el_torito"] = _parse_el_torito(data)
    return result


def _parse_el_torito(data: bytes) -> dict[str, Any]:
    """Parse the El Torito boot record and catalog (BIOS + UEFI entries)."""
    et: dict[str, Any] = {"present": False, "entries": [], "errors": []}
    # Boot Record Volume Descriptor at sector 17.
    brvd_off = 17 * _ISO_SECTOR
    if len(data) < brvd_off + _ISO_SECTOR:
        return et
    brvd = data[brvd_off:brvd_off + _ISO_SECTOR]
    if brvd[0] != 0x00 or brvd[1:6] != b"CD001":
        return et
    if brvd[7:7 + 23].rstrip(b"\x00") != b"EL TORITO SPECIFICATION":
        return et
    et["present"] = True
    catalog_sector = struct.unpack_from("<I", brvd, 71)[0]
    cat_off = catalog_sector * _ISO_SECTOR
    if len(data) < cat_off + 64:
        et["errors"].append("boot catalog beyond supplied data")
        return et

    # Validation entry (32 bytes): header id 0x01, platform id at byte 1.
    validation = data[cat_off:cat_off + 32]
    if validation[0] != 0x01:
        et["errors"].append("boot catalog validation entry malformed")
        return et
    _PLATFORMS = {0x00: "x86-BIOS", 0x01: "PowerPC", 0x02: "Mac", 0xEF: "UEFI"}

    def _entry(off: int, platform: int, kind: str) -> dict[str, Any]:
        chunk = data[off:off + 32]
        boot_indicator = chunk[0]
        media_type = chunk[1]
        load_sectors = struct.unpack_from("<H", chunk, 6)[0]
        load_rba = struct.unpack_from("<I", chunk, 8)[0]
        return {
            "kind": kind,
            "platform_id": f"0x{platform:02X}",
            "platform": _PLATFORMS.get(platform, "unknown"),
            "bootable": boot_indicator == 0x88,
            "media_type": media_type,
            "load_sectors": load_sectors,
            "load_rba": load_rba,
        }

    # Initial/default entry immediately follows the validation entry, and its
    # platform is the one declared in the validation entry.
    et["entries"].append(_entry(
        cat_off + 32, validation[1], "initial/default"))

    # Section headers (id 0x90/0x91) introduce further platforms -- this is
    # where the UEFI entry lives on a modern hybrid installer.
    pos = cat_off + 64
    guard = 0
    while pos + 32 <= len(data) and guard < 64:
        guard += 1
        header = data[pos:pos + 32]
        header_id = header[0]
        if header_id not in (0x90, 0x91):
            break
        platform = header[1]
        section_entries = struct.unpack_from("<H", header, 2)[0]
        pos += 32
        for _ in range(max(section_entries, 0)):
            if pos + 32 > len(data):
                break
            et["entries"].append(_entry(pos, platform, "section"))
            pos += 32
        if header_id == 0x91:  # final section header
            break

    et["has_bios_boot"] = any(
        e["platform"] == "x86-BIOS" and e["bootable"] for e in et["entries"])
    et["has_uefi_boot"] = any(
        e["platform"] == "UEFI" and e["bootable"] for e in et["entries"])
    return et
