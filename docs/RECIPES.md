# 🍳 Quick-start recipes for AI agents

Copy-paste starting points for the jobs agents actually do. Every recipe
follows [AGENTS.md](AGENTS.md): one lease, `lease-end` in a trap, no completion
claim without `"host_powered_off": true`. Every command below was checked
against the argparse registrations in the current source (flag names, required
arguments).

```bash
L=$(proxmox-lab lease-begin --purpose "<task>" \
    | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
trap 'proxmox-lab lease-end --lease "$L"' EXIT
# long job? heartbeat every ~20 min:
# while :; do sleep 1200; proxmox-lab lease-heartbeat --lease "$L" || break; done &
```

## Shared preamble: clone and start a fresh guest

Recipes 1 and 3 start the same way: clone a template, register it to the lease,
and start it. The three lines below are the preamble; the recipes refer back
to it as "clone → register → start".

```bash
# clone a template into a fresh lease-owned guest (replace <template-vmid>, <new-vmid>, <name>)
proxmox-lab api --lease "$L" --method POST \
  --path /nodes/$NODE/qemu/<template-vmid>/clone \
  --data newid=<new-vmid> --data name=<name> --wait-task
proxmox-lab lease-register --lease "$L" --kind qemu --vmid <new-vmid>
proxmox-lab api --lease "$L" --method POST --path /nodes/$NODE/qemu/<new-vmid>/status/start --wait-task
```

`--wait-task` is required to block until the Proxmox task finishes
(`cli.py:2951-2978`); templates vary — pick the one from `site-notes.md`.

## 1. Browse the web in a throwaway browser (GUI sites, logins, JS)

For pages that need a real browser profile or resist plain HTTP fetches.

Use the shared preamble above (e.g. `<template-vmid>` → `9001` `browse`), then:

```bash
proxmox-lab guest probe --vmid 9001            # which channels answer?

# look → act loop:
proxmox-lab console screenshot --vmid 9001     # PNG path printed; hand to your vision model
proxmox-lab console click --lease "$L" --vmid 9001 --x 512 --y 384
proxmox-lab console keys  --lease "$L" --vmid 9001 enter f5 ctrl-l
proxmox-lab console type  --lease "$L" --vmid 9001 --text-stdin --enter <<< "https://example.com"
```

- Type URLs into the address bar (`ctrl-l`) instead of clicking it — deterministic.
- Secrets (passwords) go through `--text-stdin`; they never appear in argv,
  shell history, or the audit journal.
