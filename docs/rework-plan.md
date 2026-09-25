# Rework plan — `proxmox-agent-lab` becomes an SSH + SQLite MCP skill

Branch: `rework-mcp-skill`. Companion evidence: [rework-inventory.md](rework-inventory.md)
(file-level facts, dependency audit) and [AUDIT-2026-08-24.md](AUDIT-2026-08-24.md)
(historical). This document is the implementation plan; sections A–I are self-contained
enough to hand to workers.

**What changes, in one paragraph.** The controller stops being a hosted service stack
(MariaDB ledger, HTTPS API client with tokens, S3 transfer scratch, secret backends,
installers, onboarding, a dozen research subsystems) and becomes a small stdlib-only
Python package that talks to one Proxmox host over SSH as root, keeps all of its state in
one SQLite file, and serves an MCP tools surface over stdio. Setup is `ssh-copy-id
root@proxmox` plus one config file. The only thing ever installed on the Proxmox host is
one optional crontab line running a bundled garbage collector. There is **no migration**:
fresh start (see §C — the legacy `events` DDL is reused verbatim so old journal files stay
structurally readable, but nothing is imported).

## A. Target architecture

### A.1 The shape

```text
                 ┌─────────────────────────────┐
   MCP client ──▶│ mcp.py   stdio JSON-RPC 2.0 │
                 └──────────────┬──────────────┘
   terminal ────▶┌──────────────▼──────────────┐
                 │ cli.py   argparse, policy   │
                 │          gates, lab facade  │
                 └──────────────┬──────────────┘
        ┌───────────┬───────────┼───────────┬─────────────┬──────────┐
        ▼           ▼           ▼           ▼             ▼          ▼
   leases.py    guest.py   transfer.py  console.py   cleanup.py   gc.py
   (lifecycle,  (create/    (push/pull)  (screenshot  (finalize,   (cron
    long-term)   clone/…               /type/keys)   sweep)        install)
        │           │           │           │             │          │
        └───────────┴─────────┬─┴───────────┴──────┬──────┘          │
                              ▼                    ▼                 ▼
                        proxmox.py            power.py      resources/pxl-gc.py
                        (qm/pct/pvesh         (WoL packet,  (standalone script,
                         wrappers, task        verified       runs on the host,
                         wait)                 shutdown)      no imports of ours)
                              │
                              ▼
                           ssh.py ──▶ `ssh root@<target> …`  (command allowlist)
                              │
   side services: store.py (SQLite), audit.py (redact + events), journal.py (query),
                  config.py (TOML), diagnostics.py (init/doctor), errors.py
```

Dependency direction is strictly downward: command layers (`cli`, `mcp`, feature
modules) call services; `proxmox.py` calls `ssh.py`; `ssh.py` and `store.py` call nothing
above them. `resources/pxl-gc.py` imports **nothing** from the package — it is a
self-contained script copied to the host. No cycles; no second dispatch architecture —
the existing `register(sub, lab)` + `_bind` callback pattern from
[architecture.md](architecture.md) is preserved (it is load-bearing for tests).

### A.2 Module disposition (summary — full matrix in §B)

| Module (new) | Provenance | Replaces / absorbs |
|---|---|---|
| `cli.py` | REWRITE | parser + policy gates stay; ~2.3k lines of facade wrappers go away |
| `config.py` | REWRITE | same TOML loader, far fewer keys (§G); `[memflow] ssh_host` → `[ssh] target` |
| `errors.py` | KEEP | `LabError`/`ConfigError` unchanged |
| `ssh.py` | NEW | `api.py`, `host_transport.py`, `host_policy.py` — one subprocess-ssh transport with a **command allowlist** (the new least-privilege boundary that replaces the scoped API token) |
| `proxmox.py` | NEW | `api.py` request/task layer, `guest_agent.py` primitives — `qm`/`pct`/`pvesh` wrappers |
| `store.py` | NEW | `state.py` JSON files, `mariadb.py`, journal spool — one SQLite file (§C) |
| `leases.py` | REWRITE | `leases.py` + `longterm.py`; keeps the MCP idle-activity clock |
| `cleanup.py` | REWRITE | same orchestration, now through `qm`/`pct` |
| `power.py` | REWRITE | WoL magic packet (stdlib UDP) + verified shutdown; Home-Assistant HTTP path cut |
| `guest.py` | REWRITE | guest lifecycle + run/probe/list |
| `transfer.py` | REWRITE | push/pull over ssh/`qm guest`; S3/MinIO path cut |
| `console.py` | REWRITE | screenshot/type/key via `qm monitor` screendump + `qm sendkey`; VNC/websocket/vision cut |
| `png.py` | KEEP | plus PPM→PNG (QM `screendump` writes PPM) |
| `audit.py`, `journal.py` | REWRITE | events land in SQLite (legacy `events` DDL); `journal` reads it |
| `diagnostics.py` | REWRITE | `init`/`doctor` only |
| `mcp.py` | NEW | stdio JSON-RPC 2.0 MCP server (§E); absorbs `record_mcp_activity`/idle shutdown |
| `gc.py` + `resources/pxl-gc.py` | NEW | `resources/pxl-hostguard.py` — the host-side enforcer, rebuilt stateless (§F) |

Everything else in `src/` is CUT (§B) — 40-odd modules: MariaDB, secrets, HTTPS API,
onboarding, Windows/PE/bootstruct, Android, memflow, netcap, USB, share, netgw, virtio,
disk/ioworkload, isoinspect, recipes, oci, crash, vision, rfb/ws/des/textmode, serial,
updates, hostguard, storage, inventory, connection, hostinfo.

### A.3 Where the four moving parts live

- **SQLite** — `store.py`; the file is `<state dir>/lab.db` (§G). One connection per
  process with WAL + `busy_timeout`; the schema is in §C. It holds leases, lease
  resources, and audit events — and nothing else. No JSON state files survive.
- **SSH transport** — `ssh.py`, the only module that spawns `ssh`. Every call is
  argv-recordable (for tests), timeout-bounded, and checked against a command allowlist
  before execution (§D). Guest file IO and exec ride `qm guest`/`pct exec` *through* this
  transport; nothing opens its own channel.
- **Lifecycle** — `leases.py` owns lease policy (begin/heartbeat/end/expiry/long-term,
  resource registration, ownership checks); `cleanup.py` owns teardown (`finalize_lease`,
  `cleanup-expired`, shared-guest cross-references). They communicate through
  `store.py`, never by importing each other's handlers (same rule as today).
- **GC script** — `resources/pxl-gc.py` is bundled in the wheel and copied to
  `/usr/local/sbin/pxl-gc` by `proxmox-lab gc install`; `gc.py` is the thin
  install/status/uninstall command. The script never sees the controller (§F).
- **MCP server** — `mcp.py`, started by `proxmox-lab mcp`, stdio JSON-RPC 2.0. Each tool
  is a thin wrapper over the same functions `cli.py` calls; the tool surface is §E.

### A.4 Safety invariants in the new world

The eight invariants of [architecture.md](architecture.md) survive, re-expressed:

1. **Every mutation belongs to a lease** — `store.resources` is the registry;
   `proxmox.py` refuses mutating calls for a `(kind, vmid)` not registered to the
   supplied lease. Guest metadata (`pxl-lease`, `pxl-expiry`) is the durable copy.
2. **Cleanup only destroys lease-owned resources; idempotent; records failures** —
   unchanged policy, `qm destroy`/`pct destroy` underneath; `cleanup_failed` retried by
   every later sweep (§C).
3. **Host power-off is verified by repeated probe failure** — `power.py` probes ssh +
   TCP :22 repeatedly after `shutdown -h now`; a timeout reports `host_powered_off=false`
   and is never silently rounded up (§D).
4. **Destructive/host-change ops need an explicit flag** — `--host-change-authorized`,
   `--confirm`, `--standalone-authorized` gates stay (§E/§F).
5. **Audit redacts and never fails the action** — `audit.py` redacts before insert into
   `events.data`; an unrecordable event is reported, never raises into the action.
6. **Imports survive missing config** — `diagnostics.py`/`init`/`doctor` must work on a
   broken install.
7. **New: least privilege is a command allowlist.** The scoped API token is gone;
   `ssh.py` refuses any remote command outside the `qm`/`pct`/`pvesh`/host-tooling
   allowlist, and the host-changing subset (`shutdown`, `crontab`, `install`) additionally
   requires the authorization flags. Root ssh is the trust boundary; the allowlist is the
   policy inside it.
8. **The host still powers itself off.** Today `resources/pxl-hostguard.py` (systemd
   timer over MariaDB) is the only off-controller enforcement. The GC cron replaces it
   and carries both duties: destroy expired-lease guests *and* power off when the host is
   clear (§F) — plus the controller-side MCP idle shutdown (§E) as the second net.

### A.5 Deliverable form

- Installable package `proxmox-agent-lab` (hatchling, **zero runtime dependencies**,
  Python ≥ 3.11) with console script `proxmox-lab`.
- `proxmox-lab mcp` serves the MCP tools surface (§E) over stdio; the same functions back
  the slim CLI (pinned below).
- Skill content: root `SKILL.md` (operator quick-ref) is rewritten for the ssh-copy-id
  setup and the slim command set. `.agents/skills/proxmox-agent-lab/SKILL.md` is a
  **symlink to root `SKILL.md`** (verified: `lrwxr-xr-x SKILL.md -> ../../../SKILL.md`),
  so one rewrite serves both locations — keep the symlink as the single content source
  (the wheel already force-includes `.agents/`).
- CLI surface (pinned; MCP tools mirror it in §E): `init`, `doctor`, `journal`, `mcp`,
  `gc install|status|uninstall`, `lease-begin|heartbeat|end|list|destroy|register`,
  `guest create|clone|start|stop|destroy|probe|list|run`, `push`, `pull`,
  `console screenshot|type|keys`, `cleanup-expired`, `power wake|status|shutdown`
  (standalone `power wake`/`power shutdown` gated `--standalone-authorized`).
## B. Disposition matrix

Every file in the repository (180 tracked + `docs/rework-inventory.md` + `docs/rework-plan.md`) appears exactly once below. Src dispositions follow the contract's pinned calls verbatim; where the contract names a destination for CUT logic it is noted in *Reason*. Absorbed-into-another-module rows carry the `REWRITE -> <module>` form. The four tree tables cover the current tree only; the eight files the rework adds are in table 5 (`New files`), and the thirteen new test files appear as `NEW` rows at the end of the tests table per instruction (three names recur there — the old file is CUT, its replacement is a fresh `NEW` suite).

