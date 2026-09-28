# Rework inventory — codebase facts for the SSH + SQLite rework

Read-only inventory produced 2026-09-25 at package version **0.18.0**, to feed the
keep/cut decisions in `docs/rework-plan.md`. Facts only; the **Hunch** columns carry a
one-word initial reaction (KEEP / CUT / REWRITE / TRIM), not a recommendation.

**Touchpoint legend** used throughout:

| Tag | Meaning |
|---|---|
| `API` | Proxmox HTTPS API — `api.py` (urllib + TLS + token); facade root is `cli.API_ROOT = https://<host>:<port>/api2/json` |
| `WSS` | Proxmox console websockets — `/api2/json/nodes/<node>/<kind>/<vmid>/vncwebsocket` via `ws.py` / `serial.py` |
| `SSH` | Opt-in host SSH channel — `host_transport.py` (subprocess `ssh`), gated on `[memflow] ssh_host` being configured |
| `MariaDB` | Shared audit ledger — `mariadb.py` (PyMySQL), host-published port 3306 |
| `S3` | Presigned-URL scratch space — `s3.py` (SigV4 with stdlib), MinIO |
| `sock` | Raw sockets (UDP Wake-on-LAN, TCP listeners) |
| `ext-HTTP` | Other outbound HTTP (Home Assistant, GitHub, vision providers) |
| `ledger-sqlite` | Legacy read of `journal.db` (pre-MariaDB SQLite), see §5 |

Scope covered: **57 Python files (23,844 lines) in `src/proxmox_agent_lab/`** plus 3
`android_scripts/*.sh` and 4 non-Python files in `resources/`; **36 test files + 2
support files + 1 fixture**; **34 `docs/*.md` pages + 1 doc image + 9 top-level `.md`**;
**8 scripts, 5 top-level shell installers, 2 workflows, 1 git hook**.

---

## 1. Module inventory — `src/proxmox_agent_lab/`

Sorted alphabetically. "Top importers" is the static import graph (AST scan, including
function-level lazy `from . import x` — several modules import `host_transport` only
inside function bodies). "Tests" lists suites that reference the module directly;
indirect coverage is noted as such.

