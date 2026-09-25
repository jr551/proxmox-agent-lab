# Wave 1 audit — store / ssh / proxmox / pxl-gc / gc against the rework plan

Read-only audit, 2026-09-25, branch `rework-mcp-skill`. Base: HEAD `68a8a80` plus the
working tree as audited (the parallel session's in-flight edits to `config`,
`power`, `leases`, `cleanup`, `audit`, `journal`, `diagnostics`, `cli`, `fixtures`,
`test_abstractions`, `test_install` and the untracked `test_audit.py`,
`test_cleanup.py`, `test_leases.py`, `test_power.py` were observed but not
audited — they are being actively rewritten; the only worktree delta inside an
audited file was `proxmox.py`'s appended `from_config()` helper, which was read
and is conformant). Every finding below is a place where committed (or
tracked-WIP) code actually deviates from `docs/rework-plan.md` or `AGENTS.md`,
with the violated text quoted.

## Summary counts

| Severity | Count |
|---|---|
| BLOCKER | 1 |
| DRIFT | 4 |
| NIT | 5 |
| **Total** | **10** |

None of the ten sits inside `store.py`, `proxmox.py`, `pxl-gc.py`, or the
wave-1 test files' assertions; one (N1) is a store API-surface NIT, two (B1, N4)
and one DRIFT (D4) belong to the parallel lane and are reported, not fixed.

---

## Findings by file

### BLOCKER

**B1 — `tests/test_abstractions.py:286` (tracked WIP) and `tests/test_audit.py:80,82` (untracked): the canonical secret guard is red on the working tree.**

- Evidence: `python3 scripts/check-secrets.py .` now prints
  `Potential secrets found:` for all three lines (it was green at `68a8a80`; the
  pattern has zero hits at HEAD — these are new in the parallel lane's edits).
  `test_abstractions.py:286` contains `"PVEAPIToken=user@pve!name=zzz"`, which
  matches the scanner's `PVEAPIToken\s*=\s*[A-Za-z0-9._~!@#$%^&*+-]{12,}` rule;
  `test_audit.py` supplies `password="fake-password"`, `ssh_key="fake-key-material"`
  and a `PVEAPIToken=…deadbeef=cafe` fixture string.
- Violated: AGENTS.md — *"Before committing, do not include credentials, private
  keys, presigned URLs, host addresses, MAC addresses, VMIDs, disk serials,
  captures, guest memory, site notes, or runtime journals. Keep
  `scripts/check-secrets.py` strict: add an allowlist entry only for a genuinely
  public constant with an explanation."*
- Severity: **BLOCKER** (a mandated guard command fails for everyone in this checkout).
- Fix (parallel lane, before commit): shape the fixtures so they do not match
  (split the literal, e.g. `"PVEAPIToken" + "=" + …`), or add one justified
  allowlist entry.

### DRIFT

**D1 — `host_transport.py:81-86` and `host_transport.py:149-153`: a second module spawns `ssh` and composes the remote command string by hand.**

- Evidence: `remote = shlex.join(remote_argv)` (line 81) fed to
  `subprocess.run(_ssh_argv(remote), …)` (lines 83, 151); `_ssh_argv` (lines
  52-69) builds its own `ssh -o BatchMode=yes …` invocation. The module never
  calls `check_allowed`, so the new allowlist boundary does not hold on this path.
  Live importers today: `console`, `diagnostics`, `disk`, `diskactivity`,
  `hostinfo`, `memflow`, `netcap`, `usb` (lazy `from . import host_transport`).
- Violated: plan §D Transport rules — *"**One seam.** `ssh.py` is the only module
  that spawns `ssh`; everything above it (including `qm guest` traffic) goes
  through it, argv by argv."* and *"No caller composes remote shell strings by
  hand."*
- Severity: **DRIFT** (the file is already dispositioned CUT in §B and dies in
  the deletion pass, but it is reachable from live modules right now).
- Fix: freeze it — no new callers; migrate its eight consumers to `ssh.py` in
  their phases so the deletion pass can remove it.

**D2 — `ssh.py:73`: `ethtool` sits in `ALLOWED_COMMANDS`, not `HOST_CHANGE_COMMANDS`.**

- Evidence: `ALLOWED_COMMANDS` includes `"ethtool"` (line 73);
  `HOST_CHANGE_COMMANDS` is `{shutdown, crontab, install, tee, rm}` (line 87).
  `check_allowed(["ethtool", "-s", …])` therefore passes with `host_change=False`.
- Violated: plan §D — *"The host-changing subset (`shutdown`, `crontab`,
  `install`, `ethtool`) additionally requires the explicit authorization flags
  (`--host-change-authorized` / `--standalone-authorized`)"*.
