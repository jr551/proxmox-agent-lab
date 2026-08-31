# ✅ What has actually been verified

Unit tests prove the code does what the code says. This page records what has
been run **against real hardware** — a Proxmox 9.2.2 node that powers itself on
and off — and, just as usefully, what has not.

Read it as a statement of confidence, not a feature list. Anything marked
"unit-tested only" may work; it has simply never been watched working.

## 🟢 Verified on hardware

One claim per row; dates are when the row was last watched on the node.

| Subsystem | Observed | Last verified |
|---|---|---|
| Leases | Power-on by WoL, heartbeat, `lease-end` reports `host_powered_off=true`, refusal to delete a foreign guest | 2026-08-21 |
| Audit ledger | Every mutating call recorded; secrets redacted | 2026-08-21 |
| VNC console | Screenshots of graphical desktop; `keys`/`click`/`type` drove a Windows installer | 2026-08-21 |
| Guarded GUI vision | Auto parallel vision returned valid OpenRouter Nemotron on Haiku VM 9060 (2.7–4.5 s), temporal deltas preserved, cursor checkpoints accepted | 2026-08-21 |
| Kilo vision provider | Live `api.kilo.ai` returned HTTP 200 actionable analysis (synthetic 400×300); `kilo-auto/balanced` routed to qwen/kimi-k2 | 2026-08-21 |
| Compressed image handback | 1920×1080 synthetic → 1280×720 @0.667, 17 KB PNG / 23 KB base64 in 0.31 s; every grid label recovered | 2026-08-21 |
| Serial console | Exact text read from a Linux guest without VNC | 2026-08-21 |
| S3 scratch | `health`/`put`/`list`/`get` byte-identical round trip | 2026-08-21 |
| File transfer | 200 KB push SHA-256 verified inside guest, pull byte-identical | 2026-08-21 |
| Storage | `status`/`list-disks`; 1 TB USB as `usb-bulk` (933 GB free) | 2026-08-21 |
| Experimental OCI LXC | BusyBox 1.37.0 `oci pull` → `oci create --start` running unpriv LXC (128 MiB, no swap); lease cleanup removed it | 2026-08-21 |
| Forced VPN | Guest egress via tunnel; kill-switch UNREACHABLE→REACHABLE | 2026-08-21 |
| Console sharing | Link minted, RFB through public tunnel, revoked link 404 immediate | 2026-08-21 |
| Android | Device built, booted to home screen, driven via console + adb | 2026-08-21 |
| Android templates | Template clone booted to home screen ~1 min vs ~20 min build, across host power cycle | 2026-08-21 |
| Windows install | Server 2022 unattended reached Server Manager desktop (partitioning, EULA, VirtIO, autologon; one Enter at language page) | 2026-08-21 |
| `windows finish` | RDP/SSH enabled, addresses reported, answer ISO detached/deleted | 2026-08-21 |
| qemu-guest-agent | Reachable after `vioserial` driver; missing driver was cause | 2026-08-21 |
| Long-term leases | Proxmox refused to delete a protected guest | 2026-08-21 |
| Physical memory patch | Marker found/read via memflow, `phys-write` changed it, observed inside live Debian process | 2026-08-21 |
| Compiled pin-check patch | Live conditional branch patched in RAM; client changed behaviour without restart | 2026-08-21 |
| Guarded GUI click | Haiku 9060: `Installer`(900,700) rejected `clicked:false`; `Install Haiku`(781,582) accepted and frame changed to *Welcome to the Haiku Installer!* | 2026-08-21 |

<details>
<summary>Annex: 2026-08-21 fix round — what the node actually showed</summary>

Each row was reproduced with the released build first, then re-run with the fix.

| Fix | Before | After |
|---|---|---|
| Serial stream carried transport text | `console text` returned `text: "starting serial terminal on interface serial0\n"` | Same read returns `""`; record gone across repeated attachments |
| Serial stream on a **stopped** guest | returned `text: "VM 9231 not running\n"` and **exit 0** — looked successful | exits 1 with `qemu/9231 is not running … pass --wait-for-guest` |
| Attach before power-on | log held one transport sentence | Capture started while stopped, waited, attached at power-on, recorded `Booting from Hard Disk…`, `GRUB loading.`, `Linux version 6.12.101…` — 548 lines from t=0, no transport records |
| Real guest text still arrives | — | Nudged serial console returned `debian@…:~$ ` intact; removing the record does not remove the prompt after it |
| Console WebSocket certificate policy | opened regardless of `verify_tls` | `verify_tls=true` → `SSLCertVerificationError` against self-signed node; `verify_tls=false` → opens |
| `upload` certificate policy | always `--insecure` | curl without `--insecure` refuses this node (`SSL certificate problem`), with it connects |
| `journal` on a JSONL backend | reported SQLite DB and returned `[]` | Summary over real mirror: 2,607 events in 15 files, 89 leases, correct first/last timestamps; `--event 'lease-*'` returned real records newest-first |
| `doctor` on a broken audit mirror | reported one problem, no mirror status | reports `audit.git_status.problem = "checkout is dirty…"` and `audit.spooled_records` |
| Expiry sweep scope | considered 1 lease, skipping a `cleanup_failed` one 7 days old whose guest was still running | considers both; dry run showed stale lease's `qemu/9211 running -> would delete` |

