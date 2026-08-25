# 📌 Long-term leases

## Purpose

When work must survive the host powering off — a build box, an always-on service, debugging that spans weeks — a long-term lease keeps the host up and its guests protected and backed up, until you explicitly destroy or release it.

## Commands

All flags verified against `src/proxmox_agent_lab/longterm.py:register()` and `src/proxmox_agent_lab/cli.py` / `guest.py`.

| Command | Key flags (verified) | Notes |
|---|---|---|
| `lease-begin --purpose "..." --long-term` | `--long-term`, `--purpose` | host stays on; never expires |
| `lease-list` / `status` | — | shows `kind: long-term`, `host_pinned_on` |
| `lease-destroy --lease ID --confirm` | `--lease`, `--confirm` | lifts Proxmox `protection`, stops & deletes lease guests |
| `lease-release --lease ID --confirm` | `--lease`, `--confirm` | removes protection, finalizes lease, leaves retained guests stopped, may power off host |
| `guest retain --vmid ID [--purpose ...] [--lease ID] [--forget]` | `--vmid`, `--purpose`, `--forget` | records a deliberately-kept guest in `retained.json` |
| `backup [--force] [--keep N] [--storage ID] [--interval-days N]` | `--force`, `--keep`, `--interval-days`, `--storage` | weekly `vzdump --mode snapshot` |
| `backup --retained --force` | `--retained`, `--force` | one-off retained-registry backup |
| `lease-end --lease ID` | `--lease` | refuses a long-term lease; points at `lease-destroy` |
| `lease-end --lease ID --shared-guests-authorized` | `--shared-guests-authorized` | only for cross-registered guests with expired claimants |

Quick examples:

```bash
proxmox-lab lease-begin --long-term --purpose "persistent build box"
proxmox-lab lease-list
proxmox-lab backup --force
proxmox-lab lease-destroy --lease <id> --confirm
```

An ordinary lease promises that **everything disappears**. A long-term lease makes the opposite promise, for the machines you want to keep — a build box, a always-on service, something you are still debugging next week.

```bash
proxmox-lab lease-begin --long-term --purpose "persistent build box"
```

## ⚠️ What changes

Three things, and the first one costs money:

| | Ordinary lease | Long-term lease |
|---|---|---|
| 🔌 **The host** | Powered off when the last lease ends | **Stays on**, permanently |
| ⏰ **Expiry** | 2 hours, renewed by heartbeat | Never expires |
| 🧹 **Its guests** | Destroyed at `lease-end` | Kept, and `protection` set |
| 💾 **Backups** | None by the lease; see the retained registry | Weekly, to the bulk storage |
| 🚪 **Ending it** | `lease-end` | `lease-destroy --confirm` |

**While any long-term lease is active, the machine never powers down.** Not by `lease-end`, not by the idle timer, not by the watchdog. That is the whole feature, and it is also the thing that will show up on your electricity bill, so the tool says so every time:

```json
{
  "host_powered_off": false,
  "host_left_running": true,
  "reason": "1 long-term lease(s) keep this machine on: 20260101-abcd1234"
}
```

## 👀 Seeing what is pinned

```bash
proxmox-lab lease-list
```

```json
{
  "active": [
    {"id": "…-abcd1234", "kind": "long-term", "purpose": "build box",
     "guests": [9001], "last_backup_at": "2026-01-08T03:00:00Z"}
  ],
  "host_pinned_on": true,
  "pinned_by": ["…-abcd1234"]
}
```

If you ever wonder why the lab is still humming, that is the command.

## 🛡️ Protection

Guests created under a long-term lease get Proxmox's `protection` flag, so a stray delete — from this tool, the web UI, or `qm destroy` — is refused. They are also registered with policy `retain`, so ordinary cleanup skips them.

You cannot end a long-term lease with `lease-end`; it refuses and points you at `lease-destroy`. Two different intentions deserve two different commands.

The protection runs the other way too. If an *ordinary* lease has registered a guest that a long-term lease also registers — an idempotent setup command run under both, say — `lease-end` on the ordinary lease refuses before it touches anything, naming the guest and the long-term lease.

To close the lease while preserving every guest registered with policy `retain`, use the distinct release operation:

```bash
proxmox-lab lease-release --lease <id> --confirm
```

It removes protection, finalizes the lease, leaves retained guests stopped, and powers off the host when no other lease is active. It does not weaken the destructive semantics of `lease-destroy`.

## 💾 Weekly backups

Every seven days, each guest is backed up with `vzdump` in **snapshot** mode — the guest keeps running — to the storage in `[lease] long_term_backup_storage`, defaulting to `[storage] bulk_storage`. Old generations are pruned to `long_term_backup_keep` (default 2).

The watchdog runs them, so nothing extra needs scheduling. To take one now:

```bash
proxmox-lab backup --force
```

`last_backup_at` only advances when *every* guest in the lease succeeded, so a partial failure is retried rather than quietly waiting another week.

