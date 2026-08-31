# Changelog

All notable changes to this project will be documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases use
[Semantic Versioning](https://semver.org/).

## 0.13.0 - 2026-08-25

### Added

- **The host guard powers the host off when the lab goes idle.** It already
  stopped guests whose lease had ended or gone quiet; a lab that cleans up but
  stays awake has only solved half the problem -- the eight-day lease that
  prompted the guard also meant eight days of electricity.

  The condition is deliberately blunt: no guest running at all, infrastructure
  aside, for several consecutive checks. Not "no lease the ledger knows about"
  -- a controller that has not been upgraded writes nowhere the guard can
  read, and its guests would look like an idle host. The one exception is the
  long-term pin below. Set `shutdown_when_idle` false in
  `/etc/pxl-hostguard.json` to disable it.

- **Windows: the controller runs there now.** `import fcntl` is guarded and
  falls back to `msvcrt` locking, and `os.uname` (POSIX-only) goes through
  `platform.system()`. The 0o077 permission check on a file-backend secrets
  file is skipped on Windows, where NTFS ACLs are the equivalent and the mode
  bits meant every read was refused. Covered by tests that simulate Windows by
  hiding `fcntl` and deleting `os.uname`, so they run on any platform.

### Changed

- **`upload` defaults to the bulk store, not `local`.** `local` is the Proxmox
  root filesystem, and ISOs are the biggest thing this tool writes: this
  project's own lab reached 96% full on ISOs alone, with 2.8 GB left. A full
  root takes the hypervisor down with it, which is a worse failure than a
  slower ISO read. Falls back to whatever `upload_storages` allows when the
  bulk store is not one of them. The `--storage` help now says which case
  applies, and a config with an empty `upload_storages` no longer makes
  argparse reject every value.

### Fixed

- **The host guard honors long-term leases.** A long-term lease never
  heartbeats by design, so the guard read its silence as abandonment and
  stopped its live guests after the grace window, and the idle power-off
  would have pulled the host out from under an active long-term lease whose
  guests happened to be stopped. The guard now reads each lease's `kind`
  from the ledger: a long-term lease is never "abandoned", and while one is
  active the host stays on. Only a recorded end (`lease-end`,
  `long-term-destroyed`, `long-term-released`) ends one -- the long-term
  endings were invisible to the guard's query before. The guard runs on the
  host, so picking this up means re-running `proxmox-lab journal host-setup`
  to rewrite `/usr/local/lib/pxl-hostguard.py`.
- The guard's idle counter is now written atomically (write then rename), so
  a kill mid-write cannot truncate it and silently defeat the
  consecutive-checks rule.
- The Windows tests now pass on real Windows, not only in simulation: the
  lock test no longer asserts flock's same-handle reentrancy of msvcrt (and a
  second test pins the two-handle contention rule with a fake msvcrt), and
  the secrets-file tests patch the platform helper explicitly so both
  branches are exercised on either real platform. Removed a stray shell
  fragment accidentally committed to the repository root.

## 0.12.1 - 2026-08-25

### Fixed

- `doctor` listed a spooled audit backlog twice. The ledger check counted the
  spool as well as the block that already reported it, so a controller with
  events waiting saw the same problem printed two ways.

## 0.12.0 - 2026-08-25

One shared audit ledger, and one secret per machine.

The journal was a SQLite file per controller, optionally mirrored to PocketBase
and to a git repo. Two machines driving the same lab kept two partial histories
that never met, and adding a machine meant repeating the whole secret setup.
This replaces all of that with one MariaDB on the Proxmox host.

### Changed — the audit ledger

- **MariaDB is now the only backend, and it is required.** `sqlite`, `jsonl`,
  PocketBase and the git mirror are gone, along with `pocketbase.py`,
  `pocketbase-host-setup.sh` and every `[audit] pocketbase_*`/`git_*` key.
- **Provisioned in one command**: `proxmox-lab journal host-setup
  --host-change-authorized` creates a persistent unprivileged container on the
  Proxmox host, publishes it on the hypervisor's own address, and creates the
  database. It is deliberately not lease-owned, so lease-end cannot destroy the
  history it just wrote. `mariadb-host-setup.sh` is the same script for a host
  you would rather set up directly.
- **The lab host is off between leases, so the ledger is too.** No action ever
  fails for it: events spool locally and upload on the next
  `journal --flush-spool`. `doctor` reports a backlog, and its reachability
  probe uses a short timeout so it stays fast while the host sleeps.
- **New query surface**: `journal --controller`, `--migrations`, `--migrate`.

### Changed — secrets

- **Secrets live in the environment**, not an OS keyring. `env` is the default
  backend; `keychain` and `secret-tool` remain available but are legacy.
- **One credential per machine.** `mariadb-password` is the only secret a
  controller needs; every other secret is stored in the ledger and handed out
  from there, so adding a machine is a single `export`. Provisioning copies
  this controller's existing secrets — including any still in its OS keyring —
  into the shared store. An environment variable still overrides any single
  secret locally.
- That bootstrap password is now the key to all the others. The ledger listens
  on the lab LAN only; treat it as the master secret.
- The OS keyring is no longer an *implicit* fallback inside `get`. Reaching
  past the configured backend into whatever the desktop keyring held made a
  "missing" secret unpredictable — in tests it returned the developer's real
  secrets. Migration reads it explicitly instead.

### Added — upgrading, including from a second machine

- **Upgrades port themselves.** The first command after an upgrade carries this
  controller's old local ledger into the shared one. Safe to re-run.
- **A second machine knows what to do.** Event ids are derived from content and
  inserts are `INSERT IGNORE`, so a second controller adds only the events the
  first did not already have, and reports what it found rather than presenting
  a no-op as a failure. Concurrent migrations serialise on a MariaDB advisory
  lock, and a `migrations` table records who has migrated.
