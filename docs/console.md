# Console access, screenshots and text

Everything here runs through `proxmox-lab console`. Reads need no
lease; anything that touches a guest (keys, typing, clicks, exec) requires an
active lease and is audited.

## Choosing a channel

Pick the cheapest channel that answers the question.

| Situation | Use | Why |
|---|---|---|
| Guest is a Linux shell, LXC, or has a serial console | `console text` | Returns the exact character stream from Proxmox |
| Guest runs qemu-guest-agent | `console exec` | Real exit codes, stdout and stderr |
| Graphical screen: installer, desktop, BIOS, boot menu | `console screenshot` | A multimodal model reads the PNG directly |
| Text-mode screen reachable only over VNC | `console screenshot --ocr` | Opt-in glyph decoding; see below |

## Screenshots

```bash
proxmox-lab console screenshot --vmid 9001 --settle 2
```

Writes a PNG to `~/.local/state/proxmox-agent-lab/screens/` and prints its
path, dimensions, and whether the screen looks like a text console. Add
`--upload` to also place it in the S3 scratch bucket and print a presigned URL.

The capture path is a self-contained RFB client: Proxmox `vncproxy`, a
WebSocket upgrade, RFB 3.8 with VNC authentication, and Raw/Zlib/CopyRect
decoding into a PNG written with `zlib` alone. No Pillow, numpy, or noVNC.

## Optional cloud vision fallback

```bash
proxmox-lab console inspect --lease "$L" --vmid 9001
```

This captures one PNG and tries NVIDIA Nemotron Nano 12B v2 VL, the named
OpenRouter Nemotron Omni free endpoint, and finally `openrouter/free`, in that
order. Use `--provider` to test one stage. The guest must be registered to the
given lease. The command audits the selected provider/model but never the
image, prompt, or key. Free OpenRouter providers may log prompts for service
improvement, so do not submit confidential or personal screens.

This is intentionally separate from `screenshot`: external image transmission
must be explicit. The model's coordinates are advisory and still pass through
the cursor-calibration workflow before any click occurs. Accordingly, a vision
response recommending a click always reports `actionable: false` and
`requires_cursor_calibration: true`, even when its JSON is structurally valid.

## Input

```bash
proxmox-lab console keys  --lease "$L" --vmid 9001 enter f2 ctrl-alt-delete
proxmox-lab console click --lease "$L" --vmid 9001 --x 640 --y 412
proxmox-lab console type  --lease "$L" --vmid 9001 --text-stdin --enter
```

For a graphical workflow, add `--screenshot-after SECONDS` to any input
command. It waits for the UI to settle and returns the resulting PNG in the
same JSON response, avoiding a separate reconnect and screenshot call:

```bash
proxmox-lab console keys --lease "$L" --vmid 9001 enter --screenshot-after 3
```

- `keys` defaults to the VNC path; `--via api` uses Proxmox `sendkey`, which
  works even when RFB input is unavailable.
- `type` takes `--text-stdin` so a password never appears in `argv`, the shell
  history, or the audit ledger. Only the character count is recorded.
- Clicks are refused outside the current screen bounds.
- `--screenshot-out PATH` gives the post-input PNG a stable checkpoint name.

The first click at a given VM resolution is intentionally not a click. The
client moves the visible cursor to the requested coordinate and returns a PNG.
Confirm the pointer is on the intended control, then repeat the same command
with `--confirm-calibration`. The confirmed calibration is cached for that
lease and VM until the framebuffer resolution changes, at which point the safe
two-step check repeats.

See [gui-installers.md](gui-installers.md) for the bounded state loop agents
should use with installers, including Haiku. A model without image vision
should delegate the current full-screen decision to a vision-capable model,
not run Tesseract over crops.

### Keyboard input needs a VGA display

Verified on the live host: RFB key events go to the emulated PS/2 keyboard, so
they only reach the guest when the VM has a graphical display.

- `vga: serial0` (the Linux cloud templates): VNC shows a *rendering* of the
  serial output, and screenshots work, but typing goes nowhere. Drive these
  with `console text --send`.
- `vga: std` and Windows guests: VNC keyboard and pointer work normally.

Check with `qm config <vmid> | grep vga`, or just try `console text` first. A
template can be switched with `--data vga=std`, but the change needs a full
stop and start — a reset keeps the old display device.

## Terminal text

```bash
proxmox-lab console text --vmid 9001 --send "ip -br a" --seconds 4
```

Attaches to the Proxmox terminal (`termproxy`) for an LXC container or a QEMU
guest that has a serial device, optionally sends one line, and returns the
output with escape sequences stripped. This is exact text. Prefer it over any
form of OCR.

A QEMU guest needs `serial0: socket` in its config for this path; cloud-init
templates in this lab are built with it.

## OCR policy

OCR is **off by default and only meaningful for text-mode screens**. The
reasoning:

1. Models driving this lab are multimodal, so a graphical screen is better read
   from the PNG than from any decoder here.
2. Where a guest is genuinely a terminal, Proxmox already hands over the real
   characters, and a guess is strictly worse than the truth.

That leaves one real gap: a VGA text-mode screen reachable only over VNC — a
boot menu, BIOS setup, the Windows Setup text phase, a kernel panic. Those are
a strict grid of fixed-size glyphs in a tiny palette, so `--ocr` does exact
glyph lookup rather than fuzzy recognition. It reports `confidence` and warns
below 0.5. On a graphical screen it refuses and says so.

Lookup needs a font table, which is not shipped:

```bash
# from any local PSF console font
proxmox-lab console import-font --file /path/to/Lat15-VGA16.psf.gz

# or read one out of a running Linux guest
proxmox-lab console import-font --lease "$L" --from-vmid 9001 \
  --guest-path /usr/share/consolefonts/Lat15-VGA16.psf.gz
```

PSF1 and PSF2, plain or gzipped, with or without a Unicode table. The table is
written to `assets/console-font.json` and is deliberately not committed.

## Preflight

```bash
proxmox-lab console preflight
```

Reports capabilities rather than raw privilege names, because the names moved:
**Proxmox 9 replaced `VM.Monitor` with granular `VM.GuestAgent.*` privileges**
(`Unrestricted`, `FileRead`, `FileWrite`, `FileSystemMgmt`, `Audit`). Preflight
accepts either spelling, so it tells the truth on PVE 8 and PVE 9 instead of
reporting a privilege that no longer exists.

Granting a missing privilege is a host-level access change and needs the user's
explicit authorization for that exact change. Note that an API token with
privilege separation does not inherit its user's grants — ACLs must be set for
both `agent@pve` and `agent@pve!lab`.

## Serial login on agentless guests

Generic cloud images ship without qemu-guest-agent. `TermSession` therefore
supports driving a getty directly:

- `expect(patterns, poke=True)` — read until a pattern appears, nudging an idle
  console with newlines so it redraws a prompt drawn before we attached;
- `login(user, password)` — log in without echoing the password anywhere;
- `run(command)` — run one command, using an echoed sentinel so completion is
  unambiguous even when the command prints nothing.

This is how the VPN gateway bootstraps its agent, and how `net leak-test` runs
inside an Alpine guest that has no agent at all.
