# Console access, screenshots and text

## Purpose

Read and drive a guest screen without installing extra tools. This covers
screenshots, exact serial text, keystrokes, pointer clicks, and a cloud-vision
shortcut. Reads need no lease; anything that mutates a guest (input, `exec`)
requires an active lease and is audited.

## Prerequisites

- An active lease for any command that sends input or runs a guest command.
- A vision API key stored for `console inspect` (NVIDIA, OpenRouter, or Kilo).
  Without one, use `console screenshot --for-model`.
- For `console text`, the QEMU guest needs `serial0: socket` in its config.
  Cloud-init templates in this lab are built with it.
- For `console screenshot --via monitor`, the opt-in `[memflow]` host SSH
  channel is required and the lab token needs `Sys.Audit|Sys.Modify` on the VM
  path.

## Choosing a channel

Pick the cheapest channel that answers the question.

| Situation | Use | Why |
|---|---|---|
| Guest is a Linux shell, LXC, or has a serial console | `console text` | Returns the exact character stream from Proxmox |
| Guest runs qemu-guest-agent | `console exec` | Real exit codes, stdout and stderr |
| Graphical screen: installer, desktop, BIOS, boot menu | `console screenshot` | A multimodal model reads the PNG directly |
| You cannot see images yourself | `console screenshot --for-model` or `console inspect` | Hands the screen to a model that can |

## Minimal example

```bash
L=$(proxmox-lab lease-begin --purpose "console demo" \
    | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
trap 'proxmox-lab lease-end --lease "$L"' EXIT

proxmox-lab guest clone --lease "$L" --template 9000 --newid 9101
proxmox-lab guest probe --vmid 9101

# a terminal guest
proxmox-lab console text --vmid 9101 --send "ip -br a" --seconds 4

# a graphical guest
proxmox-lab console screenshot --vmid 9101 --settle 2
proxmox-lab console type --lease "$L" --vmid 9101 --text-stdin --enter <<< "hello"
```

## Expected result

`console screenshot` writes a PNG under the state directory and prints its
path, dimensions, and whether the screen looks like a text console:

```json
{
  "path": "/…/vm-9101-… .png",
  "width": 1280,
  "height": 720,
  "text_mode": false
}
```

`console text` returns the exact text the guest sent, with Proxmox transport
records stripped:

```json
{
  "text": "1: lo: <LOOPBACK,UP,LOWER_UP> …\n2: ens18: …\n",
  "bytes_read": 1234
}
```

`console type`/`console keys` report `characters_sent` or `keys_sent`. With
`--screenshot-after SECONDS` they also report `screen_changed`:

- `true` — the framebuffer differs from the previous capture.
- `false` — pixel-identical; an `agent_hint` suggests what to check.
- `null` — no earlier capture to compare.

`screen_changed` is evidence, not proof. Stop and re-read the screen before
sending more input when it is `false`.

`console exec` returns real `exitcode`, `stdout`, and `stderr` as JSON when the
guest runs `qemu-guest-agent`.

## Reading a screen

A screen is read by a model, never by glyph matching.

### `console text` — the guest is a terminal

```bash
proxmox-lab console text --vmid 9001 --send "ip -br a" --seconds 4
```

Exact and cheap; always better than looking at pixels. Proxmox hands over the
guest's real character stream.

Flags: `--vmid` (required), `--kind qemu|lxc` (auto-detected), `--seconds`
(default `3.0`), `--timeout`, `--follow`, `--send`, `--send-raw`, `--nudge`,
`--from-reset` (requires `--lease` and QEMU), `--wait-for-guest SECONDS`,
`--lease` (optional; required with `--from-reset`).

For early-boot capture, `--from-reset` attaches first, then triggers a guest
reset, so output from `t=0` lands in the stream. A `stop`/`start` instead of a
reset replaces the QEMU process and drops the serial socket.

### `console screenshot` — the guest is graphical

```bash
proxmox-lab console screenshot --vmid 9001 --settle 2
```

Writes a PNG to the state `screens/` directory. Flags: `--vmid` (required),
`--out`, `--settle` (default `0.0`), `--timeout` (default `25.0`), `--upload`
(also place in S3 and print a presigned URL), `--url-expiry` (default `3600`),
`--via vnc|monitor` (default `vnc`), `--lease` (required with `--via monitor`),
and `--for-model` (see below). `--ocr` is a deprecated signpost that errors
with a pointer.

The capture path is a self-contained RFB client: Proxmox `vncproxy`, a WebSocket
upgrade, RFB 3.8 with VNC authentication, and Raw/Zlib/CopyRect decoding into a
PNG written with `zlib` alone. No Pillow, numpy, or noVNC.

#### `screenshot-burst` for slow changes

