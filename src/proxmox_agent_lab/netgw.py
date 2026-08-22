"""Forced-VPN egress for lab guests.

Approach
--------
Rather than configuring a VPN client inside every guest -- which a guest can
disable, which Windows and Linux do differently, and which cannot protect a
machine mid-install -- lab traffic is routed through one gateway VM:

    lab guest ---- vmbr1 ---- [ gateway VM ] ---- wg0 ---- external endpoint
                              eth1      eth0/vmbr0

Lab guests sit on an isolated bridge with the gateway as their only route, so
there is no path to the internet that does not cross the tunnel. The gateway's
nftables `forward` chain defaults to `drop` and permits only `eth1 -> wg0`, so
if the tunnel drops, guest egress stops rather than leaking to the home WAN.
That fail-closed behaviour matches the rest of this skill.

The WireGuard private key and preshared key are read from the macOS Keychain
and written straight into the guest with `agent/file-write`. They never appear
in argv, in a presigned URL, in this repository, or in the audit ledger.
"""

from __future__ import annotations

import base64
import json
import secrets
import subprocess
import time
from typing import Any

from . import console

# Tunnel definition. Addresses and the endpoint are configuration; every piece
# of key material, public half included, stays in the Keychain so that no
# WireGuard key shape ever appears in this repository.
from . import config as _config
from . import secrets_store

_CONFIG = _config.get()
WG_ADDRESS = _CONFIG.vpn.address
WG_DNS = _CONFIG.vpn.dns
WG_ENDPOINT = _CONFIG.vpn.endpoint
WG_KEEPALIVE = int(_CONFIG.vpn.keepalive)
VPN_ENABLED = bool(_CONFIG.vpn.enabled)

# The isolated lab network behind the gateway.
LAB_BRIDGE = _CONFIG.network.lab_bridge
LAB_NETWORK = _CONFIG.network.lab_network
LAB_GATEWAY_IP = _CONFIG.network.lab_gateway_ip
LAB_DHCP_RANGE = (_CONFIG.network.dhcp_start, _CONFIG.network.dhcp_end)

GATEWAY_TEMPLATE_VMID = int(_CONFIG.network.gateway_template_vmid)
PRIVATE_KEY_ACCOUNT = "wg-private-key"
PRESHARED_KEY_ACCOUNT = "wg-preshared-key"
PEER_PUBLIC_KEY_ACCOUNT = "wg-peer-public-key"


def _keychain(account: str) -> str:
    try:
        return secrets_store.get(_CONFIG, account)
    except secrets_store.SecretError as exc:
        raise RuntimeError(str(exc)) from None


def render_wg_config() -> str:
    """Build wg0.conf. Only called with secrets in memory, never logged."""
    if not VPN_ENABLED:
        raise RuntimeError(
            "the VPN gateway is not configured. Set [vpn] enabled, address, "
            "dns and endpoint, then store the keys with 'proxmox-lab secrets "
            "set wg-private-key' (and wg-preshared-key, wg-peer-public-key)."
        )
    for field, value in (("address", WG_ADDRESS), ("endpoint", WG_ENDPOINT)):
        if not value:
            raise RuntimeError(f"[vpn] {field} is not set")
    return (
        "[Interface]\n"
        f"PrivateKey = {_keychain(PRIVATE_KEY_ACCOUNT)}\n"
        f"Address = {WG_ADDRESS}\n"
        "Table = auto\n"
        "\n"
        "[Peer]\n"
        f"PublicKey = {_keychain(PEER_PUBLIC_KEY_ACCOUNT)}\n"
        f"PresharedKey = {_keychain(PRESHARED_KEY_ACCOUNT)}\n"
        "AllowedIPs = 0.0.0.0/0, ::/0\n"
        f"Endpoint = {WG_ENDPOINT}\n"
        f"PersistentKeepalive = {WG_KEEPALIVE}\n"
    )


def render_nftables() -> str:
    """Fail-closed forwarding: guests reach the internet only through wg0.

    `__LAB_IF__` is substituted during provisioning with the interface that
    actually holds the lab address. Hardcoding a name here would fail
    silently -- the rules simply would not match, and the symptom is "no
    egress" with a ruleset that looks perfectly correct.
    """
    return f"""#!/usr/sbin/nft -f
flush ruleset

table inet labgw {{
  chain input {{
    type filter hook input priority 0; policy accept;
  }}

  chain forward {{
    type filter hook forward priority 0; policy drop;

    # Lab guests out through the tunnel, and the replies back.
    iifname "__LAB_IF__" oifname "wg0" accept
    iifname "wg0" oifname "__LAB_IF__" ct state established,related accept

    # Deliberately absent: any rule permitting the lab network out over the
    # WAN interface. If wg0 goes down there is no fallback path, so guest
    # egress fails closed instead of leaking to the home WAN.
  }}

  chain postrouting {{
    type nat hook postrouting priority srcnat; policy accept;
    oifname "wg0" masquerade
  }}
}}
"""


def render_dnsmasq() -> str:
    start, end = LAB_DHCP_RANGE
    return (
        "interface=__LAB_IF__\n"
        "bind-interfaces\n"
        "domain-needed\n"
        "bogus-priv\n"
        "no-resolv\n"
        f"server={WG_DNS}\n"
        f"dhcp-range={start},{end},12h\n"
        f"dhcp-option=option:router,{LAB_GATEWAY_IP}\n"
        f"dhcp-option=option:dns-server,{LAB_GATEWAY_IP}\n"
    )


