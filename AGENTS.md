# Repository Guidelines

> **Audience:** Contributors and maintainers editing `src/proxmox_agent_lab/`, `tests/`, and automation in `.github/`/`scripts/`.
> **Scope:** Project overview, architecture, key directories, dev commands, and code conventions. For lab operation, see `SKILL.md` (lease quick-ref) and `docs/AGENTS.md` (deep operational guidance).

## Project Overview

`proxmox-agent-lab` is a Python package and agent skill for operating a disposable Proxmox research lab. The `proxmox-lab` CLI powers on a spare host, creates or operates leased VMs/LXCs, exposes guest consoles and file/network tooling, records an audit trail, destroys lease-owned resources, and verifies host power-off.

Use it only for systems the operator owns or is authorized to test. The safety model is part of the product: leases, ownership checks, expiry, audit redaction, fail-closed networking, explicit host-change gates, and verified shutdown must remain intact.

## Architecture & Data Flow

1. **CLI and command registration**
   - `src/proxmox_agent_lab/cli.py` owns configuration loading, parser construction, command policy gates, and the `lab` facade every feature module receives. Implementation lives in focused modules: `errors.py` (exceptions), `state.py` (JSON persistence and controller locking), `api.py` (the Proxmox HTTPS client and task waiting), `audit.py` (the shared ledger), `updates.py` (version check), `leases.py` and `cleanup.py` (lease lifecycle, resource registration, teardown, shutdown), and `diagnostics.py` (`init`, `doctor`, `journal`, secrets commands).
   - `src/proxmox_agent_lab/__main__.py` and the installed `proxmox-lab` entry point call `cli.main()`.
   - Feature modules register subcommands with `register(sub, lab)` and reach shared state through the `lab` facade (`from .cli import _bind`). Do not create a second command-dispatch architecture. The module map, dependency direction, and compatibility alias policy are documented in `docs/architecture.md`.

2. **Configuration and secrets**
   - `config.py` loads TOML with precedence from `PROXMOX_AGENT_LAB_CONFIG`, checkout/config locations, XDG config, and the default user config path. Site-specific values belong there, not in source.
   - `secrets_store.py` reads the configured backend first (`auto` selects environment variables), then environment and shared MariaDB fallbacks. OS keychains remain explicit options. Secrets must not appear in argv, config, audit records, or committed files.
   - Imports must survive missing or malformed configuration. `cli.py` records configuration errors so `init` and `doctor` can still diagnose and repair the install.

3. **Lease, API, and cleanup flow**
   - A lease begins by ensuring the host is reachable/powered on, snapshots initial resources, and writes lease state under the configured state directory.
   - Mutating API calls require a lease and are restricted to safe guest paths unless an explicit host-change authorization flag is supplied. Resources are registered with the lease; destructive operations require ownership.
   - Lease end cleans up owned resources in dependency-safe order, records failures, and powers off the host only when the shutdown and no-other-lease conditions are satisfied. Shutdown is verified by repeated API failure, not assumed from a request.
   - Ordinary leases expire; long-term leases deliberately pin the host on and use separate protection, release, destroy, and backup semantics.
   - Operational lease shape (trap, `lease-begin`/`lease-end`, `lease-heartbeat`) is canonical in `SKILL.md` § Every task follows this shape — see there for the copy-paste block. Host-setup one-liners live in `docs/INSTALL.md`.

4. **Guest and protocol channels**
   - `guest.py` probes the guest and prefers qemu-guest-agent for real exit codes, then serial when available; console/VNC is used when the screen is the source of truth.
   - `console.py` keeps the screen/input/inspection commands; `serial.py` holds the terminal session and its Proxmox websocket transport, `guest_agent.py` the qemu-guest-agent primitives, and `transfer.py` the S3 push/pull wiring. `rfb.py`, `ws.py`, `des.py`, `png.py`, and `textmode.py` implement the WebSocket, RFB, PNG encode/decode/resample, and terminal-text paths without third-party runtime packages. Reading a screen is a vision job: `console inspect` sends one to a configured provider and `console screenshot --for-model` hands a bounded base64 copy back to the caller. Long operations use bounded polling/deadlines and explicit timeouts.
   - `storage.py`, `s3.py`, `share.py`, and `share_server.py` handle transfers, backups, and expiring local console links. `netgw.py` creates fail-closed VPN gateway networking.
   - `memflow.py`, `usb.py`, and `netcap.py` are deliberate exceptions to the API-token boundary: they use opt-in SSH access to host-side tooling or disposable LXCs and require their documented authorization gates. `host_transport.py` owns the shared host-SSH transport for them; it never imports the opt-in feature modules. Large remote programs installed on the host live in `src/proxmox_agent_lab/resources/`.

