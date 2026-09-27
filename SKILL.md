---
name: proxmox-agent-lab
description: Drive a self-hosted Proxmox lab over root SSH with leased, fail-closed guests and automatic host power on/off. Create, start, probe, and destroy VMs and LXC containers, run commands, move files, and type at or screenshot guest consoles — from the `proxmox-lab` CLI or its stdio MCP server. Use for authorized security research, clean-machine testing, installers, home labs, or spare-PC virtualization.
---

# proxmox-agent-lab

One controller (this machine) drives one Proxmox host over **root SSH** — the
`ssh` binary and your keys/agent are the only transport and the only
credential. All lease, resource, and journal state lives in one local SQLite
file (`lab.db`, under `[state] dir`). The host powers itself on for work and
is verified powered off when the last lease ends. **Nothing runs on the
Proxmox host** except `qm`/`pct`/`pvesh` — plus one optional garbage-collector
cron line (`gc install`). Zero third-party dependencies.

## The host may be in use

The Proxmox host is often **someone's live machine** — the lab is a guest on
it, not a replacement for it. Everything the lab creates is labelled:

```
tags:        pxl;lease-<id>
description: pxl-lease=<id> pxl-expiry=<epoch>
```

Rules that follow from that, and they are not negotiable:

- **Never touch a guest without a `pxl` tag.** Do not destroy, stop, resize
  or reconfigure it, and do not `lease-register` one you were not asked to
  adopt. A refusal here is the design working, not an obstacle.
- **Check VMID and storage before creating.** Use an unused VMID and a pool
  that exists on this host: `guest create --vmid <free> --storage <pool>
  --disk-gb <n>`. A collision destroys a real machine.
- **Do not force a power-off.** `lease-end` powers the host down only when
  nothing is running and two clear checks agree. If it leaves the host up,
  read the `reason` it prints — usually the operator's own guests. Report it;
  do not override it.
- Lab guests are visible in the normal Proxmox web UI under the `pxl` tag.
  Their presence is expected, not a problem to hide.

## Setup (operator, once)

1. `python3 -m pip install proxmox-agent-lab` (Python ≥ 3.11).
2. Trust this machine's key on the host: `ssh-copy-id root@<proxmox>` —
   `<proxmox>` is an ssh alias, hostname, or IP you control as root.
   Prove it: `ssh -o BatchMode=yes root@<proxmox> true && echo ok`.
3. `proxmox-lab init` writes a starter `config.toml` (`--path` to choose,
   `--force` to overwrite) and, if the host answers, discovers the wired NIC's
   WoL MAC for `[power] mac`. Edit four site-specific values:

   ```toml
   [ssh]
   target = "proxmox"        # the ssh alias/host from step 2

   [pve]
   node = "pve"              # the host's short hostname
   template_vmid = 100       # template guests are cloned from

   [power]
   mac = "aa:bb:cc:dd:ee:ff" # wired NIC MAC for Wake-on-LAN
   ```

   The whole file is nine keys — `[power] broadcast`/`port`, `[state] dir`,
   `[lease] ttl_seconds`/`idle_shutdown_seconds` have sane defaults. See
   [docs/CONFIGURATION.md](docs/CONFIGURATION.md).

4. `proxmox-lab doctor` checks everything end to end and exits non-zero on
   failure. Run it whenever anything is unclear — it names the config it
   used, whether root ssh answers, and what is missing.

## Every task is a lease

```bash
L=$(proxmox-lab lease-begin --purpose "<sanitized purpose>" \
    | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
trap 'proxmox-lab lease-end --lease "$L"' EXIT
```

1. `lease-begin` requires the host to answer ssh — it does **not** wake it.
   If it refuses, wake the host first with
   `proxmox-lab power wake --standalone-authorized` (a manual-operation gate),
   then re-run. WoL boot takes a minute or two.
2. Pass `--lease "$L"` to every mutating command. Read-only commands
   (`doctor`, `status`, `journal`, `guest probe`, `guest list`,
   `power status`) take no lease.
