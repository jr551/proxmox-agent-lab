# Configuration

One TOML file is the entire configuration surface: nine keys, all site
values, nothing secret. The control plane is ssh with your own agent and
keys, so no credential ever belongs in this file — or on any command line.

## Where the config lives

Searched in this order:

1. `$PROXMOX_AGENT_LAB_CONFIG` — an explicit path, always wins
2. `./proxmox-agent-lab.toml` — handy inside a checkout
3. `$XDG_CONFIG_HOME/proxmox-agent-lab/config.toml`
4. `~/.config/proxmox-agent-lab/config.toml`

Create one with `proxmox-lab init` (use `--path` to choose another location,
`--force` to overwrite). `proxmox-lab doctor` prints the file it actually
loaded and the path it expected.

A missing or broken config never takes the tools down with it: imports
survive, `init` and `doctor` still run, and `doctor` reports the problem.
Sections this version does not know are ignored and listed by `doctor` — an
older file loads; it just does not configure anything new.

## The nine settings

This is the whole file, with the values `proxmox-lab init` writes:

```toml
[ssh]
target = "proxmox"           # ssh alias/host reached as root

[pve]
node = "pve"                 # node name used in remote paths
template_vmid = 100          # default template for guest clone/create

[power]
mac = ""                     # wired NIC MAC for Wake-on-LAN
broadcast = "255.255.255.255"
port = 9

[state]
dir = "~/.local/share/proxmox-agent-lab"   # lab.db lives here

[lease]
ttl_seconds = 7200           # a lease not renewed within this window is swept
idle_shutdown_seconds = 28800
```

### `[ssh]`

| Key | Default | Meaning |
|---|---|---|
| `target` | *(unset)* | The ssh alias or host reached as root — a `~/.ssh/config` alias, a hostname, or an IP. This one setting is the entire gate: nothing else turns remote access on. A value of `""` means "not configured"; commands that need the host say so and point here. |

### `[pve]`

| Key | Default | Meaning |
|---|---|---|
| `node` | `pve` | The node name used in remote paths — the machine's short hostname, not the FQDN. `doctor` warns when it does not match the host. |
| `template_vmid` | `0` (unset) | The VMID of the template guests are cloned from. `0` means "not chosen yet" and is treated as missing; the starter file writes `100` as a placeholder. |

### `[power]`

| Key | Default | Meaning |
|---|---|---|
| `mac` | *(unset)* | The wired NIC's MAC address, used to build the Wake-on-LAN packet. Read it on the host console with `ip -br link show` and take the interface that carries your LAN IP — usually `enp*`/`eno*`, not `vmbr0`. |
| `broadcast` | `255.255.255.255` | Where magic packets are sent. If your router drops the global broadcast, set your LAN's directed broadcast instead (e.g. `192.168.1.255`). |
| `port` | `9` | UDP destination port of the magic packet. |

### `[state]`

| Key | Default | Meaning |
|---|---|---|
| `dir` | `~/.local/share/proxmox-agent-lab` | Where runtime state lives: `lab.db`, the SQLite file that holds leases, lease resources and the journal, plus captures. `$PROXMOX_AGENT_LAB_STATE` overrides it (throwaway runs and tests use that). Nothing is ever written inside the installed package. |

### `[lease]`

| Key | Default | Meaning |
|---|---|---|
| `ttl_seconds` | `7200` | How long a lease lives without a heartbeat. A lease not renewed inside this window is swept: its guests are destroyed and its resources released. |
| `idle_shutdown_seconds` | `28800` | With no active lease and no MCP tool call for this long, the host is shut down — verified by probing until it stops answering, never assumed. Every MCP tool call refreshes the clock. |

## Environment variables

| Variable | Effect |
|---|---|
| `PROXMOX_AGENT_LAB_CONFIG` | Explicit config path; beats every search location above. |
| `PROXMOX_AGENT_LAB_STATE` | Overrides `[state] dir` — where `lab.db` and captures are written. |

## Where to go next

- [safety-policy.md](safety-policy.md) — why every write belongs to a lease,
  and what the machine is allowed to do on its own.
- [architecture.md](architecture.md) — how configuration, the ssh seam and
  the state store fit together.
- [troubleshooting.md](troubleshooting.md) — symptom → diagnostic command →
  next decision. Start here before blaming the tool.
- [../SKILL.md](../SKILL.md) — the canonical lease shape every task follows.
- [VERIFICATION.md](VERIFICATION.md) — what has actually been run, honestly.
