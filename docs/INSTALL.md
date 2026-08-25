# Installation

> **Two paths**
>
> - **Fastest:** `proxmox-host-setup.sh` on the Proxmox host (as root) → `install.sh` on your laptop → `proxmox-lab doctor`.
> - **Full manual:** BIOS/WoL (step 2) → manual API token (step 3 · by hand) → same `install.sh` → `doctor`.
>
> Budget about an hour, most of it waiting for the Proxmox installer.

**Hard prerequisites** — fail fast if any is missing:

- **Controller:** Python 3.11+ (`install.sh:70` probes `python3.13` → `python3.12` → `python3.11` → `python3`), `pipx` if available otherwise `pip --user` fallback (`install.sh:96-104`).
- **Network:** wired NIC with Wake-on-LAN and a static IP (or DHCP reservation) for the Proxmox host.
- **Proxmox:** VE 8 or 9 on the spare PC.

If you already run Proxmox, skip to [step 3](#3-create-an-api-token).

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
[step 4](#4-one-time-controller-setup).

> Same one-liner is in [README.md](../README.md#-install) — detail stays here so this guide is self-contained.

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

It installs the CLI isolated via `pipx` when available, otherwise `pip --user`
(`install.sh:96-104`), asks for the Proxmox address, node, API-token identity,
power details, audit backend, and S3 scratch bucket, stores secrets in the OS
keyring, writes a mode-600 config, and runs `doctor`. If you want an agent to
drive the install, copy the template in [Agent first message](#agent-first-message).

Re-run it with `--configure` to safely replace configuration answers
(`install.sh:33-36` defines `--configure`/`--yes`; `install.sh:44-49` reads
`PXL_*` before prompting; without `--configure` it asks via `PXL_RECONFIGURE`):

```sh
curl -fsSL https://raw.githubusercontent.com/jr551/proxmox-agent-lab/main/install.sh | bash -s -- --configure
```

The script never writes the Proxmox token to TOML. Enter each
secret only at its local hidden prompt. For unattended provisioning, add `--yes`
(`install.sh:31,35` — skips prompts) and supply `PXL_*` variables only in a
protected CI secret environment.

#### Non-interactive `PXL_*` variables (see [`install.sh:10-19`](../install.sh#L10-L19))

| Variable | Purpose |
|---|---|
| `PXL_HOST` | Proxmox IP/hostname |
| `PXL_NODE` | Node name (hostname, e.g. `pve`) |
| `PXL_TOKEN_USER` | API token user (`agent@pve`) |
| `PXL_TOKEN_NAME` | API token name (`lab`) |
| `PXL_TOKEN_SECRET` | API token secret (keyring, not TOML) |
| `PXL_MAC` | Wired NIC MAC for Wake-on-LAN |
| `PXL_S3_BACKEND` | `none` / `existing` / `lxc` |
| `PXL_S3_ENDPOINT` | S3 endpoint (when `existing`) |
| `PXL_S3_BUCKET` | Bucket name |
| `PXL_S3_REGION` | Region (`us-east-1`) |
| `PXL_S3_KEY_ID_SECRET` | S3 access-key ID (keyring) |
| `PXL_S3_SECRET_KEY_SECRET` | S3 secret access key (keyring) |
| `PXL_ALLOW_HOST_ADMIN` | `proxmox-host-setup.sh` only — `1` grants host-admin (node/storage) |
| `PXL_RECONFIGURE` | `y` forces reconfigure without `--configure` prompt |

All `PXL_*` names match `ask VAR` in `install.sh` via `PXL_${VAR}` (`install.sh:43-44`). Secrets (`*_SECRET`) are piped to `proxmox-lab secrets set --stdin` and never written to config. For the host-setup flags see [`proxmox-host-setup.sh:27-29`](../proxmox-host-setup.sh#L27-L29) (`PXL_USER`/`PXL_TOKEN`/`PXL_ROLE`) and `PXL_ALLOW_HOST_ADMIN`.

> **No install at all?** `bootstrap.sh` (same one-liner in [README.md](../README.md#-install)) builds a throwaway venv under `$TMPDIR/proxmox-agent-lab-env` and prints its `proxmox-lab` path — handy for an agent with no checkout: `PXL=$(curl -fsSL https://raw.githubusercontent.com/jr551/proxmox-agent-lab/main/bootstrap.sh | sh) && "$PXL" doctor` (`bootstrap.sh:25-52`). This guide uses `install.sh`; bootstrap is the escape hatch.

### The audit ledger

The ledger is MariaDB in a persistent container on the Proxmox host, and it is
required — there is no local-only mode. Provision it once, from any
controller, after `install.sh` has finished:

```bash
proxmox-lab journal host-setup --host-change-authorized
```

That creates the container (unprivileged, `onboot`, not lease-owned so
lease-end never destroys it), publishes it on the hypervisor's own address,
creates the database, and copies this controller's existing secrets into the
shared store. It prints one line:

```bash
export PROXMOX_AGENT_LAB_MARIADB_PASSWORD='...'
```

Paste that into the environment of every other controller. It is the only
credential they need; everything else is read from the shared store. Treat it
as the master secret.

To set the host up directly instead, `mariadb-host-setup.sh` is the same
script:

```sh
curl -fsSL https://raw.githubusercontent.com/jr551/proxmox-agent-lab/main/mariadb-host-setup.sh \
  | CTID=9310 STORAGE=local-lvm DBPASS="$(openssl rand -base64 24)" bash
```

Upgrading a controller from an older release needs nothing: the first command
after the upgrade carries its old local ledger into the shared one, and doing
that on a second machine adds only the events the first did not already have.

### Optional: host MinIO on Proxmox

The S3 scratch bucket (see [storage.md](storage.md#s3-scratch-bucket)) moves
files in and out of guests. If you don't already run an S3-compatible
service, the installer can point you at one it creates for you. When it asks
for the S3 backend, choose `lxc`. It prints this command to run **as root on
the Proxmox host**:

```sh
curl -fsSL https://raw.githubusercontent.com/jr551/proxmox-agent-lab/main/minio-host-setup.sh | bash
```

> Same one-liner appears in [README.md](../README.md#-install) — detail stays here so this guide is self-contained.

The host script asks for an LXC ID, storage, bridge, IP configuration, disk
size, bucket name, and access key. It creates a persistent unprivileged
Debian LXC set to start automatically whenever the Proxmox host does — this
lab powers its host off between leases, so nothing else would start the
container back up — installs a single-binary MinIO server as a restricted
systemd service (S3 API only — the browser console is disabled), creates
the bucket, and prints the endpoint, bucket, region, and credentials.
> **Trusted LAN only** — see the canonical warning in [storage.md](storage.md#s3-scratch-bucket). The host setup script prints the same warning.
Re-run controller setup, select `existing`, enter the printed endpoint, bucket and
region, and enter the printed access key and secret key at their hidden
local prompts.

### Agent first message

```text
Install proxmox-agent-lab as a first-stage task. First ask me only for the
non-secret setup choices: Proxmox address, node, API-token user and name,
power method, and whether the S3 scratch bucket should be skipped, an existing
bucket, or a new MinIO LXC on the Proxmox host. Never ask me to paste a token
or password into chat, config, command arguments, or an environment variable.
Run the project's guided install.sh locally so its hidden prompts store
secrets safely, then run `proxmox-lab doctor` and report every remaining issue
exactly.

The audit ledger is MariaDB on the Proxmox host and is required. After
install.sh, run `proxmox-lab journal host-setup --host-change-authorized` and
report the single `export PROXMOX_AGENT_LAB_MARIADB_PASSWORD=...` line it
prints, which is what every other controller needs. Do not expose port 3306 to
the Internet or change host firewall rules.

If I choose a new MinIO LXC, show me the root-only `minio-host-setup.sh`
command and wait for me to run it on the Proxmox host. Do not expose the S3
port to the Internet or change host firewall rules. After I provide the
resulting trusted-LAN endpoint, bucket, region, access key, and secret key,
finish the controller setup and confirm both secrets landed in the OS
keyring, never in chat, config, or command arguments.
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

**macOS (from a checkout):**

```sh
./scripts/install-watchdog
```

> Verified: `scripts/install-watchdog` exists in the checkout. It writes
> `~/Library/LaunchAgents/lol.rowe.proxmox-agent-lab-watchdog.plist` with
> `Label` `lol.rowe.proxmox-agent-lab-watchdog` (personal/hardcoded — see
> `scripts/install-watchdog:5,17`) and `StartInterval` 300 s / `RunAtLoad`,
> running `scripts/proxmox-lab cleanup-expired`.

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

**`journalctl -u pvedaemon` is full of `writing cluster log failed:
ipcc_send_rec[7] failed: Invalid argument`.** Expected on a standalone node and
safe to ignore: pmxcfs has no cluster to write the entry to, so every task
completion logs one. The lab node produced about 5,300 in 24 hours. It is host
behaviour, not this tool — but it does bury genuine pvedaemon errors, so filter
it when reading logs:

```bash
journalctl -u pvedaemon | grep -v "writing cluster log failed"
```

**`ps` on the node shows a zombie `qm terminal` under `termproxy`.** Cosmetic,
and not something the controller can reap. `termproxy` is spawned by
**pvedaemon** when a console session is requested, and it spawns
`/usr/sbin/qm terminal` as its own child — so the reaping parent is a Proxmox
binary, not this tool. Observed on the node: opening a session created
`termproxy` (parent: pvedaemon) plus its `qm terminal` child, and closing the
session removed both, leaving nothing behind. A long-lived `console bridge` is
therefore the case where children can accumulate; ending the bridge clears
them. Nothing here parses `ps`, so no summary of "is anything running" is
misled by a zombie.

**Is the node itself up to date?** Nothing in the lease workflow shows it, so
ask explicitly:

```bash
proxmox-lab doctor --host-checks
```

It reports `updates_pending`, `security_updates` and `reboot_required` as
advisory fields — a pending security update is something to schedule between
leases, not a reason for `doctor` to fail. It needs the opt-in `[memflow]` host
SSH channel, since there is no API for `apt`. Patching and rebooting stay
manual and deliberately outside this tool.

**The machine wakes on its own.** Something else on your network is sending
traffic that wakes it. That is a BIOS setting (*Wake on PME*, *Wake on Ring*),
not this tool.
