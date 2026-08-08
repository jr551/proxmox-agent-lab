# USB passthrough and traffic sniffing

For driver development you often need to watch the USB traffic between a guest
and a real device passed through to it. This module does that from the host,
with nothing installed in the guest.

## How it works

When Proxmox passes a physical USB device to a QEMU guest (`usb-host`), QEMU
claims it through usbfs — so the host's **`usbmon`** facility still sees every
URB. `usb sniff` captures that traffic on the host with `tcpdump` and returns a
standard **pcap** that Wireshark decodes as USB. It works whether the device is
currently owned by the host or passed through to a guest.

Like memflow, the capture runs on the hypervisor as root, so it reuses the same
opt-in **SSH channel** — it is off until `[memflow] enabled = true` and
`ssh_host` are set (see [docs/memflow.md](memflow.md)). Nothing is installed on
the host beyond loading the stock `usbmon` module.

## List devices

```bash
proxmox-lab usb list
```

Shows every host USB device (bus, address, `vendor:product`, name) and which
guests have a `usbN` passthrough configured.

## Sniff traffic

```bash
proxmox-lab usb sniff --lease "$L" --device 04e8:61b6 --seconds 15 --out ./cap.pcap
# or address a specific unit by bus-dev:
proxmox-lab usb sniff --lease "$L" --device 1-2 --seconds 15 --out ./cap.pcap
```

Captures the whole bus the device sits on (usbmon is per-bus) for `--seconds`
(or until `--count` packets), writes the pcap locally, and reports the device
address plus a ready-made Wireshark display filter
(`usb.device_address == N`) so you can narrow to just that device. Open the
pcap in Wireshark to decode descriptors, control transfers, and bulk/interrupt
data.

Typical driver-dev flow: pass the device to a guest, exercise the driver inside
the guest, and sniff from the host in parallel — the capture sees exactly what
crosses the wire.

## Passthrough management

Passing a physical device to a guest takes it away from the host, so it is a
passthrough change, gated behind `--host-change-authorized` on top of the lease:

```bash
proxmox-lab usb attach --lease "$L" --vmid <id> --device 0d8c:0012 --host-change-authorized
proxmox-lab usb detach --lease "$L" --vmid <id> --slot usb0
```

`attach` picks the next free `usbN` slot and addresses the device by
`vendor:product` so it survives re-enumeration; it hotplugs into a running
guest. `detach` removes the slot and returns the device to the host.

> **Never pass through a device backing active storage.** On this lab the USB
> hard drive is the `usb-bulk` storage — attaching it to a guest would pull the
> disk out from under the host. Sniffing it is safe (passive); attaching it is
> not.