### B.1 `src/proxmox_agent_lab/` (incl. `resources/`, `android_scripts/`)

| File | Disposition | Reason |
|---|---|---|
| src/proxmox_agent_lab/__init__.py | KEEP | Package version marker, unchanged. |
| src/proxmox_agent_lab/__main__.py | KEEP | Entry shim `-> cli.main()`, unchanged. |
| src/proxmox_agent_lab/android.py | CUT | Android emulator subsystem cut entirely. |
| src/proxmox_agent_lab/android_scripts/01-install-sdk.sh | CUT | Android SDK setup cut with android.py. |
| src/proxmox_agent_lab/android_scripts/02-create-avd.sh | CUT | AVD creation cut with android.py. |
| src/proxmox_agent_lab/android_scripts/03-launch-emulator.sh | CUT | Emulator launch cut with android.py. |
| src/proxmox_agent_lab/api.py | CUT | HTTPS/token API client dies; primitives re-expressed in ssh.py + proxmox.py. |
| src/proxmox_agent_lab/audit.py | REWRITE | Event write + secret redaction into the SQLite store. |
| src/proxmox_agent_lab/bootstruct.py | CUT | Windows boot-structure feature cut (windows/pe family). |
| src/proxmox_agent_lab/cleanup.py | REWRITE | Finalize-lease, cleanup-expired, shared-guest/ownership checks. |
| src/proxmox_agent_lab/cli.py | REWRITE | argparse + policy gates + slim `lab` facade over the surviving modules. |
| src/proxmox_agent_lab/config.py | REWRITE | New TOML schema (`[ssh] target`, `[pve]`, `[power]`, `[state]`, `[lease]`). |
| src/proxmox_agent_lab/connection.py | CUT | Credential bundles die with shared secrets; SSH agent/keys only. |
| src/proxmox_agent_lab/console.py | REWRITE | Screenshot/type/keys via `qm monitor` screendump + `qm sendkey`. |
| src/proxmox_agent_lab/crash.py | CUT | Crash-report subsystem cut entirely. |
| src/proxmox_agent_lab/des.py | CUT | VNC auth crypto for the cut ws/RFB console. |
| src/proxmox_agent_lab/diagnostics.py | REWRITE | init/doctor over the new config/store/ssh stack. |
| src/proxmox_agent_lab/disk.py | CUT | Disk-management feature cut entirely. |
| src/proxmox_agent_lab/diskactivity.py | CUT | Disk-activity monitoring cut entirely. |
| src/proxmox_agent_lab/errors.py | KEEP | LabError/ConfigError unchanged. |
| src/proxmox_agent_lab/guest.py | REWRITE | Guest create/clone/start/stop/destroy/probe/list/run via proxmox.py. |
| src/proxmox_agent_lab/guest_agent.py | CUT | Guest-agent primitives move to proxmox.py (`qm guest`/`pct exec`). |
| src/proxmox_agent_lab/host_policy.py | CUT | API-path gate dies; re-expressed as the ssh.py command allowlist. |
| src/proxmox_agent_lab/host_transport.py | CUT | Subprocess-ssh skeleton becomes ssh.py (gated on new `[ssh] target`). |
| src/proxmox_agent_lab/hostguard.py | CUT | Host enforcer replaced by GC (resources/pxl-gc.py). |
| src/proxmox_agent_lab/hostinfo.py | CUT | MAC discovery folds into `init`/diagnostics.py. |
| src/proxmox_agent_lab/inventory.py | CUT | Orphan/retained registry superseded by GC + pxl guest metadata. |
| src/proxmox_agent_lab/ioworkload.py | CUT | I/O workload feature cut entirely. |
| src/proxmox_agent_lab/isoinspect.py | CUT | ISO inspection feature cut entirely. |
| src/proxmox_agent_lab/journal.py | REWRITE | Journal query over SQLite; legacy-events reader informs the DDL reuse. |
| src/proxmox_agent_lab/leases.py | REWRITE | Lease lifecycle ordinary + long-term; keeps mcp idle-activity plumbing. |
| src/proxmox_agent_lab/longterm.py | REWRITE -> leases.py | Long-term lease kind absorbed into the unified lease lifecycle. |
| src/proxmox_agent_lab/mariadb.py | CUT | MariaDB ledger dies; SQLite store replaces it (legacy events DDL reused). |
| src/proxmox_agent_lab/memflow.py | CUT | memflow/live-memory feature cut entirely. |
| src/proxmox_agent_lab/netcap.py | CUT | Packet-capture/MITM feature cut entirely. |
| src/proxmox_agent_lab/netgw.py | CUT | Forced-VPN egress gateway feature cut entirely. |
| src/proxmox_agent_lab/oci.py | CUT | OCI image feature cut entirely. |
| src/proxmox_agent_lab/onboarding.py | CUT | Onboarding/ISO/pairing ceremony cut entirely. |
| src/proxmox_agent_lab/onboarding_host.py | CUT | Host-side onboarding companion cut with onboarding.py. |
| src/proxmox_agent_lab/pe.py | CUT | PE-analysis feature cut (windows family). |
| src/proxmox_agent_lab/png.py | KEEP | PNG encoder survives; gains PPM->PNG for screendump conversion. |
| src/proxmox_agent_lab/power.py | REWRITE | WoL magic packet + shutdown with verified power-off (repeated probe failure). |
| src/proxmox_agent_lab/recipes.py | CUT | Guest recipes cut entirely. |
| src/proxmox_agent_lab/resources/README.md | CUT | Documents cut host-setup scripts (ledger/memflow/mitm/hostguard). |
| src/proxmox_agent_lab/resources/ledger-host-setup.sh | CUT | MariaDB ledger host setup dies with mariadb.py. |
| src/proxmox_agent_lab/resources/memflow-host-setup.sh | CUT | memflow host setup cut with memflow.py. |
| src/proxmox_agent_lab/resources/mitm-setup.sh | CUT | MITM/cert-pin experiment cut with netcap.py. |
| src/proxmox_agent_lab/resources/pxl-hostguard.py | CUT | Only off-controller enforcer; replaced by resources/pxl-gc.py. |
| src/proxmox_agent_lab/rfb.py | CUT | RFB/VNC client for the cut websocket console. |
| src/proxmox_agent_lab/s3.py | CUT | MinIO/S3 backup feature cut entirely. |
| src/proxmox_agent_lab/secrets_store.py | CUT | Shared secret store eliminated entirely (SSH agent/keys only). |
| src/proxmox_agent_lab/serial.py | CUT | Websocket console transport gone. |
| src/proxmox_agent_lab/share.py | CUT | Connection-sharing feature cut entirely. |
| src/proxmox_agent_lab/share_server.py | CUT | noVNC share server cut with share.py. |
| src/proxmox_agent_lab/state.py | REWRITE -> store.py | JSON state files replaced by the SQLite store (leases/resources/events). |
| src/proxmox_agent_lab/storage.py | CUT | Storage management feature cut entirely. |
| src/proxmox_agent_lab/textmode.py | CUT | Text-mode console helper for the cut ws console. |
| src/proxmox_agent_lab/transfer.py | REWRITE | push/pull via ssh + `qm guest`/`pct exec`. |
| src/proxmox_agent_lab/updates.py | CUT | Host-update checks cut entirely. |
| src/proxmox_agent_lab/usb.py | CUT | USB passthrough feature cut entirely. |
| src/proxmox_agent_lab/virtio.py | CUT | Virtio/queue feature cut entirely. |
| src/proxmox_agent_lab/vision.py | CUT | Vision-screenshot feature cut entirely. |
| src/proxmox_agent_lab/windows.py | CUT | Windows-provisioning feature cut entirely. |
| src/proxmox_agent_lab/ws.py | CUT | Websocket console transport cut entirely. |

### B.2 `tests/` (incl. `tests/support/`, `tests/fixtures/`)