PROVISION_SCRIPT = """#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

apt-get update -qq
apt-get install -y -qq wireguard-tools nftables dnsmasq curl >/dev/null

printf 'net.ipv4.ip_forward=1\\nnet.ipv6.conf.all.forwarding=1\\n' \\
  > /etc/sysctl.d/99-labgw.conf
sysctl -q --system

# Find the lab-side interface by the address it carries, never by name.
LAB_IF=$(ip -o -4 addr show | awk -v a="__LAB_GATEWAY_IP__/" \\
  'index($4, a) == 1 {print $2; exit}')
if [ -z "$LAB_IF" ]; then
  echo "no interface holds __LAB_GATEWAY_IP__; cannot build the ruleset" >&2
  ip -o -4 addr show >&2
  exit 1
fi
echo "lab interface: $LAB_IF"

chmod 600 /etc/wireguard/wg0.conf
sed "s/__LAB_IF__/$LAB_IF/g" /tmp/labgw-nftables.nft > /etc/nftables.conf
chmod 0755 /etc/nftables.conf
sed "s/__LAB_IF__/$LAB_IF/g" /tmp/labgw-dnsmasq.conf \\
  > /etc/dnsmasq.d/labgw.conf
chmod 0644 /etc/dnsmasq.d/labgw.conf

systemctl enable --now nftables
systemctl restart nftables
systemctl enable --now wg-quick@wg0
systemctl restart wg-quick@wg0
systemctl enable --now dnsmasq
systemctl restart dnsmasq

sleep 3
wg show wg0 >/dev/null
nft list chain inet labgw forward | grep -q "$LAB_IF" \\
  || { echo "ruleset does not reference $LAB_IF" >&2; exit 1; }
echo provisioned
"""


def provision_script() -> str:
    """The provisioning script with the lab address substituted in."""
    return PROVISION_SCRIPT.replace("__LAB_GATEWAY_IP__", LAB_GATEWAY_IP)


def _write_guest_file(lab: Any, api: Any, vmid: int, path: str,
                      content: str) -> None:
    """Write a file into a guest without the content touching the ledger."""
    api.call(
        "POST",
        f"/nodes/{lab.NODE}/qemu/{vmid}/agent/file-write",
        {
            "file": path,
            "content": base64.b64encode(content.encode()).decode(),
            "encode": 0,
        },
    )


def _exec(lab: Any, api: Any, vmid: int, script: str, timeout: int = 900) -> dict:
    return console.agent_exec(
        lab, api, vmid, ["/bin/bash", "-c", script], timeout=timeout
    )


def require_bridge(lab: Any, api: Any) -> None:
    """Fail with an actionable message when the lab bridge is unusable.

    Proxmox filters the interface list by permission, so a bridge that exists
    on the host is simply absent from the API response unless the caller holds
    SDN.Use on it. "Missing" and "invisible" therefore look identical here, and
    the fix differs, so say both.
    """
    bridges = api.call("GET", f"/nodes/{lab.NODE}/network") or []
    if any(item.get("iface") == LAB_BRIDGE for item in bridges):
        return
    raise lab.LabError(
        f"{LAB_BRIDGE} is not usable. Either it does not exist -- create it "
        "with 'net host-bridge --host-change-authorized' -- or this token "
        "cannot see it, because Proxmox hides bridges the caller lacks SDN.Use "
        "on. Both the user and the token need it, as an API token with "
        "privilege separation does not inherit the user's grant:\n"
        f"  pveum acl modify /sdn/zones/localnetwork/{LAB_BRIDGE} "
        f"--users {lab.TOKEN_USER} --roles AgentBridgeUse\n"
        f"  pveum acl modify /sdn/zones/localnetwork/{LAB_BRIDGE} "
        f"--tokens '{lab.TOKEN_USER}!{lab.TOKEN_NAME}' --roles AgentBridgeUse"
    )


def cmd_host_bridge(lab: Any, args: Any) -> None:
    """Create the isolated lab bridge. This is a host networking change."""
    if not args.host_change_authorized:
        raise lab.LabError(
            f"Creating {LAB_BRIDGE} modifies host networking on {lab.NODE}. "
            "Re-run with --host-change-authorized only when the user has "
            "explicitly asked for that exact change."
        )
    api = lab.ProxmoxAPI()
    existing = api.call("GET", f"/nodes/{lab.NODE}/network")
    if any(item.get("iface") == LAB_BRIDGE for item in existing or []):
        print(json.dumps({"bridge": LAB_BRIDGE, "created": False,
                          "reason": "already present"}, indent=2))
        return
    api.call(
        "POST",
        f"/nodes/{lab.NODE}/network",
        {
            "iface": LAB_BRIDGE,
            "type": "bridge",
            "autostart": 1,
            "bridge_ports": "",
            "comments": "proxmox-agent-lab isolated VPN-only lab network",
        },
    )
    api.call("PUT", f"/nodes/{lab.NODE}/network")  # apply pending changes
    lab.audit("lab-bridge-created", node=lab.NODE, bridge=LAB_BRIDGE)
    visible = any(
        item.get("iface") == LAB_BRIDGE
        for item in (api.call("GET", f"/nodes/{lab.NODE}/network") or [])
    )
    result: dict[str, Any] = {
        "bridge": LAB_BRIDGE,
        "created": True,
        "visible_to_this_token": visible,
    }
    if not visible:
        result["next_step_as_root"] = [
            f"pveum acl modify /sdn/zones/localnetwork/{LAB_BRIDGE} "
            f"--users {lab.TOKEN_USER} --roles AgentBridgeUse",
            f"pveum acl modify /sdn/zones/localnetwork/{LAB_BRIDGE} "
            f"--tokens '{lab.TOKEN_USER}!{lab.TOKEN_NAME}' --roles AgentBridgeUse",
        ]
        result["why"] = (
            "the bridge exists but Proxmox hides bridges the caller lacks "
            "SDN.Use on, and guests cannot be attached to it until granted"
        )
    print(json.dumps(result, indent=2, sort_keys=True))