5. **State and audit**
   - Lease and activity state are JSON files under the runtime state directory, protected by the controller lock. The audit journal uses shared MariaDB with a local spool when the ledger is unreachable; legacy SQLite/JSONL data can be migrated. Audit fields are redacted.
   - Never put runtime state, journals, captures, or site topology in the repository.

## Key Directories

- `src/proxmox_agent_lab/` — installable package and all CLI/subsystem modules.
- `tests/` — deterministic `unittest` suite, protocol fakes, guard tests, and `tests/fixtures/config.toml`.
- `docs/` — installation, configuration, operational agent guidance, safety policy, subsystem behavior, and hardware-verification notes.
- `scripts/` — checkout CLI wrapper, watchdog installer, secret/public-content guards, and release metadata validation.
- `.github/workflows/` — CI and tag-gated release workflows.
- `examples/`, `assets/`, and `agents/` — examples, image/template metadata, and agent integration metadata.
- `bootstrap.sh`, `install.sh`, and `proxmox-host-setup.sh` — bootstrap, installation, and Proxmox host setup paths.

## Development Commands

Create a development environment and install only optional development tooling:

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

Run from a checkout without installing, or use the installed entry point:

```bash
scripts/proxmox-lab --help
proxmox-lab init
proxmox-lab doctor
```

Run the canonical test suite and required local checks:

```bash
PYTHONWARNINGS=error python3 -m unittest discover -s tests -q
python3 -m compileall -q src tests
python3 scripts/check-secrets.py .
python3 scripts/check-public.py .
python3 scripts/check-release.py
git diff --check
# Also run bash -n on every changed shell script.
```

Build and smoke-test distribution artifacts as CI does:

```bash
python3 -m pip install --disable-pip-version-check build
python3 -m build
python3 -m venv /tmp/proxmox-agent-lab-smoke
/tmp/proxmox-agent-lab-smoke/bin/pip install --no-deps dist/*.whl
PROXMOX_AGENT_LAB_CONFIG=/tmp/missing.toml \
  /tmp/proxmox-agent-lab-smoke/bin/proxmox-lab --help
```

For a release, update the version in `pyproject.toml`, `src/proxmox_agent_lab/__init__.py`, and `REQUIRED_VERSION` in `bootstrap.sh`, update the dated `CHANGELOG.md` section, then run `python3 scripts/check-release.py --tag vX.Y.Z`. The release workflow builds the wheel and sdist, smoke-installs the wheel, and writes SHA-256 checksums.

## Code Conventions & Common Patterns

- Keep runtime code compatible with Python 3.11+ and prefer the standard library. The shared MariaDB client already depends on `PyMySQL` and `cryptography`. Existing modules use `from __future__ import annotations`, type annotations, snake_case names, and small focused helpers.
- Add commands through the existing `cmd_*`/parser and sibling-module registration conventions. Reuse `LabError`, `ConfigError`, API helpers, lease helpers, audit helpers, and shared configuration instead of duplicating them.
- Preserve bounded behavior: use existing timeout/deadline polling for Proxmox tasks, guest operations, network calls, and power transitions. Map expected operational failures to the package's user-facing error path; do not swallow safety failures.
- Treat configuration as process-wide cached state and runtime state as explicit files/databases. Tests may reset caches and patch state roots, but production code must keep locking, atomic writes, expiry, and audit behavior.
- Put guard checks before side effects. Host networking/storage/permissions, USB passthrough, memflow preparation, live memory writes, disk formatting, and similar operations require their documented authorization flags and target verification.
- Protocol tests must assert required client messages, ordering, framing, and side effects. A passive fake that accepts an incomplete protocol is not sufficient.
- Generated guest scripts must be deterministic, escape values correctly, contain no unresolved placeholders, and pass the existing syntax checks.
- No repository formatter, linter, type checker, or task runner is configured. Do not introduce a parallel style/tooling convention without updating project configuration and CI.