```toml
[lease]
long_term_backup = true
long_term_backup_storage = ""   # blank = [storage] bulk_storage
long_term_backup_keep = 2
```

A slow, large disk is the right target. These are safety copies.

### What this does *not* cover

Only guests of an **active long-term lease**. That leaves the rest of the keep-forever set — templates, a released long-term lease's machines, persistent gateway and share workers — with no coverage at all, which is the opposite of what their value deserves. Those are covered by the retained registry instead:

```bash
proxmox-lab guest retain --vmid 101 --purpose "Ubuntu cloud-init template"
proxmox-lab backup --retained --force        # once, now
```

`doctor` reports `retained_backup` — how many retained guests exist, which have never been backed up, and the oldest backup age — whether or not the sweep is enabled, so the gap is visible rather than assumed. To have the watchdog do it on the same weekly interval:

```toml
[lease]
retained_backup = true              # off by default: it writes GBs on a schedule
retained_backup_interval_days = 7
```

It is off by default deliberately. Turning it on starts writing vzdump archives of every retained guest to the bulk store, which on a slow disk is hours of wall clock and gigabytes of space — a decision for the operator, not a default. When the watchdog runs it, it runs *outside* the controller lock and under its own non-blocking lock, so a long backup can neither block a lease operation nor have a second copy started by the next five-minute tick.

## Safety gate

| Destructive op | Required flag | What it guards |
|---|---|---|
| Destroy long-term lease + its guests (`lease-destroy`) | `--confirm` | protected guests only deleted after protection is lifted; refused without `--confirm`, which previews the guests and `last_backup_at` |
| Release lease but keep retained guests (`lease-release`) | `--confirm` | retained machines become independent of the lease, left stopped |
| End ordinary lease with cross-registered guest | `--shared-guests-authorized` (on `lease-end`) | refuses when any `delete`-policy guest is also registered to another `active` lease (including long-term and expired-but-still-active); without the flag it names the guest/other lease and records `lease-end-refused-shared-guest`; with it, still leaves a live-owned guest as `left_to_another_lease` |
| Host power-off while pinned | none — refused | while any long-term lease is `active`, `lease-end`/idle/watchdog reports `host_left_running: true` and does not power off — see [safety-policy.md](safety-policy.md) invariant 16 |

A long-term lease suspends safety-policy invariant 6: while one is active the host stays powered on, and every command that would otherwise shut it down reports that it did not, and why. Long-term guests carry `protection` and `retain` and are removed only by `lease-destroy --confirm`, which lifts protection first.

## Failure mode

- `lease-end` on a long-term lease always fails and points at `lease-destroy`; this is intentional — two intentions, two commands.
- `lease-destroy` without `--confirm` previews the loss (`qemu/9001 (buildbox)`, `Backed up so far: …`) and does not mutate. With `--confirm` it lifts protection, stops and deletes, then powers off the host only if nothing else pins it — order matters because Proxmox refuses to delete a protected guest.
- `backup` with a partial failure does not advance `last_backup_at` — a partial failure retries instead of waiting a week (see [safety-policy.md](safety-policy.md) invariant 18). The same rule applies to retained backups.
- A lease is active even past its expiry until finalized; an expired-but-`active` claimant still blocks a cross-registered delete unless `--shared-guests-authorized` is passed, and even then a still-live claimant keeps the guest alive (`left_to_another_lease`).
- `retained_backup = true` is off by default because it writes GBs on a slow bulk store; run `backup --retained --force` once to prove it fits before enabling the watchdog.

## 🔥 Destroying one

```bash
proxmox-lab lease-destroy --lease <id> --confirm
```

Without `--confirm` it refuses and shows you exactly what would be lost, including when it was last backed up:

```
This permanently destroys a long-term lease and everything in it:
  qemu/9001 (buildbox)

Backed up so far: 2026-01-08T03:00:00Z
Re-run with --confirm if that is what you want.
```

With `--confirm` it lifts the protection flag, stops and deletes the guests, and — if nothing else is holding the machine up — powers it off. The order matters: Proxmox refuses to delete a protected guest, so protection comes off first.

## 🤔 When not to use one

- **For work that finishes today.** Use an ordinary lease; that is what the automatic cleanup is for.
- **As a substitute for a server.** If something needs to be up all the time, a machine that is *designed* to stay on is a better home than a lab that merely stops turning itself off.
- **When you would not miss it.** A long-term lease is a commitment to power draw and disk space. Anything you would shrug at losing belongs in a disposable one.

## See also

- [safety-policy.md](safety-policy.md) — invariants 6, 17, 18, graceful finalization and orphan handling
- [storage.md](storage.md) — `storage status` classes and weekly backup target `[storage] bulk_storage`
- [CONFIGURATION.md](CONFIGURATION.md#lease) — `[lease]` keys `long_term_backup*`, `retained_backup*`