- Severity: **DRIFT** (ethtool can change link state; the plan names it as gated
  and the code does not gate it).
- Fix: move `"ethtool"` into `HOST_CHANGE_COMMANDS` and add one refusal case to
  `test_ssh.py`.

**D3 — `gc.py:176-204`: `gc status` implements only half of §F's status report.**

- Evidence: `status()` reports script presence, bundled-vs-remote checksum, and
  the crontab line — but never reads `/var/log/pxl-gc.log` and never runs the
  remote `pxl-gc --dry-run` that §F specifies (both would need allowlist entries
  for `pxl-gc` and a log read; the script's log path is only readable via `cat`
  if it lived under `/tmp/pxl-`, which it does not).
- Violated: plan §F — *"`status` (ungated, read-only): script presence at
  `/usr/local/sbin/pxl-gc` + checksum against the bundled copy, `# pxl-gc` block
  present in root's crontab, the last N lines of `/var/log/pxl-gc.log`, and a
  remote `pxl-gc --dry-run` showing what would be destroyed plus the power-off
  verdict."*
- Severity: **DRIFT** (two of four specified elements missing; the implementing
  task scoped status narrower than §F).
- Fix: either extend the allowlist with `pxl-gc` (plus a confined log read) and
  implement the tail + dry-run verdict, or amend §F's status text to the
  delivered scope — design call.

**D4 — In-flight non-wave-1 tests spawn real `ssh` (parallel lane).**

- Evidence: failures in `tests/test_diagnostics.py` (4× `InfrastructureGuestTests`)
  and `tests/test_diskactivity.py` (`ReclaimGuardDiskCounterTests`) carry the
  actual process output `ssh: Could not resolve hostname fixture-host: nodename
  nor servname provided` — real `ssh` was spawned because `cleanup.py:74`
  `_pvesh_get` reaches the real seam in those tests instead of a double.
- Violated: plan §H — *"Modules under test receive it via injection or
  monkeypatched `ssh.run`; no test spawns a real ssh."* (also §H "Tests are
  warning-clean and deterministic").
- Severity: **DRIFT** (determinism rule; DNS lookup attempt on every run).
- Fix (parallel lane): inject `FakeSSH`/seam double in the cleanup-backed paths
  those tests call before commit.

### NIT

**N1 — `store.py:233-261` (`set_lease_state`): a public state transition with no precondition.**

- Evidence: `UPDATE leases SET state = ? WHERE id = ?` — no `AND state = <from>`;
  any caller can move any lease from any state, including resurrecting a
  terminal one. The CAS primitive exists (`claim_lease`, `store.py:223-231`) and
  is tested, but nothing in the store forces transitions through it.
- Violated: plan §C — *"State transitions are compare-and-swap, not
  read-modify-write."*
- Severity: **NIT** (API surface; the CAS path exists and phase-3 can be written
  correctly — but the footgun is one line wide).
- Fix: add an optional `from_state` kwarg to `set_lease_state` that returns
  `False`/raises on mismatch, and document it as the post-claim setter.

**N2 — `tests/test_store.py`: §H's "WAL concurrency" is pinned, not exercised; CAS is proven sequentially only.**

- Evidence: `test_schema_created_with_wal_mode` (lines 58, 72) asserts
  `PRAGMA journal_mode == 'wal'`; `busy_timeout` is never asserted; no test
  opens a second connection or contends for the write lock.
  `test_claim_lease_is_compare_and_swap` (line 146) issues two claims back-to-back
  on one connection — it proves rowcount semantics, not a race between two
  finalizers.
- Violated: plan §H coverage row — *"store/SQLite | legacy-events DDL reuse +
  schema version; CAS state transitions; **WAL concurrency**; redaction-before-insert;
  journal query filters."*
- Severity: **NIT** (sqlite's single-writer behavior makes the sequential test
  persuasive; the must-cover item is still only half-demonstrated).
- Fix: add one two-connection test (second writer hits `busy_timeout` and
  recovers) plus a two-thread racing-claims test, or narrow §H's wording.

**N3 — `tests/test_gc.py:318-334`: wiring-only assertions inside `RegisterTests`.**

- Evidence: `test_register_wires_the_three_subcommands` asserts argparse field
  presence/defaults/callability (`hasattr(args, "host_change_authorized")`,
  `callable(args.func)`) without exercising behavior; the behavioral companions
  (flag-refused-before-spawn, status-runs-read-only) are separate tests.
