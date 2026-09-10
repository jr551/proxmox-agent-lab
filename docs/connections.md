# Share a lab connection

On a configured controller, generate a setup block for another dev machine or
agent:

```bash
proxmox-lab connection export --out connection-setup.sh
```

The new file is mode 600 and contains credentials. Give its contents to the
recipient privately. Pasting it into bash or zsh finds Python 3.11+, installs
the matching release in a user virtual environment, and imports the connection.
The recipient must already be able to reach the host, over the LAN or Tailscale.
The final output prints the installed CLI path to use for `doctor`.

The handoff carries the effective configuration, API token, available audit
bootstrap credential, and credentials for enabled power, S3, VPN and ngrok
features. Missing optional credentials are listed. A configured TLS CA is
included and relocated. Leases, runtime state and controller identity are not
copied. Host SSH is disabled on the recipient unless you explicitly include
its configured private key:

```bash
proxmox-lab connection export --include-ssh-key --out connection-setup.sh
```

SSH options are not copied because they often reference local programs or
paths. Passphrase-protected keys still need an unlocked agent on the recipient.
Custom power commands must be installed separately on that machine.

Import writes a new private directory with separate config and secrets files;
credentials never appear in the generated config, subprocess arguments or
audit events. Existing configuration directories are refused, not overwritten.
This transfers the existing credentials; it does not create separately
revocable API identities for each recipient.

For machines with the CLI already installed, including Windows, transfer JSON
and import it on stdin instead:

```bash
proxmox-lab connection export --format json --out connection.json
proxmox-lab connection import < connection.json
```

To keep an existing connection, use `connection import --directory` with a new
directory, then set `PROXMOX_AGENT_LAB_CONFIG` to the returned config path.
POSIX files are created with mode 600 and the directory with mode 700; Windows
users should use their private user profile directory and its normal ACLs.

## Optional Tailscale during host setup

`proxmox-host-setup.sh` asks whether to install/connect Tailscale on the host.
The prompt works even when running the script through `curl | bash`. Enter
skips it. Unattended setup skips it unless `PXL_TAILSCALE=yes` is set; use
`PXL_TAILSCALE=no` to suppress the question explicitly.

Accepted setup follows the [official Linux installation flow](https://tailscale.com/docs/install/linux)
and shows a login URL when authentication is needed. Installation and login
have deadlines. An existing running Tailscale connection is reused. The script
does not enable subnet advertisement, exit-node service or Tailscale SSH.
After a successful connection the printed Proxmox address uses the Tailscale IP.
Join each dev machine to the tailnet and allow it access to the lab services.

Tailscale cannot wake a powered-off host. Remote development still needs a
reachable smart-plug power service, LAN wake relay, or a host kept running by
an explicitly long-term lease. Enabled services such as S3 also need reachable
addresses; a host Tailscale connection does not automatically expose guest
networks.
