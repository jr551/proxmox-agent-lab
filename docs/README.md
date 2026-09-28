# Documentation index

Use this page to find the right guide for what you are doing.

## Install and configure

- **[INSTALL.md](INSTALL.md)** — blank PC → working lab: Proxmox, Wake-on-LAN,
  `ssh-copy-id`, `proxmox-lab init`, the `doctor` checklist, and the optional
  lease garbage collector.
- **[CONFIGURATION.md](CONFIGURATION.md)** — every key of the TOML schema with
  its default and meaning, plus the lookup order and environment overrides.

## Run your first guest

Take a lease before anything else — the canonical shape lives in
**[SKILL.md](../SKILL.md)**:

```bash
L=$(proxmox-lab lease-begin --purpose "first run" \
    | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
trap 'proxmox-lab lease-end --lease "$L"' EXIT
```

- **[AGENTS.md](AGENTS.md)** — how an agent should choose channels, read
  screens, and avoid common traps.

## Operate and read

- **[commands.md](commands.md)** — map of every `proxmox-lab` subcommand
  (generated from the parser with `scripts/gen-commands.py`).
- **[long-term-leases.md](long-term-leases.md)** — machines that stay on:
  `pxl-expiry=0` semantics, the GC exemption, and `lease-destroy`.
- **[troubleshooting.md](troubleshooting.md)** — symptom → diagnostic command →
  next decision. Start here before blaming the tool.
- **[VERIFICATION.md](VERIFICATION.md)** — what has been run on real hardware
  and what is unit-tested only.

## Rules of the road

- **[safety-policy.md](safety-policy.md)** — the rules the code enforces:
  leases, ownership, authorization flags, verified shutdown.

## Contribute

- **[architecture.md](architecture.md)** — how the module tree fits together
  and the contracts that keep it that way.
- **[CONTRIBUTING.md](../CONTRIBUTING.md)** — development setup, required
  checks, and the release checklist.
- **[AGENTS.md](../AGENTS.md)** (repository root) — architecture, conventions,
  and important files for contributors.
- **[AUDIT-2026-08-24.md](AUDIT-2026-08-24.md)** — historical security review
  (kept as evidence; some findings predate the rework).