- Violated: AGENTS.md testing guidance — *"Test negative paths and guards, not
  only success"* — the flag's own gate is tested elsewhere, so this block alone
  asserts structure a consumer never observes.
- Severity: **NIT**.
- Fix: shrink the test to the parse-shape facts the other cases do not already
  cover, or drive `--host-change-authorized` through to an emitted argv and drop
  the field assertions.

**N4 — `tests/test_power.py` (untracked, parallel lane): real home-subnet broadcast address in a test.**

- Evidence: three occurrences of `192.168.69.255` in the untracked file; the
  current `check-secrets.py`/`check-public.py` patterns do not match it (both
  guards pass with the file present), so it would land silently. Committed test
  material elsewhere uses only sanctioned placeholders (`aa:bb:cc:dd:ee:ff`,
  `192.0.2.x` TEST-NET, fixture lab net `10.66.0.0/24`).
- Violated: AGENTS.md — *"Before committing, do not include … host addresses,
  MAC addresses, VMIDs …"* (site material).
- Severity: **NIT** (uncommitted; will become a rule violation the moment it is
  tracked, with no guard to catch it).
- Fix (parallel lane): use `192.0.2.255`/`255.255.255.255` before committing.

**N5 — `ssh.py` allowlist vs §F/§D prose: sanctioned code extensions are not yet reflected in the plan text.**

- Evidence: `HOST_CHANGE_COMMANDS` now contains `tee` and `rm` (path-confined,
  `ssh.py:87`, `_confine` at `ssh.py:98-121`) and `check_allowed` exempts exactly
  `crontab -l` from the flag (`ssh.py:152-156`) — all three were required by the
  `gc.py` implementation task ("extend ssh.py's allowlist minimally and note it")
  and are committed (`f06d6a7`, `68a8a80`), but §D still enumerates the allowlist
  as *"`cat` (pxl temp files only), `install`, `crontab`, `shutdown`, `base64`,
  `true`"* and describes the host-changing subset as always flag-gated.
- Violated: plan §D — the enumeration and *"a command in
  `HOST_CHANGE_COMMANDS` additionally requires the explicit authorization flags"*.
- Severity: **NIT** (code sanctioned, documentation lagging).
- Fix: design session adds `tee`/`rm` (with confinement) and the `crontab -l`
  read-only exception to §D's allowlist paragraph; `docs/rework-plan.md` is not
  this auditor's file to edit.

---

## Targets verified clean (no finding)

1. **`store.py` vs §C** — legacy `events` DDL verbatim (`store.py:50-52` matches
   `id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, event TEXT, lease TEXT,
   vmid INTEGER, data TEXT`; byte-equivalent to the DDL pinned in
   `test_store.py:34-37`); `leases`/`resources`/`schema_meta` columns match §C;
   `PRAGMA journal_mode = WAL` + `busy_timeout = 5000` + `foreign_keys = ON`
   (`store.py:122-124`); `BEGIN IMMEDIATE` transaction helper (`store.py:151-160`)
   used by `register_resource` and schema init; CAS via `claim_lease`
   (`store.py:223-231`, `rowcount == 1`) and `heartbeat` (`… AND state = 'active'`);
   **redaction-before-insert proven**: the only `INSERT INTO events` in the
   package is `store.record` (`store.py:351-359`) and it wraps
   `redact_data(data)` — `test_record_redacts_secrets_before_insert` closes the
   DB and scans every byte on disk for the plaintext; `TERMINAL_STATES =
   ("ended","destroyed","abandoned")` (`store.py:35`) excludes
   `cleanup_failed`, and `owner_elsewhere` (`store.py:323-340`) shields
   `cleanup_failed` owners — tested both directions.
2. **`ssh.py` vs §D + AGENTS** — `check_allowed` runs before any spawn
   (`ssh.py:232` precedes `self._runner`), 8 refusal cases assert
   `runner.calls == []`; host-change gate for gc is doubled (module gate
   `gc.py:113-118` before any call, seam re-check per call) and tested;
   `_confine` rejects `..` outright and `posixpath.normpath`s before the prefix
   test for `cat`/`tee`/`rm` (`ssh.py:98-121`, refusal + benign-pin tests);
   `BatchMode=yes`/`ConnectTimeout=5` defaults (`ssh.py:192`); every call
   bounded (`timeout=None` → 30 s default, `ssh.py:233-234`; timeout kills the
   child → `TransportError`); quoting complete (`build_argv` quotes every
   element — metacharacter/unicode round-trip tested); among the new
   controller-side modules only `ssh.py` imports `subprocess`
   (`resources/pxl-gc.py` does too — legitimate: it is the host-side command
   runner; see D1 for the legacy exception).
