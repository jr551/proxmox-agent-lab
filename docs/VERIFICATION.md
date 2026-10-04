# What has actually been verified

This page is a statement of confidence, not a feature list. It separates what
the test suite proves from what has been watched working on real hardware —
and, just as usefully, what has not.

## Where things stand after the rework

The control plane was rebuilt on 2026-09-25: the old HTTP API, central
ledger, and host daemon were replaced by a single root ssh channel and a
local SQLite `lab.db` ([architecture.md](architecture.md) has the module
map).
Several runs against a real Proxmox node **were** recorded on 2026-09-25 and
2026-09-27 (PVE 9.2.2, node `pve`), covering `init`, `doctor`, `status`,
`lease-begin`/`lease-end`, fresh LXC create, `guest probe`/`guest run`,
`push`/`pull` with a sha256-verified payload, `guest destroy`, `journal`,
`gc status`, and an MCP `guest_list` over real stdio. Two defects were found
by those runs and fixed (an LXC `rootfs` with no storage knob, and
`guest destroy` refusing a running guest). A 2026-09-27 review pass added
the safety fixes listed below and re-ran the full lease lifecycle on
hardware: create → run → lease-end destroyed the guest while leaving every
pre-existing guest (100–103) untouched.

Claims below are still labelled per-section: test-suite evidence unless the
section says otherwise. What remains unobserved is the host power-off
duty — see "What it cannot prove".

On 2026-10-04 a throwaway config was pointed at node `pve` (PVE 9.2.2).
`doctor` was clean on ssh, host tools, the node name, `lab.db`, an already
installed `pxl-gc`, and no metadata drift, and it warned that
`[pve] template_vmid` is a normal guest rather than `template: 1`.
`guest create` against that vmid refused before `qm clone`. A fresh LXC
(not a clone) was created, started, probed, given a command, and used for
a push/pull whose sha256 matched, then destroyed. `lease-end` left the
host up because other guests were running, and exited 0. `power status`
showed the host reachable. `gc install` replaced the script (checksum
match, crontab line unchanged). A `--dry-run` with `PATH=/usr/bin:/bin`
enumerated every guest and refused to power off. A later read of the cron
log showed the scheduled runs doing the same: each guest skipped, power-off
refused while two guests were running.
`memflow doctor` refused because `/usr/local/bin/pxl-memflow-run` is not
installed; the helper was not built during this check. Guests already on
the node were left as found.

That does not mean the suite is thin — it means the boundary between
"the code does X" and "Proxmox accepts X" is drawn explicitly, not assumed.

## The suite that exists

373 tests, warning-clean (`PYTHONWARNINGS=error`), run with:

```bash
python3 -m unittest discover -s tests -q
```

CI runs them on Python 3.11–3.14 alongside `compileall`, the
secrets/public/release/docs check scripts, and `gen-commands.py --check`.
Everything is deterministic and writes only inside temp dirs; every test
redirects `$PROXMOX_AGENT_LAB_STATE` so the suite can never touch a real
controller's `lab.db`.

Three harnesses do the heavy lifting:

- **`tests/support/fakessh.py`** — a duck-typed stand-in for the one module
  that spawns `ssh`. It records every remote argv, answers scripted outputs
  matched by regex, and fails loudly on any call no rule covers. Tests
  assert the *call sequence*, not just results.
- **Stub `qm`/`pct`/`poweroff` on `PATH`** — `test_pxl_gc.py` drives the
  real, standalone `resources/pxl-gc.py` as a subprocess: nothing from the
  package is imported, because the script must run alone on the host. The
  stubs serve guests from files, so the GC's own parsing, locking, and
  power-off decision run end to end.
- **Real stdio for the MCP server** — `test_mcp.py` spawns
  `proxmox-lab mcp` and speaks newline-delimited JSON-RPC over real pipes:
  `initialize`, `tools/list` returning exactly 29 schema'd tools,
  `tools/call` dispatch, error shapes, and the idle-shutdown self-wake.

## What the suite proves

Verified as code behavior, area by area:

- **ssh seam.** Exact argv shape (`ssh -o BatchMode=yes -o ConnectTimeout=5
  <target> '<quoted argv>'`); shell metacharacters and Unicode arrive as
  literal remote text; the command allowlist refuses before any spawn;
  host-changing commands (`shutdown`, `crontab`, `install`, `ethtool`,
  `tee`, `rm`) need the `host_change` gate; `cat`/`tee`/`rm`/`base64` are
  path-confined to the `pxl` namespaces and `..` is refused after
  normalization; timeouts map to `TransportError`, never a hang.
