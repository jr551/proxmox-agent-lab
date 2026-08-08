"""Disposable, pre-authenticated links to a guest console.

Send someone a URL; they see one VM's screen in their browser, with no
Proxmox account, no VPN, and no client to install. The link dies on its own.

How it fits together
--------------------
A small worker VM runs two things: `pxl-share`, a stdlib HTTP server that
serves noVNC and relays the console, and `ngrok`, which gives it a public
address. The controller talks to the worker through the guest agent -- there
is no control port to secure.

Why the worker holds an API token
---------------------------------
A Proxmox VNC ticket lives for seconds and is spent by the first connection,
so it cannot be baked into a link that someone opens later. The worker mints
one at the moment of connection instead. That is the whole reason this needs
a worker at all rather than a URL.

What that means for trust
-------------------------
The worker can open a console on any guest its token allows, so treat it as
sensitive: it is created inside a lease, stopped when idle, and destroyed with
the lease. A link grants one VMID until it expires, and nothing else.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
import time
from typing import Any
from urllib import error, request

from . import config as _config
from . import console
from . import secrets_store

_CONFIG = _config.get()

ENABLED = bool(_CONFIG.share.get("enabled", False))
WORKER_VMID = int(_CONFIG.share.get("worker_vmid", 0) or 0)
PORT = int(_CONFIG.share.get("port", 8900))
DEFAULT_MINUTES = int(_CONFIG.share.get("default_minutes", 30))
MAX_MINUTES = int(_CONFIG.share.get("max_minutes", 480))
NOVNC_VERSION = _CONFIG.share.get("novnc_version", "1.6.0")
REGION = _CONFIG.share.get("ngrok_region", "")
# cloudflared's quick tunnels need no account and no credential at all, which
# is one fewer thing to hold and one fewer thing to get wrong.
TUNNEL = _CONFIG.share.get("tunnel", "cloudflared")


class ShareError(RuntimeError):
    pass


SETUP_SCRIPT = """#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl tar ca-certificates python3 >/dev/null

# noVNC is plain static files; no build step, no runtime.
NOVNC=__NOVNC__
if [ ! -f /opt/novnc/core/rfb.js ]; then
  curl -fsSL -o /tmp/novnc.tar.gz \\
    "https://github.com/novnc/noVNC/archive/refs/tags/v${NOVNC}.tar.gz"
  rm -rf /opt/novnc /tmp/novnc-src && mkdir -p /tmp/novnc-src
  tar -xzf /tmp/novnc.tar.gz -C /tmp/novnc-src --strip-components=1
  mv /tmp/novnc-src /opt/novnc
fi
[ -f /opt/novnc/core/rfb.js ] || { echo "noVNC did not unpack" >&2; exit 1; }

if [ "__TUNNEL__" = "cloudflared" ] && ! command -v cloudflared >/dev/null 2>&1; then
  curl -fsSL -o /usr/local/bin/cloudflared \\
    https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
  chmod +x /usr/local/bin/cloudflared
fi
if [ "__TUNNEL__" = "ngrok" ] && ! command -v ngrok >/dev/null 2>&1; then
  curl -fsSL -o /tmp/ngrok.tgz \\
    https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-amd64.tgz
  tar -xzf /tmp/ngrok.tgz -C /usr/local/bin ngrok
  chmod +x /usr/local/bin/ngrok
fi

install -d -m 0700 /etc/pxl-share /var/lib/pxl-share
install -m 0755 /tmp/pxl-share.py /usr/local/bin/pxl-share

cat > /etc/systemd/system/pxl-share.service <<UNIT
[Unit]
Description=disposable console sharing
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/local/bin/pxl-share serve --port __PORT__
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/pxl-tunnel.service <<UNIT
[Unit]
Description=public address for console sharing
After=pxl-share.service
Wants=pxl-share.service

[Service]
# systemd starts services with no HOME, and ngrok resolves its config
# relative to it -- without this it silently runs unauthenticated. Harmless
# for cloudflared, which needs no credential at all.
Environment=HOME=/root
ExecStart=__TUNNEL_CMD__
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

