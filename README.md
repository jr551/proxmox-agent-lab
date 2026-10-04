# proxmox-agent-lab

**A disposable Proxmox guest for an agent, cleaned up when the lease ends.**

[![CI](https://github.com/jr551/proxmox-agent-lab/actions/workflows/ci.yml/badge.svg)](https://github.com/jr551/proxmox-agent-lab/actions/workflows/ci.yml)
[![GitHub release](https://img.shields.io/github/v/release/jr551/proxmox-agent-lab)](https://github.com/jr551/proxmox-agent-lab/releases/latest)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

One machine drives one Proxmox host over root SSH. The agent takes a lease,
creates what it needs, and `lease-end` destroys those guests. The host powers
off only when nothing else is running. Your SSH key is the only credential.
Python 3.11 or newer, no extra packages, nothing to install on the host
except an optional cleanup cron job.

## Setup

```bash
ssh-copy-id root@your-proxmox-host
pip install proxmox-agent-lab
proxmox-lab init
proxmox-lab doctor
```

`init` writes a config. `doctor` checks SSH, the node, and the local store,
and exits non-zero when something is wrong. Read its warnings.

## One session

```bash
L=$(proxmox-lab lease-begin --purpose "build a test rig" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
trap 'proxmox-lab lease-end --lease "$L"' EXIT

proxmox-lab guest create --lease "$L" --vmid 9101 --start
proxmox-lab guest run --lease "$L" --vmid 9101 -- uname -a
```

That clones your template into an unused VMID. If `doctor` warned that it
is not `template: 1`, pass `--fresh` and an OS image instead (`--kind lxc`
and `--ostemplate` for a container). Check the VMID is free first. A
collision destroys a real machine.

If `lease-end` leaves the host up, it prints `reason`. That usually means
your own guests are running. Leave the host up.

## Sharing a host

Guests the lab creates are tagged `proxmoxagentlab`. Cleanup never stops or
deletes a guest without that tag. The host stays on while any guest is
running, including yours.

Agents should follow [SKILL.md](SKILL.md). The other docs are listed in
[docs/README.md](docs/README.md). Memory reads of a lease-owned qemu guest
are command-line only: [docs/memflow.md](docs/memflow.md).

`proxmox-lab mcp` serves the same operations as 29 tools over stdio.

MIT licensed. Only for machines you own or are allowed to test:
[RESPONSIBLE_USE.md](RESPONSIBLE_USE.md).
