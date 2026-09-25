# proxmox-agent-lab

**Turn a spare PC into an on-demand lab an AI agent can wake, use, and switch
back off.**

[![CI](https://github.com/jr551/proxmox-agent-lab/actions/workflows/ci.yml/badge.svg)](https://github.com/jr551/proxmox-agent-lab/actions/workflows/ci.yml)
[![GitHub release](https://img.shields.io/github/v/release/jr551/proxmox-agent-lab)](https://github.com/jr551/proxmox-agent-lab/releases/latest)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

`proxmox-lab` is an SSH + SQLite skill and CLI for a spare Proxmox host: the
controller drives the host **as root over ssh**, keeps every lease, resource
record and journal event in **one local SQLite file**, and talks to agents
either as CLI commands or as an MCP server over stdio. The only thing that
ever runs on the host itself is the optional garbage-collector script —
nothing is installed there, no daemon, no listener, no credentials beyond
your own ssh key.

> **New here?** The [documentation index](docs/README.md) lists the shortest
> path for every task.

## Requirements

- A spare PC running [Proxmox VE 8 or 9](https://www.proxmox.com), with a wired
  NIC and Wake-on-LAN (or another power mode).
- Python 3.11+ on the controller — the package is **stdlib only**: zero
  runtime dependencies to install.
- Root ssh access to the host, set up once with
  `ssh-copy-id root@proxmox`.
- One TOML config file. Nothing secret ever goes in it.

## Install

```bash
python -m pip install proxmox-agent-lab
proxmox-lab init        # writes the starter config
$EDITOR ~/.config/proxmox-agent-lab/config.toml
proxmox-lab doctor      # checks the install end to end; non-zero if anything fails
```

From a checkout, `scripts/proxmox-lab` runs the same commands without
installing. Full setup — host preparation, ssh trust, the doctor checklist
and the optional garbage collector — is in
[docs/INSTALL.md](docs/INSTALL.md); every setting with its default is in
[docs/CONFIGURATION.md](docs/CONFIGURATION.md).

## First safe workflow

Every task follows this shape. `lease-end` sits in a `trap` so it runs even if
the work fails:

```bash
(
set -e
L=$(proxmox-lab lease-begin --purpose "first run" \
    | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
trap 'proxmox-lab lease-end --lease "$L"' EXIT

proxmox-lab guest clone --lease "$L" --source 9000 --vmid 9101
proxmox-lab guest probe --vmid 9101
proxmox-lab guest run --lease "$L" --vmid 9101 uname -a
)  # the trap destroys the clone and powers the host off, including on failure
```

`lease-begin` wakes the host if needed and records what the lease owns;
`lease-end` destroys exactly that and verifies the host is actually off.
Long work renews with `lease-heartbeat --lease "$L"`. If cleanup cannot
finish, run `proxmox-lab cleanup-expired` and report the exact blocker. The
full lease skeleton is in [SKILL.md](SKILL.md); fixes are in
[docs/troubleshooting.md](docs/troubleshooting.md).

## What it can do

| Capability | Command | Guide |
|---|---|---|
| Disposable guests (create, clone, run) | `guest create` / `guest clone` / `guest run` | [SKILL.md](SKILL.md), [docs/commands.md](docs/commands.md) |
| Drive the screen | `console screenshot`, `console type`, `console keys` | [docs/commands.md](docs/commands.md) |
| File transfer | `push` / `pull` | [docs/commands.md](docs/commands.md) |
| Power | `power wake`, `power status`, `power shutdown` | [docs/INSTALL.md](docs/INSTALL.md) |
| Leases, including machines that stay on | `lease-begin --long-term`, `lease-destroy` | [docs/long-term-leases.md](docs/long-term-leases.md) |
| Audit journal (local SQLite) | `journal` | [docs/commands.md](docs/commands.md) |
| MCP server — 23 tools over stdio | `proxmox-lab mcp` | [SKILL.md](SKILL.md) |
| Host-side lease garbage collector | `gc install` / `gc status` / `gc uninstall` | [docs/INSTALL.md](docs/INSTALL.md) |

Every subcommand is mapped in [docs/commands.md](docs/commands.md).

## Lifecycle and safety

An ordinary lease wakes the host, clones a guest, runs the work, then
`lease-end` destroys the lease's guests and verifies the host is off.
`lease-begin --long-term` keeps the host and its guests running until
`lease-destroy --confirm` — the guests are stamped `pxl-expiry=0` so the
host-side collector never touches them either. Details in
[docs/long-term-leases.md](docs/long-term-leases.md).

The enforced rules are in [docs/safety-policy.md](docs/safety-policy.md):

- **Lease-owned guests only** — cleanup deletes only what the lease created;
  a guest without pxl metadata is never touched.
- **Verified shutdown** — success is claimed only after the host stops
  answering across repeated probes; a shutdown that cannot be confirmed is
  reported as a failure, never assumed.
- **Host changes refused by default** — anything that edits the host itself
  needs `--host-change-authorized`; standalone `power wake` / `power shutdown`
  need `--standalone-authorized`.
- **Destructive actions are pinned** — `guest destroy` refuses without
  `--confirm`, and only for guests the lease owns.
- **Fails closed** — a missing lease, a missing flag or an unreachable host
  stops the action; guest metadata that does not parse is warned about and
  skipped, never "cleaned up" on a guess.
- **No secrets anywhere** — your ssh agent and keys are the only credential;
  nothing secret enters argv, the config file, or the journal.

This project is for systems you own or are authorized to test. See
[RESPONSIBLE_USE.md](RESPONSIBLE_USE.md) and [SECURITY.md](SECURITY.md).

## Documentation

- [docs/README.md](docs/README.md) — task-oriented index
- [docs/INSTALL.md](docs/INSTALL.md) — host preparation, ssh trust, first run
- [docs/CONFIGURATION.md](docs/CONFIGURATION.md) — every setting and default
- [SKILL.md](SKILL.md) — copy-paste lease skeleton and quick reference
- [docs/AGENTS.md](docs/AGENTS.md) — how an agent should drive it
- [docs/commands.md](docs/commands.md) — generated map of every subcommand
- [docs/troubleshooting.md](docs/troubleshooting.md) — fix a failure
- [docs/safety-policy.md](docs/safety-policy.md) — enforced rules
- [docs/architecture.md](docs/architecture.md) — how the pieces fit together
- [docs/VERIFICATION.md](docs/VERIFICATION.md) — hardware-tested vs unit-tested
- [CONTRIBUTING.md](CONTRIBUTING.md) — developer setup and checks

## Status

Beta. The 0.x series is mid-rework onto the SSH + SQLite architecture
described here — core lifecycle, store, transport and the garbage collector
have landed; the CLI surface and MCP server are completing now, and
interfaces may change before 1.0. Compatibility for the package name and the
`proxmox-lab` command is a goal. What has actually been verified lives in
[docs/VERIFICATION.md](docs/VERIFICATION.md).

MIT licensed — see [LICENSE](LICENSE).
