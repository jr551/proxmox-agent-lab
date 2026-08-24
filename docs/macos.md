# 🍎 macOS guests on the lab (OSX-PROXMOX)

The lab can host **macOS VMs** the same way it hosts Linux and Windows guests,
by using [OSX-PROXMOX](https://github.com/luchina-gabriel/OSX-PROXMOX) to build
the OpenCore + recovery boot media on the Proxmox host. The lab then treats the
resulting macOS VM like any other guest: leases, screenshots, `guest run`,
push/pull, VPN egress, all of it.

> ⚠️ Apple's macOS licence permits running the OS on Apple hardware only.
> This recipe is for development and testing on hardware you own; check your
> local terms before use.

## One-time host preparation (host change)

The OSX-PROXMOX installer mutates the hypervisor: it adds repositories, builds
OpenCore, downloads recovery images, and patches QEMU args. That is exactly
what the lab's `--host-change-authorized` gate exists for — run it deliberately,
once, with the user's explicit OK:

```bash
# on the Proxmox host (SSH as root), NOT via proxmox-lab:
/bin/bash -c "$(curl -fsSL https://install.osx-proxmox.com)"
```

The interactive menu lets you pick a macOS version (High Sierra → Sequoia),
CPU vendor profile, cores/RAM/disk, storage and bridge. It creates the VM for
you; you can also just build the ISOs and create the VM yourself (below).

### TSC requirement (Monterey 12+)

Since Monterey the host needs a working TSC or macOS crashes under more than
one vCPU. Check before committing hours to an install:

```bash
dmesg | grep -i -e tsc -e clocksource
cat /sys/devices/system/clocksource/clocksource0/current_clocksource
```

`clocksource: Switched to clocksource tsc` is what you want; `tsc: Marking TSC
unstable` means fix BIOS C-states/ErP first, or add `clocksource=tsc
tsc=reliable` to the host GRUB line.

## Creating the guest yourself

If you prefer to drive creation through the lab's audited API passthrough
instead of OSX-PROXMOX's own `qm create`, the essential shape of its VM is:

```bash
L=$(proxmox-lab lease-begin --purpose "macos guest" \
    | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
trap 'proxmox-lab lease-end --lease "$L"' EXIT

# OpenCore ISO + recovery image live in /var/lib/vz/template/iso on the host
# after the OSX-PROXMOX setup ran there once.
proxmox-lab api --lease "$L" --method POST --path /nodes/$NODE/qemu/ \
  --data vmid=9300 --data name=macos-dev \
  --data agent=1 --data bios=ovmf --data machine=q35 \
  --data cores=4 --data memory=8192 \
  --data efidisk0=local-lvm:4 \
  --data net0=vmxnet3,bridge=vmbr0 \
  --data ostype=other --data vga=vmware \
  --data scsihw=virtio-scsi-pci \
  --data sata0=local-lvm:64,cache=none,discard=on \
  --data ide0=local:iso/opencore-osx-proxmox-vm.iso,media=disk \
  --data ide2=local:iso/recovery-sequoia.iso,media=cdrom \
  --wait-task
proxmox-lab lease-register --lease "$L" --kind qemu --vmid 9300
```

Two details matter and are easy to miss:

- The IDE disks carrying OpenCore must be `media=disk`, not `media=cdrom`
  (OSX-PROXMOX rewrites this after `qm create`; if you create via the API,
  write `media=disk` directly).
- Sonoma/Sequoia need the extra QEMU args (`qemu-xhci`, `usb-kbd`,
  `usb-tablet`, `nec-usb-xhci.msi=off`) plus an `-cpu` block from the
  OSX-PROXMOX script; pass them via `--data args=...`. Without them the
  installer boots to a black screen or panics early.

## The look → act loop works unchanged

Once the VM reaches the Recovery/desktop GUI, drive it like any other screen:

```bash
proxmox-lab console screenshot --vmid 9300     # Disk Utility / installer progress
proxmox-lab console click --lease "$L" --vmid 9300 --x 512 --y 384
proxmox-lab console keys  --lease "$L" --vmid 9300 enter
```

Install macOS onto the virtual disk through the GUI (Disk Utility first:
erase as APFS), then let it reboot into the installed system.

### After install: SSH beats pixels

Enable Remote Login in System Settings (or paste a one-liner over `console text`
if you set that up) and from then on prefer real channels over screenshots:

```bash
proxmox-lab push --lease "$L" --vmid 9300 --file ./tool.tar.gz --dest /tmp/
proxmox-lab guest run --lease "$L" --vmid 9300 -- sw_vers && uname -m
```

`pull` artifacts back out with `--out ./result.tgz` — always a concrete file
path, never a directory (see [RECIPES.md](RECIPES.md) recipe 3).

## Gotchas specific to macOS guests

| Symptom | Cause / fix |
|---|---|
| Black screen at boot | Missing `media=disk` on the OpenCore IDE entry, or missing USB device args for Sonoma+ |
| Installer says "recovery server could not be contacted" | Old macOS bug; see the OSX-PROXMOX README HTTP-fix section |
| Multi-vCPU crash / clock skew | Host TSC broken — see the check above |
| Serial console shows nothing useful | Expected: macOS has no serial getty. Use screenshots + SSH |
| Guest agent never answers | qemu-guest-agent is not part of macOS. Use `console screenshot/click/type` and SSH inside the guest instead of `guest probe`'s agent channel |
| Performance feels poor | vmxnet3 NIC and virtio-scsi are correct; make sure `balloon=0` and the CPU flags from the OSX-PROXMOX script are present |

## What not to do

- Do not point `net leak-test` at the macOS guest expecting serial output —
  it drives a serial login, which macOS does not have. Verify egress from
  Terminal inside the guest.
- Long-term leases work fine (`--long-term`) if you want a persistent Mac dev
  box; remember they keep the host powered until `lease-destroy --confirm`.
