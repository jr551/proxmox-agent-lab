# GUI installer playbook

This deliberately small workflow is for agents driving an operating-system
installer. It is designed to work with inexpensive models: the tool owns the
safety and observation loop; the model only identifies the current screen and
chooses one next action.

## The loop

1. Capture one full-screen checkpoint and read the PNG directly.
2. Name the screen in a few words: `boot menu`, `language`, `disk setup`,
   `copying`, `finished`, or `unknown`.
3. Choose **one** keyboard action or click and state the expected next screen.
4. Send the action and capture the settled result in the same command:

   ```bash
   proxmox-lab console keys --lease "$L" --vmid "$VMID" enter \
     --screenshot-after 3

   proxmox-lab console click --lease "$L" --vmid "$VMID" --x 640 --y 412 \
     --screenshot-after 3
   ```

5. Read the returned PNG. Record `screen -> action -> observed screen`, then
   repeat.

The first coordinate click at each framebuffer resolution is a two-step
calibration. `console click` moves the visible cursor to the target and returns
a checkpoint **without pressing the button**. After vision confirms the cursor
is on the intended control, repeat the same command with
`--confirm-calibration`. That calibration is kept for this lease and VM until
its reported resolution changes, when clicking becomes two-step again.

Do not crop, sharpen, recolour, or run external OCR over a graphical installer.
Those transformations discard context and turn one uncertain observation into
dozens of guesses. `console screenshot --ocr` is only for a VGA text grid and
will refuse a graphical screen.

If the active model cannot see images, delegate **only the current full-screen
checkpoint** to a vision-capable model and ask it for the screen name, relevant
controls, and safest single next action. Keep lease ownership and all mutations
in the primary agent. Tesseract is not a substitute for vision on a graphical
desktop or installer.

## Hard limits

- Never make more than three attempts on an unchanged screen.
- Never sweep coordinates or click controls whose purpose is unknown.
- Never pass `--confirm-calibration` without reading its cursor checkpoint.
- After an unchanged action, run `guest probe`; confirm `keyboard_input` and
  take a fresh full screenshot before trying a different input path.
- After three unchanged attempts, stop and report the exact screen and actions.
  Do not spend the rest of the context window manufacturing image variants.
- Reuse the existing lease and VM when resuming. Do not repeat downloads,
  clones, or disk creation unless current state proves they failed.
- Send a progress update at each major screen or at least every five minutes.
  Heartbeat the lease when work reaches 30 minutes.

## Before touching the installer

- Let `lease-begin` use `power.boot_timeout_seconds`. A cold host is given at
  least 90 seconds; a smaller explicit timeout is rejected.
- Use an ordinary lease unless the user explicitly asked to retain the machine.
- Verify the target VM belongs to the lease before changing its disk.
- Prefer a purpose-built template. If a fresh install is required, verify the
  image checksum and keep the installer ISO on bulk storage and the guest disk
  on fast storage.
- Quote API values containing shell metacharacters, especially boot order:

  ```bash
  proxmox-lab api --lease "$L" --method PUT \
    --path "/nodes/$NODE/qemu/$VMID/config" \
    --data 'boot=order=scsi0;ide2'
  ```

## Haiku checkpoint map

Haiku has no qemu-guest-agent, so its graphical console is the source of truth.
Treat these as checkpoints, not coordinates; labels and layout may change.

1. Boot the verified anyboot ISO and reach the language/welcome screen.
2. Choose **Install Haiku**, not the live desktop, unless the installer must be
   launched from the desktop.
3. At destination selection, open DriveSetup only if no usable BFS destination
   exists. Confirm the lease-owned virtual disk by size; never select the CD.
4. Create/format a BFS partition on that virtual disk, return to Installer, and
   start the copy.
5. Wait while copying; do not click a progress screen. Check at sensible
   intervals and heartbeat the lease if necessary.
6. On completion, power the guest down, detach or demote the ISO, put the guest
   disk first in boot order, and boot once from disk.
7. A Haiku desktop reached from the guest disk is install proof. Record the
   final screenshot. It is not guest-agent or command-execution proof.

For a retained development machine, use a long-term lease only because the
user asked for persistence and report that the host remains powered on. For a
disposable install test, end the lease and require `host_powered_off=true`.
