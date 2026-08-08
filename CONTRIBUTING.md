# Contributing

Thanks for helping turn spare hardware into safer research infrastructure.

## Before opening a change

- Keep the runtime package standard-library only and compatible with Python
  3.11+.
- Put values that differ between labs in `config.py`, with documentation and a
  safe default.
- Never commit credentials, host addresses, MAC addresses, VMIDs, device
  serials, private endpoints, captures, or runtime journals.
- Do not weaken lease ownership, auditing, expiry, cleanup, or verified
  shutdown invariants.
- For a destructive or protocol-level change, test the guard that should stop
  unsafe behaviour—not only the successful path.

Repository-specific engineering rules are in [AGENTS.md](AGENTS.md). Security
boundaries are documented in [docs/safety-policy.md](docs/safety-policy.md).

## Development setup

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m unittest discover -s tests -v
```

The runtime has no third-party dependencies. `pytest` is optional development
tooling; the canonical suite also runs directly through `unittest`.

## Required checks

```bash
PYTHONWARNINGS=error python3 -m unittest discover -s tests -q
python3 -m compileall -q src tests
python3 scripts/check-secrets.py .
python3 scripts/check-public.py .
git diff --check
```

Also run `bash -n` over changed shell scripts. Hardware-facing changes should
update [docs/VERIFICATION.md](docs/VERIFICATION.md) with exactly what was
observed and what remains unit-tested only.

## Pull requests

Keep each pull request focused. Explain the failure mode, the safety boundary
affected, how the change was tested, and whether real hardware was involved.
Never paste tokens, presigned links, captures, guest memory, or site topology
into an issue or pull request.