3. Work past ~30 minutes: `proxmox-lab lease-heartbeat --lease "$L"`, or the
   lease expires and the GC / `cleanup-expired` sweeps your guests. One lease
   per session, heartbeated — each begin/end cycle costs a host boot.
4. `lease-end` tears down the lease's guests and, when no active lease
   remains, powers the host off with a **verified** probe. It must print
   `"host_powered_off": true`; if not (or it exits non-zero with a
   `cleanup_failed` lease), run `proxmox-lab cleanup-expired --all` and report
   the exact blocker. Never claim done while the host still runs.

Machines that must **survive** use `lease-begin --long-term` — the host then
stays on until `lease-destroy --lease "$L" --confirm`. Only when the user
asked for persistence. See [docs/long-term-leases.md](docs/long-term-leases.md).

## Command surface

| Command | Purpose |
|---|---|
| `proxmox-lab init` | write the starter config; discover the WoL MAC |
| `proxmox-lab doctor` | end-to-end health check (config, ssh, host tools, store) |
| `proxmox-lab journal` | read the audit event log (`--limit --lease --since`) |
| `proxmox-lab status` | host reachability, leases and guests at a glance |
| `proxmox-lab lease-begin --purpose P` | open a lease (`--long-term`, `--ttl`) |
| `proxmox-lab lease-heartbeat --lease L` | renew the lease and guest expiry stamps |
| `proxmox-lab lease-register --lease L --kind K --vmid N` | adopt an existing guest (K = `qemu` or `lxc`; `--policy retain` keeps it) |
| `proxmox-lab lease-list` | active leases |
| `proxmox-lab lease-destroy --lease L --confirm` | forcibly end a lease (the only exit for long-term) |
| `proxmox-lab lease-abandon --lease L --confirm` | close a lease, touching neither guests nor host power |
| `proxmox-lab lease-register --lease L --kind qemu\|lxc --vmid N` | adopt an existing guest (`--policy retain` to keep it) |
| `proxmox-lab cleanup-expired` | sweep expired leases; `--all`, or `--reclaim-orphans` with `--host-change-authorized` |
| `proxmox-lab guest create --lease L --vmid N` | clone the configured template (`--fresh` builds from scratch) |
| `proxmox-lab guest clone --lease L --vmid N --source M` | clone a registry-vouched template |
| `proxmox-lab guest start --lease L --vmid N` / `guest stop` | lifecycle; stop is graceful then hard |
| `proxmox-lab guest destroy --lease L --vmid N --confirm` | irreversible delete of a lease-owned guest |
| `proxmox-lab guest probe --vmid N` | how this guest can be reached (read-only) |
| `proxmox-lab guest list` | registered guests joined with live state |
| `proxmox-lab guest run --lease L --vmid N -- cmd` | run a command inside a guest |
| `proxmox-lab push --lease L --vmid N --file F --dest P` | copy a file in (over ssh) |
| `proxmox-lab pull --lease L --vmid N --remote P --out F` | copy a file out (`--sha256` verifies) |
| `proxmox-lab console screenshot --vmid N` | guest screen to a PNG (`--out`) |
| `proxmox-lab console type --lease L --vmid N --text-stdin` | keystrokes; text is never audited |
| `proxmox-lab console keys --lease L --vmid N KEY...` | QEMU key names (`ret`, `f2`, `ctrl-alt-delete`) |
| `proxmox-lab power status` / `wake` / `shutdown` | manual power; wake/shutdown need `--standalone-authorized` |
| `proxmox-lab gc install` / `status` / `uninstall` | host-side GC cron; install/uninstall need `--host-change-authorized` |
| `proxmox-lab mcp` | serve the MCP tool surface over stdio |

## MCP server

Point an MCP client at `proxmox-lab mcp` — a stdlib-only JSON-RPC 2.0 server
on stdio (newline-delimited messages) exposing these 23 tools. They call the
same functions as the CLI; results are `content[0].text` carrying the CLI JSON
body (screenshots as `png_base64`). Errors are JSON-RPC errors: `-32602` for
schema/`confirm`/key-name violations, `-32603` for a failed action (redacted).
Every call refreshes the idle clock — after `idle_shutdown_seconds` with no
active lease the server performs the verified host shutdown itself.

