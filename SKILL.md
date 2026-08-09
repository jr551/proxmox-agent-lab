---
name: proxmox-agent-lab
description: Turn an old computer into a leased, disposable AI research lab that powers itself on and off. Create and destroy Proxmox VMs and LXC containers, drive screens over VNC, run guest commands, move files, install Windows or Linux, isolate traffic, and verify cleanup and physical shutdown. Use for authorized reverse engineering, defensive analysis, digital forensics, interoperability, clean-machine testing, home labs, spare-PC virtual machines, or guest OS installers.
---

# Old Computer AI Lab

The `proxmox-agent-lab` engine turns spare hardware into a leased, auditable,
fail-closed research host. Experiment freely inside a lease; never leave the
machine running after it ends.

## 🔌 If `proxmox-lab` is not installed

Everything below assumes the `proxmox-lab` command exists. If it does not,
bootstrap it into a temporary environment and use the printed path — no
checkout, no permanent install:

```bash
PXL=$(curl -fsSL https://raw.githubusercontent.com/jr551/proxmox-agent-lab/main/bootstrap.sh | sh)
"$PXL" doctor
```

Use `"$PXL"` wherever the examples say `proxmox-lab`. The environment is
cached, so later runs are instant. Before the first command, the bootstrap and
CLI check the latest GitHub release, but never more than once per 24 hours.
The check is fail-open and a GitHub outage must not block lab work. Config and
lease state still go to their normal locations, which is what lets the
watchdog clean up after you.

Run `proxmox-lab doctor` first if anything about the setup is unclear — it
reports the config in use, whether the host answers, and any missing
privileges.

An unreachable host with `ok: true`, a populated `config_file`, and
`proxmox_token_stored: true` usually means the spare PC is simply powered off;
continue with `lease-begin` so the configured power path can wake it. Do not
ask the user to re-enter configuration that `doctor` already found.

## 🔑 Every task follows this shape

Never call standalone `power-on` for agent work. It deliberately refuses
without `--standalone-authorized` because it has no lease finalizer; start with
`lease-begin`, which both wakes the host and establishes cleanup ownership.

```bash
L=$(proxmox-lab lease-begin --purpose "<sanitized purpose>" \
    | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
trap 'proxmox-lab lease-end --lease "$L"' EXIT
```

1. `lease-begin` powers the machine on if needed. It takes a minute or two.
   Omit `--timeout` to use the configured boot budget; cold-start values below
   90 seconds are rejected.
2. Pass `--lease "$L"` to every mutating command.
3. `lease-heartbeat --lease "$L"` if the work runs beyond 30 minutes.
4. `lease-end` must report `host_powered_off=true`. **Never claim completion
   until it does.** If cleanup fails, run `cleanup-expired --all` and report
   the exact blocker.

Read [docs/AGENTS.md](docs/AGENTS.md) before first use. Read
[docs/safety-policy.md](docs/safety-policy.md) for the enforced rules, and the
topic guides ([console](docs/console.md), [storage](docs/storage.md),
[network](docs/network.md), [windows](docs/windows.md)) when a task needs
them. [VERIFICATION.md](docs/VERIFICATION.md) records which subsystems have
actually been exercised on hardware and which are unit-tested only.

## 📌 Long-term leases

Most work uses an ordinary lease. When the user wants machines that **survive
and stay running**, use a long-term lease instead:

```bash
proxmox-lab lease-begin --long-term --purpose "<why this must persist>"
proxmox-lab lease-list                                    # what is pinned on?
proxmox-lab lease-destroy --lease <id> --confirm          # the only way out
```

**While one exists the machine never powers down** — not by `lease-end`, not
by the idle timer. Say so explicitly when you create one; it costs the user
electricity. Its guests are protected from deletion and backed up weekly.

Only create one when the user has asked for something persistent. If they just
want work done now, an ordinary lease is right. See
[docs/long-term-leases.md](docs/long-term-leases.md).

## 🧪 Work inside a lease

- Prefer an unprivileged LXC for ordinary Linux commands; a QEMU VM when you
  need a real kernel, another OS, nested virtualisation or device emulation.
- Clone a template rather than installing an OS. Check node, storage and VMID
  availability first.