# Supersedes the earlier ngrok-only unit, if this worker predates it.
systemctl disable --now pxl-ngrok 2>/dev/null || true
rm -f /etc/systemd/system/pxl-ngrok.service
systemctl daemon-reload
systemctl enable --now pxl-share
if [ "__TUNNEL__" != "none" ]; then
  systemctl restart pxl-tunnel
  systemctl enable pxl-tunnel >/dev/null
fi
sleep 6
systemctl is-active pxl-share >/dev/null || { journalctl -u pxl-share -n 20 --no-pager >&2; exit 1; }
echo provisioned
"""


def tunnel_command() -> str:
    if TUNNEL == "cloudflared":
        return ("/usr/local/bin/cloudflared tunnel --no-autoupdate "
                f"--url http://127.0.0.1:{PORT}")
    if TUNNEL == "ngrok":
        region = f" --region {REGION}" if REGION else ""
        return (f"/usr/local/bin/ngrok http {PORT} --log stdout "
                f"--config /root/.config/ngrok/ngrok.yml{region}")
    return "/bin/true"


def setup_script() -> str:
    return (SETUP_SCRIPT
            .replace("__NOVNC__", NOVNC_VERSION)
            .replace("__PORT__", str(PORT))
            .replace("__TUNNEL_CMD__", tunnel_command())
            .replace("__TUNNEL__", TUNNEL)
            .replace("__REGION__", f"--region {REGION}" if REGION else ""))


def _worker(lab: Any) -> int:
    if not ENABLED:
        raise ShareError(
            "console sharing is disabled. Set [share] enabled = true and "
            "worker_vmid, then run 'proxmox-lab share setup'."
        )
    if not WORKER_VMID:
        raise ShareError("[share] worker_vmid is not set")
    return WORKER_VMID


def _run(lab: Any, api: Any, command: str, timeout: int = 120) -> str:
    result = console.agent_exec(lab, api, _worker(lab), ["/bin/bash", "-c",
                                                         command],
                                timeout=timeout)
    if result["exitcode"] not in (0, None):
        raise ShareError(
            (result["stderr"] or result["stdout"] or "command failed")[-400:]
        )
    return result["stdout"].strip()


def lan_url(lab: Any, api: Any) -> str:
    """The worker's address on the local network."""
    interfaces = api.call(
        "GET", f"/nodes/{lab.NODE}/qemu/{_worker(lab)}/agent/network-get-interfaces"
    )
    for interface in (interfaces or {}).get("result", []):
        for entry in interface.get("ip-addresses", []) or []:
            address = entry.get("ip-address", "")
            if (entry.get("ip-address-type") == "ipv4"
                    and not address.startswith("127.")):
                return f"http://{address}:{PORT}"
    raise ShareError("the share worker has no usable address")


def base_url(lab: Any, api: Any) -> tuple[str, str]:
    """Where links live: the public tunnel if there is one, else the LAN.

    ngrok is an optional convenience. Without it -- no account, or an
    authtoken that will not authenticate -- sharing still works for anyone on
    the same network, which is most of the time. Failing outright would be
    needlessly strict.
    """
    if TUNNEL == "cloudflared":
        # cloudflared announces its quick-tunnel hostname in the log and
        # offers no API to ask, so the log is the only place to read it.
        found = _run(lab, api,
                     "journalctl -u pxl-tunnel --no-pager -n 200 2>/dev/null "
                     "| grep -oE 'https://[a-z0-9-]+\\.trycloudflare\\.com' "
                     "| tail -1 || true")
        if found:
            return found.strip().rstrip("/"), "public"
    elif TUNNEL == "ngrok":
        raw = _run(lab, api, "curl -fsS --max-time 8 "
                             "http://127.0.0.1:4040/api/tunnels || true")
        tunnels: list[dict[str, Any]] = []
        if raw:
            try:
                tunnels = json.loads(raw).get("tunnels", [])
            except ValueError:
                tunnels = []
        for tunnel in tunnels:
            if tunnel.get("proto") == "https":
                return tunnel["public_url"].rstrip("/"), "public"
        if tunnels:
            return tunnels[0]["public_url"].rstrip("/"), "public"
    return lan_url(lab, api), "lan"