- **Leases.** `lease-begin` refuses an unreachable host rather than opening
  an ungovernable lease; begin stamps `pxl-lease=`/`pxl-expiry=` metadata;
  heartbeat rewrites expiry on every registered guest; `pxl-expiry=0` is
  the long-term spelling; lease claims are compare-and-swap so two
  finalizers cannot race; `lease-end` refuses a shared guest *before*
  touching anything; finalization is idempotent (an already-gone guest is
  success); a failed teardown lands `cleanup_failed` and is retried by the
  next sweep; `host_powered_off` is reported truthfully, never rounded up.
- **Power.** The Wake-on-LAN packet is golden-tested byte-for-byte
  (6×0xFF + 16×MAC). Verified shutdown counts consecutive all-fail probe
  rounds across ssh and TCP :22 — a single success resets the count, a
  burst inside the window does not succeed early, a timeout reports
  `host_powered_off: false` and fails.
- **Store.** Compare-and-swap lease transitions, WAL concurrency between
  readers and a writer, redaction before insert, journal filters, and the
  rule that an unrecordable audit event warns on stderr instead of failing
  the action it was watching.
- **Guests.** Create/clone stamp tags and description in one call; destroy
  refuses an unregistered guest and one carrying no `pxl` tag; teardown
  order is graceful shutdown → wait → hard stop → destroy.
- **Transfer.** `push`/`pull` are chunked base64 through guest exec — the
  only path — with sha256 verified in the guest; a `pull` that mismatches
  its expected digest deletes the partial file and fails.
- **Console.** `sendkey` translation including shifted glyphs, `ret`/`f2`/
  `ctrl-alt-delete`; unknown key names refuse with the accepted list;
  typed text is paced and audited by count, never content; the QEMU
  screendump PPM→PNG path round-trips a golden fixture; LXC screenshots are
  refused cleanly.
- **Host-side GC.** Driven as a real subprocess against the stubs: destroys
  only expired `pxl` guests; skips `pxl-expiry=0`, templates, and
  unparseable metadata with a warning; serializes per-vmid under flock;
  powers off only on two *consecutive* clear runs ≥600 s apart; `--dry-run`
  deletes nothing; `qm`/`pct` are found in `/usr/sbin` when cron's PATH
  omits them; idempotent install/uninstall of its one crontab line.
- **Memflow.** Lease ownership and a running qemu guest are required before
  any helper argv; an oversized `--len`, a write without `--i-understand`,
  and `host-setup` without `--host-change-authorized` spawn nothing. Audited
  fields are address, length and counts — never bytes, process names or
  scanned boot text. The helper's own argv shape (subcommand, numeric vmid,
  bounded hex) is refused at the seam. Building that helper on a host is
  not what the suite does.
- **MCP.** Over real stdio: initialize shape, the static 29-tool list, bad
  params as `-32602` naming the field, action failures as `-32603` with
  redacted messages, notifications answered with silence, every call
  refreshing the idle clock, and the idle sweep firing a verified shutdown
  with no client traffic.

## What it cannot prove

The seams that stub the host are also the seams where reality can disagree.
Except where the 2026-10-04 note above says a path was watched, none of
the following has been observed on this reworked code:

- **Every byte on the wire to a live Proxmox.** FakeSSH asserts the argv the
  tool *emits*; it cannot confirm the real `qm`/`pct` accept it. Three
  commands are believed correct from the manpages but unconfirmed on a
  node, each with a tested fallback branch that has likewise never run:
  `pct create --tags --description` (falls back to create-then-`pct set`),
  `pct shutdown --timeout` (falls back to plain shutdown + status polling +
  stop), and `qm guest exec --synchronous` (falls back to async exec +
  `exec-status` polling). If a fallback fires on hardware, that is the
  first time it has ever run.
- **A real guest doing everything.** One fresh LXC was created, started,
  probed, run-in, pushed, pulled, and destroyed (2026-10-04). Cloning a
  real template, screenshots, and console typing have not been watched.
  `guest run`'s remote quoting and the exec-status polling loop are
  asserted against scripted output; the LXC `pct exec` path is what ran.
- **The host actually coming up or going down.** The magic packet's bytes
  are proven; a NIC receiving one is not. Verified shutdown's probe math is
  proven; real sshd dying mid-request, DDNS lag, and a warm host refusing
  to die are not covered by a fake clock. And the MCP/idle power-off has
  never been watched happen — the self-wake fires in tests, the physical
  shutdown it would trigger has not been staged.
- **`gc uninstall`.** Install, a checksum match, and scheduled runs are
  observed (2026-10-04): the cron log shows each guest skipped and
  power-off refused while guests are running. Uninstall was not run.