```bash
proxmox-lab console screenshot-burst --vmid 9001 --count 6 --interval 10
```

One VNC session stays open and captures `--count` frames (default 6) spaced
`--interval` seconds apart (default 10), then stitches them left to right into a
single PNG with each frame's elapsed seconds stamped in the corner. Frames are
never scaled or cropped, so a resolution change mid-sequence is preserved rather
than distorted. Prefer this over a manual sleep-and-screenshot loop.

#### `--via monitor` fallback

```bash
proxmox-lab console screenshot --vmid 9001 --via monitor --lease "$L"
```

Asks QEMU for a `screendump` through the Proxmox monitor endpoint, fetches the
PNG, and deletes the host copy. This writes a file on the Proxmox host, so it
is deliberately narrow:

- the host path is fixed and lease-scoped;
- the only format requested is PNG and the bytes are verified;
- the file is deleted in a `finally` path;
- it needs `--lease`, an owned QEMU guest, and the opt-in `[memflow]` host SSH
  channel;
- only the fact of the capture is audited.

### `--for-model` and `console inspect`

When you cannot see images, hand pixels to a vision model.

`console screenshot --for-model` returns the screen inline as a bounded base64
PNG in the JSON `image` object:

| Field | Meaning |
|---|---|
| `encoding`, `mime_type` | `base64` and `image/png` |
| `width`, `height` | What was emitted |
| `original_width`, `original_height` | The real framebuffer |
| `scale` | Emitted width ÷ original width |
| `bytes`, `base64_bytes` | PNG size and encoded size |
| `base64` | The image; absent if the cap could not be met |

A screen already within 1280 pixels on its longest edge is sent untouched at
`scale: 1.0`. Larger screens are downscaled to 1280, then re-compressed, never
below 640. Only the fact and byte size are audited.

`console inspect` sends the screen to a configured vision provider:

```bash
proxmox-lab console inspect --lease "$L" --vmid 9001
```

It races NVIDIA Nemotron, OpenRouter, and Kilo in `auto` mode, or use
`--provider` to test one route. If every provider fails, it still returns the
screen as a bounded base64 PNG under `image` (unless `--no-image-fallback`). The
model's coordinates are advisory and still pass through cursor-calibration
before any click occurs.

## Writing to the guest: `keys`, `type`, `click`

```bash
proxmox-lab console keys  --lease "$L" --vmid 9001 enter f2 ctrl-alt-delete
proxmox-lab console click --lease "$L" --vmid 9001 --target "Install" --x 640 --y 412
proxmox-lab console type  --lease "$L" --vmid 9001 --text-stdin --enter
```

Add `--screenshot-after SECONDS` to any input command to get delivery evidence.

- `keys`: positional keys (e.g. `enter f2 ctrl-alt-delete`), `--via vnc|api`
  (default `vnc`), `--delay` (default `0.08`), `--screenshot-after`,
  `--screenshot-out` / `--out`.
- `type`: `--text TEXT` or `--text-stdin`, `--enter`, `--delay` (default
  `0.012`), `--screenshot-after`, `--screenshot-out` / `--out`.
- `click`: `--x`/`--y` (required), `--target` (visible label), `--empty-space`
  (omit `--target` for a deliberate background click), `--button 1|2|3`
  (default `1`), `--double`, `--calibration-settle` (default `1.0`),
  `--vision-timeout` (default `45`), `--provider`, `--screenshot-after`,
  `--screenshot-out` / `--out`.

By default, a click names a visible target. Cloud vision independently matches
the label and coordinate before pressing the button. If it fails, `clicked` is
`false` — stop and inspect. For a click on known empty space, pass
`--empty-space` and omit `--target`; it bypasses target verification, remains
bounds-checked, and is audited.

Clicks outside the current screen bounds are refused.

### Keyboard input needs a VGA display

RFB key events go to the emulated PS/2 keyboard. A VM with `vga: serial0`
(which most Linux cloud templates use) will give a screenshot but silently
discard VNC keystrokes. `guest probe` reports this as
`"keyboard_input": false`. Drive these with `console text --send` instead.

## Running a command inside the guest — `console exec`

```bash
proxmox-lab console exec --lease "$L" --vmid 9001 -- uname -a
proxmox-lab console exec --lease "$L" --vmid 9010 --windows -- "dir C:\\"
```

When the guest runs `qemu-guest-agent`, this is the cheapest command channel.
Flags: `--lease`, `--vmid` (required), `--shell` (wrap as `/bin/sh -c`),
`--windows` (Windows quoting), `--timeout` (default `300`), then the command as
positional `command …`.

## Liveness probes

`console has-gui-locked-up` and `console has-terminal-locked-up` are best-effort
probes for when a screen has looked the same for a while.

