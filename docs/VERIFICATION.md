# ✅ What has actually been verified

Unit tests prove the code does what the code says. This page records what has
been run **against real hardware** — a Proxmox 9.2.2 node that powers itself on
and off — and, just as usefully, what has not.

Read it as a statement of confidence, not a feature list. Anything marked
"unit-tested only" may work; it has simply never been watched working.

## 🟢 Verified on hardware

| Subsystem | What was actually observed |
|---|---|
| Leases | Power-on by WoL, heartbeat, `lease-end` reporting `host_powered_off=true`, refusal to delete a guest the lease did not create |
| Audit ledger | Every mutating call recorded; secrets redacted (see below) |
| VNC console | Screenshots of a graphical desktop, `keys`, `click` and `type` driving a real Windows installer |
| Guarded GUI vision | An untouched 1280x800 Haiku capture and a same-size labelled grid were produced; OpenRouter free vision returned structured controls; a new target moved the visible cursor and returned `clicked=false` despite an older resolution-level approval |
| Serial console | Text read back from a Linux guest without VNC |
| S3 scratch | `health`, `put`, `list`, `get` — byte-identical round trip |
| File transfer | 200 KB pushed into a guest and verified by SHA-256 inside it, then pulled back byte-identical |
| Storage | `status` and `list-disks`; a 1 TB USB disk formatted and used as `usb-bulk` (933 GB free) |
| Forced VPN | Egress from a guest used the tunnel rather than the home WAN; kill switch went UNREACHABLE→REACHABLE |
| Console sharing | A link minted, the RFB handshake completed **through the public tunnel**, and a revoked link 404ed immediately |
| Android | A device built, booted to the home screen, and driven over the VM console; adb reachable |
| Android templates | A template cloned and booted to the Android home screen in about a minute, versus roughly twenty for a build from scratch — including across a host power cycle |
| Windows install | Server 2022 reached a logged-in Server Manager desktop unattended: partitioning, image selection, EULA, VirtIO drivers and autologon all applied from the answer file, after one Enter at the language page |
| `windows finish` | RDP enabled, OpenSSH installed and sshd started, both guest addresses reported, and the answer ISO detached and deleted — all confirmed in one run |
| qemu-guest-agent | Reachable from the host after `vioserial` was installed; the missing driver was the reason it was not |
| Long-term leases | Proxmox refused to delete a protected guest, as intended |
| Physical memory patch | A marker was found in a running Debian guest, read from outside it, changed with `phys-write`, and observed changing inside the live process |
| Compiled pin-check patch | A live conditional branch in a disposable test client was patched in RAM and the client changed behaviour without a restart |

### Security properties, checked rather than assumed

These were tested by looking for the secret afterwards, not by reading the code:

- The Windows Administrator password does **not** appear in the audit ledger
  or in the VM config.
- Presigned S3 URLs do **not** appear in the journal — no `X-Amz-Signature`
  anywhere in it after a push and a pull.
- A share link is revoked the moment it is revoked; the URL 404s.
- Deleting a file from node storage was **refused** with `Host-level change
  refused without --host-change-authorized`, without the flag. The guardrail
  was tried, not assumed.

One check **failed**, and is now fixed: the unattended answer ISO stayed on
`local:iso` after the install, with the Administrator password in plain text
inside it. `windows finish` now deletes it. The password never was in the
ledger — but "not in the ledger" is not the same as "not on disk", and only
the first had been checked.

## 🟡 Known gaps, measured

**Windows Setup's language page.** An answer file automates everything except
Setup's first screen, which waits for input indefinitely. `--unattended` now
dismisses it. Full detail, including three wrong guesses about the cause, is in
[windows.md](windows.md).

**An Android profile's model string is cosmetic.** Screen, RAM, storage and API
level are real and verifiable in the guest. `ro.product.model` is not — it is
baked into the system image, and a device reports `sdk_gphone_x86_64` whatever
the profile says. See [android.md](android.md).

**Orphaned answer ISOs need a person.** Because deleting node storage content
is a host-level change, cleaning up an answer ISO left by an abandoned install
needs `--host-change-authorized`. That is the right trade — but it means the
leftovers do not disappear on their own.

**Setup's language page needs Alt+N, not Enter.** Focus sits on a combo box,
which consumes Enter — thirty of them changed nothing. The accelerator is
English-specific, so a non-`en-US` `--locale` may still need a click.

**"Is this install stuck?" has no reliable automatic answer.** Disk writes
fall to nothing during first boot, and a spinner is too small for a coarse
frame diff to notice. `wait-agent` warns rather than failing, and a screenshot
remains the way to know. Both heuristics were caught misfiring on this run.

**`args` needs root.** Proxmox restricts the `args` VM option to `root@pam`, so
an API token cannot set it. Nothing here depends on that; it is recorded
because it costs time to rediscover.

## 🔴 Unit-tested only

Not yet watched working end to end on hardware:

- Windows **2025** (2022 is the version that was installed)
- Weekly backups of long-term leases (`backup`)
- `storage add-disk` against a **second** unfamiliar disk
- ngrok as the share tunnel (cloudflared is the tested default)
- `--abi arm64-v8a` Android, which has no KVM and is expected to be very slow
- USB capture against a real passed-through device
- Passive guest network capture and end-to-end TLS interception
- Ghidra analysis and multi-step live tracing through the memflow helpers

## 🔁 Reproducing this

Every row above came from a single lease:

```bash
L=$(proxmox-lab lease-begin --purpose "verification sweep" \
    | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
trap 'proxmox-lab lease-end --lease "$L"' EXIT

proxmox-lab doctor
proxmox-lab storage status
proxmox-lab s3 health
proxmox-lab push --lease "$L" --vmid <id> --file ./probe --dest /tmp/probe
proxmox-lab guest run --lease "$L" --vmid <id> -- sha256sum /tmp/probe
```

The two long ones, which is where the interesting failures were:

```bash
proxmox-lab windows install --lease "$L" --vmid 9060 --version 2022   --unattended --password-stdin --full-clone --storage usb-bulk
proxmox-lab windows wait-agent --lease "$L" --vmid 9060   # stalls fast if stuck
proxmox-lab windows finish --lease "$L" --vmid 9060       # also shreds the ISO

proxmox-lab android create --lease "$L" --vmid 9050 --profile minimal
proxmox-lab android template --lease "$L" --vmid 9050
```

Check the claims rather than trusting them. If something here is no longer
true, the honest fix is to change this page, not to leave it aspirational.