| Tool | Purpose |
|---|---|
| `lease_begin` | open a lease (`purpose` required; `long_term`, `ttl_seconds`) |
| `lease_heartbeat` | extend a lease and refresh its guests' expiry |
| `lease_end` | close a lease and tear down what it owned |
| `lease_list` | leases (active unless `include_ended`) |
| `lease_destroy` | forcibly destroy a lease and its guests (`confirm`) |
| `lease_register` | adopt an existing guest into a lease |
| `guest_create` | create a lease-owned guest from the template |
| `guest_clone` | clone a vouched template into a lease-owned guest |
| `guest_start` | start a lease-owned guest |
| `guest_stop` | stop a lease-owned guest (graceful, then hard) |
| `guest_destroy` | destroy a lease-owned guest (`confirm`) |
| `guest_probe` | how one guest can be reached (read-only) |
| `guest_list` | registered guests with live state (read-only) |
| `guest_run` | run a command in a lease-owned guest |
| `push_file` | copy a local file into a guest |
| `pull_file` | copy a file out of a guest |
| `console_screenshot` | guest screen as PNG (`png_base64` in result) |
| `console_type` | type at a guest's console (text never audited) |
| `console_keys` | QEMU key names (`ret`, `f2`, ...) |
| `cleanup_expired` | sweep expired leases for a lease (`confirm`; may power off an idle host) |
| `journal_query` | audit events from lab.db (read-only) |
| `doctor` | end-to-end health check (read-only) |
| `power_status` | host reachability and what pins it on (read-only) |

`gc` and standalone `power wake`/`shutdown` are CLI-only: host maintenance and
bare power levers belong to operators, not agents.

## Guest metadata contract

Every lease-owned guest is stamped at create/register time:

- **tags**: `pxl;lease-<lease-id>`
- **description**: `pxl-lease=<lease-id> pxl-expiry=<unix epoch>`
  (`pxl-expiry=0` means long-term — never swept)

Heartbeat rewrites `pxl-expiry`. `doctor` cross-checks this metadata against
`lab.db` and reports drift. The GC cron and `cleanup-expired` trust this
stamp, so **never edit tags or descriptions on `pxl` guests** and never stamp
them yourself — `lease-register` is the way in.

## Safety rules (enforced, not advisory)

- **Lease-owned only.** Every mutation names a lease; only `pxl`-tagged,
  lease-registered guests are ever stopped or destroyed. A guest another
  active lease also registers is refused unless you pass
  `lease-end --shared-guests-authorized` — reach for ending the other lease
  first.
- **Verified shutdown.** Host power-off is confirmed by repeated probe
  failure, never assumed; `host_powered_off: false` means the host is still
  on and you report it.
- **Gates.** Destructive commands take `--confirm` (no interactive prompt).
  `power wake`/`power shutdown` take `--standalone-authorized` (a person owns
  shutdown, not the lease finalizer). `gc install`/`uninstall` and orphan
  reclaim take `--host-change-authorized` (persistent host change).
- **Fails closed.** Missing config → `doctor` reports it and nothing mutates.
  Inconclusive probes are unproven, not passes. Unknown `console keys` names
  are rejected before anything is sent. Remote output and MCP errors are
  redacted before reaching the journal.
- **GC backstop.** `gc install` puts one root crontab line on the host
  (`/usr/local/sbin/pxl-gc`, every 10 min): it destroys guests whose
  `pxl-expiry` has passed and powers the host off when nothing is running —
  the net for agents that walked away.

## Docs

[docs/INSTALL.md](docs/INSTALL.md) — host prep (WoL, ssh) and full setup ·
[docs/CONFIGURATION.md](docs/CONFIGURATION.md) — every config key ·
[docs/AGENTS.md](docs/AGENTS.md) — deep operational guidance ·
[docs/safety-policy.md](docs/safety-policy.md) — enforced invariants ·
[docs/troubleshooting.md](docs/troubleshooting.md) — failure modes ·
[docs/architecture.md](docs/architecture.md) — how it works ·
[docs/VERIFICATION.md](docs/VERIFICATION.md) — what has been exercised on hardware
