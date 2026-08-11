# Changelog

All notable changes to this project will be documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases use
[Semantic Versioning](https://semver.org/).

## Unreleased

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
