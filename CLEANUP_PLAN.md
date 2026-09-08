# Simplicity and documentation cleanup plan

Review date: 2026-09-08. Baseline package version: 0.14.1.

The main recommendation is to separate responsibilities already visible in the
code, make documentation easier to enter, and repair test isolation before
moving lifecycle code. Preserve the current CLI and module callback registration
pattern. No new framework is needed.

This is a static maintainability review of the package structure, central code
paths, representative tests, documentation, and CI. It is not a complete security
audit or hardware validation. Actions below are recommendations, not implemented
changes. File sizes describe this review baseline and are not size targets.

## Findings that drive the plan

| Finding | Evidence | Consequence |
|---|---|---|
| CLI owns too many responsibilities | `src/proxmox_agent_lab/cli.py`: 2,970 lines; `ProxmoxAPI`, `audit`, `finalize_lease`, `cmd_doctor`, `parser` | Understanding one change requires navigating transport, policy, persistence, and presentation together. |
| Console includes unrelated workflows | `console.py`: 2,475 lines; `TermSession`, `agent_exec`, `_push_chunked`, `cmd_s3`, `_register_transfer` | File transfer and guest execution changes require knowledge of the screen subsystem. |
| Generic host access lives in a feature module | `memflow.py`: `host_run`, `require_host_ssh`, `host_read_bytes`; `usb.py` and `netcap.py` import it; `netcap.cmd_mitm_setup` calls `_mf._ssh` | Unrelated features depend on memory introspection internals. |
| Dependencies are implicit | `cli._module()` supplies the entire module to handlers; `lab: Any` is widespread; globals derive from configuration at import | Dependencies are hard to discover and moving functions can silently change patch/configuration behavior. |
| Tests share a predictable temporary directory | Import-time setup in `tests/test_console.py` and `tests/test_proxmox_lab.py` deletes and recreates the same directory | Concurrent test processes can interfere; import order affects state setup. This is a structural risk, not a reproduced failure. |
| Tests are grouped inconsistently | `test_console.py` contains DES, PNG, S3, Windows, storage, VPN, transfer, and console classes | Contributors cannot reliably infer where a test belongs from its filename. |
| Secret documentation contradicts implementation | README safety list and configuration introduction say OS keyring; `secrets_store.get` uses configured backend, environment fallback, then shared store | Operators can form an incorrect expectation about where secrets are read. `detect_backend` also claims an implicit legacy fallback that `get` does not perform. |
| Removal guidance has expired | `console.cmd_import_font`, `SKILL.md`, and `docs/AGENTS.md` refer to removal in 0.11.0; package is 0.14.1 | Readers cannot distinguish supported compatibility behavior from stale instructions. |
| Getting started competes with the full catalogue | README: 322 lines; SKILL: 430; console guide: 555; detailed recipes also appear in `docs/AGENTS.md` | The shortest safe path is harder to find and repeated explanations can drift. |

## Ordered action list

Priority describes order for this cleanup, not vulnerability severity. P1 should
precede structural changes; P2 is the main restructuring; P3 is conditional
follow-through. Effort is relative: S = localized, M = several files, L = central
behavior or many callers. Each row is independently reviewable unless its
dependencies say otherwise.

### P1: correct the reading experience and establish a safe baseline

