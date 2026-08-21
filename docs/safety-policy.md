# Lease and shutdown policy

## Invariants

1. Every write belongs to one active lease.
2. Every created guest carries `codex-lab` and `lease-<id>` tags.
3. Cleanup deletes only guests registered to that lease.
4. Durable templates use `codex-template` and policy `retain`.
5. Lease expiry is two hours by default and is extended by heartbeats.
6. Ending or expiring the last lease powers off `pve`.
7. Completion requires the Proxmox API to remain unreachable for two
   consecutive checks.
8. Every operation writes a redacted audit event and attempts a normal
   fast-forward/rebase Forgejo sync.
   PocketBase authorization failures identify the audit-token secret to refresh.
   An `api` write may already have reached Proxmox before its audit event fails;
   it reports that write as succeeded but unrecorded rather than claiming an
   unrelated Proxmox permission failure.
9. Every MCP tool call records only its tool name and refreshes the idle clock.
10. A reachable host with no active leases is shut down after eight hours
    without an MCP tool call.
11. Console input (keys, typing, clicks), guest execution and file transfer
    require an active lease. Screenshots and terminal reads do not.
12. Typed text and generated passwords are never audited; only counts,
    exit codes and object keys are.
13. A no-op watchdog sweep writes nothing. The journal and the Forgejo history
    record events, not heartbeats.
14. Lab guests that reach the internet do so through the VPN gateway. The
    gateway forwards only `eth1 -> wg0` and drops everything else, so a
    dropped tunnel stops egress rather than leaking to the home WAN.
15. `net verify` must pass before a guest behind the gateway is used for real
    work, and after any change to the gateway ruleset.
16. A long-term lease suspends invariant 6: while one is active the host stays
    powered on, and every command that would otherwise shut it down reports
    that it did not, and why.
17. Long-term guests carry Proxmox `protection` and policy `retain`. They are
    removed only by `lease-destroy --confirm`, which lifts protection first.
18. A long-term backup marks success only when every guest in the lease
    succeeded, so a partial failure retries instead of waiting a week.

## Graceful finalization

For each disposable registered guest:

1. request ACPI shutdown for QEMU or shutdown for LXC;
2. wait up to 120 seconds for `stopped`;
3. issue a hard guest stop only if graceful guest shutdown timed out;
4. delete the stopped guest;
5. preserve the Proxmox task ID and result.

After all leases are closed:

1. request `command=shutdown` on `/nodes/pve/status`;
2. wait up to 240 seconds for the API to disappear;
3. retry one reachability check;
4. invoke `script.nanokvm_pc2_force_off` only if the API remains reachable;
5. wait up to 60 seconds and verify the API is down.

## Refuse or stop

- Refuse a write with no active lease.
- Refuse deletion of an unregistered VMID.
- Refuse host storage, network, access-control, cluster, SDN, firewall-default,
  or device-passthrough changes unless the user's current request explicitly
  authorizes that category.
- Stop and report if an untagged or pre-existing guest would be deleted.
- Treat the `[memflow]` SSH connection as a distinct trust boundary: it reaches
  the host as root outside the API token, so it stays off unless the user has
  configured it, and `memflow host-setup` / `usb attach` are host changes gated
  behind `--host-change-authorized`. `netcap` (capture, SSL inspection, MITM)
  rides the same connection and is subject to the same boundary.
- Refuse a `memflow write` or `memflow phys-write` (both mutate live guest
  memory -- kernel-virtual and physical/RAM-injection respectively) unless the
  user's current request explicitly authorizes it, then pass `--i-understand`.
  Never pass a USB device backing active storage through to a guest.
