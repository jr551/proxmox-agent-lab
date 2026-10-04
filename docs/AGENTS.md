# 🤖 Driving this lab as an agent

> **Audience:** AI agents that already know the lease shape from `SKILL.md`.
> **Scope:** deep operational guidance — the MCP surface, the channel decision,
> console input, hygiene, and honest reporting. For quick-ref copy-paste read
> [`SKILL.md`](../SKILL.md); for the rules enforced in code read
> [safety-policy.md](safety-policy.md).

The lab is one Proxmox host driven over **root SSH** from this controller;
every byte crosses one `ssh` subprocess with a command allowlist. All state —
leases, resource registry, journal — lives in one local SQLite file, `lab.db`
under `[state] dir`. Remote commands stay on the ssh allowlist. The
host-side extras are the GC cron and, for one command, `tcpdump` on a
lease-owned VM's tap.

## 🔑 The one rule

**Every run is a lease, and every lease ends.** The canonical copy-paste
block — `lease-begin`, `trap ... lease-end` on EXIT — lives in
[`SKILL.md`](../SKILL.md#every-task-is-a-lease). Use it verbatim.

Deep notes beyond the quick-ref:

- `lease-begin` **does not wake the host**. It refuses outright when the host
  does not answer ssh, with a message pointing at `power wake`. A dark host is
  therefore an operator step, not an agent one: ask the user to run
  `proxmox-lab power wake --standalone-authorized` (the flag exists precisely
  because bare power has no lease finalizer behind it), then retry. Never hold
  bare power yourself — `power wake`/`power shutdown` are gated for a reason.
- Put `lease-end` in a trap/finally so it runs even when the work fails.
  `"host_powered_off": true` means the host went down.
  `"host_left_running": true` means it stayed up on purpose; quote `reason`
  and stop. A non-zero exit or a `cleanup_failed` lease is the failure to
  report.
- `lease-end` refuses before it touches anything if a guest it would destroy
  is still registered to another non-terminal lease, naming the guest and that
  lease. Do not reach for `--shared-guests-authorized` to get past it: end or
  abandon the other lease, unless the user said that guest is theirs.
- Work lasting more than ~30 minutes needs
  `proxmox-lab lease-heartbeat --lease "$L"` — a heartbeat extends the lease
  *and* rewrites `pxl-expiry` on every registered guest, so neither
  `cleanup-expired` nor the host GC reaps live work.
- One lease per session. Automatic power-off is off unless
  `[power] auto_shutdown` is true. With it on, each begin/end cycle can
  cost a host boot; reuse the lease, don't churn it.

## 🧰 The MCP surface

If your client speaks MCP, point it at `proxmox-lab mcp` (stdio JSON-RPC 2.0).
All 38 tools call the same handlers the CLI binds — same behavior, same gates:

| Group | Tools |
|---|---|
| leases | `lease_begin`, `lease_heartbeat`, `lease_end`, `lease_list`, `lease_destroy`, `lease_register` |
| guests | `guest_nextid`, `guest_create`, `guest_clone`, `guest_media`, `guest_start`, `guest_stop`, `guest_destroy`, `guest_snapshot`, `guest_template`, `guest_probe`, `guest_list`, `guest_run` |
| files | `push_file`, `pull_file` |
| console | `console_screenshot`, `console_type`, `console_keys`, `console_move`, `console_click`, `console_drag`, `console_calibrate`, `console_grid`, `console_burst` |
| hygiene & health | `cleanup_expired`, `journal_query`, `doctor`, `status`, `power_status`, `storage_status`, `storage_content`, `network_bridges`, `net_capture` |

Notes that matter:

- `lease_destroy`, `guest_destroy`, `guest_template` and `cleanup_expired`
  require `"confirm": true` — missing or false fails `-32602` before anything
  runs. `guest_snapshot` requires it for `delete` and `rollback` only.
  Pass it only when the user asked for that destruction.
- Guest-scoped tools take `lease_id` + `vmid`; read-only tools (`guest_probe`,
  `guest_list`, `guest_nextid`, `journal_query`, `doctor`, `status`,
  `power_status`, `storage_status`, `storage_content`, `network_bridges`)
  need no lease.
- Results arrive as `content[0].text` carrying the same JSON the CLI prints;
  `console_screenshot` returns the PNG inline as `png_base64`.
- Every call refreshes the idle clock and records name + ok + target only —
  after `idle_shutdown_seconds` (default 8h) with no active lease the server
  powers the host off itself. Going silent is not a way to keep the host up.
- No `gc`, no `memflow`, and no standalone `power wake`/`shutdown` tools
  exist. Host maintenance, live memory access and bare power are CLI work.

## 👀 Choosing how to talk to a guest

Ask first:

```bash
proxmox-lab guest probe --vmid 9001
```

Probe reports `exists`, `running`, `kind`, `agent_ok`, `ip` and `channel`
(`"agent"` | `"pct"` | `"ssh"`). Then:

| The guest is... | Use | Why |
|---|---|---|
| qemu with guest agent answering | `guest run` | real exit code, separated stdout/stderr |
| lxc (running) | `guest run` | `pct exec` is native — no agent needed |
| qemu without agent | `console type`/`keys` + `console screenshot` | you drive the emulated keyboard, then *look* |

`guest run` picks `qm guest exec` or `pct exec` by kind automatically and
times out at 300s unless `--timeout` says otherwise. Only argv0 and the exit
code are audited — the command text never is.

**Prefer text over pixels.** Output you can grep beats a screenshot of a
terminal. **But do look when a screen is the truth** — a stuck boot, a GUI
installer, a login prompt. `console screenshot --vmid <id> --out ./shot.png`
writes a PNG (QEMU only — LXC guests have no `qm monitor` and report
`supported: false`).

`keys_sent`/`sent` counts what the controller transmitted, not what the guest
received. `console keys --screenshot-after 3` bundles proof: it waits, then
returns a capture. If the screen did not change, stop and re-read instead of
sending more input — that is how a guest gets driven blind for an hour.

Input to a guest the lease does not own is refused *before* anything is sent;
the error names the `lease-register` command that fixes it. Read it rather
than retrying.

## 📦 Creating and feeding guests

Before `guest create`, size the machine from the host's free resources.

```bash
proxmox-lab guest nextid     # next free VMID; pass it to guest create
proxmox-lab network bridges  # bridges, so you are not stuck on vmbr0
proxmox-lab storage status   # each store's avail, in bytes
proxmox-lab storage content  # volid for --iso, --ostemplate, guest media
proxmox-lab status           # memory.free, memory.total, cpu_count
```

`guest nextid` is a read. Create does not pick an id. A VMID collision
destroys a real machine. Over MCP the same numbers are `guest_nextid`,
`network_bridges`, `storage_status`, `storage_content`, and `status`.

`storage status` is the disk figure. `status` is the RAM and CPU figure.
Pick a disk and a memory size the task needs, and leave headroom. A small
test guest stays small: a few GB of disk and a gigabyte or two of RAM is
enough to boot and look. Leave the rest of the store. Leave the host
enough RAM to keep itself and its other guests running. Memory is your
judgment — a large share of the host's RAM is still your call to make,
so make it with headroom left.

`guest create` refuses a disk larger than the free space on that store
before it creates anything. The error names the store and the free
amount. Read it and ask for less.

```bash
proxmox-lab guest create --lease "$L" --vmid 9001 --name probe --start
proxmox-lab guest clone --lease "$L" --vmid 9002 --source 100
```

`guest create` clones the configured `[pve] template_vmid` only when that
guest is a template (`template: 1`). A normal VM is refused. `--fresh` builds
from scratch (LXC fresh needs `--ostemplate`). A fresh qemu create passes
`--agent 1` so `guest run` and `guest probe` have a channel; that does not
install the agent inside the guest. A clone keeps the source's setting.
`guest clone` accepts any
*vouched* source — a config template (`template: 1`) or a `policy=retain`
registry row. Either way the guest is registered to your lease and stamped
`proxmoxagentlab` metadata **before** it can ever be started.

Adopt a pre-existing guest — e.g. one created in the Proxmox UI — with
`proxmox-lab lease-register --lease "$L" --kind qemu --vmid <id>`; without that
row every mutating command refuses it. Use `--policy retain` for a guest that
should outlive the lease (it becomes a clone source, never driven or
destroyed by lease teardown).

Files move with `push`/`pull`, chunked base64 through the same guest-exec
channel as `guest run` — so they need the agent on qemu or a running lxc.
`--sha256` verifies end to end; a mismatched `pull` leaves no file behind.

## 🧹 Hygiene and your own audit trail

- `proxmox-lab journal --limit 20` — what you just did, as recorded. The audit
  is redacted by construction: typed text and command strings never appear.
- `proxmox-lab cleanup-expired` — sweep any expired leases (yours or a dead
  session's). A no-op sweep writes no event, so the journal stays signal.
- `proxmox-lab gc install --host-change-authorized` — the host-side net:
  root cron every 10 minutes reaps guests whose `pxl-expiry` passed and powers
  the host off after two consecutive clear runs. Install it once when the
  user asks; `proxmox-lab gc status` verifies script, checksum and crontab.
- Long-term work: `lease-begin --long-term` pins the host on and stamps
  `pxl-expiry=0` — never swept. Exit is `lease-destroy --confirm`. Use only
  when the user asked for persistence.

## 🩺 When something fails

[troubleshooting.md](troubleshooting.md) is the symptom-to-command guide.
Work down this list before concluding the tool is broken:

```bash
proxmox-lab doctor        # config, ssh answer, host tooling, lab.db, GC cron
proxmox-lab status        # host reachable? leases? vmids on the node?
proxmox-lab guest probe --vmid <id>
proxmox-lab journal --limit 20
```

## 📢 Reporting honestly

- If `lease-end` leaves the host up because other guests are running, quote
  `reason` and stop. That is a finished lease, not a failed one, and it is
  not a reason to power the host off. A non-zero exit, a reason that
  power-off could not be verified, or a `cleanup_failed` lease is a failure:
  say so. Same for `power shutdown` exiting non-zero.
- Distinguish *inconclusive* from *negative*. `agent_ok: false` from probe is
  a fact; a screenshot you never took is not. `doctor` marks unreachable-host
  checks `skipped`, and skips never count as failures.
- Quote the output that supports your claim — `"host_powered_off": true`, an
  exit code, a `sha256` that matched.
- If you skipped part of the task, name the part.

## 🛑 Things that need explicit permission

The tool refuses these unless you pass a flag (or `confirm: true` over MCP),
and you should not pass it unless the user asked for that specific change.
Canonical command+flag pairs live in [safety-policy.md](safety-policy.md) —
do not invent flags not listed there.

- `guest destroy`, `lease-destroy`, `lease-abandon` → `--confirm`
- ending a lease that shares a guest with another live lease →
  `--shared-guests-authorized` (prefer ending the other lease)
- `cleanup-expired --reclaim-orphans` / `--orphans-only` →
  `--host-change-authorized` (`--include-active` overrides the in-use signals)
- `gc install`/`uninstall` → `--host-change-authorized`
- `memflow host-setup` → `--host-change-authorized` (`--print` previews the
  script and changes nothing). `memflow write` / `phys-write` →
  `--i-understand`, and only when the user asked to patch that guest's RAM
- `power wake`/`power shutdown` → `--standalone-authorized`, and these are
  *operator* levers: agents work through leases.
- deleting a guest the lease does not own → not possible, refused outright.

Before anything destructive, **look at the target**: `guest list`, `status`,
a screenshot. Report what you found.

## 📝 A worked example

Bring up a VM, check something, tear it down:

```bash
set -e
L=$(proxmox-lab lease-begin --purpose "check systemd unit ordering" \
    | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
trap 'proxmox-lab lease-end --lease "$L"' EXIT

proxmox-lab guest clone --lease "$L" --vmid 9001 --source 100 --name probe
proxmox-lab guest start --lease "$L" --vmid 9001
proxmox-lab guest probe --vmid 9001
proxmox-lab guest run --lease "$L" --vmid 9001 -- systemd-analyze critical-chain
```

The `trap` is the important line. Everything else is detail.