| File | Disposition | Reason |
|---|---|---|
| tests/test_abstractions.py | REWRITE | Retargeted at the surviving config/errors/png/power/audit layers over store.py. |
| tests/test_android.py | CUT | Tests android.py (cut). |
| tests/test_bootstruct.py | CUT | Tests bootstruct.py (cut). |
| tests/test_connection.py | CUT | Tests connection.py (cut with shared secrets). |
| tests/test_console.py | CUT | Old ws/RFB/des/S3 console suite; replaced by NEW test_console.py. |
| tests/test_crash.py | CUT | Tests crash.py (cut). |
| tests/test_diagnostics.py | CUT | Old doctor/spool/update-check suite; replaced by NEW test_diagnostics.py. |
| tests/test_disk_iso.py | CUT | Tests disk.py/isoinspect.py (cut). |
| tests/test_diskactivity.py | CUT | Tests diskactivity.py (cut). |
| tests/test_guest.py | CUT | Old HTTPS/guest_agent guest suite; replaced by NEW test_guest.py (qm/pct over FakeSSH). |
| tests/test_host_setup.py | CUT | Tests proxmox-host-setup.sh (cut). |
| tests/test_hostguard.py | CUT | Tests hostguard/pxl-hostguard (cut; pxl-gc covered by NEW test_gc.py). |
| tests/test_hostinfo.py | CUT | Tests hostinfo.py (cut). |
| tests/test_install.py | CUT | Tests install.sh (cut). |
| tests/test_ioworkload.py | CUT | Tests ioworkload.py (cut). |
| tests/test_lifecycle.py | CUT | Lease/cleanup coverage moves to NEW test_leases.py + test_cleanup.py. |
| tests/test_longterm.py | CUT | longterm.py absorbed into leases.py; NEW test_leases.py covers the long-term kind. |
| tests/test_mariadb.py | CUT | Tests mariadb.py (cut); legacy-DDL reuse covered by NEW test_store.py. |
| tests/test_memflow.py | CUT | Tests memflow.py (cut). |
| tests/test_netcap.py | CUT | Tests netcap.py (cut). |
| tests/test_netgw.py | CUT | Tests netgw.py (cut). |
| tests/test_oci.py | CUT | Tests oci.py (cut). |
| tests/test_omp_skill.py | REWRITE | Guard for the surviving SKILL.md content. |
| tests/test_onboarding.py | CUT | Tests onboarding.py (cut). |
| tests/test_pe.py | CUT | Tests pe.py (cut). |
| tests/test_proxmox_lab.py | REWRITE | Runner/CLI smoke for the thin scripts/proxmox-lab. |
| tests/test_public.py | REWRITE | Guard for surviving check-public.py (drops bootstrap.sh/REQUIRED_VERSION coupling). |
| tests/test_release.py | REWRITE | Guard for surviving check-release.py (drops bootstrap.sh/REQUIRED_VERSION coupling). |
| tests/test_share.py | CUT | Tests share.py/share_server.py (cut). |
| tests/test_storage_guards.py | CUT | Tests storage.py (cut). |
| tests/test_usb.py | CUT | Tests usb.py (cut). |
| tests/test_virtio.py | CUT | Tests virtio.py (cut). |
| tests/test_vision.py | CUT | Tests vision.py (cut). |
| tests/test_windows.py | CUT | Tests windows.py (cut). |
| tests/test_windows_host.py | CUT | Tests windows host-side flow (cut). |
| tests/test_ws.py | CUT | Tests ws.py (cut). |
| tests/support/__init__.py | KEEP | Support package marker/docstring stays accurate. |
| tests/support/bootstrap.py | REWRITE | New fixture config loader: points the package at tests/fixtures/config.toml and a per-process temp state dir for store.py. |
| tests/fixtures/config.toml | REWRITE | Rewritten to the new schema keys (`[ssh] target`, `[pve]`, `[power]`, `[state]`, `[lease]`). |
| tests/test_ssh.py | NEW | Command-allowlist gate, transport failures/timeouts via FakeSSH. |
| tests/test_store.py | NEW | SQLite store: legacy events DDL reuse, leases/resources CRUD, CAS transitions, WAL/pragma setup. |
| tests/test_proxmox.py | NEW | qm/pct/pvesh wrappers + task-wait parsing over FakeSSH. |
| tests/test_leases.py | NEW | Lease begin/heartbeat/end/list/destroy/register incl. long-term kind + idle-activity plumbing. |
| tests/test_cleanup.py | NEW | Finalize-lease, cleanup-expired, shared-guest/ownership checks, idempotency. |
| tests/test_power.py | NEW | WoL magic-packet bytes + verified power-off (repeated probe failure). |
| tests/test_guest.py | NEW | Guest create/clone/start/stop/destroy/probe/list/run over FakeSSH. |
| tests/test_transfer.py | NEW | push/pull via ssh + `qm guest`/`pct exec`. |
| tests/test_console.py | NEW | screendump PPM->PNG + sendkey sequencing. |
| tests/test_gc.py | NEW | Real resources/pxl-gc.py driven with tests/support/fakeqm stubs. |
| tests/test_mcp.py | NEW | stdio JSON-RPC 2.0: initialize/tools-list/tools-call, 23 tools, idle shutdown sweep. |
| tests/test_journal.py | NEW | Journal query over SQLite incl. legacy-events readback. |
| tests/test_diagnostics.py | NEW | init/doctor on healthy and broken installs (import-without-config). |

### B.3 `docs/` (incl. `docs/images/`)

| File | Disposition | Reason |
|---|---|---|
| docs/AGENTS.md | REWRITE | Agent guidance re-slimmed to the surviving CLI/MCP surface. |
| docs/AUDIT-2026-08-24.md | KEEP | Historical evidence; not rewritten. |
| docs/CONFIGURATION.md | REWRITE | New slim shape: the pinned TOML schema and lookup order only. |
| docs/INSTALL.md | REWRITE | New slim shape: SSH target setup + `proxmox-lab init`, no installers/tokens. |
| docs/README.md | REWRITE | Docs index rebuilt around the slim set. |
| docs/RECIPES.md | CUT | Feature guide of recipes.py (cut). |
| docs/VERIFICATION.md | REWRITE | New slim shape: the canonical gates verbatim. |
| docs/android.md | CUT | Feature guide of android.py (cut). |
| docs/architecture.md | REWRITE | New slim shape: ssh/proxmox/store spine, SQLite schema, GC cron, metadata contract. |
| docs/commands.md | REWRITE | Regenerated by scripts/gen-commands.py from the slim parser. |
| docs/connections.md | CUT | Feature guide of connection/share (cut with shared secrets). |
| docs/console.md | CUT | ws/VNC/vision console cut; slim screendump/sendkey console lives in commands.md. |
| docs/crash-reports.md | CUT | Feature guide of crash.py (cut). |
| docs/disk.md | CUT | Feature guide of disk.py (cut). |
| docs/gui-installers.md | CUT | Installer-era guidance (installers cut). |
| docs/host-info.md | CUT | Feature guide of hostinfo.py (cut). |
| docs/images/vnc-capture.png | CUT | VNC capture illustration for the cut console. |
| docs/io-workloads.md | CUT | Feature guide of ioworkload.py (cut). |
| docs/long-term-leases.md | REWRITE | New slim shape: `pxl-expiry=0` semantics in the unified lease kind. |
| docs/macos.md | CUT | macOS-guest (OSX-PROXMOX) feature guide; memflow/guest tooling cut. |
| docs/memflow.md | CUT | Feature guide of memflow.py (cut). |
| docs/netcap.md | CUT | Feature guide of netcap.py (cut). |
| docs/network.md | CUT | Feature guide of netgw.py (forced-VPN egress, cut). |
| docs/oci.md | CUT | Feature guide of oci.py (cut). |
| docs/onboarding.md | CUT | Feature guide of onboarding.py (cut). |
| docs/pe.md | CUT | Feature guide of pe.py (cut). |
| docs/reactos.md | CUT | Feature guide of the cut windows/bootstruct family. |
| docs/safety-policy.md | REWRITE | New slim shape: the surviving safety invariants + authorization flags. |
| docs/share.md | CUT | Feature guide of share.py (cut). |
| docs/site-notes.example.md | CUT | Per-site notes template for the old stack; no consumer remains. |
| docs/storage.md | CUT | Feature guide of storage.py (cut). |
| docs/troubleshooting.md | REWRITE | New slim shape: ssh/SQLite/GC failure modes only. |
| docs/usb.md | CUT | Feature guide of usb.py (cut). |
| docs/virtio-queues.md | CUT | Feature guide of virtio.py (cut). |
| docs/windows.md | CUT | Feature guide of windows.py (cut). |
| docs/rework-inventory.md | KEEP | Historical evidence of the pre-rework tree. |
| docs/rework-plan.md | KEEP | This design document. |

### B.4 Top-level, `scripts/`, `examples/`, `assets/`, `agents/`, `.agents/`, `.githooks/`, `.github/`

| File | Disposition | Reason |
|---|---|---|
| .gitignore | REWRITE | Drops journal/secrets-store-era entries; keeps local config/site-notes ignores. |
| .agents/skills/proxmox-agent-lab/SKILL.md | REWRITE | The empty skill file is filled with the slim CLI/MCP usage. |
| .githooks/pre-commit | KEEP | Runs only check-secrets.py + check-public.py; references no cut scripts. |
| .github/ISSUE_TEMPLATE/bug_report.yml | KEEP | Issue template, unaffected. |
| .github/ISSUE_TEMPLATE/config.yml | KEEP | Issue template config, unaffected. |
| .github/ISSUE_TEMPLATE/feature_request.yml | KEEP | Issue template, unaffected. |
| .github/dependabot.yml | KEEP | Pip update config unaffected by the dep drop. |
| .github/pull_request_template.md | KEEP | PR template, unaffected. |
| .github/workflows/ci.yml | REWRITE | Drops the `import pymysql, cryptography` wheel-smoke assert (dep-drop site 2/3). |
| .github/workflows/release.yml | REWRITE | Drops the `import pymysql, cryptography` wheel-smoke assert (dep-drop site 3/3). |
| AGENTS.md | REWRITE | Root agent guidance re-slimmed to the surviving CLI/MCP surface. |
| CHANGELOG.md | REWRITE | Adds the SQLite/SSH rework release section; history retained. |
| CLEANUP_PLAN.md | CUT | Stale 2026-09-08 maintainability plan superseded by docs/rework-plan.md. |
| CONTRIBUTING.md | REWRITE | Contributor workflow now = scripts/check canonical gates only. |
| LICENSE | KEEP | MIT text unchanged. |
| README.md | REWRITE | Describes the slim SSH/SQLite tool; drops installers/API-token/feature sprawl. |
| RESPONSIBLE_USE.md | KEEP | Policy unchanged. |
| SECURITY.md | KEEP | Reporting policy unchanged. |
| SKILL.md | REWRITE | Root skill file rewritten for the slim CLI/MCP surface. |
| SUPPORT.md | KEEP | Support policy unchanged. |
| agents/openai.yaml | KEEP | Agent interface blurb; still accurate for the surviving tool. |
| assets/image-library.json | CUT | Windows eval-ISO catalog for the cut windows/onboarding flows. |
| assets/templates/debian-13-lxc.json | CUT | Per-OS provisioning recipe; slim guest create clones `[pve] template_vmid`. |
| assets/templates/kali-2026.2-cloud.json | CUT | Per-OS provisioning recipe; slim guest create clones `[pve] template_vmid`. |
| assets/templates/ubuntu-24.04-cloudinit.json | CUT | Per-OS provisioning recipe; slim guest create clones `[pve] template_vmid`. |
| bootstrap.sh | CUT | Bootstrap ceremony cut per the director's list. |
| examples/cert-pin-poc/README.md | CUT | Cert-pinning PoC cut per the director's list. |
| examples/cert-pin-poc/pincheck.c | CUT | Cert-pinning PoC cut per the director's list. |
| examples/cert-pin-poc/pinned_client.py | CUT | Cert-pinning PoC cut per the director's list. |
| install.sh | CUT | Install ceremony cut per the director's list. |
| mariadb-host-setup.sh | CUT | MariaDB ledger host dies with mariadb.py. |
| minio-host-setup.sh | CUT | MinIO/S3 backup subsystem cut. |
| proxmox-host-setup.sh | CUT | Host ceremony cut; control plane is stock Proxmox CLI over ssh. |
| pyproject.toml | REWRITE | Drops PyMySQL/cryptography (dep-drop site 1/3) and cut sdist includes; keeps hatchling + `.agents` wheel force-include. |
| scripts/check | REWRITE | Runs the new gate list; drops `bash -n` over cut scripts. |
| scripts/check-docs.py | REWRITE | Retargeted at the slim docs set + new argparse surface. |
| scripts/check-public.py | REWRITE | Drops bootstrap.sh/REQUIRED_VERSION coupling; keeps the site-material guard. |
| scripts/check-release.py | REWRITE | Drops bootstrap.sh/REQUIRED_VERSION coupling; keeps version+changelog validation. |
| scripts/check-secrets.py | KEEP | Credential-shape scanner unchanged. |
| scripts/gen-commands.py | REWRITE | Regenerates docs/commands.md from the slim parser. |
| scripts/install-watchdog | CUT | Launchd watchdog ceremony cut per the director's list. |
| scripts/proxmox-lab | REWRITE | Thin checkout runner (pinned interpreter -> cli.main()). |