def ensure_worker(lab: Any, api: Any) -> None:
    vmid = _worker(lab)
    status = api.call("GET", f"/nodes/{lab.NODE}/qemu/{vmid}/status/current")
    if status.get("status") != "running":
        upid = api.call("POST", f"/nodes/{lab.NODE}/qemu/{vmid}/status/start")
        lab.wait_task(api, upid, timeout=180)
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            if console.agent_ready(lab, api, vmid):
                break
            time.sleep(5)
        else:
            raise ShareError(f"share worker {vmid} did not come up")


# --- commands -------------------------------------------------------------


def cmd_setup(lab: Any, args: Any) -> None:
    """Build the share worker: noVNC, ngrok, and the relay."""
    api = lab.ProxmoxAPI()
    lease = lab.load_lease(args.lease)
    if args.vmid in lease["initial_vmids"]:
        raise ShareError(f"VMID {args.vmid} existed before this lease")
    template = args.template or int(
        _CONFIG.network.get("gateway_template_vmid") or 0
    )
    if not template:
        raise ShareError("pass --template <vmid> of a cloud-init image")

    name = args.name or f"share-{args.vmid}"
    upid = api.call("POST", f"/nodes/{lab.NODE}/qemu/{template}/clone",
                    {"newid": args.vmid, "name": name, "full": 1,
                     "target": lab.NODE})
    lab.wait_task(api, upid, timeout=args.clone_timeout)
    lab.register_resource(lease, "qemu", args.vmid, args.policy, name)

    import secrets as _secrets
    password = _secrets.token_urlsafe(18)
    template_config = api.call("GET", f"/nodes/{lab.NODE}/qemu/{template}/config")
    cloud_user = template_config.get("ciuser") or "debian"
    api.call("PUT", f"/nodes/{lab.NODE}/qemu/{args.vmid}/config", {
        "cores": args.cores, "memory": args.memory,
        "ciuser": cloud_user, "cipassword": password,
        "ipconfig0": "ip=dhcp", "agent": "enabled=1", "onboot": 0,
        "tags": f"codex-lab;lease-{args.lease};share",
    })
    start = api.call("POST", f"/nodes/{lab.NODE}/qemu/{args.vmid}/status/start")
    lab.wait_task(api, start, timeout=180)

    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if console.agent_ready(lab, api, args.vmid):
            break
        time.sleep(5)
    else:
        console.bootstrap_guest_agent(lab, api, args.vmid, cloud_user, password)

    # The relay needs its own Proxmox credentials, because it mints a console
    # ticket per connection. Written 0600, never on a command line.
    worker_config = {
        "host": lab.HOST, "port": lab.PORT, "node": lab.NODE,
        "token_user": lab.TOKEN_USER, "token_name": lab.TOKEN_NAME,
        "token_secret": lab.keychain_secret(),
        "verify_tls": bool(lab.VERIFY_TLS),
    }
    server_source = (Path(__file__).parent / "share_server.py").read_text()
    for path, body in (("/tmp/pxl-share.py", server_source),
                       ("/tmp/pxl-share-setup.sh", setup_script())):
        api.call("POST", f"/nodes/{lab.NODE}/qemu/{args.vmid}/agent/file-write",
                 {"file": path,
                  "content": base64.b64encode(body.encode()).decode(),
                  "encode": 0})

    console.agent_exec(lab, api, args.vmid,
                       ["/bin/bash", "-c", "install -d -m 0700 /etc/pxl-share"],
                       timeout=60)
    api.call("POST", f"/nodes/{lab.NODE}/qemu/{args.vmid}/agent/file-write",
             {"file": "/etc/pxl-share/config.json",
              "content": base64.b64encode(
                  json.dumps(worker_config).encode()).decode(),
              "encode": 0})
    console.agent_exec(
        lab, api, args.vmid,
        ["/bin/bash", "-c", "chmod 600 /etc/pxl-share/config.json"], timeout=60)

    token = (secrets_store.get(_CONFIG, "ngrok-authtoken", required=False)
             if TUNNEL == "ngrok" else "")
    if token:
        console.agent_exec(
            lab, api, args.vmid,
            ["/bin/bash", "-c",
             'mkdir -p /root/.config/ngrok && '
             'HOME=/root ngrok config add-authtoken "$0" '
             '--config /root/.config/ngrok/ngrok.yml && '
             'chmod 600 /root/.config/ngrok/ngrok.yml',
             token],
            timeout=60)

    result = console.agent_exec(lab, api, args.vmid,
                                ["/bin/bash", "/tmp/pxl-share-setup.sh"],
                                timeout=args.setup_timeout)
    if result["exitcode"] not in (0, None):
        raise ShareError("share worker setup failed: "
                         + (result["stderr"] or result["stdout"])[-600:])

    try:
        api.call("PUT", f"/nodes/{lab.NODE}/qemu/{args.vmid}/config",
                 {"delete": "cipassword"})
    except lab.LabError:
        pass

    lab.audit("share-worker-provisioned", lease=args.lease, vmid=args.vmid)
    print(json.dumps({
        "vmid": args.vmid, "name": name,
        "next": ["add to your config:", "  [share]", "  enabled = true",
                 f"  worker_vmid = {args.vmid}",
                 "then: proxmox-lab share status"],
    }, indent=2, sort_keys=True))