Two bugs in the first attempt at these fixes were found only by running them on the node: the transport record is not always the first thing on the stream (a blank line precedes it, which ended the search early), and a read containing only a transport record looks like an idle socket, so the capture stopped at it and lost the prompt that followed. Proxmox also issues a termproxy ticket for a *stopped* guest and reports the refusal in the byte stream rather than as an API error, so the attach retry has to ask `status/current` instead of trusting a successful attach.

</details>

<details>
<summary>Annex: 2026-08-21 retained-lifecycle round — what the node actually showed</summary>

| Fix | What the node actually showed |
|---|---|
| Orphan detection | `guest inventory` over 26 real guests: 19 resolved to a lease record, **6 were orphaned** (4 running), 1 was not this tool's. Running set — 106, 9242, 9243, 9244, 9999 — is why the node had been up for five days |
| `doctor` fails on a running orphan | `5 running guest(s) carry a lease tag this controller has no record of … the host cannot power off: 106, 9242, 9243, 9244, 9999` |
| `storage gc` | Reported 4 unreferenced qcow2 images on bulk store (VMIDs 9180, 9190, 9200 — guests long gone), correctly keeping 43 referenced volumes. Nothing deleted: reporting is default. They were **provisioned at 51.54 GB but held 9.33 MB**, which is why the report now separates the two figures |
| `storage status` class | `usb-bulk` → `bulk`, `local` and `local-lvm` → `fast` |
| `doctor --host-checks` | `updates_pending: 78`, `security_updates: true`, `reboot_required: false` over host SSH channel |
| Retained registry round trip | `guest retain --vmid 101` recorded the template, `doctor` then reported `never_backed_up: [101]` with coverage note, `backup --retained` correctly refused while disabled, and `--forget` restored empty registry |

Running the reclamation for real also surfaced the limit of the orphan heuristic: it stopped `qemu/9243` and `qemu/9244`, ReactOS benchmark guests that a *different* controller session was driving over VNC through the same API token. Stopping (never deleting) meant it was recoverable, and that session restarted 9244 within 90 seconds — but the guard added in 0.9.3 exists because of it, not in anticipation of it.

A test-hygiene bug surfaced only because of this hardware check: `register_resource` began writing the retained registry under `STATE_ROOT`, and one pre-existing test did not redirect that root — so running the suite put a bogus retained guest (`qemu/9001`, purpose "test") into the developer's **live** controller state, where an enabled backup sweep would have picked it up. Every test module now points `PROXMOX_AGENT_LAB_STATE` at a disposable directory, and a guard test asserts it.

</details>

### Security properties, checked rather than assumed

These were tested by looking for the secret afterwards, not by reading the code:

- The Windows Administrator password does **not** appear in the audit ledger or in the VM config.
- Presigned S3 URLs do **not** appear in the journal — no `X-Amz-Signature` anywhere in it after a push and a pull.
- A share link is revoked the moment it is revoked; the URL 404s.
- Deleting a file from node storage was **refused** with `Host-level change refused without --host-change-authorized`, without the flag. The guardrail was tried, not assumed.

One check **failed**, and is now fixed: the unattended answer ISO stayed on `local:iso` after the install, with the Administrator password in plain text inside it. `windows finish` now deletes it. The password never was in the ledger — but "not in the ledger" is not the same as "not on disk", and only the first had been checked.

## 🟡 Known gaps, measured

**Windows Setup's language page.** An answer file automates everything except Setup's first screen, which waits for input indefinitely. `--unattended` now dismisses it. Full detail, including three wrong guesses about the cause, is in [windows.md](windows.md).

**An Android profile's model string is cosmetic.** Screen, RAM, storage and API level are real and verifiable in the guest. `ro.product.model` is not — it is baked into the system image, and a device reports `sdk_gphone_x86_64` whatever the profile says. See [android.md](android.md).

