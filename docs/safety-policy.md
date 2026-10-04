# Lease and shutdown policy

## Purpose

Every mutation belongs to a lease, every lab guest carries `proxmoxagentlab` metadata the
host-side garbage collector can trust, and host power-off is verified by
repeated probe failure — so abandoned work cannot linger, unowned guests are
never touched, and destructive or host-changing actions need explicit,
narrowly-scoped authorization.

## Commands

Authoritative flags verified against `src/proxmox_agent_lab/cli.py` and the
`guest`, `console`, `gc`, `memflow`, `cleanup` and `power` registrations.

| Command | Authorization flag | Scope |
|---|---|---|
| `proxmox-lab guest destroy --lease <id> --vmid <n> --confirm` | `--confirm` | irreversible delete of one lease-owned guest |
| `proxmox-lab lease-destroy --lease <id> --confirm` | `--confirm` | destroy a lease and its machines; the only exit for a long-term lease |
| `proxmox-lab lease-abandon --lease <id> --confirm` | `--confirm` | close a stopped lease record; guests and host untouched |
| `proxmox-lab lease-end --lease <id> --shared-guests-authorized` | `--shared-guests-authorized` | destroy a guest another non-terminal lease also registers |
| `proxmox-lab cleanup-expired --reclaim-orphans --host-change-authorized` | both flags | stop (never delete) `proxmoxagentlab` guests this controller has no record of |
| `proxmox-lab cleanup-expired --orphans-only --host-change-authorized` | both flags | reclaim orphans and nothing else — no lease finalized, host left as found |
| `proxmox-lab cleanup-expired --reclaim-orphans --host-change-authorized --include-active` | `--include-active` on top | also stop an orphan whose 30-minute task/uptime/CPU signals say it is in use |
| `proxmox-lab gc install` / `proxmox-lab gc uninstall` | `--host-change-authorized` | write or remove the root GC script, its state dir and one crontab line on the host |
| `proxmox-lab memflow host-setup` | `--host-change-authorized` | install the memflow helper on the host; `--print` previews the script and changes nothing |
| `proxmox-lab memflow write` / `memflow phys-write` | `--lease` and `--i-understand` | patch live RAM of a lease-owned running qemu guest; the bytes are not audited |
| `proxmox-lab power wake` / `proxmox-lab power shutdown` | `--standalone-authorized` | bare host power outside any lease — a person, not the lease finalizer, owns shutdown |

The same gates exist on the MCP surface as data, not flags: `lease_destroy`,
`guest_destroy`, `guest_template` and `cleanup_expired` must carry `"confirm": true`
(`guest_snapshot` requires it for `delete` and `rollback`); a missing
or false value fails `-32602` before anything runs. The server exposes no `gc`, no `memflow`, and no standalone
`power wake`/`shutdown` tool. Host maintenance, live memory access and bare
power stay on the CLI.

## Invariants

1. Every mutation belongs to one lease in a non-terminal state (`active`,
   `ending`, `cleanup_failed`). `store.resources` is the registry; the
   ownership gate refuses before any host call and names the `lease-register`
   command that would authorize the guest.
2. Every lease-owned guest is stamped at create/register time: tags
   `proxmoxagentlab;<controller-hostname>;lease-<id>` and a `pxl-lease=<id> pxl-expiry=<unix epoch>` line in its
   description. `lease-heartbeat` rewrites the expiry on every registered
   guest; `pxl-expiry=0` means long-term, never swept.
3. Cleanup destroys only `disposable` resources registered to that lease, in
   reverse registration order. It is idempotent — an already-gone guest is
   success — and any failure lands the lease in `cleanup_failed`, which every
   later `cleanup-expired` sweep retries.
4. `retain`-policy resources and templates (`template: 1`) are never stopped
   or destroyed: they are clone sources and read-only surface. `guest destroy`
   additionally refuses a readable guest config that carries no `proxmoxagentlab` tag —
   a machine somebody else owns can never be mistaken for ours.
5. Automatic host power-off is off unless `[power] auto_shutdown` is true.
   `lease-end`, the idle sweep and the host GC then leave the host up.
   `power shutdown --standalone-authorized` is the explicit path and ignores
   that switch. When a shutdown does run, it is verified, never assumed. After `shutdown -h now` the host
   is probed alternately over ssh and TCP :22; `host_powered_off` is `true`
   only after at least six consecutive all-fail rounds spanning at least 30
   seconds. A timeout reports `host_powered_off: false` and exits non-zero.
   There is no force-off path: a host that refuses to die is reported, not
   killed.
