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

`verify_tls` is one switch for every connection to the node: REST calls, the
VNC and serial console WebSockets, and `upload`'s curl invocation. Turning it
on leaves no unverified path behind — earlier versions verified the API while
the console and upload paths still accepted any certificate.

## `[lease]`

| Key | Default | Meaning |
|---|---|---|
| `default_ttl_seconds` | `7200` | A lease not renewed within this window is swept by the watchdog |
| `idle_shutdown_seconds` | `28800` | Shut a reachable host down after this long with no activity and no lease |
| `long_term_backup` | `true` | Back up an active long-term lease's guests weekly |
| `long_term_backup_storage` | — | Where those backups go; blank means `[storage] bulk_storage` |
| `long_term_backup_keep` | `2` | Backup generations kept per guest |
| `retained_backup` | `false` | Have the watchdog also back up retained-registry guests (templates, persistent workers). Off by default: it writes gigabytes on a schedule |
| `retained_backup_interval_days` | `7` | How stale a retained guest's backup may get before the sweep takes another |

A lease is the unit of accountability: writes require one, created guests are
registered to it, and ending it destroys them and powers the host off.

Guests meant to outlive their lease are recorded in `retained.json` under the
state directory — see [safety-policy.md](safety-policy.md) for why a node tag
cannot serve as the owner. `doctor` reports their backup coverage whether or
not `retained_backup` is on.

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

### `wake-on-lan+home-assistant`

Sends the magic packet and triggers the Home Assistant script together on
every power-on — useful when WoL alone isn't reliable enough to trust by
itself (a NIC that occasionally drops the setting, a flaky BIOS) but a
smart-plug/KVM fallback is also available. Takes every key from both modes
above. Force-off still goes through Home Assistant, since WoL cannot cut
power.