| Module | ~Lines | Purpose | External touchpoints | Top importers | Tests | Hunch |
|---|---|---|---|---|---|---|
| `__init__.py` | 7 | Package docstring; single `__version__` constant synced with `pyproject.toml`/`bootstrap.sh` by release checks. | none | package entry point | test_release (version/tag/changelog), CI smoke | KEEP |
| `__main__.py` | 4 | `python -m proxmox_agent_lab` → `cli.main()`. | none | — (entry point) | indirect (CLI suites) | KEEP |
| `android.py` | 429 | Android devices as QEMU guests (arm64 full-emulation rationale, device profiles, AVD provisioning driver); ships/streams `android_scripts/*.sh` into the guest. | API (via facade `wait_task`), console reuse | cli | test_android | CUT |
| `api.py` | 147 | Proxmox HTTPS transport: TLS setup, request construction, error mapping, bounded task polling. Knows nothing of leases/audit/parsing. | API (urllib), secrets_store (API token) | cleanup, cli, leases | indirect (test_lifecycle, test_proxmox_lab) | REWRITE → SSH |
| `audit.py` | 144 | Audit facade: event redaction, ledger caching, local spool, one-time auto-migration of legacy ledgers (`auto_migrate_once`, invoked from the audit write path at `audit.py:139` on every recorded event until the marker exists). | MariaDB (via journal), local `spool.jsonl`, `socket.gethostname` (controller id) | cli, diagnostics | indirect (test_android, test_console, test_diskactivity, test_guest, test_netgw, test_oci via `lab.audit`); test_diagnostics (spool visibility) | REWRITE → SQLite |
| `bootstruct.py` | 509 | Pure parsers for on-disk boot structures: MBR, GPT, ISO 9660 El Torito. Decoders only — no host, no I/O. | none (pure) | disk, isoinspect, pe | test_bootstruct, test_disk_iso, test_pe | CUT (except if `disk`/`pe` survive) |
| `cleanup.py` | 927 | Guest teardown, orphan recovery, host power-off. Orchestration takes `lab` facade; dependency-safe destroy order; verifies shutdown by repeated API failure. | API (api.py + facade), state lock, power, inventory | cli | test_lifecycle, test_proxmox_lab | KEEP |
| `cli.py` | 1281 | Parser construction, command policy gates, config load, the `lab` facade every feature module binds to (`from .cli import _bind`); registers all subcommands; holds `API_ROOT`; triggers journal auto-migration; catches `journal_module.sqlite3.Error`. | API root, MariaDB (journal commands, `journal --migrate`), secrets_store, socket | `__main__` + nearly all feature modules (android, connection, console, crash, disk, guest, hostinfo, ioworkload, longterm, memflow, netcap, netgw, share, storage, transfer, usb, virtio, windows) | most suites (test_proxmox_lab, test_lifecycle, test_diagnostics, …) | REWRITE → MCP |
| `config.py` | 442 | TOML site configuration: search order (`$PROXMOX_AGENT_LAB_CONFIG`, checkout, XDG, default), defaults, `require()` complaints, template values. Nothing raises on import. | none (local file) | android, cli, connection, diagnostics, host_transport, longterm, memflow, netgw, power, s3, secrets_store, share, storage | test_abstractions, test_connection, test_onboarding, test_share, test_windows, test_windows_host | KEEP |
| `connection.py` | 224 | "Share a lab connection": build a pasteable setup block for a second machine; explicit credential handoff; config+secrets export with no secrets in argv/logs. | secrets_store, config; generates ssh/Tailscale setup text | cli | test_connection | REWRITE (depends on shared-secrets) |
| `console.py` | 1670 | Screenshots, text, keys, clicks, screen inspection across RFB/serial/guest-agent; QEMU `screendump` fallback **over host SSH**; `screenshot --for-model` uploads a bounded base64 copy via S3; local TCP listener (port-forward helper). Re-exports `serial`/`guest_agent`/`transfer` names. | API/WSS, SSH (monitor fallback), S3 presigned upload, vision | android, cli, guest, netgw, share, windows | test_console (largest suite, 2837 lines), test_guest, test_netgw, test_share, test_windows, test_proxmox_lab | REWRITE |
| `crash.py` | 404 | Offline, build-pinned crash address symbolization; manifest boundary; runs a local `llvm-symbolizer`. | subprocess (llvm-symbolizer); no network | cli | test_crash | CUT |
| `des.py` | 166 | DES-ECB, only to answer the RFB VNC-authentication challenge (security type 2). Implemented in stdlib because the system interpreter has no crypto library. | none (pure) | rfb | test_console | KEEP if RFB stays |
| `diagnostics.py` | 589 | Repair surface: `init`, `doctor`, `status`, `secrets`, `journal`. Reads config/errors through the `lab` facade; provisions MariaDB container + pxl-hostguard over host SSH (`journal host-setup`); surfaces legacy-secret and ledger migration state. | MariaDB (`HOST_SETUP_SCRIPT` streaming, `migrations()`), SSH channel, secrets_store, inventory, hostguard | cli | test_diagnostics | REWRITE |
| `disk.py` | 292 | Offline guest-disk debugging: boot-info from a stopped guest's disk; streams libguestfs/bash scripts over SSH for mount/write jobs; `disk host-setup` installs host tooling. | SSH (`host_transport.run` with stdin scripts), host-change gate | cli | test_disk_iso | CUT |
| `diskactivity.py` | 556 | Ground truth for "is this guest writing to its disk": cross-check that replaces the untrustworthy Proxmox `diskwrite` counter (seen reading 0); QEMU qcow2 attribution on the host. | SSH (host queries), audit | guest | test_diskactivity | CUT |
| `errors.py` | 18 | `LabError` base type: deliberate, user-facing operational failures (never programming errors). | none (pure) | api, cleanup, cli, connection, crash, diagnostics, ioworkload, leases | indirect everywhere | KEEP |
| `guest.py` | 883 | One way to talk to a guest regardless of channel: prefers qemu-guest-agent, falls back serial; also template/clone/create and detached command runs. | API (`wait_task`, agent endpoints), audit, inventory, console | cli | test_guest, test_proxmox_lab, test_abstractions (channel selection) | KEEP |
| `guest_agent.py` | 203 | qemu-guest-agent primitives: exec with real exit codes, file get/put, readiness/bootstrap. | API agent endpoints (via facade) | console, transfer | test_android, test_guest, test_netgw | KEEP |
| `host_policy.py` | 34 | Controller-side capability policy for container-only VPS installs: `check_api` rejects QEMU/host-power API paths, `check_command` rejects VM-device commands when `guest_mode=lxc-only`. | gates API paths centrally inside `api.py` | api, cleanup, cli | test_onboarding | REWRITE (API-path gate must be re-expressed under SSH) |
| `host_transport.py` | 231 | The one opt-in SSH channel to the Proxmox host: `require_host_ssh` gate, `run`/`host_run`, `host_read_bytes`, `host_mkdir`, `host_remove_file`. Off until `[memflow] ssh_host` is set. | SSH (subprocess `ssh`), config | lazy-imported by console, diagnostics, disk, diskactivity, hostinfo, memflow, netcap, usb | test_console, test_diagnostics, test_diskactivity, test_memflow, test_netcap, test_usb | KEEP (becomes the backbone) |
| `hostguard.py` | 75 | Host-side lease-guard glue: pure decision helpers + systemd unit text; embeds `resources/pxl-hostguard.py` as `GUARD_SCRIPT`, installed by `journal host-setup`. | none in-process; the installed guard script talks MariaDB | diagnostics | test_hostguard | CUT (guard reads MariaDB) |
| `hostinfo.py` | 103 | Read-only host hardware inspection (hwmon temperatures, NIC MACs) over the SSH channel. | SSH | cli | test_hostinfo | KEEP |
| `inventory.py` | 219 | Retained-guest registry: `code:lab:lease-<id>` tags prove which lease created a guest; prune/forget rules. | guest tags via facade, local state | cleanup, cli, diagnostics, guest, leases, longterm | test_abstractions, test_guest, test_lifecycle, test_longterm, test_diagnostics | KEEP |
| `ioworkload.py` | 516 | Bounded, portable record/replay of file-I/O traces (JSON Lines, checksums, deterministic descriptions — no paths/sizes). | none (local FS) | cli | test_ioworkload | CUT |
| `isoinspect.py` | 162 | Local diagnosis of why a boot/install ISO won't boot (BIOS vs UEFI El Torito findings). | none (local file parse) | cli, pe | test_disk_iso | CUT |
| `journal.py` | 346 | Audit ledger plumbing: append/query through MariaDB, local `spool.jsonl` when the DB is down, `flush_spool`, and **migration off the legacy ledgers** (SQLite `journal.db` + `*.jsonl`, marker `.migrated-to-mariadb`). | MariaDB (via `mariadb.py`), **`ledger-sqlite`** (read-only) | audit, cli, diagnostics, secrets_store | test_mariadb, test_abstractions (migration), test_diagnostics | REWRITE → SQLite (this is the migration surface to collapse) |
| `leases.py` | 425 | Lease lifecycle: state files, expiry, registration, power-on helper, heartbeat; data-layer functions take state paths explicitly (patchable). | API (imports `api`), state, power, audit, inventory | cli, transfer | test_lifecycle | KEEP |
| `longterm.py` | 388 | Long-term leases: host stays on, Proxmox `protect` flag on guests, backups, distinct close events; nothing powers the host down while one is active. | API (via facade), audit, state | cleanup, cli | test_longterm | KEEP |
| `mariadb.py` | 501 | The MariaDB ledger client: `SCHEMA` (`events`), `MIGRATIONS_SCHEMA` (`migrations`), `SECRETS_SCHEMA` (`secrets`), append/query/count, cross-controller migration lock, shared-secret get/set, `HOST_SETUP_SCRIPT` loader (streams `resources/ledger-host-setup.sh`). | **MariaDB** via `pymysql` + `DictCursor` (guarded `ModuleNotFoundError` path); `cryptography` for sha256 auth | audit, cli, diagnostics, journal, secrets_store | test_mariadb (needs `PXL_TEST_MARIADB`), test_connection | CUT |
| `memflow.py` | 812 | Agentless guest memory introspection: requires `[memflow] ssh_host` + `memflow host-setup` (streams `resources/memflow-host-setup.sh`, builds Rust tool); reconstructs guest process list via `/proc/<qemu-pid>/mem` and QEMU gdbstub. | SSH, subprocess (host tools), audit | cli | test_memflow | CUT |
| `netcap.py` | 520 | Network capture (`tap<vmid>i0` pcap over SSH) + SSL inspection/MITM via disposable mitmproxy LXC (streams `resources/mitm-setup.sh`). | SSH, API (LXC lifecycle), audit | cli | test_netcap | CUT |
| `netgw.py` | 1066 | Forced-VPN egress: builds a gateway VM (DHCP/TFTP/DNS appliance) and leak-tests guests through it. | API, console, guest_agent, audit | cli | test_netgw, test_console | CUT |
| `oci.py` | 283 | Experimental OCI image → unprivileged LXC on Proxmox VE 9.1+ (`oci pull` / `oci create`). | API | cli | test_oci | CUT |
| `onboarding.py` | 456 | Experimental installer generation + host pairing: private `host-setup.py` bundles (ISO or VPS), pairing secrets, receiver orchestration. | TLS listener socket, urllib, hmac, subprocess (ISO build), local files | cli | test_onboarding | CUT |
| `onboarding_host.py` | 401 | Standalone first-boot payload executed on the target: enrolls, writes API token/keys, self-disables after success. Uses only stdlib shipped with Debian/PVE. | TLS/socket/urllib, local state | onboarding | test_onboarding | CUT |
| `pe.py` | 737 | Inspect/extract/rebuild/boot a **user-supplied** Windows PE ISO; external tools via subprocess; uploads go through `lab.cmd_upload` (S3). | subprocess (external tools), S3 upload (facade) | cli | test_pe | CUT (depends on `isoinspect`/upload path) |
| `png.py` | 386 | Stdlib-only PNG writer/reader/resampler (`zlib` + loops; no Pillow/numpy) for 8-bit non-interlaced PNGs. | none (pure) | console | test_console | KEEP |
| `power.py` | 225 | Powering on/off: Wake-on-LAN magic packets (UDP broadcast to directed + global broadcast), Home Assistant HTTP hook, configured shell commands, graceful-vs-forced modes. | `sock` (UDP WOL), `ext-HTTP` (Home Assistant via urllib), subprocess commands, secrets_store (`home-assistant-token`) | cleanup, cli, diagnostics, leases | test_abstractions | KEEP |
| `recipes.py` | 405 | Small read-only runbooks for models: checksum-pinned ReactOS facts, legacy SeaBIOS/IDE recipe, media-upload recipe. Machine-readable JSON, no network. | none (pure) | cli | test_pe | KEEP/REWRITE |
| `resources/pxl-hostguard.py` | 331 | Root watchdog **installed on the Proxmox host** (systemd timer): reads leases from the MariaDB ledger, kills abandoned guests, powers the host off when every lease is over. | **pymysql → MariaDB** (reads `/etc/pxl-hostguard.json`), subprocess (power) | embedded by `hostguard.GUARD_SCRIPT`, written by diagnostics `journal host-setup` | none directly (decision logic twin `hostguard.py` → test_hostguard) | CUT |
| `rfb.py` | 340 | Stdlib-only VNC (RFB) client for QEMU consoles; Raw/CopyRect/Zlib encodings only. | TCP to VNC port (often tunnelled via `ws`) | console | test_console, test_proxmox_lab | KEEP if console stays |
| `s3.py` | 294 | AWS SigV4 presigned URL signing + S3 scratch helpers with stdlib (`urllib`, `xml`); only endpoints/bucket are recorded, keys stay local. | S3/MinIO endpoints (presigned GET/PUT; no SDK) | console, transfer | test_console (signing checked against known vector) | CUT |
| `secrets_store.py` | 340 | Secret storage over backends: `env` / `file` (0600 TOML) / macOS `keychain` / `secret-tool`, **plus a shared store in the MariaDB ledger** (`_read_shared` → `journal.settings_from_config` + `mariadb.get_secret`, bootstrap secret `mariadb-password`); explicit legacy-keystore reads (`read_legacy`). | subprocess (`security`, `secret-tool`), MariaDB (lazy import), env/files | api, audit, cli, connection, console, diagnostics, netgw, power, s3, share, vision | test_abstractions, test_connection, test_windows_host | REWRITE → simplify (backends removed) |
| `serial.py` | 374 | Guest serial / LXCTerminal transport: Proxmox termproxy websocket handshake + line discipline turning byte streams into terminal text. | WSS (via `ws.py`), API status records | console, guest_agent | indirect (test_console re-exports), test_ws covers `ws` | KEEP |
| `share.py` | 487 | Disposable console links: spawn a share-worker VM, establish a public tunnel, lease-scoped revocable URLs. | API, secrets (tunnel token), console, audit | cli | test_share | CUT |
| `share_server.py` | 587 | The public relay (`pxl-share`) that runs **on the share worker VM**, not the controller: stdlib `http.server` serving noVNC and relaying `vncwebsocket` with access control, file locks, TTLs. | Internet-facing HTTP socket, upstream API/WSS, TLS | none (shipped and run standalone on the worker) | test_share | CUT |
| `state.py` | 104 | Local file persistence + process lock: atomic JSON writes, controller lock (`fcntl`/`msvcrt`), no config/audit semantics. | none (local FS) | audit, cleanup, cli, diagnostics, leases, updates | test_windows_host (lock portability) | KEEP |
| `storage.py` | 547 | Physical disk & node storage management; every destructive action gated on `--host-change-authorized` / `--expect-serial` / `--expect-size`. | API, audit | cli | test_storage_guards, test_proxmox_lab, test_console | REWRITE |
| `textmode.py` | 69 | Terminal-text cleanup + cheap character-cell screen check (the glyph-matching OCR decoder was removed). | none (pure) | console, serial | test_console | KEEP |
| `transfer.py` | 387 | Guest file `push`/`pull` via S3 presigned chunked upload/download, SHA-256 verified both ends; no credential reaches the guest or argv. | S3 presigned URLs, guest_agent, audit, leases | console | test_console | CUT (S3 removed) |
| `updates.py` | 83 | Fail-open upstream release check against GitHub (≤ once/24h, cached under state dir, never blocks lab work). | `ext-HTTP` (GitHub urllib), state | cli | none directly | TRIM |
| `usb.py` | 274 | USB passthrough and usbmon traffic sniffing for lab guests. | SSH (usbmon/tcpdump on host), API (USB device pass-through) | cli | test_usb | CUT |
| `virtio.py` | 755 | VirtIO queue/device diagnostics for driver porting: advertised features, negotiated state, queue samples via QEMU monitor. | API (QEMU monitor), audit | cli | test_virtio | CUT |
| `vision.py` | 677 | Optional vision analysis of screenshots: NVIDIA, OpenRouter, and Kilo Code gateway routes; keys from secrets_store; only sent by explicit `console inspect --for-model`. | `ext-HTTP` (vision providers via urllib), secrets_store | console | test_vision | CUT |
| `windows.py` | 706 | Windows guest install (interactive VNC-driven or unattended `autounattend.xml`) + first-boot setup; auto-tap for the UEFI boot prompt. | API, console (click/type), `lab.cmd_upload` (S3), secrets_store | cleanup, cli, pe | test_windows, test_console (windows helpers) | CUT |
| `ws.py` | 306 | Stdlib-only WebSocket client for the Proxmox console endpoints; resumable reads; certificate verification is the caller's decision. | WSS to API host (`ssl`, `socket`) | console, serial | test_ws, test_console, test_proxmox_lab | KEEP |