def cmd_gateway_create(lab: Any, args: Any) -> None:
    api = lab.ProxmoxAPI()
    lease = lab.load_lease(args.lease)
    if args.vmid in lease["initial_vmids"]:
        raise lab.LabError(f"VMID {args.vmid} existed before this lease")
    require_bridge(lab, api)
    name = args.name or f"labgw-{args.vmid}"

    upid = api.call(
        "POST",
        f"/nodes/{lab.NODE}/qemu/{args.template}/clone",
        {"newid": args.vmid, "name": name, "full": 1, "target": lab.NODE},
    )
    lab.wait_task(api, upid, timeout=args.clone_timeout)
    lab.register_resource(lease, "qemu", args.vmid, args.policy, name)

    # A console password is set before first boot so the gateway can be
    # bootstrapped over serial when the image has no guest agent. It is
    # generated per build, held only in memory, and never audited.
    password = secrets.token_urlsafe(18)
    template_config = api.call(
        "GET", f"/nodes/{lab.NODE}/qemu/{args.template}/config"
    )
    cloud_user = template_config.get("ciuser") or "debian"
    api.call(
        "PUT",
        f"/nodes/{lab.NODE}/qemu/{args.vmid}/config",
        {
            "cores": args.cores,
            "memory": args.memory,
            "ciuser": cloud_user,
            "cipassword": password,
            "net0": "virtio,bridge=vmbr0,firewall=1",
            "net1": f"virtio,bridge={LAB_BRIDGE}",
            "ipconfig0": "ip=dhcp",
            "ipconfig1": f"ip={LAB_GATEWAY_IP}/{LAB_NETWORK.split('/')[1]}",
            "agent": "enabled=1",
            "onboot": 0,
            "tags": f"codex-lab;lease-{args.lease};labgw",
        },
    )
    start = api.call("POST", f"/nodes/{lab.NODE}/qemu/{args.vmid}/status/start")
    lab.wait_task(api, start, timeout=180)

    deadline = time.monotonic() + args.agent_timeout
    while time.monotonic() < deadline:
        if console.agent_ready(lab, api, args.vmid):
            break
        time.sleep(5)
    else:
        # Debian and Ubuntu generic cloud images ship without
        # qemu-guest-agent, so install it over the serial console first.
        console.bootstrap_guest_agent(lab, api, args.vmid, cloud_user, password)

    # wireguard-tools is installed later by the provision script, so its
    # config directory does not exist yet. Create it, locked down, before the
    # private key lands in it.
    _exec(lab, api, args.vmid,
          "mkdir -p /etc/wireguard && chmod 700 /etc/wireguard", timeout=60)
    _write_guest_file(lab, api, args.vmid, "/etc/wireguard/wg0.conf",
                      render_wg_config())
    _exec(lab, api, args.vmid, "chmod 600 /etc/wireguard/wg0.conf", timeout=60)
    _write_guest_file(lab, api, args.vmid, "/tmp/labgw-nftables.nft",
                      render_nftables())
    _write_guest_file(lab, api, args.vmid, "/tmp/labgw-dnsmasq.conf",
                      render_dnsmasq())
    _write_guest_file(lab, api, args.vmid, "/tmp/labgw-provision.sh",
                      provision_script())
    result = _exec(lab, api, args.vmid, "bash /tmp/labgw-provision.sh",
                   timeout=args.provision_timeout)
    if result["exitcode"] not in (0, None):
        raise lab.LabError(
            "gateway provisioning failed: "
            + (result["stderr"] or result["stdout"])[-600:]
        )

    # The console password existed only to bootstrap the agent. Proxmox keeps
    # cipassword in the VM config, so leaving it behind would park a live
    # credential in cleartext for the life of the guest.
    try:
        api.call("PUT", f"/nodes/{lab.NODE}/qemu/{args.vmid}/config",
                 {"delete": "cipassword"})
        cipassword_cleared = True
    except lab.LabError:
        cipassword_cleared = False

    lab.audit("vpn-gateway-created", lease=args.lease, vmid=args.vmid,
              bridge=LAB_BRIDGE, endpoint=WG_ENDPOINT)
    print(json.dumps(
        {
            "vmid": args.vmid,
            "name": name,
            "lab_bridge": LAB_BRIDGE,
            "lab_gateway_ip": LAB_GATEWAY_IP,
            "provisioned": True,
            "bootstrap_password_cleared": cipassword_cleared,
            "next": [
                f"verify: proxmox-lab net verify --lease {args.lease} "
                f"--vmid {args.vmid}",
                f"attach a guest: proxmox-lab net attach --lease {args.lease} "
                "--vmid <guest> --gateway-vmid " + str(args.vmid),
            ],
        },
        indent=2,
        sort_keys=True,
    ))


def cmd_verify(lab: Any, args: Any) -> None:
    """Confirm the tunnel is up, egress uses it, and it fails closed."""
    api = lab.ProxmoxAPI()
    lab.load_lease(args.lease)
    checks: dict[str, Any] = {}

    # Age, not merely presence: a handshake from an hour ago means the tunnel
    # is dead, but a bare "has it ever handshaked" check would still pass.
    handshake = _exec(
        lab, api, args.vmid,
        "hs=$(wg show wg0 latest-handshakes | awk '{print $2; exit}'); "
        "echo \"${hs:-0} $(( $(date +%s) - ${hs:-0} ))\"",
        timeout=60,
    )
    parts = handshake["stdout"].strip().split()
    last = int(parts[0]) if parts and parts[0].lstrip("-").isdigit() else 0
    age = int(parts[1]) if len(parts) > 1 and parts[1].lstrip("-").isdigit() else -1
    checks["wireguard_handshake"] = {
        "epoch": last,
        "age_seconds": age,
        "ok": last > 0 and 0 <= age <= args.max_handshake_age,
    }

    egress = _exec(
        lab, api, args.vmid,
        "curl -fsS --max-time 20 https://api.ipify.org || true", timeout=60,
    )
    tunnel_ip = egress["stdout"].strip()
    checks["egress_ip_through_tunnel"] = tunnel_ip

    # Reference the home WAN from the controller, which shares the LAN. The
    # old in-guest probe hardcoded `--interface eth0`, so any other naming
    # made this check fail for a reason unrelated to the tunnel.
    home_ip = _controller_public_ip()
    checks["home_wan_ip"] = home_ip or "unknown"
    checks["egress_differs_from_home_wan"] = bool(
        tunnel_ip and home_ip and tunnel_ip != home_ip
    )

    rules = _exec(lab, api, args.vmid, "nft list chain inet labgw forward",
                  timeout=60)
    ruleset = rules["stdout"]
    checks["forward_policy_drop"] = "policy drop" in ruleset
    # Check what every accept rule actually permits, rather than searching for
    # one known-bad literal: a rule carrying a counter or a different
    # interface name would slip straight past a substring match.
    accepts = [
        line.strip() for line in ruleset.splitlines()
        if line.strip().endswith("accept")
    ]
    checks["accept_rules"] = accepts
    checks["every_accept_rule_uses_the_tunnel"] = bool(accepts) and all(
        '"wg0"' in line for line in accepts
    )

    healthy = (
        checks["wireguard_handshake"]["ok"]
        and checks["egress_differs_from_home_wan"]
        and checks["forward_policy_drop"]
        and checks["every_accept_rule_uses_the_tunnel"]
    )
    lab.audit("vpn-gateway-verified", vmid=args.vmid, healthy=healthy,
              egress_ip=tunnel_ip, sync=False)
    print(json.dumps({"vmid": args.vmid, "healthy": healthy, "checks": checks},
                     indent=2, sort_keys=True))
    if not healthy:
        raise lab.LabError("VPN gateway did not pass every check")