6. The host is never powered off while any guest is running (guests tagged
   `codex-lab-infra` are the documented exception), nor while any lease is
   still non-terminal, nor while a long-term lease exists.
7. Destructive and host-changing operations need the explicit flag in the
   table above. There is no interactive prompt anywhere.
8. Audit redacts before insert and never fails the action: an unrecordable
   event is a stderr warning, not a raised error.
9. Every MCP `tools/call` — read-only or mutating, success or failure —
   refreshes the idle clock and records only tool name, `ok` and target
   (vmid/lease). No argument value is ever recorded by that path.
10. The MCP server is a second power-off net: `idle_shutdown_seconds` with no
    active lease triggers the same verified shutdown `cleanup-expired` uses,
    via a self-wake that fires even when the client has gone silent.
11. `console type` audits the character count only; `console keys` audits the
    key count only (key names can spell typed content); `guest run` audits
    argv0 and the exit code, never the full command or its output; `push` and
    `pull` record the remote path, byte count and digest, never the payload.
12. The host-side GC fails closed: unreadable config, unparseable metadata or
    an internal error on one guest is logged and skipped, never deleted.
13. Least privilege is a command allowlist, not a credential scope (§below).
    Root ssh is the trust boundary; the allowlist is the policy inside it.

## The ssh allowlist — the least-privilege boundary

Every byte to and from the host crosses `ssh.py`, the only module that spawns
`ssh`. The rules, enforced before any process is spawned:

- `argv[0]` must be in the allowlist: `qm`, `pct`, `pvesh`, `pveversion`,
  `hostname`, `ip`, `cat`, `base64`, `true` for ordinary calls; `shutdown`,
  `crontab`, `install`, `ethtool`, `tee`, `rm` additionally need
  `host_change=True`, plumbed from the CLI authorization flags — with the one
  read-only exception `crontab -l`. Anything else, an arbitrary root shell
  included, is refused as a `PolicyError` before spawn. The one extra shape
  is passive VM capture: `timeout --signal=TERM <1-120> tcpdump -n -i
  tap<vmid>i<n> -w - -U`, optional `-c` and a short BPF expression. The
  interface must be that guest tap. A bridge, a physical NIC, a veth, or
  `-w` to a host file is refused before spawn. Which VM may be named is
  still a lease check above this seam.
- The remote argv is `shlex.quote`d word-by-word and joined; `;`, `$(...)` and
  spaces arrive as literal argument text, never as shell syntax.
- `cat` and `tee` are confined to `/tmp/pxl-*`; `rm` to `/tmp/pxl-*` and
  `/usr/local/sbin/pxl-*`; `base64` may read `/tmp/pxl-*`,
  `/usr/local/sbin/pxl-*` and `/var/log/pxl-*`. A path containing `..`
  anywhere is refused outright, and the path is normalized before the prefix
  test, so the seam cannot read, write or remove host files outside the pxl
  namespaces however the path is spelled.
- `BatchMode=yes` and `ConnectTimeout=5` are always set: the transport can
  never hang on a prompt, and every call is timeout-bounded.
- The spawn happens as `ssh <target> '<quoted argv>'` — your ssh config, keys
  and agent decide what `<target>` means. No credential of any kind is handled
  by this tool.

## Audit and redaction

Every action appends one event row to `lab.db`'s `events` table (the legacy
DDL, reused verbatim). The `data` column carries `{actor, tool, ok, target,
…fields}` with every value redacted *before* insert: keys that smell like
credentials (passwords, keys, auth material) become `[REDACTED]`, strings are
capped at 1000 characters. A failure to record warns on stderr and the action
proceeds — auditing must never break the work it observes. `journal` reads
this table (`--limit`, `--lease`, `--since`); there is no upload step because
there is no central ledger — the store is local.

## What `proxmoxagentlab` metadata proves, and what owns a guest

A guest this tool created is tagged `proxmoxagentlab;<controller-hostname>;lease-<id>`
and carries `pxl-lease=<id> pxl-expiry=<epoch>` in its description. The stamp outlives the
lease record that explains it — lease rows end; guest metadata does not — so
it is evidence that *some* lease on *some* controller created the guest, and
nothing more. Ownership comes from `lab.db`, the only registry: a
`(kind, vmid)` must be registered to the supplied lease, live, before any
mutating call. A `proxmoxagentlab`-tagged guest whose lease id no local lease row owns
is an **orphan**. The pre-rename `pxl` tag is still honoured everywhere the
new one is, so a guest stamped before the rename is never orphaned by it.

