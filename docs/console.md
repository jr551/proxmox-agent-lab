# Console access, screenshots and text

Everything here runs through `proxmox-lab console`. Reads need no
lease; anything that touches a guest (keys, typing, clicks, exec) requires an
active lease and is audited.

A guest the current lease did not create is not drivable by default, and the
refusal is raised before anything is transmitted — no keystroke, click, or
`sendkey` reaches a guest the lease does not own. Reaching for a guest left
over from an earlier lease therefore fails with the command that fixes it:

```
VMID 9246 existed before this lease; register it with 'proxmox-lab
lease-register --lease <id> --kind qemu --vmid 9246 --allow-existing' if you
intend to drive it
```

Run that once per lease, then drive the guest normally. `--allow-existing` is
for a guest that genuinely predates the lease; a guest this lease created is
registered without it.

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

### Watching something slow: `screenshot-burst`

```bash
proxmox-lab console screenshot-burst --vmid 9001 --count 6 --interval 10
```

For a progress bar, an installer's copy step, or a boot animation — anything
that changes slowly enough that one screenshot can't tell you whether it's
progressing or stuck. One VNC session stays open and captures `--count`
frames (default 6) spaced `--interval` seconds apart (default 10, so the
default run spans about a minute), then stitches them left to right into a
single PNG with each frame's elapsed seconds stamped in its corner. Frames
are never scaled or cropped to match, so a resolution change mid-sequence
(a boot menu switching to a desktop, for instance) is preserved rather than
distorted. Takes `--out` and `--upload` like `screenshot`. Prefer this over a
manual sleep-then-screenshot loop.

### If VNC itself is the problem: `--via monitor`

```bash
proxmox-lab console screenshot --vmid 9001 --via monitor --lease "$L"
```

An explicit fallback for the case where the VNC path cannot produce a frame at
all. It asks QEMU for a `screendump` through the Proxmox monitor endpoint, then
fetches the PNG and deletes the host copy.

Unlike everything else in this file, it **writes a file on the Proxmox host**,
so it is deliberately narrow:

- the host path is fixed and lease-scoped (`/var/tmp/proxmox-agent-lab-screens/
  <lease>/vm<vmid>-<stamp>.png`); no path can be passed in
- the only format requested is PNG, and the bytes are verified to be one
- the file is deleted in a `finally` path, and the result says whether that
  succeeded
- it needs `--lease`, an owned QEMU guest, and the opt-in `[memflow]` host SSH
  channel to bring the file back — see [memflow.md](memflow.md) for that gate
- only the fact of the capture is audited (source, dimensions, byte count),
  never image content
- arbitrary `qm monitor` commands stay unavailable; `virtio monitor` remains a
  read-only `info` allowlist

Proxmox guards the monitor endpoint with `Sys.Audit|Sys.Modify` on the VM path,
which a `PVEVMAdmin`-scoped lab token does not have. Without it the command
fails with a clear `Permission check failed (/vms/<vmid>, Sys.Audit|Sys.Modify)`
rather than doing anything halfway. Grant that privilege only if this fallback
is actually needed; VNC needs nothing beyond `VM.Console`.

The result carries `"source": "monitor"` (the default VNC path reports
`"source": "vnc"`). It has no text-console analysis and no stale-frame check,
because those need the raw framebuffer — so keep VNC as the default and reach
for this only when VNC fails.

## Optional cloud vision fallback

```bash
proxmox-lab console inspect --lease "$L" --vmid 9001
```

This stores the untouched PNG locally and overlays a labelled 100-pixel X/Y
grid on a separate same-size model-input PNG. Later frames dim unchanged pixels
while changed regions stay bright with a magenta boundary. Automatic mode
races NVIDIA Nemotron Nano 12B v2 VL, the named OpenRouter Nemotron Omni free
endpoint, and `openrouter/free`, returning the first structurally valid answer.
Use `--provider` to test one stage. Automatic mode sends the screen to every
configured route concurrently. The guest must be registered to the
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
proxmox-lab console click --lease "$L" --vmid 9001 --target "Install" --x 640 --y 412
proxmox-lab console type  --lease "$L" --vmid 9001 --text-stdin --enter
```

For a graphical workflow, add `--screenshot-after SECONDS` to any input
command. It waits for the UI to settle and returns the resulting PNG in the
same JSON response, avoiding a separate reconnect and screenshot call:

```bash
proxmox-lab console keys --lease "$L" --vmid 9001 enter --screenshot-after 3
```

### Did the input actually arrive?

`keys_sent` and `characters_sent` count what the controller transmitted, not
what the guest received. With `--screenshot-after`, `keys` and `type` also
report a delivery signal derived from that capture:

| Field | Meaning |
|---|---|
| `screen_changed: true` | the framebuffer differs from the previous capture of this guest at this resolution |
| `screen_changed: false` | pixel-identical to the previous capture; an `agent_hint` says the input may not have reached the guest and what to check |
| `screen_changed: null` | there was no earlier capture to compare against, so this run carries no delivery evidence yet |

It is evidence, not proof: a guest can change on its own, and a settled screen
can legitimately look the same. Nothing here fails or blocks — an unchanged
screen is a prompt to re-read with `console screenshot` or `console text`
before sending more input, not an error. Without `--screenshot-after` no
capture is taken and neither field appears, which means driving a screen with
no delivery evidence at all; on a graphical guest, pass it.

- `keys` defaults to the VNC path; `--via api` uses Proxmox `sendkey`, which
  works even when RFB input is unavailable.
- `type` takes `--text-stdin` so a password never appears in `argv`, the shell
  history, or the audit ledger. Only the character count is recorded.
- Clicks are refused outside the current screen bounds.
- `--screenshot-out PATH` (or the shorter `--out PATH`) gives the post-input
  PNG a stable checkpoint name.

By default, every click names its visible target. The harness moves the cursor,
captures a full checkpoint, and asks cloud vision to independently match the
label and coordinate before pressing the button:

```bash
proxmox-lab console click --lease "$L" --vmid 9001 \
  --target "Install" --x 640 --y 412 --screenshot-after 3