### 1b. `src/proxmox_agent_lab/android_scripts/` (shipped data, run inside the guest)

| File | ~Lines | Purpose | Touchpoints | Driven by | Tests | Hunch |
|---|---|---|---|---|---|---|
| `01-install-sdk.sh` | 58 | Downloads/installs the Android SDK inside the provisioning guest. | guest-internal network | android.py (over console/guest-agent) | test_android | CUT |
| `02-create-avd.sh` | 29 | Creates the AVD (device profile). | guest-internal | android.py | test_android | CUT |
| `03-launch-emulator.sh` | 45 | Launches the emulator with the right flags/ports. | guest-internal | android.py | test_android | CUT |

### 1c. `src/proxmox_agent_lab/resources/` (shipped assets streamed to the host)

| File | ~Lines | Purpose | Touchpoints | Loaded by | Tests | Hunch |
|---|---|---|---|---|---|---|
| `ledger-host-setup.sh` | 100 | Template (`__CTID__`/`__STORAGE__`/`__BRIDGE__`/`__DBNAME__`/`__DBPASS__` placeholders): creates the MariaDB LXC, DNATs 3306 onto the hypervisor address, installs `python3-pymysql` + pxl-hostguard config + `__GUARD_INSTALL__`. | host root (pct/iptables), MariaDB | `mariadb.HOST_SETUP_SCRIPT` → diagnostics `journal host-setup` over SSH | indirectly via test_diagnostics | CUT |
| `pxl-hostguard.py` | 331 | The host-side watchdog (see module table). | pymysql → MariaDB, power commands | `hostguard.GUARD_SCRIPT` | logic twin only (test_hostguard) | CUT |
| `memflow-host-setup.sh` | 488 | Prepares host for memflow: installs Rust toolchain, builds `pxl-memflow` + `pxl-memflow-run`. | host root (cargo/pip) | `memflow.HOST_SETUP_SCRIPT` | test_memflow (fake-command path) | CUT |
| `mitm-setup.sh` | 77 | Builds the disposable mitmproxy LXC for SSL inspection/MITM. | host root (pct) | `netcap.MITM_SETUP_SCRIPT` | test_netcap (fake path) | CUT |
| `README.md` | 3 | One-liner describing the directory. | — | — | — | with parents |