3. **`proxmox.py` vs §D** — all three Q4 fallbacks degrade on `_USAGE_ERROR`
   only (`proxmox.py:47`): pct create `proxmox.py:220`, pct shutdown
   `proxmox.py:284`, `qm guest exec --synchronous` `proxmox.py:367`; each has a
   paired "non-usage failure raises, exactly one spawn" test; transfer is
   base64-via-guest-exec only (`push_bytes` `proxmox.py:440`, `pull_bytes`
   `proxmox.py:479`, guest-side paths `shlex.quote`d) and `test_proxmox.py:479-480`
   asserts `file-write`/`pct push` never appear in any spawned argv (repo-wide
   grep: forbidden paths survive only in dispositioned old-world modules
   `guest_agent.py`/`netgw.py`/`share.py` + `resources/*.sh` — CUT/REWRITE per §B);
   task waits carry deadlines (`wait_task` `proxmox.py:159`, async-exec poll),
   clone runs at `CLONE_TIMEOUT = 3600` (`proxmox.py:41`), create at 600 s.
4. **`resources/pxl-gc.py` vs §F** — AST scan: imports are exactly `fcntl, os,
   re, shlex, shutil, subprocess, sys, time, traceback`, **zero** package
   imports, **no `sqlite3`** (stateless — no controller DB reads anywhere);
   parse failure → warn-skip (`pxl-gc.py:132-139` + per-guest guard in `main`);
   pxl token gate precedes metadata parse; `template: 1` skipped before tags;
   `pxl-expiry=0` → long-term skip **and** counted `pinned` for the power-off
   verdict (`power_pass` `pxl-gc.py:283-296`); power-off requires stamp age
   `>= CLEAR_SECONDS = 600` (`pxl-gc.py:38`) on a second clear run, never the
   first; per-vmid flock; `--dry-run` logs without mutating; usage → exit 2,
   everything else 0 (`pxl-gc.py:319-383`); every §F claim has a stub-driven
   test (see §H table).
5. **`gc.py` + `tests/test_gc.py` vs §F** — crontab block is `# pxl-gc` +
   `*/10 * * * * /usr/local/sbin/pxl-gc >>/var/log/pxl-gc.log 2>&1`
   (`gc.py:46-48`, verbatim §F); install is checksum-idempotent (second run is
   read-only: `base64`, `install -d`, `crontab -l` — no tee/copy/write) and
   duplicate blocks collapse to one; uninstall strips marker+path lines
   (user lines preserved) and `rm -f`s script+staging idempotently; `status`
   passes `host_change=False` on both calls and its argvs pass `check_allowed`
   with no flag; `PolicyError` fires before the first ssh call in all four
   gate tests (`cmd`-level and core-level).

---

## §H claims: proven vs merely implemented (target 7)

| §H row / claim | Status in the five new test files | Where |
|---|---|---|
| ssh: argv construction + quoting | **Proven** — exact invocation, metacharacters survive as literals, unicode round-trip | `test_ssh.py` BuildArgvTests |
| ssh: timeout mapping | **Proven** — default 30.0 / override 5 recorded; timeout → `TransportError`, spawn failure → `TransportError` | RunStdin/Transport tests |
| ssh: allowlist refusal of arbitrary shell | **Proven** — 8 zero-spawn refusal cases + crontab/tee/rm confinement + `..` traversal refusals + benign pins | RunPolicy/CheckAllowed + `test_gc.py` SeamPolicyTests |
| store: legacy-events DDL reuse + schema version | **Proven** — exact legacy DDL opened and appended to; version-mismatch raises | `test_store.py` schema/legacy tests |
| store: CAS state transitions | **Proven sequentially** — second claim from same state loses, wrong-from-state loses, `rowcount` decides; **not raced** across connections/threads (no "two finalizers" race test; finalize orchestration itself not landed) | `test_claim_lease_is_compare_and_swap` |
| store: WAL concurrency | **Implemented only** — `journal_mode=wal` pragma asserted; no concurrent-access test; `busy_timeout=5000` never asserted (N2) | `test_schema_created_with_wal_mode` |
| store: redaction-before-insert | **Proven** — plaintext absent from every byte of the closed DB file; recursive redactor unit | `test_record_redacts_secrets_before_insert` |
| store: journal query filters | **Proven** — lease/since/limit combos, newest-first | `test_query_events_filters` |
| transfer: push/pull framing + base64 + guest file IO on FakeSSH | **Proven** — chunk argv/stdin order, `>` then `>>`, sha256-mismatch negatives both directions, forbidden-path absence | `test_proxmox.py` transfer tests |
| gc script: expired-only destroy, `expiry=0` skip, unparseable→warn-skip, idempotent, `--dry-run`, template refusal, two-run power-off ≥10 min | **Proven** — all seven, incl. injected clock at T/+599/+600 and running-guest block | `test_pxl_gc.py` (15 tests) |
| gc command: marker contract, idempotent install/uninstall, status read-only, gate-before-spawn | **Proven** — exact ordered argv sequences, stdin payloads, zero-call refusals | `test_gc.py` (19 tests) |
| leases / cleanup / power / guest lifecycle / mcp / diagnostics rows | **Not yet implemented** (phases 2–6; parallel lane in flight) — out of wave-1 scope; the store/ssh/proxmox primitives they rest on are covered above | — |