- Display trap: see [console.md §4 — Keyboard input needs a VGA display](console.md#keyboard-input-needs-a-vga-display). A `vga: serial0` guest takes screenshots but `guest probe` reports `keyboard_input: false`; drive those over `console text` instead.
- Untrusted site? Attach a VPN gateway first (recipe 5) and prove isolation
  with `net leak-test`.

## 2. Read a page exactly (no pixels)

When the guest has a terminal, exact characters beat screenshots:

```bash
# LXC or QEMU with serial console:
proxmox-lab console text --vmid 9001 --send "curl -s https://example.com | head -50" --seconds 8
```

Or skip the screen entirely if the guest agent answers:

```bash
proxmox-lab guest run --lease "$L" --vmid 9002 -- curl -fsSL <url> -o /tmp/p.html
proxmox-lab pull --lease "$L" --vmid 9002 --remote /tmp/p.html --out ./p.html
```

- `console text` takes an optional `--lease`; `pull` requires `--remote`
  (guest path) and writes locally via `--out`.
- `console screenshot --ocr` and `console import-font` were removed in 0.11.0 — glyph matching could not read a guest's own font (see [console.md appendix](console.md#appendix-why-there-is-no-ocr)). Use `console text` for a real terminal or `console screenshot --for-model` / `console inspect` for a model-read screen.

## 3. Develop and test an app in a disposable environment

The core loop: clone → push code → build/test → pull artifacts → destroy.

Use the shared preamble above (e.g. `<ubuntu-template>` → `9002` `devbox`), then:

```bash
proxmox-lab push --lease "$L" --vmid 9002 --file ./myapp.tar.gz --dest /root/
proxmox-lab guest run --lease "$L" --vmid 9002 -- bash -c "cd /root && tar xzf myapp.tar.gz && make test"
proxmox-lab pull --lease "$L" --vmid 9002 --remote /root/artifacts.tar --out ./artifacts.tar
```

- Real exit codes from `guest run`; take a screenshot when a command hangs
  (usually a prompt waiting for input).
- Artifacts that should outlive the guest go to S3: `proxmox-lab s3 put --key artifacts.tar --file artifacts.tar`,
  then `s3 presign` a download link.
- Keep guest disks on fast storage; bulk USB disks are for ISOs, not builds.
- Reproduce across distros: clone Rocky/Ubuntu/Debian templates in turn, same
  script each time.

### Long-running builder

Work that outlives one session (big compiles, persistent dev box) uses a
long-term lease: `lease-begin --long-term`, register guests, weekly `backup`.
It keeps the host powered on until `lease-destroy --confirm` — say so when you
create it.

## 4. Windows app testing

```bash
proxmox-lab windows install --lease "$L" --vmid 9100    # clone + boot; you drive Setup
proxmox-lab windows wait-agent --vmid 9100              # optional --lease; warns on stalls
proxmox-lab windows finish --lease "$L" --vmid 9100     # REQUIRED
```

Unattended variant:

```bash
proxmox-lab windows install --lease "$L" --vmid 9100 \
  --unattended --password-stdin <<< "<admin-password>"
```

- `windows finish` enables RDP/SSH and deletes the answer ISO that carries the
  plaintext admin password. Never abandon an install before finishing it.
- After finish: `push/pull --windows` work against the guest.

## 5. Test untrusted software safely (VPN egress + leak proofing)

```bash
proxmox-lab net gateway-create --lease "$L" --vmid <free-vmid>   # build the gateway VM
proxmox-lab net attach --lease "$L" --vmid 9002                  # move guest behind gateway
proxmox-lab net verify --lease "$L" --vmid <gateway-vmid>
proxmox-lab net leak-test --lease "$L" --vmid 9002 --password-stdin <<< "<guest-console-password>"
```

- The gateway needs a free VMID (`--vmid`) and is cloned from the configured
  template; `attach` optionally takes `--gateway-vmid`, `leak-test` uses it to
  cycle wg0 for the kill-switch check.
- Now detonate: kernel modules, firewall changes, sketchy installers. Blast
  radius = this lease. Gateway dies at `lease-end` → guests lose all egress.

## 6. Mobile app testing (Android emulators)

```bash
proxmox-lab android profiles                                   # galaxy-s20, pixel-6, ...
proxmox-lab android create --lease "$L" --vmid 9200 --profile galaxy-s20
proxmox-lab android status --vmid 9200                         # trust THIS, not ro.product.model
proxmox-lab push --lease "$L" --vmid 9200 --file ./app.apk --dest /data/local/tmp/
proxmox-lab android adb --lease "$L" --vmid 9200 install /data/local/tmp/app.apk
proxmox-lab console screenshot --vmid 9200                     # emulator draws on the console
```

- x86_64 profiles need nested KVM on the host; arm64 runs under pure emulation
  at single-digit FPS. Build once with `--as-template`, clone thereafter.

## 7. Debug what you cannot log into (agentless)

```bash
# what is running in there, no guest agent required:
proxmox-lab memflow processes --lease "$L" --vmid 9002
# single-step from outside (advanced):
proxmox-lab memflow trace --lease "$L" --vmid 9002 --steps 10
# capture its network passively:
proxmox-lab netcap capture --lease "$L" --vmid 9002 --seconds 60 --out ./traffic.pcap
```

memflow/netcap need the opt-in host SSH channel (`[memflow] enabled`); run
`memflow doctor` / `netcap doctor` first. Memory writes are gated behind
`--i-understand` — do not pass it unless the task explicitly asks for
live-memory modification.

## 8. Show a human the screen

```bash
proxmox-lab share create --lease "$L" --vmid 9002 --minutes 30
proxmox-lab share list     # live links; revoke with share revoke
```

Expiring public noVNC link; the URL *is* the credential.

## Choosing a channel (cheat sheet)

See [console.md §1 — Choosing a channel](console.md#1-choosing-a-channel) for
the canonical channel table. Do not duplicate it here. Note: `console screenshot
--ocr` / `console import-font` were removed in 0.11.0 — use `console text`
or `console screenshot --for-model` / `console inspect` (see
[console.md appendix](console.md#appendix-why-there-is-no-ocr)).

## Failure drill

```bash
proxmox-lab doctor
proxmox-lab guest probe --vmid 9001
proxmox-lab console screenshot --vmid 9001
proxmox-lab journal --limit 20
```

HTTP 403 = token privilege missing (doctor lists it). Hung guest command =
take a screenshot; it is usually sitting at a prompt. "No egress" behind VPN =
check ICMP first, minimal images often lack curl — `net leak-test` handles it.


## 9. macOS guests

The lab can also run macOS VMs built with
[OSX-PROXMOX](https://github.com/luchina-gabriel/OSX-PROXMOX) — same lease,
screenshot/click loop, push/pull. Host prep is a one-time host change; full
details and gotchas: [macos.md](macos.md).

## Reporting honestly

- `lease-end` must print `"host_powered_off": true`; otherwise say cleanup did not complete.
- Distinguish inconclusive from negative — a probe returning nothing proves nothing.
- Quote the output that supports the claim.