```

Failure, timeout, ambiguity, or disagreement leaves `clicked: false`. Stop and
inspect; there is no self-confirmation option for a named control.

For a deliberate click on background rather than a control, pass
`--empty-space` and omit `--target`. It bypasses cloud-vision target
verification, remains bounds-checked, and is audited as an unverified
coordinate click. Use it only when the intended point is known to be empty:

```bash
proxmox-lab console click --lease "$L" --vmid 9001 \
  --empty-space --button 3 --x 640 --y 412 --screenshot-after 3
```

When a click opens a popup menu or combobox, prefer arrow keys plus `enter` for
the visible selection. This preserves menu state and avoids guessing a second
coordinate.

See [gui-installers.md](gui-installers.md) for the bounded state loop agents
should use with installers, including Haiku. A model without image vision
should delegate the current full-screen decision to a vision-capable model,
not run Tesseract over crops.

## Is it actually frozen?

Two best-effort liveness probes, for when a screen has looked the same for a
while and you need to know whether it's genuinely stuck or just quiet:

```bash
proxmox-lab console has-gui-locked-up --lease "$L" --vmid 9001
proxmox-lab console has-terminal-locked-up --vmid 9001
```

`has-gui-locked-up` moves the pointer to two different points a moment apart
and checks whether the screen changed either time, using the same pixel-diff
this project already uses for change highlighting — no cloud vision call.
This client declares no support for RFB's Cursor pseudo-encoding, so a
compliant server (QEMU's among them) draws the pointer into the framebuffer
itself rather than compositing it client-side; `console click`'s own
verification already depends on this same fact. `has-terminal-locked-up`
sends no input at all — it samples a text console several times over about
two seconds and checks whether anything changed, since a live console's
cursor normally blinks on its own; it refuses a screen `console screenshot`
would not call text-mode.

Both return `"locked_up"` as a bool alongside the raw per-sample pixel
deltas, plus a `"caveat"` when the verdict is `true`: a static screen is good
evidence of a hang but not proof — an app that paints no hover feedback, or a
console run with cursor blink disabled, looks the same. Treat this as one
signal, not a certain diagnosis.

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

### The stream is guest bytes only

Proxmox puts its own records on the same channel: the websocket auth is
acknowledged with a bare `OK`, and the process behind `termproxy` announces
itself (`starting serial terminal on interface serial0 …` for a QEMU serial
line, `Connected to tty 1` for a container). Those are removed — from `console
text`, from `--follow`, and from `console bridge` alike — including when a
record is split across websocket reads, so a saved boot log, a boot-marker
match, and a debugger's input all see exactly what the guest sent. A genuine
guest line that merely *starts* with `OK` is left alone.

### Capturing before power-on

```bash
proxmox-lab console text --vmid 9001 --follow --timeout 900 \
  --wait-for-guest 300 > run.log 2>&1 &
proxmox-lab api --lease "$L" --method POST \
  --path "/nodes/$NODE/qemu/9001/status/start"
```

Proxmox will not open a terminal for a stopped guest, so a capture started
before power-on used to fail immediately with `VM 9001 not running` and lose
exactly the output worth having. `--wait-for-guest SECONDS` retries the attach
until the serial line exists, then streams. Without the flag the old behaviour
is unchanged: the first refusal is the answer.

This narrows the gap to one poll interval; it does not close it. For output
guaranteed from t=0, use `--from-reset` below — the QEMU process and its serial
socket survive a reset.

### Kernel debugging and boot capture

The serial chardev streams only to a **connected** client, so anything printed
before you attach is gone. Two things make early capture reliable:

- **Reset semantics**: `reset` restarts only the guest — the QEMU process and
  its serial socket stay alive, so an attached session or `console bridge`
  survives it. A stop/start replaces the QEMU process and drops everything.
- **`--from-reset`**: attaches the terminal session *first*, then triggers the
  reset, so output from t=0 lands in the stream:

```bash
proxmox-lab console text --vmid 9001 --follow --from-reset --lease "$L"
```

For a debugger prompt that acts on bare characters (KDB's `cont`, GRUB menus),
`--send-raw` transmits exactly the given characters with no trailing newline:

```bash
proxmox-lab console text --vmid 9001 --send-raw "cont" --lease "$L"
```

`console bridge` is bidirectional — bytes typed into the TCP connection reach
the guest — so `nc 127.0.0.1 <port>` is a full interactive serial terminal; a
read-only `socat -u` fallback is not needed.

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
