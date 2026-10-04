---
name: proxmox-agent-lab
description: Drive a self-hosted Proxmox lab over root SSH with leased, fail-closed guests. Host power-off is optional. Create, start, probe, and destroy VMs and LXC containers, run commands, move files, and type at or screenshot guest consoles — from the `proxmox-lab` CLI or its stdio MCP server. Use for authorized security research, clean-machine testing, installers, home labs, or spare-PC virtualization.
---

# proxmox-agent-lab

One controller (this machine) drives one Proxmox host over **root SSH** — the
`ssh` binary and your keys/agent are the only transport and the only
credential. All lease, resource, and journal state lives in one local SQLite
file (`lab.db`, under `[state] dir`). Host power-off is optional
(`[power] auto_shutdown`, default false). **Nothing runs on the
Proxmox host** except `qm`/`pct`/`pvesh` — plus one optional garbage-collector
cron line (`gc install`). Zero third-party dependencies.

## The host may be in use

The Proxmox host is often **someone's live machine** — the lab is a guest on
it, not a replacement for it. Everything the lab creates is labelled:

```
tags:        proxmoxagentlab;<controller-hostname>;lease-<id>
description: pxl-lease=<id> pxl-expiry=<epoch>
```

Rules that follow from that, and they are not negotiable:

- **Never touch a guest without a `proxmoxagentlab` tag.** Do not destroy,
  stop, resize or reconfigure it, and do not `lease-register` one you were
  not asked to adopt. A refusal here is the design working, not an obstacle.
- **Check VMID and storage before creating.** Use an unused VMID and a pool
  that exists on this host: `guest create --vmid <free> --storage <pool>
  --disk-gb <n>`. A collision destroys a real machine.
- **Do not force a power-off.** `lease-end` powers the host down only when
  nothing is running and two clear checks agree. If it leaves the host up,
  read the `reason` it prints — usually the operator's own guests. Report it;
  do not override it.
- Lab guests are visible in the normal Proxmox web UI under the
  `proxmoxagentlab` tag, with the creating machine's hostname beside it.
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

   `[power] broadcast`/`port`/`auto_shutdown`, `[state] dir`, and
   `[lease] ttl_seconds`/`idle_shutdown_seconds` have sane defaults.
   `auto_shutdown` is false: the host stays up unless you turn it on. See
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
   per session, heartbeated. With `auto_shutdown` on, each begin/end cycle
   can cost a host boot.
4. `lease-end` destroys this lease's guests. It does **not** power the host
   off unless `[power] auto_shutdown` is true. With the default, it prints
   `"host_left_running": true` and a `reason`; that is success. Report it
   and stop. Do not power the host off yourself. When auto-shutdown is on,
   `"host_powered_off": true` means it went down, and a host left up because
   other guests are running is still success — quote `reason`. A non-zero
   exit, a `cleanup_failed` lease, or a reason that power-off could not be
   verified is a real failure: run `proxmox-lab cleanup-expired --all` and
   report the blocker.

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
| `proxmox-lab cleanup-expired` | sweep expired leases; `--all`, or `--reclaim-orphans` with `--host-change-authorized` |
| `proxmox-lab guest create --lease L --vmid N` | clone `[pve] template_vmid` when it is `template: 1` (`--fresh` builds from scratch; `--iso local:iso/name.iso` boots that CD; a normal VM is refused) |
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
| `proxmox-lab console move --lease L --vmid N --x X --y Y` | move the pointer, no click — safe for probing a layout |
| `proxmox-lab console click --lease L --vmid N --x X --y Y` | click (`--button`, `--double`, `--screenshot-after`) |
| `proxmox-lab console drag --lease L --vmid N --x X --y Y --to-x A --to-y B` | press, drag, release (`--steps` interpolates) |
| `proxmox-lab console grid --vmid N` | screenshot with a labelled coordinate grid burned in |
| `proxmox-lab console burst --vmid N --frames 3` | several frames in one call, stitched |
| `proxmox-lab console calibrate --vmid N --action start` | lay markers to measure this client's image scaling |
| `proxmox-lab power status` / `wake` / `shutdown` | manual power; wake/shutdown need `--standalone-authorized` |
| `proxmox-lab gc install` / `status` / `uninstall` | host-side GC cron; install/uninstall need `--host-change-authorized` |
| `proxmox-lab guest snapshot list\|create\|delete\|rollback` | snapshots of a lease-owned guest; delete and rollback need `--confirm`, rollback needs the guest stopped |
| `proxmox-lab guest template --lease L --vmid N --confirm` | turn a stopped lease-owned guest into a template. Teardown will not destroy it afterwards |
| `proxmox-lab storage status` | free space on each store (read-only) |
| `proxmox-lab netcap capture --lease L --vmid N --out cap.pcap` | pcap of that running qemu VM's tap only (`--nic net0`, `--seconds`, `--filter`). No `--iface`. TLS stays ciphertext |
| `proxmox-lab memflow …` | read a lease-owned qemu guest from the hypervisor; see below |
| `proxmox-lab mcp` | serve the MCP tool surface over stdio |

## MCP server