- **Concurrent controllers on one `lab.db`.** WAL covers same-host
  concurrency in tests. Two machines running the tool against one host
  (orphans in each other's eyes) is a documented design limit, not a tested
  one.
- **Performance.** Nothing about chunk throughput, screendump latency, or
  exec polling cadence has been measured on real hardware.

## Hardware history — superseded

The previous version of this page carried a verified-on-hardware table
dated 2026-08-21 through 2026-09-11, exercised against a Proxmox 9.2.2
node over the old HTTP API control plane. Two things changed underneath it:

- most of its rows covered features that no longer exist;
- the surviving actions moved to different transports — console reads went
  from WebSocket/VNC to `qm monitor screendump` over ssh, guest exec from
  the API to `qm guest exec`, and teardown from API calls to `qm`/`pct`
  shutdown/stop/destroy — so "watched working" on the old code does not
  transfer.

That history is kept in git, not on this page: it described a system that
has been replaced.

## Review findings fixed on 2026-09-27

A three-way review of the rework diff (destruction/ownership, remote seam
and MCP protocol, state/audit/host GC) found these defects. Each is fixed
and each fix has a regression test; the ones marked *hardware* were
re-verified on the real node afterwards.

- **Host GC failed open on enumeration failure** (most serious). A failed or
  missing `qm`/`pct list` returned `[]`, which the power-off duty read as
  "zero running guests" — so a listing failure (for example cron's PATH
  lacking `/usr/sbin`) could power the host off under a running untracked
  guest. Enumeration now reports failure distinctly and the power-off duty
  refuses to judge, dropping the clear stamp so a later clear run must
  re-observe. *Unit-tested; the installed-cron path is still unobserved.*
- **The expiry sweep destroyed templates.** `guest destroy` refuses a
  template, but `finalize_lease` never read the guest config, so a template
  registered `disposable` was destroyed — killing the shared clone source.
  The sweep now consults the host config and skips templates. *Hardware:
  a real lease-end destroyed only its own guest and left 100–103 intact.*
- **A teardown error was mistaken for success.** `_guest_is_gone` matched
  any "no such file or directory" in stderr, so a destroy that failed over a
  disk path stamped the resource destroyed and ended the lease, orphaning a
  live guest with no sweep left to retry it. Absence is now confirmed by a
  fresh status probe. *Unit-tested.*
- **`guest destroy` ignored cross-lease ownership**, so one lease could
  destroy a guest another live lease had registered. Now refused.
- **MCP transfer tools had no path confinement** — a remote client could
  read any host file (`push_file`) or overwrite any host file with
  guest-controlled bytes (`pull_file`), running as root. MCP transfer paths
  are now confined to `<state dir>/transfers`; the CLI operator is
  unaffected.
- **The seam allowed host-mutating shapes ungated**: `pvesh` (any verb) and
  `ip` (any subcommand) could reconfigure networking, storage and cluster
  state without `host_change=True`, and `install` was not path-confined.
  `pvesh` is now read-verbs-only, `ip` is limited to the read-only probe,
  and `install` is confined to the pxl namespaces. *Hardware: `doctor`,
  which exercises both reads, still passes.*
- **Unbounded client numbers**: `guest_run`/`guest_stop` `timeout` and
  `journal_query` `limit` were accepted with no ceiling and flowed into a
  blocking ssh subprocess. Now capped.

Still unobserved on hardware: the verified host power-off and shutdown
probes, and an installed `pxl-gc` crontab run (the GC script itself was
exercised with stubbed `qm`/`pct`, and the enumeration-failure path was
verified directly against the real script).

## Pointer input and calibration — observed on hardware 2026-09-27

The mouse and capture tools ported from vnc-mcp (BSD 2-Clause, credited in
NOTICE) were exercised against a real 512MB QEMU guest on 192.168.69.105
(PVE 9.2.2), booted from an ISO and driven through `qm monitor` HMP:

- **A real click lands.** `console move` then `console click` at (360,200)
  on a 720x400 framebuffer were accepted by the guest's HID tablet, and
  `--screenshot-after` returned a fresh 720x400 frame.
- **Calibration is correct, not merely present.** Markers were laid on the
  real 720x400 display, readings submitted as if by a client showing it at
  50% scale, and the fit came back `x: a=2.0 b=0.0`, `y: a=2.0 b=0.0` with
  `rmse_px: 0.0`. An image-space click at (180,100) then resolved to
  framebuffer (360,200) — exactly the true pixel — and was delivered.
- **The refusals hold on real hardware.** `(5000,200)` was rejected as
  outside the 720x400 framebuffer, and `--space image` without a calibration
  was refused before any input was sent.
- **Drag and burst work.** A calibrated drag (180,100)→(300,150) resolved to
  (360,200)→(600,300) and interpolated 6 hops; `console burst --frames 3`
  stitched three real captures into a 2168x400 image.
- **MCP carries the same behaviour.** `console_move` and `console_click`
  over real stdio on the running server applied the saved calibration
  (100,50 → 180,100) and reported `clicked: true`.

Test guest 9196 was destroyed afterwards and the host was verified back to
its original four guests (100–103). The `/tmp/pxl-shot-9196.ppm` left behind
is the documented by-design behaviour: the screenshot host temp is never
removed, so capture stays read-only on the host.

Still unobserved on hardware: the calibration workflow driven by a *real*
downscaling image viewer (the 50% case above was submitted from measured
half-scale readings, not from an actual IDE's rendering), and guest
applications that specifically require intermediate drag motion.

## Ownership labels and host cleanup — observed on hardware 2026-09-27

The guest tag contract was made human-readable at the user's request: guests
are stamped `proxmoxagentlab;<controller-hostname>;lease-<id>` (e.g.
`proxmoxagentlab;mac;lease-…` on a Mac), replacing the opaque `pxl` token.
Verified live on 192.168.69.105:

- `guest create --fresh` produced tags `proxmoxagentlab;omp-box;lease-…` and
  description `pxl-lease=… pxl-expiry=…`, confirmed by `qm config`, `qm
  list`, and `pvesh` — the labels are real host state, not local bookkeeping.
- **Host-side GC reaped an expired guest.** A guest whose `pxl-expiry` was
  past was graceful-stopped, destroyed and gone within one `pxl-gc` run;
  guests without the ownership tag (100-103) and the unexpired lab guest
  were skipped, and power-off was refused while 102/103 ran.
- **Controller-side expiry swept the lease.** `cleanup-expired` on a lease
  with a past `expires_at` destroyed its remaining guest and left the host
  up (operator guests running) — exactly the pin rule.
- **`gc install` was broken and is now fixed.** The seam's `install -d`
  allowlist knew `/tmp/pxl-*` and `/var/log/pxl-*` but not `/var/lib/pxl-gc`,
  so install shipped the script then refused before the crontab. `/var/lib/pxl-*`
  joined the `install -d` confinement; install now completes script + state
  dir + crontab, and `gc status` reports `checksum match / crontab present`.
- `doctor` reports the gc_cron installed and zero metadata drift.

The legacy `pxl` tag is still honoured by every matcher (GC, orphan scan,
guest destroy, doctor) so a guest stamped before the rename is never
orphaned by it; new stamps never write it.

## Not ported, deliberately

The remaining ~30 vnc-mcp tools were not ported because each requires an
agent listening inside the guest — a VNC server, or the WinMCP tray
application for registry, services, PowerShell, Marionette browser control
and UI automation. That is exactly the "runs on the guest" surface this
project deleted. The QMP socket (`/run/qemu-server/<vmid>.qmp`) does work and
was proven to accept absolute mouse events, but reaching it needs `socat`,
`nc`, `python3` or `bash` on the host, all of which the ssh allowlist
refuses on purpose: admitting them would hand an MCP tool arbitrary code
execution as root. HMP over the already-allowlisted `qm` is the only input
transport that keeps that boundary closed.

## Reproducing a hardware pass

The honest baseline is a single ordinary lease:

```bash
proxmox-lab power wake --standalone-authorized   # if the host is down
proxmox-lab doctor
proxmox-lab status

L=$(proxmox-lab lease-begin --purpose "verification sweep" \
    | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
trap 'proxmox-lab lease-end --lease "$L"' EXIT

proxmox-lab guest create --lease "$L" --vmid 9100 --name verify --fresh --start
proxmox-lab guest probe --vmid 9100
proxmox-lab guest run --lease "$L" --vmid 9100 -- uname -a
proxmox-lab push --lease "$L" --vmid 9100 --file ./probe.bin --dest /tmp/probe.bin
proxmox-lab pull --lease "$L" --vmid 9100 --remote /tmp/probe.bin --out ./probe.back
proxmox-lab console screenshot --vmid 9100 --out screen.png
proxmox-lab journal --limit 20
```

`lease-end` (via the trap) then destroys the guest. It powers the host off
only when nothing else is running and the shutdown can be verified; if
other guests are running it leaves the host up and says why. `--fresh` is
required unless `[pve] template_vmid` is actually `template: 1`. For the
GC, `proxmox-lab gc install` plus one `--dry-run` under `PATH=/usr/bin:/bin`
covers the rest.

Check the claims rather than trusting them. If something here is no longer
true, the honest fix is to change this page, not to leave it aspirational.