- Use `proxmox-lab api --lease "$L" --method <M> --path <p> --data k=v` for
  writes. The wrapper tags new guests with `codex-lab` and the lease — leave
  those tags alone.
- Register anything created through an unusual path with `lease-register`.

## 👀 Reach into a guest

```bash
proxmox-lab guest probe --vmid <id>                        # ask before assuming
proxmox-lab guest run --lease "$L" --vmid <id> uname -a     # picks the channel
proxmox-lab console screenshot --vmid <id> --settle 2       # PNG
proxmox-lab console keys  --lease "$L" --vmid <id> enter f2
proxmox-lab console click --lease "$L" --vmid <id> \
  --target "visible label" --x 640 --y 412
proxmox-lab console type  --lease "$L" --vmid <id> --text-stdin --enter
```

For GUI installers, attach `--screenshot-after 3` to `keys`, `type`, or
`click`. Make one action, read the returned full PNG, and repeat. After three
unchanged attempts, stop and diagnose; never build an ad-hoc Pillow/Tesseract
crop loop. If the active model has no vision, delegate the single-screen
decision to a vision-capable model while keeping all mutations in the primary
agent. Follow [docs/gui-installers.md](docs/gui-installers.md), including its
Haiku checkpoint map.

When `proxmox-lab secrets list` reports a vision key stored, prefer `console
inspect --lease "$L" --vmid <id>` for the first graphical read. It explicitly
sends one lease-owned PNG concurrently through NVIDIA Nemotron Nano 12B v2 VL,
the named OpenRouter Nemotron Omni free endpoint, and `openrouter/free`, using
the first structurally valid proposal. Every configured route receives the
screen in automatic mode. The provider sees a same-size copy with labelled
100-pixel X/Y axes; later frames dim stable pixels while changes stay bright
and outlined. The original screenshot remains untouched.
Never treat model output as
authorization or bypass the click-calibration guard. OpenRouter free providers
may retain prompts for service improvement; do not send confidential or
personal screens. Without a key, use native vision or the single-screen
delegation above.

`console click` requires a visible target label. The harness moves the cursor,
captures a full checkpoint, and clicks only when cloud vision independently
matches that one label and coordinate. A failed or timed-out verdict returns no
click: stop and diagnose. Never bypass it with raw `api`, keyboard input, or a
reboot, and never mutate guest storage as a GUI recovery step.

After a calibrated click opens a popup menu, prefer arrow keys and `enter` for
the visibly highlighted selection instead of guessing another coordinate.

`guest probe` tells you what will actually work. Prefer real text over pixels;
read the PNG when a screen is the truth. **A guest whose display is the serial
console accepts screenshots but silently ignores VNC keystrokes** — probe
reports that as `keyboard_input: false`.

OCR is opt-in (`console screenshot --ocr`), only meaningful on VGA text-mode
screens, and refuses on graphical ones. Never reach for it when `console text`
can answer the same question.

## 🔗 Sharing a console with a person

When the user wants to *see* a guest themselves, or show someone else, send a
disposable link instead of describing the screen:

```bash
proxmox-lab share create --lease "$L" --vmid <id> --minutes 30 [--once]
```

Returns a public URL to that one console, expiring on its own. The URL is a
credential: never put it in a commit, an issue, or the journal. Prefer
`--once` and short expiries. `share revoke --all` or `share down` kills every
link. See [docs/share.md](docs/share.md).

## 📦 Move files

```bash
proxmox-lab push --lease "$L" --vmid <id> --file ./payload --dest /tmp/payload
proxmox-lab pull --lease "$L" --vmid <id> --remote /var/log/x.log --out ./x.log
```

Transfers use short-lived presigned URLs, so no credential enters the guest.
`--windows` for Windows guests, `--url-only` when there is no guest agent.
Never paste a presigned URL into a commit or the journal.

## 💿 Storage

```bash
proxmox-lab storage status
proxmox-lab storage list-disks
proxmox-lab storage download-url --lease "$L" --url <url> --filename <name> \
  --checksum <digest>
```

Image downloads happen on the node and require a checksum. Adding a physical
disk **formats it**: `storage add-disk` needs `--host-change-authorized`, an
explicit `--device`, and `--expect-serial`. Look at what is on a disk before
wiping it, and report what you found.

## 🔒 Forced-VPN egress

