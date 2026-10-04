# Repository Guidelines

> **Audience:** Contributors and maintainers editing `src/proxmox_agent_lab/`, `tests/`, and automation in `.github/`/`scripts/`.
> **Scope:** Project overview, architecture, key directories, dev commands, and code conventions. For lab operation, see `SKILL.md` (lease quick-ref) and `docs/AGENTS.md` (deep operational guidance).

## Project Overview

`proxmox-agent-lab` is a Python package and agent skill for operating a disposable Proxmox research lab. It is a slim SSH-only control plane: `ssh-copy-id root@proxmox` is the whole credential story, `qm`/`pct`/`pvesh` run over one SSH seam, one local SQLite `lab.db` holds leases, resources and the audit journal, and `proxmox-lab mcp` serves the same operations as a 33-tool MCP surface. The `proxmox-lab` CLI takes a lease, creates and operates lease-owned VMs/LXCs, transfers files and drives the console, then destroys lease-owned resources and verifies the host powered off.

Use it only for systems the operator owns or is authorized to test. The safety model is part of the product: leases, ownership checks, expiry, audit redaction, the remote-command allowlist, explicit host-change gates, and verified shutdown must remain intact.

## Architecture & Data Flow

`docs/architecture.md` is the module map and the contracts below are summarized here; that page is the detail. Dependency direction is strictly downward: command layers call services, `proxmox.py` calls `ssh.py`, and `ssh.py`/`store.py` call nothing above them.

1. **CLI facade and command registration**
   - `src/proxmox_agent_lab/cli.py` owns configuration loading, parser construction, command policy gates, and the `lab` facade every feature module receives. `__main__.py` and the installed `proxmox-lab` entry point call `cli.main()`.
   - Feature modules register subcommands with `register(sub, lab)` and reach shared state through the `lab` facade (`cli._bind` binds one-argument callables for argparse). Do not create a second command-dispatch architecture.
   - `mcp.py` is a stdlib-only stdio JSON-RPC 2.0 server (`initialize`, `tools/list`, `tools/call`, 33 tools, no MCP SDK) started by `proxmox-lab mcp`. It binds the *same* `cmd_*` handlers the CLI binds, so the two surfaces cannot drift. `memflow` stays CLI-only.

2. **The one SSH seam**
   - `ssh.py` is the only module that spawns `ssh`: every remote action is a single argv over `ssh -o BatchMode=yes root@<target>`, argv-recordable and timeout-bounded. Arguments are `shlex.quote`d at the seam; no caller composes remote shell strings.
   - `check_allowed` is the least-privilege boundary: a remote command outside the `qm`/`pct`/`pvesh`/host-tooling `ALLOWED_COMMANDS` set is refused before any process spawns, and the `HOST_CHANGE_COMMANDS` subset (`shutdown`, `crontab`, `install`, `ethtool`, `tee`, `rm`) additionally requires `host_change=True`, which the CLI gates behind `--host-change-authorized`.
   - `proxmox.py` wraps `qm`/`pct`/`pvesh`/`pveversion` plus bounded task-wait; it is the only consumer-facing layer above the seam.

3. **The one SQLite store**
   - `store.py` owns `<state dir>/lab.db` (stdlib `sqlite3`, WAL + `busy_timeout=5000`): the `leases`, `resources` and `events` tables and nothing else. The `events` DDL reuses the legacy `journal.db` schema verbatim; richer fields live in the `data` JSON column. There are no JSON lease files, no MariaDB, no spool, no migration machinery — `lab.db` is a fresh start.

4. **Leases and cleanup**
   - `leases.py` owns lease policy: begin/heartbeat/register/list/destroy, expiry, and long-term semantics — long-term is `kind='long_term'` with `expires_at=0` and guest metadata `pxl-expiry=0`, not a separate subsystem.
   - `lease-begin` does *not* wake the host: it probes the SSH seam and **refuses** on an unreachable host, telling the operator to run `power wake --standalone-authorized` (or fix the network) first. A lease is only opened when the host can be governed.
   - `cleanup.py` owns teardown: `finalize_lease`, `lease-end`, `cleanup-expired`, shared-guest cross-reference refusal. It is idempotent; `cleanup_failed` leases are retried by every later sweep. The two lifecycle modules communicate through `store.py`, never by importing each other's handlers.
   - Ownership proof is dual: the `resources` table registers every lease-owned guest, and the guest itself carries `pxl`/`lease-<id>` tags plus a `pxl-lease=<id> pxl-expiry=<epoch>` description line — the durable copy the host-side GC trusts.

5. **Power and the host-side GC**
   - `power.py` sends the WoL magic packet over stdlib UDP and implements verified shutdown: `shutdown -h now`, then repeated ssh/TCP :22 probe failure — power-off is a proven fact, never assumed. Standalone `power wake`/`power shutdown` are refused without `--standalone-authorized`.
   - `resources/pxl-gc.py` is a standalone stdlib-only script (no package imports) installed to `/usr/local/sbin/pxl-gc` plus one root crontab line by `gc.py` (`gc install|status|uninstall`, gated `--host-change-authorized` except `status`). It reaps expired-lease pxl-tagged guests and powers the host off when clear — it reads guest metadata, never the controller's database.

