# What has actually been verified

This page is a statement of confidence, not a feature list. It separates what
the test suite proves from what has been watched working on real hardware —
and, just as usefully, what has not.

## Where things stand after the rework

The control plane was rebuilt on 2026-09-25: the old HTTP API, central
ledger, and host daemon were replaced by a single root ssh channel and a
local SQLite `lab.db` ([architecture.md](architecture.md) has the module
map). **No run against a real Proxmox node has been recorded since that
rebuild.** Every claim below is test-suite evidence; nothing on this page
has been observed end to end on hardware in its current form.

That does not mean the suite is thin — it means the boundary between
"the code does X" and "Proxmox accepts X" is drawn exactly where it is.

## The suite that exists

308 tests, warning-clean (`PYTHONWARNINGS=error`), run with:

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
  `initialize`, `tools/list` returning exactly 23 schema'd tools,
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
  deletes nothing; idempotent install/uninstall of its one crontab line.
- **MCP.** Over real stdio: initialize shape, the static 23-tool list, bad
  params as `-32602` naming the field, action failures as `-32603` with
  redacted messages, notifications answered with silence, every call
  refreshing the idle clock, and the idle sweep firing a verified shutdown
  with no client traffic.

## What it cannot prove

The seams that stub the host are also the seams where reality can disagree.
None of the following has been observed on this reworked code:

- **Every byte on the wire to a live Proxmox.** FakeSSH asserts the argv the
  tool *emits*; it cannot confirm the real `qm`/`pct` accept it. Three
  commands are believed correct from the manpages but unconfirmed on a
  node, each with a tested fallback branch that has likewise never run:
  `pct create --tags --description` (falls back to create-then-`pct set`),
  `pct shutdown --timeout` (falls back to plain shutdown + status polling +
  stop), and `qm guest exec --synchronous` (falls back to async exec +
  `exec-status` polling). If a fallback fires on hardware, that is the
  first time it has ever run.
- **A real guest doing anything.** No guest has been created, cloned,
  started, probed, run-in, screenshotted, or typed at under this code.
  `guest run`'s remote quoting, `guest probe`'s agent detection, and the
  exec-status polling loop are all asserted against scripted output.
- **The host actually coming up or going down.** The magic packet's bytes
  are proven; a NIC receiving one is not. Verified shutdown's probe math is
  proven; real sshd dying mid-request, DDNS lag, and a warm host refusing
  to die are not covered by a fake clock. And the MCP/idle power-off has
  never been watched happen — the self-wake fires in tests, the physical
  shutdown it would trigger has not been staged.
- **`gc install`'s byte stream landing.** Install/uninstall are
  FakeSSH-tested; whether the script survives a real `install`+crontab
  round-trip on the node, and whether root's cron actually runs it, is
  unobserved. `gc status`'s drift detection is unit-tested only.
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

## Reproducing a hardware pass

The honest baseline is a single ordinary lease:

```bash
proxmox-lab power wake --standalone-authorized   # if the host is down
proxmox-lab doctor
proxmox-lab status

L=$(proxmox-lab lease-begin --purpose "verification sweep" \
    | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
trap 'proxmox-lab lease-end --lease "$L"' EXIT

proxmox-lab guest create --lease "$L" --vmid 9100 --name verify --start
proxmox-lab guest probe --vmid 9100
proxmox-lab guest run --lease "$L" --vmid 9100 -- uname -a
proxmox-lab push --lease "$L" --vmid 9100 --file ./probe.bin --dest /tmp/probe.bin
proxmox-lab pull --lease "$L" --vmid 9100 --remote /tmp/probe.bin --out ./probe.back
proxmox-lab console screenshot --vmid 9100 --out screen.png
proxmox-lab journal --limit 20
```

`lease-end` (via the trap) then destroys the guest and verifies the host
power-off — exercising the two riskiest paths (guest lifecycle and verified
shutdown) in one run. For the GC, `proxmox-lab gc install` plus one
`--dry-run`-equivalent log read on the host covers the rest.

Check the claims rather than trusting them. If something here is no longer
true, the honest fix is to change this page, not to leave it aspirational.
