# USB passthrough and traffic sniffing

## Purpose

Watch USB traffic or pass a physical device into a QEMU guest from the Proxmox host, with nothing installed in the guest. Sniffing is passive via `usbmon`; passthrough is a host change that moves the device from host to guest.

## Commands

All flags verified against `src/proxmox_agent_lab/usb.py:register()`.

| Command | Key flags (verified) | Example |
|---|---|---|
| `usb list` | — | `usb list` |
| `usb sniff --lease L --device VENDOR:PRODUCT|BUS-DEV --seconds 15 --count N --out FILE` | `--lease`, `--device`, `--seconds`, `--count`, `--out` | `usb sniff --lease "$L" --device 04e8:61b6 --seconds 15 --out ./cap.pcap` |
| `usb attach --lease L --vmid ID --device VENDOR:PRODUCT|BUS-DEV --host-change-authorized` | `--lease`, `--vmid`, `--device`, `--host-change-authorized` | `usb attach --lease "$L" --vmid 9001 --device 0d8c:0012 --host-change-authorized` |
| `usb detach --lease L --vmid ID --slot usb0..usb4 --host-change-authorized` | `--lease`, `--vmid`, `--slot`, `--host-change-authorized` | `usb detach --lease "$L" --vmid 9001 --slot usb0 --host-change-authorized` |

Quick examples:

```bash
proxmox-lab usb list
proxmox-lab usb sniff --lease "$L" --device 04e8:61b6 --seconds 15 --out ./cap.pcap
proxmox-lab usb sniff --lease "$L" --device 1-2 --seconds 15 --out ./cap.pcap
```

For driver development you often need to watch the USB traffic between a guest and a real device passed through to it. This module does that from the host, with nothing installed in the guest.

## How it works

When Proxmox passes a physical USB device to a QEMU guest (`usb-host`), QEMU claims it through usbfs — so the host's **`usbmon`** facility still sees every URB. `usb sniff` captures that traffic on the host with `tcpdump` and returns a standard **pcap** that Wireshark decodes as USB. It works whether the device is currently owned by the host or passed through to a guest.

Like memflow, the capture runs on the hypervisor as root, so it reuses the same opt-in **SSH channel** — it is off until `[memflow] enabled = true` and `ssh_host` are set (see [memflow.md](memflow.md)). Nothing is installed on the host beyond loading the stock `usbmon` module.

## List devices

```bash
proxmox-lab usb list
```

Shows every host USB device (bus, address, `vendor:product`, name) and which guests have a `usbN` passthrough configured. No lease or flags required — run this first to discover the device address for `sniff`/`attach`.

## Sniff traffic

```bash
proxmox-lab usb sniff --lease "$L" --device 04e8:61b6 --seconds 15 --out ./cap.pcap
# or address a specific unit by bus-dev:
proxmox-lab usb sniff --lease "$L" --device 1-2 --seconds 15 --out ./cap.pcap
```

Captures the whole bus the device sits on (usbmon is per-bus) for `--seconds` (or until `--count` packets), writes the pcap locally, and reports the device address plus a ready-made Wireshark display filter (`usb.device_address == N`) so you can narrow to just that device. Open the pcap in Wireshark to decode descriptors, control transfers, and bulk/interrupt data.

Typical driver-dev flow: pass the device to a guest, exercise the driver inside the guest, and sniff from the host in parallel — the capture sees exactly what crosses the wire.

## Passthrough management

Passing a physical device to a guest takes it away from the host, so it is a passthrough change, gated behind `--host-change-authorized` on top of the lease:

```bash
proxmox-lab usb attach --lease "$L" --vmid <id> --device 0d8c:0012 --host-change-authorized
proxmox-lab usb detach --lease "$L" --vmid <id> --slot usb0 --host-change-authorized
```

`attach` picks the next free `usbN` slot and addresses the device by `vendor:product` so it survives re-enumeration; it hotplugs into a running guest. `detach` removes the slot and returns the device to the host.

> **Never pass through a device backing active storage.** On this lab the USB hard drive is the `usb-bulk` storage — attaching it to a guest would pull the disk out from under the host. Sniffing it is safe (passive); attaching it is not.

## Safety gate

| Operation | Required flag | What it guards |
|---|---|---|
| `usb list` | none (read-only) | host USB enumeration |
| `usb sniff` | `--lease` + opt-in `[memflow]` SSH channel | passive capture on host via `usbmon`; pcap written only to local `--out`, never to ledger (fact only) |
| `usb attach` / `usb detach` | `--lease` + `--host-change-authorized` | host device passthrough: device moves from host to guest (or back); refuse unless the user's current request explicitly authorizes a device-passthrough change |

The SSH channel is the deliberately separate trust boundary described in [memflow.md](memflow.md) and [safety-policy.md](safety-policy.md): it reaches the host as root and stays off until `[memflow] enabled` and `ssh_host` are set. Never pass through a device backing active storage.

## Failure mode

- Without `[memflow] enabled` + `ssh_host`, `sniff`/`attach`/`detach` refuse — they share the memflow/netcap SSH boundary and will not use the API token.
- `attach` without `--host-change-authorized` is refused even with a valid lease — this is a host device-passthrough change, same category as host storage/networking.
- `sniff` captures the whole bus (`usbmon` is per-bus) and reports a `usb.device_address` filter — forgetting the filter shows other devices on the same bus, not a leak.
- Attaching a storage-backed USB device (e.g. the `usb-bulk` disk) succeeds at the API level but pulls the disk out from under the host; the guard is a policy warning plus a safety-policy invariant — refuse or stop if the user's request does not explicitly authorize device-passthrough for that category.

## See also

- [memflow.md](memflow.md) — the `[memflow]` SSH channel and the shared trust boundary
- [netcap.md](netcap.md) — same SSH channel for network capture / MITM
- [storage.md](storage.md) — `usb-bulk` storage that must never be passed through
- [safety-policy.md](safety-policy.md) — host-change and device-passthrough authorization