def _fetch(url: str, family: str = "-4", timeout: int = 15) -> str:
    """A fetch one-liner that survives a minimal guest.

    A stock Alpine image has no curl, and its busybox wget cannot do HTTPS
    without ca-certificates, so an HTTPS-only probe reports "no egress" on a
    guest whose network is perfectly fine. Try curl, then wget over HTTPS,
    then the same host over plain HTTP.
    """
    plain = url.replace("https://", "http://", 1)
    return (
        f"(curl -s {family} --max-time {timeout} {url} "
        f"|| wget -q {family} -O- -T {timeout} {url} "
        f"|| wget -q {family} -O- -T {timeout} {plain} "
        f"|| echo NONE) 2>/dev/null"
    )


def _ip_via_dns() -> str:
    """Report the public IP using DNS alone.

    A stock Alpine cloud image has no curl, no wget and no sudo, so nothing
    can be fetched or installed. It does have nslookup, and OpenDNS answers
    `myip.opendns.com` with the querying address -- which, through the
    gateway, is the tunnel exit. The last Address line is the answer; the
    first is the server that was asked.
    """
    # The port suffix differs by implementation: GNU nslookup writes
    # "1.2.3.4#53", busybox writes "1.2.3.4:53". Strip either, but only a
    # trailing separator-plus-digits so an IPv6 answer survives intact.
    return (
        "nslookup myip.opendns.com resolver1.opendns.com 2>/dev/null "
        "| awk '/^Address/{a=$NF} END{sub(/[#:][0-9]+$/,\"\",a); "
        "print (a==\"\")?\"NONE\":a}'"
    )


def _ping(target: str = "1.1.1.1", count: int = 2, wait: int = 3) -> str:
    """ICMP reachability, available on every image including busybox."""
    return (
        f"ping -c{count} -W{wait} {target} >/dev/null 2>&1 "
        f"&& echo REACHABLE || echo UNREACHABLE"
    )


def _controller_public_ip() -> str:
    """The home WAN address, as seen from the controller on the same LAN."""
    from urllib import request as _request

    try:
        req = _request.Request(
            "https://api.ipify.org", headers={"User-Agent": "proxmox-agent-lab"}
        )
        with _request.urlopen(req, timeout=15) as response:
            return response.read().decode().strip()
    except OSError:
        return ""


