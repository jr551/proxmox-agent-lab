# Long-term leases

An ordinary lease promises that everything disappears: guests destroyed, host
powered off. A long-term lease makes the opposite promise for the machines you
want to keep — a build box, an always-on service, something you are still
debugging next week.

```bash
proxmox-lab lease-begin --long-term --purpose "persistent build box"
```

That is one flag on the ordinary command. It changes four things, and the
first one costs money:

| | Ordinary lease | Long-term lease |
|---|---|---|
| **The host** | Powered off when the last lease ends | **Stays on**, indefinitely |
| **Expiry** | `ttl_seconds` (default 2 h), renewed by heartbeat | Never expires (`expires_at` 0) |
| **Its guests** | Destroyed at `lease-end` | Kept, stamped `pxl-expiry=0` |
| **Ending it** | `lease-end` | `lease-destroy --confirm` — the only exit |

`lease-begin` does not send Wake-on-LAN: if the host is off, it refuses and
tells you to run `power wake` first (a standalone power lever, gated
`--standalone-authorized` because a person must own it). So begin a long-term
lease while the host is up — which it will then stay.

The command itself says all of this back at you:

```json
{
  "kind": "long_term",
  "expires_at": 0,
  "warning": "This is a long-term lease: the lab machine will stay powered on until it is destroyed with 'lease-destroy'. Its guests carry pxl-expiry=0 and are never swept."
}
```

## Why the host stays on

Three independent mechanisms all agree to leave it running:

- **The controller's cleanup.** `lease-end` and `cleanup-expired` never
  finalize a long-term lease, and when the last *ordinary* lease ends the
  finalizer reports the host left on rather than shutting it down:

  ```json
  {
    "host_powered_off": false,
    "host_left_running": true,
    "reason": "1 long-term lease(s) keep this machine on: 20260925-abcd1234",
    "to_power_off": "destroy them with 'lease-destroy', or stop the host yourself"
  }
  ```

- **The host-side GC.** Guests stamped `pxl-expiry=0` are never reaped and
  count as *pinned*: `pxl-gc` logs `skip <vmid>: long-term (pxl-expiry=0)`
  and any pinned guest vetoes the idle power-off, forever.

- **The MCP idle shutdown.** It fires only when no lease is active at all;
  a long-term lease is active, so the server never powers the host down
  under one.

See [safety-policy.md](safety-policy.md) invariants 2 and 6.

## Heartbeat does nothing — by design

A heartbeat exists to push an expiry forward. A long-term lease has no
expiry, so the command answers rather than acting:

```bash
proxmox-lab lease-heartbeat --lease <id>
```

```json
{
  "lease": "<id>",
  "kind": "long_term",
  "expires_at": 0,
  "note": "long-term leases do not expire; no heartbeat needed"
}
```

Nothing breaks if a task heartbeats a lease it does not know is long-term;
it just gets a no-op.

## Seeing what is pinned

```bash
proxmox-lab lease-list
```

```json
{
  "active": [
    {
      "id": "20260925093000-abcd1234",
      "kind": "long_term",
      "purpose": "persistent build box",
      "expires_at": 0,
      "guests": [9001]
    }
  ],
  "host_pinned_on": true,
  "pinned_by": ["20260925093000-abcd1234"]
}
```

If you ever wonder why the lab is still humming, `pinned_by` is the answer.

## Protection is metadata, not a guest flag

A guest created or registered under a long-term lease is stamped
`pxl;lease-<id>` plus `pxl-lease=<id> pxl-expiry=0` in its description. That
stamp is the whole protection:

- this tool's cleanup and the host-side GC both skip `pxl-expiry=0`;
- `guest destroy` still requires the guest be registered to *your* lease in
  `lab.db`;
- `lease-end` on another lease refuses before touching a guest a long-term
  lease also registers (see `--shared-guests-authorized` in
  [safety-policy.md](safety-policy.md)).

There is deliberately **no Proxmox `protection` flag** set on the guest. A
human running `qm destroy` on the host will still delete the machine — the
lease then holds a dead registration, and the tool notices on the next
operation. The pin protects against the tool and the sweeps, not against
root on the host. Two different intentions, two different mechanisms, only
one of which exists.

`lease-end` and `lease-abandon` both refuse a long-term lease outright and
name `lease-destroy` — ending one is a destructive decision, not an
administrative one.

## Destroying one

```bash
proxmox-lab lease-destroy --lease <id> --confirm
```

Without `--confirm` it refuses and previews exactly what would be lost:

```
This permanently destroys a long-term lease and everything in it:
  qemu/9001 (buildbox)
Re-run with --confirm if that is what you want.
```

With `--confirm` the order matters and is deliberate:

1. Each registered guest's stamp is rewritten to `pxl-expiry=<past>`. A
   teardown that half-fails leaves expired guests the host-side GC will
   reap — never guests that pin the machine on for ever.
2. Guests are shut down (graceful first, hard stop after the timeout) and
   destroyed.
3. The lease record ends in state `destroyed`. On a partial failure it stays
   `cleanup_failed` with the per-guest errors.
4. The host powers off, verified by repeated probe failure, only if no other
   lease — ordinary or long-term — is still active.

## Failure modes

- `lease-destroy` on an *ordinary* lease refuses: end those with
  `lease-end`. The destructive verb is reserved for the kind that needs it.
- If a destroy half-fails, re-run `lease-destroy --confirm`; the lease is
  `cleanup_failed`, which `cleanup-expired` skips for long-term leases — the
  sweep will not finish the job for you.
- `power shutdown --standalone-authorized` does not check leases; it refuses
  only while a guest is running. With all long-term guests stopped it *will*
  power the host off — the machines then simply sit stopped until the host
  is booted again. The pin is a policy against *automatic* power-off, not a
  lock against a person with the flag.
- A guest deleted out-of-band on the host leaves its registration behind;
  `doctor` drift reporting and the next lease operation surface it.

## When not to use one

- **For work that finishes today.** An ordinary lease is what the automatic
  cleanup is for.
- **As a substitute for a server.** If something needs to be up all the
  time, a machine designed to stay on is a better home than a lab that
  merely stops turning itself off.
- **When you would not miss it.** A long-term lease is a commitment to power
  draw and disk space; anything you would shrug at losing belongs in a
  disposable one.

## See also

- [safety-policy.md](safety-policy.md) — the invariants behind the pin,
  the shared-guest refusal, and verified shutdown
- [INSTALL.md](INSTALL.md) — installing the host-side GC that honours
  `pxl-expiry=0`
- [commands.md](commands.md) — generated command reference
- [CONFIGURATION.md](CONFIGURATION.md) — `[lease] ttl_seconds`,
  `idle_shutdown_seconds`