- Capture and decrypt only a guest's own traffic, within a lease. `netcap
  intercept` is an active MITM: install its CA only in guests the user controls,
  for work the user has authorized, and never rewrite traffic the user did not
  ask to rewrite.
- Do not log cloud-init passwords, tokens, authorization headers, SSH private
  keys, presigned S3 URLs, or full environment files. Guest memory, USB and
  network captures — and decrypted MITM flows — are never written to the ledger;
  only the fact of the capture is.
- Refuse to write any credential into this repository. The S3 key ID and
  secret belong in the macOS Keychain; `scripts/check-secrets.py` blocks the
  common shapes at commit time.

## What a tag proves, and what owns a guest

Every guest this tool creates is tagged `codex-lab;lease-<id>`. Those tags stay
on the node for ever; the lease records that explain them do not — they are
pruned, and on a rebuilt controller they were never there. So a tag is evidence
that *some* lease created a guest and nothing more. **Ownership checks must not
resolve `tag → lease file`**: on a fresh controller almost nothing resolves, and
nearly every deliberately-kept guest would be called unowned.

Ownership comes from the two things the controller actually keeps:

1. **The lease record**, for the life of the lease. This is what
   `require_lease_resource` checks before any mutation.
2. **The retained registry** (`retained.json` under the state root), for guests
   that outlive their lease on purpose. Written at register time for any
   `policy = retain` resource, never pruned automatically, and cleared when the
   guest is actually deleted.

`guest inventory` prints both for every guest on the node: its tag, whether
anything local resolves it, and whether the registry vouches for it. A guest
this tool created that neither vouches for is **orphaned**.

An orphan matters because of a deliberate interaction between two safety rules:
cleanup only ever finalizes resources listed in a lease, and `shutdown_host()`
refuses to power off while *any* guest is running. So one running orphan is
invisible to every sweep and keeps the machine on indefinitely — five days, in
the run that prompted this. `doctor` therefore fails when a running orphan
exists, and reclamation is explicit:

```bash
proxmox-lab guest inventory --orphaned-only
proxmox-lab cleanup-expired --orphans-only --host-change-authorized
```

`--orphans-only` does exactly that and nothing else. Plain `--reclaim-orphans`
folds reclamation into a full expiry sweep, which in the same run finalizes
every expired lease — deleting their guests — and then decides whether to power
the host off. Those are much larger intentions, so they have separate flags.

### "Orphaned" does not mean "abandoned"

It means *this* controller has no record of the guest. A second controller — or
one whose state directory lives elsewhere — drives guests through the same API
token, and its lease records are not here. So a running orphan may be somebody
else's live work.

Reclamation therefore leaves a guest alone if either signal says it is in use:
a non-stop task for it in the last 30 minutes (console, start, agent), or an
uptime shorter than that. Stop tasks are excluded, or our own stop would make
every later run refuse; an unreadable task log counts as in use, because not
knowing must not resolve to stopping someone's work. `--include-active`
overrides it. `doctor` reports such guests as `orphaned_but_active` rather than
as a problem — they keep the host on, which is correct while they are in use.

This was learned the direct way: a reclamation run stopped a ReactOS benchmark
that another session had been screenshotting every 45 seconds, and that session
restarted the guest 90 seconds later.

Reclamation **stops, and never deletes.** Stopping is reversible and is all
that is needed to unblock power-off; a controller that has lost the record of a
guest cannot vouch for what is on its disk, so deleting it stays a human
decision. Adopting one instead is the other half:

```bash
proxmox-lab guest retain --vmid 101 --purpose "Ubuntu cloud-init template"
```

That records the guest as deliberately kept — it stops being reported as an
orphan and becomes eligible for retained backups. It changes controller state
only; the guest is never touched.

## Recovery

The macOS LaunchAgent runs `cleanup-expired` every five minutes. It makes
abandoned work eventually safe even if the calling agent crashes or loses its
thread, and enforces the eight-hour MCP-idle shutdown threshold. A lease
heartbeat prevents cleanup and idle shutdown during legitimate long-running
work.

Two properties of that sweep matter enough to state:

- **It retries a failed cleanup.** A lease left in `cleanup_failed` — say a
  QEMU lock timed out while stopping one guest — is picked up by every later
  sweep until it succeeds. Finalizing is idempotent, so a resource that is
  already gone costs nothing. Previously such a lease was skipped for ever and
  its guests, and the host, stayed up until someone reran `lease-end` by hand.
- **It never destroys a resource another live lease owns.** Before stopping or
  deleting a registered guest, cleanup checks whether any other lease that is
  still live (long-term, or not yet expired) also registers that
  `(kind, vmid)`. If one does, the resource is left alone and reported as
  `left_to_another_lease`. An expired claim does not shield a guest, so two
  stale leases cannot leave one running for ever. `lease-register` also refuses
  outright to take a guest that a live lease already owns.

If an ordinary lease is stale but every registered guest is already stopped,
use `proxmox-lab lease-abandon --lease <id> --confirm`. It verifies those
guest states, then closes only the local lease record: it does not start,
stop, delete, or otherwise mutate a guest, and it does not shut down the
host. It refuses long-term leases, unreachable Proxmox, or any guest that is
not verifiably stopped. It attempts an audit event and reports explicitly if
the record could not be written.
