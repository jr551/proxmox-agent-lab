# Forced-VPN egress

## Purpose

Lab guests have no direct route to the Internet — all egress is forced through a
single WireGuard gateway VM so traffic cannot bypass or leak to the home WAN.
This page covers the one-time host bridge, the gateway build, verification, leak
testing and attaching guests.

## Prerequisites

- An active lease.
- A Debian/Ubuntu cloud-init template on the node (set by
  `[network] gateway_template_vmid`).
- `Sys.Audit`/`AgentBridgeUse` on `/sdn/zones/localnetwork/vmbr1` to see a
  bridge the token creates.
- WireGuard credentials stored in the configured secret backend:
  `wg-private-key`, `wg-preshared-key` (if used), and `wg-peer-public-key`.

## Minimal example

```bash
L=$(proxmox-lab lease-begin --purpose "vpn demo" \
    | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
trap 'proxmox-lab lease-end --lease "$L"' EXIT

# one-time host setup
proxmox-lab net host-bridge --host-change-authorized

# build, verify and use the gateway
proxmox-lab net gateway-create --lease "$L" --vmid 9000
proxmox-lab net verify --lease "$L" --vmid 9000

proxmox-lab guest clone --lease "$L" --template 9000 --newid 9001
proxmox-lab net attach --lease "$L" --vmid 9001
proxmox-lab net leak-test --lease "$L" --vmid 9001 \
  --user alpine --password-stdin --gateway-vmid 9000 <<< 'console-password'
```

## Expected result

`net gateway-create` clones the template, installs WireGuard, nftables and
dnsmasq, and returns the gateway's VMID and addresses.

`net verify` exits `0` when:

- WireGuard's last handshake is within `--max-handshake-age` (default 300 s);
- the egress IP differs from the home WAN IP;
- the nftables `forward` chain is `policy drop`;
- every accept rule in that chain egresses via `wg0`.

`net leak-test` exits `0` when the guest's egress is via the tunnel, IPv6 does
not bypass it, DNS does not reach the home resolver, and stopping `wg0` stops
egress (kill switch). An inconclusive result is reported as **unproven**, never
as a pass.

## Commands

| Command | Key flags | Notes |
|---|---|---|
| `net host-bridge` | `--host-change-authorized` | creates isolated `vmbr1` once |
| `net gateway-create` | `--lease`, `--vmid`, `--template`, `--cores`, `--memory`, `--name`, `--policy` | clones template, builds gateway |
| `net verify` | `--lease`, `--vmid`, `--max-handshake-age` | asserts tunnel + egress + forward chain |
| `net leak-test` | `--lease`, `--vmid`, `--user`, `--password-stdin`, `--no-install-tools`, `--gateway-vmid` | guest-side leak proof |
| `net attach` | `--lease`, `--vmid` | move guest NIC to `vmbr1` |
| `net status` | — | lists guests on the lab network |

## Why a gateway rather than a client in each guest

- A guest cannot bypass it. Lab guests sit on an isolated bridge whose only
  route out is the gateway.
- It covers guests that cannot run a VPN client: a machine mid-install, a
  Windows box before first logon, a throwaway container.
- One place to verify, one place to fail closed. The gateway's nftables
  `forward` chain is `policy drop` and permits only `lab interface → wg0`.
- Windows and Linux guests need no VPN configuration — they just DHCP.

```
lab guest ---- vmbr1 ---- [ gateway VM ] ---- wg0 ---- vpn.example.com:51820
                          eth1      eth0/vmbr0
```

## One-time host prerequisite

The isolated bridge `vmbr1` must exist. Creating it modifies host networking, so
it is gated:

```bash
proxmox-lab net host-bridge --host-change-authorized
```

Proxmox filters the interface list by permission, so a bridge the token lacks
`SDN.Use` on is simply absent from the API response. Grant it as root, to both
user and token, since a privilege-separated token does not inherit the user's
grant:

```sh
pveum acl modify /sdn/zones/localnetwork/vmbr1 \
  --users agent@pve --roles AgentBridgeUse
pveum acl modify /sdn/zones/localnetwork/vmbr1 \
  --tokens 'agent@pve!lab' --roles AgentBridgeUse
```

`net host-bridge` reports `visible_to_this_token` and prints these two lines
when the grant is missing.

## Credentials

| Item | Where |
|---|---|
| Private key | configured secret `wg-private-key` |
| Preshared key | configured secret `wg-preshared-key` (if your provider uses one) |
| Peer public key | configured secret `wg-peer-public-key` |
| Address, DNS, endpoint | `[vpn]` section of the config |

Even the peer *public* key is stored as a secret. The secret scanner cannot
distinguish a public WireGuard key from a private one by shape, and keeping the
repository free of any key-shaped string keeps that check strict and useful.

`wg0.conf` is written into the gateway with `agent/file-write`, so key material
never appears in `argv`, a presigned URL, or the audit ledger.

## Bringing up the gateway

```bash
proxmox-lab net gateway-create --lease "$L" --vmid 9000
proxmox-lab net verify --lease "$L" --vmid 9000
```

