# proxmox-agent-lab

**Turn a spare PC into an on-demand lab an AI agent can wake, use, and switch
back off.**

[![CI](https://github.com/jr551/proxmox-agent-lab/actions/workflows/ci.yml/badge.svg)](https://github.com/jr551/proxmox-agent-lab/actions/workflows/ci.yml)
[![GitHub release](https://img.shields.io/github/v/release/jr551/proxmox-agent-lab)](https://github.com/jr551/proxmox-agent-lab/releases/latest)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

The `proxmox-lab` CLI powers a spare Proxmox host on and off, creates and
destroys lease-owned guests, drives screens, runs commands, moves files, and
records an audit trail. All work happens inside a **lease**: ordinary leases
clean up and verify shutdown when they end; long-term leases deliberately keep
the host on for persistent machines.

> **New here?** The [documentation index](docs/README.md) lists the shortest
> path for every task.

## Requirements

- A spare PC running [Proxmox VE 8 or 9](https://www.proxmox.com), with a wired
  NIC and Wake-on-LAN (or another power mode).
- Python 3.11+ on the controller.
- A secrets backend. The default `auto` reads `PROXMOX_AGENT_LAB_*` environment
  variables; `keychain`, `secret-tool` and `file` are explicit options. See the
  [secrets guide](docs/CONFIGURATION.md#secrets).

## Install

One command installs, configures, stores secrets, and health-checks:

```bash
curl -fsSL https://raw.githubusercontent.com/jr551/proxmox-agent-lab/main/install.sh | bash
```

No install? `bootstrap.sh` drops a throwaway environment under
`$TMPDIR/proxmox-agent-lab-env` and prints the path:

```bash
PXL=$(curl -fsSL https://raw.githubusercontent.com/jr551/proxmox-agent-lab/main/bootstrap.sh | sh) && "$PXL" doctor
```

Full setup — Proxmox host preparation and the audit ledger — is in
[docs/INSTALL.md](docs/INSTALL.md). Every setting is in
[docs/CONFIGURATION.md](docs/CONFIGURATION.md).

### Onboard a host

Two ways to bring a machine under `proxmox-lab` control. Both end with
`onboard serve` on the controller receiving the pairing callback and writing a
verified config.

**💿 New host — boot an auto-install ISO.** Generate a pairing bundle, build a
Proxmox ISO that installs the host and pairs it on first boot, then boot the
spare PC from it (UEFI required):

```bash
proxmox-lab onboard prepare --mode iso --directory ~/pxl-pc-bundle \
  --controller-host pc.example.com --fqdn lab.example.com \
  --disk-serial "$TARGET_DISK_ID_SERIAL" \
  --root-password-hash-file ~/pxl-root-password.hash --wipe-confirmed
proxmox-lab onboard build-iso --bundle ~/pxl-pc-bundle \
  --source ~/Downloads/proxmox-ve.iso --sha256 "$OFFICIAL_ISO_SHA256"
proxmox-lab onboard serve --bundle ~/pxl-pc-bundle \
  --config-out ~/.config/proxmox-agent-lab/new-lab.toml
```

**🖥️ Existing Proxmox host — pair in place.** On a machine that already runs
Proxmox, generate a bundle, copy `host-setup.py` to it, and run it as root to
create the API principal, isolated bridge and pairing — no reinstall:

```bash
proxmox-lab onboard prepare --mode existing --directory ~/pxl-host-bundle \
  --controller-host pc.example.com --fqdn lab.example.com
proxmox-lab onboard serve --bundle ~/pxl-host-bundle \
  --config-out ~/.config/proxmox-agent-lab/new-lab.toml
# on the Proxmox host, as root:
python3 host-setup.py --host-change-authorized
```

A fresh Debian VPS (no Proxmox yet) uses `--mode vps` instead — it installs
the Proxmox kernel and packages first. Details and the Wi-Fi option are in
[docs/onboarding.md](docs/onboarding.md).

## First safe workflow

Every task follows this shape. `lease-end` sits in a `trap` so it runs even if
the work fails:

```bash
(
set -e
L=$(proxmox-lab lease-begin --purpose "first run" \
    | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
trap 'proxmox-lab lease-end --lease "$L"' EXIT

proxmox-lab guest clone --lease "$L" --template 9000 --newid 9101
proxmox-lab guest probe --vmid 9101
proxmox-lab guest run --lease "$L" --vmid 9101 uname -a
)  # the trap destroys the clone and powers the host off, including on failure
```

`lease-end` must report `"host_powered_off": true`. If cleanup fails, run
`proxmox-lab cleanup-expired --all` and report the exact blocker. The full
lease skeleton is in [SKILL.md](SKILL.md); fixes are in
[docs/troubleshooting.md](docs/troubleshooting.md).

## What it can do

| Capability | Command | Guide |
|---|---|---|
| Disposable Linux/Windows/Android guests | `guest clone` / `guest run` | [SKILL.md](SKILL.md), [docs/RECIPES.md](docs/RECIPES.md) |
| Drive the screen | `console screenshot`, `console type`, `console click` | [docs/console.md](docs/console.md) |
| Cloud vision | `console inspect` | [docs/console.md](docs/console.md) |
| File transfer | `push` / `pull` | [docs/storage.md](docs/storage.md) |
| VPN egress and leak testing | `net gateway-create` / `net leak-test` | [docs/network.md](docs/network.md) |
| Install Windows | `windows install` | [docs/windows.md](docs/windows.md) |
| Android emulators | `android create` | [docs/android.md](docs/android.md) |
| Memory introspection | `memflow processes` / `scan` / `write` | [docs/memflow.md](docs/memflow.md) |
| USB/network capture | `usb sniff` / `netcap capture` / `intercept` | [docs/usb.md](docs/usb.md), [docs/netcap.md](docs/netcap.md) |
| Long-running persistent guests | `lease-begin --long-term` | [docs/long-term-leases.md](docs/long-term-leases.md) |

Every subcommand is mapped in [docs/commands.md](docs/commands.md).

## Lifecycle and safety

An ordinary lease wakes the host, clones a guest, runs the work, then
`lease-end` destroys the lease's guests and verifies the host is off. A
`lease-begin --long-term` keeps the host and its guests running until
`lease-destroy --confirm`. Details in
[docs/long-term-leases.md](docs/long-term-leases.md).

Shutdown is verified, not assumed: `lease-end` reports whether the API stopped
answering. If cleanup or power-off fails, the failure is recorded,
`cleanup-expired` retries, and the host stays on when shutdown cannot be
verified. The enforced rules are in [docs/safety-policy.md](docs/safety-policy.md):

- **Lease-owned guests only** — cleanup deletes only what the lease created.
- **Verified shutdown** — `lease-end` does not claim success until the API
  stops answering, twice.
- **Host changes refused by default** — networking, storage, disks and
  permissions need `--host-change-authorized`.
- **Destructive actions are pinned** — formatting a disk requires the serial
  number to match.
- **Fails closed** — with VPN egress on, a dropped tunnel stops guest traffic
  rather than leaking to your home connection.
- **Secrets stay in the configured backend** — never in `argv`, the config
  file, or the audit log.

This project is for systems you own or are authorized to test. See
[RESPONSIBLE_USE.md](RESPONSIBLE_USE.md) and [SECURITY.md](SECURITY.md).

## Documentation

- [docs/README.md](docs/README.md) — task-oriented index
- [docs/INSTALL.md](docs/INSTALL.md) — install and first boot
- [docs/CONFIGURATION.md](docs/CONFIGURATION.md) — every setting and secret
- [SKILL.md](SKILL.md) — copy-paste lease skeleton and quick reference
- [docs/commands.md](docs/commands.md) — generated map of every subcommand
- [docs/AGENTS.md](docs/AGENTS.md) — how an agent should drive it
- [docs/RECIPES.md](docs/RECIPES.md) — common agent workflows
- [docs/troubleshooting.md](docs/troubleshooting.md) — fix a failure
- [docs/safety-policy.md](docs/safety-policy.md) — enforced rules
- [docs/VERIFICATION.md](docs/VERIFICATION.md) — hardware-tested vs unit-tested
- [CONTRIBUTING.md](CONTRIBUTING.md) — developer setup and checks

## Status

Beta. Core lifecycle, console, storage, transfer, VPN, Android and Windows
paths have been exercised against real hardware. Advanced capabilities clearly
mark what has and has not been observed end to end. Interfaces may change
before 1.0; compatibility for the package name and `proxmox-lab` command is a
goal.

MIT licensed — see [LICENSE](LICENSE).
