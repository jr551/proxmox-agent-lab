# Site notes (example)

Copy to `docs/site-notes.md` and describe **your** lab. That file is
gitignored: it names your hardware and addresses, which is exactly the sort of
thing you do not want in a public repository.

An agent reads this to avoid rediscovering your setup on every run. Keep it
short and factual, and record when you last checked it.

---

Verified: `2026-01-01`.

| Item | Value |
|---|---|
| Physical machine | Dell OptiPlex under the stairs |
| Proxmox node | `pve` |
| API | `https://192.168.1.50:8006` |
| Proxmox version | 9.x |
| CPU / RAM | 8 threads, 32 GiB |
| Wired NIC MAC | `aa:bb:cc:dd:ee:ff` (Wake-on-LAN armed) |
| Storage | `local` (100 GB), `local-lvm` (400 GB) |
| Bulk disk | none |

## Guest templates

| VMID | Name | What it is |
|---|---|---|
| `9000` | `tpl-debian-13` | Debian 13 cloud-init, boot-verified |
| `9001` | `tpl-ubuntu-2404` | Ubuntu 24.04 cloud-init |

Record for each template: the source URL, the published checksum you verified
before importing, the cloud-init user, and whether a clone has actually been
booted. A template nobody has booted is a guess.

## Access

The API token is `agent@pve!lab` with `PVEVMAdmin` on `/vms`. Privilege
separation is on, so the **token** holds the role, not just the user.

Note anything the token deliberately cannot do — that list is a feature, and
future-you will want to know it was a choice rather than an oversight.

## Quirks worth remembering

Things that cost you an hour once:

- Which display each template uses. VNC keyboard input reaches a guest only
  with a graphical display (`vga: std`); on `vga: serial0` you get a picture
  but keystrokes go nowhere — drive those over the serial console instead.
- Whether your cloud images ship `qemu-guest-agent` (Debian and Ubuntu generic
  images do not).
- Anything your router does to broadcasts, if Wake-on-LAN is unreliable.