| ID | Action | Why | Completion evidence | Effort |
|---|---|---|---|---|
| 1 | Correct secret-backend language in README, configuration introduction, `cli`/`secrets_store` docstrings, and related installation text. Describe `auto → env`, configured backend, env fallback, shared-store fallback, and the bootstrap-secret exception once. | Removes a confirmed contradiction without changing behavior. | Statements match `secrets_store.get`; other pages link to the canonical section. | S |
| 2 | Resolve the overdue OCR/import-font removal notice. Keep the error signpost with accurate wording unless a deliberate compatibility removal is approved for a release. | Removing a compatibility command as incidental cleanup could break callers. | Help, errors, tests, and docs agree on whether the command exists and what replaces it. | S |
| 3 | Qualify README lifecycle promises with long-term leases and cleanup/power-off failure outcomes. Link to the lifecycle guide. | “Nothing outlives a lease” and “nothing runs between jobs” omit supported persistent workloads and failure cases. | Introductory claims explain the ordinary case without promising unconditional deletion or shutdown. | S |
| 4 | Add `docs/README.md` as a small task-oriented index: install, first disposable guest, operate, troubleshoot, advanced features, contribute. Link it prominently from README. | Gives readers an entry point without learning the repository layout. | Each major document has a route from the index; the first-run path is obvious. | S |
| 5 | Shorten README to purpose, prerequisites, install link, one safe workflow, capabilities summary, and documentation links. Move extended use-case prose into a guide only where it adds information. | Reduces scanning and repeated claims. | A new reader can identify requirements and the first successful workflow near the top. | M |
| 6 | Give each recurring subject one owner: configuration in CONFIGURATION, installation in INSTALL, runnable lease skeleton in SKILL, enforced rules in safety-policy, hardware evidence in VERIFICATION, developer checks in CONTRIBUTING. | Existing audience boundaries are useful but repetition still causes drift. | Repeated explanations become links; brief safety reminders stay beside dangerous examples. | M |
| 7 | Establish a standard feature-page layout: purpose, prerequisites, minimal example, expected result, cleanup, common failures, advanced usage, verification status. Apply first to console, storage, and network. | Consistent pages answer practical questions faster than command catalogues. | Readers can complete the main workflow without opening advanced sections. | M |
| 8 | Consolidate troubleshooting into a short symptom-to-command guide linked from feature pages. Start with existing doctor/probe/screenshot/journal advice. | Avoids scattered recovery instructions and makes errors actionable. | Each symptom names a diagnostic command and the next decision, including inconclusive results. | M |
| 9 | Replace import-time deletion of the fixed test-state directory with a shared test bootstrap using a unique temporary root per process and scoped per-test state where needed. Preserve fixture configuration before imports. | Prevents cross-process interference and prepares tests for module moves. | Two simultaneous offline suite runs do not share state; cleanup affects only their own directories. | M |
| 10 | Record an offline baseline before refactoring: canonical suite, compilation, repository guards, release check, and packaging smoke checks. Record skipped tooling and test counts. | A baseline distinguishes introduced regressions from existing/environment failures. | Saved PR/check output identifies Python version, pass/fail/skip counts, and any missing tools. | S |

### P2: restructure around existing responsibilities

| ID | Action | Why | Completion evidence | Effort |
|---|---|---|---|---|
| 11 | Extract generic host SSH execution from `memflow.py` into `host_transport.py`, including bounded execution, binary reads, and capture decoding. Migrate USB, netcap, disk, and monitor-screenshot callers. | Removes the strongest misplaced dependency and private `_ssh` use. | Callers use a public transport interface; existing opt-in gates, quoting, timeouts, and redaction remain covered. | M |
| 12 | Move `LabError` and deliberate user-facing exception definitions toward `errors.py`; migrate incrementally to an explicit common error family. Review `_expected_errors` introspection and broad `ValueError` handling separately. | Error behavior becomes discoverable instead of depending on a hard-coded module scan. | Expected operational failures retain messages and exit codes; programming errors are not accidentally hidden. | M |
| 13 | Extract `ProxmoxAPI` and bounded task waiting into `api.py`. Pass required settings and audit/error dependencies explicitly at that boundary. | HTTPS transport can be understood and tested without parsing commands or loading lease orchestration. | TLS, error mapping, request/audit behavior, and task deadline tests pass; no guard moves after its side effect. | M |
| 14 | Extract local JSON persistence and controller/sweep locking into `state.py`, keeping lease-specific decisions elsewhere. | Separates file mechanics from lifecycle policy. | Atomic-write behavior, locking, configured paths, and failure behavior remain equivalent. | M |
| 15 | Extract audit facade duties from CLI into `audit.py`; keep journal storage and MariaDB work in their existing modules. | Redaction, spooling, ledger caching, and migration orchestration have a clear owner. | Existing redaction and ledger-outage tests pass; extracting code does not initialize a connection on import. | M |
| 16 | Move lease loading, ownership, expiry, registration, and lifecycle orchestration into `leases.py`; then extract cleanup/reclamation into `cleanup.py` if their dependency boundary is clear. | These are central concepts hidden in CLI today. | Guard tests preserve long-term protection, shared ownership checks, cleanup order, retries, and verified shutdown. | L |
| 17 | Move init/doctor/provisioning and update notices into `diagnostics.py` and a small `updates.py` if warranted. Keep parser wiring in CLI. | Diagnostic output and update caching should not obscure lifecycle code. | Missing/malformed config and missing optional runtime dependencies still permit help and diagnostics. | M |
| 18 | Keep `register(sub, lab)` and `_bind` dispatch. Document the callback contract, then add narrow structural typing only at extracted boundaries where it clarifies dependencies. | Makes dependencies clearer without replacing the existing command architecture or creating a giant context object. | No second dispatcher, service locator, or inheritance framework; existing command names and arguments remain stable. | M |
| 19 | Audit configuration-derived aliases and caches during each extraction. Use one process-wide cached configuration and explicitly supplied state paths; retain compatibility wrappers only where actual callers/tests require them. | Blindly copying globals into new modules creates stale values and breaks monkeypatch-based tests. | Fixture configuration and path patching affect the intended dependency; malformed-import tests remain green. | M |
| 20 | Move `TermFilter`/`TermSession` and serial attachment helpers into `serial.py`; move guest-agent execution/file-write/readiness primitives into `guest_agent.py`. Keep `GuestSession` channel selection in `guest.py`. | Separates terminal protocol and agent transport from graphical commands. | Protocol ordering and real exit-code/transcript behavior remain covered; transports do not import command modules. | L |
| 21 | Move push/pull, chunking, transfer script builders, and S3 command wiring to `transfer.py`; retain `s3.py` signing/storage primitives. | File transfers become a coherent feature with their own tests and guide. | CLI registration exposes the same commands; checksum, retry, chunk, and quoting tests still pass. | M |
| 22 | Leave screenshot capture, input actions, image handback, and inspection orchestration in `console.py` initially. Extract another module only if the remaining code has a clear independent responsibility. | Avoids turning one big file into many tightly coupled small ones. | Console has an understandable purpose and no longer owns transfer/agent/serial implementation. | S |
| 23 | Extract large embedded remote programs, starting with memflow's debugger script, into packaged resource files where this simplifies editing. Retain small shell builders near their feature. | Large programs inside Python strings are difficult to inspect and syntax-check. | Resource loading works from installed wheel and sdist; substitution, quoting, and script syntax tests pass. | M |
| 24 | Split mixed test files by responsibility and share only genuine protocol builders/fakes under `tests/support/`. Keep discovery compatible with direct unittest execution. | Test names and location should explain the behavior they protect. | Before/after test counts agree, no tests disappear from discovery, and active protocol assertions remain intact. | M |

