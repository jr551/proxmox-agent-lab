# Changelog

All notable changes to this project will be documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases use
[Semantic Versioning](https://semver.org/).

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