def cmd_create(lab: Any, args: Any) -> None:
    """Mint a disposable link to one guest's console."""
    api = lab.ProxmoxAPI()
    lab.load_lease(args.lease)
    minutes = min(max(1, args.minutes), MAX_MINUTES)
    if minutes != args.minutes:
        print(json.dumps({"note": f"clamped to {minutes} minutes "
                                  f"([share] max_minutes = {MAX_MINUTES})"}),
              flush=True)
    ensure_worker(lab, api)

    label = args.label or f"VM {args.vmid}"
    once = "--once" if args.once else ""
    raw = _run(lab, api,
               f"pxl-share add --vmid {int(args.vmid)} --kind {args.kind} "
               f"--minutes {minutes} --label {json.dumps(label)} {once}")
    session = json.loads(raw)
    base, reach = base_url(lab, api)
    url = f"{base}/v/{session['token']}/"

    # The URL is the credential: audit the fact, never the token.
    lab.audit("share-created", lease=args.lease, vmid=args.vmid,
              minutes=minutes, once=bool(args.once))
    result = {
        "url": url,
        "reachable_from": reach,
        "vmid": args.vmid,
        "expires_in_minutes": minutes,
        "single_use": bool(args.once),
        "warning": "anyone with this URL can use that console until it "
                   "expires. Do not paste it anywhere public.",
    }
    if reach == "lan":
        result["note"] = (
            "no ngrok tunnel, so this link only works on your local network. "
            "Store a valid authtoken and restart the worker for a public one."
        )
    print(json.dumps(result, indent=2, sort_keys=True))


def cmd_list(lab: Any, args: Any) -> None:
    api = lab.ProxmoxAPI()
    ensure_worker(lab, api)
    print(json.dumps(json.loads(_run(lab, api, "pxl-share list") or "[]"),
                     indent=2, sort_keys=True))


def cmd_revoke(lab: Any, args: Any) -> None:
    api = lab.ProxmoxAPI()
    lab.load_lease(args.lease)
    ensure_worker(lab, api)
    command = "pxl-share revoke --all" if args.all else \
        f"pxl-share revoke --token {json.dumps(args.token)}"
    result = json.loads(_run(lab, api, command))
    lab.audit("share-revoked", lease=args.lease, count=result.get("revoked"))
    print(json.dumps(result, indent=2))