```bash
proxmox-lab net gateway-create --lease "$L" --vmid 9000
proxmox-lab net verify --lease "$L" --vmid 9000         # before real work
proxmox-lab net attach --lease "$L" --vmid 9001
proxmox-lab net leak-test --lease "$L" --vmid 9001 --password-stdin \
  --gateway-vmid 9000
```

One gateway VM on an isolated bridge, so a guest cannot bypass it and egress
fails closed if the tunnel drops. `verify` judges from the gateway;
`leak-test` judges from inside a guest and covers IPv4 bypass, IPv6, DNS and
the kill switch. An inconclusive probe is **unproven**, not a pass.

## 📱 Android devices

```bash
proxmox-lab android profiles
proxmox-lab android create --lease "$L" --vmid <id> --profile galaxy-s20
proxmox-lab android adb --lease "$L" --vmid <id> shell getprop ro.product.model
```

The emulator draws on the VM's console, so `console screenshot`, `console
click` and `share create` all work on it unchanged.

A profile's screen, RAM, storage and API level are real. **Its model string is
not** — `ro.product.model` is baked into the system image and cannot be
overridden, so a device reports `sdk_gphone_x86_64` whatever the profile says.
`android status` reports what the device actually says; trust that, not the
profile.

**Default to `x86_64`.** Proxmox cannot accelerate ARM, so `--abi arm64-v8a`
runs with no KVM and is close to unusable for a UI; x86_64 images from API 30
translate most ARM-only apps anyway. An S20 needs ~11 GB of host RAM — check
free memory first and prefer `--profile minimal` (3.6 GB) on a busy host.
Build once with `--as-template` (or `android template --vmid <id>` on a device
already built), then clone — a clone boots Android without re-downloading the
SDK. See
[docs/android.md](docs/android.md).

## 🪟 Windows

```bash
proxmox-lab windows install --lease "$L" --vmid <id> --version 2025
proxmox-lab windows wait-agent --vmid <id>
proxmox-lab windows finish --lease "$L" --vmid <id>
```

Default is an interactive install you drive over VNC — screenshot, click,
type. `--unattended` generates and attaches an answer ISO instead.

## ⚛️ ReactOS

Before planning or browsing, run `proxmox-lab recipe reactos`. It returns
machine-readable, checksum-pinned release facts, compatible QEMU hardware, and
the bounded cleanup sequence. Follow it directly: do not rediscover SourceForge
metadata, print downloads into the model context, or use standalone `power-on`.

For DragonFlyBSD, Haiku, OpenBSD, or Windows ME, use `proxmox-lab recipe
dragonfly`, `recipe haiku`, `recipe openbsd`, or `recipe windows-me` the same
way. The freely downloadable OS recipes pin verified media and exact storage
and guest-creation semantics. The Windows ME recipe instead requires
user-supplied licensed media and describes the legacy QEMU hardware; the
Server-only `windows install` helper is not the only way to create a Windows
guest.

## 🔬 Introspection (advanced, opt-in)

To see what a running guest is *really* doing from underneath it — malware
triage, rootkit hunting, an agent-less or untrusted VM — memflow reads its live
memory from the hypervisor, so a process hidden inside the guest still shows up.

```bash
proxmox-lab memflow doctor                                 # is the host ready?
proxmox-lab memflow processes --lease "$L" --vmid <id>     # process list from outside
proxmox-lab memflow read  --lease "$L" --vmid <id> --addr 0x... --len 64
proxmox-lab memflow registers --lease "$L" --vmid <id>     # vCPU state (via QMP)
proxmox-lab memflow write --lease "$L" --vmid <id> --addr 0x... --hex 90 --i-understand
proxmox-lab memflow scan  --lease "$L" --vmid <id> --hex <needle>          # find in physical RAM (any OS)
proxmox-lab memflow phys-read  --lease "$L" --vmid <id> --addr 0x... --len 32
proxmox-lab memflow phys-write --lease "$L" --vmid <id> --addr 0x... --hex 00 --i-understand  # RAM injection
proxmox-lab memflow dump  --lease "$L" --vmid <id> --addr 0x... --len 4096 --out ./code.bin
proxmox-lab memflow trace --lease "$L" --vmid <id> --steps 20 [--over]   # step into/over
proxmox-lab memflow break --lease "$L" --vmid <id> --addr 0x...          # breakpoint
proxmox-lab memflow ghidra-setup --lease "$L" --lxc <ct>                 # once: build analysis LXC
proxmox-lab memflow analyze --lease "$L" --vmid <id> --lxc <ct> --addr 0x... --len 4096
```

Unlike everything else here, this reaches the Proxmox host over **SSH** (a
separate trust boundary from the API token). It needs no patched kernel — the
QEMU connector reads `/proc/<qemu-pid>/mem` — but the host must be prepared once
with `memflow host-setup --host-change-authorized`, and it stays **off** until
`[memflow] enabled` and `ssh_host` are set. `doctor` proves each layer
fail-closed. Process introspection is fully supported for **Windows** guests
(memflow-win32); Linux is best-effort. A process visible here but hidden inside
the guest is the classic rootkit tell. `read`/`registers` are read-only;
`write` (kernel VA, Windows) and `phys-write` (physical RAM, **any OS** — this
is RAM injection, reaches userspace) both mutate a live guest and are hard-gated
behind `--i-understand`. `scan` + `phys-write` can, for example, override a
client's cert pinning so [`netcap intercept`](docs/netcap.md) can read its
HTTPS. See [docs/memflow.md](docs/memflow.md).

## 🔌 USB traffic sniffing

For driver development, capture the USB traffic of a device passed through to a
guest — the host's `usbmon` sees QEMU's usbfs traffic, so no guest agent is
needed and you get a Wireshark-readable pcap.

```bash
proxmox-lab usb list                                       # host devices + passthroughs
proxmox-lab usb sniff --lease "$L" --device 04e8:61b6 --seconds 15 --out ./cap.pcap
proxmox-lab usb attach --lease "$L" --vmid <id> --device 04e8:61b6 --host-change-authorized
```

Shares memflow's opt-in host SSH channel (off until `[memflow]` ssh is set).
Sniffing is passive; `attach`/`detach` are passthrough changes gated behind
`--host-change-authorized`. **Never attach a device backing active storage.**
See [docs/usb.md](docs/usb.md).

## 🕸️ Network capture, SSL inspection & MITM

See — and rewrite — what a guest puts on the wire. Passive capture taps the
guest's host interface (`tap<vmid>i0`) for a Wireshark pcap, no guest agent
needed. To read TLS, terminate it in a **disposable LXC** running mitmproxy (the
same container pattern as the Ghidra box); `ca` prints an install helper for the
guest OS so it trusts the interception.

```bash
proxmox-lab netcap capture --lease "$L" --vmid <id> --seconds 15 --out ./guest.pcap
proxmox-lab netcap mitm-setup --lease "$L" --lxc <ct>              # once: build proxy LXC
proxmox-lab netcap ca --lease "$L" --lxc <ct> --os windows --out ./ca.pem
proxmox-lab netcap intercept --lease "$L" --lxc <ct> --probe https://example.com --har ./flows.har
# active MITM — rewrite traffic passing through:
proxmox-lab netcap intercept --lease "$L" --lxc <ct> \
    --set-response-header 'X-Debug: 1' --replace 'OLD/NEW'
```

Shares memflow's opt-in host SSH channel. Capture and interception both need a
lease; the MITM LXC is a lab guest, registered to the lease and destroyed with
it. Only intercepts traffic a client routes through the proxy, and only decrypts
once its CA is trusted — install it in guests you control. See
[docs/netcap.md](docs/netcap.md).

## 📓 Audit

```bash
proxmox-lab journal --limit 20
proxmox-lab journal --lease "$L"
proxmox-lab journal --summary
```

Every action appends a redacted event. Secrets, typed text and presigned URLs
are never recorded.

## 🛑 Boundaries

- Refuse host changes — networking, storage, disks, permissions, cluster, SDN,
  firewall defaults, passthrough — unless the user asked for that exact change,
  then pass `--host-change-authorized`.
- Never delete a guest the lease did not create.
- Never print, commit, or pass a secret on a command line. Use `--text-stdin`
  and `--password-stdin`.
- Treat a force-off as an emergency finaliser, not a normal shutdown.
- Preserve task IDs and failure text when reporting, with secrets redacted.