### P3: finish consistency and prevent drift

| ID | Action | Why | Completion evidence | Effort |
|---|---|---|---|---|
| 25 | Add a concise architecture guide mapping CLI → feature handlers → transport/state services, and explain guard placement and one representative command. Link it from contributor guidance. | Future contributors need a map and an example rather than another large file list. | Each extracted module has one stated responsibility and dependencies match the code. | S |
| 26 | Add offline checks for internal documentation links and a curated set of documented CLI examples using parser-only validation. Do not execute operational examples in CI. | Catches moved links and stale flags cheaply. | Deliberately broken links/flags fail; shell placeholders and examples with pipelines are handled explicitly. | M |
| 27 | Consider generated command-reference pages from argparse only after documentation ownership is settled. Keep task guides handwritten. | Exact option lists drift; procedural explanations need editorial judgment. | Generated output is deterministic, requires no config/network/secrets, and has one reproducible update command. | M |
| 28 | Inventory legacy aliases, `_module()` path-loading shim, deleted-feature signposts, and old journal migrations. Remove only after checking callers and recording a support decision. | Historical compatibility can accumulate, but age alone is not evidence that removal is safe. | Each retained item has a reason; each removal has release notes and a migration path when needed. | M |
| 29 | Review repeated polling, subprocess wrappers, and exception fallbacks after the main extractions. Share helpers only where semantics actually match. | A universal retry/helper layer would conceal important timeout and failure differences. | Each consolidation removes real duplication without changing failure classification or extending deadlines. | M |
| 30 | Review result documentation, especially `guest.CommandResult.ok`, where an unknown exit code currently counts as no observed failure. Document unknown versus proven success; handle any API semantic change separately. | A concise field name can imply more certainty than its implementation provides. | Examples distinguish serial uncertainty from guest-agent exit status; no unannounced JSON contract change. | S |
| 31 | Reconcile local verification commands with CI, optionally through one small `scripts/check` entry point. Preserve individual commands for debugging. | Contributors should not maintain separate interpretations of required checks. | CONTRIBUTING and CI call the same checks, including packaging and relevant shell syntax verification. | M |
| 32 | Refresh VERIFICATION and release notes after implementation. Label pure moves as offline-tested; document any actual hardware observations separately. | Structural cleanup must not imply new hardware coverage or silently change support claims. | Claims are traceable to executed checks; commands, flags, and result changes are explicit. | S |

## Proposed code layout