---

## 2. Test inventory — `tests/`

Suite command: `python -m unittest discover -s tests -q` (pytest only in optional
`[dev]`; CI runs warning-clean). "Src modules needed" = modules the suite imports or
drives through the `lab` facade directly.

| Test file | ~Lines | What it covers | Src modules needed | Hunch |
|---|---|---|---|---|
| `test_abstractions.py` | 658 | Layers that hide platform/guest differences: config, secrets backends, power, audit ledger incl. **legacy SQLite+JSONL migration** (creates a real `journal.db`), guest channel selection. | config, guest, inventory, journal, power, secrets_store | REWRITE (migration tests die with MariaDB path) |
| `test_android.py` | 239 | Android device profiles + provisioning scripts. | android, audit, guest_agent | CUT |
| `test_bootstruct.py` | 370 | MBR/GPT/ISO boot-structure parsers via synthesized data. | bootstruct | CUT |
| `test_connection.py` | 170 | Connection handoffs preserve settings without exposing credentials (incl. mariadb/secrets references). | cli, config, connection, mariadb, secrets_store | REWRITE |
| `test_console.py` | 2837 | Console, image, transfer, Windows helpers, RFB against a scripted in-memory server, S3 signing vectors, host-SSH monitor fallback, netgw/textmode/des/png/ws. Nothing touches the network. | audit, cli, console, des, host_transport, netgw, png, rfb, s3, storage, textmode, transfer, windows, ws | REWRITE (biggest blast radius) |
| `test_crash.py` | 141 | Exact-build crash symbolization. | crash | CUT |
| `test_diagnostics.py` | 232 | Doctor reports, spool visibility, inventory judgement, host-update checks (fake SSH). | cli, config, host_transport (+ diagnostics indirectly) | REWRITE |
| `test_disk_iso.py` | 235 | Offline iso and disk command modules. | bootstruct, disk, isoinspect | CUT |
| `test_diskactivity.py` | 579 | The diskwrite cross-check: exact endpoints, host queries, counter parsing, guards. | audit, cli, diskactivity, host_transport | CUT |
| `test_guest.py` | 514 | Guest template/clone and detached-run commands. | audit, console, guest, guest_agent, inventory | KEEP |
| `test_host_setup.py` | 74 | Runs `proxmox-host-setup.sh` against fake commands; never changes the host. | (shell script only) | REWRITE (script changes) |
| `test_hostguard.py` | 273 | Host-side lease guard decisions (abandon/heartbeat/long-term endings) — pure logic of `hostguard.py`. | hostguard | CUT |
| `test_hostinfo.py` | 82 | Host hardware inspection parses real hwmon/net sysfs output. | hostinfo | KEEP |
| `test_install.py` | 179 | `bash -n` syntax of `install.sh`, `mariadb-host-setup.sh`, `minio-host-setup.sh` + install failure-message behaviour (asserts `minio-host-setup.sh` in output). | (shell scripts only) | REWRITE (drop mariadb/minio rows) |
| `test_ioworkload.py` | 127 | Portable scratch-file I/O evidence and replay. | ioworkload | CUT |
| `test_lifecycle.py` | 888 | Lease ownership, expiry, orphan reclamation against the lifecycle layer. | cleanup, cli, inventory, leases | KEEP |
| `test_longterm.py` | 438 | Long-term lease inversion (host stays on, guests survive). | cli, inventory, longterm | KEEP |
| `test_mariadb.py` | 256 | Ledger against a real MariaDB (skipped unless `PXL_TEST_MARIADB`); legacy schema creation, migration idempotence, shared-secret store. | journal, mariadb | CUT |
| `test_memflow.py` | 455 | memflow guards: opt-in, lease-bound, host-change-gated. | cli, host_transport, memflow | CUT |
| `test_netcap.py` | 147 | Capture/intercept guards over the shared SSH channel. | cli, host_transport, netcap | CUT |
| `test_netgw.py` | 222 | Optional DHCP/TFTP gateway spawners. | audit, console, guest_agent, netgw | CUT |
| `test_oci.py` | 266 | OCI-to-LXC provisioning guards. | audit, oci | CUT |
| `test_omp_skill.py` | 34 | `SKILL.md` structure sanity (parses frontmatter/sections via tomllib). | (SKILL.md only) | KEEP/REWRITE (deliverable IS SKILL.md) |
| `test_onboarding.py` | 441 | Installer generation, host-setup payload privacy, pairing, mode gating. | cli, config, host_policy, onboarding, onboarding_host | CUT |
| `test_pe.py` | 602 | pe command module with fully faked external tools (`shutil.which` + recorded `subprocess.run`). | cli, pe, recipes | CUT |
| `test_proxmox_lab.py` | 1235 | Core suite: CLI wiring, guest/storage paths, RFB, ws, cleanup (the historical catch-all). | cleanup, cli, guest, rfb, storage, ws | REWRITE |
| `test_public.py` | 53 | Runs `scripts/check-public.py` against fixture trees. | (script only) | KEEP |
| `test_release.py` | 75 | Runs `scripts/check-release.py` (version/tag/changelog/bootstrap `REQUIRED_VERSION` agreement). | (script only) | KEEP |
| `test_share.py` | 428 | Share server access control and framing (only internet-facing component). | config, share, share_server | CUT |
| `test_storage_guards.py` | 122 | Upload TLS, slow-storage, default-storage guards. | cli (+ storage indirectly) | REWRITE |
| `test_usb.py` | 117 | USB sniff/passthrough gates over the SSH channel. | cli, host_transport, usb | CUT |
| `test_virtio.py` | 300 | Virtio driver-porting diagnostics (fake `_Lab`). | virtio | CUT |
| `test_vision.py` | 483 | Vision provider routes, bounded payloads, key handling. | vision (+ urllib.error) | CUT |
| `test_windows.py` | 346 | Unattended Windows install: auto-tap helper, autounattend generation. | cli, config, console, windows | CUT |
| `test_windows_host.py` | 181 | Running the **controller** on Windows (no `fcntl`/`os.uname`) — state lock, config, secrets. | cli, config, secrets_store, state | KEEP |
| `test_ws.py` | 187 | Wire-level regressions for resumable console reads. | ws | KEEP |
| `support/__init__.py` | 5 | Package marker for shared bootstrap. | — | KEEP |
| `support/bootstrap.py` | 29 | Applies `tests/fixtures/config.toml` + a per-process state dir **before any package import**. | config (fixture) | KEEP |
| `fixtures/config.toml` | — | The only deterministic non-site test values; must keep loading after any config change. | config | KEEP |

