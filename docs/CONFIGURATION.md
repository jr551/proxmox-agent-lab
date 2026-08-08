# Configuration

Two things to know: **site settings** live in a TOML file, **secrets** live in
your OS keyring. Nothing secret ever belongs in the config file.

## Where the config lives

Searched in this order:

1. `$PROXMOX_AGENT_LAB_CONFIG`
2. `./proxmox-agent-lab.toml` — handy inside a checkout
3. `$XDG_CONFIG_HOME/proxmox-agent-lab/config.toml`
4. `~/.config/proxmox-agent-lab/config.toml`

`proxmox-lab doctor` prints which file it actually loaded. Create one with
`proxmox-lab init`.

Runtime state — leases, screenshots, the journal — goes to
`~/.local/state/proxmox-agent-lab`, overridable with
`$PROXMOX_AGENT_LAB_STATE`. Nothing is written inside the installed package.

---

## `[proxmox]`

| Key | Default | Meaning |
|---|---|---|
| `host` | — | IP or hostname of the Proxmox machine |
| `port` | `8006` | API port |
| `node` | — | Node name, i.e. its hostname — not the FQDN |
| `token_user` | — | Token owner, e.g. `agent@pve` |
| `token_name` | — | Token id, e.g. `lab` |
| `verify_tls` | `false` | Verify the certificate. Off by default because a fresh Proxmox install is self-signed; turn it on once you have a trusted cert. |

## `[lease]`

| Key | Default | Meaning |
|---|---|---|
| `default_ttl_seconds` | `7200` | A lease not renewed within this window is swept by the watchdog |
| `idle_shutdown_seconds` | `28800` | Shut a reachable host down after this long with no activity and no lease |

A lease is the unit of accountability: writes require one, created guests are
registered to it, and ending it destroys them and powers the host off.

## `[power]`

How the machine is switched on. It cannot be the Proxmox API — the machine is
off.

```toml
[power]
mode = "wake-on-lan"
```

### `wake-on-lan` (default)

| Key | Meaning |
|---|---|
| `mac` | MAC of the **wired** NIC |
| `broadcast` | Your subnet's broadcast, e.g. `192.168.1.255`. `255.255.255.255` is also tried. |
| `wol_port` | `9` |
| `boot_timeout_seconds` | How long to wait for the API after waking |

Needs no extra hardware, but **cannot force the machine off**. That is usually
fine: graceful shutdown through the API is the normal path down. If it hangs,
the tool reports the failure rather than pretending.

### `home-assistant`

For a smart plug or a KVM that presses the power button.

| Key | Meaning |
|---|---|
| `home_assistant_url` | Base URL |
| `entity_on` | Script entity to switch on, e.g. `script.lab_power_on` |
| `entity_off` | Script entity that cuts power — the emergency finaliser |

Store the token: `proxmox-lab secrets set home-assistant-token`.

### `command`

Anything with a CLI — IPMI, a PDU, a cloud API.

```toml
mode = "command"
on_command  = "ipmitool -H bmc.lan -U admin -P … chassis power on"
off_command = "ipmitool -H bmc.lan -U admin -P … chassis power off"
```

Put credentials in a wrapper script, not here — this file is not secret.

### `none`

No remote power control. You switch it on; the tool still shuts it down.

## `[storage]`

| Key | Default | Meaning |
|---|---|---|
| `upload_storages` | `["local"]` | Storages this tool may upload into. An allowlist, so a typo cannot fill the wrong volume. |
| `bulk_storage` | `"local"` | Default target for images and ISOs |

If you add a big disk for images, set `bulk_storage` to it and include it in
`upload_storages`. See [storage.md](storage.md).

## `[network]`

Only needed for forced-VPN egress ([network.md](network.md)).

| Key | Default | Meaning |
|---|---|---|
| `lab_bridge` | `vmbr1` | Isolated bridge the guests sit on |
| `lab_network` | `10.66.0.0/24` | Its subnet |
| `lab_gateway_ip` | `10.66.0.1` | The gateway VM's address on it |
| `dhcp_start` / `dhcp_end` | `.50` / `.200` | DHCP range served to guests |
| `gateway_template_vmid` | `0` | VMID of a Debian/Ubuntu cloud-init template to clone |

## `[vpn]`

| Key | Meaning |
|---|---|
| `enabled` | Master switch |
| `address` | Tunnel address from your provider, e.g. `10.100.0.2/32` |
| `dns` | Resolver reachable through the tunnel |
| `endpoint` | `host:port` of the WireGuard server |
| `keepalive` | `25` suits most NATs |

Keys go in the keyring, never here:

```sh
proxmox-lab secrets set wg-private-key
proxmox-lab secrets set wg-preshared-key      # if your provider uses one
proxmox-lab secrets set wg-peer-public-key
```

The server's *public* key is in the keyring too. It is not secret, but keeping
every WireGuard-shaped string out of config and version control means a secret
scanner can flag all of them without exceptions.

