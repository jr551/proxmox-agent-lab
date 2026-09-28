# proxmox-agent-lab

**A skill that gives an AI agent a disposable Proxmox lab — and cleans up
after itself.**

[![CI](https://github.com/jr551/proxmox-agent-lab/actions/workflows/ci.yml/badge.svg)](https://github.com/jr551/proxmox-agent-lab/actions/workflows/ci.yml)
[![GitHub release](https://img.shields.io/github/v/release/jr551/proxmox-agent-lab)](https://github.com/jr551/proxmox-agent-lab/releases/latest)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

The agent takes a **lease**, creates the machines it needs, works, and when
the lease ends everything it made is destroyed and the host is switched off.
Nothing is left behind, so this is safe to point at a Proxmox box you already
use.

## Setup

**1. Trust your machine on the host** — the only host setup there is:

```bash
ssh-copy-id root@your-proxmox-host
```

**2. Install:**

```bash
pip install proxmox-agent-lab
```

**3. Point it at your host and check it works:**

```bash
proxmox-lab init                       # writes a starter config
proxmox-lab doctor                     # verifies the whole path; non-zero on failure
```

That's it. No daemon, no API token, no database to provision, nothing running
on the host.

## Use it as a skill

This is a skill first. Point your agent at it and it reads the instructions
only when a task actually needs a lab, so keeping it installed costs almost
nothing. The agent takes a lease, works, and cleans up on the way out —
including when the work fails:

```bash
# what the agent does, in short
L=$(proxmox-lab lease-begin --purpose "build a test rig" | jq -r .id)
trap 'proxmox-lab lease-end --lease "$L"' EXIT   # always cleans up

proxmox-lab guest clone --lease "$L" --source 9000 --vmid 9101
proxmox-lab guest run   --lease "$L" --vmid 9101 -- uname -a
proxmox-lab push        --lease "$L" --vmid 9101 --file payload.bin --dest /tmp/payload.bin
proxmox-lab console click --lease "$L" --vmid 9101 --x 360 --y 200
```

The full instructions live in [SKILL.md](SKILL.md).

### Prefer tools over shell commands? Mount the MCP server

That surface exists too — 29 tools over stdio:

```json
{
  "mcpServers": {
    "proxmox-agent-lab": {
      "command": "proxmox-lab",
      "args": ["mcp"]
    }
  }
}
```

**Worth knowing before you mount it:** a mounted MCP server publishes its
tool schemas into *every* session, used or not — around 2,400 tokens,
permanently, on top of the skill. That is what the convenience costs. If you
would rather pay it only when it earns its keep, leave the server unmounted
and let the skill drive the CLI.

## Works alongside your existing Proxmox install

**This is a guest, not a management layer. It shares the host; it does not own
it.** Point it at a Proxmox server already running your real workloads and
the two coexist — that is the normal case, not a special one.

Every guest it creates is **labelled at creation**:

```
tags:        proxmoxagentlab;my-mac;lease-20260927174717-ae7ddc76
description: pxl-lease=20260927174717-ae7ddc76 pxl-expiry=1790538437
```

Those labels are the entire boundary, and they are what every check reads:

- **Cleanup only ever touches labelled guests.** A guest without a
  `proxmoxagentlab` label is never destroyed, stopped, or reclaimed — the
  refusal happens before any host command runs. Do not work around it.
- **You can see the lab in the normal web UI** — ordinary guests, filterable
  by the `proxmoxagentlab` tag in Datacenter → the node. The second tag is
  the hostname of the machine that created it.
- **Guests you created yourself are never at risk.** No label, no lease, no
  touch. Long-term lab machines carry `pxl-expiry=0` so the collector leaves
  them running by design.
- **The host is only ever powered off when it is genuinely idle** — no running
  guest, including *your* unlabelled ones — and only after two clear checks
  several minutes apart. If anything of yours is running, the host stays up
  and the lab tells you why in `reason`.
- **Storage and VMIDs are yours to choose.** Fresh creates take
  `--storage <name>`, `--disk-gb <n>` and `--vmid <id>`, so the lab shares
  your existing pools. Check what is free first: colliding with a real VMID
  destroys somebody's machine.

A dedicated host works too and is still the simplest deployment — it just
removes the question entirely.

## The rules it holds itself to

- **Lease-owned only.** Every mutation is checked against the lease registry
  before a single host command is sent.
- **Nothing is assumed.** A machine is only reported destroyed, or a host
  reported off, once the host has been asked again and agrees.
- **Host changes are opt-in.** Anything editing the host itself needs
  `--host-change-authorized`; standalone `power wake` / `power shutdown` need
  `--standalone-authorized`.
- **Destructive actions are pinned.** `guest destroy` refuses without
  `--confirm`.
- **Fails closed.** An unreachable host, a missing lease, or guest metadata
  that doesn't parse stops the action instead of guessing.
- **No secrets anywhere.** Your ssh key is the only credential; nothing secret
  enters the config, the journal, or any command line.

## Documentation

- [docs/README.md](docs/README.md) — index of everything
- [docs/INSTALL.md](docs/INSTALL.md) — host preparation and Wake-on-LAN
- [docs/CONFIGURATION.md](docs/CONFIGURATION.md) — every setting and default
- [SKILL.md](SKILL.md) — how an agent should drive it
- [docs/AGENTS.md](docs/AGENTS.md) — operational guidance for agents
- [docs/commands.md](docs/commands.md) — every subcommand
- [docs/safety-policy.md](docs/safety-policy.md) — the enforced rules
- [docs/long-term-leases.md](docs/long-term-leases.md) — persistent machines
- [docs/troubleshooting.md](docs/troubleshooting.md) — fix a failure
- [docs/VERIFICATION.md](docs/VERIFICATION.md) — what has actually been tested
- [docs/architecture.md](docs/architecture.md) — how it fits together
- [CONTRIBUTING.md](CONTRIBUTING.md) — development and checks

## Status

Beta. The architecture is settled (ssh + one local SQLite file, nothing
host-side but an optional garbage collector) but interfaces may change before
1.0. What has been verified on real hardware — and what hasn't — is tracked
honestly in [docs/VERIFICATION.md](docs/VERIFICATION.md).

For systems you own or are authorized to test: [RESPONSIBLE_USE.md](RESPONSIBLE_USE.md)
and [SECURITY.md](SECURITY.md).

MIT licensed — see [LICENSE](LICENSE).
