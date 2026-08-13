# Installation

From a spare PC to a working lab. Budget about an hour, most of it waiting for
the Proxmox installer.

If you already run Proxmox, skip to [step 3](#3-install-the-controller).

---

## 1. Install Proxmox on the spare PC

Download **Proxmox VE 8 or 9** from
[proxmox.com/downloads](https://www.proxmox.com/en/downloads), write it to a
USB stick, and install. Two choices matter:

- **Hostname.** Whatever you pick becomes the *node name* you put in the
  config. `pve` is the default; the short name is what you want, not the FQDN.
- **Network.** Give the machine a **static IP**, or a DHCP reservation. If its
  address moves, nothing here can find it.

After it reboots, check you can reach `https://<ip>:8006` in a browser. The
certificate warning is expected — it is self-signed.

> Proxmox nags about a subscription on login. It is free to use; the dialog is
> harmless.

## 2. Prepare the machine to be woken and shut down

This is the part people skip and then wonder why the lab never turns on.

**Enable Wake-on-LAN in the BIOS.** Reboot into firmware setup and look for
*Wake on LAN*, *Power On By PCI-E*, or *Resume by LAN* — usually under Power
Management. Turn it on.

**Note the MAC address** of the wired NIC. On the Proxmox console:

```sh
ip -br link show
```

Take the MAC of the interface with your LAN IP — usually `enp*` or `eno*`, not
`vmbr0`.

**Check WoL is armed on the NIC.** Some cards need it enabled per boot:

```sh
apt install -y ethtool
ethtool <interface> | grep Wake-on      # want: Wake-on: g
```

If it says `Wake-on: d`, arm it and make it stick:

```sh
ethtool -s <interface> wol g
printf '#!/bin/sh\nethtool -s %s wol g\n' <interface> > /etc/network/if-up.d/wol
chmod +x /etc/network/if-up.d/wol
```

Wi-Fi cannot do this. Use the wired port.

> **No Wake-on-LAN?** A smart plug through Home Assistant, an IPMI/BMC, or any
> script that switches the machine on all work — see
> [CONFIGURATION.md](CONFIGURATION.md#power). Wake-on-LAN is only the default
> because it needs no extra hardware.

## 3. Create an API token

### 🚀 The quick way

Run this **on the Proxmox host, as root**. It creates the user and token,
grants exactly the right privileges, arms Wake-on-LAN so it survives reboots,
and prints the config block to paste on your laptop:

```sh
curl -fsSL https://raw.githubusercontent.com/jr551/proxmox-agent-lab/main/proxmox-host-setup.sh | bash
```

Add `PXL_ALLOW_HOST_ADMIN=1` before `bash` if you want disk management or the
VPN gateway — that grants the token permission to change host configuration,
so it is opt-in.

It is safe to re-run and touches nothing that already exists. Then skip to
[step 4](#4-install-the-controller).

### 🔧 Or by hand

Everything talks to Proxmox as a restricted token, never as root.

In the Proxmox web UI:

1. **Datacenter → Permissions → Users → Add**: user `agent`, realm `pve`.
2. **Datacenter → Permissions → API Tokens → Add**: user `agent@pve`, token id
   `lab`. **Leave "Privilege Separation" ticked.**
   Copy the secret now — it is shown once.
3. **Datacenter → Permissions → Add → API Token Permission**:
   path `/vms`, token `agent@pve!lab`, role `PVEVMAdmin`, Propagate on.

Because privilege separation is on, the token does **not** inherit the user's
permissions. If you also grant the *user* a role, grant the *token* the same
one — a mismatch here is the single most common setup problem.

Equivalent on the Proxmox shell:

```sh
pveum user add agent@pve
pveum user token add agent@pve lab --privsep 1
pveum acl modify /vms --tokens 'agent@pve!lab' --roles PVEVMAdmin
```

## 4. One-time controller setup

On the machine you drive from — your laptop, not the lab host — run the
guided installer:

```sh
curl -fsSL https://raw.githubusercontent.com/jr551/proxmox-agent-lab/main/install.sh | bash
```

It installs the isolated CLI where `pipx` is available, asks for the Proxmox
address, node, API-token identity, power details, and audit backend, stores
secrets in the OS keyring, writes a mode-600 config, and runs `doctor`.
Re-run it with `--configure` to safely replace configuration answers:

```sh
curl -fsSL https://raw.githubusercontent.com/jr551/proxmox-agent-lab/main/install.sh | bash -s -- --configure
```

The script never writes the Proxmox or PocketBase token to TOML. Enter each
secret only at its local hidden prompt. For unattended provisioning, use the
documented `PXL_*` variables only in a protected CI secret environment.

### Optional: host PocketBase on Proxmox

When the installer asks for an audit backend, choose `pocketbase`, then
`proxmox` if you want the lab host to run the service. It prints this command
to run **as root on the Proxmox host**:

```sh
curl -fsSL https://raw.githubusercontent.com/jr551/proxmox-agent-lab/main/pocketbase-host-setup.sh | bash
```

The host script asks for an LXC ID, storage, bridge, IP configuration, HTTP
port, and first superuser. It creates a persistent unprivileged Debian LXC,
installs PocketBase as a restricted systemd service, and prints the dashboard
and API URLs. It does not modify existing guests or the host firewall.

The default service is HTTP for a trusted LAN; do not port-forward it. Put a
TLS reverse proxy in front of it before access from an untrusted network. In
the PocketBase dashboard create a separate nonrenewable superuser
impersonation token for each controller. Re-run controller setup, select
`existing`, enter the printed API URL, and enter that controller's token at
the hidden local prompt.

### Copy this as the first message to your installation agent

```text
Install proxmox-agent-lab as a first-stage task. First ask me only for the
non-secret setup choices: Proxmox address, node, API-token user and name,
power method, and whether audit storage should be SQLite, JSONL, an existing
PocketBase service, or a new PocketBase LXC on the Proxmox host. Never ask me
to paste a token or password into chat, config, command arguments, or an
environment variable. Run the project’s guided install.sh locally so its
hidden prompts store secrets only in the OS keyring, then run
`proxmox-lab doctor` and report every remaining issue exactly.

If I choose a new PocketBase LXC, show me the root-only
`pocketbase-host-setup.sh` command and wait for me to run it on the Proxmox
host. Do not expose the HTTP port to the Internet or change host firewall
rules. After I provide the resulting trusted-LAN API URL, finish the
controller setup, create/validate the private audit collection, and report
the connection details another controller needs: API URL, collection name,
and the instruction to create a separate nonrenewable PocketBase
impersonation token for that controller.
```


## 5. Check it

```sh
proxmox-lab doctor
```

This is the command to trust. It reports the config file in use, the secrets
backend, whether the token is stored, whether Proxmox answers, and which
privileges the token is missing. Fix anything it lists before going further.

`"proxmox_reachable": false` is *correct* if the machine is powered off.

Now try the real thing:

```sh
proxmox-lab status        # wakes nothing, just looks
proxmox-lab power-on      # sends the magic packet, waits for the API
```

If power-on times out: the machine did not wake (BIOS/`ethtool`), the broadcast
address is wrong for your subnet, or your router drops directed broadcasts —
try `broadcast = "255.255.255.255"`.

## 6. Your first lease

```sh
L=$(proxmox-lab lease-begin --purpose "first run" | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
proxmox-lab status
proxmox-lab lease-end --lease "$L"
```

`lease-end` must print `"host_powered_off": true`. That is the whole promise of
this tool: when the work is done, the machine is off.

## 7. Install the watchdog (recommended)

If the controller crashes or you close the laptop mid-run, nothing would clean
up. The watchdog sweeps expired leases and shuts an idle host down.

**macOS:**

```sh
./scripts/install-watchdog
```

**Linux (systemd user units):**

```sh
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/proxmox-lab-watchdog.service <<'EOF'
[Unit]
Description=proxmox-agent-lab watchdog

[Service]
Type=oneshot
ExecStart=%h/.local/bin/proxmox-lab cleanup-expired
EOF
cat > ~/.config/systemd/user/proxmox-lab-watchdog.timer <<'EOF'
[Unit]
Description=Run the proxmox-agent-lab watchdog every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
EOF
systemctl --user enable --now proxmox-lab-watchdog.timer
loginctl enable-linger "$USER"     # keep it running when logged out
```

## Optional extras

None of these are needed for a working lab.

| Feature | Why | Guide |
|---|---|---|
| Cloud-image templates | Fast throwaway Linux guests | [storage.md](storage.md) |
| S3 scratch bucket | Move files in and out of guests | [storage.md](storage.md) |
| Forced-VPN egress | All lab traffic leaves via WireGuard | [network.md](network.md) |
| Windows guests | Install Server 2022/2025 | [windows.md](windows.md) |

## Troubleshooting

**`doctor` says the token is missing but you stored it.** You probably have two
config files. `doctor` prints `config_file` — check it is the one you edited.
`$PROXMOX_AGENT_LAB_CONFIG` overrides everything.


**An agent says `mcp:proxmox-complete` or `proxmox-mcp-wrapper` is missing.**
That is a stale local agent integration, not a lab failure. Use the installed
`proxmox-lab` CLI and this guide’s first-stage prompt; refresh the agent skill
before relying on an MCP entry that points at an absent wrapper.

**HTTP 401.** The token secret is wrong, or `token_user`/`token_name` do not
match. The secret is the UUID shown once at creation, not the token id.

**HTTP 403 on things that should work.** Privilege separation again: grant the
role to the *token*, not just the user.

**Everything works, then stops after a reboot.** `ethtool wol g` did not
persist — see step 2.

**The machine wakes on its own.** Something else on your network is sending
traffic that wakes it. That is a BIOS setting (*Wake on PME*, *Wake on Ring*),
not this tool.