def cmd_leak_test(lab: Any, args: Any) -> None:
    """Prove a guest on the lab network cannot reach the internet un-tunnelled.

    Run from inside the guest over the serial console, so it works on an
    image with no guest agent. Checks the three ways traffic actually leaks:
    IPv4 egress outside the tunnel, IPv6 egress alongside it, and DNS queries
    going to the home resolver.
    """
    import sys as _sys

    api = lab.ProxmoxAPI()
    lab.load_lease(args.lease)
    # An empty line is a legitimate credential: a stock image can have no
    # console password at all. Only a missing flag means "none was offered".
    password = _sys.stdin.readline().rstrip("\r\n") if args.password_stdin else None
    if password is None:
        raise lab.LabError("provide the guest console password on stdin")

    home_ip = _controller_public_ip()
    checks: dict[str, Any] = {"home_wan_ip": home_ip or "unknown"}

    with console.TermSession(lab, api, "qemu", args.vmid, timeout=30) as term:
        term.login(args.user, password)

        route = term.run("ip -4 route get 1.1.1.1 2>&1 | head -1")
        checks["default_route_via_gateway"] = LAB_GATEWAY_IP in route

        # ICMP first: it needs no HTTP client, so it separates "the network is
        # broken" from "this image has no curl".
        checks["icmp_egress"] = term.run(_ping()).strip()
        checks["icmp_egress_works"] = checks["icmp_egress"] == "REACHABLE"

        tunnel_ip = term.run(_fetch("https://api.ipify.org")).strip()
        if tunnel_ip in ("", "NONE") and checks["icmp_egress_works"] \
                and args.install_tools:
            # A minimal image has no HTTPS-capable client, which would leave
            # the exit address unproven. Egress works, so fetch one.
            term.run(
                "(command -v apk >/dev/null && sudo apk add --no-cache curl "
                "ca-certificates) || (command -v apt-get >/dev/null && sudo "
                "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl) "
                "|| true",
                timeout=300,
            )
            checks["installed_http_client"] = True
            tunnel_ip = term.run(_fetch("https://api.ipify.org")).strip()
        if tunnel_ip in ("", "NONE") and checks["icmp_egress_works"]:
            # Last resort: ask DNS. Works on an image with no HTTP client and
            # no way to install one.
            tunnel_ip = term.run(_ip_via_dns(), timeout=120).strip()
            if tunnel_ip not in ("", "NONE"):
                checks["exit_ip_source"] = "dns (myip.opendns.com)"
        checks["egress_ipv4"] = tunnel_ip
        checks["ipv4_leaks_home_wan"] = bool(
            tunnel_ip and home_ip and tunnel_ip == home_ip
        )
        checks["ipv4_egress_works"] = bool(tunnel_ip and tunnel_ip != "NONE")

        v6 = term.run(_fetch("https://api6.ipify.org", "-6", 8)).strip()
        checks["egress_ipv6"] = v6 or "NONE"
        # The tunnel carries ::/0, so any working IPv6 that is not tunnelled
        # would be a leak. No IPv6 at all is a pass.
        checks["ipv6_present"] = bool(v6 and v6 != "NONE")

        resolv = term.run("cat /etc/resolv.conf | grep -i nameserver")
        checks["resolvers"] = [
            line.split()[-1]
            for line in resolv.splitlines()
            if line.lower().strip().startswith("nameserver")
        ]
        checks["dns_via_lab_gateway"] = any(
            LAB_GATEWAY_IP in r for r in checks["resolvers"]
        )
        checks["dns_resolves"] = "NXDOMAIN" not in term.run(
            "nslookup example.com 2>&1 | tail -3"
        )

        if args.gateway_vmid:
            # Kill-switch: with the tunnel down, egress must stop entirely
            # rather than fall back to the home WAN.
            _exec(lab, api, args.gateway_vmid, "systemctl stop wg-quick@wg0",
                  timeout=120)
            time.sleep(3)
            during = term.run(_ping()).strip()
            checks["icmp_with_tunnel_down"] = during or "NO RESULT"
            # Only UNREACHABLE proves the kill switch held. An empty result
            # means the probe itself failed, which must never be reported as
            # either a pass or a leak.
            checks["fails_closed"] = (
                True if during == "UNREACHABLE"
                else False if during == "REACHABLE"
                else None
            )
            _exec(lab, api, args.gateway_vmid, "systemctl start wg-quick@wg0",
                  timeout=120)
            time.sleep(8)
            after = term.run(_ping()).strip()
            checks["icmp_after_tunnel_returns"] = after
            checks["recovers_after_tunnel_returns"] = after == "REACHABLE"

    failures = []
    if not checks["icmp_egress_works"]:
        failures.append("no egress at all: the guest cannot reach the internet")
    if not checks["ipv4_egress_works"]:
        if checks["icmp_egress_works"]:
            # The network is up; the guest simply cannot speak HTTP(S), so the
            # exit address is unproven rather than wrong.
            checks["egress_ip_unproven"] = (
                "ICMP egress works but no usable HTTP client was found in the "
                "guest, so the exit address could not be confirmed from here"
            )
            failures.append(
                "could not confirm the exit IP from the guest (no HTTP client)"
            )
        else:
            failures.append("no IPv4 egress at all through the tunnel")
    if checks["ipv4_leaks_home_wan"]:
        failures.append("IPv4 egress is the home WAN address: TUNNEL BYPASSED")
    if not checks["default_route_via_gateway"]:
        failures.append("default route does not point at the lab gateway")
    if not checks["dns_via_lab_gateway"]:
        failures.append("guest is not using the lab gateway resolver")
    if args.gateway_vmid:
        if checks.get("fails_closed") is False:
            failures.append(
                "egress continued with the tunnel down: KILL SWITCH LEAK"
            )
        elif checks.get("fails_closed") is None:
            failures.append(
                "kill-switch probe returned nothing, so it is unproven "
                "(this is an inconclusive test, not a detected leak)"
            )
        if not checks.get("recovers_after_tunnel_returns"):
            failures.append("egress did not recover after the tunnel returned")

    lab.audit("vpn-leak-test", lease=args.lease, vmid=args.vmid,
              passed=not failures, egress_ip=checks.get("egress_ipv4"),
              failures=failures)
    print(json.dumps(
        {"vmid": args.vmid, "passed": not failures, "failures": failures,
         "checks": checks},
        indent=2,
        sort_keys=True,
    ))
    if failures:
        raise lab.LabError("VPN leak test failed")


def cmd_attach(lab: Any, args: Any) -> None:
    """Move a guest onto the VPN-only lab network."""
    api = lab.ProxmoxAPI()
    lab.require_lease_resource(lab.load_lease(args.lease), "qemu", args.vmid)
    config = api.call("GET", f"/nodes/{lab.NODE}/qemu/{args.vmid}/config")
    current = config.get(args.nic, "")
    if not current:
        raise lab.LabError(f"{args.nic} is not configured on VMID {args.vmid}")
    replaced = []
    for part in current.split(","):
        replaced.append(
            f"bridge={LAB_BRIDGE}" if part.startswith("bridge=") else part
        )
    updated = ",".join(replaced)
    api.call("PUT", f"/nodes/{lab.NODE}/qemu/{args.vmid}/config",
             {args.nic: updated, f"ipconfig{args.nic[-1]}": "ip=dhcp"})
    lab.audit("vpn-guest-attached", lease=args.lease, vmid=args.vmid,
              nic=args.nic, bridge=LAB_BRIDGE, sync=False)
    print(json.dumps(
        {
            "vmid": args.vmid,
            "nic": args.nic,
            "bridge": LAB_BRIDGE,
            "note": (
                "reboot the guest (or replug the NIC) to pick up DHCP from the "
                "gateway; all its egress now fails closed without the tunnel"
            ),
        },
        indent=2,
        sort_keys=True,
    ))


# --- optional spawnable DHCP + TFTP servers ------------------------------
#
# Two disposable, lease-owned dnsmasq servers that guests on the lab bridge
# can optionally use: a DHCP server (with an optional PXE `dhcp-boot`
# next-server) and a TFTP server for boot files. Neither is created by
# default; spawn one per lease with `net dhcp-create` / `net tftp-create`.
# Together (dhcp-boot pointing at the TFTP server) they form a minimal PXE
# stack for netbooting installers on guests that lack an optical drive.

DHCP_SERVER_IP = "10.66.0.2"
TFTP_SERVER_IP = "10.66.0.3"
TFTP_ROOT = "/srv/tftp"