def cmd_status(lab: Any, args: Any) -> None:
    report: dict[str, Any] = {
        "enabled": ENABLED, "worker_vmid": WORKER_VMID or None,
        "port": PORT, "default_minutes": DEFAULT_MINUTES,
        "max_minutes": MAX_MINUTES,
    }
    if ENABLED and WORKER_VMID:
        api = lab.ProxmoxAPI()
        try:
            status = api.call(
                "GET", f"/nodes/{lab.NODE}/qemu/{WORKER_VMID}/status/current")
            report["guest_state"] = status.get("status")
            if status.get("status") == "running":
                report["services"] = _run(
                    lab, api,
                    "systemctl is-active pxl-share pxl-tunnel || true"
                ).split()
                try:
                    base, reach = base_url(lab, api)
                    report["base_url"] = base
                    report["reachable_from"] = reach
                except ShareError as exc:
                    report["base_url"] = f"unavailable: {exc}"
                report["active_links"] = len(
                    json.loads(_run(lab, api, "pxl-share list") or "[]"))
        except (lab.LabError, ShareError) as exc:
            report["error"] = str(exc)[:200]
    print(json.dumps(report, indent=2, sort_keys=True))


def cmd_down(lab: Any, args: Any) -> None:
    """Stop the worker. Every link dies with it."""
    api = lab.ProxmoxAPI()
    lab.load_lease(args.lease)
    vmid = _worker(lab)
    upid = api.call("POST", f"/nodes/{lab.NODE}/qemu/{vmid}/status/shutdown")
    try:
        lab.wait_task(api, upid, timeout=90)
    except lab.LabError:
        lab.wait_task(api, api.call(
            "POST", f"/nodes/{lab.NODE}/qemu/{vmid}/status/stop"), timeout=60)
    lab.audit("share-worker-stopped", lease=args.lease, vmid=vmid)
    print(json.dumps({"stopped": vmid, "links_revoked": "all"}, indent=2))


def register(sub: Any, lab: Any) -> None:
    def bind(handler: Any) -> Any:
        return lambda args: handler(lab, args)

    share = sub.add_parser(
        "share", help="disposable links to a guest console, via noVNC")
    share_sub = share.add_subparsers(dest="share_command", required=True)

    build = share_sub.add_parser("setup", help="build the share worker")
    build.add_argument("--lease", required=True)
    build.add_argument("--vmid", type=int, required=True)
    build.add_argument("--name")
    build.add_argument("--template", type=int)
    build.add_argument("--cores", type=int, default=2)
    build.add_argument("--memory", type=int, default=1024)
    build.add_argument("--policy", choices=("delete", "retain"),
                       default="retain")
    build.add_argument("--clone-timeout", type=int, default=1800)
    build.add_argument("--setup-timeout", type=int, default=1200)
    build.set_defaults(func=bind(cmd_setup))

    create = share_sub.add_parser("create", help="mint a link to one console")
    create.add_argument("--lease", required=True)
    create.add_argument("--vmid", type=int, required=True)
    create.add_argument("--kind", choices=("qemu", "lxc"), default="qemu")
    create.add_argument("--minutes", type=int, default=DEFAULT_MINUTES)
    create.add_argument("--label")
    create.add_argument("--once", action="store_true",
                        help="revoke after the first connection")
    create.set_defaults(func=bind(cmd_create))

    share_sub.add_parser("list", help="live links"
                         ).set_defaults(func=bind(cmd_list))
    share_sub.add_parser("status", help="worker and public address"
                         ).set_defaults(func=bind(cmd_status))

    revoke = share_sub.add_parser("revoke", help="kill a link, or all of them")
    revoke.add_argument("--lease", required=True)
    revoke.add_argument("--token")
    revoke.add_argument("--all", action="store_true")
    revoke.set_defaults(func=bind(cmd_revoke))

    stop = share_sub.add_parser("down", help="stop the worker; revokes all")
    stop.add_argument("--lease", required=True)
    stop.set_defaults(func=bind(cmd_down))