```bash
proxmox-lab console has-gui-locked-up --lease "$L" --vmid 9001
proxmox-lab console has-terminal-locked-up --vmid 9001 --samples 4 --interval 0.6
```

A static screen is good evidence of a hang but not proof. Both return
`"locked_up"` alongside per-sample deltas and a `"caveat"` when the verdict is
`true`.

## `console bridge` — serial over a local TCP port

```bash
proxmox-lab console bridge --lease "$L" --vmid 9001 --port 0
```

Exposes the guest serial console on a local TCP port. `nc 127.0.0.1 <port>` is
a full interactive terminal. Flags: `--lease`/`--vmid` (required), `--kind`
(default `qemu`), `--host` (default `127.0.0.1`), `--port` (default `0` = pick
free). Resetting the guest keeps the QEMU process alive, so a connected bridge
survives it.

## Preflight and agentless serial login

`console preflight` reports capabilities rather than raw privilege names,
including screen-reading readiness under `vision`.

```bash
proxmox-lab console preflight
```

Generic cloud images ship without `qemu-guest-agent`. `TermSession` can drive a
getty directly through `expect`, `login`, and `run`, which is how the VPN
gateway bootstraps and how `net leak-test` works in an agentless Alpine guest.

## Cleanup

Most console operations are stateless and need no explicit cleanup. The
exceptions are:

- `console bridge` holds the serial WebSocket open. End it with Ctrl-C or close
  the client; ending the bridge clears the `termproxy`/`qm terminal` processes
  on the node.
- `console screenshot --upload` writes a presigned URL and an S3 object; that
  object is scratch space and can be left to expire or deleted with `s3 delete`.
- PNG files written by `screenshot` and `screenshot-burst` live in the state
  `screens/` directory. Remove them when no longer needed.

A guest the current lease did not create is not drivable. The refusal is raised
before anything is transmitted and names the command that fixes it:

```
VMID 9246 existed before this lease; register it with 'proxmox-lab
lease-register --lease <id> --kind qemu --vmid 9246 --allow-existing' if you
intend to drive it
```

## Common failures

| Symptom | Diagnostic | Next step |
|---|---|---|
| Typing has no effect | `guest probe --vmid <id>` | If `keyboard_input: false`, the guest is `vga: serial0`; switch to `console text` |
| `console screenshot` is pixel-identical after input | Re-run with `--screenshot-after 3` | Stop and re-read the screen before sending more input |
| `console text` is empty on a stopped guest | Check guest is running, or use `--wait-for-guest SECONDS` | Start the guest first |
| `--via monitor` returns `Permission check failed` | `proxmox-lab console preflight` | Grant `Sys.Audit|Sys.Modify` on the VM path, or use VNC |
| `console inspect` returns `vision_error` | `proxmox-lab console preflight` | Add a vision key, or use `console screenshot --for-model` |
| `--for-model` returns `error: exceeded size cap` | Read the PNG from `path` instead | A very dense screen can exceed the 1.5 MB base64 cap; open the file |

For the full symptom-decision list, see [troubleshooting.md](troubleshooting.md).

## Advanced usage

- `console text --follow --from-reset --lease "$L"` captures boot output from
  `t=0` on a QEMU guest.
- `console text --send-raw "cont"` transmits exactly the given characters with
  no trailing newline, for debugger prompts.
- `console screenshot-burst --count 10 --interval 5` watches a long installer.
- `console click --target "Install" --x 640 --y 412 --screenshot-after 3` is the
  bounded GUI-installer loop; see [gui-installers.md](gui-installers.md).

## Verification status

See [VERIFICATION.md](VERIFICATION.md) for what has been observed on real
hardware (VNC screenshots, serial text, `console inspect` with OpenRouter/Kilo)
and what remains unit-tested only (`--for-model` and `--via monitor` against a
real guest framebuffer, the monitor path itself).

## Appendix: why there is no OCR

Earlier versions decoded VGA text screens by matching each character cell
against a font table. It is gone; see
[issue #95](https://github.com/jr551/proxmox-agent-lab/issues/95). The decoder
could only read a guest whose console font the controller happened to hold; a
guest shipping its own font decoded to a wall of replacement characters, which
is worse than useless because it looks like an answer. The general fix does not
exist: any guest can draw any glyph it likes. Reading a screen is a model's job.

`console screenshot --ocr` and `console import-font` remain registered so an
upgrade fails with an explanation rather than `unrecognized arguments`. Both now
error with a pointer to the screen-reading paths above; they will be removed only
when release notes announce a deliberate removal.

## See also

- [gui-installers.md](gui-installers.md) — driving installers from the screen
- [AGENTS.md](AGENTS.md) — agent guidance on choosing a channel
