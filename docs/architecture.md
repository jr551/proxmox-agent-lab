# Architecture

How `src/proxmox_agent_lab/` is arranged, and the three contracts that keep
the pieces interchangeable: the `lab` facade every feature module receives,
the single SSH seam every remote action goes through, and the lease/audit
rules every mutation obeys.

## Layers

The dependency direction is always downward: command layers call services;
services never call back up. `ssh.py` and `store.py` are the bottom — each
owns the only resource of its kind (the `ssh` subprocess, the `lab.db`
SQLite file).

```text
cli.py                      parser, policy gates, the lab facade
mcp.py                      stdio JSON-RPC 2.0 server, binds the SAME handlers
  |
  +-- feature modules       guest, console, transfer, gc
  |   (register(sub, lab))
  |
  +-- lifecycle             leases, cleanup, power
  |
  +-- host channels         proxmox (qm/pct/pvesh wrappers) -> ssh (allowlist)
  |
  +-- state services        store (lab.db), audit, journal, config,
  |                         diagnostics, errors, state, png
  |
  +-- host-side             resources/pxl-gc.py — standalone script, imports
                            nothing from the package
```

Two rules hold this shape:

- `ssh.py` is the only module that spawns `ssh`; every remote action —
  including `qm guest`/`pct exec` file IO — is one allowlisted argv through
  it. Nothing opens its own channel.
- `leases` and `cleanup` communicate through `store.py` and the guest
  metadata contract (`proxmoxagentlab` tags,
  `pxl-lease=`/`pxl-expiry=` description line), never by importing each other's handlers.

## The `lab` facade

`cli.py` loads configuration once, keeps the values other modules share as
module attributes (`CONFIG`, `STATE_ROOT`, `LEASE_ROOT`, `ssh`, ...), and
hands *itself* to feature modules:

```python
def register(sub, lab):        # called from cli.parser()
    from .cli import _bind
    p = sub.add_parser("feature")
    p.set_defaults(func=_bind(lab, cmd_feature))
```

`_bind(lab, fn)` returns `lambda args: fn(lab, args)`, so argparse only ever
sees one-argument callables. Inside a feature module, `lab.X` reaches the
shared configuration and the lifecycle/state helpers.

This is deliberate, not incidental:

- Tests patch `cli` attributes (`mock.patch.object(LAB, "STATE_ROOT", ...)`);
  because every helper reads them through `lab` at call time, patching keeps
  working after a function moves to its own module.
- `mcp.py` binds the same `cmd_*` handlers the CLI binds, so the 34-tool MCP
  surface and the CLI cannot drift. `memflow` is CLI-only.
- `cli._module()` rebuilds the module object (and the `proxmox_lab`
  compatibility name) when the file is path-loaded outside `sys.modules`.

Do not add a second dispatch architecture or a dependency-injection
framework on top of this.

## The lower layer takes explicit parameters

`errors`, `state`, `store`, `audit`, `journal` and `config` are pure
services: they take the configuration, state paths and connections they need
as arguments. The `cli` module binds them to the process-wide config through
thin wrappers, so the patch surface is unchanged but the services themselves
hold no import-time snapshots.

In `leases`, the *data* functions (`load_lease`, `save_lease`,
`active_leases`, `register_resource`, ...) take the lease/state roots
explicitly for the same reason. The *handlers* (`cmd_lease_begin`, ...) take
`lab`.

`cleanup` orchestrates teardown entirely through `lab`/`store`: it reaches
lease data via `lab.active_leases()` and friends rather than importing the
orchestration side of `leases`, so the two lifecycle modules never import
each other's handlers.

## Safety invariants that must survive any refactor

- Every mutation belongs to a lease; `leases.require_lease_resource` /
  `guest.require_owned` gate guest writes against the `resources` table
  *before* any remote call.
- `ssh.check_allowed` refuses any remote command outside `ALLOWED_COMMANDS`
  before a process spawns; `HOST_CHANGE_COMMANDS` additionally need
  `host_change=True`, which the CLI gates behind `--host-change-authorized`.
  The memflow helper is the same kind of exception as `qm`: one absolute
  path (`/usr/local/bin/pxl-memflow-run`) with a fixed argument shape, and
  its write subcommands need `memory_write=True`. Host setup runs only
  `/usr/local/sbin/pxl-memflow-setup` with `host_change=True`.
- `lease-begin` does not wake the host: it probes the ssh seam and refuses
  on an unreachable host, directing the operator to
  `power wake --standalone-authorized`.
- `cleanup.finalize_lease` is idempotent and records failures; a lease left
  `cleanup_failed` is retried by every later sweep.
- Host power-off is verified by repeated ssh/TCP :22 probe failure, never
  assumed. The MCP idle sweep and the host-side `pxl-gc` cron are the two
  nets that guarantee the host eventually goes off.
- `audit.audit` redacts before insert into `events.data` and never fails the
  action being audited.
- Importing any module must survive missing config so `init`/`doctor` still
  diagnose a broken install.

## Patch-target rule for tests

Patch the module that owns the code under test. `cli.*` names work for
everything re-exported through the facade (`cli.ssh`, `cli.CONFIG`,
`cli.active_leases()`, ...), but internals must be patched at their new
home — e.g. the fake seam goes in at `proxmox_agent_lab.ssh`/`proxmox`
injection points, not an imagined `api.py`. Tests use
`tests/support/fakessh.py` (argv-recording FakeSSH) and never spawn a real
ssh.

## Shared helpers

Retry and subprocess loops stay local to the caller that owns their policy.
The genuinely shared pieces already have homes — `ssh.py` (the one remote
runner + allowlist), `proxmox.py` (task-wait with bounded deadlines),
`store.py` (all persistence). Do not extract further "generic" utilities: a
retry or query policy is a behavior contract, and two loops that look alike
but answer different failure modes should stay separate until a third caller
proves the shape.