SERVER_PROVISION = """#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq dnsmasq >/dev/null
IFACE=$(ip -o -4 addr show | awk -v a="__SERVER_IP__/" 'index($4, a) == 1 {print $2; exit}')
if [ -z "$IFACE" ]; then
  echo "no interface holds __SERVER_IP__" >&2
  ip -o -4 addr show >&2
  exit 1
fi
sed "s/__LAB_IF__/$IFACE/g" /tmp/lab-server.conf > /etc/dnsmasq.d/lab-server.conf
chmod 0644 /etc/dnsmasq.d/lab-server.conf
__PRE_START__
systemctl enable --now dnsmasq
systemctl restart dnsmasq
sleep 2
__VERIFY__
echo provisioned
"""


def _sibling_ip(gateway: str, last_octet: int) -> str:
    """The lab gateway's address with the final octet replaced."""
    parts = gateway.split(".")
    parts[-1] = str(last_octet)
    return ".".join(parts)


def _ensure_agent(lab: Any, api: Any, vmid: int, cloud_user: str,
                  password: str, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if console.agent_ready(lab, api, vmid):
            return
        time.sleep(5)
    console.bootstrap_guest_agent(lab, api, vmid, cloud_user, password)


def _clear_bootstrap_password(lab: Any, api: Any, vmid: int) -> bool:
    try:
        api.call("PUT", f"/nodes/{lab.NODE}/qemu/{vmid}/config",
                 {"delete": "cipassword"})
        return True
    except lab.LabError:
        return False


def _spawn_dnsmasq_server(lab: Any, api: Any, args: Any, *, role: str,
                          conf: str, verify: str, server_ip: str,
                          name: str, pre_start: str = "") -> tuple[str, bool]:
    """Clone a template, install dnsmasq, apply conf, verify, return (name, ok)."""
    lease = lab.load_lease(args.lease)
    if args.vmid in lease["initial_vmids"]:
        raise lab.LabError(f"VMID {args.vmid} existed before this lease")
    require_bridge(lab, api)
    upid = api.call(
        "POST",
        f"/nodes/{lab.NODE}/qemu/{args.template}/clone",
        {"newid": args.vmid, "name": name, "full": 1, "target": lab.NODE},
    )
    lab.wait_task(api, upid, timeout=args.clone_timeout)
    lab.register_resource(lease, "qemu", args.vmid, args.policy, name)

    # A console password is set before first boot so the serial bootstrap can
    # install qemu-guest-agent when the image lacks one; it is cleared after.
    password = secrets.token_urlsafe(18)
    template_config = api.call(
        "GET", f"/nodes/{lab.NODE}/qemu/{args.template}/config"
    )
    cloud_user = template_config.get("ciuser") or "debian"
    prefix = LAB_NETWORK.split("/")[1]
    api.call(
        "PUT",
        f"/nodes/{lab.NODE}/qemu/{args.vmid}/config",
        {
            "cores": args.cores,
            "memory": args.memory,
            "ciuser": cloud_user,
            "cipassword": password,
            # net0 on the home bridge gives the server egress for its own
            # provisioning (apt); net1 on the lab bridge is where it serves.
            # dnsmasq binds only the lab-side interface, so it never answers
            # on the egress NIC.
            "net0": "virtio,bridge=vmbr0,firewall=1",
            "net1": f"virtio,bridge={LAB_BRIDGE}",
            "ipconfig0": "ip=dhcp",
            "ipconfig1": f"ip={server_ip}/{prefix},gw={LAB_GATEWAY_IP}",
            "agent": "enabled=1",
            "onboot": 0,
            "tags": f"codex-lab;lease-{args.lease};{role}",
        },
    )
    start = api.call(
        "POST", f"/nodes/{lab.NODE}/qemu/{args.vmid}/status/start"
    )
    lab.wait_task(api, start, timeout=180)
    _ensure_agent(lab, api, args.vmid, cloud_user, password, args.agent_timeout)

    _write_guest_file(lab, api, args.vmid, "/tmp/lab-server.conf", conf)
    provision = SERVER_PROVISION.replace(
        "__SERVER_IP__", server_ip
    ).replace("__PRE_START__", pre_start).replace("__VERIFY__", verify)
    _write_guest_file(lab, api, args.vmid, "/tmp/lab-server-provision.sh",
                      provision)
    result = _exec(lab, api, args.vmid, "bash /tmp/lab-server-provision.sh",
                   timeout=args.provision_timeout)
    if result["exitcode"] not in (0, None):
        raise lab.LabError(
            f"{role} provisioning failed: "
            + (result["stderr"] or result["stdout"])[-600:]
        )
    cleared = _clear_bootstrap_password(lab, api, args.vmid)
    return name, cleared


def cmd_dhcp_create(lab: Any, args: Any) -> None:
    """Spawn a lease-owned DHCP server (optional, dnsmasq)."""
    import json

    api = lab.ProxmoxAPI()
    server_ip = args.server_ip or _sibling_ip(LAB_GATEWAY_IP, 2)
    name = args.name or f"dhcp-{args.vmid}"
    dns = args.dns or LAB_GATEWAY_IP
    rng = args.range or f"{LAB_DHCP_RANGE[0]},{LAB_DHCP_RANGE[1]}"
    conf = (
        "interface=__LAB_IF__\n"
        "bind-interfaces\n"
        "domain-needed\n"
        "bogus-priv\n"
        "no-resolv\n"
        f"server={dns}\n"
        f"dhcp-range={rng},12h\n"
        f"dhcp-option=option:router,{args.gateway or LAB_GATEWAY_IP}\n"
        f"dhcp-option=option:dns-server,{dns}\n"
    )
    if args.bootfile:
        next_server = args.next_server or _sibling_ip(LAB_GATEWAY_IP, 3)
        conf += f"dhcp-boot={args.bootfile},{next_server}\n"
    verify = (
        "ss -ulnp | grep -q ':67 ' || "
        "{ echo 'dnsmasq not listening on :67' >&2; exit 1; }\n"
        "mkdir -p /var/lib/misc && touch /var/lib/misc/dnsmasq.leases"
    )
    name, cleared = _spawn_dnsmasq_server(
        lab, api, args, role="dhcp", conf=conf, verify=verify,
        server_ip=server_ip, name=name,
    )
    lab.audit("dhcp-server-created", lease=args.lease, vmid=args.vmid,
              server_ip=server_ip, range=rng,
              bootfile=args.bootfile or None, sync=False)
    print(json.dumps({
        "vmid": args.vmid, "name": name, "server_ip": server_ip,
        "range": rng, "dns": dns, "gateway": args.gateway or LAB_GATEWAY_IP,
        "bootfile": args.bootfile or None,
        "next_server": (args.next_server or _sibling_ip(LAB_GATEWAY_IP, 3))
                       if args.bootfile else None,
        "provisioned": True,
        "bootstrap_password_cleared": cleared,
        "next": (
            f"see leases: proxmox-lab net dhcp leases --lease {args.lease} "
            f"--vmid {args.vmid}"
        ),
    }, indent=2, sort_keys=True))


def cmd_dhcp_leases(lab: Any, args: Any) -> None:
    """Show the DHCP server's current lease table."""
    import json

    api = lab.ProxmoxAPI()
    lab.load_lease(args.lease)
    result = _exec(lab, api, args.vmid,
                   "cat /var/lib/misc/dnsmasq.leases 2>/dev/null || true",
                   timeout=30)
    leases = []
    for line in result.get("stdout", "").splitlines():
        fields = line.split()
        if len(fields) >= 4:
            leases.append({
                "expires_at": fields[0], "mac": fields[1], "ip": fields[2],
                "hostname": fields[3],
            })
    print(json.dumps({"vmid": args.vmid, "leases": leases}, indent=2,
                     sort_keys=True))


def cmd_tftp_create(lab: Any, args: Any) -> None:
    """Spawn a lease-owned TFTP server (optional, dnsmasq)."""
    import json

    api = lab.ProxmoxAPI()
    server_ip = args.server_ip or _sibling_ip(LAB_GATEWAY_IP, 3)
    name = args.name or f"tftp-{args.vmid}"
    root = args.root or TFTP_ROOT
    conf = (
        "interface=__LAB_IF__\n"
        "bind-interfaces\n"
        f"enable-tftp\n"
        f"tftp-root={root}\n"
    )
    verify = (
        "ss -ulnp | grep -q ':69 ' || "
        "{ echo 'dnsmasq not listening on :69' >&2; exit 1; }"
    )
    name, cleared = _spawn_dnsmasq_server(
        lab, api, args, role="tftp", conf=conf, verify=verify,
        server_ip=server_ip, name=name,
        pre_start=f"mkdir -p {root}",
    )
    lab.audit("tftp-server-created", lease=args.lease, vmid=args.vmid,
              server_ip=server_ip, root=root, sync=False)
    print(json.dumps({
        "vmid": args.vmid, "name": name, "server_ip": server_ip,
        "root": root, "provisioned": True,
        "bootstrap_password_cleared": cleared,
        "next": (
            f"stage a boot file: proxmox-lab net tftp push --lease "
            f"{args.lease} --vmid {args.vmid} --file <pxelinux.0>"
        ),
    }, indent=2, sort_keys=True))


def cmd_tftp_push(lab: Any, args: Any) -> None:
    """Stage a file into the TFTP root of a lease-owned TFTP server."""
    import json
    from pathlib import Path

    api = lab.ProxmoxAPI()
    lab.load_lease(args.lease)
    source = Path(args.file).expanduser().resolve()
    if not source.is_file():
        raise lab.LabError(f"not a regular file: {source}")
    name = args.name or source.name
    if "/" in name or "\\" in name or ".." in name:
        raise lab.LabError(f"unsafe TFTP file name: {name!r}")
    dest = f"{args.root or TFTP_ROOT}/{name}"
    payload = base64.b64encode(source.read_bytes()).decode()
    api.call(
        "POST",
        f"/nodes/{lab.NODE}/qemu/{args.vmid}/agent/file-write",
        {"file": dest, "content": payload, "encode": 0},
    )
    check = _exec(
        lab, api, args.vmid, f"stat -c %s {dest} 2>/dev/null || echo 0",
        timeout=60,
    )
    try:
        size = int(check.get("stdout", "0").strip())
    except ValueError:
        size = -1
    if size != source.stat().st_size:
        raise lab.LabError(
            f"staged file size mismatch on {dest}: {size} != "
            f"{source.stat().st_size}"
        )
    lab.audit("tftp-file-pushed", lease=args.lease, vmid=args.vmid,
              path=dest, bytes=size, sync=False)
    print(json.dumps({
        "vmid": args.vmid, "path": dest, "bytes": size, "name": name,
    }, indent=2, sort_keys=True))


def cmd_status(lab: Any, args: Any) -> None:
    api = lab.ProxmoxAPI()
    bridges = api.call("GET", f"/nodes/{lab.NODE}/network") or []
    guests = api.call("GET", "/cluster/resources", {"type": "vm"}) or []
    on_lab_net = []
    for guest in guests:
        vmid = guest.get("vmid")
        if not vmid or guest.get("type") != "qemu":
            continue
        try:
            config = api.call("GET", f"/nodes/{lab.NODE}/qemu/{vmid}/config")
        except lab.LabError:
            continue
        for key, value in config.items():
            if key.startswith("net") and f"bridge={LAB_BRIDGE}" in str(value):
                on_lab_net.append({"vmid": vmid, "name": config.get("name"),
                                   "nic": key})
    print(json.dumps(
        {
            "lab_bridge": LAB_BRIDGE,
            "bridge_present": any(b.get("iface") == LAB_BRIDGE for b in bridges),
            "endpoint": WG_ENDPOINT,
            "guests_on_lab_network": on_lab_net,
        },
        indent=2,
        sort_keys=True,
    ))


def register(sub: Any, lab: Any) -> None:
    def bind(handler: Any) -> Any:
        return lambda args: handler(lab, args)

    net = sub.add_parser("net", help="forced-VPN egress for lab guests")
    net_sub = net.add_subparsers(dest="net_command", required=True)

    bridge = net_sub.add_parser(
        "host-bridge", help=f"create {LAB_BRIDGE} (host networking change)"
    )
    bridge.add_argument("--host-change-authorized", action="store_true")
    bridge.set_defaults(func=bind(cmd_host_bridge))

    create = net_sub.add_parser("gateway-create", help="build the VPN gateway VM")
    create.add_argument("--lease", required=True)
    create.add_argument("--vmid", type=int, required=True)
    create.add_argument("--name")
    create.add_argument("--template", type=int, default=GATEWAY_TEMPLATE_VMID)
    create.add_argument("--cores", type=int, default=2)
    create.add_argument("--memory", type=int, default=1024)
    create.add_argument("--policy", choices=("delete", "retain"), default="delete")
    create.add_argument("--clone-timeout", type=int, default=1800)
    # A cloud image that ships qemu-guest-agent starts it within a minute of
    # boot. Waiting longer just delays the serial bootstrap that the generic
    # Debian and Ubuntu images always need.
    create.add_argument("--agent-timeout", type=int, default=120)
    create.add_argument("--provision-timeout", type=int, default=1200)
    create.set_defaults(func=bind(cmd_gateway_create))

    verify = net_sub.add_parser("verify", help="prove egress uses the tunnel")
    verify.add_argument("--lease", required=True,
                        help="required: this runs commands inside the gateway")
    verify.add_argument("--vmid", type=int, required=True)
    verify.add_argument("--max-handshake-age", type=int, default=300,
                        help="seconds; older than this means a dead tunnel")
    verify.set_defaults(func=bind(cmd_verify))

    leak = net_sub.add_parser(
        "leak-test", help="prove a guest cannot reach the internet un-tunnelled"
    )
    leak.add_argument("--lease", required=True)
    leak.add_argument("--vmid", type=int, required=True,
                      help="a guest already attached to the lab network")
    leak.add_argument("--user", default="alpine")
    leak.add_argument("--password-stdin", action="store_true", required=True,
                      help="guest console password, read from stdin; an empty "
                           "line means the guest has no password")
    leak.add_argument("--no-install-tools", dest="install_tools",
                      action="store_false",
                      help="do not install curl in the guest to confirm the exit IP")
    leak.add_argument("--gateway-vmid", type=int,
                      help="also test the kill switch by cycling wg0 there")
    leak.set_defaults(func=bind(cmd_leak_test))

    attach = net_sub.add_parser("attach", help="move a guest behind the gateway")
    attach.add_argument("--lease", required=True)
    attach.add_argument("--vmid", type=int, required=True)
    attach.add_argument("--nic", default="net0")
    attach.add_argument("--gateway-vmid", type=int)
    attach.set_defaults(func=bind(cmd_attach))

    status = net_sub.add_parser("status", help="show the lab network")
    status.set_defaults(func=bind(cmd_status))

    dhcp = net_sub.add_parser(
        "dhcp-create",
        help="spawn a lease-owned DHCP server (optional; dnsmasq)",
    )
    dhcp.add_argument("--lease", required=True)
    dhcp.add_argument("--vmid", type=int, required=True)
    dhcp.add_argument("--name")
    dhcp.add_argument("--template", type=int, default=GATEWAY_TEMPLATE_VMID)
    dhcp.add_argument("--server-ip", help=f"default {DHCP_SERVER_IP}")
    dhcp.add_argument("--range",
                      help=f"dhcp-range, default {LAB_DHCP_RANGE[0]},{LAB_DHCP_RANGE[1]}")
    dhcp.add_argument("--gateway", help=f"router option, default {LAB_GATEWAY_IP}")
    dhcp.add_argument("--dns", help=f"dns-server option, default {LAB_GATEWAY_IP}")
    dhcp.add_argument("--bootfile",
                      help="PXE: boot filename offered by DHCP (enables dhcp-boot)")
    dhcp.add_argument("--next-server",
                      help=f"PXE: TFTP server for dhcp-boot, default {TFTP_SERVER_IP}")
    dhcp.add_argument("--cores", type=int, default=1)
    dhcp.add_argument("--memory", type=int, default=1024)
    dhcp.add_argument("--policy", choices=("delete", "retain"), default="delete")
    dhcp.add_argument("--clone-timeout", type=int, default=1800)
    dhcp.add_argument("--agent-timeout", type=int, default=120)
    dhcp.add_argument("--provision-timeout", type=int, default=1200)
    dhcp.set_defaults(func=bind(cmd_dhcp_create))

    dhcp_leases = net_sub.add_parser(
        "dhcp-leases", help="show the DHCP server's lease table"
    )
    dhcp_leases.add_argument("--lease", required=True)
    dhcp_leases.add_argument("--vmid", type=int, required=True)
    dhcp_leases.set_defaults(func=bind(cmd_dhcp_leases))

    tftp = net_sub.add_parser(
        "tftp-create",
        help="spawn a lease-owned TFTP server (optional; dnsmasq)",
    )
    tftp.add_argument("--lease", required=True)
    tftp.add_argument("--vmid", type=int, required=True)
    tftp.add_argument("--name")
    tftp.add_argument("--template", type=int, default=GATEWAY_TEMPLATE_VMID)
    tftp.add_argument("--server-ip", help=f"default {TFTP_SERVER_IP}")
    tftp.add_argument("--root", help=f"TFTP root, default {TFTP_ROOT}")
    tftp.add_argument("--cores", type=int, default=1)
    tftp.add_argument("--memory", type=int, default=1024)
    tftp.add_argument("--policy", choices=("delete", "retain"), default="delete")
    tftp.add_argument("--clone-timeout", type=int, default=1800)
    tftp.add_argument("--agent-timeout", type=int, default=120)
    tftp.add_argument("--provision-timeout", type=int, default=1200)
    tftp.set_defaults(func=bind(cmd_tftp_create))

    tftp_push = net_sub.add_parser(
        "tftp-push", help="stage a file into a TFTP server's root"
    )
    tftp_push.add_argument("--lease", required=True)
    tftp_push.add_argument("--vmid", type=int, required=True)
    tftp_push.add_argument("--file", required=True)
    tftp_push.add_argument("--name", help="name in the TFTP root (default: file name)")
    tftp_push.add_argument("--root", help=f"TFTP root, default {TFTP_ROOT}")
    tftp_push.set_defaults(func=bind(cmd_tftp_push))