**Src modules with no test referencing them directly:** `__main__`, `api`, `diagnostics`,
`errors`, `serial`, `updates`, `resources/pxl-hostguard.py` (all covered indirectly
except `updates`, which has no coverage at all).

---

## 3. Documentation inventory — `docs/` + top-level Markdown

"Hunch" is the disposition under these cuts: MariaDB ledger → SQLite, control over SSH,
S3/onboarding/secrets-backends removed, heavy optional stacks cut
(windows/android/memflow/netcap/usb/share/netgw/virtio/disk/io/isoinspect/oci/crash/vision),
deliverable = MCP server + SKILL.md.

### 3a. `docs/` pages

| Page | ~Lines | What it documents | Cut impact | Hunch |
|---|---|---|---|---|
| `docs/README.md` | 82 | Documentation index; groups pages into install / first guest / operate-a-feature / troubleshoot / advanced / contribute. | Every entry for a cut stack goes; navigation rebuilt around MCP + SKILL. | REWRITE |
| `docs/AGENTS.md` | 283 | Operational guide for agents that know the lease shape: channel ladder (agent → serial → console), screen-reading ladder, traps. | Channel guidance survives; per-stack sections (memflow/usb/netcap/windows/android/netgw/vision/S3) cut; API-based probes become SSH-based. | REWRITE |
| `docs/AUDIT-2026-08-24.md` | 63 | Historical security review findings against `src/` + `scripts/`. | Historical record; findings about removed code become stale but harmless. | KEEP (archive) |
| `docs/CONFIGURATION.md` | 456 | Every TOML setting, the secrets backends, config lookup order. | `[audit]` MariaDB block, `[secrets]` backend block, `[memflow]` ssh/stack block, S3 block, netgw/netcap/android/windows sections all change; SSH transport becomes core config. | REWRITE |
| `docs/INSTALL.md` | 411 | Blank PC → working lab: Proxmox install, WOL, API token creation, first `doctor`, watchdog, host-setup one-liners. | API-token setup replaced by SSH access; MariaDB/MinIO one-liners die; watchdog tied to old lease model. | REWRITE |
| `docs/RECIPES.md` | 212 | Copy-paste agent recipes (browse, build, test, VPN, Android, Windows…). | Recipes for cut stacks deleted; lease shape itself may move under MCP. | REWRITE |
| `docs/VERIFICATION.md` | 297 | What has been verified on real hardware vs unit-tested only. | Entirely release-specific; must be re-baselined after rework. | REWRITE |
| `docs/android.md` | 148 | Emulated Android guests as lab guests. | android cut. | CUT |
| `docs/architecture.md` | 142 | Module map, dependency direction, `register(sub, lab)` contract, compatibility-alias policy. | The module map itself is the thing being replaced; contract changes from CLI subcommands to MCP tools. | REWRITE |
| `docs/commands.md` | 327 | Generated full command reference (from `scripts/gen-commands.py`). | Generated from the parser; regenerated or replaced by MCP tool list. | REWRITE (regenerate) |
| `docs/connections.md` | 68 | Pasteable setup block for a second dev machine; Tailscale offer. | Depends on shared-secrets store (MariaDB `secrets` table) — that handoff mechanism disappears. | REWRITE/CUT |
| `docs/console.md` | 344 | Screenshots, text, keys, clicks, serial, cloud vision. | S3/vision paths cut; SSH `screendump` fallback survives; core reading flow stays. | REWRITE |
| `docs/crash-reports.md` | 62 | Offline build-pinned symbolization with local llvm-symbolizer. | crash cut. | CUT |
| `docs/disk.md` | 119 | Offline disk repair/inspection, El Torito, libguestfs mount. | disk/isoinspect cut. | CUT |
| `docs/gui-installers.md` | 135 | Bounded loop for driving an OS installer over the console (model identifies screen, tool owns safety). | Generic console-driving loop (survives if console survives), but its examples are the cut installers (Windows/macOS). | REWRITE |
| `docs/host-info.md` | 19 | `proxmox-lab host` — hwmon temps, MACs over the opt-in SSH channel. | SSH channel survives (hostinfo not in cut list). | KEEP |
| `docs/io-workloads.md` | 88 | Record/replay bounded scratch-file I/O traces. | io cut. | CUT |
| `docs/long-term-leases.md` | 183 | Long-term leases: host stays up, protect flag, backups, `lease-destroy`/`lease-release`. | Core survives; its interplay with the MariaDB ledger + hostguard watchdog must be re-explained (SQLite + no guard). | REWRITE |
| `docs/macos.md` | 124 | Hosting macOS VMs via OSX-PROXMOX on the same host. | Not in the cut list; depends on API/SSH control + console driving. | REWRITE |
| `docs/memflow.md` | 244 | Agentless guest memory introspection. | memflow cut. | CUT |
| `docs/netcap.md` | 135 | Capture, SSL inspection, MITM relay. | netcap cut. | CUT |
| `docs/network.md` | 232 | Forced-VPN egress, gateway build, leak testing. | netgw cut. | CUT |
| `docs/oci.md` | 90 | OCI image → unprivileged LXC. | oci cut. | CUT |
| `docs/onboarding.md` | 269 | Experimental installer generation + host pairing (ISO/VPS). | onboarding cut. | CUT |
| `docs/pe.md` | 146 | Windows PE ISO inspect/extract/rebuild/boot. | pe depends on cut `isoinspect`/upload path (and `windows.cmd_upload`). | CUT |
| `docs/reactos.md` | 341 | Debugging ReactOS over serial + KDB with pinned release facts. | ReactOS path is console/serial + recipes — survives if console/serial/recipes survive. | REWRITE |
| `docs/safety-policy.md` | 178 | Lease/ownership/shutdown invariants the code enforces; authorization flags. | Invariants partially re-expressed: audit storage, host-change gates (API → SSH), shutdown verification mechanism. | REWRITE |
| `docs/share.md` | 145 | Disposable console links via share worker VM + tunnel. | share cut. | CUT |
| `docs/site-notes.example.md` | 53 | Template for describing your own lab site (copied to `docs/site-notes.md`). | Site-agnostic. | KEEP |
| `docs/storage.md` | 268 | Physical disks, node storage classes, cloud images, S3 transfer (`push`/`pull`). | S3 transfer section cut; disk section cut; storage management survives but moves to SSH. | REWRITE |
| `docs/troubleshooting.md` | 88 | Symptom → diagnostic command → next decision. | Commands referenced by cut stacks removed; `doctor` surface changes. | REWRITE |
| `docs/usb.md` | 90 | USB passthrough + usbmon sniffing. | usb cut. | CUT |
| `docs/virtio-queues.md` | 52 | Virtqueue sampling through the QEMU monitor. | virtio cut. | CUT |
| `docs/windows.md` | 153 | Windows Server install from retained template. | windows cut. | CUT |
| `docs/images/vnc-capture.png` | — | Screenshot used by console/agent docs. | Depends which docs survive. | KEEP if referenced |

