# 🤖 Driving this lab as an agent

Guidance for an AI agent using `proxmox-lab`. Read [safety-policy.md](safety-policy.md)
for the rules enforced in code; this is about doing the job well.

## 🔑 The one rule

**Every run is a lease, and every lease ends.**

```bash
L=$(proxmox-lab lease-begin --purpose "<what you are doing>" \
    | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')

# ... work ...

proxmox-lab lease-end --lease "$L"
```

Put `lease-end` in a trap, a `finally`, or the equivalent, so it runs even when
the work fails. It must print `"host_powered_off": true`; if it does not, say
so plainly rather than reporting success.

`lease-begin` powers the machine on. Expect it to take a minute or two. Omit
`--timeout` so the configured boot budget is used. A cold-start override below
90 seconds is rejected; a short timeout creates duplicate leases and false
failure reports rather than making the host boot faster.

Work lasting more than 30 minutes needs `lease-heartbeat --lease "$L"`, or the
watchdog will clean up underneath you.

## 👀 Choosing how to talk to a guest

This is the decision agents most often get wrong. **Ask first:**

```bash
proxmox-lab guest probe --vmid 9001
```

It reports whether the guest agent answers, whether a serial console exists,
whether VNC keystrokes will land, and what to do about it. Then:

| The guest can... | Use | Why |
|---|---|---|
| run qemu-guest-agent | `guest run` | Real exit codes and separated streams |
| offer a serial console | `guest run --password-stdin` | No agent needed |
| only show a screen | `console screenshot` | You are multimodal — look at it |

`guest run` picks between the first two automatically. Prefer it over calling
`console exec` or `console text` directly unless you need something specific.

**Prefer text over pixels.** If a guest can hand you real characters, take
them. A screenshot of a terminal is strictly worse than its output: you cannot
grep it, and you might misread it.

**But do look at screens when a screen is the truth.** A stuck boot, a GUI
installer, a kernel panic, a BIOS menu — read the PNG. That is what it is for.

### ⚠️ The trap that costs an hour

A VM whose display is the serial console (`vga: serial0`, which most Linux
cloud templates use) will happily give you a *screenshot*, and silently
discard every keystroke you send over VNC. RFB key events go to the emulated
PS/2 keyboard, which that VM does not present.

`guest probe` reports this as `"keyboard_input": false`. If you are typing at a
guest and nothing happens, that is why. Use the serial channel instead.

### ISOs that ignore the keyboard at the boot menu

Some legacy install ISOs ignore Tab and typed characters at their boot menu
while Enter and arrow keys still work (observed: Ubuntu 14.10 server,
isolinux/vesamenu). The "append `console=ttyS0` via Tab" shortcut is unusable
on that media, so the installer boots onto VGA; drive it with the bounded
screenshot/keyboard loop instead (see
[gui-installers.md](gui-installers.md)). Serial access can still be enabled
after install — for example an upstart getty plus `console=ttyS0` on the
kernel line — for later text access.

## 🔤 OCR

Off by default, and it should usually stay off:

1. You can read the PNG. A model looking at a screenshot beats any decoder here.
2. Where a guest is a real terminal, `console text` gives exact characters, and
   a guess is worse than the truth.

`console screenshot --ocr` exists for the narrow case both of those miss: a
VGA text-mode screen reachable only over VNC. It refuses on graphical screens
and needs a font table installed first.

## 🩺 When something fails

Work down this list before concluding the tool is broken.

```bash
proxmox-lab doctor          # config, secrets, reachability, privileges
proxmox-lab guest probe --vmid <id>
proxmox-lab console screenshot --vmid <id>   # what is it actually doing?
proxmox-lab journal --limit 20               # what did I just do?
```

**HTTP 403 on something that should work.** The API token almost certainly
lacks a privilege. Proxmox tokens with privilege separation inherit *nothing*
from their user, so a role granted to the user does not apply to the token.
`doctor` lists what is missing.

**A guest command hangs.** Take a screenshot. The guest is usually sitting at a
prompt — a bootloader menu, a login, a package manager asking a question.

**"No egress" from a guest behind the VPN gateway.** Check with ICMP before
concluding the network is broken; a minimal image often has no `curl` at all.
`net leak-test` does this correctly.

## 📢 Reporting honestly

- If `lease-end` does not confirm power-off, **say so**. Do not report success.
- Distinguish *inconclusive* from *negative*. A probe that returned nothing is
  not proof of safety. The leak test models this deliberately: a broken probe
  is reported as unproven, never as a pass.
- Quote the command output that supports your claim. "The tunnel works" is
  weaker than an egress IP that differs from the home WAN.
- If you skipped part of the task, name the part.