```toml
mode = "wake-on-lan+home-assistant"
mac = "aa:bb:cc:dd:ee:ff"
broadcast = "192.168.1.255"
home_assistant_url = "https://homeassistant.example"
entity_on = "script.lab_power_on"
entity_off = "script.lab_force_off"
```

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

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Master switch |
| `address` | — | Tunnel address from your provider, e.g. `10.100.0.2/32` |
| `dns` | — | Resolver reachable through the tunnel |
| `endpoint` | — | `host:port` of the WireGuard server |
| `keepalive` | `25` | Keeps NAT mappings alive; `25` suits most NATs |

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
S3-compatible service works — MinIO, Garage, Backblaze, AWS. No bucket yet?
`install.sh`'s `lxc` S3 backend provisions a minimal MinIO LXC on the Proxmox
host for you — see [storage.md](storage.md#s3-scratch-bucket).

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

## `[share]`

Disposable, pre-authenticated links to a single guest console via a throwaway
worker. Needs an `ngrok` token only when `tunnel = "ngrok"`.

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Master switch |
| `worker_vmid` | `0` | VMID of the share worker (built with `proxmox-lab share setup`) |
| `port` | `8900` | Worker HTTP port |
| `default_minutes` | `30` | Lifetime of a new share link |
| `max_minutes` | `480` | Maximum lifetime |
| `tunnel` | `cloudflared` | Tunnel provider: `cloudflared`, `ngrok`, or `none` |
| `novnc_version` | `1.6.0` | noVNC version the worker serves |
| `ngrok_region` | `""` | Optional ngrok region, e.g. `us` or `eu` |

The generated `proxmox-agent-lab.toml` template currently omits `port` and
`novnc_version` — they fall back to these defaults; this is a known code-side
TEMPLATE gap.

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
| `helper` | `pxl-memflow-run` | Wrapper invoked on the host for memflow/USB commands |
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

## `[android]`

Defaults for `android create`; the CLI flag `--abi` still overrides the config
value — see [android.md](android.md).

| Key | Default | Meaning |
|---|---|---|
| `api_level` | `33` | Android API level used when `--api` is not passed |
| `abi` | `x86_64` | System-image ABI: `x86_64` (nested KVM, usable) or `arm64-v8a` (pure emulation, very slow on an x86 host) |

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

### Cloud vision providers

Store an NVIDIA API key to let an agent explicitly send a lease-owned console
screenshot to Nemotron Nano 12B v2 VL:

```bash
proxmox-lab secrets set nvidia-api-key
proxmox-lab console inspect --lease "$L" --vmid 9001
```

Add fallback routes with either or both of:

```bash
proxmox-lab secrets set openrouter-api-key
proxmox-lab secrets set kilo-api-key
```

`console inspect` races NVIDIA, the named Nemotron Omni free endpoint,
`openrouter/free`, and the Kilo Code gateway's `kilo-auto/balanced` router,
taking the first structurally valid answer. `kilo-auto/balanced` is a balanced
auto router: Kilo picks a vision-capable model server-side, so no concrete
model is pinned here. The OpenRouter response-healing plugin repairs JSON
syntax; the local wrapper still rejects ambiguous controls and invalid
coordinates.

Ordinary `console screenshot` never uploads anything. Environment fallbacks are
`PROXMOX_AGENT_LAB_NVIDIA_API_KEY`, `PROXMOX_AGENT_LAB_OPENROUTER_API_KEY`,
`PROXMOX_AGENT_LAB_KILO_API_KEY`, and the conventional `OPENROUTER_API_KEY`
and `KILO_API_KEY`. A project-scoped stored key wins over a stale conventional
shell value. OpenRouter free providers may log prompts for service
improvement; do not submit confidential or personal screens.

No key at all is not a dead end: `console screenshot --for-model` returns the
screen inline as a bounded base64 PNG, and `console inspect` attaches the same
thing when every provider fails. `console preflight` reports which routes have
a key under `vision.provider_keys`.

See also [console.md](console.md#optional-cloud-vision-console-inspect) for the full `console inspect` flow.
## `[audit]`

The audit ledger is one shared MariaDB, running in a persistent container on
the Proxmox host and published on the hypervisor's own address. Every
controller writes to the same history, so two machines driving one lab no
longer keep two partial ones.

Provision it once, from any controller:

```bash
proxmox-lab journal host-setup --host-change-authorized
```

| Key | Default | Meaning |
|---|---|---|
| `host` | the `[proxmox]` host | Where the ledger runs |
| `port` | `3306` | MariaDB port on that host |
| `database` | `proxmox_lab` | Database name |
| `user` | `proxmox_lab` | Database user |
| `password_secret` | `mariadb-password` | Secret name holding the password |
| `timeout_seconds` | `10` | Per-statement timeout |
| `journal_dir` | `<state>/journal` | Local spool, and any pre-MariaDB ledger |
| `controller_id` | hostname | This machine's name in the shared ledger |

### The ledger is not always up, and that is fine

The lab host powers itself off between leases, so the ledger goes with it.
Nothing fails: an event that cannot be written is appended to a local spool
and uploaded later.

```bash
proxmox-lab journal --flush-spool
```

`doctor` reports a backlog. The probe it uses has a short timeout, so `doctor`
stays fast while the host is asleep.

### One secret per machine

`mariadb-password` is the only credential a controller needs. Every other
secret — WireGuard keys, tunnel tokens, vision API keys — is stored in the
ledger and handed out from there, so adding a machine is one line:

```bash
export PROXMOX_AGENT_LAB_MARIADB_PASSWORD='...'
```

Treat it accordingly: it is the key to all the others. An environment variable
still overrides any individual secret locally.

### Upgrading from a pre-MariaDB controller

The first command after an upgrade carries this machine's old local ledger
into the shared one automatically. It is safe to re-run, and safe on a second
machine: event ids are derived from content, so shared history is recognised
and only genuinely new events are added.

```bash
proxmox-lab journal --migrate      # force it now
proxmox-lab journal --migrations   # who has already migrated
```

The old files are read, never deleted.

Every action appends a redacted event: what happened, to which VMID, under
which lease. Passwords, tokens, typed text and presigned URLs are never
recorded — only counts, exit codes and object keys.

`doctor` reports `audit.spooled_records`: a non-zero backlog means events are
sitting only on local disk, and it names `journal --flush-spool` as the fix.

Reading it back:

```bash
proxmox-lab journal --limit 20
proxmox-lab journal --lease 20260825125630-8edf9a82
proxmox-lab journal --event 'guest-*'
proxmox-lab journal --controller other-pc
proxmox-lab journal --summary
```

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
