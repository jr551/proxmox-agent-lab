# `pe` — inspect, extract, rebuild and boot a user-supplied Windows PE ISO

The `pe` subcommands operate on a **user-supplied** Windows PE ISO — for
example a lawfully obtained WinPE-based rescue image such as Hiren's BootCD
PE. They cover four jobs:

1. `pe catalog` — read the ISO locally: El Torito boot record (BIOS/UEFI/
   hybrid), the `sources/boot.wim` image list, and the visible tool tree.
2. `pe extract` — unpack the ISO to a directory, and optionally apply
   `sources/boot.wim` into `<out>/wim`.
3. `pe build` — produce a customised ISO: add or remove files, and overlay or
   script changes into `boot.wim` before it is committed back.
4. `pe boot` — upload the ISO to Proxmox ISO storage and boot a lease-owned
   QEMU guest from it.

## Legal and liability

> This tool operates on **user-supplied** media. It does not contain,
> download, or redistribute any copyrighted material — no Hiren's, Windows,
> or third-party tool files are stored in or shipped by this repository.
> **The operator is solely responsible for complying with the license terms
> of the source ISO and any tools bundled in it**, including whether
> modification (`pe build`) and use (`pe boot`) are permitted.

Because of that:

- `pe catalog` and `pe extract` always print a `legal_notice` field in their
  JSON output.
- `pe build` and `pe boot` — the commands that modify media or put it into
  service — require `--legal-accepted` and refuse to run without it.

## External tool requirements

Nothing is bundled; each command degrades to a clear error naming the missing
tool and the equivalent manual command line.

| Task | Tool |
|---|---|
| Read the boot record / tree | none (pure-Python `bootstruct` parser) |
| Extract the ISO | `7z` (7-Zip) **or** `xorriso -osirrox` |
| Inspect `boot.wim` | `wimlib-imagex info` (after extraction via 7z/xorriso) |
| Apply `boot.wim` | `wimlib-imagex apply` (Linux) or `dism /Apply-Image` (Windows) |
| Modify `boot.wim` | `wimlib-imagex mountrw`/`unmount --commit` (Linux) or `dism /Mount-Wim`/`/Unmount-Wim /Commit` (Windows) |
| Rebuild the ISO | `xorriso` (required by `pe build`) |

Debian/Ubuntu: `apt install p7zip-full xorriso wimtools`. Windows: install
7-Zip; `dism.exe` ships with the OS; `wimlib-imagex` is optional.

## Usage

### `pe catalog`

```
proxmox-lab pe catalog --iso /path/to/pe.iso [--read-bytes 67108864]
```

The default `--read-bytes` is 64 MiB, which covers the El Torito boot catalog on
large PE images such as Hiren's BootCD PE.  Smaller images will be read to
their actual size.

Prints one compact JSON object:

- `iso` — path, size, and SHA-256 (record this; it is how you prove which
  media was booted).
- `boot` — `iso9660`, `bootable_bios`, `bootable_uefi`, `hybrid`,
  `el_torito_ok`, and `warnings` (shared with `iso diagnose`, so the wording
  is identical).
- `wim` — `iso_path`, size and the `wimlib-imagex info` image list when the
  tools are available, otherwise a note saying what is missing.
- `tree` — the first two ISO tree levels plus detected boot markers.
- `legal_notice`.

### `pe extract`

```
proxmox-lab pe extract --iso /path/to/pe.iso --out /tmp/pe [--wim] [--wim-index 1]
```

Extracts the whole tree with `7z x` (preferred) or `xorriso -osirrox on`.
With `--wim`, if `sources/boot.wim` exists it is applied to `<out>/wim` via
`wimlib-imagex apply` or `dism /Apply-Image`. Output JSON reports
`extracted_to`, `extract_tool`, `wim_applied`, and `programs` — the
top-level directories plus tool categories found under `HBCD`, `Programs`,
`PROGRAMS`, or `Program Files`.

### `pe build`

```
proxmox-lab pe build --from-iso /path/to/pe.iso --out /tmp/pe-custom.iso \
    --legal-accepted \
    [--add <dir> ...] [--add-to /ISO-PATH] \
    [--exclude /ISO-PATH ...] \
    [--wim-overlay <dir>] [--wim-script <script>] [--wim-index 1]
```

- Requires `xorriso`; without it the command fails and prints the equivalent
  manual `xorriso` invocation.
- `--add <dir>` maps a local directory into the ISO at `/<dir-basename>`, or
  at `--add-to` when a single directory is given (under `--add-to` when
  several are given).
- `--exclude <iso-path>` removes a path with `xorriso -rm_r`.
- `--wim-overlay <dir>` copies a directory's contents into the mounted
  `sources/boot.wim`; `--wim-script <script>` additionally runs a script
  with the mount as its working directory. The WIM is then unmounted with
  commit and mapped back into the ISO at its original path. A failure
  discards the mount rather than committing a half-applied change.
- The final image is written with
  `xorriso -indev <from> -outdev <out> -map … -rm_r …
  -boot_image any replay -compliance no_emul_toc -padding included`,
  which preserves the original El Torito boot record so the rebuilt ISO
  stays bootable on the same firmware types.

Output JSON: `output`, `modifications`, `legal_notice`.

### `pe boot`

```
proxmox-lab pe boot --lease <id> --vmid <id> --iso /path/to/pe.iso \
    --legal-accepted \
    [--name <name>] [--memory 4096] [--cores 2] [--disk-size 8] \
    [--storage local-lvm] [--iso-storage bulk] [--network-model e1000] \
    [--firmware auto|seabios|ovmf] [--no-start] [--no-boot-key]
```

- Loads the lease and refuses a `--vmid` that existed before it.
- Uploads the ISO to `--iso-storage` (default: the configured bulk upload
  storage) as `iso` content via the normal `upload` path.
- `--firmware auto` reads the ISO's El Torito catalog and picks `ovmf` when
  a UEFI boot entry exists, `seabios` otherwise.
- Creates the guest with `bios`, `machine` (`q35` for OVMF, `pc-i440fx` for
  SeaBIOS), `scsihw=virtio-scsi-single`, an 8 GiB scratch `scsi0` disk, the
  ISO on `ide2`, `boot=order=ide2;scsi0`, `cpu=host`, `ostype=win10`,
  `agent=0`, `onboot=0`, and tags `codex-lab;lease-<id>;pe`. OVMF also gets
  `efidisk0=<storage>:1`.
- Registers the guest to the lease, then (unless `--no-start`) starts it and
  taps Enter across the UEFI "press any key to boot from CD" window —
  bounded and best-effort; disable with `--no-boot-key`.

Output JSON: `vmid`, `name`, `firmware`, `machine`, `volume`, `iso`,
`started`, `boot_key_taps` when tapped, and `next` steps.

A PE environment has no qemu-guest-agent, so drive it with
`console screenshot`, `console inspect`, `console keys`, and
`console click`. The scratch disk is throwaway: anything the PE session
should keep must be copied out or written to network storage before
`lease-end` destroys the guest.
