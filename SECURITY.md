# Security policy

Old Computer AI Lab controls hypervisors, guest machines, networks, storage,
and optional host-side debugging tools. Please treat vulnerabilities in its
authorization, isolation, secret handling, cleanup, or console-sharing paths
as sensitive.

## Supported versions

Until 1.0, security fixes are made on the latest release and the `main` branch.
Older pre-1.0 versions may not receive backports. Fixes are published via
[GitHub Releases](https://github.com/jr551/proxmox-agent-lab/releases) (see also
[CHANGELOG.md](CHANGELOG.md)).

## Reporting a vulnerability
Do not open a public issue for a suspected vulnerability. Use the repository's
[private vulnerability reporting](https://github.com/jr551/proxmox-agent-lab/security/advisories/new)
form instead.

Include the affected command or module, expected safety boundary, reproduction
steps using synthetic values, and impact. Do not include real credentials,
host addresses, VMIDs, captures, VM captures, memory contents, presigned URLs,
journals, or private lab topology and site topology — see also
[SUPPORT.md](SUPPORT.md) for sanitized-output guidance.

Reports are handled on a best-effort basis. A coordinated fix and disclosure
timeline will be agreed after the report is reproduced.

## Scope

Particularly important areas include:

- lease ownership, expiry, cleanup, and verified host shutdown;
- deletion or mutation of resources not created by the active lease;
- host-change and live-memory mutation authorization gates;
- secret redaction, keyring access, presigned transfers, and audit records;
- console-share token scope, expiry, revocation, and path traversal;
- VPN fail-closed behaviour and cross-network isolation.

Vulnerabilities in Proxmox, operating systems, analysis tools, tunnel
providers, or hardware should be reported to their respective maintainers.
