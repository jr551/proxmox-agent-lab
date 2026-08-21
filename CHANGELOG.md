# Changelog

All notable changes to this project will be documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases use
[Semantic Versioning](https://semver.org/).

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