### 3b. Top-level Markdown

| Page | ~Lines | What it documents | Cut impact | Hunch |
|---|---|---|---|---|
| `README.md` | 176 | Project overview: turn a spare PC into an AI lab; feature tour; install pointers. | Feature tour describes most cut stacks. | REWRITE |
| `SKILL.md` | 442 | Agent skill entrypoint: bootstrap, canonical lease shape, per-subsystem quick-ref (long-term, OCI, console, share, storage, VPN, Android, Windows, ReactOS, memflow, USB, netcap, audit, boundaries). | **This is the future deliverable** — every cut-stack section removed, MCP tool invocation becomes the spine. | REWRITE (core artifact) |
| `CHANGELOG.md` | 1142 | Release history (line 645 even records the pre-MariaDB `[audit] backend = "jsonl"` era). | Append-only history; keep. | KEEP |
| `CLEANUP_PLAN.md` | 173 | The 2026-09-08 cleanup plan (P1/P2/P3 actions, proposed code/doc layout, baseline 0.14.1). | Superseded wholesale by this rework. | DELETE (absorb into rework-plan) |
| `CONTRIBUTING.md` | 77 | Dev setup, required checks, release checklist. | Checks/commands/module layout change; PyMySQL-specific guidance goes. | REWRITE |
| `RESPONSIBLE_USE.md` | 26 | Legitimate-use statement. | Unaffected. | KEEP |
| `SECURITY.md` | 41 | Security policy; threat model (controls hypervisors/guests/networks/storage). | Scope statement still true under SSH control; wording may need review. | KEEP/TRIM |
| `SUPPORT.md` | 21 | Where to get help (docs first). | Links survive. | KEEP |
| `AGENTS.md` | 134 | Repo rules: architecture/data flow, module map, dev commands, conventions, important files. | Describes the exact architecture being replaced (MariaDB ledger, API-token boundary, register/`_bind` contract, check scripts). | REWRITE |

---

## 4. Script / installer inventory

