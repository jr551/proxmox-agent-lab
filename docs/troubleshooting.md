# Troubleshooting

Work through symptoms in order. Each row names a diagnostic command and the
next decision; an inconclusive result is unproven, not a pass — say what to
try before concluding the tool is broken.

## Setup and reachability

| Symptom | Diagnostic | Next step |
|---|---|---|
| Nothing works, or unsure whether the lab is set up | `proxmox-lab doctor` | It prints `{name, ok, detail}` per check and exits non-zero on real failures. A `config` failure points at the file it wanted — run `proxmox-lab init`, then edit `[ssh] target`, `[pve] node`/`template_vmid`, `[power] mac`. |
| `ssh_connect` fails: host answers but auth is refused | `ssh root@<target> true` by hand | The transport runs `ssh -o BatchMode=yes`, so it *cannot* prompt: if a password is needed, the key was never installed. Run `ssh-copy-id root@<target>` once, then re-run `doctor`. Agent-based auth works too — `ssh` reads your config and agent; the tool adds nothing. |
| `ssh_connect` fails: timeout, connection refused | `ssh <target> true` by hand; `proxmox-lab status` | The host is down, off the network, or the `[ssh] target` name is wrong. A powered-off host is normal — `status` says so. Wake it (operator step): `proxmox-lab power wake --standalone-authorized`. |
| `lease-begin` refuses: "host is not reachable" | `proxmox-lab power status` | By design — a lease cannot be opened against a host that cannot be governed. Wake the host first (`power wake --standalone-authorized`), then re-run `lease-begin`. |
| `power wake` sends the packet but the host never answers | `proxmox-lab doctor` (the `wol_mac` check); re-run `proxmox-lab init` | Check `[power] mac` (init discovers it when ssh works), `broadcast` and `port`; confirm WoL is enabled in the host's BIOS and NIC. A `--timeout` below 90 s is refused — cold boot genuinely takes a minute or two. |
| `doctor` passes but commands time out mid-run | re-run `proxmox-lab doctor`; check `ssh -o ConnectTimeout=5 <target> true` | Every remote call is timeout-bounded (30 s default) — a hang is the host or the network, not the tool. If the host went away mid-command, it may be shutting down (see GC below). |

## `doctor` check-by-check

| Check fails or warns | What it means | Fix |
|---|---|---|
| `python_version` | interpreter older than 3.11 | run under Python ≥ 3.11 |
| `config` | no file at the reported path, or the file could not be parsed | `proxmox-lab init` writes a starter; a parse error names the offending line — fix the TOML |
| `ssh_target` | `[ssh] target` empty | set it to the ssh alias/host you control as root |
| `ssh_connect` | BatchMode ssh to the target failed or timed out | key not installed (`ssh-copy-id root@<target>`), host down, or wrong target — try `ssh <target> true` by hand for the real error |
| `remote_tooling` | `qm`/`pct`/`pvesh`/`pveversion` missing or erroring on the host | the target is not a Proxmox node, or these tools are broken there — check by hand |
| `node_identity` | warning only: `[pve] node` disagrees with `hostname -s` | set `[pve] node` to the host's short hostname |
| `state_dir` | `[state] dir` cannot be created/opened, or `lab.db` schema mismatch | fix the path/permissions; a schema-version mismatch means `lab.db` was written by a different build — move it aside (it is disposable state) |
| `template_vmid` | warning only: unset, or no guest resolves it | set `[pve] template_vmid` to the vmid of a template, or pass `--template`/`--fresh`/`--source` explicitly |
| `wol_mac` | warning only: `[power] mac` empty | `power wake` cannot work without it; re-run `proxmox-lab init` while ssh works, or fill it in by hand |
| `gc_cron` | info only: no pxl-gc crontab on the host | optional backstop — install with `proxmox-lab gc install --host-change-authorized` |
| `drift` | warning only: `proxmoxagentlab` guests whose metadata disagrees with `lab.db` | see "ownership metadata" below; reconcile with `lease-register` or leave it to the GC |

Skipped remote checks (`host unreachable`) never fail the run — `doctor` is
safe to run while the host is off.

## lab.db and the journal