- Verified against the real 5,138-event ledger from this project's own lab:
  full history migrated, a second controller added only its own 3 events, and
  re-running uploaded 0.

### Changed — dependencies

- **The zero-dependency policy is retired.** MariaDB needs a client protocol
  the standard library does not have. The controller now depends on PyMySQL
  (pure Python, so still no compiler) and cryptography. CI installs the package
  before running the suite.

### Fixed

- `net attach` raised `NameError: name 're' is not defined`; `netgw.py` never
  imported `re`.
- `install.sh` no longer asks about audit backends at all.

## 0.11.0 - 2026-08-24

A security-audit release: every shipped-code finding from the 2026-08-24 audit
(`docs/AUDIT-2026-08-24.md`) is fixed here, plus agent quick-start recipes and
a macOS guest recipe.

### Fixed — security

- **Command injection via share label** (`share.py`): `share create`/`revoke`
  interpolated a caller-supplied label into a root `bash -c` string on the
  share worker; `$()` or backticks executed as root on a machine holding a
  Proxmox token. All worker calls now pass argv lists through the guest agent.
- **Console WebSocket TLS** (`ws.py`, `console.py`): console websockets now
  verify certificates by default; `[proxmox] verify_tls = false` is the only
  way to disable (closes the websocket half of #72).
- **Path traversal in share static handler** (`share_server.py`): guard now
  uses `relative_to()`, so prefix-sibling directories cannot satisfy it.
- **Stored XSS via share label** (`share_server.py`): labels are HTML-escaped
  in the viewer page.
- **Secrets on argv** (`secrets_store.py`): keychain writes feed `-w` from
  stdin instead of argv (visible in `ps`); `-U` ordered before `-w`.
  Round-trip verified against the real macOS keychain.
- **File-backend TOML corruption** (`secrets_store.py`): values written as
  JSON/TOML strings, so quotes/backslashes no longer corrupt other entries.
- **netcap injection** (`netcap.py`): `--iface` whitelisted to plain interface
  names; BPF filter single-quote escaped before reaching the root shell.

### Fixed — correctness and invariants

- **Orphaned guests on clone timeout** (`cli.py`): created guests register
  into their lease BEFORE `--wait-task`, so a task timeout can no longer leave
  an unowned VM behind at lease-end. Reload-under-lock behavior preserved.
- **Answer ISO outlives abandoned installs** (`cli.py`): lease finalization
  shreds `autounattend-<vmid>.iso` for each deleted qemu resource.
- **USB detach gate** (`usb.py`): detach requires `--host-change-authorized`,
  symmetric with attach and with the module's documentation.
- **memflow read OOM** (`memflow.py`): `read`/`phys-read` capped at the same
  16 MiB as `dump`; the host helper allocates the full buffer as root.
- **Kill-switch test stranded guests offline** (`netgw.py`): wg0 restart moved
  to a `finally` block; a failed restart is reported in the check output.
- **NIC index parsing** (`netgw.py`): `net attach --nic net12` configured
  `ipconfig2`; index parsed properly with a clear error for non-`netN`.

### Added

- **Agent quick-start recipes** (`docs/RECIPES.md`) — eight copy-paste recipes,
  every command line machine-validated against the live parser.
- **macOS guest recipe** (`docs/macos.md`) — OSX-PROXMOX-based macOS VMs inside
  lab leases: host prep, TSC check, look→act loop, gotchas.

## 0.10.0 - 2026-08-22

Reading a screen is now a vision job. Glyph-matching OCR is gone, which is a
breaking change to the console surface — see **Removed** for what replaced it
and how the old flags signpost it.

### Added

- The Kilo Code gateway is a fourth vision route for `console inspect`,
  alongside NVIDIA and the two OpenRouter models. It asks for
  `kilo-auto/balanced`, the balanced auto router, rather than a concrete
  model: the router picks a vision-capable model server-side, so pinning one
  would break the provider the day that model is retired. The key comes from
  the new `kilo-api-key` secret or `KILO_API_KEY`. Verified against the lab
  node — the router answered as `alibaba/qwen3.7-plus` and read a live Arch
  installer framebuffer correctly.
- `console screenshot --for-model` hands the screen back inline as a base64
  PNG for a caller that reads images itself. Bounded and honest about it:
  box-averaged downscale to a 1280 px longest edge (never below 640 px, where
  an 80-column line stops being legible), maximum zlib compression, and a
  1.5 MB cap on the emitted base64, with the scale factor and both the
  original and emitted dimensions reported. A box average, not
  nearest-neighbour, because dropping scanlines destroys 8 px glyphs.
- `console inspect` attaches that same payload when every vision provider
  fails, so a failed analysis still gives the caller something to look at.
  `--no-image-fallback` opts out. It is additive — the command still exits
  non-zero and still reports `vision_error`.
- `guest disk-activity` samples a running guest's writes twice over a bounded
  interval and reports the delta, because Proxmox's `diskwrite` is cumulative
  and has been seen reading 0 for an entire session on a qcow2 guest that was
  demonstrably writing. `--ground-truth` adds two independent signals — QEMU's
  own block counters via `info blockstats`, and `du --block-size=1` on the
  backing image over the opt-in host SSH channel — and names any signal that
  saw nothing while another saw bytes in an explicit `disagreement` field.
  Without ground truth the verdict is `null`, never `false`: the counter alone
  cannot prove a guest idle.
- `iso diagnose` decodes the El Torito boot catalog instead of merely
  detecting the boot record: per entry, the platform, emulation type, load
  segment, boot-load-size and boot image LBA, plus a new `el_torito_ok`.
  It catches four ways an ISO is silently unbootable — an unusable catalog
  (bad LBA, failed checksum, missing `0x55 0xAA` key bytes), a boot image past
  the end of the file, a boot-load-size of zero, and no boot record at all
  over a bootloader-shaped file tree. The last quotes the mkisofs options to
  rebuild with, because that ISO boots to a black screen with no keyboard and
  no error at all.
- `console keys` and `console type` report `screen_changed` from the
  `--screenshot-after` capture. `keys_sent` counts what the controller
  transmitted, not what the guest received, and nothing else in the result
  distinguished a delivered keystroke from a dropped one.
- `lease-end --shared-guests-authorized`, for the refusal described below.
- `docs/reactos.md` documents two ReactOS build settings that break this lab's
  own channels: `DLL_EXPORT_VERSION=0x600` silencing KDBG serial output on a
  Debug/KDBG build, and `ENABLE_ROSTESTS=0` breaking ISO assembly into a
  manual repair that drops the boot record.

### Removed

- **Glyph-matching OCR, in full.** It could only read a guest whose console
  font this controller already held; a guest carrying its own font decoded to
  nothing, which is exactly what was reported for the ReactOS installer at
  confidence 0.003. There is no general fix — bundling one more font only
  moves the boundary. Gone with it: the font table, the embedded PSF1 font,
  `console import-font` including its `--from-vmid` guest pull, and the
  `console-font-imported` audit event.

  `console screenshot --ocr` and `console import-font` are **kept registered**
  and fail with a message naming the replacement, so an upgrade does not land
  on `unrecognized arguments`. Both are deleted in 0.11.0. Read a screen with
  `console screenshot --for-model`, `console inspect`, or `console text` for a
  real terminal stream.

### Changed

- A refused guest mutation names the remedy instead of only the rule. It now
  reads `VMID 9246 existed before this lease; register it with 'proxmox-lab
  lease-register --lease <id> --kind qemu --vmid 9246 --allow-existing' if you
  intend to drive it`, with the lease, kind and vmid filled in.
- An explicitly supplied **empty** console password is now a credential rather
  than an error, so `guest run --password-stdin` and `net leak-test` can drive
  a legacy or freshly-installed guest that has no password set. Omitting the
  flag still errors — an empty password is never inferred. `api
  --password-stdin` and `windows install --password-stdin` still reject one:
  those write a credential rather than use one.
- `console preflight` reports which vision providers have a key, instead of
  whether a console font was installed.

### Fixed

- A guest write whose path resolved to no readable vmid bypassed the lease
  ownership check entirely. `/nodes/<node>/qemu//9246/sendkey` — note the
  double slash — sits inside the safe write surface and reaches the guest, but
  parses as no guest at all, so it was sent to Proxmox unguarded. It is now
  refused.
- `lease-end` destroyed guests without first checking whether another active
  lease still referenced them: other leases were consulted only afterwards,
  and only to decide whether to power the host off. It now cross-references
  every resource it would delete against all other active leases — long-term
  and expired-but-active included — before it powers anything on, stops or
  deletes anything, and refuses, naming the guest and the other lease.
  `--shared-guests-authorized` overrides.
- `TermSession.login` waited only for a password prompt, so a guest with no
  password set — which never prints one — hung for the full timeout.
- `windows install --password-stdin` silently replaced an empty password with
  a generated one, and checked it only after cloning. The check now runs
  before the clone.
- `guest disk-activity --ground-truth` told the operator to rerun with
  `--ground-truth` on a run that already had it. Observed on the lab node,
  where neither extra signal is available: the monitor endpoint answers 403
  for the lab token, and the guest's disks are LVM block devices with no file
  to grow. The note now names the permission and the storage shape each
  missing signal needs.
- The Kilo router freely picks a reasoning model, which returned HTTP 200 with
  empty content after spending the whole token budget on reasoning. The
  provider now disables reasoning and asks for JSON object mode, and an
  empty response says why it was empty.

## 0.9.4 - 2026-08-21

### Fixed

- The orphan activity guard could not see work happening *inside* a guest. Its
  two signals were external — a recent Proxmox task, or a short uptime — and a
  long build in an unmanaged container produces neither: no task is recorded
  and the uptime keeps growing. After 30 minutes such a guest read as idle and
  was reclaimable. A third signal now protects it: CPU at or above 10%.

  The floor is set from measurement on the lab node rather than taste — a
  genuinely idle container runs at 0.005% and a mostly-idle Debian guest at
  about 1%, so anything lower would make nothing reclaimable at all.

### Changed

- Every orphan now carries its measured load — CPU percent, memory, and disk
  and network counters — in the reclamation result, in the audit event for a
  guest that was stopped, and in `doctor` (`orphaned_but_active` for protected
  guests, `orphaned_idle_load` for the rest). Below the CPU floor a guest is
  not proven idle, only not proven busy, so the numbers behind the decision are
  reported instead of just its outcome.

## 0.9.3 - 2026-08-21

### Fixed

- Orphan reclamation stopped guests that were in active use. "Orphaned" means
  *this* controller has no record of a guest — not that nobody is using it: a
  second controller, or one whose state directory lives elsewhere, drives
  guests through the same API token and its lease records are not here. A run
  on the lab node stopped a ReactOS benchmark that another session had been
  screenshotting every 45 seconds; that session restarted the guest 90 seconds
  later.

  Reclamation now leaves a running orphan alone when either signal says it is
  in use — a non-stop task for it within the last 30 minutes, or an uptime
  shorter than that — and reports it as `left_active`. Stop tasks are excluded
  from the signal, or the first reclamation would make every later run refuse;
  an unreadable task log counts as in use, because not knowing must not resolve
  to stopping someone's work. `--include-active` overrides it.
- `doctor` distinguishes the two cases. A running orphan with recent activity
  is reported as `orphaned_but_active` with a note that another controller
  likely owns it; only an idle one is a problem, since only that one is keeping
  the host on for no reason.

## 0.9.2 - 2026-08-21

### Fixed

- `storage gc` reported a volume's **provisioned** size as though it were
  reclaimable space. On the lab node its four orphans read as 51.54 GB and held
  9.33 MB between them — three creates that failed almost immediately — so the
  headline number overstated the gain from an irreversible deletion by a factor
  of about 5,500. The report now separates `orphaned_provisioned_gb` from
  `orphaned_on_disk_gb`, gives both per volume (`size_gb`, `used_gb`), and
  audits both on deletion. `docs/storage.md` and `docs/VERIFICATION.md` carry
  the corrected figures.

## 0.9.1 - 2026-08-21

### Added

- `cleanup-expired --orphans-only --host-change-authorized` reclaims orphaned
  guests and does nothing else: no lease is finalized, no backup runs, and the
  host is left on. Reclamation had only been reachable as part of a full expiry
  sweep, which in the same run deletes every expired lease's guests and then
  decides whether to power the machine off. On the lab node that meant "stop
  the four guests nothing owns" also proposed deleting ten guests that were
  fine, including the ReactOS builders and the build gateway. Wanting the first
  is not consenting to the second.

## 0.9.0 - 2026-08-21

Eight more reported issues, triaged and fixed. The theme is the *keep-forever*
half of the lab: guests that outlive their lease had no durable owner, no
backup coverage, and no way to be reclaimed — so one abandoned guest could keep
the machine on indefinitely.

### Added

- **A retained-guest registry.** `retained.json` under the state directory
  records any guest registered with `policy = retain`, and is never pruned
  automatically. A node tag (`codex-lab;lease-<id>`) lives for ever while the
  lease record does not, so a tag proves only that *some* lease created a
  guest — ownership now comes from the lease record while it exists and this
  registry afterwards. Tags are documented as informational, and no ownership
  check resolves them.
- `guest inventory` prints every guest with its tag, whether anything local
  resolves it, and whether the registry vouches for it. `--orphaned-only` and
  `--retained-only` narrow it.
- `guest retain --vmid <id> --purpose ...` adopts an existing guest into the
  registry (and `--forget` removes it). Needed on any install that predates
  the registry, and it changes controller state only — the guest is untouched.
- `cleanup-expired --reclaim-orphans --host-change-authorized` **stops, and
  never deletes**, guests that neither a lease record nor the registry vouches
  for. Cleanup only ever finalized lease-listed resources, and host power-off
  refuses while any guest runs, so one such guest kept the lab machine on for
  five days. Stopping is reversible and is all that is needed to unblock
  power-off; a controller that has lost a guest's record cannot vouch for its
  disk, so deletion stays manual.
- `storage gc` finds image volumes no guest config references. It **reports by
  default**; `--delete --host-change-authorized` removes only what that same
  run classified as unreferenced, and each deletion is audited with volid and
  size. Snapshot configs are scanned as well as live ones, because a snapshot's
  `vmstate` volume is listed as ordinary `images` content but referenced only
  from the snapshot; and if any config or snapshot cannot be read, nothing is
  classified at all.
- `storage status` reports a `class` per storage (`bulk` for
  `[storage] bulk_storage`, `fast` otherwise), so callers stop hardcoding site
  storage ids.
- Retained-guest backups: `backup --retained`, plus an opt-in watchdog sweep
  behind `[lease] retained_backup` (default **false**) and
  `retained_backup_interval_days`. When the watchdog runs it, it runs outside
  the controller lock and under its own non-blocking lock, so a long vzdump can
  neither block a lease operation nor be started twice by successive ticks.
- `doctor --host-checks` reports the node's `updates_pending`,
  `security_updates` and `reboot_required` as advisory fields, over the opt-in
  `[memflow]` host SSH channel. Patching stays manual and outside this tool.
- `doctor` also reports orphaned guests — failing when one is *running*, since
  that is what blocks power-off — and `retained_backup` coverage: how many
  retained guests exist, which have never been backed up, and the oldest
  backup age, whether or not the sweep is enabled.

### Changed

- A write that would place a *guest disk* on the configured bulk storage now
  warns unless `--slow-storage-accepted` is passed. An ISO mounted from bulk
  storage is not warned about; that is the recommended arrangement. The lab's
  USB store measured about 25 MB/s, enough to turn an I/O comparison into a
  measurement of the cable.
- `docs/long-term-leases.md` no longer implies that weekly backups are a
  general safety net: they only ever covered an active long-term lease's
  guests. The gap, and how to close it, is now stated.
- Standalone-node `ipcc_send_rec` log noise and zombie `qm terminal` children
  are documented in `docs/INSTALL.md`. Neither is fixable here: the first is
  pmxcfs with no cluster to write to, and `termproxy` is spawned by pvedaemon
  and reaps its own child, so the controller never owns those processes.

### Fixed

- Test isolation: `register_resource` writes the retained registry under
  `STATE_ROOT`, and one pre-existing test did not redirect that root, so
  running the suite wrote a bogus retained guest into the developer's live
  controller state. Every test module now points
  `PROXMOX_AGENT_LAB_STATE` at a disposable directory, and a guard test
  asserts it.

## 0.8.0 - 2026-08-21

Ten reported issues, fixed and verified against the lab node. The serial and
cleanup fixes change observable behaviour; see the notes at the end.

### Fixed

- **The serial stream is guest bytes only.** `console text`, `--follow` and
  `console bridge` shared an incomplete filter that stripped the `OK`
  handshake and nothing else, so Proxmox's own terminal records reached
  callers as if the guest had printed them -- contaminating boot logs and
  potentially reaching a kernel debugger's input. Complete transport records
  are now removed for every consumer of a session, including when one is split
  across websocket reads, when a blank line or console echo precedes it, and
  for the pair an LXC console emits. A guest line that merely begins with `OK`
  is no longer truncated.
- **`console text` can follow the documented attach-before-power-on order.**
  Proxmox issues a terminal ticket for a *stopped* guest and reports the
  refusal in the byte stream (`VM <id> not running`), so a capture started
  before power-on used to exit 0 having recorded that one sentence where boot
  output should have been. Attachment now asks `status/current`, and the new
  `console text --wait-for-guest SECONDS` waits, bounded, for the guest to
  start and attaches as soon as it does. `--from-reset` remains the only way
  to guarantee output from t=0.
- **`journal query` and `journal summary` read the configured backend.** With
  `[audit] backend = "jsonl"` both opened `journal.db` and reported a
  configured JSONL ledger as empty -- worst of all during an incident review.
  They now read the daily files, with the same `lease`, `event` wildcard,
  `since` and `limit` filters, newest first.
- **`upload` honours `[proxmox] verify_tls`.** It always passed curl
  `--insecure`, so an operator who had enabled certificate verification still
  got an unverified upload path for ISO and import content.
- **The console WebSocket honours `[proxmox] verify_tls`.** VNC and serial
  sessions unconditionally disabled certificate and hostname checking, leaving
  guest input and output on the one connection to the node that trusted any
  certificate.
- **`storage add-disk` fails non-zero when it only half-succeeded.** Formatting
  the disk and registering the storage but failing to set its content types
  printed a warning and exited 0, so automation went on to upload content the
  storage would not accept. The partial result is still printed -- the disk is
  already formatted, so recovery is `storage set-content`, not a rerun.
- **Expiry cleanup will not destroy a resource another lease is using.**
  Registration never prevented two leases from naming the same guest, and
  finalization stopped and deleted every registered resource unconditionally,
  so a watchdog sweep could delete a VM a newer, still-live lease was working
  on. Cleanup now checks ownership first and reports what it left alone as
  `left_to_another_lease`; two *expired* leases still release a guest, so
  nothing leaks. `lease-register` refuses a guest a live lease already owns.
- **`cleanup-expired` retries a lease whose cleanup failed.** A transient QEMU
  lock while stopping one guest left the lease `cleanup_failed`, which every
  later sweep skipped -- so its guests, and the host, stayed up until someone
  reran `lease-end` by hand with the exact lease id. Such leases are now swept
  again (finalizing is idempotent) and reported as `retried`.
- **`doctor` reports an unusable audit mirror.** With `[audit] git_sync = true`
  pointing at a missing, non-repository, dirty or unwritable checkout, every
  mutating command printed a warning that is easy to miss for weeks while
  `doctor` said nothing. It now reports `audit.git_status` and fails, and
  reports `audit.spooled_records` so a local backlog the configured backend
  refused is visible too.

### Added

- `console screenshot --via monitor` is an explicit fallback for when the VNC
  path cannot produce a frame. It asks QEMU for a `screendump` through the
  monitor endpoint, fetches the PNG over the opt-in `[memflow]` host SSH
  channel, and deletes the host copy in a `finally` path. The host path is
  fixed and lease-scoped, the only format is PNG, the bytes are verified to be
  one, and no remote path can be passed in. Arbitrary `qm monitor` commands
  remain unavailable. VNC stays the default and the preferred route.

### Changed

- `console text` on a stopped guest now exits non-zero with an explanation
  instead of exiting 0 with a capture containing `VM <id> not running`.
- `storage add-disk` exits non-zero on partial success (see above).
- `cleanup-expired` output gains `retried` and `left_to_another_lease`; its
  first sweep after upgrading may clean up a `cleanup_failed` lease that
  previous versions had been skipping.

## 0.7.0 - 2026-08-18

### Added

- `iso diagnose --path <file>` inspects a boot/install ISO entirely locally:
  volume identity plus, from the El Torito boot catalog, whether it has
  bootable BIOS and/or UEFI entries and a hybrid MBR/GPT for USB booting -- so
  the "boots under SeaBIOS but not OVMF" case is caught before install.
- `disk boot-info` parses MBR and GPT partition tables (types, GUIDs, ESP,
  protective MBR) from a local image (`--image`) or a stopped guest's disk
  (`--vmid`).
- Offline guest-filesystem access for a powered-off guest, over the memflow
  host channel via libguestfs: `disk ls`, `disk read`, and `disk write`
  (hard-gated behind `--i-understand`), plus `disk host-setup` to install the
  host side. All refuse a running guest.
- `virtio` command group for porting and debugging virtio drivers on any guest
  OS: `virtio inspect --vmid` reports the configured virtio devices and their
  live negotiated feature bits from the read-only QEMU monitor, and
  `virtio decode --value 0x... --device net|blk|scsi` names every bit in a
  feature word offline. The monitor path is hard-restricted to an allowlist of
  read-only `info` queries, so it can never mutate a guest.

- `memflow boot-diagnose` diagnoses a stuck boot from a guest's RAM without
  entering the guest: it samples the vCPU registers twice to tell a wedged CPU
  (panic spin, HLT loop, firmware dead end) from one still executing, and scans
  guest-physical memory for the text a failed boot leaves behind (Linux kernel
  panic / unmountable root, dracut emergency, GRUB rescue, BIOS "no bootable
  device", Windows boot errors). Read-only, any guest OS; matched text is not
  audited, only the failure category.

## 0.6.6 - 2026-08-18

### Added

- A PocketBase **superuser token** stored as the audit token is detected on
  first use and converted automatically into a permanent least-privileged
  agent: the controller provisions the agent with the superuser token, stores
  the agent's password credentials, and atomically replaces the audit token
  with the agent's renewable one. Pasting a superuser token is now a one-time
  bootstrap rather than a standing over-privileged credential.

## 0.6.5 - 2026-08-18

### Added

- An expired or rejected PocketBase audit credential no longer aborts the
  action being audited: the event is spooled append-only to
  `<journal_dir>/spool.jsonl` with a stderr notice, and
  `journal --flush-spool` uploads the backlog once credentials are fixed
  (duplicates are skipped via the collection's unique `event_id`).
- `console text --from-reset` (with `--follow`) attaches the serial session
  before resetting the guest, so boot output from t=0 is captured instead of
  being lost to the connect race. Lease-gated, QEMU only.
- `console text --send-raw` transmits exactly the given characters with no
  trailing newline, for kernel-debugger prompts (KDB `cont`, GRUB menus) that
  act on bare characters.
- `console bridge` help and `docs/console.md` now document reset semantics up
  front: a guest reset keeps the QEMU process and its serial socket alive (a
  connected bridge survives it), while stop/start replaces the process — and
  the bridge is bidirectional, so no read-only `socat -u` fallback is needed.
- Journal and audit commands now print a stderr notice when the PocketBase
  token is nonrenewable and expires within 48 hours, before every read and
  write would start failing hard, with the renewable-agent fix spelled out.
- `oci validate` checks a registry reference offline against the accepted
  grammar and prints the template volume a pull would produce, without
  touching the Proxmox host.

### Fixed

- `oci pull` no longer rejects valid references whose first repository path
  component contains `-`, `_`, or `.` (for example
  `ghcr.io/home-assistant/home-assistant:stable`), and the tag grammar is now
  ASCII-only instead of accepting any Unicode word character.


## 0.6.4 - 2026-08-16

### Added

- Experimental `oci pull` and `oci create` commands for Proxmox VE 9.1+
  OCI-to-unprivileged-LXC workloads. Pulling requires explicit authorization
  for persistent host template storage and PVE's mutable-tag-only registry
  limitation; creation is lease-owned, unprivileged, non-onboot, and
  ordinary-lease-only. Documentation makes clear that this is not QEMU-grade
  isolation and must not run untrusted code.

## 0.6.3 - 2026-08-16

### Fixed

- The daily update-check cache is invalidated when the controller version
  changes, so an upgraded controller cannot display an obsolete update notice.

## 0.6.2 - 2026-08-16

### Fixed

- Tag-release CI now updates a pre-existing GitHub release and replaces its
  generated assets instead of failing after a manual release was published
  first.

## 0.6.1 - 2026-08-16

### Added

- OMP project discovery through `.agents/skills/proxmox-agent-lab/SKILL.md`,
  with delegated-agent lease and mutation boundaries documented in the
  canonical skill.

### Fixed

- Both the wheel and source distribution now ship the OMP skill layout.

## 0.6.0 - 2026-08-14

### Added

- `lease-abandon --lease <id> --confirm` safely closes an ordinary stale or
  foreign lease only after every registered guest is verified stopped or
  absent. It never mutates guests or the host and refuses long-term,
  unreachable, running, and indeterminate leases.
- `console click --empty-space` explicitly permits an in-bounds click with no
  target when the operator intends to click unlabelled screen space. The
  existing target-verification gate remains the default, and unverified clicks
  are audited.

### Fixed

- Guest power actions (`start`, `stop`, `shutdown`, `reset`, and `suspend`) now
  enforce lease ownership before reaching the Proxmox API.
- `lease-begin` removes a just-persisted lease if a later setup step fails,
  retaining the original failure instead of stranding an active lease.
- `lease-end` can retry an owned guest deletion without destroying
  unreferenced disks after an unrelated storage I/O failure, allowing cleanup
  and verified host shutdown to proceed.
- Expired PocketBase audit credentials now identify the audit-token refresh
  action rather than reporting a misleading superuser failure; completed API
  writes remain explicitly reported as unrecorded.
- `guest run` preserves its parsed argv for guest-agent, serial, and detached
  execution paths instead of lossy command-string reconstruction.
- Guest probes require a successful guest-agent execution, so Proxmox HTTP 596
  no longer reports a nonfunctional agent as available.
- `check-public.py` ignores generic lowercase Proxmox node fixture values while
  retaining detection of distinctive local-site markers.

## 0.5.3 - 2026-08-14

### Added

- `android-x86` recipe (`proxmox-lab recipe android-x86`): installs the
  android-x86 project's own OS as a real QEMU guest, distinct from `android
  create`'s SDK emulator, for testing that need a genuine device network
  stack -- most commonly, driving Android's own proxy settings from an
  external tool. Verified live end-to-end, including
  `adb shell settings put global http_proxy` actually redirecting device
  traffic to an external listener.
- `console screenshot-burst`: captures several screenshots over time
  (default 6 over about a minute) and stitches them into one labeled PNG,
  for watching a progress bar, installer copy step, or boot animation
  without a manual sleep-then-screenshot loop.
- `console has-gui-locked-up` / `console has-terminal-locked-up`: best-effort
  liveness probes. The GUI check moves the pointer and diffs the screen
  before/after; the terminal check samples a text console over ~2 seconds
  and checks for any change (a live cursor normally blinks on its own).
  Both use the existing pixel-diff helper -- no cloud vision call -- and
  report a caveat alongside the verdict rather than a bare bool.

### Fixed

- `pocketbase-host-setup.sh` and `minio-host-setup.sh` now set `--onboot 1`
  on the LXC they create, so an audit backend or S3 bucket hosted on the
  lab host itself comes back after the host powers off between leases --
  previously nothing would restart it.
- `ensure_on()` now tolerates its own audit backend being briefly
  unreachable right after waking the host (an onboot LXC starting
  alongside it), retrying for up to 30s before warning and continuing,
  instead of failing the power-on itself.

### Changed

- PocketBase audit controllers can refresh renewable superuser and restricted
  agent JWTs before expiry. `journal --provision-pocketbase-agent` now uses
  locally stored superuser credentials to create a least-privileged
  password-authenticated audit account; it persists only the generated agent
  credentials and active token in the configured secret store.

## 0.5.2 - 2026-08-14

### Added

- Guided PocketBase setup: `install.sh` asks whether the audit backend should
  be an existing PocketBase service or a new one, and `pocketbase-host-setup.sh`
  provisions a persistent unprivileged LXC running it as a restricted systemd
  service.
- Guided MinIO setup for the S3 scratch bucket: `install.sh` asks whether it
  should be skipped, an existing bucket, or a new MinIO LXC, and
  `minio-host-setup.sh` provisions a minimal, unprivileged LXC running a
  version-pinned, checksum-verified MinIO server (S3 API only, no browser
  console), creating the bucket and an access key.
- `wake-on-lan+home-assistant` power mode: sends the magic packet and
  triggers a Home Assistant script together on every power-on, for a host
  where WoL alone isn't reliable enough to trust by itself but a smart-plug
  or KVM fallback is also available. Force-off still goes through Home
  Assistant, since WoL cannot cut power.
- `journal --summary` on the PocketBase backend now reports the first/last
  event timestamp and a labeled recent-sample event-type breakdown, instead
  of only a bare count.

### Fixed

- `lease-end`/the watchdog no longer power off the host while any guest is
  still running, even one outside lease tracking (a persistent builder kept
  alive across sessions via `guest template`/`guest clone`, or one driven
  directly by VMID). Previously the decision looked only at whether any
  lease was still active.
- Finalizing a lease whose guest was already gone (deleted by a prior run,
  or by hand) no longer leaves the lease permanently stuck in
  `cleanup_failed`; deleting an already-gone guest is now treated the same
  as the existing "already stopped" idempotency `stop_guest` had.
- A qemu-guest-agent command killed by a signal is no longer reported as a
  successful run. `exitcode` and `signal` are mutually exclusive in the
  guest agent's response; a missing `exitcode` was being treated the same
  as the serial channel's legitimate "no code available," masking real
  failures on the one channel that normally always reports one.

## 0.5.1 - 2026-08-13

### Added

- Optional PocketBase audit backend. Configure `[audit] backend = "pocketbase"`
  with a URL, private collection, and Keychain/secret-store token; `doctor`
  verifies token availability and collection reachability.
- `journal --provision-pocketbase` creates or validates the private audit
  collection without altering an existing schema. `journal
  --migrate-sqlite-to-pocketbase` performs an idempotent, read-only SQLite
  ledger import using deterministic event IDs and reports count, time range,
  and SHA-256 digest before the explicit backend cutover.

## 0.5.0 - 2026-08-11

### Added

- `push`/`pull` move files above 32 MiB in chunks with end-to-end SHA-256
  verification on Linux guests; `--chunk-size` tunes the part size and a
  `pull --sha256` that already matches skips the transfer entirely
  (resumable, idempotent retries).
- `guest template` / `guest clone` promote a stopped lease-owned guest to a
  template and clone it back (auto-registered), so a provisioned builder is
  reusable across leases in seconds.
- `guest run --detach` starts long builds in the background and returns a pid;
  `guest log --follow` streams their output and `guest wait` blocks until
  exit, reporting the recorded exit code.
- `console bridge` exposes a guest serial console on a local TCP port so
  kernel debuggers (rosdbg, windbg, gdb) can attach instead of hitting the
  "connect a debugger" wall.
- Optional lease-owned DHCP and TFTP servers: `net dhcp-create` (with PXE
  `--bootfile`/`--next-server`), `net tftp-create`, `net tftp-push`, and
  `net dhcp-leases` — a minimal PXE stack on the lab bridge, spawned only on
  demand.
- `lease-end` prints a hint when a lease is ended seconds after it began,
  nudging agents to reuse one lease per work session instead of paying a boot
  cycle each attempt.

### Fixed

- The checkout wrapper now pins a Python 3.11+ interpreter: under a minimal
  PATH (supervised processes, cron) `python3` could resolve to the macOS
  system 3.9, which silently degraded the config to defaults and broke every
  API call with "This install is not configured yet".
- DHCP/TFTP spawns get an egress NIC on the home bridge so `apt` provisioning
  works from the isolated lab bridge, and the TFTP root is created before
  dnsmasq starts (dnsmasq refuses to start when `tftp-root` is missing).
- `guest template` / `guest clone` wait for their async Proxmox tasks, so
  cloning immediately after a conversion no longer races the template
  config.

## 0.4.0 - 2026-08-10

### Added

- `console screenshot --ocr` works out of the box: an embedded public-domain
  VGA 8x16 font is installed on first use when no table has been imported,
  so legacy-only guests (Windows 2000 setup, DOS, BIOS screens) no longer
  need a Linux guest to pull a PSF from (#32).
- `storage download-url` accepts `sha1` checksums (digest prefixes, `SHA1=`,
  and bare 40-hex digests) — archive.org media publishes sha1 but not sha256
  (#28).
- `console click` verification now requires a vision-reported control bounding
  box containing the click point, instead of accepting a model-echoed point;
  verified clicks report the matched `control_bbox` (#27).
- Screenshots (and `--screenshot-after` captures) flag pixel-identical repeat
  frames as possibly stale, so a QEMU VNC dirty-tracking glitch cannot pass
  an old page to a vision read unnoticed (#29).
- `console inspect` vision failures surface per-provider diagnostics and are
  recorded as `console-vision-inspect-failed` audit events (#30).
- The api wrapper warns when PVE persists a different boot order than the
  requested one (e.g. `ide2;ide0` stored as `ide0;ide2` when the CD attach
  and boot order are set in one call) (#26).

### Fixed

- Guest creation now registers the new guest under `controller_lock()` with a
  reloaded lease, so concurrent creations no longer clobber each other's
  lease registrations and `console inspect` no longer refuses a freshly
  created guest (#25).
- Journal git sync retries a non-fast-forward push (refetch, rebase, push)
  instead of failing every command under concurrent CLI processes (#31).

### Docs

- GUI-installer memory floors and the recovery path for interrupted
  OpenIndiana/illumos installs (bootfs, `bootadm install-bootloader`) (#23).
- Note that some legacy ISOs (Ubuntu 14.10 server isolinux) ignore Tab and
  typed keys at the boot menu, blocking the serial-console kernel-cmdline
  shortcut; the installer then runs on VGA (#24).

## 0.3.5 - 2026-08-09

### Added

- Deterministic OpenBSD 7.9 and user-media Windows ME recipes prevent small
  models from confusing clone/create fields, storage content, legacy hardware,
  and Server-only helpers.

### Fixed

- Bootstrap now refreshes a cached CLI older than the bootstrap script itself,
  even when the once-daily GitHub release check is not due.
- Storage downloads accept `sha256:<digest>` and similar checksum prefixes,
  selecting the matching Proxmox checksum algorithm automatically.

## 0.3.4 - 2026-08-09

### Added

- Checksum-pinned DragonFlyBSD/HAMMER and Haiku/BFS recipes give small models
  correct storage roles, quoted boot-order values, and strict phase ordering.

### Fixed

- API methods are case-insensitive, avoiding needless retries for `get` versus
  `GET`.
- RFB and WebSocket failures are rendered as concise CLI errors instead of raw
  Python tracebacks.

## 0.3.3 - 2026-08-09

### Added

- `recipe reactos` gives small models a checksum-pinned, machine-readable
  install runbook with enforced phase ordering and explicit invalid shortcuts,
  avoiding SourceForge discovery, binary output, and pre-VM console commands.

### Changed

- Standalone `power-on` now requires `--standalone-authorized`; ordinary agent
  work must use `lease-begin` so every wake-up has a cleanup owner.

## 0.3.2 - 2026-08-09

### Fixed

- Bootstrap and CLI now share one update-check cache, enforcing the 24-hour
  GitHub request limit across both startup paths rather than once per path.

## 0.3.1 - 2026-08-09

### Fixed

- Startup checks GitHub for a newer release at most once per 24 hours. Cached
  bootstrap environments self-update, while network failures are cached and
  never block lab work.
- Skill guidance distinguishes a configured but powered-off host from a
  genuinely missing setup, preventing needless requests for existing secrets.

## 0.3.0 - 2026-08-08

### Added

- Automatic vision races NVIDIA and both OpenRouter routes and returns the
  first structurally valid answer with provider timing.
- Later model-input screenshots dim stable pixels and retain bright,
  magenta-outlined changes. Baselines are isolated by lease.
- `lease-release --confirm` closes a long-term lease while preserving guests
  registered with policy `retain`.

### Changed

- Single-provider vision results report their elapsed time and strategy.
- The GUI playbook prefers keyboard selection inside open popup menus.

## 0.2.1 - 2026-08-08

### Fixed

- Guarded clicks accept legitimate two-character control labels such as
  Haiku's `OK` button while continuing to reject empty and one-character
  targets.

## 0.2.0 - 2026-08-08

### Added

- Console input commands can capture a settled post-action screenshot in the
  same response with `--screenshot-after`.
- Coordinate clicks now require one visible target label and perform their own
  vision-confirmed cursor checkpoint; the self-confirmation flag is gone.
- A bounded GUI-installer playbook gives small models a deterministic workflow
  and delegates graphical checkpoints to vision instead of external OCR.
- An opt-in, lease-bound `console inspect` wrapper tries NVIDIA Nemotron Nano
  12B v2 VL, the named OpenRouter Nemotron Omni free endpoint, then
  `openrouter/free`, using keys from the OS secret store.

### Fixed

- Cold lab starts now use the configured boot timeout and reject waits shorter
  than 90 seconds, preventing premature failure and duplicate leases.
- Git audit sync now keeps the local SQLite ledger queryable while copying
  redacted JSONL records to a dedicated private `logs` branch.
- A project-scoped OpenRouter key now wins over a stale inherited shell key;
  free-router singleton JSON responses are normalized and validated.
- Cloud vision receives a labelled, same-size 100-pixel coordinate grid while
  the original screenshot remains untouched. Failed, ambiguous, and timed-out
  cursor verdicts cannot click, and agents are explicitly stopped from using
  reboots or storage mutations as GUI recovery.
- Log sync fails closed on a dirty checkout and can stage only the daily
  journal file, preventing source edits from entering audit commits.

## 0.1.0 - 2026-08-08

First public beta release of the leased, auditable Proxmox lab controller.

### Added

- “Old Computer → AI Lab” public-facing identity for authorized research.
- GitHub CI, contribution guidance, responsible-use guidance, security policy,
  and structured issue templates.
- Per-site Windows template configuration and a `--template-vmid` override.

### Fixed

- Windows 2022 installs now select the `2k22` VirtIO driver directory by
  default instead of inheriting the Windows 2025 default.
- Console-share tests now close HTTP responses and server sockets cleanly.
