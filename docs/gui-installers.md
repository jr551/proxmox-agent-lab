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

   proxmox-lab console click --lease "$L" --vmid "$VMID" \
     --target "Install" --x 640 --y 412 \
     --screenshot-after 3
   ```

5. Read the returned PNG. Record `screen -> action -> observed screen`, then
   repeat.

When a cloud vision key is stored, use the guarded wrapper as the first
graphical read:

```bash
proxmox-lab console inspect --lease "$L" --vmid "$VMID"
```

It keeps one untouched full PNG locally and sends a separate, same-size copy
with labelled 100-pixel X/Y axes to NVIDIA, the named OpenRouter Nemotron Omni
free endpoint, and OpenRouter's free router concurrently. On later frames,
unchanged pixels are dimmed while changed regions stay bright and outlined.
Grid coordinates map directly to the original framebuffer. Treat its
recommended action as a proposal, not permission. Ordinary screenshots stay
local. Do not send
confidential or personal screens to free providers.

`console click` has one minimal guarded interface: name the visible `--target`
and provide its proposed coordinates. The harness moves the cursor, captures a
full checkpoint, and asks cloud vision to independently match the named control
and coordinate. It clicks only after that positive verdict. Failure, timeout,
ambiguity, or disagreement returns `clicked: false`; stop instead of retrying.
There is no self-confirmation flag.

For a popup menu or combobox opened by a verified click, use arrow keys and
`enter` to choose the visibly highlighted item. Haiku's menus preserve
keyboard selection more reliably than a second coordinate click.

Do not crop, sharpen, recolour, or run external OCR over a graphical installer.
Those transformations discard context and turn one uncertain observation into
dozens of guesses. `console screenshot --ocr` is only for a VGA text grid and
will refuse a graphical screen.

If the active model cannot see images, use `console inspect` first when its key
is configured. Otherwise delegate **only the current full-screen checkpoint**
to a vision-capable model and ask it for the screen name, relevant controls,
and safest single next action. Keep lease ownership and all mutations in the
primary agent. Tesseract is not a substitute for vision on a graphical desktop
or installer.

## Hard limits

- Never make more than three attempts on an unchanged screen.
- Never sweep coordinates or click controls whose purpose is unknown.
- Never retry a rejected click or bypass it with raw `api`, `keys`, or reboot.
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

## Memory and sizing for GUI installers

Legacy GUI installers have hard memory floors; undersized VMs die mid-install
with no error surface. Minimums that have been validated:

- Windows 2000 / ME: at least 512 MB
- Ubuntu 14.10 or newer desktop: at least 1 GB
- OpenIndiana / illumos GUI (Caiman) installer: at least 4 GB — the
  installer's own recommendation; the live session plus installer exceed 2 GB

The failure mode on an undersized VM is silent: the GUI session dies under
memory pressure mid-`Transferring Contents`, leaving a partial ZFS pool and a
non-bootable loader, with nothing on screen to say what happened.

To recover an interrupted OpenIndiana install:

1. Boot the live DVD.
2. Import the pool: `zpool import -f rpool`
3. Verify what made it onto disk: `zpool status` and `zfs list -r rpool/ROOT`
4. Point the pool at the ROOT dataset that survived:
   `zpool set bootfs=rpool/ROOT/<dataset> rpool`
5. Reinstall the loader (this illumos takes no device argument):
   `sudo bootadm install-bootloader -f -P rpool`
6. Shut down and boot from disk.

If finalization was interrupted, the loader usually aborts with
`No rootfs module provided, aborting` at the `ok` prompt — the rootfs was never
made resolvable. Re-running the full install on a correctly sized VM is the
reliable option then.

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