## Test-quality cross-checks (target 6)

- **Secrets/site material in committed wave-1 files:** clean — only sanctioned
  placeholders (`aa:bb:cc:dd:ee:ff`, `192.0.2.x`, fixture `10.66.0.0/24`,
  synthetic `10.0.0.5`, fixture vmids 900x/910x). B1 and N4 are parallel-lane.
- **Determinism:** the five wave-1 files open no sockets, spawn no network —
  `test_pxl_gc` spawns only the real script plus PATH-stubbed `sh` fakes,
  `test_ssh` constructs `TimeoutExpired`/`CompletedProcess` values without
  running anything, `test_proxmox`/`test_gc`/`test_store` are pure in-process.
  The only real-ssh spawn observed in the suite is D4 (parallel lane).
- **Mock-echo:** none of the five imports `unittest.mock`; assertions are on
  observable outcomes (DB reads, printed JSON, byte scans, spawned-argv
  sequences at the seam boundary — which is §H's stated contract). One
  wiring-only block remains (N3).

## Suite state at audit time (verbatim)

```
PYTHONWARNINGS=error python3.14 -m unittest tests.test_store tests.test_ssh tests.test_proxmox tests.test_pxl_gc tests.test_gc -q
Ran 100 tests in 3.685s
OK
```

Full `discover` currently aborts in an untracked parallel-lane file:
`TypeError: 'TextTestResult' object is not callable` at
`tests/test_cleanup.py:142` (no totals produced). Last complete discover:
`Ran 875 tests in 27.288s` / `FAILED (failures=12, errors=84, skipped=17)`.
Discover excluding the three untracked in-flight modules:
`Ran 732 tests in 14.956s` / `FAILED (failures=3, errors=21, skipped=17)` —
**0 of those failures are in the wave-1 five (100/100 green)**; red clusters in
`test_diagnostics` (deleted `longterm` import + real ssh), `test_console`
(store-backed `save_lease` WIP), `test_connection`, `test_onboarding`,
`test_pe`, `test_storage_guards` (same `cli.py` import), `test_diskactivity`
(real ssh), and a `test_audit` loader error (`ConfigError` not yet in
`errors.py`) — all parallel-lane or transitional, reported not fixed.
Guards: `compileall` clean, `check-public` passed, `git diff --check` clean,
`check-secrets .` **RED (B1)**.

## Verdict

Wave 1 is sound enough to build phases 2–3 on. The core artifacts measure up to
the plan where it matters: `store.py` implements §C literally (verbatim legacy
DDL, CAS claims, `BEGIN IMMEDIATE`, proven redaction-before-insert,
`cleanup_failed` non-terminal), `ssh.py` enforces allowlist-before-spawn with
bounded, quoted, BatchMode transports, `proxmox.py` degrades only on usage
errors and moves bytes exclusively through guest-exec base64, and both GC
halves match §F down to the crontab marker line, with every §F GC behavior
covered by deterministic stub-driven tests. None of the BLOCKER or the
determinism DRIFT originates in wave-1 files — they are parallel-lane
pre-commit hazards that must clear before that lane lands — and the three
wave-1-owned deltas are small and mechanical: reclassify `ethtool` (D2),
implement-or-amend §F's `gc status` scope (D3), update §D's allowlist prose for
the sanctioned `tee`/`rm`/`crontab -l` additions (N5), and add the missing
WAL-concurrency/CAS-race tests (N2) while `store.py`'s contract is still fresh.
The one structural risk carried forward is `host_transport.py` (D1): until its
eight consumers migrate, the "one seam" invariant is aspirational, so phase
planning should treat that migration — not just the phase-7 delete — as a
prerequisite for trusting the allowlist boundary.
