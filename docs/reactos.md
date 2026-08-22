# Debugging ReactOS guests

ReactOS is one of the harder guests to drive from here. It has no
qemu-guest-agent, no SSH, and no answer-file equivalent, so the only two
channels that work are the graphical console and a serial port — and the
serial port is useless unless the guest was built and booted to use it.

This page collects the operating knowledge from one intensive workstream:
bringing up VirtIO storage and graphics drivers on an amd64 ReactOS build and
getting it to install under both UEFI and legacy BIOS. It is not an exhaustive
ReactOS debugging manual, and it says nothing about ReactOS internals beyond
what the lab has to know. Treat it as the set of traps that have already cost
lab time, so the next person spends theirs on the actual bug.

Everything here assumes the ordinary rules: a lease for every mutation,
heartbeats while a long run is in progress, and guests that egress only through
the VPN gateway on `vmbr1`. See [AGENTS.md](AGENTS.md) and
[safety-policy.md](safety-policy.md).

## Getting ReactOS to talk

Serial is the primary debugging channel. A ReactOS boot entry with
`/DEBUGPORT=COM1` routes the kernel debugger's *output* to COM1, which
`console text` can read as exact characters. That is enough for boot messages,
`DPRINT` output and assertion text, and it should be the default for every
run: a graphical screenshot of a driver failure tells you far less than the
serial log printed alongside it.

Output alone is not enough once the kernel actually breaks into KDB. KDB reads
its *input* from the PS/2 keyboard unless `/KDSERIAL` is also set —
`ntoskrnl/kd/kdterminal.c` gates serial input on `KD_DEBUG_KDSERIAL`. Without
that flag a break is close to invisible and completely undrivable:

- Under a graphics-mode framebuffer the prompt is never painted, because KDB
  writes to the legacy text buffer at `0xB8000`, which reads back as zeros.
- Keys injected over VNC do not reach it either. RFB key events are delivered
  to the emulated i8042 controller in a way that does not set the
  output-buffer-full bit KDB polls for, so every keystroke is silently
  discarded.

The symptom is a guest that has stopped making progress with one vCPU spinning
in `KdbpTryGetCharKeyboard()`, and no way to ask it anything.

The upstream boot entries (`LiveImg_Debug`, `Setup_Debug`) carry
`/DEBUGPORT=COM1` but not `/KDSERIAL`, so a stock ISO cannot be driven this
way. The fix belongs in the build, not in the lab: have the builder synthesise
an **additional** `bootcd.ini` section — a new `[LiveImg_KdSerial]` or
`[Setup_KdSerial]` entry whose options include `/KDSERIAL` — and select it with
the default-OS setting. Adding a new section rather than editing the existing
ones matters for two reasons: upstream's own entries stay untouched, so the
change can never leak into a patch you intend to submit, and the plain entries
remain available for comparison in the same ISO. The synthesised entries are
only meaningful in a `KDBG=1` build, so make the build fail closed when they
are requested without it rather than discover it an hour later.

## Check where the disks actually are before benchmarking

The rule is ISO on bulk storage, guest disk on fast storage. A live
virtio-vs-IDE comparison broke it: **both** guests' disks were on the USB bulk
directory store, which measures about 25 MB/s sequential write, so the run
mostly measured the USB bus.

`storage status` now reports a `class` per storage, and a write that puts a
guest disk on the bulk store warns unless `--slow-storage-accepted` is passed.
Assert it before trusting any number:

```bash
proxmox-lab storage status | grep -A2 '"class": "bulk"'
proxmox-lab api --lease "$L" --method GET \
  --path "/nodes/$NODE/qemu/$VMID/config" | grep -E 'scsi0|virtio0|ide0'
```

If a disk resolves to the bulk store, move it before benchmarking — or state
plainly that the figure is a floor, not a comparison.

## The serial socket has no scrollback

A QEMU guest needs `serial0: socket` in its config for `console text` to work
at all. Given that, the socket delivers only what arrives while you are
attached. Anything the guest printed before that is gone — there is no buffer
to replay.

This is the single most expensive trap on this list. Several early runs
attached a capture after the guest had already booted, saw an empty log, and
concluded that COM1 was not wired up or that the ReactOS build had debugging
compiled out. Both conclusions were wrong; the output had simply already
happened. Attach before or at power-on — and note both flags, because
Proxmox refuses to open a terminal for a stopped guest, so the naive ordering
used to exit immediately with `VM not running`:

```bash
proxmox-lab console text --vmid "$VMID" --follow --timeout 900 \
  --wait-for-guest 300 > run.log 2>&1 &
proxmox-lab api --lease "$L" --method POST \
  --path "/nodes/$NODE/qemu/$VMID/status/start"
```

`--wait-for-guest` retries the attach until the serial line exists; `--follow`
is what streams for the whole window (a plain `--seconds 900` read returns as
soon as the output pauses). This still leaves a one-poll-interval gap at the
very start. When the first bytes matter absolutely, take the reset route
instead — `console text --follow --from-reset --lease "$L"` attaches first and
then restarts the guest inside the live session, which is the only way to be
certain of t=0.

**One `console text` session per VM.** A second concurrent session on the same
guest does not error — it just receives nothing, while the first keeps all the
bytes. Stop a running capture before starting another one or before using
`--send`. If a capture comes back empty, the first hypothesis should always be
that it never attached, not that the guest printed nothing; force some traffic
and check again before writing anything down.

## Driving KDB safely

KDB has two prompt styles and they consume input differently.

At the `(boipt)?` prompt — the one offered immediately after a break, where the
letters in the parentheses are the choices — **KDB acts on single characters as
they arrive.** Sending the word `bt` is therefore read as `b` (break
repeatedly) followed by `t` (terminate thread), and the thread you were trying
to inspect is gone. That is how one run lost the ReactOS Setup thread it was
debugging.

Word commands such as `bt` are safe only at a bare `kdb:>` prompt. So the rule
is: read the captured serial log to see which prompt you are actually at before
sending anything, send exactly one character at a `(boipt)?` prompt, and keep
word commands for `kdb:>`.

## Driving the guest UI

ReactOS has no agent, so the graphical console is the only way to reach Setup,
the desktop and the control panel applets.

The subcommand is **`console keys`**, plural. There is no `console key`. The
singular form fails with an argparse error, and inside a compound command that
error is easy to miss — the command appears to run and every keystroke is
silently lost. This cost several boots before anyone noticed. Always confirm
with `--screenshot-after`:

```bash
proxmox-lab console keys --lease "$L" --vmid "$VMID" --delay 0.6 up up enter \
  --screenshot-after 4
```

Prefer the keyboard over clicking. `console click` requires a `--target` naming
a visible control and passes the proposed coordinate to a vision model that
must independently identify exactly one matching control; it refuses to click
empty space and sometimes times out. That guard is correct, but it makes
clicking a poor primitive for a desktop where much of what you want is not a
labelled button. Two keyboard habits cover most of it:

- **Start → Run** is a reliable way into any control panel applet without a
  desktop right-click: `ctrl-esc`, then `r`, then type the applet name
  (`desk.cpl` for Display Properties).
- **Arrow-key menu navigation counts disabled items.** A ReactOS context menu
  steps through greyed-out entries such as *Paste* and *Paste shortcut* just
  like enabled ones, so count them when working out how many presses a target
  needs, and screenshot rather than assume.

The FreeLoader boot menu is the tightest timing in the whole workflow: it shows
for about **five seconds**, and any key stops the countdown. Reset the guest,
wait roughly 1.6 seconds, then send keys. At three seconds you have usually
already missed it.

```bash
proxmox-lab api --lease "$L" --method POST \
  --path "/nodes/$NODE/qemu/$VMID/status/reset"
sleep 1.6
proxmox-lab console keys --lease "$L" --vmid "$VMID" --delay 0.15 \
  up up up up up --screenshot-after 1
```

Count entries from the highlighted default rather than from the top of the
list, and take the screenshot before pressing Enter.

## Telling a hung guest from a waiting one

Never poll for a target value with an open-ended wait. Every early long wait in
this workstream was written as "loop until the disk has been written N bytes",
and every one of them sat there indefinitely the first time the guest froze —
burning lease time and, worse, producing no information about *where* it froze.

Watch for **change** instead, and report a stall. Sample the guest's counters
periodically and compare against the previous sample:

```bash
proxmox-lab api --method GET \
  --path "/nodes/$NODE/qemu/$VMID/status/current"
```