Introduce files as their actions land, not as empty scaffolding. Keep the flat
package: the current size does not by itself justify a `core/`, `services/`, and
`commands/` hierarchy.

```text
src/proxmox_agent_lab/
  cli.py             # parser, registration, top-level invocation/error display
  errors.py          # deliberate operational errors
  config.py          # existing configuration loading/cache
  api.py             # Proxmox HTTPS transport and task waiting
  state.py           # local persistence and locks
  audit.py           # audit facade, redaction, spool orchestration
  journal.py         # existing journal implementation
  mariadb.py         # existing shared ledger client
  leases.py          # lease policy, ownership, registration, lifecycle
  cleanup.py         # cleanup/reclamation orchestration, if split is useful
  diagnostics.py     # init, doctor, provisioning diagnostics
  updates.py         # optional small update-cache module
  host_transport.py  # opt-in host SSH transport shared by features
  guest.py           # capabilities, channel selection, guest commands
  guest_agent.py     # qemu-guest-agent primitives
  serial.py          # terminal transport, filtering, attachment
  console.py         # screen capture, input, inspection commands
  transfer.py        # push/pull and transfer orchestration
  resources/         # only substantial standalone remote programs
  ...                # retain existing focused feature/protocol modules
```

The intended dependency direction is from command handlers toward transports
and state services. In particular, host transport must not depend on memflow,
and guest transports must not depend on graphical command handlers. Preserve
the existing callback pattern while migrating; avoid introducing a cycle
through convenience re-exports. Lease and cleanup boundaries deserve extra care:
pass required operations explicitly instead of having the two modules import
each other's orchestration functions.

Keep `rfb.py`, `ws.py`, `png.py`, `des.py`, `s3.py`, and binary parsing modules
focused. Do not combine them into a generic utilities module. Do not rewrite
protocols, replace argparse, add runtime dependencies, or migrate the test
framework as part of this cleanup.

## Proposed documentation layout

Keep existing document paths during the first pass so links and installed skill
references remain useful. Add navigation before considering directory moves.

| Reader need | Canonical destination | Restructuring reason |
|---|---|---|
| What is this and how do I start? | README → `docs/README.md` | Short introduction plus an obvious route to detail. |
| Install and configure | Existing INSTALL and CONFIGURATION | Keeps setup separate from everyday operation. |
| Perform one safe disposable task | SKILL lease skeleton plus a short first-run guide | One maintained lifecycle pattern, with explanation for humans. |
| Choose and operate a feature | Existing feature guides, consistent page template | Groups information by the task the reader is doing. |
| Fix a failure | New troubleshooting guide | Gives symptoms a single searchable home. |
| Understand enforced boundaries | Existing safety-policy | Prevents divergent copies of authorization rules. |
| Understand hardware coverage | Existing VERIFICATION | Separates observations from guarantees and unit tests. |
| Change the package | CONTRIBUTING plus new architecture guide | Separates development from lab operation. |
| Read historical review evidence | Keep dated audit report clearly marked historical | Prevents historical findings being mistaken for current status. |

Only introduce `guides/` or `reference/` folders later if navigation remains
unclear. If pages move, update all references, packaging paths, skill links,
and retained forwarding pages in the same change.

## Suggested implementation sequence

1. **Documentation corrections and navigation:** actions 1–8. Review rendered
   Markdown, links, and examples; no behavioral changes.
2. **Test foundation:** actions 9–10. Establish isolated state and a baseline.
3. **Host transport:** action 11, with its directly related tests. This is a
   useful bounded extraction before touching leases.
4. **Core boundaries:** actions 12–19 in several small PRs. Move API/state/audit
   first, then lease/cleanup. Keep mechanical moves separate from policy fixes.
5. **Guest and console boundaries:** actions 20–24 in separate transport and
   transfer PRs; compare test discovery counts for every test move.
6. **Documentation and maintenance finish:** actions 25–32. Architecture docs
   should also be updated alongside each extraction, not deferred entirely.

Each code PR should preserve command names, flags, exit codes, JSON fields,
configuration precedence, runtime-state format, and audit events unless a
separately described change is intentional. Run the canonical warning-clean
suite, compilation, secret/public/release guards, and diff checks; syntax-check
changed shell code. Resource/package changes also need clean wheel/sdist smoke
tests. Hardware-facing behavior changes require the appropriate authorized lab
validation and an exact VERIFICATION update.

Do not use a target line count as the completion criterion. Cleanup is complete
when a contributor can locate a command's policy, transport, tests, and guide
without tracing unrelated feature modules, and the existing safety contracts
remain demonstrably intact.