6. **Guest channels**
   - `guest.py` covers create/clone/start/stop/destroy/run/probe/list through `proxmox.py`; `transfer.py` pushes/pulls files via chunked base64 over guest exec; `console.py` takes screenshots (`qm monitor` screendump → PPM → PNG) and sends keystrokes (`qm sendkey`). Every mutation goes through `guest.require_owned` against the store before any remote call.
   - `png.py` is the stdlib PNG writer/reader/resampler (plus PPM→PNG for screendumps).

7. **State, audit, config**
   - `audit.py` appends one redacted event row per action into `lab.db` and never fails the action being audited; `journal.py` is the read side.
   - `config.py` loads the single TOML schema (`[ssh]`, `[pve]`, `[power]`, `[state]`, `[lease]`) from `PROXMOX_AGENT_LAB_CONFIG` or `~/.config/proxmox-agent-lab/config.toml`. No config key holds a credential — SSH keys are the entire secret surface.
   - `diagnostics.py` is `init`/`doctor`/`status`/`journal` and must work on a broken install; `errors.py` holds `LabError`/`ConfigError`. `state.py` retains atomic-file/lock/timestamp helpers (superseded for lease data by `store.py` — audit remaining callers before relying on it).
   - Never put runtime state, journals, captures, or site topology in the repository.

## Key Directories

- `src/proxmox_agent_lab/` — installable package and all CLI/subsystem modules.
- `src/proxmox_agent_lab/resources/pxl-gc.py` — the host-side GC script bundled in the wheel.
- `tests/` — deterministic `unittest` suite, `tests/support/` fakes (`fakessh.py`, `bootstrap.py`), and `tests/fixtures/config.toml`.
- `docs/` — installation, configuration, operational agent guidance, safety policy, subsystem behavior, architecture, generated command map, and hardware-verification notes. `docs/rework-*.md` are historical planning docs.
- `scripts/` — checkout CLI wrapper (`proxmox-lab`), the `check` gate runner, secret/public/docs guards, `gen-commands.py`, and release metadata validation.
- `.github/workflows/` — CI and tag-gated release workflows.
- `agents/`, `.agents/` — agent integration metadata and the bundled skill (`.agents/skills/proxmox-agent-lab/SKILL.md` is a symlink to root `SKILL.md` — edit the root file, never the link target).

## Development Commands

No third-party dependency is needed at runtime or for the test suite — work from the checkout:

```bash
scripts/proxmox-lab --help          # checkout runner; pins a 3.11+ interpreter
PYTHONPATH=src python3 -m proxmox_agent_lab --help   # equivalent
```

Run the canonical gates (what `scripts/check` and CI run):

```bash
PYTHONWARNINGS=error python3 -m unittest discover -s tests -q
python3 -m compileall -q src tests
python3 scripts/check-secrets.py .
python3 scripts/check-public.py .
python3 scripts/check-release.py
python3 scripts/check-docs.py
python3 scripts/gen-commands.py --check
git diff --check
# Also run bash -n on every changed shell script.
# `scripts/check` runs this whole set; `scripts/check --fast` is guards only.
```

The suite is exercised here on Python 3.13 (`python3 --version`); the code must remain compatible with Python 3.11+. An optional venv (`pip install -e '.[dev]'`) adds `pytest` tooling, but the canonical suite is direct `unittest` discovery.

Build and smoke-test distribution artifacts as CI does:

```bash
python3 -m pip install --disable-pip-version-check build
python3 -m build
python3 -m venv /tmp/proxmox-agent-lab-smoke
/tmp/proxmox-agent-lab-smoke/bin/pip install --no-deps dist/*.whl
PROXMOX_AGENT_LAB_CONFIG=/tmp/missing.toml \
  /tmp/proxmox-agent-lab-smoke/bin/proxmox-lab --help
```

For a release, update the version in `pyproject.toml` and `src/proxmox_agent_lab/__init__.py`, move the `CHANGELOG.md` `Unreleased` notes into a dated `## X.Y.Z` section, then run `python3 scripts/check-release.py --tag vX.Y.Z`. The release workflow builds the wheel and sdist, smoke-installs the wheel, and writes SHA-256 checksums.

## Code Conventions & Common Patterns

