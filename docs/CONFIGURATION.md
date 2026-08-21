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

### Optional cloud vision

Store an NVIDIA API key to let an agent explicitly send a lease-owned console
screenshot to Nemotron Nano 12B v2 VL:

```bash
proxmox-lab secrets set nvidia-api-key
proxmox-lab console inspect --lease "$L" --vmid 9001
```

Add OpenRouter fallback access with:

```bash
proxmox-lab secrets set openrouter-api-key
```

`console inspect` tries NVIDIA, the named Nemotron Omni free endpoint, then
`openrouter/free`. The OpenRouter response-healing plugin repairs JSON syntax;
the local wrapper still rejects ambiguous controls and invalid coordinates.
Ordinary `console screenshot` never uploads anything. Environment fallbacks
are `PROXMOX_AGENT_LAB_NVIDIA_API_KEY`,
`PROXMOX_AGENT_LAB_OPENROUTER_API_KEY`, and conventional
`OPENROUTER_API_KEY`. A project-scoped stored OpenRouter key wins over a stale
conventional shell value. OpenRouter free providers may log prompts for
service improvement; do not submit confidential or personal screens.
- **`env`** — read `PROXMOX_AGENT_LAB_<NAME>`, e.g.
  `PROXMOX_AGENT_LAB_PROXMOX_TOKEN`. Read-only; good for CI and containers.
- **`file`** — a TOML file that must be `0600`. For headless boxes with no
  keyring. Least safe; a keyring is better where one exists.

An environment variable always overrides the chosen backend, so you can point
a single install at a different lab for one command.

## `[audit]`

| Key | Default | Meaning |
|---|---|---|
| `backend` | `sqlite` | Audit backend: `sqlite`, `jsonl`, or `pocketbase` |
| `journal_dir` | `<state>/journal` | Local audit ledger directory |
| `git_sync` | `false` | Copy each redacted event to a private git log |
| `git_repo` | — | Dedicated private logging checkout |
| `git_branch` | `logs` | Remote branch receiving logging commits |
| `controller_id` | hostname | Stable identifier written by the PocketBase backend |
| `pocketbase_url` | — | Absolute HTTP(S) URL of the PocketBase service |
| `pocketbase_collection` | `proxmox_lab_events` | Private collection for audit records |
| `pocketbase_token_secret` | `audit-token` | Secret-store name containing the active API token |
| `pocketbase_timeout_seconds` | `10` | Per-request timeout |
| `pocketbase_auth_refresh_before_seconds` | `300` | Renew a renewable JWT this many seconds before expiry |
| `pocketbase_agent_collection` | `proxmox_lab_agents` | Restricted password-auth collection for controllers |

Every action appends a redacted event: what happened, to which VMID, under
which lease. Passwords, tokens, typed text and presigned URLs are never
recorded — only counts, exit codes and object keys.

The `pocketbase` backend sends the same redacted event to a private
PocketBase collection and does not silently fall back to a local ledger. Keep
the token in the configured secret store:

```toml
[audit]
backend = "pocketbase"
pocketbase_url = "https://pocketbase.example"
pocketbase_collection = "proxmox_lab_events"
pocketbase_token_secret = "audit-token"
```

```bash
# One-time bootstrap; these credentials are used only by the explicit
# provisioning command and are kept in the configured secret store.
proxmox-lab secrets set pocketbase-superuser-email
proxmox-lab secrets set pocketbase-superuser-password
proxmox-lab journal --provision-pocketbase-agent
proxmox-lab doctor
```

A shortcut: storing a **superuser token** directly as the audit token
(`proxmox-lab secrets set audit-token`) is detected on first use and converted
automatically — the controller provisions the permanent least-privileged agent
with that token, stores the agent's password credentials, and replaces the
superuser token in the secret store with the agent's renewable one. Pasting a
superuser token is therefore a one-time bootstrap, never a standing
credential.

PocketBase JWTs are inherently finite; there is no unlimited token. The
controller refreshes a renewable `_superusers` or agent token before its
configured expiry window and atomically replaces the keyring value. The
provisioning command instead creates a password-authenticated
`pocketbase_agent_collection` record, stores its generated credentials and
active token in the secret store, and grants that collection only list, view,
and create access to the configured audit collection. If a stored agent token
has already expired, the controller obtains a replacement through that stored
account password. Update and delete remain superuser-only.

`journal --provision-pocketbase-agent` may change the configured audit
collection rules to that restricted agent collection. Use it only for the
controller collection named in the current configuration. The original
`journal --provision-pocketbase` creates or validates a superuser-only audit
collection without changing its rules.

### SQLite migration and rollback

The backend switch is a clean cutover, not a live dual-write migration:

1. Stop all controllers that can write the audit ledger and copy the SQLite
   database from `journal_dir` to an offline backup.
2. Add the PocketBase settings and token, leaving `backend = "sqlite"`.
3. Run `proxmox-lab journal --migrate-sqlite-to-pocketbase`. It validates or
   creates the collection, preserves each redacted JSON record, and prints the
   source count, time range, and deterministic SHA-256 digest.
4. Restarts are safe: deterministic event IDs skip already imported records;
   the SQLite database is read-only throughout.
5. Set `backend = "pocketbase"` and run `proxmox-lab doctor` before resuming
   leases.

Rollback is fail-safe: stop writers, restore `backend = "sqlite"`, and retain
the untouched SQLite backup. PocketBase records are not deleted on rollback.

`git_sync` is off by default. Most people do not want their lab's audit trail
pushed anywhere. If you enable it, point `git_repo` at the root of a clean,
**private, logging-only checkout**: the journal records host addresses and
VMIDs. The local backend can remain `sqlite`; the logging checkout receives
one JSONL file per day. Sync fails closed if that checkout contains any other
uncommitted file, and only `journal/YYYY-MM-DD.jsonl` is ever staged.
`doctor` reports these independently as `audit.local_backend` and
`audit.git_sync`; SQLite plus Git sync is an intentional supported setup.

When `git_sync` is on, `doctor` also checks that the mirror could actually
receive a record and reports `audit.git_status` — the repository exists, is a
repository root, is clean and is writable. A failure there is a `doctor`
problem, because each mutating command only prints a warning when a push
fails, which is easy to miss for weeks. `doctor` reports
`audit.spooled_records` for the same reason: a non-zero backlog means the
configured backend refused events that are now only on local disk, and it
names `journal --flush-spool` as the fix.

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
