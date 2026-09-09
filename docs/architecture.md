# Architecture

How `src/proxmox_agent_lab/` is arranged, and the two contracts that keep the
pieces interchangeable: the `lab` facade every feature module receives, and
the lease/audit rules every mutation goes through.

## Layers

The dependency direction is always downward: command handlers may use
transports and state services; transports and state services never call back
up into handlers.

```text
cli.py                      parser, command policy gates, the lab facade
  |
  +-- feature modules       android, console, disk, diskactivity, guest,
  |   (register(sub, lab))  isoinspect, longterm, memflow, netcap, netgw,
  |                         oci, onboarding, pe, recipes, share, storage,
  |                         usb, virtio, windows
  |
  +-- lifecycle             leases, cleanup, diagnostics
  |
  +-- guest channels        transfer -> guest_agent -> serial -> ws
  |                         console  -> serial / guest_agent / transfer
  |                         host_transport (host SSH for the opt-in modules)
  |
  +-- state services        state, audit, updates, config, inventory,
  |                         journal, mariadb, secrets_store, host_policy,
  |                         power, errors
  |
  +-- protocols             api (HTTPS client), rfb, ws, png, des, s3,
                            textmode, binparse
```

Two rules hold this shape:

- `host_transport` does not import `memflow`; `memflow`, `usb`, `netcap`,
  `disk`, `diskactivity` and `console` use it for host-side SSH.
- `serial`, `guest_agent` and `transfer` do not import `console`; console is
  the handler layer above them.

## The `lab` facade

`cli.py` loads configuration once, keeps the values other modules share as
module attributes (`HOST`, `NODE`, `STATE_ROOT`, `LEASE_ROOT`, `audit`,
`ProxmoxAPI`, ...), and hands *itself* to feature modules:

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
- Third-party or experimental modules can register subcommands against the
  same contract.
- The historical `proxmox_lab` path-loaded module is rebuilt by
  `cli._module()` for callers that still import the old name.

Do not add a second dispatch architecture or a dependency-injection
framework on top of this.

## The lower layer takes explicit parameters

`errors`, `state`, `api`, `audit` and `updates` are pure services: they take
the configuration, state paths and credentials they need as arguments. The
`cli` module binds them to the process-wide config through thin wrappers, so
the patch surface is unchanged but the services themselves hold no
import-time snapshots.

In `leases`, the *data* functions (`load_lease`, `save_lease`,
`active_leases`, `register_resource`, ...) take the lease/state roots
explicitly for the same reason. The *handlers* (`cmd_lease_begin`, ...) take
`lab`.

`cleanup` orchestrates teardown entirely through `lab`: it reaches lease data
via `lab.active_leases()` and friends rather than importing the
orchestration side of `leases`, so the two lifecycle modules never import
each other's handlers.

## Safety invariants that must survive any refactor

- Every mutation belongs to a lease; `leases.require_lease_resource` /
  `require_owned_qemu` gate guest writes.
- `cleanup.finalize_lease` is idempotent and records failures; a lease left
  `cleanup_failed` is retried by every later sweep.
- Host power-off is verified by repeated API failure, never assumed.
- `audit.audit` redacts secrets and never fails the action being audited;
  events spool locally while the ledger is unreachable.
- Importing any module must survive missing config or dependencies so
  `init`/`doctor` still diagnose a broken install.

## Legacy aliases and the patch surface

These names are deliberately re-exported so callers and tests keep working:

| Kept on | Alias for |
| --- | --- |
| `cli.LabError` | `errors.LabError` |
| `cli.utc_now` / `iso_now` / `json_dump` | `state.*` |
| `cli.controller_lock` / `sweep_lock` / `_lock_file` | `state.*` (bound to `STATE_ROOT`/`LOCK_PATH`) |
| `cli.ProxmoxAPI` / `wait_task` / `keychain_secret` | `api.*` (config-bound subclass) |
| `cli.audit` / `ledger` / `redact` / `SENSITIVE_KEY` | `audit.*` |
| `cli.check_for_updates` / `update_notice` / `UPDATE_CHECK_*` | `updates.*` |
| `cli.load_lease` / `save_lease` / `active_leases` / ... | `leases.*` (bound roots) |
| `cli.finalize_lease` / `shutdown_host` / `*_guest` helpers | `cleanup.*` |
| `cli.cmd_init` / `cmd_doctor` / `cmd_journal` / ... | `diagnostics.*` |
| `cli.fcntl` | `state.fcntl` (None on Windows) |
| `console.TermSession` / `TermFilter` / `_open_websocket` | `serial.*` |
| `console.agent_exec` / `exec_guest` / `write_guest_file` / ... | `guest_agent.*` |
| `console.cmd_push` / `cmd_pull` / `cmd_s3` internals | `transfer.*` |

Support decision: these aliases are *load-bearing compatibility*, not
deprecated shims — they are how the test suite and any third-party
`register(sub, lab)` callers reach the implementation. Removing one is a
breaking change; if an alias is ever dropped, the removal is announced in the
release notes first. New code should prefer the owning module.

Patch-target rule for tests: patch the module that owns the code under test.
`cli.*` names still work for everything re-exported through the facade, but
internals that were moved (e.g. `transfer.agent_exec`, `state.fcntl`,
`cleanup.time`) must be patched at their new home.

## Shared helpers

Retry and subprocess loops stay local to the caller that owns their policy.
The two genuinely shared wait loops already have homes — `api.wait_task`
(Proxmox task polling) and `guest_agent.wait_agent_ready` (agent polling) —
and `host_transport` owns the host-SSH runner. Do not extract further
"generic retry" utilities: a retry policy is a behavior contract, and two
loops that look alike but answer different failure modes should stay separate
until a third caller proves the shape.