| Path | ~Lines | Purpose | Likely disposition | Hunch |
|---|---|---|---|---|
| `scripts/check` | 30 | Single entry point running CI's checks in CI order (`--fast` skips tests). | Still useful; re-point at new checks. | REWRITE |
| `scripts/check-docs.py` | 156 | Offline doc checks: relative links resolve, every `proxmox-lab` subcommand mentioned exists in the parser. | Survives only while docs keep a generated command list; MCP tool names change the vocabulary. | REWRITE |
| `scripts/check-public.py` | 123 | Fails when tracked files contain local-site material. | Repo-safety guard, unrelated to cuts. | KEEP |
| `scripts/check-release.py` | 85 | Validates release metadata (version ↔ tag ↔ changelog ↔ `bootstrap.sh` `REQUIRED_VERSION`). | Survives. | KEEP |
| `scripts/check-secrets.py` | 75 | Fails when candidate files contain secret material. | Repo-safety guard; allowlist may shrink with removed config examples. | KEEP |
| `scripts/gen-commands.py` | 98 | Regenerates `docs/commands.md` from the real parser (`--check` in CI). | Input (CLI parser) is being replaced by MCP tools. | REWRITE |
| `scripts/install-watchdog` | 37 | Installs a macOS launchd timer running `scripts/proxmox-lab cleanup-expired` every 300 s. | Depends on `cleanup-expired` + lease model surviving. | KEEP/REWRITE |
| `scripts/proxmox-lab` | 19 | Run from a checkout without installing; pins a Python 3.11+ interpreter deliberately. | Survives. | KEEP |
| `bootstrap.sh` | 156 | Agent bootstrap: prints a working `proxmox-lab` path, builds a throwaway venv if uninstalled; carries `REQUIRED_VERSION` cross-checked by release script. | Still the "agent with only SKILL.md" entry — central to the MCP+SKILL deliverable. | KEEP/REWRITE |
| `install.sh` | 322 | One-touch installer: prompts for API token/host/WOL, **and S3 backend (`PXL_S3_*`)**, writes config + private secrets file, runs health check; references `minio-host-setup.sh` in failure output. | API-token and S3 paths die; SSH-credential equivalent needed. | REWRITE |
| `proxmox-host-setup.sh` | 226 | On the PVE host: creates a restricted API user + token, persistent WOL, optional Tailscale, prints config block. | SSH control needs a different host prep (authorized key, python3, user) instead of an API token. | REWRITE |
| `mariadb-host-setup.sh` | 107 | Curl-able standalone copy of `resources/ledger-host-setup.sh`: MariaDB LXC + DNAT + guard install. | MariaDB removed. | CUT |
| `minio-host-setup.sh` | 201 | Curl-only MinIO LXC for the S3 scratch bucket (no src references — invoked by humans/`install.sh` docs only; syntax-checked by `test_install.py`). | S3 removed. | CUT |
| `.github/workflows/ci.yml` | 81 | Tests on Python 3.11–3.14; installs **xorriso** for ISO tests; runs all check scripts; `bash -n` over `bootstrap.sh install.sh proxmox-host-setup.sh scripts/* resources/*.sh`; wheel smoke asserts **`import pymysql, cryptography`**. | xorriso + shell list + pymysql/cryptography assert all track cut code. | REWRITE |
| `.github/workflows/release.yml` | 83 | Tag-triggered release: tests, guards, `check-release.py`, build, wheel smoke (again asserts `import pymysql, cryptography`), SHA256SUMS, GitHub release. | Same pymysql/cryptography smoke must go for stdlib-only. | REWRITE |
| `.githooks/pre-commit` | 5 | Runs `check-secrets.py` + `check-public.py`. | Unrelated to cuts. | KEEP |

---

## 5. Dependency-sanity: SQLite in the codebase today

The plan says "move **back** to SQLite". Current state:

- **Only one `import sqlite3` in the package**: `src/proxmox_agent_lab/journal.py:25`.
  It is used **exclusively** to *read the legacy pre-MariaDB ledger*:
  - Legacy file: `LEGACY_SQLITE = "journal.db"` → `journal_dir / "journal.db"` (`journal.py:33`, `:50-51`).
  - Reader (`journal.py:180-198`):

    ```python
    def _legacy_sqlite_records(path: Path) -> list[dict[str, Any]]:
        ...
        connection = sqlite3.connect(path)
        rows = connection.execute(
            "SELECT data FROM events ORDER BY id ASC"
        ).fetchall()
    ...
        for (blob,) in rows:
            out.append(json.loads(blob))
    ```

  - Sibling legacy source: `*.jsonl` day-files in the journal dir (excluding the live
    `spool.jsonl`), marker file `.migrated-to-mariadb`.
- **The legacy SQLite schema is never created by current code**, but two tests create it
  verbatim (identical in `tests/test_mariadb.py:173-175` and
  `tests/test_abstractions.py:359-361`) — this is the old on-disk schema:

  ```sql
  CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, event TEXT, lease TEXT, vmid INTEGER, data TEXT)
  ```

  Rows were inserted as `INSERT INTO events (timestamp, event, data) VALUES (?, ?, ?)`
  with `data` a JSON blob of the whole redacted record. The current reader only consumes
  `data`; `id`/`timestamp`/`event`/`lease`/`vmid` are legacy index columns.
- **Historical note**: `CHANGELOG.md:645` records that `[audit] backend = "jsonl"` once
  opened `journal.db` too — i.e. the SQLite file predates the JSONL spool.
- **`state.py` has no SQLite** — it is pure atomic-JSON persistence + controller lock.
  `audit.py` has no SQLite either; it triggers the migration (`auto_migrate_once`,
  invoked from the audit write path `audit.py:139`, guarded by `_AUTO_MIGRATED`) which
  reads the legacy SQLite and writes to MariaDB (`journal.migrate_legacy` → `mariadb.append_many`, idempotent by
  content-hash `event_id`).
- **MariaDB schema to replace** (`mariadb.py:45-104`, verbatim structure):

  ```sql
  CREATE TABLE IF NOT EXISTS events (
      id         BIGINT       NOT NULL AUTO_INCREMENT,
      event_id   CHAR(36)     NOT NULL,
      controller VARCHAR(190) NOT NULL,
      timestamp  VARCHAR(32)  NOT NULL,
      event      VARCHAR(200) NOT NULL,
      lease      VARCHAR(190)     NULL,
      vmid       INT              NULL,
      data       LONGTEXT     NOT NULL,
      PRIMARY KEY (id),
      UNIQUE KEY events_event_id (event_id),
      KEY events_timestamp (timestamp), KEY events_lease (lease),
      KEY events_event (event), KEY events_controller (controller)
  ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  ```

  plus `migrations (controller UNIQUE, migrated_at, source_events, uploaded,
  already_present, detail)` and `secrets (name PK, value, updated_at, updated_by)`.
