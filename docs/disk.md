# Offline disk & boot debugging

Some failures happen before any OS is running, or when the guest will not boot
at all. These tools read the structures the firmware reads — partition tables,
the ESP, the ISO boot catalog — and let you repair a powered-off guest's
filesystem from outside it.

## Diagnose an install ISO — `iso diagnose`

Entirely local; needs no host or VM. Point it at an ISO you built or
downloaded:

```bash
proxmox-lab iso diagnose --path ~/isos/debian-12.iso
```

It reports the volume identity and, from the El Torito boot catalog, whether
the image has a bootable **BIOS/legacy** entry, a bootable **UEFI** entry, and
whether it is **hybrid** (carries an MBR/GPT so it can boot when written to a
USB/disk). The common silent failure — an installer that boots under SeaBIOS
but not OVMF because it has no UEFI El Torito entry — shows up as a warning.

The catalog itself is decoded, not merely counted: per entry you get the
platform, the boot media/emulation type, the load segment, the boot-load-size
and the boot image LBA, and `el_torito_ok` is true only when some entry would
actually hand control to a bootloader. Three ways an image can be silently
unbootable are reported by name — a boot record whose catalog fails its
validation checksum or `0x55 0xAA` key bytes, an entry pointing past the end of
the file or loading zero sectors, and a bootloader-shaped file tree with no
boot record over it at all, which is what a hand re-run of `mkisofs` without
its El Torito options produces. **Run this on every ISO you assembled or
repaired by hand before attaching it to a guest**; the failure looks like a
hung guest once it is running. See
[reactos.md](reactos.md#build-settings-that-break-the-labs-own-channels) for
the build workflow that produces such an image and the flags to rebuild with.

## Read partition tables — `disk boot-info`

Parses the MBR and GPT (partition types, GUIDs, the ESP, a GPT protective
MBR). From a local image:

```bash
proxmox-lab disk boot-info --image /path/to/disk.img
```

…or from a **stopped** guest's disk (over the memflow host channel):

```bash
proxmox-lab disk boot-info --lease "$L" --vmid 9001
```

## Offline filesystem access — `disk ls | read | write`

For a guest that will not boot, mount its filesystem from the host with
libguestfs and repair it. **The guest must be stopped** — touching a mounted
filesystem under a live kernel corrupts it — and these reuse the memflow
SSH-to-host trust boundary, so they need `[memflow] enabled` and a host
prepared once with:

```bash
proxmox-lab disk host-setup --host-change-authorized
```

Then:

```bash
proxmox-lab disk ls   --lease "$L" --vmid 9001 --mount /dev/sda1 --path /boot
proxmox-lab disk read --lease "$L" --vmid 9001 --mount /dev/sda1 --path /etc/fstab
proxmox-lab disk write --lease "$L" --vmid 9001 --mount /dev/sda1 \
  --src ./fixed-grub.cfg --dest /boot/grub/grub.cfg --i-understand
```

`write` mutates the guest's on-disk filesystem and is hard-gated behind
`--i-understand`: a wrong file or path can make the guest unbootable. The
uploaded bytes are never audited — only the fact, the mount and the
destination path.

> These host-backed operations run libguestfs on the Proxmox host. The
> client-side flow (guest-stopped gate, disk resolution, base64 round-trip,
> parsing) is covered by tests and validated end-to-end against a stubbed
> host; run `disk boot-info` against a real stopped guest to confirm your
> host's `disk host-setup` before relying on `write`.

## Is the running guest actually writing? — `guest disk-activity`

The commands above need the guest powered off. For a *running* guest the
question is usually the opposite one — "is this thing making progress or has it
hung?" — and the obvious answer, the Proxmox `diskwrite` counter, is not
reliable on its own. On a qcow2 image over directory-backed storage it has been
observed reading **0 bytes for an entire session** while the guest was
demonstrably writing.

```bash
proxmox-lab guest disk-activity --vmid 9001                 # counter only
proxmox-lab guest disk-activity --lease "$L" --vmid 9001 \
  --ground-truth --interval 10
```

It samples twice and reports the **change** across the interval — a cumulative
counter means nothing as a single reading — for up to three independent
signals:

| Signal | What it measures | Blind spot |
|---|---|---|
| `proxmox_diskwrite` | the cached counter, kept so you can watch it disagree | can sit at 0 on a writing guest |
| `qmp_blockstats` | QEMU's own block-layer counters, via `info blockstats` on the monitor endpoint; no SSH | the endpoint wants `Sys.Audit\|Sys.Modify`, which the `PVEVMAdmin` lab token does not have |
| `host_image_du` | `du --block-size=1` on the backing image, over the opt-in `[memflow]` host SSH channel | nothing to measure on LVM/ZFS; reports 0 for a guest rewriting blocks it already owns |

`du`, not `ls`: `ls` reports a sparse image's apparent size, so an untouched
100 GB qcow2 would look like 100 GB of I/O. Conversely qcow2 growth can be
metadata-only (L1 and refcount tables), so a growing file is not proof of guest
data either — which is exactly why all three signals are printed rather than
one being chosen.

The `disagreement` field is the point of the output. It names any signal that
saw nothing while another saw bytes; magnitudes are *expected* to differ and
are not reported as a conflict. A signal that is switched off or refused is
reported as unavailable and the rest of the measurement still happens.

`--ground-truth` drives the QEMU monitor and reads the guest's image file on
the host, the same two boundaries `console screenshot --via monitor` crosses,
so it requires `--lease` and a lease that owns the guest. The plain form is a
read-only status query and needs neither.
