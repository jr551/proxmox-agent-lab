# Repository Guidelines

## Project Overview

`proxmox-agent-lab` is a standard-library Python package and agent skill for operating a disposable Proxmox research lab. The `proxmox-lab` CLI powers on a spare host, creates or operates leased VMs/LXCs, exposes guest consoles and file/network tooling, records an audit trail, destroys lease-owned resources, and verifies host power-off.

Use it only for systems the operator owns or is authorized to test. The safety model is part of the product: leases, ownership checks, expiry, audit redaction, fail-closed networking, explicit host-change gates, and verified shutdown must remain intact.

## Architecture & Data Flow

1. **CLI and command registration**
   - `src/proxmox_agent_lab/cli.py` owns configuration loading, the Proxmox HTTPS API client, parser construction, lease lifecycle, audit, core commands, and error handling.
   - `src/proxmox_agent_lab/__main__.py` and the installed `proxmox-lab` entry point call `cli.main()`.
   - Feature modules register subcommands and call shared CLI state through the existing module callback pattern. Do not create a second command-dispatch architecture.

2. **Configuration and secrets**
   - `config.py` loads TOML with precedence from `PROXMOX_AGENT_LAB_CONFIG`, checkout/config locations, XDG config, and the default user config path. Site-specific values belong there, not in source.
   - `secrets_store.py` retrieves tokens from the configured OS keychain backend (with documented development fallbacks). Secrets must not appear in argv, config, audit records, or committed files.
   - Imports must survive missing or malformed configuration. `cli.py` records configuration errors so `init` and `doctor` can still diagnose and repair the install.

3. **Lease, API, and cleanup flow**
   - A lease begins by ensuring the host is reachable/powered on, snapshots initial resources, and writes lease state under the configured state directory.
   - Mutating API calls require a lease and are restricted to safe guest paths unless an explicit host-change authorization flag is supplied. Resources are registered with the lease; destructive operations require ownership.
   - Lease end cleans up owned resources in dependency-safe order, records failures, and powers off the host only when the shutdown and no-other-lease conditions are satisfied. Shutdown is verified by repeated API failure, not assumed from a request.
   - Ordinary leases expire; long-term leases deliberately pin the host on and use separate protection, release, destroy, and backup semantics.

4. **Guest and protocol channels**
   - `guest.py` probes the guest and prefers qemu-guest-agent for real exit codes, then serial when available; console/VNC is used when the screen is the source of truth.
   - `console.py`, `rfb.py`, `ws.py`, `des.py`, `png.py`, and `textmode.py` implement the screen, serial, WebSocket, RFB, PNG, and opt-in text OCR paths without third-party runtime packages. Long operations use bounded polling/deadlines and explicit timeouts.
   - `storage.py`, `s3.py`, `share.py`, and `share_server.py` handle transfers, backups, and expiring local console links. `netgw.py` creates fail-closed VPN gateway networking.
   - `memflow.py`, `usb.py`, and `netcap.py` are deliberate exceptions to the API-token boundary: they use opt-in SSH access to host-side tooling or disposable LXCs and require their documented authorization gates.

5. **State and audit**
   - Lease and activity state are JSON files under the runtime state directory, protected by the controller lock. The audit journal is append-only SQLite WAL by default (with the configured JSONL/private-sync alternatives) and redacts sensitive fields.
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

For a release, update the version in `pyproject.toml` and `src/proxmox_agent_lab/__init__.py`, update the dated `CHANGELOG.md` section, then run `python3 scripts/check-release.py --tag vX.Y.Z`. The release workflow builds the wheel and sdist, smoke-installs the wheel, and writes SHA-256 checksums.

## Code Conventions & Common Patterns

- Keep runtime code compatible with Python 3.11+ and standard-library-only. Existing modules use `from __future__ import annotations`, type annotations, snake_case names, and small focused helpers.
- Add commands through the existing `cmd_*`/parser and sibling-module registration conventions. Reuse `LabError`, `ConfigError`, API helpers, lease helpers, audit helpers, and shared configuration instead of duplicating them.
- Preserve bounded behavior: use existing timeout/deadline polling for Proxmox tasks, guest operations, network calls, and power transitions. Map expected operational failures to the package's user-facing error path; do not swallow safety failures.
- Treat configuration as process-wide cached state and runtime state as explicit files/databases. Tests may reset caches and patch state roots, but production code must keep locking, atomic writes, expiry, and audit behavior.
- Put guard checks before side effects. Host networking/storage/permissions, USB passthrough, memflow preparation, live memory writes, disk formatting, and similar operations require their documented authorization flags and target verification.
- Protocol tests must assert required client messages, ordering, framing, and side effects. A passive fake that accepts an incomplete protocol is not sufficient.
- Generated guest scripts must be deterministic, escape values correctly, contain no unresolved placeholders, and pass the existing syntax checks.
- No repository formatter, linter, type checker, or task runner is configured. Do not introduce a parallel style/tooling convention without updating project configuration and CI.

## Important Files

- `pyproject.toml` — package metadata, Python requirement, optional dev dependency, Hatchling build, and CLI entry point.
- `src/proxmox_agent_lab/cli.py` — core parser, API client, leases, cleanup, audit, config-error handling, and built-in commands.
- `src/proxmox_agent_lab/config.py` — TOML defaults, config discovery, state directory, and template generation.
- `src/proxmox_agent_lab/secrets_store.py` — secret storage/retrieval backends.
- `src/proxmox_agent_lab/guest.py`, `console.py`, `longterm.py`, `netgw.py`, `storage.py`, `windows.py`, `android.py`, `memflow.py`, `usb.py`, and `netcap.py` — major feature boundaries.
- `tests/fixtures/config.toml` — the only configuration tests should load; it contains deterministic non-site test values.
- `tests/test_proxmox_lab.py`, `test_abstractions.py`, `test_console.py`, and `test_longterm.py` — core lifecycle, abstraction, protocol, and lease-invariant coverage.
- `scripts/check-secrets.py`, `check-public.py`, and `check-release.py` — repository safety and release guards.
- `CONTRIBUTING.md` — authoritative developer setup, required checks, and release checklist.
- `docs/AGENTS.md` and `docs/safety-policy.md` — operational agent workflow and enforced safety invariants; `docs/VERIFICATION.md` separates real-hardware evidence from unit-tested-only behavior.
- `.github/workflows/ci.yml` and `release.yml` — authoritative CI, package smoke test, and release behavior.

## Runtime/Tooling Preferences

- Required runtime: system Python 3.11 or newer; CI covers 3.11–3.14. The package has no runtime dependencies and must work after a normal `pip install` without an extra dependency bundle.
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