## Important Files

- `pyproject.toml` — package metadata, Python requirement, optional dev dependency, Hatchling build, and CLI entry point.
- `src/proxmox_agent_lab/cli.py` — parser, command gates, the `lab` facade, and thin compatibility wrappers; `errors.py`, `state.py`, `api.py`, `audit.py`, `updates.py`, `leases.py`, `cleanup.py`, and `diagnostics.py` hold the implementation it exposes.
- `src/proxmox_agent_lab/config.py` — TOML defaults, config discovery, state directory, and template generation.
- `src/proxmox_agent_lab/secrets_store.py` — secret storage/retrieval backends.
- `src/proxmox_agent_lab/guest.py`, `console.py`, `serial.py`, `guest_agent.py`, `transfer.py`, `host_transport.py`, `longterm.py`, `netgw.py`, `storage.py`, `windows.py`, `android.py`, `memflow.py`, `usb.py`, and `netcap.py` — major feature boundaries.
- `tests/fixtures/config.toml` — the only configuration tests should load; it contains deterministic non-site test values.
- `tests/test_proxmox_lab.py`, `test_abstractions.py`, `test_console.py`, and `test_longterm.py` — core lifecycle, abstraction, protocol, and lease-invariant coverage.
- `scripts/check-secrets.py`, `check-public.py`, and `check-release.py` — repository safety and release guards.
- `CONTRIBUTING.md` — authoritative developer setup, required checks, and release checklist.
- `docs/AGENTS.md` and `docs/safety-policy.md` — operational agent workflow and enforced safety invariants; `docs/architecture.md` documents the module map, the `register(sub, lab)`/`_bind` contract, and the compatibility-alias policy; `docs/VERIFICATION.md` separates real-hardware evidence from unit-tested-only behavior.
- `.github/workflows/ci.yml` and `release.yml` — authoritative CI, package smoke test, and release behavior.

## Runtime/Tooling Preferences

- Required runtime: system Python 3.11 or newer; CI covers 3.11–3.14. A normal `pip install` installs the declared `PyMySQL` and `cryptography` runtime dependencies. Imports and `--help` must also survive their absence so broken installs remain diagnosable.
- Build backend: Hatchling. The optional `.[dev]` extra provides `pytest`, but the canonical suite is direct `unittest` discovery.
- `scripts/proxmox-lab` is the preferred checkout runner; installed users use the same `proxmox-lab` command from PATH.
- CI installs `xorriso` for ISO-related tests. Do not assume host-side Rust tools, Ghidra, tcpdump, mitmproxy, or other memflow/USB/netcap tooling is bundled in the Python package; those are installed on the hypervisor or disposable LXC during setup.
- Keep imports safe on broken installs and keep `init`/`doctor` usable when configuration or secrets are absent.

## Testing & QA

- Tests use `unittest.TestCase` and `python -m unittest discover -s tests`; there is no configured coverage threshold.
- Set `PROXMOX_AGENT_LAB_CONFIG` to `tests/fixtures/config.toml` before importing package modules. Tests commonly isolate filesystem state with `TemporaryDirectory`, patch module state and network/secrets, and clean up servers/threads in `finally`/`tearDown`.
- Test negative paths and guards, not only success: lease ownership and expiry, long-term confirmation, host-change authorization, disk serial/size checks, VPN fail-closed behavior, memflow live-write confirmation, USB/netcap boundaries, secret redaction, and verified shutdown.
- Use active protocol fakes that record writes and assert handshake/message order, framing, request paths, and call ordering. Exercise real localhost servers where access-control or framing behavior is the contract.
- Keep warning-clean tests (`PYTHONWARNINGS=error`), compile source and tests, run secret/public/release guards, and syntax-check changed shell scripts. CI runs these across Python 3.11–3.14 and smoke-installs the built wheel.
- Hardware-facing changes must update `docs/VERIFICATION.md` with exactly what was observed and what remains unit-tested only. Do not claim real hardware validation from offline tests.
- Before committing, do not include credentials, private keys, presigned URLs, host addresses, MAC addresses, VMIDs, disk serials, captures, guest memory, site notes, or runtime journals. Keep `scripts/check-secrets.py` strict; add an allowlist entry only for a genuinely public constant with an explanation.