An orphan matters because two rules interact deliberately: cleanup only
finalizes resources a lease registered, and host power-off refuses while
*any* guest runs — so one running orphan is invisible to every sweep and pins
the machine on indefinitely. Reclamation is explicit and stops, never
deletes:

```bash
proxmox-lab status            # every vmid on the node vs. `guest list` (registered)
proxmox-lab cleanup-expired --orphans-only --host-change-authorized
```

`--orphans-only` does exactly that and nothing else. `--reclaim-orphans`
folds reclamation into a full expiry sweep — finalizing every expired lease
and possibly powering the host off — a much larger intention, so a separate
flag.

### "Orphaned" does not mean "abandoned"

It means *this* `lab.db` has no record. Another controller drives guests
through the same root ssh channel, and its lease records are not here — a
running orphan may be somebody else's live work. Reclamation leaves a guest
alone if **any** of three signals says it is in use: a non-stop task for it
in the last 30 minutes (stop tasks excluded, or our own stop would veto every
later run); uptime under 30 minutes; CPU at or above 10%. An unreadable task
list counts as in use — not knowing must not resolve to stopping someone's
work. `--include-active` overrides all three; pass it only when you have
proven the orphan is not live work elsewhere. The measured load is reported
either way, so disagree with the floor using numbers, not trust.

## Graceful finalization

Per disposable resource, in reverse registration order:

1. skip it if already stamped `destroyed_at` (idempotency);
2. leave it, reported `left_to_another_lease`, if another *live* lease
   registers the same guest — an expired claim shields nothing;
3. graceful shutdown (`qm shutdown`/`pct shutdown`), then a hard stop only if
   the grace period expires;
4. destroy the stopped guest; QEMU destroys purge the disks;
5. a failure lands the lease in `cleanup_failed` with `last_error`, retried
   by every later sweep.

`lease-end` refuses *before* touching anything when a guest it would destroy
is registered to another non-terminal lease (recorded as
`lease-end-refused-shared-guest`): end or abandon the other lease first, or
re-run with `--shared-guests-authorized` only when the user has said that
guest is theirs to delete. `lease-destroy` on a long-term lease first rewrites
each guest's `pxl-expiry` to the past, so a half-failed teardown is reaped by
the GC instead of pinning the machine on for ever. `lease-abandon` verifies
every registered guest is stopped (or already gone), then closes only the
record: no guest mutation, no host mutation.

## The host-side GC — fail closed by construction

`proxmox-lab gc install --host-change-authorized` puts exactly one thing on
the host: `/usr/local/sbin/pxl-gc` (mode 0755, sha256-checked against the
bundled copy), a `/var/lib/pxl-gc` state dir, and one root crontab line
running it every ten minutes. The script is standalone — stdlib only, no
controller database — the net for agents that walked away:

- a guest without the `proxmoxagentlab` tag (or pre-rename `pxl`) is skipped; so is a template, an unreadable
  config, and metadata that does not parse — a warning, never a delete;
- `pxl-expiry=0` (long-term) and unexpired guests pin the host on;
- an expired guest gets graceful shutdown, then a hard stop, then destroy
  (`--purge` for QEMU), under a per-vmid flock so overlapping runs never race;
- power-off needs two *consecutive* clear runs ≥600 s apart — zero running
  and zero pinned; anything running or pinned removes the clear stamp at once.

`gc status` reports script presence, checksum, crontab line and the log tail
(read-only, no authorization). `gc uninstall --host-change-authorized` removes
the crontab line first, then the script; guests are never touched on the way
out.

## Credentials

SSH keys are the whole story. There is no credential store in this tool at
all — no backend to configure, nothing to fetch or rotate. `[ssh] target` is
an ssh alias or host you control as root, trusted once with `ssh-copy-id`;
BatchMode means the transport physically cannot prompt. The config file holds
no secret material, and none should ever be written there.

## Safety gate — refuse or stop

Every gate above resolves the same way: refuse before the host call, name the
authorizing flag or `lease-register` in the error, and record the refusal.
`host_powered_off: false` is reported loudly and never rounded up to success;
an inconclusive probe is unproven, never a pass.

## See also

- [AGENTS.md](AGENTS.md) — how an agent should drive this surface
- [troubleshooting.md](troubleshooting.md) — symptom-to-command guide
- [CONFIGURATION.md](CONFIGURATION.md) — the config file keys
- [commands.md](commands.md) — generated command reference
- [VERIFICATION.md](VERIFICATION.md) — what has been exercised on hardware
