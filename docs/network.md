# Forced-VPN egress

All internet traffic from lab guests leaves through an external WireGuard
endpoint. The tunnel is terminated once, on a gateway VM, not inside every
guest.

```
lab guest ---- vmbr1 ---- [ gateway VM ] ---- wg0 ---- vpn.example.com:51820
                          eth1      eth0/vmbr0
```

## Why a gateway rather than a client in each guest

- A guest cannot bypass it. Lab guests sit on an isolated bridge whose only
  route out is the gateway, so there is no non-tunnelled path to configure
  around.
- It covers guests that cannot run a VPN client: a machine mid-install, a
  Windows box before first logon, a throwaway container.
- One place to verify, one place to fail closed. The gateway's nftables
  `forward` chain is `policy drop` and permits only *lab interface → wg0*.
  There is deliberately no rule out over the WAN interface, so if the tunnel
  drops, guest traffic stops instead of leaking to the home WAN.
- Windows and Linux guests need no VPN configuration at all — they just DHCP.

## Credentials

| Item | Where |
|---|---|
| Private key | Keychain `proxmox-agent-lab` / `wg-private-key` |
| Preshared key | Keychain `proxmox-agent-lab` / `wg-preshared-key` |
| Peer public key | Keychain `proxmox-agent-lab` / `wg-peer-public-key` |
| Address, DNS, endpoint | `scripts/lab_netgw.py` constants |

Even the peer *public* key lives in the Keychain. The secret scanner cannot
distinguish a public WireGuard key from a private one by shape, and keeping the
repository free of any key-shaped string keeps that check strict and useful.

`wg0.conf` is written into the gateway with `agent/file-write`, so key material
never appears in `argv`, in a presigned URL, or in the audit ledger.

## One-time host prerequisite

The isolated bridge `vmbr1` must exist. Creating it modifies host networking on
`pve`, so it is gated:

```bash
proxmox-lab net host-bridge --host-change-authorized
```

Run this only when the user has explicitly asked for that exact change. It
creates an internal bridge with no physical ports — it carries lab traffic to
the gateway and nothing else.

**A created bridge is not yet a usable bridge.** Proxmox filters the interface
list by permission, so a bridge the token lacks `SDN.Use` on is simply absent
from the API response — indistinguishable from one that does not exist, and
guests cannot be attached to it. Grant it as root, to both the user *and* the
token, since a privilege-separated token does not inherit the user's grant:

```sh
pveum acl modify /sdn/zones/localnetwork/vmbr1 \
  --users agent@pve --roles AgentBridgeUse
pveum acl modify /sdn/zones/localnetwork/vmbr1 \
  --tokens 'agent@pve!lab' --roles AgentBridgeUse
```

`net host-bridge` reports `visible_to_this_token` and prints these two lines
when the grant is missing.

## Bringing up the gateway

```bash
proxmox-lab net gateway-create --lease "$L" --vmid 9000
proxmox-lab net verify --lease "$L" --vmid 9000
```

`gateway-create` clones the Debian 13 template, gives it an uplink NIC on
`vmbr0` and a lab NIC on `vmbr1` at `10.66.0.1/24`, then installs
wireguard-tools, nftables and dnsmasq. dnsmasq serves DHCP on the lab network
and forwards DNS to `10.100.0.1` through the tunnel, so guests do not leak
lookups to the home resolver.

The generic Debian and Ubuntu cloud images ship **without**
qemu-guest-agent, so there is no agent to provision through on first boot.
`gateway-create` therefore sets a one-off console password before first boot,
logs in over the serial console, installs the agent, and only then switches to
the agent for everything else — including writing `wg0.conf`, so key material
goes through `agent/file-write` and never through the console or a URL. The
password is generated per build, held in memory, and never audited.

Interface names are never hardcoded. The provisioning step finds the lab-side
interface by the address it carries and substitutes it into both the nftables
and dnsmasq configs, then refuses to continue if no interface holds the lab
address. A hardcoded name is the worst kind of wrong here: the rules install
cleanly, match nothing, and present as "no egress" with a ruleset that reads
perfectly.

`verify` is the check that matters. It asserts, from inside the gateway, that:

1. WireGuard's last handshake is recent, not merely that one ever happened
   (`--max-handshake-age`, default 300s) — a dead tunnel keeps its old
   handshake timestamp forever;
2. the egress IP differs from the home WAN IP, taken from the controller;
3. the forward chain is `policy drop`;
4. *every* accept rule in that chain egresses via `wg0` — checked by reading
   the rules, not by grepping for one known-bad string, so a rule carrying a
   counter or a different interface name cannot slip through.

It exits non-zero if any of those fail. Run it after every gateway build and
after anything that touches the ruleset.

## Leak testing

`net verify` needs a lease, because it runs commands inside the gateway, and it
judges the tunnel from the gateway's own point of view. `net leak-test` judges
it from a guest's, which is the one that matters:

```bash
proxmox-lab net leak-test --lease "$L" --vmid 9002 \
  --user alpine --password-stdin --gateway-vmid 9000 <<< 'console-password'
```

It runs inside the guest over the serial console, so it needs no guest agent,
and covers the three ways traffic actually escapes a tunnel:

| Check | Leak it catches |
|---|---|
| Egress IPv4 vs the controller's own public IP | Traffic bypassing the tunnel to the home WAN |
| Egress IPv6 | A v6 route alongside a v4-only tunnel |
| `/etc/resolv.conf` and resolution | Queries reaching the home resolver |
| Default route points at `10.66.0.1` | Guest never entered the lab network |
| With `--gateway-vmid`: `wg0` stopped, egress retried | Kill switch failing open |

The home WAN address is taken from the controller, which sits on the same LAN,
so the comparison needs nothing from the gateway.

Reachability is probed with ICMP, not HTTP, so "the network is broken" is never
confused with "this image has no curl". For the exit address the test tries, in
order: `curl`, `wget` over HTTPS, the same host over plain HTTP, installing
`curl` (unless `--no-install-tools`), and finally DNS —
`nslookup myip.opendns.com resolver1.opendns.com`, which OpenDNS answers with
the querying address. A stock Alpine cloud image has no curl, no wget and no
sudo, so the DNS path is the one that actually works there.

A probe that returns nothing is reported as **unproven**, never as a leak. An
inconclusive test and a detected leak demand different responses, and a false
alarm here is as damaging as a missed one. The kill-switch check stops
`wg0`, confirms egress *stops* rather than falling back, then restarts it and
confirms egress returns. It exits non-zero on any failure.

## Putting guests behind it

```bash
proxmox-lab net attach --lease "$L" --vmid 9001
proxmox-lab net status
```

`attach` moves the guest's NIC to `vmbr1` and sets it to DHCP. Reboot the guest
or replug the NIC to pick up the new lease. `status` lists every guest
currently on the lab network.

## Limits worth knowing

- The gateway is an ordinary lease resource. `lease-end` destroys it, and any
  guest still on `vmbr1` loses egress entirely — which is the correct failure
  direction, but plan the teardown order.
- Guests left on `vmbr0` are *not* tunnelled. `net status` is the way to tell.
- Killing the gateway does not kill guest-to-guest traffic on `vmbr1`.