### B.5 New files

| File | Disposition | Reason |
|---|---|---|
| src/proxmox_agent_lab/ssh.py | NEW | SSH transport + command allowlist (replaces api.py, host_transport.py, host_policy.py). |
| src/proxmox_agent_lab/proxmox.py | NEW | qm/pct/pvesh wrappers + task wait (replaces api.py + guest_agent.py primitives). |
| src/proxmox_agent_lab/store.py | NEW | SQLite store (replaces state.py JSON + mariadb.py + journal spool; reuses legacy events DDL). |
| src/proxmox_agent_lab/mcp.py | NEW | stdlib stdio JSON-RPC 2.0 MCP server; 23 pinned tools; keeps idle-shutdown plumbing. |
| src/proxmox_agent_lab/gc.py | NEW | `gc install|status|uninstall` — one root crontab line for the bundled GC. |
| src/proxmox_agent_lab/resources/pxl-gc.py | NEW | Standalone host-side GC script (python3, stdlib only, no package install). |
| tests/support/fakessh.py | NEW | FakeSSH runner: records argv, scripted outputs keyed by command-pattern regex, scriptable failures/timeouts. |
| tests/support/fakeqm/ | NEW | Stub `qm`/`pct` executables on PATH for running the real resources/pxl-gc.py. |

Self-check (rows per table, matches enumeration — 180 tracked files + `docs/rework-inventory.md` + `docs/rework-plan.md` in tables 1–4): src: 64, tests: 52 (39 current + 13 NEW), docs: 37 (35 tracked + 2 rework docs), top-level: 42, new: 8.
## C. SQLite schema

One file, `<state dir>/lab.db`, opened with the **stdlib `sqlite3`** module. It replaces
the JSON lease files (`state.py`), the MariaDB ledger (`mariadb.py`), and the journal
spool in one stroke. There is **no migration**: this is a fresh-start database (the old
`journal.db`/JSON state is not imported — see the reuse note below).

```sql
-- store.py opens with:
PRAGMA journal_mode = WAL;      -- readers (journal query) never block writers
PRAGMA busy_timeout = 5000;     -- ms; the only concurrency knob we need
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- rows: ('schema_version', '1'), ('last_mcp_activity', '<unix epoch>')

-- LEGACY DDL REUSED VERBATIM (old pre-MariaDB journal.db):
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT,
    event     TEXT,
    lease     TEXT,
    vmid      INTEGER,
    data      TEXT
);

CREATE TABLE IF NOT EXISTS leases (
    id           TEXT PRIMARY KEY,          -- lease id, lowercase-safe (goes in tags)
    kind         TEXT NOT NULL DEFAULT 'ordinary',   -- 'ordinary' | 'long_term'
    purpose      TEXT NOT NULL DEFAULT '',
    state        TEXT NOT NULL,             -- see transitions below
    created_at   TEXT NOT NULL,             -- ISO-8601 UTC
    expires_at   INTEGER NOT NULL,          -- unix epoch; GC + expiry source of truth
    heartbeat_at TEXT,
    ended_at     TEXT,
    last_error   TEXT
);

CREATE TABLE IF NOT EXISTS resources (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    lease_id     TEXT NOT NULL REFERENCES leases(id),
    kind         TEXT NOT NULL,             -- 'qemu' | 'lxc' | 'other'
    vmid         INTEGER,
    name         TEXT,
    policy       TEXT NOT NULL DEFAULT 'disposable',  -- 'disposable' | 'retain'
    created_at   TEXT NOT NULL,
    destroyed_at TEXT,
    UNIQUE (lease_id, kind, vmid)
);

CREATE INDEX IF NOT EXISTS idx_events_lease   ON events(lease);
CREATE INDEX IF NOT EXISTS idx_events_ts      ON events(timestamp);
CREATE INDEX IF NOT EXISTS idx_resources_vmid ON resources(kind, vmid);
```

### Why the legacy `events` DDL is reused verbatim

The old `journal.db` (pre-MariaDB) used exactly

```sql
CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, event TEXT, lease TEXT, vmid INTEGER, data TEXT)
```

(`tests/test_mariadb.py:173-175`, `tests/test_abstractions.py:359-361`; reader
`journal.py:180 _legacy_sqlite_records`). The rework brief is "move **back** to a very
basic sqlite system", so the new store creates the same table with the same column
names: the event's identity stays `event` (its name), `lease`/`vmid` stay queryable
index columns, and everything richer (actor, tool name, ok flag, target, detail) lives
inside `data` as a JSON object — **redacted before insert**. Consequences, stated so
nobody re-litigates them later:

- `journal` queries read this table; `journal.py`'s legacy reader shape is retained as
  the reading style, but **nothing imports old `journal.db` files** — fresh start.
- The MariaDB-era columns (`event_id`, `controller`) are dropped with MariaDB; `actor`
  replaces `controller` inside `data`.
- No spool: the database is local, so "ledger unreachable" cannot happen. The
  audit-never-fails-the-action invariant remains (a failed insert is reported, never
  raised into the action being audited).

### Secrets: eliminated entirely

The shared secret store lived inside the ledger (`secrets_store._read_shared` → MariaDB
`secrets` table, bootstrap secret `mariadb-password`). With MariaDB gone there is **no
secret store at all** — and none is needed: the only credential in the new shape is the
root SSH key in the operator's agent (`ssh-copy-id`). Cut as a result: `secrets_store.py`
(all backends: keychain/env/file/shared), `connection.py` (credential-bundle
export/import), `secrets list`, `diagnostics._seed_shared_secrets`, and every doc line
about token/keychain/secret backends. Nothing secret-shaped is ever written to `lab.db`,
argv, config, or the repo (config holds at most the WoL MAC, which is hardware
identifier, not a credential).

### Locking and races

