# Old Computer → AI Lab

[![CI](https://github.com/jr551/proxmox-agent-lab/actions/workflows/ci.yml/badge.svg)](https://github.com/jr551/proxmox-agent-lab/actions/workflows/ci.yml)
[![GitHub release](https://img.shields.io/github/v/release/jr551/proxmox-agent-lab)](https://github.com/jr551/proxmox-agent-lab/releases/latest)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**Give an old computer one more job.**

Turn spare hardware into a safe, AI-operated research lab. Boot disposable
Windows, Linux and Android machines; inspect software, drivers, firmware,
memory, USB traffic and network behaviour; then destroy the experiment and
switch the physical computer off — verified.

Built for authorized reverse engineering, defensive security research,
digital forensics, interoperability, debugging and education. The technical
package and CLI remain **`proxmox-agent-lab`** and **`proxmox-lab`**.

```bash
curl -fsSL https://raw.githubusercontent.com/jr551/proxmox-agent-lab/main/install.sh | bash
```

<p align="center">
  <img src="docs/images/vnc-capture.png" width="600"
       alt="Screenshot captured over VNC showing a Debian login prompt with text typed into it">
  <br>
  <em>👀 What the agent sees. A real screen capture from a running VM —<br>
  and the agent typed that text.</em>
</p>

---

## Why an old computer?

Containers cannot reproduce every kernel, driver, installer, USB device or
firmware interaction. A spare desktop running Proxmox can. It is cheap, local,
and physically separate from the computer holding your everyday work.

But a lab left running becomes a liability: wasted power, abandoned machines
and experiments that outlive the agent controlling them. Here, work happens in
a **lease**. Lease ends → guests destroyed → host powered off, *verified*.
Agent dies → a watchdog cleans up anyway.

## ⚡ What it can do

| | Capability | Command |
|---|---|---|
| 🖥️ | Create throwaway VMs and containers | `api`, clone a template |
| 👀 | **See and drive the screen** (input + settled PNG) | `console screenshot`, `--screenshot-after` |
| ⌨️ | **Type and click** | `console type`, `keys`, `click` |
| 🔧 | **Run commands** (picks the channel for you) | `guest run` |
| 📄 | Read a console as exact text | `console text` |
| 📦 | Move files in and out | `push`, `pull` |
| 🪟 | Install Windows | `windows install` |
| 💿 | Fetch cloud images, checksum-verified | `storage download-url` |
| 🔒 | Force all traffic through a VPN | `net gateway-create` |
| 🕵️ | Prove there is no leak | `net leak-test` |
| 📓 | Audit everything (SQLite) | `journal` |
| 🔌 | Power on, power off, verified | `lease-begin` / `lease-end` |
| 📌 | Keep machines alive (host stays on) | `lease-begin --long-term` |
| 💾 | Weekly backups of what you keep | `backup` |
| 🔗 | Send someone a disposable console link | `share create` |
| 📱 | Emulated phones (Galaxy S20, Pixel…) | `android create` |
| 🔬 | Introspect a guest's memory, agentless | `memflow processes` |
| 🐞 | Step through / read / write guest memory | `memflow trace`, `analyze` |
| 💉 | Scan & inject physical RAM (any guest OS) | `memflow scan`, `phys-write` |
| 🔌 | Sniff a USB device's traffic to a pcap | `usb sniff` |
| 🕸️ | Capture a guest's network traffic to a pcap | `netcap capture` |
| 🔓 | Decrypt & rewrite its HTTPS (MITM in a container) | `netcap intercept` |

## What people use it for

🧹 **A clean machine on demand.** Not a container — a real OS with a real
kernel, booted from nothing, gone afterwards. "Works on my machine" stops being
a question.

📖 **Testing your own install docs.** Point an agent at a fresh VM and your
README. If step 4 is wrong, it finds out, not your users.

💥 **Letting an agent break things.** Kernel modules, firewall rules,
partitioning, `rm -rf`. The blast radius is one lease.

🔬 **Authorized reverse engineering.** Study an application, driver or firmware
image from the screen down to live memory, USB and network behaviour.

🕵️ **Defensive analysis of untrusted software.** Route the guest through a VPN
gateway with no path to your home network, and prove it with `net leak-test`
first.

🪟 **Driving a GUI installer.** Windows Setup has no API. A multimodal model
looks at the screen and clicks *Next*.

🐧 **Reproducing a bug across distros.** Boot Rocky, Ubuntu and Debian in turn,
run the same script, compare.

### Bring the analysis environment you already trust

Old Computer AI Lab is the orchestration and containment layer, not another
tool-bundle distribution. Use clean OS templates or bring environments such as
[FLARE-VM](https://github.com/mandiant/flare-vm) and
[REMnux](https://docs.remnux.org/). The lab handles physical power, disposable
guests, agent access, isolation, outside-the-guest observation and cleanup.

## Responsible research

This project is intended for systems you own or are explicitly authorized to
test. Good uses include vulnerability reproduction, malware defence, incident
response, interoperability, driver and firmware development, forensics and
education. Advanced mutation and interception features are opt-in, auditable
and documented with their trust boundaries.

See [RESPONSIBLE_USE.md](RESPONSIBLE_USE.md) and
[SECURITY.md](SECURITY.md). These statements describe the project's intent;
the software remains MIT licensed.

## Why it is safe to hand to an agent

- 🧹 **Nothing outlives a lease.** Cleanup deletes only what the lease created.
- ✅ **The off switch is verified.** `lease-end` doesn't claim success until the
  API stops answering, twice.
- 🚧 **Host changes refused by default.** Networking, storage, disks and
  permissions all need an explicit flag.
- 🎯 **Destructive actions are pinned.** Formatting a disk requires the serial
  number to match.
- 🔒 **Fails closed.** With VPN egress on, a dropped tunnel stops guest traffic
  rather than leaking to your home connection.
- 🔑 **Secrets never travel.** Kept in your OS keyring; never in argv, the
  config file, or the audit log.

## 📦 Install

**One touch** — installs, configures, stores your token, health-checks:

```bash
curl -fsSL https://raw.githubusercontent.com/jr551/proxmox-agent-lab/main/install.sh | bash
```

**By hand:**

```bash
pip install proxmox-agent-lab
proxmox-lab init
proxmox-lab secrets set proxmox-token
proxmox-lab doctor
```

**Nothing installed at all?** `bootstrap.sh` builds a throwaway environment in
your temp directory and prints the path — handy for an agent that only has the
skill file:

```bash
PXL=$(curl -fsSL .../bootstrap.sh | sh) && "$PXL" doctor
```

**Blank Proxmox machine?** Run this on it, as root — it creates the API token,
grants the right privileges, and arms Wake-on-LAN:

```bash
curl -fsSL https://raw.githubusercontent.com/jr551/proxmox-agent-lab/main/proxmox-host-setup.sh | bash
```

**You need:** a spare PC running [Proxmox VE](https://www.proxmox.com) 8 or 9,
and Python 3.11+ to drive it from. Wake-on-LAN is the default power-on and
needs only the NIC's MAC address.

**Zero dependencies.** VNC client, WebSockets, S3 signing, PNG encoding — all
standard library.

📘 Full walkthrough: **[docs/INSTALL.md](docs/INSTALL.md)**

## 🚀 Five-minute tour

```bash
L=$(proxmox-lab lease-begin --purpose "tour" \
    | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')

proxmox-lab guest probe --vmid 9001        # how can I reach this guest?
proxmox-lab guest run --lease "$L" --vmid 9001 uname -a
proxmox-lab console screenshot --vmid 9001 # what's on the screen?

proxmox-lab lease-end --lease "$L"         # destroy guests, power off
```

That last line prints `"host_powered_off": true`. That's the whole point.

## Point your agent at it

`SKILL.md` is a ready-made skill for Claude Code and Codex — drop it in your
skills directory. **[docs/AGENTS.md](docs/AGENTS.md)** is the guidance an agent
should read: which channel to use when, what to do when things fail, and the
traps that cost real debugging time.

## 📚 Docs

| | |
|---|---|
| 📘 [INSTALL.md](docs/INSTALL.md) | Bare PC → working lab |
| ⚙️ [CONFIGURATION.md](docs/CONFIGURATION.md) | Every setting and secret |
| 🤖 [AGENTS.md](docs/AGENTS.md) | How an agent should drive it |
| 👀 [console.md](docs/console.md) | Screens, keyboard, serial, OCR |
| 📦 [storage.md](docs/storage.md) | Disks, images, file transfer |
| 🔒 [network.md](docs/network.md) | VPN egress and leak testing |
| 🪟 [windows.md](docs/windows.md) | Installing Windows |
| ✅ [VERIFICATION.md](docs/VERIFICATION.md) | What has been run on real hardware, and what has not |
| 📌 [long-term-leases.md](docs/long-term-leases.md) | Machines that stay |
| 🔗 [share.md](docs/share.md) | Disposable console links |
| 📱 [android.md](docs/android.md) | Emulated Android devices |
| 🔬 [memflow.md](docs/memflow.md) | Agentless memory introspection & debugging |
| 🔌 [usb.md](docs/usb.md) | USB passthrough & traffic sniffing |
| 🕸️ [netcap.md](docs/netcap.md) | Network capture, SSL inspection & MITM relay |
| 🛡️ [safety-policy.md](docs/safety-policy.md) | The rules enforced in code |

## Status

Beta. Core lifecycle, console, storage, transfer, VPN, Android and Windows paths
have been exercised against real hardware. Advanced capabilities clearly mark
what has and has not been observed end to end. Interfaces may change before
1.0; compatibility for the package name and `proxmox-lab` command is a goal.

MIT licensed — see [LICENSE](LICENSE).
