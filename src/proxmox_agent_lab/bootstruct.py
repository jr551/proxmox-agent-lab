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

# El Torito platform ids and boot media (emulation) types.
_ET_PLATFORMS = {0x00: "x86-BIOS", 0x01: "PowerPC", 0x02: "Mac", 0xEF: "UEFI"}

_ET_MEDIA = {
    0x00: "no-emulation",
    0x01: "1.2M-floppy-emulation",
    0x02: "1.44M-floppy-emulation",
    0x03: "2.88M-floppy-emulation",
    0x04: "hard-disk-emulation",
}

# Directory and file names that only appear on media somebody intended to
# boot. Matched against the ISO 9660 tree's first two levels; they are what
# separates "a data ISO, as intended" from "an installer that lost its boot
# record" when there is no El Torito structure at all.
_BOOT_TREE_MARKERS = frozenset({
    "AMD64", "BOOT", "CASPER", "EFI", "GRUB", "I386", "IMAGES", "ISOLINUX",
    "LIVE", "LOADER", "REACTOS", "SOURCES", "SYSLINUX",
    "BOOTIA32.EFI", "BOOTMGR", "BOOTX64.EFI", "EFISYS.BIN", "ELTORITO.IMG",
    "FREELDR.SYS", "GRUB.CFG", "INITRD.IMG", "ISOBOOT.BIN", "ISOBTRT.BIN",
    "ISOLINUX.BIN", "SETUPLDR.SYS", "TXTSETUP.SIF", "VMLINUZ",
})

# The options a hand-run mkisofs/genisoimage must carry or the image comes out
# silently unbootable. Quoted verbatim in the warning so it can be pasted; the
# boot image path is build-specific.
ELTORITO_MKISOFS_FLAGS = (
    "-eltorito-platform x86 -eltorito-boot loader/isobtrt.bin "
    "-no-emul-boot -boot-load-size 4"
)

# Bounds on tree walking: a malformed image must not make the parser loop.
_MAX_TREE_DIRS = 32
_MAX_TREE_NAMES = 2048


def parse_iso(data: bytes, image_size: int | None = None) -> dict[str, Any]:
    """Diagnose an ISO 9660 image: volume identity and El Torito boot catalog.

    ``data`` must include at least the system area and volume descriptors
    (the first ~64 KiB), and enough beyond that to reach the boot catalog for
    El Torito decoding. Reports BIOS vs UEFI boot entries -- the usual reason a
    burned or dd'd installer only boots one firmware type.

    ``image_size`` is the size of the whole image in bytes when ``data`` is
    only its opening slice. Catalog and boot-image extents are range-checked
    against it, so an entry pointing past the end of the file is reported
    rather than silently believed.
    """
    result: dict[str, Any] = {
        "structure": "iso9660",
        "is_iso9660": False,
        "volume_id": None,
        "system_id": None,
        "el_torito": None,
        "tree": None,
        "hybrid": None,
        "errors": [],
    }
    if image_size is None or image_size < len(data):
        image_size = len(data)
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

    result["el_torito"] = _parse_el_torito(data, image_size)
    result["tree"] = _parse_iso_tree(data)
    return result