The useful fields are `diskread`/`diskwrite` and `cpu`. If the disk counters do
not move for some threshold — two minutes is a reasonable default for a
ReactOS install — stop waiting and report a stall, distinguishing that outcome
from "reached the target" and from "still progressing when the overall timeout
expired". Those three outcomes want three different next actions.

### `diskwrite` can read 0 on a guest that is writing hard

**Do not treat a still `diskwrite` as proof of a stall.** On a qcow2 image over
directory-backed storage the counter has been observed sitting at **0 bytes for
an entire session** while the guest was demonstrably writing. It is a cached,
summed value that does not update in real time for every storage backend
combination, so "the counter did not move" and "the guest did nothing" are two
different statements and only one of them is measured.

Two further traps in the same area:

- qcow2 growth can be **metadata-only** — L1 and refcount tables allocated for
  a large sparse image — so a file that grew by megabytes may carry no guest
  data at all.
- `ls -la` reports a sparse image's **apparent** size, which for an untouched
  100 GB qcow2 is 100 GB of I/O that never happened. `du` reports the
  **allocated** bytes, which is the number you want.

Cross-check before concluding anything, with a command that samples twice and
compares three independent signals:

```bash
proxmox-lab guest disk-activity --vmid "$VMID"                    # counter only
proxmox-lab guest disk-activity --lease "$L" --vmid "$VMID" \
  --ground-truth --interval 10
```

`--ground-truth` adds QEMU's own block-layer counters (`info blockstats` over
the Proxmox monitor endpoint — no SSH needed) and `du --block-size=1` on the
backing image file (over the opt-in `[memflow]` host SSH channel), then reports
a `disagreement` list naming any signal that saw nothing while another saw
bytes. That list is the diagnostically useful part: it is what tells you the
counter is lying rather than the guest being dead. Either extra signal may be
unavailable — the monitor endpoint needs a privilege the `PVEVMAdmin` lab token
does not have, and `du` needs the host SSH opt-in — and the command reports
that and returns the rest rather than failing.

Without `--ground-truth` the command deliberately reports `"writing": null`
rather than `false` for a still counter, because that counter on its own cannot
tell an idle guest from a stalled counter.

A `cpu` reading near **0.5 on a 2-vCPU guest** means exactly one core is
spinning: the guest is not idle and not making progress, which is the signature
of a busy-wait such as the KDB keyboard poll described above. To see where it
is spinning, use memflow, which needs no agent in the guest:

```bash
proxmox-lab memflow registers --lease "$L" --vmid "$VMID"
proxmox-lab memflow trace     --lease "$L" --vmid "$VMID" --steps 20
```

The one thing a stall detector cannot tell you: **a guest sitting at an
installer dialog waiting for input looks identical to a hang.** ReactOS Setup
paused on a partition prompt writes no disk blocks, exactly like a frozen
kernel. Always take a screenshot before concluding the guest is dead. This is
the same failure mode documented for Windows Setup in [windows.md](windows.md),
and it has the same answer: look at the screen.

## VM shapes that work

These are measured on an amd64 build, not deductions from configuration.

| Path | Firmware | Machine | Disk | Notes |
|---|---|---|---|---|
| UEFI + VirtIO | `bios=ovmf` with a fresh `efidisk0` (`efitype=4m`, no pre-enrolled keys) | `machine=pc` | `virtio0` | The target configuration for VirtIO work |
| Legacy regression | `bios=seabios` | `machine=pc` | `ide0`, no `efidisk0` | Installs and boots to a desktop; use it to prove a patch has not broken the legacy path |

`machine=pc` (i440fx) is deliberate in both cases. Under `q35` the guest stalls
at `Loading boot drivers...` and never reaches Setup, so an otherwise healthy
ISO looks broken. This is a compatibility workaround, not a statement about
what ReactOS ought to support.

The rest of the shape:

- `serial0=socket` always, on every ReactOS VM, whatever else you are testing.
- `vga=virtio` when the virtio-GPU driver is under test, `vga=std` otherwise.
  Keyboard input over RFB needs a real display device, so never use
  `vga=serial0` on a guest you intend to drive.