**Orphaned answer ISOs need a person.** Because deleting node storage content is a host-level change, cleaning up an answer ISO left by an abandoned install needs `--host-change-authorized`. That is the right trade — but it means the leftovers do not disappear on their own.

**Setup's language page needs Alt+N, not Enter.** Focus sits on a combo box, which consumes Enter — thirty of them changed nothing. The accelerator is English-specific, so a non-`en-US` `--locale` may still need a click.

**"Is this install stuck?" has no reliable automatic answer.** Disk writes fall to nothing during first boot, and a spinner is too small for a coarse frame diff to notice. `wait-agent` warns rather than failing, and a screenshot remains the way to know. Both heuristics were caught misfiring on this run.

**`args` needs root.** Proxmox restricts the `args` VM option to `root@pam`, so an API token cannot set it. Nothing here depends on that; it is recorded because it costs time to rediscover.

## 🔴 Unit-tested only

Not yet watched working end to end on hardware:

- Windows **2025** (2022 is the version that was installed)
- `console screenshot --for-model` and `console inspect`'s image fallback against a **real guest framebuffer**. The encoder, bound, cap and readability were measured (see 🟢 above), but on synthetic pixels; no lab guest screen has been handed back this way yet
- `console screenshot --for-model --via monitor` decoding QEMU's own PNG before resizing — unit-tested against a generated PNG only; monitor path itself is blocked on this node (see `console screenshot --via monitor` below)
- Weekly backups of long-term leases (`backup`)
- `storage add-disk` against a **second** unfamiliar disk
- ngrok as the share tunnel (cloudflared is the tested default)
- `--abi arm64-v8a` Android, which has no KVM and is expected to be very slow
- USB capture against a real passed-through device
- Passive guest network capture and end-to-end TLS interception
- Ghidra analysis and multi-step live tracing through the memflow helpers
- `console screenshot --via monitor` (QEMU `screendump` fallback) — on the lab node the guards, SSH channel and host cleanup ran, but Proxmox refused the monitor endpoint `Permission check failed (/vms/9004, Sys.Audit|Sys.Modify)`; no PNG has been fetched off a real node this way. The lab token is `PVEVMAdmin`-scoped by design; this single entry covers all monitor-path variants (see also `guest disk-activity` QMP note below)
- `[proxmox] verify_tls` against a *trusted* certificate completing a handshake — both `verify_tls=true/false` positions were exercised on the self-signed node (see annex), but the verified path has only been observed refusing; no trusted-cert handshake has been watched succeeding
- `storage add-disk` failing non-zero when content configuration fails — exit status and preserved partial result are unit-tested only. Forcing it on hardware would mean reformatting a disk
- Ownership-aware and retrying `cleanup-expired` verified as a decision, not as execution — actually running the sweep would have deleted live guests and powered the host off
- `cleanup-expired --reclaim-orphans`: the orphans it would stop were listed on the real node, but the stop itself was not run — those guests are live work
- `storage gc --delete`: the 51.54 GB it would remove was reported on the real node; the deletion was left for a human
- The host guard's idle **power-off** and its **long-term pin**. The guard's stop loop ran on the real host at install time, but a full idle power-off has never been watched happen, and the long-term pin (a live long-term guest surviving past the grace window, and an idle-but-pinned host staying up) is exercised with fakes only. Watching the power-off for real means letting a verified-idle host actually shut down; the pin can be observed harmlessly: with an active long-term lease whose guests are stopped, the guard's journal should read "long-term lease ... active; host stays up" every sweep and the host must never go down
- The guard idle-counter state file (`/var/lib/pxl-hostguard-state.json`) write-then-rename is unit-tested only; no kill-mid-write has been staged on the real host
- The bulk-storage warning on a guest disk — unit-tested only. Firing it for real means attaching a disk to a live guest
- `backup --retained` actually writing a vzdump archive — refused-while-disabled was verified (see 🟢 retained row); the write itself was not. It is gigabytes to a slow disk
- `guest disk-activity` in **every** mode — nothing about it has been run against real hardware. The counter-only sampling, the `info blockstats` parsing, the `du --block-size=1` delta, the graceful degradation and `disagreement` computation are unit-tested with active fakes. Two claims are therefore unverified: that the `qmp_blockstats` signal is reachable at all on this node (same monitor endpoint as `console screenshot --via monitor` above, so the QMP half may be unavailable here) and that the `info blockstats` transcript from this Proxmox/QEMU build matches the parser shape. The motivating observation (a `diskwrite` of 0 across a whole session on a writing qcow2 guest) was reported from a real ReactOS session; the cross-check written in response has not been back to the node to confirm it catches that case

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