Point an MCP client at `proxmox-lab mcp` — a stdlib-only JSON-RPC 2.0 server
on stdio (newline-delimited messages) exposing these 33 tools. They call the
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
| `guest_snapshot` | list/create/delete/rollback snapshots (`confirm` on delete and rollback) |
| `guest_template` | turn a stopped lease-owned guest into a template (`confirm`) |
| `guest_run` | run a command in a lease-owned guest |
| `push_file` | copy a local file into a guest |
| `pull_file` | copy a file out of a guest |
| `console_screenshot` | guest screen as PNG (`png_base64` in result) |
| `console_type` | type at a guest's console (text never audited) |
| `console_keys` | QEMU key names (`ret`, `f2`, ...) |
| `console_move` | move the guest pointer without clicking |
| `console_click` | click a point (`button`, `double`, `space`, `client`) |
| `console_drag` | press, drag, release between two points |
| `console_calibrate` | measure this client's image scaling (`action`, `samples`) |
| `console_grid` | screenshot with a coordinate grid burned in |
| `console_burst` | capture several frames in one call, stitched |
| `cleanup_expired` | sweep expired leases (`confirm`; may power off an idle host) |
| `journal_query` | audit events from lab.db (read-only) |
| `doctor` | end-to-end health check (read-only) |
| `power_status` | host reachability and what pins it on (read-only) |
| `storage_status` | free space on each store (read-only) |
| `net_capture` | pcap of one lease-owned running qemu VM's tap (`out` path; TLS stays ciphertext) |

### Clicking: which coordinates are you reading?

This is the one thing to get right, because the failure is silent: a click
lands *near* the target rather than on it, and the guest does something else
entirely.

- **Default (`--space framebuffer`)** — the numbers are real guest pixels. Use
  this when reading coordinates off `console grid`, or a screenshot whose
  `width`/`height` match the framebuffer the tool reported.
- **`--space image`** — the numbers came off a *downscaled* image (what your
  viewer actually shows you). This **requires a saved calibration** and is
  refused without one. Calibrate first:

  ```bash
  # 1. lay a numbered marker grid, get the annotated screenshot back
  proxmox-lab console calibrate --vmid N --action start --client "My IDE"
  # 2. read where each marker landed IN THE IMAGE YOU WERE SHOWN, then
  proxmox-lab console calibrate --vmid N --action commit \
      --client "My IDE" --samples '[{"id":"M1","x":144,"y":81}, ...]'
  # 3. now --space image works and maps your coordinates for you
  proxmox-lab console click --lease L --vmid N --x 144 --y 81 \
      --space image --client "My IDE"
  ```

  Read the markers off the image, do not guess them — a guessed reading
  produces a confidently wrong transform. `--action submit` shows the fit and
  its error without saving, so you can check before committing.

If the guest's resolution changes, the saved calibration reads `STALE` and
calibrated clicks are refused. Re-calibrate; never fall back to
`--space framebuffer` carrying image-space numbers.

Pointer input is a mutation like any other: lease-gated, and it drives the
QEMU HID tablet over the same ssh seam. Nothing is installed in the guest.

`gc`, `memflow`, and standalone `power wake`/`shutdown` are CLI-only: host
maintenance, live memory access and bare power levers belong to operators,
not the MCP surface.

## Guest metadata contract

Every lease-owned guest is stamped at create/register time:

- **tags**: `proxmoxagentlab;<controller-hostname>;lease-<lease-id>`
  (e.g. `proxmoxagentlab;mac;lease-2026…` on a Mac)
- **description**: `pxl-lease=<lease-id> pxl-expiry=<unix epoch>`
  (`pxl-expiry=0` means long-term — never swept)

Heartbeat rewrites `pxl-expiry`. `doctor` cross-checks this metadata against
`lab.db` and reports drift. The GC cron and `cleanup-expired` trust this
stamp, so **never edit tags or descriptions on `proxmoxagentlab` guests** and never stamp
them yourself — `lease-register` is the way in.

## Safety rules (enforced, not advisory)

- **Lease-owned only.** Every mutation names a lease; only `proxmoxagentlab`-tagged,
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

## Memory, from underneath

`memflow` reads a running qemu guest from the hypervisor rather than from
inside the guest. Every such command names a lease that owns that guest; an
unregistered or stopped guest is refused before the helper runs. The journal
records that a read happened (address, length, counts), never the bytes.

```bash
proxmox-lab memflow host-setup --print
proxmox-lab memflow processes --lease "$L" --vmid 9001
proxmox-lab memflow boot-diagnose --lease "$L" --vmid 9001
```

`host-setup` installs the helper on the host and needs
`--host-change-authorized`. `write` and `phys-write` change live RAM and need
`--i-understand`, and only when the user asked to patch that guest. Full
command list: [docs/memflow.md](docs/memflow.md).

## Docs

[docs/INSTALL.md](docs/INSTALL.md) — host prep (WoL, ssh) and full setup ·
[docs/CONFIGURATION.md](docs/CONFIGURATION.md) — every config key ·
[docs/AGENTS.md](docs/AGENTS.md) — deep operational guidance ·
[docs/safety-policy.md](docs/safety-policy.md) — enforced invariants ·
[docs/troubleshooting.md](docs/troubleshooting.md) — failure modes ·
[docs/architecture.md](docs/architecture.md) — how it works ·
[docs/VERIFICATION.md](docs/VERIFICATION.md) — what has been exercised on hardware ·
[docs/memflow.md](docs/memflow.md) — agentless memory reads