def _dir_records(data: bytes, lba: int,
                 length: int) -> list[tuple[str, bool, int, int]]:
    """Decode one ISO 9660 directory extent into (name, is_dir, lba, size)."""
    out: list[tuple[str, bool, int, int]] = []
    if lba <= 0 or length <= 0:
        return out
    pos = lba * _ISO_SECTOR
    end = min(pos + length, len(data))
    while pos < end:
        rec_len = data[pos]
        if rec_len == 0:
            # Records never straddle a logical sector; a zero length byte
            # means "nothing more in this one".
            pos = ((pos // _ISO_SECTOR) + 1) * _ISO_SECTOR
            continue
        if rec_len < 33 or pos + rec_len > end:
            break
        rec = data[pos:pos + rec_len]
        ext_lba = struct.unpack_from("<I", rec, 2)[0]
        size = struct.unpack_from("<I", rec, 10)[0]
        is_dir = bool(rec[25] & 0x02)
        name_len = rec[32]
        raw = rec[33:33 + name_len]
        pos += rec_len
        if name_len == 0 or (name_len == 1 and raw in (b"\x00", b"\x01")):
            continue  # the "." and ".." self/parent records
        name = raw.decode("ascii", "replace").split(";", 1)[0]
        out.append((name.rstrip(".").upper(), is_dir, ext_lba, size))
        if len(out) >= _MAX_TREE_NAMES:
            break
    return out


def _parse_iso_tree(data: bytes) -> dict[str, Any]:
    """List the first two levels of the ISO tree and spot bootloader files.

    An image whose boot record was dropped during a hand re-run of mkisofs
    still carries the bootloader it was supposed to load. Naming those files
    is what lets a caller say "this was meant to boot" about an image that no
    longer can.
    """
    tree: dict[str, Any] = {
        "root_entries": [],
        "entry_count": 0,
        "boot_markers": [],
        "looks_bootable": False,
    }
    # The PVD carries the root directory record at offset 156.
    root = data[16 * _ISO_SECTOR + 156:16 * _ISO_SECTOR + 190]
    if len(root) < 34:
        return tree
    root_lba = struct.unpack_from("<I", root, 2)[0]
    root_len = struct.unpack_from("<I", root, 10)[0]
    entries = _dir_records(data, root_lba, root_len)
    markers: set[str] = set()
    names: list[str] = []
    dirs_walked = 0
    for name, is_dir, lba, size in entries:
        names.append(name + "/" if is_dir else name)
        if name in _BOOT_TREE_MARKERS:
            markers.add(name)
        if not is_dir or dirs_walked >= _MAX_TREE_DIRS:
            continue
        dirs_walked += 1
        for sub, _is_dir, _lba, _size in _dir_records(data, lba, size):
            tree["entry_count"] += 1
            if sub in _BOOT_TREE_MARKERS:
                markers.add(f"{name}/{sub}")
    tree["entry_count"] += len(entries)
    tree["root_entries"] = names[:64]
    tree["boot_markers"] = sorted(markers)
    tree["looks_bootable"] = bool(markers)
    return tree


def _validation_checksum_ok(entry: bytes) -> bool:
    """El Torito validation entry: all 16 LE words must sum to 0 mod 2**16."""
    if len(entry) < 32:
        return False
    return sum(struct.unpack_from("<16H", entry, 0)) & 0xFFFF == 0


def _parse_el_torito(data: bytes, image_size: int) -> dict[str, Any]:
    """Parse the El Torito boot record and catalog (BIOS + UEFI entries).

    The catalog is reported as *usable* only when the boot record points
    somewhere real and the validation entry passes both its checksum and its
    0x55 0xAA key bytes -- the same checks the firmware makes before it will
    believe the catalog. A boot record whose catalog fails them boots nothing
    and says nothing about it.
    """
    et: dict[str, Any] = {
        "present": False,
        "catalog_lba": None,
        "catalog_usable": False,
        "validation": None,
        "entries": [],
        "has_bios_boot": False,
        "has_uefi_boot": False,
        "errors": [],
    }
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
    et["catalog_lba"] = catalog_sector
    cat_off = catalog_sector * _ISO_SECTOR
    if catalog_sector == 0:
        et["errors"].append(
            "boot record points at boot catalog LBA 0, which is the system "
            "area and cannot hold a catalog")
        return et
    if cat_off + 64 > image_size:
        et["errors"].append(
            f"boot catalog LBA {catalog_sector} lies past the end of the "
            f"{image_size}-byte image")
        return et
    if cat_off + 64 > len(data):
        et["errors"].append(
            f"boot catalog LBA {catalog_sector} lies beyond the bytes read; "
            "re-read with a larger --read-bytes to decode it")
        return et

    # Validation entry (32 bytes): header id 0x01, platform id at byte 1,
    # checksum at 28..29, and the 0x55 0xAA key bytes at 30..31.
    validation = data[cat_off:cat_off + 32]
    platform = validation[1]
    checksum_ok = _validation_checksum_ok(validation)
    key_ok = validation[30:32] == b"\x55\xAA"
    et["validation"] = {
        "header_id": f"0x{validation[0]:02X}",
        "platform_id": f"0x{platform:02X}",
        "platform": _ET_PLATFORMS.get(platform, "unknown"),
        "id_string": validation[4:28].decode(
            "ascii", "replace").rstrip("\x00 ") or None,
        "checksum": f"0x{struct.unpack_from('<H', validation, 28)[0]:04X}",
        "checksum_ok": checksum_ok,
        "key_bytes_ok": key_ok,
    }
    if validation[0] != 0x01:
        et["errors"].append(
            f"boot catalog validation entry has header id "
            f"0x{validation[0]:02X}, not 0x01")
        return et
    if not key_ok:
        et["errors"].append(
            "boot catalog validation entry is missing its 0x55 0xAA key bytes")
    if not checksum_ok:
        et["errors"].append(
            "boot catalog validation entry fails its 16-bit checksum")
    if not (key_ok and checksum_ok):
        return et
    et["catalog_usable"] = True

    def _entry(off: int, entry_platform: int, kind: str) -> dict[str, Any]:
        chunk = data[off:off + 32]
        boot_indicator = chunk[0]
        media = chunk[1]
        media_type = media & 0x0F
        load_segment = struct.unpack_from("<H", chunk, 2)[0]
        load_sectors = struct.unpack_from("<H", chunk, 6)[0]
        load_rba = struct.unpack_from("<I", chunk, 8)[0]
        image_offset = load_rba * _ISO_SECTOR
        image_bytes = load_sectors * 512
        return {
            "kind": kind,
            "platform_id": f"0x{entry_platform:02X}",
            "platform": _ET_PLATFORMS.get(entry_platform, "unknown"),
            "bootable": boot_indicator == 0x88,
            "boot_indicator": f"0x{boot_indicator:02X}",
            "media_type": media_type,
            "media_type_name": _ET_MEDIA.get(
                media_type, f"unknown (0x{media_type:02X})"),
            "media_flags": f"0x{media & 0xF0:02X}",
            # A load segment of 0 means the El Torito default, 0x7C0.
            "load_segment": f"0x{load_segment or 0x07C0:04X}",
            "load_segment_default": load_segment == 0,
            "system_type": f"0x{chunk[4]:02X}",
            "load_sectors": load_sectors,
            "load_rba": load_rba,
            "image_offset": image_offset,
            "image_bytes": image_bytes,
            "image_in_range": (
                load_rba > 0
                and image_offset + max(image_bytes, 1) <= image_size),
        }

    # Initial/default entry immediately follows the validation entry, and its
    # platform is the one declared in the validation entry.
    et["entries"].append(_entry(cat_off + 32, platform, "initial/default"))

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
        section_platform = header[1]
        section_entries = struct.unpack_from("<H", header, 2)[0]
        pos += 32
        for _ in range(max(section_entries, 0)):
            if pos + 32 > len(data):
                break
            et["entries"].append(_entry(pos, section_platform, "section"))
            pos += 32
        if header_id == 0x91:  # final section header
            break

    et["has_bios_boot"] = any(
        e["platform"] == "x86-BIOS" and e["bootable"] for e in et["entries"])
    et["has_uefi_boot"] = any(
        e["platform"] == "UEFI" and e["bootable"] for e in et["entries"])
    return et