- Runtime code is **standard library only** and Python 3.11+ compatible. `pyproject.toml` declares zero dependencies — adding one is a rework-level decision, not a drive-by.
- Existing modules use `from __future__ import annotations`, type annotations, snake_case names, and small focused helpers.
- Add commands through the existing `register(sub, lab)`/`_bind` conventions and `cmd_*` handlers. Reuse `LabError`, `ConfigError`, the `ssh.py` seam, `proxmox.py` wrappers, lease helpers, and audit instead of duplicating them. `ssh.PolicyError` is the allowlist refusal.
- Preserve bounded behavior: use existing timeout/deadline polling for Proxmox tasks, guest operations, network calls, and power transitions. Map expected operational failures to the package's user-facing error path (`cli.main` prints `LabError` without a traceback); do not swallow safety failures.
- Put guard checks before side effects: ownership checks against the store (`guest.require_owned`, `leases.require_lease_resource`) run before any remote call, and host-changing commands take their authorization flag before the seam is touched.
- Lease/pxl invariants: every mutation belongs to a lease; guest `pxl-lease`/`pxl-expiry` metadata is written at creation and refreshed on every heartbeat; a lease left `cleanup_failed` stays non-terminal so sweeps retry it.
- Treat configuration as process-wide cached state and `lab.db` as the only runtime state. Tests may reset caches and point the state root at temp dirs, but production code must keep WAL/busy-timeout, atomicity, expiry, and redaction behavior.
- No repository formatter, linter, type checker, or task runner is configured. Do not introduce a parallel style/tooling convention without updating project configuration and CI.
- Never commit or push without being asked. Never commit credentials, private keys, host addresses, MAC addresses, VMIDs, disk serials, captures, or runtime journals.

## Important Files

- `pyproject.toml` — package metadata, Python ≥3.11, Hatchling build, zero dependencies, CLI entry point.
- `src/proxmox_agent_lab/cli.py` — parser, command gates, the `lab` facade, and thin wrappers over `errors`, `config`, `ssh`, `proxmox`, `store`, `leases`, `cleanup`, `power`, `audit`, `journal`, `diagnostics`.
- `src/proxmox_agent_lab/ssh.py` / `proxmox.py` — the one remote channel and the `qm`/`pct`/`pvesh` wrappers on it.
- `src/proxmox_agent_lab/store.py` — `lab.db` schema and queries.
- `src/proxmox_agent_lab/leases.py`, `cleanup.py`, `power.py`, `gc.py`, `mcp.py`, `guest.py`, `console.py`, `transfer.py`, `diagnostics.py` — major feature boundaries.
- `src/proxmox_agent_lab/resources/pxl-gc.py` — standalone host-side GC; imports nothing from the package.
- `tests/fixtures/config.toml` — the only configuration tests should load; deterministic non-site values (a few transitional keys exist only for dying code paths — do not extend them).
- `tests/support/fakessh.py` — argv-recording FakeSSH double; `tests/test_pxl_gc.py` stubs `qm`/`pct` on `PATH` for the real GC script.
- `scripts/check-secrets.py`, `check-public.py`, `check-release.py`, `check-docs.py`, `check` — repository safety and doc/release guards.
- `CONTRIBUTING.md` — developer setup, required checks, and pull-request rules.
- `docs/AGENTS.md` and `docs/safety-policy.md` — operational agent workflow and enforced safety invariants; `docs/architecture.md` documents the module map and the `register(sub, lab)`/`_bind` contract; `docs/VERIFICATION.md` separates real-hardware evidence from unit-tested-only behavior.
- `.github/workflows/ci.yml` and `release.yml` — authoritative CI, package smoke test, and release behavior.

## Runtime/Tooling Preferences

- Required runtime: system Python 3.11 or newer, **stdlib only**. Imports and `--help` must survive a missing config so broken installs remain diagnosable.
- Build backend: Hatchling. The optional `.[dev]` extra provides `pytest`, but the canonical suite is direct `unittest` discovery.
- `scripts/proxmox-lab` is the preferred checkout runner; it deliberately picks the newest installed 3.11+ interpreter. Installed users use the same `proxmox-lab` command from PATH.
- Host-side requirements are SSH root access and `qm`/`pct`/`pvesh` on the Proxmox host — the Python package installs nothing on the hypervisor except the optional `pxl-gc` script/cron via `gc install`.
- Keep imports safe on broken installs and keep `init`/`doctor` usable when configuration is absent.

## Testing & QA

- Tests use `unittest.TestCase` and `python -m unittest discover -s tests`; there is no configured coverage threshold.
- Tests must never spawn a real ssh: `tests/support/fakessh.py` records argv lists and scripts outputs/failures, injected or monkeypatched in place of the seam. `tests/support/bootstrap.py` sets `PROXMOX_AGENT_LAB_CONFIG` to the fixture and a per-process temp state root before imports.
- Test negative paths and guards, not only success: lease ownership and expiry, `lease-begin` refusal on an unreachable host, long-term `pxl-expiry=0` protection, host-change authorization, the ssh command allowlist, shared-guest cross-references, `confirm` gates on destructive MCP tools, secret redaction, and verified shutdown.
- Use active fakes that record writes and assert argv construction, quoting, allowlist refusal, and call ordering. A passive fake that accepts an incomplete protocol is not sufficient.
- Keep warning-clean tests (`PYTHONWARNINGS=error`), compile source and tests, run the secret/public/release/docs guards, and syntax-check changed shell scripts.
- Hardware-facing changes must update `docs/VERIFICATION.md` with exactly what was observed and what remains unit-tested only. Do not claim real hardware validation from offline tests.
- Before committing, do not include credentials, private keys, presigned URLs, host addresses, MAC addresses, VMIDs, disk serials, captures, site notes, or runtime journals. Keep `scripts/check-secrets.py` strict; add an allowlist entry only for a genuinely public constant with an explanation.
