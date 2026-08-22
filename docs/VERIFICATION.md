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
| Guarded GUI vision | On Haiku VM 9060, automatic parallel vision returned a valid OpenRouter Nemotron result in 2.7-4.5s provider time; temporal inputs highlighted measured local deltas while preserving the untouched capture; repeated cursor checkpoints independently accepted each named control before clicking |
| Serial console | Text read back from a Linux guest without VNC |
| S3 scratch | `health`, `put`, `list`, `get` — byte-identical round trip |
| File transfer | 200 KB pushed into a guest and verified by SHA-256 inside it, then pulled back byte-identical |
| Storage | `status` and `list-disks`; a 1 TB USB disk formatted and used as `usb-bulk` (933 GB free) |
| Experimental OCI LXC | On Proxmox VE 9.2.2, native `oci pull` fetched the public BusyBox 1.37.0 tag into template storage; `oci create --start` converted it to a running, tagged, lease-owned unprivileged LXC with 128 MiB RAM and no swap. Lease cleanup removed the stopped LXC; the explicitly authorized template deletion also completed. The host stayed on only because an independent lease remained active. |
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
| Guarded GUI click | On Haiku VM 9060, `Installer` at `(900,700)` was rejected with `clicked: false`; the cursor on `Install Haiku` at `(781,582)` was independently accepted, clicked once, and the returned frame changed to `Welcome to the Haiku Installer!` |

### The 2026-08-21 fix round, checked against the node

Each of these was reproduced with the released build first, then re-run with
the fix, on a Proxmox 9.2 node and its real controller state:

| Fix | Before | After |
|---|---|---|
| Serial stream carried transport text | `console text` on a running guest returned `text: "starting serial terminal on interface serial0\n"` | The same read returns `""`; the record is gone across repeated attachments |
| Serial stream on a **stopped** guest | returned `text: "VM 9231 not running\n"` and **exit 0** — a capture that looked successful | exits 1 with `qemu/9231 is not running … pass --wait-for-guest` |
| Attach before power-on | not executable; the log held one transport sentence | capture started while stopped, waited, attached at power-on, and recorded `Booting from Hard Disk…`, `GRUB loading.`, `Linux version 6.12.101…` — 548 lines from t=0, with no transport records |
| Real guest text still arrives | — | a nudged serial console returned `debian@…:~$ ` intact; removing the record does not remove the prompt after it |
| Console WebSocket certificate policy | opened regardless of `verify_tls` | `verify_tls=true` → `SSLCertVerificationError` against the self-signed node; `verify_tls=false` → opens |
| `upload` certificate policy | always `--insecure` | curl without `--insecure` refuses this node (`SSL certificate problem`), with it connects |
| `journal` on a JSONL backend | reported the SQLite database and returned `[]` for a query | summary over the real mirror: 2,607 events in 15 files, 89 leases, correct first/last timestamps; `--event 'lease-*'` returned real records newest-first |
| `doctor` on a broken audit mirror | reported one problem, no mirror status | reports `audit.git_status.problem = "checkout is dirty…"` and `audit.spooled_records` |
| Expiry sweep scope | considered 1 lease, skipping a `cleanup_failed` one 7 days old whose guest was still running | considers both; the dry run showed the stale lease's `qemu/9211 running -> would delete` |

Two bugs in the first attempt at these fixes were found only by running them
on the node, and are worth recording because a fake would not have caught
either: the transport record is **not** always the first thing on the stream
(a blank line precedes it, which ended the search early), and a read
containing only a transport record looks exactly like an idle socket, so the
capture stopped at it and lost the prompt that followed. Proxmox also issues a
termproxy ticket for a *stopped* guest and reports the refusal in the byte
stream rather than as an API error, so the attach retry has to ask
`status/current` instead of trusting a successful attach.

### The 2026-08-21 retained-lifecycle round, checked against the node