## `[s3]`

Optional scratch bucket for moving files in and out of guests. Any
S3-compatible service works — MinIO, Garage, Backblaze, AWS.

| Key | Meaning |
|---|---|
| `enabled` | Master switch |
| `endpoint` | e.g. `https://s3.example.com` |
| `bucket` | Bucket name |
| `region` | `us-east-1` unless your provider says otherwise |

```sh
proxmox-lab secrets set s3-key-id
proxmox-lab secrets set s3-secret-key
```

Transfers use short-lived presigned URLs, so **no credential ever enters a
guest**.

## `[memflow]`

Advanced, opt-in. Enables agentless guest introspection and live debugging with
[memflow](https://github.com/memflow/memflow), USB traffic sniffing, and network
capture / SSL inspection / MITM relay. Unlike everything else here, these run
**resident on the Proxmox host** (or in a throwaway LXC it launches) and reach
it over **SSH** — a deliberately separate trust boundary from the API token — so
they stay off until you set both `enabled` and `ssh_host`. No patched kernel is
required. See [memflow.md](memflow.md), [usb.md](usb.md) and
[netcap.md](netcap.md).

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Master switch for memflow **and** USB sniffing |
| `ssh_host` | `""` | The host to reach (usually the Proxmox host) |
| `ssh_user` | `root` | Needs to read `/proc/<qemu-pid>/mem` and run `usbmon` |
| `ssh_port` | `22` | SSH port |
| `ssh_key` | `""` | Path to a private key file — never the key itself |
| `ssh_options` | `""` | Extra `ssh` options, space-separated |
| `connect_timeout` | `10` | SSH connect timeout, seconds |

```toml
[memflow]
enabled  = true
ssh_host = "192.168.1.50"
ssh_user = "root"
ssh_key  = "~/.ssh/pxl_vmi"
```

Prepare the host once with `proxmox-lab memflow host-setup
--host-change-authorized` (installs the memflow toolchain), then confirm with
`proxmox-lab memflow doctor`. The `usb` and `netcap` commands reuse this same
connection; `netcap`'s MITM proxy needs no host toolchain — it provisions a
disposable LXC on demand (`netcap mitm-setup`).

## `[windows]`

Windows installation clones a retained installer template from your own lab.
Template VMIDs are site inventory, so there are deliberately no universal
defaults:

| Key | Default | Meaning |
|---|---|---|
| `template_2025_vmid` | `0` | Retained Windows Server 2025 installer template |
| `template_2022_vmid` | `0` | Retained Windows Server 2022 installer template |

Set the template you use, or pass `windows install --template-vmid <id>` for a
one-off run.

## `[secrets]`

| Key | Default | Meaning |
|---|---|---|
| `backend` | `auto` | `keychain`, `secret-tool`, `env`, or `file` |
| `file_path` | — | Only for the `file` backend |

- **`keychain`** — macOS `security`.
- **`secret-tool`** — Linux libsecret (GNOME Keyring, KWallet). Install
  `libsecret-tools`.
- **`env`** — read `PROXMOX_AGENT_LAB_<NAME>`, e.g.
  `PROXMOX_AGENT_LAB_PROXMOX_TOKEN`. Read-only; good for CI and containers.
- **`file`** — a TOML file that must be `0600`. For headless boxes with no
  keyring. Least safe; a keyring is better where one exists.

An environment variable always overrides the chosen backend, so you can point
a single install at a different lab for one command.

## `[audit]`

| Key | Default | Meaning |
|---|---|---|
| `backend` | `sqlite` | Local audit backend: `sqlite` or `jsonl` |
| `journal_dir` | `<state>/journal` | Local audit ledger directory |
| `git_sync` | `false` | Copy each redacted event to a private git log |
| `git_repo` | — | Dedicated private logging checkout |
| `git_branch` | `logs` | Remote branch receiving logging commits |

Every action appends a redacted event: what happened, to which VMID, under
which lease. Passwords, tokens, typed text and presigned URLs are never
recorded — only counts, exit codes and object keys.

`git_sync` is off by default. Most people do not want their lab's audit trail
pushed anywhere. If you enable it, point `git_repo` at the root of a clean,
**private, logging-only checkout**: the journal records host addresses and
VMIDs. The local backend can remain `sqlite`; the logging checkout receives
one JSONL file per day. Sync fails closed if that checkout contains any other
uncommitted file, and only `journal/YYYY-MM-DD.jsonl` is ever staged.

---

## A complete example

```toml
[proxmox]
host = "192.168.1.50"
node = "pve"
token_user = "agent@pve"
token_name = "lab"

[power]
mode = "wake-on-lan"
mac = "d8:5e:d3:11:22:33"
broadcast = "192.168.1.255"

[storage]
upload_storages = ["local", "bulk"]
bulk_storage = "bulk"

[lease]
default_ttl_seconds = 7200
```

That is a fully working lab. Everything else is opt-in.
