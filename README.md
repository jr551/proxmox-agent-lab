# proxmox-agent-lab

**Disposable Proxmox guests over SSH. The lease cleans them up. Powering the host off is optional.**

[![CI](https://github.com/jr551/proxmox-agent-lab/actions/workflows/ci.yml/badge.svg)](https://github.com/jr551/proxmox-agent-lab/actions/workflows/ci.yml)
[![GitHub release](https://img.shields.io/github/v/release/jr551/proxmox-agent-lab)](https://github.com/jr551/proxmox-agent-lab/releases/latest)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

One machine, one Proxmox host, root SSH. Your key is the only credential.
Python 3.11+, no extra packages.

## Why this one

[ProxmoxMCP-Plus](https://github.com/RekklesNA/ProxmoxMCP-Plus) and [canvrno/ProxmoxMCP](https://github.com/canvrno/ProxmoxMCP) authenticate with a Proxmox API token and call the HTTPS API. This one does not. Your SSH key is the only credential, and every remote command is one argv on an allowlist. A lease owns each guest and deletes it when the lease ends. The host stays up unless `[power] auto_shutdown` is on. `guest create` refuses a disk larger than the free space on that store. Cleanup will not touch a guest without the `proxmoxagentlab` tag. The package is the Python standard library only.

## 🚀 Setup

```bash
ssh-copy-id root@your-proxmox-host
pip install proxmox-agent-lab
proxmox-lab init
proxmox-lab doctor
```

`init` writes the config. `doctor` checks SSH, the node and the store. Read its warnings. A template that is not `template: 1` will not be cloned.

## 🧪 One session

```bash
L=$(proxmox-lab lease-begin --purpose "build a test rig" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
trap 'proxmox-lab lease-end --lease "$L"' EXIT

proxmox-lab guest create --lease "$L" --vmid 9101 --start
proxmox-lab guest run --lease "$L" --vmid 9101 -- uname -a
```

That clones your template into a free VMID. If `doctor` warned, use `--fresh` and an OS image instead (`--kind lxc --ostemplate …` for a container). A VMID collision destroys a real machine.

`lease-end` destroys that lease's guests and leaves the host up. `"host_left_running": true` is the normal result.

## ⌨️ Commands

| | Command | What it does |
|---|---|---|
| 🩺 | `proxmox-lab doctor` | Is the host reachable and the config sound? |
| 📋 | `proxmox-lab status` | Host, leases and guests at a glance |
| 🪪 | `proxmox-lab lease-begin --purpose "…"` | Open a lease. Nothing else mutates without one |
| 💓 | `proxmox-lab lease-heartbeat --lease "$L"` | Keep a long session from expiring |
| 🔢 | `proxmox-lab guest nextid` | Next free VMID. Pass it to create. A collision destroys a real machine |
| 🌉 | `proxmox-lab network bridges` | Host bridges, so create is not stuck guessing vmbr0 |
| 💿 | `proxmox-lab storage content` | ISO and template volids for `--iso`, `--ostemplate`, and `guest media` |
| 🆕 | `proxmox-lab guest create --lease "$L" --vmid N --fresh --iso local:iso/name.iso --start` | Build a guest and boot that CD. Or omit `--iso` and clone a template |
| ▶️ | `proxmox-lab guest run --lease "$L" --vmid N -- uname -a` | Run a command in the guest |
| 📤 | `proxmox-lab push --lease "$L" --vmid N --file F --dest P` | Copy a file in |
| 📥 | `proxmox-lab pull --lease "$L" --vmid N --remote P --out F` | Copy a file out |
| 🔍 | `proxmox-lab guest probe --vmid N` | Can this guest be reached? |
| 🧹 | `proxmox-lab lease-end --lease "$L"` | Destroy this lease's guests. Host stays up |
| 🗑️ | `proxmox-lab guest destroy --lease "$L" --vmid N --confirm` | Delete one guest now |
| 📸 | `proxmox-lab guest snapshot create --lease "$L" --vmid N --name boot` | Snapshot a lease-owned guest. `rollback` needs it stopped, and `--confirm` |
| 📦 | `proxmox-lab guest template --lease "$L" --vmid N --confirm` | Turn a stopped guest into a template `guest create` can clone |
| 💾 | `proxmox-lab storage status` | Free space on each store. Read-only |
| 🕸️ | `proxmox-lab netcap capture --lease "$L" --vmid N --out cap.pcap` | Pcap of that VM's tap only. TLS stays ciphertext |
| 🧠 | `proxmox-lab memflow read --lease "$L" --vmid N --addr 0x1000` | Read a running qemu guest from outside it |
| 🔌 | `proxmox-lab mcp` | The same operations as 38 tools over stdio |

Agents follow [SKILL.md](SKILL.md). Everything else is in [docs/README.md](docs/README.md).

## 🔌 Power, if you want it

The host stays on. Turn automatic power-off on only for a machine that should sleep when the lab is idle:

```toml
[power]
auto_shutdown = true
```

Then `lease-end`, the idle sweep and the cleanup cron power it off when nothing is running, and only after that is verified. Your own guests keep it up.

Or power it off once, yourself:

```bash
proxmox-lab power shutdown --standalone-authorized
proxmox-lab power wake --standalone-authorized
proxmox-lab power status
```

## 🛡️ Sharing a host

Guests the lab creates are tagged `proxmoxagentlab`. Cleanup never stops or deletes a guest without that tag.

MIT licensed. Only for machines you own or are allowed to test: [RESPONSIBLE_USE.md](RESPONSIBLE_USE.md).