- **Consequence for the rework**: a single local SQLite DB can absorb `events` (a
  renamed `journal.db`), and the `secrets` table disappears with the shared-store
  feature; the two legacy-migration paths (SQLite reader + JSONL reader) become either
  dead or the actual import path for old `journal.db` files.

---

## 6. Third-party dependency audit (target: stdlib-only)

**Every non-stdlib import in `src/`** (AST scan over all 57 files):

| Import | Where | Guarded? | What breaks if removed |
|---|---|---|---|
| `pymysql` (+ `from pymysql.cursors import DictCursor`) | `mariadb.py:25-33` (try/except `ModuleNotFoundError` → `_IMPORT_ERROR`, module still imports) | yes | Entire MariaDB ledger: append/query/spool-flush, migration lock, shared-secret store, `journal host-setup` status. With it missing, `journal.record` falls back to spooling locally and `mariadb.Settings` raises `MariaDBError` on use — i.e. the tool degrades but imports (deliberate broken-install rule). |
| `pymysql` | `resources/pxl-hostguard.py` (unguarded; host-installed script) | no | The host-side watchdog — but the script only runs on the host, installed with `python3-pymysql` by `ledger-host-setup.sh`. |

**Not imported anywhere in `src/` but declared**: `cryptography>=41` in
`pyproject.toml [project] dependencies` — exists solely so PyMySQL's sha256 auth plugins
work; no direct import in package code. **CI and release workflows both assert
`python -c 'import pymysql, cryptography'` in the wheel smoke test** and `pyproject.toml`
comments document both — three places to change for stdlib-only.

**Everything else is stdlib.** Notable stdlib modules doing "third-party-like" work:

- `urllib.request`/`urllib.parse` — Proxmox API (`api.py`), Home Assistant (`power.py`),
  GitHub check (`updates.py`), vision providers (`vision.py`), onboarding TLS fetch,
  SigV4 presigned transfers (`s3.py`), S3 upload (`transfer.py`).
- `ssl`/`socket` — API TLS, `ws.py` websockets, onboarding listeners, `share_server`.
- `subprocess` + `ssh` — the entire host channel (`host_transport.py`), host-setup
  scripts, external tools (pe, crash/llvm-symbolizer, windows helpers).
- `http.server` — `share_server.py` (public relay) and onboarding receivers.
- `zlib`/`struct` — PNG codec (`png.py`) and RFB Zlib decoding (`rfb.py`).
- `hashlib`/`hmac` — SigV4, content-hash `event_id`, pairing secrets.
- `sqlite3` — legacy ledger read only (§5).
- `tomllib` — config (Python ≥3.11, which is `requires-python`).

**Tests**: no third-party imports at all (only in-repo `tests.support`; suite is
`unittest`). **Scripts**: no third-party imports. **Optional dev extra**: `pytest>=7`
(never used by the canonical suite). **CI installs OS packages**: `xorriso` (ISO tests —
dies with isoinspect/disk/pe test data? it is currently installed for the bootstruct/iso
suite) and `build` for packaging.

---

## 7. Surprising coupling (facts worth the design session's attention)

1. **The shared-secret store lives inside the audit ledger.**
   `secrets_store._read_shared` (secrets_store.py:147-175) resolves the bootstrap secret
   `mariadb-password` locally, then lazily imports `journal` + `mariadb` to read the
   `secrets` table (`mariadb.get_secret`). So "cut MariaDB" silently also removes the
   cross-controller secret distribution that `connection.py` ("add a machine in one
   line"), `diagnostics._seed_shared_secrets`, and onboarding all lean on — even though
   "secrets backends" and "audit ledger" are being cut as *separate* items.
2. **The host watchdog consumes the ledger with its own pymysql.**
   `resources/pxl-hostguard.py` (installed as root by the *same* `journal host-setup`
   stream that provisions MariaDB) reads leases directly from the DB to power the host
   off. Cutting the ledger removes the only off-controller enforcement of
   "host powers itself off" — a stated product invariant (README/AGENTS).
3. **`host_transport` is invisible to a naive import-graph scan.**
   Eight modules (`console`, `diagnostics`, `disk`, `diskactivity`, `hostinfo`,
   `memflow`, `netcap`, `usb`) import it *inside function bodies*
   (`from . import host_transport`), and everything is gated on the single config key
   `[memflow] ssh_host`. The SSH channel is already the hub for host-side work — and the
   config key that enables it is named after a *cut* feature.
4. **A legacy-SQLite read runs automatically on normal operation.**
   The audit write path (`audit.py:139`, i.e. the first recorded event of any command)
   calls `auto_migrate_once`, so the old `journal.db` schema (§5) is live code today,
   not just a utility. `diagnostics.py:505-515` exposes it manually as
   `journal --migrate` / `--migrations`; `cli.py:706-707` defines a
   `_auto_migrate_once` wrapper with **zero call sites** (dead code).
   The new single-SQLite design must decide whether `journal.db` is *the* new DB or a
   one-time import.
5. **`api.py` funnels every HTTPS call through one seam** — including a policy gate:
   `host_policy.check_api` inspects API paths/methods inside the client for `lxc-only`
   VPS mode (QEMU ops and host power ops blocked). An SSH transport must re-express this
   "capability policy" somewhere other than API-path parsing.
6. **`console.py` re-exports `serial`/`guest_agent`/`transfer` names**, so the biggest
   test suite (`test_console.py`, 2837 lines) exercises the S3 transfer and Windows
   helpers indirectly; cutting `transfer`/`s3`/`windows` ripples into console tests even
   where console itself survives. Similarly `pe.py` and `windows.py` reach uploads
   through `lab.cmd_upload` (S3), and `diskactivity`/`disk` reach the host through the
   same SSH transport as memflow/netcap/usb.
7. **Two components face off the controller entirely**: `share_server.py` (internet-facing
   relay, runs on a worker VM) and `onboarding.py`/`onboarding_host.py` (TLS enrollment
   listener + first-boot payload) — plus three *outbound* HTTP integrations unrelated to
   Proxmox: Home Assistant (`power.py`), GitHub (`updates.py`), vision providers
   (`vision.py`). None of these are in the touchpoint list the rework starts from.
8. **CI/release smoke tests hard-code the runtime deps**: both workflows end their wheel
   smoke with `python -c 'import pymysql, cryptography'` — the stdlib-only target fails
   CI at exactly that line until `pyproject.toml` deps and both workflows change.
