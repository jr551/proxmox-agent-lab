# Support

Start with the documentation:

- [Installation](docs/INSTALL.md)
- [Configuration](docs/CONFIGURATION.md)
- [Agent operating guide](docs/AGENTS.md)
- [Hardware verification status](docs/VERIFICATION.md)
- [Safety policy](docs/safety-policy.md)

For setup and runtime failures, run `proxmox-lab doctor`, `guest probe`, and
`journal --limit 20` before opening an issue. Include sanitized output and the
package version, but remove credentials, addresses, VMIDs, captures, journals,
and site topology.

Use GitHub issues for reproducible bugs and feature proposals. Use private
vulnerability reporting for security issues, as described in
[SECURITY.md](SECURITY.md).

The project is maintained on a best-effort basis; there is no guaranteed
response time for general support.