- `ostype=wxp`.
- The NIC stays `link_down=1` unless the run genuinely needs egress. When it
  does, egress goes through the VPN gateway on `vmbr1` and never `vmbr0` —
  see [network.md](network.md).
- Keep the ISO on bulk storage and the guest disk on fast storage, as in
  [storage.md](storage.md).

## Build-loop discipline

A full ReactOS build is **40 to 60 minutes**, which changes the economics of
everything around it. Three habits follow from that:

1. **Review the source change before building it.** A defect a code review
   would have caught costs an hour of build plus the lab run that was going to
   validate it. Reading the touched driver files in full is cheap by
   comparison.
2. **Never idle while a build runs.** Advance the next piece of work and check
   the build log between tasks rather than polling it in a loop. Watch for
   `CMake Error` in the first couple of minutes — a configure failure surfaces
   immediately and saves the other fifty-eight.
3. **Batch features into one build.** Several independent changes in a single
   ISO is almost always the right trade, even when it makes attribution
   slightly harder, because a second build costs another hour of lease time.

One build-configuration fact is worth stating here because it will otherwise be
rediscovered the slow way: **`KDBG=1` requires a Debug build.** `KDBG=1` with a
Release build does not link, failing on undefined references to
`ExpKdbgExtIrpFind` and `ExpKdbgExtHandle`, which exist only in Debug. Since
`/KDSERIAL` needs `KDBG=1`, any run you intend to drive through KDB is a Debug
build. Have the build script reject the combination up front.

## Reading results

The most useful independent signal the lab gives you is a screenshot's **pixel
dimensions**. `console screenshot` reports the size of the guest's real
scanout, so a display mode change can be confirmed from outside the guest
regardless of what the guest's own UI claims. That is how a virtio-GPU mode
switch was shown to be genuinely reprogramming the hardware even though the
resulting screen was black — which moved the investigation from "the mode
change fails" to "nothing is being painted after it succeeds", a completely
different bug.

Screenshots are read-only and take no lease:

```bash
proxmox-lab console screenshot --vmid "$VMID" --settle 3
```

For the serial log, a handful of greps are worth running over every capture
before reading it in detail. Use `grep -a`, because a serial log routinely
contains binary noise and GNU grep will otherwise decide it is not a text file:

```bash
grep -acE "ASSERT_FAILURE|Assertion failed"   run.log   # any assertion at all
grep -aiE "viostor|storport|virtio_gpu"       run.log   # driver-specific tags
grep -acE "IOCTL_[A-Z_]+ failed"              run.log   # rejected IOCTLs
```

Beyond that, grep for the exact `file.c(line)` or `file.c:line` string of any
assertion you are tracking. Those strings are stable across builds and make a
good regression check: a fix has worked when the string is gone from the log,
not when the guest merely gets further.

## Evidence discipline, and three diagnoses that were wrong

Do not describe anything as verified without a captured log or screenshot
behind it, and when a diagnosis turns out to be wrong, correct the earlier
write-up in place rather than leaving a confident falsehood in the record for
the next person to build on. Record the ISO and patch hashes alongside each
run's evidence so a claim can be traced back to the exact artifact that
produced it, and treat any artifact a run has cited as immutable from then on.

That discipline is not theoretical. Three diagnoses in this workstream were
stated with confidence and were wrong:

1. **A fix was credited with resolving a downstream assertion it had nothing to
   do with.** An oversized allocation was found and fixed, and the assertion
   that was assumed to follow from it kept firing afterwards. Two real defects
   had been collapsed into one causal story on no evidence beyond plausibility.
2. **An informational message was read as a failure.** A storage port driver
   logs "No driver object extension!" and then creates the extension on the
   next line. The message is normal; a run was spent chasing it.
3. **An error was attributed to the driver under test when a different driver
   emitted it.** A `HwFindAdapter` failure blamed on a new virtio-GPU miniport
   was actually the legacy VGA driver failing, which is expected when the VM is
   configured with `vga=virtio`. The real fault was elsewhere entirely.

Two transferable lessons come out of all three. First, **identify which
component emitted a message before attributing it** — on a serial log with
several drivers logging into the same stream, the nearest plausible owner is
frequently not the actual one. Second, **confirm that a fix changed the
observed symptom before believing the causal chain**; a fix that is correct in
isolation is not evidence that it was the fix for the thing you were chasing.