- **Single writer, many readers.** SQLite in WAL mode with `busy_timeout=5000` gives us
  readers that never block the writer. The controller (CLI or MCP server) is the only
  writer; the GC script never opens the database (it is host-side and stateless — its
  actions land in the host's syslog).
- **State transitions are compare-and-swap, not read-modify-write.** Finalization is
  claimed by a guarded UPDATE:

  ```sql
  UPDATE leases SET state='ending' WHERE id=? AND state='active';
  ```

  If `rowcount == 0`, another actor (a concurrent `cleanup-expired` sweep) already
  claimed the lease — this is what makes `lease-end` and `cleanup-expired` racing
  harmless. The winner performs teardown; the loser reports "already ending".
- **Multi-table operations use `BEGIN IMMEDIATE`** (register resource + lease row, or
  finalize: mark lease + mark resources destroyed + append event) so the write lock is
  taken up front and `SQLITE_BUSY` surfaces as a retryable error, not a torn state.
- **Finalize is idempotent by construction.** A resource whose `destroyed_at` is set is
  skipped; a `qm destroy` that reports the guest already gone is success. This is what
  lets a lease left `cleanup_failed` be retried by every later sweep until it succeeds —
  the cleanup path records `last_error` and leaves the lease in `cleanup_failed` rather
  than pretending completion.
- **Failed shutdown is recorded, never assumed.** When verified shutdown cannot confirm
  power-off (`host_powered_off=false`), the event is written with `ok=0` and the
  failure text; the lease still closes but the failure stays visible in `journal` and
  `doctor` until a later sweep confirms the host is down.
- **Shared-guest protection reads, then acts.** Before destroying a guest, cleanup
  cross-references `resources` on `(kind, vmid)` across all leases whose state is not
  terminal (`ended`/`destroyed`/`abandoned`) — a guest another live lease holds is left
  alone and reported `left_to_another_lease` (safety-policy: an expired claim never
  shields a guest; a live one always does).
- **The MCP idle clock** is one row in `schema_meta` (`last_mcp_activity`), refreshed by
  every `tools/call`; the shutdown sweep is a read-then-act over `leases.state` exactly
  like the sweep above.

## D. SSH control mapping

Every remote action is `ssh.py` running one argv through `ssh -o BatchMode=yes
-o ConnectTimeout=5 root@<target> …` (the config names `<target>`). Rows marked
`[UNVERIFIED]` are believed correct from the `qm(1)`/`pct(1)` manpages but must be
confirmed against the live host's `pveversion` during implementation phase 2 — they are
isolated inside `proxmox.py`, so a flag correction is a one-line change.

| Capability | SSH-invoked command(s) | Notes |
|---|---|---|
| Host reachability probe | `ssh <target> true` | exit 0 = up; used by doctor, lease-begin, and the verified-shutdown probe loop |
| Wake-on-LAN | *(no ssh)* — stdlib UDP socket | `power.py` builds the magic packet (6×`FF` + 16×MAC from `[power].mac`) and sends it to `[power].broadcast:[power].port` with `SO_BROADCAST` |
| Host shutdown (verified) | `ssh <target> "nohup sh -c 'sleep 1; shutdown -h now' >/dev/null 2>&1 &"` | Detached so the request cannot hang on dying sshd. Then probe: ≥ 6 failures across ≥ 30 s, alternating `ssh <target> true` and TCP connect to `<host>:22`. Two consecutive successes cancel. Timeout ⇒ `host_powered_off=false`, non-zero exit, audit `ok=0`. **Never assumed.** No force-off path exists any more (the NanoKVM hook dies with `hostguard`) — a host that refuses to die is reported, loudly |
| Node status / version | `pvesh get /nodes/<node>/status --output-format json`; `pveversion` | `<node>` from `[pve] node`; doctor cross-checks `hostname -s` |
| Guest create (QEMU) | `qm create <vmid> --name <n> --net0 virtio,bridge=vmbr0 --scsi0 <storage>:<size> --tags 'pxl;lease-<id>' --description 'pxl-lease=<id> pxl-expiry=<epoch>'` | Storage/bridge defaults collapse to config flags; the tag + description pair is written in the SAME call so a crash cannot leave an untagged guest |
| Guest create (LXC) | `pct create <vmid> <ostemplate> --hostname <n> --tags 'pxl;lease-<id>' --description 'pxl-lease=<id> pxl-expiry=<epoch>'` | `pct create --tags` is `[UNVERIFIED]`: both branches implemented and unit-tested — pass tags/description on create, and fall back to create-then-`pct set <vmid> --tags … --description …`; live confirmation deferred to the VERIFICATION honesty list |
| Clone | `qm clone <template> <vmid> --name <n>`; `pct clone <template> <vmid>` | clone source must be a registered/retained template; metadata is re-stamped with `qm set`/`pct set` after clone (clone does not copy our per-lease expiry) |
| Metadata refresh (heartbeat) | `qm set <vmid> --description 'pxl-lease=<id> pxl-expiry=<epoch>'` / `pct set …` | every heartbeat rewrites expiry on every registered guest (§F requirement) |
| Start / stop / graceful shutdown / destroy | `qm start <vmid>`; `qm shutdown <vmid> --timeout 120`; `qm stop <vmid>`; `qm destroy <vmid> --purge 1`; `pct start\|shutdown\|stop\|destroy <vmid>` | teardown order: `shutdown` → wait stopped (120 s) → `stop` → `destroy`; `pct shutdown --timeout` is `[UNVERIFIED]`: both branches unit-tested — the timeout variant, falling back to plain `pct shutdown` + `pct status` polling + `pct stop`; live confirmation deferred to the VERIFICATION honesty list; destroy only for pxl-tagged, lease-registered guests |
| Status / probe | `qm status <vmid>`; `pct status <vmid>`; `qm guest ping <vmid>`; `qm guest network-get-interfaces <vmid>` | `guest ping` succeeding ⇒ agent channel usable (real exit codes); interface output gives the guest IP for the ssh fallback |
| Guest run (LXC) | `pct exec <vmid> -- <cmd> <args…>` | blocking, real exit code, stdout/stderr captured |
| Guest run (QEMU) | `qm guest exec <vmid> -- <cmd> <args…>` then `qm guest exec-status <vmid> <pid>` polling | bounded deadline (default 120 s, override per call); `out-data`/`err-data` are base64; `--synchronous` is `[UNVERIFIED]`: both branches unit-tested — try `qm guest exec --synchronous --timeout N …`, fall back to async exec + `exec-status` polling when the CLI rejects the flag; live confirmation deferred to the VERIFICATION honesty list. Fallback channel: direct `ssh <guest>` when the agent is absent (probe reports which channel exists before anything runs) |
| Push (file to guest) | chunked base64 via guest exec ONLY: `qm guest exec <vmid> -- sh -c 'base64 -d >> <dest>'` / `pct exec <vmid> -- sh -c 'base64 -d >> <dest>'`, each chunk fed on stdin | PRIMARY AND ONLY transfer path (director decision): `qm guest file-write`/`file-read`/`file-pull` and `pct push` are NOT used — nothing unverified is depended on. 64 KiB payload per exec; first chunk truncates (`>`), later chunks append (`>>`); sha256 recomputed in the guest and compared |
| Pull (file from guest) | chunked base64 via guest exec ONLY: `qm guest exec <vmid> -- base64 <src>` / `pct exec <vmid> -- base64 <src>` | chunks reassembled and decoded controller-side; sha256 compared; same framing rules as push |
| Console screenshot | `printf 'screendump /tmp/pxl-<vmid>-<ts>.ppm\nquit\n' \| ssh <target> qm monitor <vmid>`; then `ssh <target> cat /tmp/pxl-… .ppm` | QM `screendump` writes PPM; `png.py` converts PPM→PNG in the controller; host temp file deleted in a `finally` even on error; unique name per call |
| Console keys | `qm sendkey <vmid> <key…>` | first-class `qm` shortcut for the monitor `sendkey` (verified present in qm(1)); fallback `printf 'sendkey …\nquit\n' \| ssh <target> qm monitor <vmid>`. Key names are QEMU's (`ret`, `f2`, `spc`, `ctrl-alt-delete`, `shift-a`) |
| Console type | `qm sendkey <vmid> <key…>` per character | char→key translation table (shift-modified glyph names, `spc` for space, `dot`, `comma`, `minus`, …) with bounded pacing (≤ ~20 keys/s); text is never audited |
| Snapshots | — CUT — | Dropped from `guest.py`, the CLI and MCP by director decision (minimal surface); the `qm snapshot` family is not wrapped |
| Task waiting | `qm`/`pct` CLI calls block until their task ends (the ssh call IS the wait); final state re-verified with `qm status`/`pct status` | where a UPID is captured (e.g. `pvesh create` output), poll `pvesh get /nodes/<node>/tasks/<upid>/status --output-format json` until `stopped`, then read `exitstatus`; bounded by the same deadline machinery |
| GC install/status/uninstall | `ssh <target> install -m 0755 /tmp/pxl-gc /usr/local/sbin/pxl-gc`; `ssh <target> crontab -l`; `ssh <target> crontab -` (script on stdin) | see §F; upload via `ssh <target> cat > /tmp/pxl-gc`; every step idempotent and reported |

### Transport rules

- **One seam.** `ssh.py` is the only module that spawns `ssh`; everything above it
  (including `qm guest` traffic) goes through it, argv by argv.
- **Quoting.** ssh flattens its argv into a single remote shell command, so every
  argument is `shlex.quote`d at the seam — including guest-supplied strings. No caller
  composes remote shell strings by hand.
- **Command allowlist (the new least-privilege boundary).** `ssh.py` refuses any remote
  command outside the allowlist — `qm`, `pct`, `pvesh`, `pveversion`, `hostname`, `ip`,
  `ethtool`, `cat` (pxl temp files only), `install`, `crontab`, `shutdown`, `base64`,
  `true`. Arbitrary root shell is not a feature. The host-changing subset (`shutdown`,
  `crontab`, `install`, `ethtool`) additionally requires the explicit authorization
  flags (`--host-change-authorized` / `--standalone-authorized`) checked in `cli.py`
  before the call is built. This replaces both the scoped API token of the old world and
  `host_policy.check_api`'s API-path gate (the gate moved from URL parsing to argv
  policy; the *category* of what needs authorization is unchanged).
- **Bounded everything.** Every call carries a timeout (default 30 s, per-call override;
  long operations use deadline polling instead of long sockets); a timeout is a real
  error, never a silent partial result.
- **Host temp hygiene.** Files staged on the host (`/tmp/pxl-…`) use unique names and are
  deleted in `finally` blocks; the GC log is the only durable host-side file besides the
  script itself.
- **Redaction at the boundary.** Command output is redacted (same `SENSITIVE_KEY` rules
  as audit) before it can reach an event row or an MCP result.
## E. MCP tool surface

The MCP server is a NEW module `mcp.py`: a stdlib-only JSON-RPC 2.0 server speaking MCP over stdio, with **no MCP SDK**. There is no existing MCP module to extend — `mcp.py` absorbs the existing `leases.record_mcp_activity` / MCP idle-shutdown plumbing (`mcp_idle_elapsed`, `idle_shutdown_due`, `MCP_IDLE_SHUTDOWN_SECONDS`) and exposes exactly three methods: `initialize`, `tools/list`, `tools/call`. Handlers are thin wrappers over the SAME `lab` facade functions the slim CLI binds, so the CLI and MCP surfaces cannot drift.

### Protocol

Transport: JSON-RPC 2.0 over stdin/stdout, **newline-delimited JSON — one complete JSON-RPC message per line** (no LSP-style `Content-Length` framing), UTF-8. stdout carries protocol messages only; all logs and diagnostics go to stderr. Requests without an `id` are notifications and get no response. Malformed JSON → `-32700`; unknown method → `-32601`; both standard JSON-RPC.

`initialize` returns the negotiated protocol version, `serverInfo`, and tool capability — nothing else:

```json
→ {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"…","version":"…"}}}
← {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"serverInfo":{"name":"proxmox-agent-lab","version":"<__version__>"}}}
```

`tools/list` takes no meaningful params and returns the **static** list of 23 tools — built once at startup from a module-level spec (name, one-line description, JSON Schema `inputSchema` with `properties` / `required` / `additionalProperties: false`). The list never changes at runtime.

`tools/call` dispatches onto the same functions the slim CLI uses (shared `lab` facade; `cli.py` and `mcp.py` bind the same callables):

```json
→ {"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"guest_start","arguments":{"lease_id":"<lease-id>","vmid":101}}}
← {"jsonrpc":"2.0","id":3,"result":{"content":[{"type":"text","text":"{\"lease_id\":\"<lease-id>\",\"vmid\":101,\"state\":\"running\",\"upid\":\"UPID:…\"}"}]}}
```

Pinned result shape (every tool, including screenshots — `png_base64` lives inside the JSON body so exactly one result shape exists):

```json
{"content":[{"type":"text","text":"<json body mirroring CLI JSON output>"}]}
```

Pinned error shape — a JSON-RPC error object, never a result with an error string:

```json
{"jsonrpc":"2.0","id":4,"error":{"code":-32602,"message":"guest_destroy: missing required field 'confirm'"}}
{"jsonrpc":"2.0","id":5,"error":{"code":-32603,"message":"guest_stop failed for vmid 101: <redacted>"}}
```

- `-32602` (bad params): unknown tool name, schema violation, wrong type, unknown `console_keys` key name, missing/`false` `confirm`. The message names the offending field, never its value.
- `-32603` (action failure): dispatch happened and the action failed (`LabError`). The message is redacted through the audit redaction path — no secrets, no typed text, no tracebacks, no raw remote output that could embed credentials.

Every `tools/call` — read-only or mutating, success or failure — refreshes `last_mcp_activity` (see below) and records an audit event carrying **tool name + ok + target only** (target = `vmid` / `lease`), never text or secret arguments (`console_type.text`, `guest_run.command`, file contents are excluded entirely). This is safety-policy invariant 9.

### Idle shutdown

Load-bearing; it MUST survive the rewrite. Every `tools/call` refreshes `last_mcp_activity` (unix epoch) in `schema_meta` via the absorbed `record_mcp_activity` plumbing. When **no tool call has happened for `idle_shutdown_seconds` (28800 = 8h, config `[lease] idle_shutdown_seconds`) AND no lease is active**, a sweep performs the VERIFIED shutdown (`power.py`: initiate `shutdown`, then confirm by repeated probe until the host stops answering — success is never assumed). The check runs both on every `tools/call` and on a periodic self-wake of the long-lived server (stdin read with a ~60s `selectors` timeout, stdlib only), so it fires even when the client has gone silent. `cleanup-expired` enforces the same threshold through the same code path, so the duty survives hosts where no MCP server is running.

Reason this exists: an untracked guest would otherwise keep the host powered on for ever — a guest left behind by an agent that walked away is exactly the case where `lease_end` never arrives, and the host power-off rule never kills running guests. The idle threshold is the backstop that finally stops the work and the host.