| Fix | What the node actually showed |
|---|---|
| Orphan detection | `guest inventory` over 26 real guests: 19 resolved to a lease record, **6 were orphaned** (4 running), 1 was not this tool's. The running set — 106, 9242, 9243, 9244, 9999 — is why the node had been up for five days |
| `doctor` fails on a running orphan | `5 running guest(s) carry a lease tag this controller has no record of … the host cannot power off: 106, 9242, 9243, 9244, 9999` |
| `storage gc` | Reported 4 unreferenced qcow2 images on the bulk store (VMIDs 9180, 9190, 9200 — guests long gone), while correctly keeping 43 referenced volumes. Nothing was deleted: reporting is the default. They were **provisioned at 51.54 GB but held 9.33 MB**, which is why the report now separates the two figures |
| `storage status` class | `usb-bulk` → `bulk`, `local` and `local-lvm` → `fast` |
| `doctor --host-checks` | `updates_pending: 78`, `security_updates: true`, `reboot_required: false` over the host SSH channel |
| Retained registry round trip | `guest retain --vmid 101` recorded the template, `doctor` then reported `never_backed_up: [101]` with the coverage note, `backup --retained` correctly refused while disabled, and `--forget` restored an empty registry |

Running the reclamation for real also surfaced the limit of the orphan
heuristic: it stopped `qemu/9243` and `qemu/9244`, ReactOS benchmark guests
that a *different* controller session was driving over VNC through the same API
token. Stopping (never deleting) meant it was recoverable, and that session
restarted 9244 within 90 seconds — but the guard added in 0.9.3 exists because
of it, not in anticipation of it.

A test-hygiene bug surfaced only because of this hardware check, and is worth
recording: `register_resource` began writing the retained registry under
`STATE_ROOT`, and one pre-existing test did not redirect that root — so running
the suite put a bogus retained guest (`qemu/9001`, purpose "test") into the
developer's **live** controller state, where an enabled backup sweep would have
picked it up. Every test module now points `PROXMOX_AGENT_LAB_STATE` at a
disposable directory, and a guard test asserts it.

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
- `console screenshot --via monitor`: the QEMU `screendump` fallback. On the
  lab node the guards, the SSH channel and the host cleanup all ran, but
  Proxmox refused the monitor endpoint itself — `Permission check failed
  (/vms/9004, Sys.Audit|Sys.Modify)` — so no PNG has been fetched off a real
  node this way. The lab token is `PVEVMAdmin`-scoped by design; this fallback
  needs a privilege it does not have.
- `[proxmox] verify_tls = true` completing a handshake against a *trusted*
  certificate. Both switch positions were exercised on the node (see below),
  but it still serves the self-signed certificate a fresh install ships with,
  so the verified path has only ever been observed refusing.
- `storage add-disk` failing non-zero when content configuration fails: the
  exit status and the preserved partial result are unit-tested only. Forcing
  it on hardware would mean reformatting a disk.
- Ownership-aware and retrying `cleanup-expired` was verified as a decision
  (see below), not as an execution: actually running the sweep would have
  deleted live guests and powered the host off.
- `cleanup-expired --reclaim-orphans`: the orphans it would stop were listed on
  the real node, but the stop itself was not run — those guests are live work.
- `storage gc --delete`: the 51.54 GB it would remove was reported on the real
  node; the deletion was left for a human.
- The bulk-storage warning on a guest disk: unit-tested only. Firing it for
  real means attaching a disk to a live guest.
- `backup --retained` actually writing a vzdump archive: refused-while-disabled
  was verified, the write itself was not. It is gigabytes to a slow disk.
- `guest disk-activity`, in **every** mode. Nothing about it has been run
  against real hardware. The counter-only sampling, the `info blockstats`
  parsing, the `du --block-size=1` delta, the graceful degradation when either
  extra signal is missing and the `disagreement` computation are all covered by
  unit tests with active fakes and nothing else. Two specific claims are
  therefore *unverified*: that the `qmp_blockstats` signal is reachable at all
  on this node — `console screenshot --via monitor` above shows the same
  monitor endpoint being refused for the `PVEVMAdmin` lab token, so the QMP
  half may simply be unavailable here — and that the `info blockstats`
  transcript from this Proxmox/QEMU build matches the shape the parser was
  written against. The motivating observation itself (a `diskwrite` of 0 across
  a whole session on a writing qcow2 guest) was reported from a real ReactOS
  session; the cross-check written in response to it has not been back to the
  node to confirm it catches that case.

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
