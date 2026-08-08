# Repository operating rules

This repository is both a published Python package (`proxmox-agent-lab`) and
an agent skill. Treat it as software other people install, not as one site's
configuration.

## Never commit

- Credentials of any kind: tokens, passwords, private keys, presigned URLs,
  `.env` files, cloud-init secrets. Secrets belong in the OS keyring, reached
  through `secrets_store`, never in a file here.
- Anything describing one specific site: host addresses, MAC addresses, VPN
  endpoints, disk serials, Home Assistant URLs. Those go in the config file or
  `docs/site-notes.md`, both gitignored.
- The runtime journal. It lives in the state directory and records real host
  addresses and VMIDs.

`scripts/check-secrets.py` blocks the common shapes at commit time. Keep it
strict; add an entry to its `ALLOWED` list only for a genuinely public
constant, with a comment saying why.

## Structure

- `src/proxmox_agent_lab/` is the package. `cli.py` owns leases, the API
  client, auditing and the parser; each sibling module registers its own
  subcommands and calls back into it: `console` (VNC, serial, guest agent,
  transfer), `rfb`/`ws`/`des`/`png` (the self-contained VNC stack), `textmode`
  (opt-in text-mode OCR), `s3`, `storage`, `netgw`, `windows`, `memflow`
  (agentless guest introspection and live debugging), `usb` (passthrough and
  usbmon traffic sniffing) and `netcap` (network capture, SSL inspection, and
  MITM relay in a disposable LXC). `config`, `secrets_store` and `power` hold
  everything site-specific.
- `memflow`, `usb` and `netcap` are the exception to "everything goes through
  the API token": they run resident on the hypervisor and reach it over SSH
  (the `[memflow]` host connection), a deliberate opt-in trust boundary. The
  runtime package stays standard-library only -- their host-side tooling (Rust,
  `pxl-memflow`, `pxl-gdb`, Ghidra, `tcpdump`, mitmproxy) is installed on the
  host, or into a throwaway LXC, at setup time, never bundled into the package.
- `scripts/proxmox-lab` runs the package from a checkout; installing gives the
  same entry point on PATH.
- `docs/` is user-facing. `SKILL.md` is the agent skill.

## Rules

- Standard library only in the runtime package. It must work under any system
  Python 3.11+, with no install step beyond `pip install`.
- Nothing site-specific in code. If a value differs between two people's labs,
  it belongs in `config.py` with a default and documentation.
- Importing the package must never fail, whatever the config says. `init` and
  `doctor` have to run on a broken install.
- Test the guard, not just the happy path. A destructive or protocol-level
  change needs a test that fails when the guard is removed; a passive fake
  server will happily accept a client that skips a required message.
- Tests read `tests/fixtures/config.toml`, never the developer's own config.
- Do not weaken lease, ownership, audit, expiry, or host-shutdown invariants.
