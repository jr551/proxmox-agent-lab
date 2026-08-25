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

## 1. Choosing a channel

Pick the cheapest channel that answers the question. This is the only channel
table; the reading and writing sections below expand it without repeating it.

| Situation | Use | Why |
|---|---|---|
| Guest is a Linux shell, LXC, or has a serial console | `console text` | Returns the exact character stream from Proxmox |
| Guest runs qemu-guest-agent | `console exec` | Real exit codes, stdout and stderr (§2.4) |
| Graphical screen: installer, desktop, BIOS, boot menu | `console screenshot` | A multimodal model reads the PNG directly |
| You cannot see images yourself | `console screenshot --for-model` or `console inspect` | Hands the screen to a model that can (§2.3) |

There is no OCR. For the one-paragraph explanation and the removal schedule see the [appendix](#appendix-why-there-is-no-ocr).

## 2. Reading a screen

A screen is read by a model, never by glyph matching. Three paths, in the
order you should reach for them. The fourth row in the table above, `console
exec`, is a command channel, not a screen read — see §2.4.

### 2.1 The guest is a terminal — `console text`

Exact, cheap, always better than looking at pixels. Proxmox hands over the
guest's real character stream; no model needed.

```bash
proxmox-lab console text --vmid 9001 --send "ip -br a" --seconds 4
```

A QEMU guest needs `serial0: socket` in its config for this path; cloud-init
templates in this lab are built with it. Full `text` flags and the
`--follow`/`--from-reset`/`--wait-for-guest`/`bridge` modes are in §5 — this
subsection is the quick pointer.

### 2.2 The guest is graphical — `console screenshot`

```bash
proxmox-lab console screenshot --vmid 9001 --settle 2
```

Writes a PNG to `~/.local/state/proxmox-agent-lab/screens/` and prints its
path, dimensions, and whether the screen looks like a text console. Flags:

* `--vmid` (required), `--out` (override path), `--settle` seconds to wait
  before capturing (default `0.0`), `--timeout` (default `25.0`), `--upload` to
  also place the PNG in the S3 scratch bucket and print a presigned URL
  (`--url-expiry` default `3600`), `--via vnc|monitor` (default `vnc`), `--lease`
  (required with `--via monitor`), and `--for-model` (see §2.3). `--ocr` is a
  removal signpost — see the appendix.

The capture path is a self-contained RFB client: Proxmox `vncproxy`, a
WebSocket upgrade, RFB 3.8 with VNC authentication, and Raw/Zlib/CopyRect
decoding into a PNG written with `zlib` alone. No Pillow, numpy, or noVNC.

#### Watching something slow: `screenshot-burst`

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
distorted. Takes `--out` and `--upload` (and `--timeout`, `--url-expiry`)
like `screenshot`. Prefer this over a manual sleep-then-screenshot loop.

#### If VNC itself is the problem: `--via monitor`

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

### 2.3 You cannot see images — `--for-model` and `console inspect`

Both hand pixels to a vision model instead of to you. They share one bounded
image shape described once here; every other reference points back to this
table.

#### Getting the pixels back inline: `screenshot --for-model`

```bash
proxmox-lab console screenshot --vmid 9001 --for-model
```

Adds an `image` object to the JSON holding the screen as a base64 PNG, for a
caller that reads images itself but cannot open a file on this machine. It is
opt-in on purpose — an unrequested megabyte of base64 in every screenshot
would be hostile — and works on both `--via vnc` and `--via monitor`.

The image is compressed but deliberately not over-compressed. This is the
canonical shape (also used by `console inspect`'s fallback):

| Field | Meaning |
|---|---|
| `encoding`, `mime_type` | Always `base64` and `image/png` |
| `width`, `height` | What was actually emitted |
| `original_width`, `original_height` | The real framebuffer |
| `scale` | Emitted width ÷ original width; multiply coordinates back by it |
| `bytes`, `base64_bytes` | PNG size and encoded size |
| `base64` | The image. Absent if the cap could not be met — `error` says why |

A screen already within 1280 pixels on its longest edge is sent untouched at
`scale: 1.0`. Anything larger is box-averaged down to 1280 (a 1920x1080
desktop becomes 1280x720) and re-encoded at maximum zlib compression. Averaging
matters: nearest-neighbour resampling drops whole scanlines and destroys
8-pixel glyphs, which is the entire point of keeping the text readable. If the
result still exceeds a 1.5 MB base64 cap, the bound steps down until it fits,
never below 640 pixels — under that an 80-column line falls below 8 pixels per
glyph and stops being readable. A screen that cannot fit even at the floor
returns `error` instead of an unbounded blob; read the written PNG instead.

Only the fact and the byte size are audited. The image itself never enters the
journal.

#### Optional cloud vision: `console inspect`

```bash
proxmox-lab console inspect --lease "$L" --vmid 9001
```

Stores the untouched PNG locally and overlays a labelled 100-pixel X/Y
grid on a separate same-size model-input PNG. Later frames dim unchanged pixels
while changed regions stay bright with a magenta boundary. Flags: `--lease` and
`--vmid` (required), `--out`, `--settle` (default `2.0`), `--timeout` (default
`120`), `--max-tokens` (default `1024`), `--prompt`, `--provider
auto|nvidia|openrouter-nemotron|openrouter-free|kilo` (default `auto`), and
`--no-image-fallback` (see below).

Automatic mode races four routes and returns the first structurally valid answer:

| `--provider` | Route |
|---|---|
| `nvidia` | NVIDIA Nemotron Nano 12B v2 VL |
| `openrouter-nemotron` | The named OpenRouter Nemotron Omni free endpoint |
| `openrouter-free` | `openrouter/free` |
| `kilo` | The Kilo Code gateway's `kilo-auto/balanced` router |

Use `--provider` to test one stage. Automatic mode sends the screen to every
configured route concurrently. The guest must be registered to the given
lease. The command audits the selected provider/model but never the image,
prompt, or key. Free OpenRouter providers may log prompts for service
improvement, so do not submit confidential or personal screens.

`kilo-auto/balanced` is Kilo's balanced auto router: it picks a vision-capable
model on Kilo's side rather than pinning one here, so the provider does not
break the day a specific model is retired. The result records the router id
under `requested_model` and whichever concrete model actually answered under
`model`.

*Fallback shape.* `console inspect` still exits non-zero and still audits the
failure when every provider fails — a vision outage is never quietly reported
as success. But it also prints the screen as a base64 PNG under `image` in
exactly the [canonical shape above](#getting-the-pixels-back-inline-screenshot---for-model),
with the same bounds and cap. `vision_error` carries the reason. Pass
`--no-image-fallback` to suppress the blob and get the error alone.

This handback is intentionally separate from `screenshot`: external image
transmission must be explicit. The model's coordinates are advisory and still
pass through the cursor-calibration workflow before any click occurs.
Accordingly, a vision response recommending a click always reports
`actionable: false` and `requires_cursor_calibration: true`, even when its
JSON is structurally valid.

### 2.4 Running a command inside the guest — `console exec`

When the guest runs `qemu-guest-agent`, the cheapest path is not a screen at
all — it is `console exec`, which runs a process inside the guest over the
agent channel and returns real `exitcode`, `stdout`, and `stderr` as JSON.

```bash
proxmox-lab console exec --lease "$L" --vmid 9001 -- uname -a
proxmox-lab console exec --lease "$L" --vmid 9001 --shell -- "echo $HOME && ls -la"
proxmox-lab console exec --lease "$L" --vmid 9010 --windows -- "dir C:\\"
```

Flags: `--lease` and `--vmid` (required), `--shell` (wrap the command as
`/bin/sh -c "…"` or `cmd.exe /c "…"` with `--windows`), `--windows` (Windows
guest quoting), `--timeout` seconds (default `300`), then the command as
positional `command …`. The guest must be owned by the lease; the fact of the
execution (argv0, exit code) is audited, not the output. For long-lived or
file-oriented work, `guest run`/`push`/`pull` remain the bulk-transfer path;
`console exec` is the single-command analogue that lives under `console`
because it shares the same lease-owned, audited surface. Registration is at
[`src/proxmox_agent_lab/console.py:2222`](../src/proxmox_agent_lab/console.py#L2222).

## 3. Writing to the guest: `keys`, `type`, `click`

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

### Flags

* `keys`: `--lease`/`--vmid` (required), positional `keys …` (e.g.
  `ctrl-alt-delete f2 enter`), `--via vnc|api` (default `vnc`; `api` uses
  Proxmox `sendkey` when RFB input is unavailable), `--delay` (default `0.08`),
  `--screenshot-after SECONDS`, `--screenshot-out PATH` (also spelled `--out`).
* `type`: `--lease`/`--vmid`, `--text TEXT` or `--text-stdin` (read from stdin
  so a password never appears in `argv`, shell history, or the audit ledger;
  only the character count is recorded), `--enter`, `--delay` (default `0.012`),
  `--screenshot-after`, `--screenshot-out`/`--out`.
* `click`: `--lease`/`--vmid`/`--x`/`--y` (required), `--target` (short visible
  label of the intended control), `--empty-space` (click a known empty
  coordinate without target verification — omit `--target`), `--button
  1|2|3` (default `1`), `--double`, `--calibration-settle` (default `1.0`),
  `--vision-timeout` (default `45`), `--provider
  auto|nvidia|openrouter-nemotron|openrouter-free` (default `auto`),
  `--screenshot-after`, `--screenshot-out`/`--out`.

Clicks are refused outside the current screen bounds.

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

## 4. Liveness probes: `has-gui-locked-up`, `has-terminal-locked-up`

Two best-effort probes, for when a screen has looked the same for a while and
you need to know whether it's genuinely stuck or just quiet:

```bash
proxmox-lab console has-gui-locked-up --lease "$L" --vmid 9001
proxmox-lab console has-terminal-locked-up --vmid 9001 --samples 4 --interval 0.6
```

`has-gui-locked-up` moves the pointer to two different points a moment apart
and checks whether the screen changed either time, using the same pixel-diff
this project already uses for change highlighting — no cloud vision call.
This client declares no support for RFB's Cursor pseudo-encoding, so a
compliant server (QEMU's among them) draws the pointer into the framebuffer
itself rather than compositing it client-side; `console click`'s own
verification already depends on this same fact. `has-terminal-locked-up`
sends no input at all — it samples a text console several times and checks
whether anything changed, since a live console's cursor normally blinks on its
own; it refuses a screen `console screenshot` would not call text-mode.

Flags:

* `has-gui-locked-up`: `--lease`/`--vmid` (required), `--settle` (default
  `0.3`), `--timeout` (default `25.0`), `--threshold` per-channel change to
  count a pixel as different (default `24`).
* `has-terminal-locked-up`: `--vmid` (required; no `--lease` — it is a passive
  read), `--samples` number of passive captures (default `4`, minimum `2`),
  `--interval` seconds between captures (default `0.6`), `--timeout` (default
  `25.0`), `--threshold` (default `24`). The defaults sample for about
  two seconds; raise `--samples` or `--interval` for a longer window.

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
  with `console text --send` (§5).
- `vga: std` and Windows guests: VNC keyboard and pointer work normally.

Check with `qm config <vmid> | grep vga`, or just try `console text` first. A
template can be switched with `--data vga=std`, but the change needs a full
stop and start — a reset keeps the old display device. This is the only
`vga: serial0` note in this file; input sections elsewhere point here.

## 5. Serial text in depth: `console text`, `bridge`, and early-boot capture

```bash
proxmox-lab console text --vmid 9001 --send "ip -br a" --seconds 4
```

Attaches to the Proxmox terminal (`termproxy`) for an LXC container or a QEMU
guest that has a serial device, optionally sends one line, and returns the
output with escape sequences stripped. This is exact text. Prefer it over
looking at pixels whenever the guest has a real terminal.

A QEMU guest needs `serial0: socket` in its config for this path; cloud-init
templates in this lab are built with it.

### Flags

`--vmid` (required), `--kind qemu|lxc` (auto-detected if omitted), `--seconds`
(default `3.0`), `--timeout` (seconds to follow; default until Ctrl+C),
`--follow` (stream continuously), `--send` (send this line first, then read),
`--send-raw` (send exactly these characters with no trailing newline — for
kernel-debugger prompts such as KDB that act on bare characters), `--nudge`
(send a bare newline to redraw the prompt), `--from-reset` (with `--follow`:
attach first, then reset the guest, so output from t=0 is captured; requires
`--lease`; QEMU only), `--wait-for-guest SECONDS` (wait up to this long for
the serial terminal to exist, so a capture can be started before the guest is
powered on), `--lease` (optional; required with `--from-reset`).

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

```bash
proxmox-lab console bridge --lease "$L" --vmid 9001 --port 0
# flags: --lease/--vmid (required), --kind qemu|lxc (default qemu),
#        --host (default 127.0.0.1), --port (default 0 = pick a free one)
```

Bidirectional pipe between a local TCP port and the guest serial console: bytes
you type reach the guest (e.g. a KDB prompt), and guest output streams back.
Tip: `reset` restarts only the guest — the QEMU process and its serial socket
stay alive, so a connected bridge survives resets and captures output from t=0.
A stop/start replaces the QEMU process and drops the bridge.

## 6. Preflight and agentless serial login

### Preflight

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

It also reports screen-reading readiness under `vision`: `any_provider_key`,
plus a `provider_keys` breakdown of which of `nvidia`, `openrouter` and `kilo`
have a key this install can reach. Only whether a key is present — never the
key. With none of them, use `console screenshot --for-model` and read the
image yourself.

### Serial login on agentless guests

Generic cloud images ship without qemu-guest-agent. `TermSession` therefore
supports driving a getty directly:

- `expect(patterns, poke=True)` — read until a pattern appears, nudging an idle
  console with newlines so it redraws a prompt drawn before we attached;
- `login(user, password)` — log in without echoing the password anywhere. It
  accepts an empty password, and returns as soon as a shell prompt appears, so
  a guest that never asks for one (an installer, a rescue shell, a blank-root
  appliance) logs in rather than waiting out the timeout;
- `run(command)` — run one command, using an echoed sentinel so completion is
  unambiguous even when the command prints nothing.

This is how the VPN gateway bootstraps its agent, and how `net leak-test` runs
inside an Alpine guest that has no agent at all.

#### An empty console password is a credential

`guest run --password-stdin` and `net leak-test --password-stdin` distinguish
*no password was supplied* from *an empty password was supplied*. The second is
a real credential — plenty of guests have no password at all — and is accepted,
but only when the flag says so explicitly:

```bash
proxmox-lab guest run --lease "$L" --vmid 9001 --password-stdin \
  -- uname -a </dev/null
```

Omitting `--password-stdin` on `guest run` still refuses the serial channel
with the old message rather than silently trying a blank login, so a caller who
simply forgot the flag is not quietly given one. (`net leak-test` requires the
flag, so there the only question is what is fed to it.)

The commands that *create* a credential are deliberately stricter and reject an
empty one: `api --password-stdin` would write a blank password into a Proxmox
object, and `windows install --password-stdin` would build a blank-password
Administrator account into the answer file. Omit the flag there to have a
strong password generated instead.

## Appendix: Why there is no OCR

Earlier versions decoded VGA text screens by matching each character cell
against a font table. It is gone; see
[issue #95](https://github.com/jr551/proxmox-agent-lab/issues/95). The decoder
could only read a guest whose console font the controller happened to hold; a
guest shipping its own font — ReactOS setup, for one — decoded to a wall of
replacement characters at 0.003 confidence, which is worse than useless because
it looks like an answer. The general fix does not exist: any guest can draw
any glyph it likes. So reading a screen is a model's job now — see §2.

`console screenshot --ocr` and `console import-font` remain registered only so
that an upgrade fails with an explanation rather than `unrecognized arguments`;
both are deleted in 0.11.0.