### Tools

| Tool | Input schema | Returns |
|---|---|---|
| `lease_begin` | `purpose: string (required)`; `long_term: boolean (optional, default false)`; `ttl_seconds: integer (optional, default 7200)` | `{id, kind, purpose, state, created_at, expires_at}` — wakes the host (WoL) first if it is down |
| `lease_heartbeat` | `lease: string (required)`; `ttl_seconds: integer (optional)` | `{id, state, heartbeat_at, expires_at, guests_refreshed}` — `guests_refreshed` = the `<vmid>`s whose `pxl-expiry` metadata was rewritten |
| `lease_end` | `lease: string (required)`; `shared_guests_authorized: boolean (optional, default false)` | `{id, state, destroyed, errors}` — `state` is `ended` or `cleanup_failed`; `destroyed` = `<vmid>` list |
| `lease_list` | `include_ended: boolean (optional, default false)` | `{leases: [{id, kind, purpose, state, created_at, expires_at, heartbeat_at}]}` |
| `lease_destroy` | `lease: string (required)`; `confirm: boolean (required, must be true)` | `{id, state, destroyed}` — forcibly closes ordinary or long-term leases; destroys only lease-owned resources |
| `lease_register` | `lease: string (required)`; `kind: enum(qemu\|lxc) (required)`; `vmid: integer (required)`; `name: string (optional)`; `allow_existing: boolean (optional, default false)` | `{lease_id, resource_id, kind, vmid, name}` — adopts an existing guest and stamps pxl metadata on it |
| `guest_create` | `lease_id: string (required)`; `vmid: integer (required)`; `name: string (optional)`; `memory: integer (optional)`; `cores: integer (optional)`; `start: boolean (optional, default true)` | `{lease_id, vmid, kind, name, state, tags, pxl_expiry}` — built from the configured `template_vmid` |
| `guest_clone` | `lease_id: string (required)`; `vmid: integer (required)`; `source: integer (required)`; `name: string (optional)`; `full: boolean (optional, default true)` | `{lease_id, vmid, source, name, state, upid}` |
| `guest_start` | `lease_id: string (required)`; `vmid: integer (required)` | `{lease_id, vmid, state, upid}` |
| `guest_stop` | `lease_id: string (required)`; `vmid: integer (required)`; `timeout: integer (optional, default 120)`; `force: boolean (optional, default false)` | `{lease_id, vmid, state, graceful}` |
| `guest_destroy` | `lease_id: string (required)`; `vmid: integer (required)`; `confirm: boolean (required, must be true)`; `purge: boolean (optional, default true)` | `{lease_id, vmid, destroyed, purged}` — `qm destroy <vmid> --purge 1` / `pct destroy <vmid>` |
| `guest_probe` | `vmid: integer (required)` | `{vmid, exists, running, agent_ok, ip}` |
| `guest_list` | `lease_id: string (optional)`; `state: enum(running\|stopped\|all) (optional, default all)` | `{guests: [{vmid, kind, name, lease_id, state, tags, pxl_expiry}]}` |
| `guest_run` | `lease_id: string (required)`; `vmid: integer (required)`; `command: string (required)`; `timeout: integer (optional, default 300)`; `stdin: string (optional)` | `{lease_id, vmid, exit_code, stdout, stderr, duration_ms}` |
| `push_file` | `lease_id: string (required)`; `vmid: integer (required)`; `local_path: string (required)`; `remote_path: string (required)` | `{lease_id, vmid, local_path, remote_path, bytes}` |
| `pull_file` | `lease_id: string (required)`; `vmid: integer (required)`; `local_path: string (required)`; `remote_path: string (required)` | `{lease_id, vmid, local_path, remote_path, bytes}` |
| `console_screenshot` | `lease_id: string (required)`; `vmid: integer (required)` | `{lease_id, vmid, png_base64, width, height, taken_at}` — `qm monitor screendump` + PPM→PNG |
| `console_type` | `lease_id: string (required)`; `vmid: integer (required)`; `text: string (required)`; `enter: boolean (optional, default true)` | `{lease_id, vmid, sent, chars, enter}` — `text` is never logged or audited |
| `console_keys` | `lease_id: string (required)`; `vmid: integer (required)`; `keys: string[] (required)` | `{lease_id, vmid, sent_keys, ok}` — Proxmox key names (`ret`, `f2`, `ctrl-alt-delete`); unknown name → `-32602` |
| `cleanup_expired` | `lease_id: string (required)`; `confirm: boolean (required, must be true)`; `all: boolean (optional, default false)`; `no_backup: boolean (optional, default false)` | `{lease_id, destroyed, failed, idle_shutdown_triggered, host_powered_off}` |
| `journal_query` | `lease_id: string (optional)`; `since: string (optional, ISO-8601)`; `limit: integer (optional, default 50)` | `{events: [{id, timestamp, event, lease, vmid, data}]}` — legacy column names kept verbatim |
| `doctor` | `host_checks: boolean (optional, default false)` | `{ok, checks, version}` — `checks` = `[{name, ok, detail}]` |
| `power_status` | *(none)* | `{reachable, powered_on, last_power_event, active_leases, running_guests}` |

Rules encoded in the schemas above:

- **Lease scope.** Every mutating tool except the lease_* family carries `lease_id: string (required)`: over MCP every mutation belongs to a lease (safety invariant 1). `cleanup_expired` is therefore lease-scoped over the wire; the unscoped whole-host sweep is the operator's CLI call. `vmid: integer (required)` wherever the tool acts on one guest. The lease_* family addresses its lease as `lease: string (required)` (mirroring CLI `--lease`); `lease_begin` creates one and takes none.
- **Destructive tools require `confirm: boolean` = true** — `guest_destroy`, `lease_destroy`, `cleanup_expired` (it can destroy, so the flag is always demanded and checked before any destruction). Absent or `false` → `-32602`. This mirrors CLI `--confirm`; there is never an interactive prompt.
- **Read-only tools** — `doctor`, `power_status`, `journal_query`, `lease_list`, `guest_list`, `guest_probe` — take no `lease_id`, mutate nothing, and are safe for any caller; the other 17 are mutating (even `console_screenshot`, which drives `qm monitor`).
- `console_keys` takes `keys: string[]` of Proxmox key names (`ret`, `f2`, `ctrl-alt-delete`); `console_type` takes `text: string` + `enter: boolean` and its text is never logged or audited; `push_file`/`pull_file` take `vmid` + local path + remote path; `journal_query` takes optional `lease_id`, `since`, `limit`.

CLI-only commands and why:

- `gc install|status|uninstall` — one-time host maintenance, not agent work. Install/uninstall change the host (`/usr/local/sbin/pxl-gc` + root's crontab) and are gated `--host-change-authorized`; `status` is ungated but still operator-facing (section F).
- Standalone `power wake` / `power shutdown` — gated `--standalone-authorized`. MCP agents wake the host through `lease_begin` and never hold a bare power lever: the only shutdown an unattended agent can trigger is the idle sweep inside `cleanup-expired` / the server, which requires zero active leases and a verified probe.

(`init` writes the local config file and `mcp` runs this server — local process concerns, likewise not tools.)

## F. Host-side GC cron design

One optional host-side artifact: `resources/pxl-gc.py`, a NEW standalone script (python3, stdlib only, executable, **no package imports** — it runs on the Proxmox host with no `proxmox_agent_lab` installed), injected as a single root crontab line by `proxmox-lab gc install|status|uninstall`. It is stateless with respect to the controller: lease identity/expiry come from guest metadata written at creation time (tags carrying `pxl-lease` id + expiry epoch); it touches only pxl-tagged guests; it is idempotent and testable with stubbed `qm`/`pct` (`tests/support/fakeqm.py`).

### Guest metadata contract

Tags field (lowercase-safe, semicolon-separated in the Proxmox tags field): `pxl` marks ownership, `lease-<id>` records the owning lease. Description contains a parseable line `pxl-lease=<id> pxl-expiry=<unix_epoch>`; `pxl-expiry=0` means long-term / never expires. Written at creation and REFRESHED on every heartbeat or expiry extension.

At creation (QEMU / LXC respectively):

```sh
qm set   <vmid> --tags "pxl;lease-<id>" --description "pxl-lease=<id> pxl-expiry=<epoch>"
pct set  <vmid> --tags "pxl;lease-<id>" --description "pxl-lease=<id> pxl-expiry=<epoch>"
```

On every heartbeat / TTL extension, for **every guest registered to the lease** (tags unchanged; only the expiry line is rewritten — the controller reads the current description, replaces just the `pxl-…` line, preserving any free text it did not write):

```sh
qm set   <vmid> --description "pxl-lease=<id> pxl-expiry=<epoch>"
pct set  <vmid> --description "pxl-lease=<id> pxl-expiry=<epoch>"
```

Long-term leases write `pxl-expiry=0` and never refresh. **Heartbeat MUST refresh expiry metadata on every registered guest** — a live guest whose metadata goes stale looks expired and the GC would kill live work; the GC itself stays stateless regardless of who writes the metadata. The GC touches a guest only if its tags contain `pxl` **and** the description line parses. Long-term protection is tag/description-based **ONLY** (`pxl-expiry=0`) — the PVE guest `protect` flag is deliberately not used (unverified across versions; one mechanism only).

### Replacing the host guard

This replaces `resources/pxl-hostguard.py` — today's systemd timer that reads MariaDB leases and powers the host off when all leases end — and therefore carries **BOTH** of its duties: **expired-guest destruction AND power-off-when-idle**. Guest destruction alone is not enough (the old guard exists precisely because destruction alone leaves the host up); the GC's second pass is the power-off-when-idle rule, and both live in the same stateless script. Unlike the guard, the GC reads no database at all: no MariaDB, no controller SQLite — the guests' own metadata is the entire lease record. The old guard's systemd units, its `__GUARD_INSTALL__` hook, and `hostguard.py` go away in the cutover.

### Crontab line

A single line injected into root's crontab, preceded by a `# pxl-gc` marker comment for idempotent detect/remove:

```
# pxl-gc
*/10 * * * * /usr/local/sbin/pxl-gc >>/var/log/pxl-gc.log 2>&1
```

Install reads `crontab -l`, strips any existing `# pxl-gc` block (marker line + the following cron line), appends the block above, and writes it back with `crontab -`; re-running install replaces the block in place and reports "unchanged" when it matches. Uninstall removes exactly the marker line and its cron line. The 10-minute cadence matches the two-consecutive-runs rule below.

### Script algorithm

`resources/pxl-gc.py` (run as `/usr/local/sbin/pxl-gc`), numbered pseudocode:

1. Parse argv: `--dry-run` (print intended actions, touch nothing) and nothing else; unknown flag or extra arg → usage on stderr, **exit 2** (the only non-zero exit — a run never fails because one guest misbehaved).
2. Enumerate every guest on the node: `qm list` and `pct list`.
3. For each guest read `qm config <vmid>` / `pct config <vmid>`; if `template: 1` → skip, log "template".
4. Read `tags`; unless the semicolon-separated list contains the token `pxl` → skip, log "not pxl".
5. Parse `description` for `pxl-lease=<id>` and `pxl-expiry=<epoch>`; missing or malformed → **WARN + skip** (log the reason; never deletes).
6. `pxl-expiry=0` → long-term → skip, log "long-term".
7. `now < expiry` → skip, log "unexpired".
8. Expired → acquire per-vmid lock `/var/lock/pxl-gc-<vmid>.lock` (`flock`, non-blocking); lock held by another run → skip, log "locked".
9. `--dry-run` → log "would stop and destroy <vmid> (expired <epoch>)", release the lock, continue with the next guest.
10. Graceful shutdown first: `qm shutdown <vmid> --timeout 120` / `pct shutdown <vmid> --timeout 120`; poll status up to 120s.
11. Still running after 120s → hard stop **this guest only**: `qm stop <vmid>` / `pct stop <vmid>`.
12. Destroy: `qm destroy <vmid> --purge 1` / `pct destroy <vmid>`; guest already gone → no-op success (idempotent). Log every action and every skip reason to stdout (→ `/var/log/pxl-gc.log`).
13. POWER-OFF pass (the second duty; runs every pass, destroyed anything or not):
    a. `running` = guests with status running — ANY guest, pxl-tagged or not.
    b. `pinned` = pxl guests whose metadata parses and whose expiry is `0` or in the future.
    c. If `running == 0` and `pinned == 0`: consult the host-side stamp `/var/lib/pxl-gc/clear-stamp`. No stamp → write it (first clear observation) and stop here. Stamp present and `now - stamp >= 600` (the condition held at two consecutive runs ≥10 min apart) → log why ("powering off: 0 running guests, 0 unexpired pxl guests, clear since <stamp>") and run `shutdown -h now` on the host. Otherwise wait.
    d. Condition not clear → remove the stamp if present.
14. Exit 0.

### Safety rules

- Only pxl-tagged guests with a parseable `pxl-lease=`/`pxl-expiry=` line are ever destruction candidates. Non-pxl guests and templates (`template: 1` in config) are never touched.
- Parse failure never resolves to deletion: a missing token, garbage epoch, or unreadable config is warn-skipped in the safe direction.
- Graceful shutdown (120s) before any hard stop; the hard stop applies only to the expired pxl guest itself. Never a forced power-off of the host — the host runs `shutdown -h now` only via the clear rule, and never while any guest is running (someone else's work is untouchable, pxl or not).
- Never powers off while any pxl guest has unexpired metadata (`pxl-expiry=0` long-term leases pin the host on).
- Never reads the controller's SQLite or any controller state — the GC knows only guest metadata plus its own host-local stamp; "stateless" means no controller-side state is required.
- Residual race — known, bounded behavior (accepted by the director): the controller has woken the host (`lease_begin` → WoL) but not yet created a guest, so the host looks clear. Bounded to ≤ one GC interval (10 min) by the two-run rule — the first clear run only stamps; the guest and its fresh metadata appear within the interval and break the clear condition before the second run. If the host does power off first, the next `lease_begin` wakes it again: recoverable, never data loss.
- Idempotent and concurrency-safe: per-vmid `flock` under `/var/lock`; an already-gone guest is a success no-op.

### gc install|status|uninstall behavior

- **install** (gated `--host-change-authorized`): scp the bundled `resources/pxl-gc.py` to `/usr/local/sbin/pxl-gc` (mode 0755), ensure `/var/lib/pxl-gc` exists, and inject the crontab line idempotently via the `# pxl-gc` marker (detect/remove/replace). Reports exactly what changed: script written/updated/unchanged with its checksum, crontab block added/replaced/unchanged.
- **status** (ungated, read-only): script presence at `/usr/local/sbin/pxl-gc` + checksum against the bundled copy, `# pxl-gc` block present in root's crontab, the last N lines of `/var/log/pxl-gc.log`, and a remote `pxl-gc --dry-run` showing what would be destroyed plus the power-off verdict. Nothing is modified.
- **uninstall** (gated `--host-change-authorized`): remove the `# pxl-gc` crontab block and `/usr/local/sbin/pxl-gc`, report the changes, and **never touch guests** — no guest state is read or written on the way out.

Install/uninstall belong to the host-changing command subset (`shutdown`, `crontab`, `install`) and are refused without `--host-change-authorized`; `status` needs no flag. The script itself is exercised against the pinned test harness (`tests/support/fakeqm.py` stub `qm`/`pct` executables on `PATH`).
## G. Config schema and doctor checklist

### Config file

One TOML file is the entire configuration surface. Exactly these keys — nothing
else is read (stdlib `tomllib`):

```toml
[ssh]
target = "proxmox"        # ssh alias/host reached as root (renames old [memflow] ssh_host)

[pve]
node = "pve"              # node name used in pvesh paths
template_vmid = 100       # default template for guest clone/create

[power]
mac = ""                  # wired NIC MAC for WoL (filled by `proxmox-lab init`)
broadcast = "255.255.255.255"
port = 9

[state]
dir = "~/.local/share/proxmox-agent-lab"   # lab.db lives here

[lease]
ttl_seconds = 7200
idle_shutdown_seconds = 28800
```

Rename note: `[ssh] target` replaces the old `[memflow] ssh_host` gate — the SSH
transport (`ssh.py`) is the whole control plane and is gated on `[ssh] target`
alone; `[memflow]` and its subsystem are gone.

Secrets/credential configuration disappears entirely: SSH agent/keys only. No
keychain/env/shared-store backends survive. Cut with the MariaDB ledger:
`secrets_store.py` (all backends), `connection.py` export/import of credential
bundles, the `secrets list` surface, and the ledger's `secrets` table (bootstrap
secret `mariadb-password`). No config key holds a credential.

### Discovery

1. env `PROXMOX_AGENT_LAB_CONFIG` (explicit path) first;
2. then `~/.config/proxmox-agent-lab/config.toml`.

A missing or unparseable config raises `ConfigError` at use time only —
importing modules MUST survive missing config so `doctor` and `init` work on a
broken install.

### `init`

- Writes the starter config (the block above) with placeholders where values are
  host-specific.
- Discovers the WoL MAC over ssh (`ip -br link show` on the target as root) and
  writes the wired NIC's MAC into `[power].mac` of the user's config file.
- Never writes a real MAC into the repo: checked-in docs and examples use
  `aa:bb:cc:dd:ee:ff`, because `scripts/check-secrets.py` bans real hardware
  identifiers.

### Doctor checklist

Numbered checks; every check reports pass/warn/fail as noted (gc crontab is
info). Doctor exits non-zero iff any check fails.

1. **Python >= 3.11** — fail on older interpreters (runtime floor).
2. **Config exists and parses** — fail on missing/unparseable config; the safety
   invariant (imports survive missing config) is what got doctor this far.
3. **SSH connect as root** — `ssh -o BatchMode=yes` to `[ssh] target`; fail on
   refusal/timeout with the hint `ssh-copy-id root@<target>`.
4. **Remote tooling present** — `qm`, `pct`, `pvesh`, `pveversion` on the host;
   fail if any is missing.
5. **Node identity** — `[pve] node` matches the host's `hostname -s`; warn on
   mismatch.
6. **State dir + store** — `[state] dir` writable and `lab.db` opens with the
   expected `schema_meta` version; fail.
7. **Template exists** — `[pve] template_vmid` resolves; warn if absent.
8. **WoL MAC** — `[power].mac` non-empty; warn if empty.
9. **GC crontab installed** — info if the host-side gc cron line is absent.
10. **Drift check** — pxl-tagged guests whose description lease/expiry metadata
    disagrees with `lab.db`; warn per drifted guest.

Safety invariant: doctor runs even on a broken or missing config — a healthy
install is never a prerequisite for diagnosing one.

## H. Test plan

### Test harness

- `tests/support/fakessh.py` — FakeSSH runner: records argv lists, scripted
  outputs keyed by regex on the command string, scriptable failures/timeouts.
  Modules under test receive it via injection or monkeypatched `ssh.run`; no test
  spawns a real ssh.
- `tests/support/fakeqm/` — stub `qm`/`pct` executables on PATH so the real
  `resources/pxl-gc.py` runs end to end.

### Coverage

| Area | Must cover |
|---|---|
| ssh transport | argv construction + quoting; timeout mapping; command-allowlist refusal of arbitrary shell |
| store/SQLite | legacy-events DDL reuse + schema version; CAS state transitions; WAL concurrency; redaction-before-insert; journal query filters |
| leases | begin requires reachable host and stamps guest metadata; heartbeat REFRESHES guest expiry metadata; expiry computation; long-term `pxl-expiry=0` protection (tag/description only — no PVE `protect` flag) |
| cleanup | idempotent finalize (second run no-op); only lease-owned pxl-tagged resources destroyed; `cleanup_failed` recorded and retried next sweep; shared-guest cross-reference refusal; lease-end pre-check refusal |
| power | magic-packet bytes == 6×0xFF + 16×MAC golden test; verified shutdown: probe-failure counting, never assumed, timeout path reports `host_powered_off=false` + non-zero exit |
| guest lifecycle | create/clone stamped tags+description; destroy refuses unregistered/untagged |
| transfer | push/pull chunked base64 via guest exec on FakeSSH (the only transfer path; sha256 verify) |
| console | sendkey translation incl. shift glyphs + `ret`/`f2`/`ctrl-alt-delete`; PPM->PNG golden fixture; type pacing bounded |
| gc script | stubbed qm: destroys only expired pxl guests; skips `pxl-expiry=0`; warns-skips unparseable description; idempotent; `--dry-run`; refuses templates; power-off-when-idle fires only when zero running guests AND two consecutive clear runs — assert the two-run rule |
| mcp | stdio smoke: spawn `python3 -m proxmox_agent_lab mcp`, initialize -> result, tools/list -> exactly 23 tools with schemas, tools/call read-only (doctor) + mutating through FakeSSH, JSON-RPC error shape on bad params; idle clock: `last_mcp_activity` refresh + idle-shutdown sweep fires after threshold with no active lease |
| diagnostics | doctor on missing config |

### Canonical commands

```
PYTHONWARNINGS=error /opt/homebrew/bin/python3.14 -m unittest discover -s tests -q
python3 -m compileall -q src tests
python3 scripts/check-secrets.py
python3 scripts/check-public.py
python3 scripts/check-release.py
bash -n on every changed shell script
git diff --check
```

The final suite must pass all of these. `resources/pxl-gc.py` is Python — it is
covered by `compileall`, not `bash -n` (that applies to changed shell scripts
only). Tests are warning-clean (`PYTHONWARNINGS=error`) and deterministic. No
formatter, linter, or task runner may be introduced.

### Dependency-removal change sites

Exactly three places change when dropping PyMySQL/cryptography:

1. `pyproject.toml` `[project].dependencies` — drop `PyMySQL>=1.1` and
   `cryptography>=41` (stdlib-only wheel).
2. `.github/workflows/ci.yml` — wheel smoke `import pymysql, cryptography` assert.
3. `.github/workflows/release.yml` — same assert.

## I. Order of work

Cutover rule: each phase deletes what it replaces in the same change set — the
superseded module, its old tests, and every remaining caller. Callers a later
phase rewrites are repointed to the new API in the landing phase (mechanical;
their real rewrite comes later); CUT callers the replacement orphans are deleted
right there. Nothing is left both superseded and imported, so no phase lands red.
Phase 7 sweeps every CUT file the earlier phases did not already orphan. Every
phase ends with the canonical gates (§H) green.

### 1. Core services

Lands `config.py` (REWRITE — TOML, discovery order, defaults; imports survive
missing config), `errors.py` (KEEP — `LabError`/`ConfigError`), `ssh.py` (NEW —
subprocess SSH transport + command allowlist: `qm`, `pct`, `pvesh`,
`pveversion`, `hostname`, `ip`, `cat` of pxl temp files, `crontab`, `install`,
`shutdown`, `ethtool`, …; arbitrary root shell refused; the host-changing subset
(`shutdown`, `crontab`, `install`) requires `--host-change-authorized` /
`--standalone-authorized`), `store.py` (NEW — `lab.db` DDL: `schema_meta`,
legacy `events` DDL reused verbatim, `leases`, `resources`; WAL, busy_timeout,
foreign_keys; CAS state-transition UPDATEs; BEGIN IMMEDIATE), and the FakeSSH
harness (`tests/support/fakessh.py`).
Deletes: `state.py` (-> store), `host_transport.py` + `host_policy.py` (->
ssh allowlist), their tests; remaining callers repointed.
Tests: `tests/test_config.py`, `tests/test_ssh.py`, `tests/test_store.py`
(legacy `CREATE TABLE events (...)` asserted against the real DDL).
**Acceptance:** the three test modules pass under the canonical unittest
command; every module imports with no config present; `ssh.py` refuses both a
non-allowlisted command and an unauthorized host-change command; canonical gates
green.

### 2. Proxmox wrappers + power

Lands `proxmox.py` (NEW — `qm`/`pct`/`pvesh`/`pveversion` wrappers + task-wait
with bounded deadlines; replaces `api.py` + `guest_agent.py` primitives),
`power.py` (REWRITE — stdlib UDP magic packet + verified shutdown).
Deletes: `api.py`, `guest_agent.py`, their tests; callers repointed.
Tests: `tests/test_proxmox.py`, `tests/test_power.py` (magic-packet golden =
6×0xFF + 16×MAC; verified shutdown: probe-failure counting, never assumed,
timeout path reports `host_powered_off=false` + non-zero exit).
**Acceptance:** golden + shutdown tests pass on FakeSSH; `api.py`/`guest_agent.py`
gone with no dangling imports; canonical gates green.

### 3. Lease lifecycle

Lands `leases.py` (REWRITE — begin/end/list/destroy/register incl. long-term
`pxl-expiry=0`; absorbs `longterm.py`), `cleanup.py` (REWRITE — finalize lease,
cleanup-expired, shared-guest/ownership checks), `audit.py` (REWRITE — event
write + redaction-before-insert into SQLite), `journal.py` (REWRITE — SQLite
query; legacy-events reader informs the reuse).
Deletes: `longterm.py`, journal-spool machinery, their tests; callers repointed.
Tests: `tests/test_leases.py`, `tests/test_cleanup.py`, `tests/test_audit.py`,
`tests/test_journal.py`, plus lifecycle-invariant tests: begin requires reachable
host and stamps guest metadata; heartbeat REFRESHES expiry metadata; CAS blocks
double-finalize; MCP idle clock (`last_mcp_activity` refresh; idle-shutdown sweep
fires after `idle_shutdown_seconds` only with no active lease).
**Acceptance:** lifecycle invariants and cleanup idempotence proven
(`cleanup_failed` retry, lease-end pre-check refusal, shared-guest refusal);
canonical gates green.

### 4. Guest, transfer, console, PNG

Lands `guest.py` (REWRITE — create/clone/start/stop/destroy/probe/list/run;
stamps tags + description at creation), `transfer.py` (REWRITE —
push/pull over ssh + `qm guest`/`pct exec` file IO), `console.py` (REWRITE —
`qm monitor` screendump + `qm sendkey`), `png.py` (KEEP + new PPM->PNG
conversion).
Deletes: `serial.py` (websocket console transport gone), its tests; callers
repointed.
Tests: `tests/test_guest.py`, `tests/test_transfer.py`, `tests/test_console.py`,
`tests/test_png.py` (PPM->PNG golden fixture; sendkey translation incl. shift
glyphs + `ret`/`f2`/`ctrl-alt-delete`; bounded type pacing).
**Acceptance:** create/clone stamping and destroy refusals (unregistered/
untagged) proven; push/pull round-trip on FakeSSH; canonical gates green.

### 5. CLI cutover + diagnostics + GC

Lands `cli.py` (REWRITE — argparse + policy gates + slim `lab` facade over the
pinned CLI surface), `diagnostics.py` (REWRITE — init/doctor), `gc.py` (NEW —
`gc install|status|uninstall`), `resources/pxl-gc.py` (NEW — standalone host-side
GC script: python3, stdlib only, no package install).
Deletes: `inventory.py`, `storage.py`, `hostinfo.py` (MAC discovery folds into
init/diagnostics), `hostguard.py` + `resources/pxl-hostguard.py` (replaced by
`resources/pxl-gc.py`), `mariadb.py`/`secrets_store.py`/`connection.py` once
their last importers (old cli/diagnostics) are gone, and their tests.
Tests: `tests/test_cli.py`, `tests/test_diagnostics.py` (doctor on missing
config), `tests/test_gc.py` (real `resources/pxl-gc.py` under
`tests/support/fakeqm/` stubs: destroys only expired pxl guests; skips
`pxl-expiry=0`; warns-skips unparseable description; idempotent; `--dry-run`;
refuses templates; power-off-when-idle only when zero running guests AND two
consecutive clear runs — assert the two-run rule).
**Acceptance:** the pinned CLI surface works end to end on FakeSSH/FakeQM; doctor
reports every checklist level correctly with a broken config; canonical gates
green.

### 6. MCP server

Lands `mcp.py` (NEW — stdlib-only stdio JSON-RPC 2.0: `initialize`,
`tools/list`, `tools/call` as thin wrappers over the same lab functions the slim
CLI uses; exactly the 23 pinned snake_case tools; refreshes `last_mcp_activity`
on every `tools/call`; idle-shutdown sweep when idle past `idle_shutdown_seconds`
with no active lease). No MCP SDK.
Deletes: the old `leases.record_mcp_activity` / `mcp_idle_shutdown` plumbing
folds into the server; no orphaned copies left.
Tests: `tests/test_mcp.py` — stdio smoke spawning `python3 -m proxmox_agent_lab
mcp`: initialize -> result; tools/list -> exactly 23 tools with schemas;
tools/call read-only (doctor) and mutating through FakeSSH; JSON-RPC error shape
on bad params; idle-clock behavior.
**Acceptance:** the 23-tool contract and idle-shutdown behavior proven over real
stdio; canonical gates green.

### 7. Deletion pass

Remove every CUT file per the disposition matrix that phases 1–6 did not already
orphan: `mariadb.py`, journal spool, `s3.py`/MinIO/backup, `secrets_store.py`,
`api.py`, `onboarding.py`/`onboarding_host.py` + ISO/pairing,
`windows.py`/`pe.py`/`bootstruct.py`, `android.py`, `memflow.py`, `netcap.py`,
`usb.py`, `share.py`/`share_server.py`, `netgw.py`, `virtio.py`,
`disk.py`/`diskactivity.py`/`ioworkload.py`, `isoinspect.py`, `recipes.py`,
`oci.py`, `crash.py`, `vision.py`, `rfb.py`/`ws.py`/`des.py`/`textmode.py`,
`updates.py`, `hostguard.py` + `resources/pxl-hostguard.py`, the host-setup shell
scripts (`resources/ledger-host-setup.sh` (mariadb), memflow, mitm, minio,
proxmox), the `install.sh`/`bootstrap.sh` ceremony, `install-watchdog`,
`examples/cert-pin-poc`, and their tests/docs. Then the pyproject slim-down and
the three dependency-removal change sites (§H): drop PyMySQL/cryptography from
`pyproject.toml` `[project].dependencies`, drop the `import pymysql, cryptography`
wheel-smoke asserts from `.github/workflows/ci.yml` and
`.github/workflows/release.yml`. Update `scripts/check-release.py` and
`scripts/check-public.py` for the new config keys and the stdlib-only wheel.
**Acceptance:** no CUT file remains and nothing imports one; `import
proxmox_agent_lab` is stdlib-only; all three dependency sites changed; canonical
gates green.

### 8. Docs pass

Root `SKILL.md` + `.agents/skills/proxmox-agent-lab/SKILL.md` (currently 0 bytes)
carrying the kept surface (CLI, the 24 MCP tools, guest metadata contract,
safety invariants), `README.md`, `docs/` trimmed to the REWRITE set, a
`CHANGELOG.md` entry for the rework, and a `docs/VERIFICATION.md` note honest
per AGENTS.md: what the unit suite proves, with no live-hypervisor run claimed.
Tests: `tests/test_omp_skill.py` (skill-contract check) updated.
**Acceptance:** docs describe exactly the shipped surface (no cut module, key,
or tool named as live); `tests/test_omp_skill.py` and the canonical gates green.