`gateway-create` clones the Debian 13 template, gives it an uplink NIC on
`vmbr0` and a lab NIC on `vmbr1` at `10.66.0.1/24`, then installs
wireguard-tools, nftables and dnsmasq. dnsmasq serves DHCP on the lab network
and forwards DNS through the tunnel.

The generic cloud images ship without qemu-guest-agent, so `gateway-create`
provisions over the serial console with a one-off generated password, installs
the agent, and then writes `wg0.conf` through `agent/file-write`.

Interface names are never hardcoded. The provisioning step finds the lab-side
interface by the address it carries and substitutes it into both the nftables and
dnsmasq configs, then refuses to continue if no interface holds the lab address.

## Leak testing

`net verify` judges from the gateway; `net leak-test` judges from a guest:

```bash
proxmox-lab net leak-test --lease "$L" --vmid 9002 \
  --user alpine --password-stdin --gateway-vmid 9000 <<< 'console-password'
```

It runs inside the guest over the serial console and covers:

| Check | Leak it catches |
|---|---|
| Egress IPv4 vs the controller's own public IP | Traffic bypassing the tunnel to the home WAN |
| Egress IPv6 | A v6 route alongside a v4-only tunnel |
| `/etc/resolv.conf` and resolution | Queries reaching the home resolver |
| Default route points at `10.66.0.1` | Guest never entered the lab network |
| With `--gateway-vmid`: `wg0` stopped, egress retried | Kill switch failing open |

Reachability is probed with ICMP, then `curl`, `wget`, plain HTTP, installing
`curl` (unless `--no-install-tools`), and finally DNS. A stock Alpine image
often has no `curl`, so the DNS path is the one that actually works. A probe
that returns nothing is **unproven**, not a pass and not a leak.

## Putting guests behind it

```bash
proxmox-lab net attach --lease "$L" --vmid 9001
proxmox-lab net status
```

`attach` moves the guest's NIC to `vmbr1` and sets it to DHCP. Reboot the guest
or replug the NIC to pick up the new lease. `status` lists every guest currently
on the lab network.

## Cleanup

The gateway is an ordinary lease resource. `lease-end` destroys it, and any
guest still on `vmbr1` loses egress entirely — the correct failure direction.
Guests left on `vmbr0` are *not* tunnelled; `net status` is the way to tell.
Killing the gateway does not kill guest-to-guest traffic on `vmbr1`.

The host bridge `vmbr1` is a persistent host change and is not removed by
`lease-end`.

## Common failures

| Symptom | Diagnostic | Next step |
|---|---|---|
| `net host-bridge` reports `visible_to_this_token: false` | `proxmox-lab doctor` | Grant `AgentBridgeUse` to both `agent@pve` and `agent@pve!lab` |
| `net verify` fails on handshake age | `proxmox-lab net status` | The tunnel is dead but the old handshake remains; restart/re-provision |
| `net verify` passes egress but a non-`wg0` rule is found | Re-run `net verify` | A counter or alternate interface is in the ruleset; fix nftables and verify again |
| `net leak-test` reports **unproven** | Re-run with `--gateway-vmid` if not already | Install `curl` or use the DNS path; an inconclusive result is not a pass |
| Guest has no egress at all | `proxmox-lab net status` | The guest may still be on `vmbr0`; `net attach` it |

For the full symptom-decision list, see [troubleshooting.md](troubleshooting.md).

## Advanced usage

- `net gateway-create --policy retain` keeps the gateway VM after the lease.
- Run `net verify` after any manual change to the gateway ruleset.
- The gateway VM is just another guest; clone it from a template you built with
  `guest template` to speed up repeat tests.

## Verification status

See [VERIFICATION.md](VERIFICATION.md) for what has been observed on real
hardware (forced VPN egress, kill-switch `UNREACHABLE→REACHABLE`) and what
remains unit-tested only (`ngrok` as the share tunnel, end-to-end TLS
interception in `netcap`).

## Safety gate

| Operation | Required flag / guard | What it guards |
|---|---|---|
| Create host bridge `vmbr1` | `--host-change-authorized` | host networking (`/nodes/pve`) |
| Build gateway VM | `--lease` (and template VMID) | lease ownership; `wg0.conf` never appears in `argv` |
| Verify / leak-test | `--lease` and console password via `--password-stdin` | runs inside gateway/guest; typed passwords never audited |
| Fail-closed forwarding | `nftables forward policy drop` + only `lab-if → wg0` accept rules; no WAN rule | a dropped tunnel stops egress rather than leaking to home WAN |

Do not run `net host-bridge` or `gateway-create` without explicit user
authorization for host network / VM creation. Keep WireGuard keys in the
configured secret backend, never in config or the repository.

## See also

- [CONFIGURATION.md](CONFIGURATION.md#network) — `[network]` bridge and DHCP keys
- [CONFIGURATION.md](CONFIGURATION.md#vpn) — `[vpn]` keys and secrets
- [safety-policy.md](safety-policy.md) — host-change authorization
- [netcap.md](netcap.md) — same `[memflow]` SSH trust boundary for capture / MITM