## 🛑 Things that need explicit permission

The tool refuses these unless you pass a flag, and you should not pass the flag
unless the user asked for that specific change:

| Action | Flag | Why it is gated |
|---|---|---|
| Host networking, storage, permissions | `--host-change-authorized` | Outlives the lease; affects the machine itself |
| Formatting a disk | `--wipe-confirmed` plus `--expect-serial` | Irreversible, and device names move between boots |
| Preparing a host for memflow, or USB passthrough | `--host-change-authorized` | Installs a toolchain / hands host hardware to a guest |
| Writing live guest memory (`memflow write`, `memflow phys-write`) | `--i-understand` | A wrong byte crashes or compromises the running guest |
| Deleting a guest the lease did not create | *not possible* | Refused outright |

Before anything destructive, **look at the target**. A disk that "should be
empty" may not be — check before you wipe, and report what you found.

## ⚡ Working efficiently

- **Do not poll a build in a tight loop.** Cloning and provisioning take
  minutes. Start it, then do something useful, then check.
- **Reuse one lease** for a session's work instead of opening and closing
  repeatedly; each cycle costs a boot.
- **Templates beat installs.** Clone a cloud-init template in seconds rather
  than installing an OS.
- **Keep guest disks on fast storage.** A bulk USB disk is fine for ISOs and
  images, painful to boot from.
- **For GUI installers, use the bounded checkpoint loop.** One action can
  return its settled screenshot with `--screenshot-after 3`; do not create
  external OCR/Pillow crop loops. Use `console inspect` first when an optional
  cloud vision key is configured. Otherwise, if the current model has no
  vision, delegate the single-screen decision to one that does. See
  [gui-installers.md](gui-installers.md).

## 📝 A worked example

Bring up a Debian VM, check something, tear it down:

```bash
set -e
L=$(proxmox-lab lease-begin --purpose "check systemd unit ordering" \
    | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
trap 'proxmox-lab lease-end --lease "$L"' EXIT

proxmox-lab api --lease "$L" --method POST \
  --path /nodes/$NODE/qemu/102/clone \
  --data newid=9001 --data name=probe --wait-task
proxmox-lab lease-register --lease "$L" --kind qemu --vmid 9001 --name probe
proxmox-lab api --lease "$L" --method POST \
  --path /nodes/$NODE/qemu/9001/status/start --wait-task

proxmox-lab guest probe --vmid 9001
proxmox-lab guest run --lease "$L" --vmid 9001 systemd-analyze critical-chain
```

The `trap` is the important line. Everything else is detail.

## ⚡ Builders, templates, and long jobs

For repeated compile/test loops against a disposable builder, do not end a
lease between attempts — each `lease-begin`/`lease-end` cycle costs a host
boot and provisioning. Begin once, keep it alive with
`lease-heartbeat --lease "$L"` (every ≤20 min), and reuse it:

- **Promote a provisioned builder to a template** once it has your toolchain
  and caches (`guest template --lease "$L" --vmid <id>`, guest must be
  stopped). Later iterations clone it in seconds
  (`guest clone --lease "$L" --template <id> --newid <id>`), and the clone is
  registered to the lease automatically.
- **Long builds** should not block on an agent exec timeout. Start them
  detached: `guest run --lease "$L" --vmid <id> --detach <command…>` returns a
  pid immediately; stream output with
  `guest log --lease "$L" --vmid <id> --pid <pid> --follow` and block on
  completion with `guest wait --lease "$L" --vmid <id> --pid <pid>`. The exit
  code is recorded as a `grun-exit:N` marker in the log.
- **Large artifacts** (ISOs, qcow2 overlays) transfer in chunks with
  end-to-end SHA-256 verification: `push`/`pull` automatically chunk files
  above 32 MiB on Linux guests (`--chunk-size MB` to tune). A `pull` with
  `--sha256` skips the transfer entirely when the local copy already matches,
  so retries are cheap and idempotent.
- **Optional network services**: spawn a lease-owned DHCP server
  (`net dhcp-create`, optional PXE via `--bootfile` + `--next-server`), a TFTP
  server for boot files (`net tftp-create`, stage files with `net tftp-push`),
  or see who got an address (`net dhcp-leases`). Together they form a minimal
  PXE stack on the lab bridge for netbooting installers. They are optional:
  nothing spawns them unless you ask.
- **Kernel debugging**: when a guest waits on a serial debugger
  (e.g. ReactOS `connect a debugger on port COM1`), attach through the serial
  bridge instead of treating it as a wall:
  `console bridge --lease "$L" --vmid <id> --port 4000`, then point rosdbg,
  windbg, gdb or `nc 127.0.0.1 4000` at that port.