| Symptom | Diagnostic | Next step |
|---|---|---|
| `database is locked` / busy errors | check for another `proxmox-lab` process (a still-running `lease-end`, `cleanup-expired`, or the MCP server) | `lab.db` runs WAL + a 5 s busy timeout: brief contention resolves itself — retry. A persistent lock means another process holds a write transaction; let it finish or kill it. |
| schema-version error opening `lab.db` | `proxmox-lab doctor` (the `state_dir` check) | the file was written by a different build. It is disposable state — move `<state dir>/lab.db` aside and re-create it; `doctor` reopens it clean. |
| Journal looks empty or sparse | `proxmox-lab journal --limit 50` | expected: a no-op `cleanup-expired` sweep writes nothing, and read-only MCP calls record only name/ok/target. Typed text, `guest run` command strings and file paths are never in there by design. |
| Need to see what a past session did | `proxmox-lab journal --lease <id>` or `--since <iso>` | events are rows in `lab.db` — no upload step, no separate spool. |

## Leases and power

| Symptom | Diagnostic | Next step |
|---|---|---|
| Lease expired mid-work | `proxmox-lab lease-list`; `proxmox-lab journal --limit 20` | If the lease is still `active` (no sweep ran yet), `lease-heartbeat --lease <id>` revives it and rewrites guest expiry. If `cleanup-expired` or the GC already finalized it, its guests are gone — `lease-begin` again and rebuild. |
| `lease-list` shows a lease stuck in `ending` | `proxmox-lab journal --limit 20` | A finalizer claimed the lease and died mid-teardown. No sweep retries `ending` — that is what `cleanup_failed` is for. Reclaim it by hand: `sqlite3 <state dir>/lab.db "UPDATE leases SET state='cleanup_failed' WHERE id='<id>' AND state='ending'"`, then `proxmox-lab cleanup-expired --all`. |
| `lease-end` prints `state: ending`, "another actor is already finalizing" | wait a few seconds, `proxmox-lab lease-list` | a concurrent `lease-end`/`cleanup-expired` owns the teardown — this is the claim working, not a failure. If it stays `ending`, see the row above. |
| `lease-end` exits non-zero / no `host_powered_off: true` | the printed `reason` field; `proxmox-lab journal --limit 20` | Two distinct cases: `failures` non-empty → the lease is `cleanup_failed` and every later sweep retries — run `proxmox-lab cleanup-expired --all`. Guests running outside any lease → it refuses to pull power; stop or register them (the output lists the vmids). |
| `power shutdown` reports the host did not power off | `proxmox-lab journal --limit 20` | Verified shutdown requires ≥6 consecutive all-fail probe rounds (ssh *and* TCP :22) spanning ≥30 s. A timeout means the host is *still answering* — something kept it up: a running guest, a hung shutdown. There is no force-off path; investigate on the host. |
| `lease-end` refuses a long-term lease | `proxmox-lab lease-list` | expected. Exit is `proxmox-lab lease-destroy --lease <id> --confirm` — it rewrites `pxl-expiry` to the past first, so a half-failed teardown is still reaped by the GC. |
| `lease-end` refuses: shared guest | `proxmox-lab lease-list` | another non-terminal lease registers a guest this one would destroy. End or abandon the other lease; `--shared-guests-authorized` only when the user said that guest is disposable. |
| Host stays on with no leases | `proxmox-lab status`; `proxmox-lab journal --limit 20` | Something is pinning it: guests still running (`status` lists vmids — compare with `guest list`'s registered set), a long-term lease, or an MCP/idle sweep that could not verify power-off. The journal names the reason (`lab-power-off-*` events). |

## The GC cron

| Symptom | Diagnostic | Next step |
|---|---|---|
| Expired guests never reaped, host never powers off | `proxmox-lab gc status` | `crontab: absent` → the GC was never installed: `proxmox-lab gc install --host-change-authorized`. `checksum: differs` → re-run install to update the script. The `log` field tails `/var/log/pxl-gc.log` — read it for what each run decided. |
| GC log says `skip <vmid>: not ours (no ownership tag)` / `unparseable ... not deleting` | `qm config <vmid>` / `pct config <vmid>` on the host | Fail-closed is the design: no `proxmoxagentlab` (or pre-rename `pxl`) tag or a malformed `pxl-lease=… pxl-expiry=…` line means *never* delete. If the guest is lab work, adopt it (`lease-register`); if it should be reaped, the stamp is wrong — fix via `qm set <vmid> --description` or destroy it by hand. |
| GC never powers off although everything looks idle | `proxmox-lab gc status` (log tail) | Power-off needs two consecutive runs ≥10 min apart with zero running guests and zero *unexpired* `proxmoxagentlab` guests — one expired-but-still-present guest, or a `pxl-expiry=0` long-term guest, keeps the host up. Any running guest removes the clear stamp and the two-run wait restarts. |
| Guest stopped by "nothing" | `/var/log/pxl-gc.log` via `gc status`; `proxmox-lab journal` | If `pxl-expiry` passed — the lease went stale and heartbeats stopped — the GC reaped it. That is the lease contract working; keep heartbeats under `[lease] ttl_seconds` (default 2 h). |

## Guests and console

| Symptom | Diagnostic | Next step |
|---|---|---|
| `guest run` fails on a qemu guest | `proxmox-lab guest probe --vmid <id>` | `agent_ok: false` means qemu-guest-agent is not answering — use `console type`/`console keys` + `console screenshot`, or install/start the agent. For lxc the channel is `pct exec`, no agent needed. |
| `console screenshot` reports `supported: false` | probe | LXC guests have no `qm monitor`; there is no frame to capture. |
| `console type`/`keys` succeed but nothing happens on screen | `proxmox-lab console keys --lease "$L" --vmid <id> <key> --screenshot-after 3` | `qm sendkey` drives the emulated keyboard — if the guest's display is a serial console, keystrokes land nowhere visible. The bundled screenshot tells you whether the screen moved; if not, stop and re-read instead of typing more. |
| `push`/`pull` fail | `proxmox-lab guest probe --vmid <id>` | Transfers ride the guest-exec channel: same requirements as `guest run` (agent on qemu, running container on lxc). `--sha256` on `pull` deletes the local file on mismatch — a bad pull leaves nothing behind. |
| `guest destroy`/`guest stop`/`guest run` refuse "not a registered guest" | `proxmox-lab guest list` | The guest has no `resources` row for your lease — adopt it first with `proxmox-lab lease-register --lease "$L" --kind qemu --vmid <id>`, or confirm you are passing the right lease/vmid. Read the refusal; it prints the exact command. |
| `guest clone` refuses the source | `proxmox-lab guest list` | The source must be vouched — a config template (`template: 1`) or a `retain`-policy registry row. Cloning a random guest's disk is deliberately not a thing. |

## MCP server

| Symptom | Diagnostic | Next step |
|---|---|---|
| Tool call returns `-32602` | the error message names the offending field | Schema violation, unknown tool, missing/false `confirm` on `lease_destroy`/`guest_destroy`/`cleanup_expired`, or an unknown `console_keys` key name. The message names the field, never echoes the value. |
| Tool call returns `-32603` | `proxmox-lab journal --limit 20` | The action itself failed; the message is redacted (no commands, no text). The journal event has the same tool name with `ok: false`. |
| Server seems to do nothing when idle | stderr of the `proxmox-lab mcp` process | All diagnostics go to stderr — stdout is protocol bytes only. The ~60 s self-wake drives the idle-shutdown check even with a silent client. |
| Host powered off while an MCP session was idle | `proxmox-lab journal --limit 20` | `mcp-idle-shutdown-triggered` is the second power-off net: `idle_shutdown_seconds` (default 8 h) with no active lease. That is the fail-safe working, not a bug — begin a new lease. |

## General recovery checklist

When nothing above fits:

1. `proxmox-lab doctor`
2. `proxmox-lab status`
3. `proxmox-lab guest probe --vmid <id>`
4. `proxmox-lab journal --limit 20`
5. `ssh <target> true` by hand — the transport is literally `ssh`; whatever it
   tells you is the truth the tool is seeing.

Quote the command output that supports your claim. Distinguish *inconclusive*
from *negative*: a probe that returned nothing is not proof of safety.

## See also

- [AGENTS.md](AGENTS.md) — agent operational guidance
- [safety-policy.md](safety-policy.md) — enforced invariants
- [CONFIGURATION.md](CONFIGURATION.md) — every config key
- [commands.md](commands.md) — generated command reference
