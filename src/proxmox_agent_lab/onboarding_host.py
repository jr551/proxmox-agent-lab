"""Standalone first-boot payload. Uses only Python shipped with Debian/PVE.

The bundle generator appends an entry point and per-install settings. Importing
this module on a controller performs no host operations.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from urllib import parse, request

ROOT = Path("/var/lib/proxmox-agent-lab-onboarding")
SERVICE = Path("/etc/systemd/system/proxmox-agent-lab-onboarding.service")


def run(*argv, timeout=120, input=None):
    # Keep pveum token output and package diagnostics out of the journal.
    result = subprocess.run(argv, input=input, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{Path(argv[0]).name} failed with exit code {result.returncode}")
    return result.stdout


def write(path, text, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".pxl-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def management_ip(settings):
    host = parse.urlsplit(settings["callback_url"]).hostname
    for family, kind, proto, _, addr in socket.getaddrinfo(host, settings["port"], type=socket.SOCK_DGRAM):
        with socket.socket(family, kind, proto) as sock:
            try:
                sock.connect(addr)
                return sock.getsockname()[0]
            except OSError:
                continue
    raise RuntimeError("Cannot determine management address toward the controller")


def os_release() -> dict[str, str]:
    return dict(line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines() if "=" in line)


def vps_preflight():
    if os.geteuid() != 0:
        raise RuntimeError("Run as root on the fresh Debian VPS")
    release = os_release()
    if release.get("ID", "").strip('"') != "debian" or release.get("VERSION_ID", "").strip('"') != "13":
        raise RuntimeError("VPS installation supports fresh Debian 13 only")
    if run("dpkg", "--print-architecture").strip() != "amd64":
        raise RuntimeError("VPS installation requires amd64")
    container = subprocess.run(["systemd-detect-virt", "--container", "--quiet"], timeout=10)
    if container.returncode != 1:
        raise RuntimeError("A full VPS with its own bootable kernel is required; container VPSs are unsupported")
    if not Path("/sys/fs/cgroup/cgroup.controllers").exists():
        raise RuntimeError("A cgroup-v2 host is required")
    # Check namespace capability before installing packages or changing boot.
    run("unshare", "--mount", "--pid", "--fork", "true", timeout=10)


def install_kernel(settings, state):
    vps_preflight()
    if shutil.which("pveversion"):
        raise RuntimeError("Proxmox is already installed; fresh Debian is required")
    local_ip = management_ip(settings)
    fqdn = settings["fqdn"]
    run("hostnamectl", "set-hostname", fqdn)
    hosts = Path("/etc/hosts")
    # Remove only aliases for the chosen hostname, preserving other mappings.
    names = {fqdn, fqdn.split(".")[0]}
    lines = []
    for line in hosts.read_text().splitlines():
        body, marker, comment = line.partition("#")
        fields = body.split()
        if fields and any(name in names for name in fields[1:]):
            aliases = [name for name in fields[1:] if name not in names]
            if aliases:
                lines.append(" ".join([fields[0], *aliases]) + (" #" + comment if marker else ""))
        else:
            lines.append(line)
    lines.append(f"{local_ip} {fqdn} {fqdn.split('.')[0]}")
    write(state / "hosts.before", hosts.read_text())
    write(hosts, "\n".join(lines) + "\n", 0o644)
    run("apt-get", "update", timeout=600)
    run("apt-get", "install", "-y", "ca-certificates", timeout=600)
    key_url = "https://enterprise.proxmox.com/debian/proxmox-archive-keyring-trixie.gpg"
    with request.urlopen(key_url, timeout=30) as response:
        key = response.read(1024 * 1024)
    if not key:
        raise RuntimeError("Empty Proxmox signing key download")
    # HTTPS authenticates the vendor key; apt subsequently verifies repository signatures.
    key_path = Path("/usr/share/keyrings/pxl-proxmox-archive-keyring.gpg")
    key_path.write_bytes(key)
    key_path.chmod(0o644)
    write(Path("/etc/apt/sources.list.d/pxl-proxmox.sources"), '''Types: deb
URIs: https://download.proxmox.com/debian/pve
Suites: trixie
Components: pve-no-subscription
Signed-By: /usr/share/keyrings/pxl-proxmox-archive-keyring.gpg
''', 0o644)
    run("apt-get", "update", timeout=600)
    run("apt-get", "install", "-y", "proxmox-default-kernel", timeout=1800)
    write(state / "stage", "kernel-installed")
    run("systemctl", "reboot")


def install_proxmox(state):
    if not os.uname().release.endswith("-pve"):
        raise RuntimeError("The VPS did not boot the Proxmox kernel; select it in the provider console before resuming")
    vps_preflight()
    run("unshare", "--user", "--map-root-user", "true", timeout=10)
    run("apt-get", "install", "-y", "proxmox-ve", "postfix", "open-iscsi", "chrony", timeout=1800)
    write(state / "stage", "proxmox-installed")


def configure_repositories():
    """Use public Proxmox repositories on a fresh, unsubscribed PVE install."""
    release = os_release()
    suite = release.get("VERSION_CODENAME", "").strip('"')
    if suite not in ("bookworm", "trixie"):
        raise RuntimeError("Only Debian 12/13 based Proxmox installations are supported")
    sources = Path("/etc/apt/sources.list.d")
    for name in ("pve-enterprise.list", "pve-enterprise.sources", "ceph.list", "ceph.sources"):
        path = sources / name
        if path.exists() and "enterprise.proxmox.com" in path.read_text():
            backup = path.with_name(path.name + ".pxl-disabled")
            if backup.exists():
                raise RuntimeError("Repository backup already exists; inspect it before retrying")
            path.rename(backup)
    key = Path("/usr/share/keyrings/proxmox-archive-keyring.gpg")
    if not key.exists():
        key = Path("/etc/apt/trusted.gpg.d/proxmox-release-bookworm.gpg")
    if not key.exists():
        raise RuntimeError("Proxmox archive keyring missing; repair the package installation")
    write(sources / "pxl-proxmox.sources", f"Types: deb\nURIs: https://download.proxmox.com/debian/pve\n"
          f"Suites: {suite}\nComponents: pve-no-subscription\nSigned-By: {key}\n", 0o644)


def wait_for_proxmox(node):
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            run("pvesh", "get", f"/nodes/{node}/status", "--output-format", "json", timeout=10)
            return
        except (RuntimeError, subprocess.TimeoutExpired):
            time.sleep(3)
    raise RuntimeError("Proxmox services did not become ready within 180 seconds")


def configure_wifi(settings):
    wifi = settings.get("wifi")
    if not wifi:
        return
    iface = wifi["interface"]
    if not Path(f"/sys/class/net/{iface}/wireless").exists():
        raise RuntimeError("Configured wireless interface is absent; check firmware and interface name")
    run("apt-get", "update", timeout=600)
    run("apt-get", "install", "-y", "wpasupplicant", "isc-dhcp-client", timeout=600)
    path = Path("/etc/wpa_supplicant/pxl-onboarding.conf")
    write(path, "ctrl_interface=/run/wpa_supplicant\nnetwork={\n    ssid=" + wifi["ssid_hex"] +
          "\n    psk=" + wifi["psk"] + "\n    key_mgmt=WPA-PSK\n}\n")
    interfaces = Path("/etc/network/interfaces")
    text = interfaces.read_text()
    if re.search(rf"(?m)^\s*iface\s+{re.escape(iface)}\s", text):
        raise RuntimeError("Wireless interface already configured; refusing to replace it")
    dropin = Path("/etc/network/interfaces.d/pxl-wifi")
    wanted = f"auto {iface}\niface {iface} inet dhcp\n    wpa-conf {path}\n    metric 200\n"
    if dropin.exists() and dropin.read_text() != wanted:
        raise RuntimeError("Wi-Fi configuration already exists with different settings")
    write(dropin, wanted, 0o600)
    if not re.search(r"(?m)^\s*source\s+/etc/network/interfaces.d/\*\s*$", text):
        write(interfaces, text + "\nsource /etc/network/interfaces.d/*\n", 0o644)
    run("ifup", iface, timeout=120)


def configure_bridge():
    # Isolated guest networking. No changes to the management interface,
    # forwarding, NAT or provider firewall are made implicitly.
    main = Path("/etc/network/interfaces")
    text = main.read_text() if main.exists() else ""
    dropin = Path("/etc/network/interfaces.d/pxl-lab")
    wanted = "auto vmbr1\niface vmbr1 inet manual\n    bridge-ports none\n    bridge-stp off\n    bridge-fd 0\n"
    if Path("/sys/class/net/vmbr1").exists():
        if not dropin.exists() or dropin.read_text() != wanted:
            raise RuntimeError("vmbr1 already exists; refusing to claim an existing bridge")
        return
    for path in [main, *Path("/etc/network/interfaces.d").glob("*")]:
        if path.is_file() and path != dropin and re.search(r"(?m)^\s*iface\s+vmbr1\s", path.read_text()):
            raise RuntimeError("vmbr1 already has a configuration")
    write(dropin, wanted, 0o644)
    if not re.search(r"(?m)^\s*source\s+/etc/network/interfaces.d/\*\s*$", text):
        write(main, text + "\nsource /etc/network/interfaces.d/*\n", 0o644)
    run("ifup", "vmbr1")


def configure_wol():
    if not shutil.which("ethtool"):
        return ""
    routes = json.loads(run("ip", "-j", "-4", "route", "show", "default"))
    if not routes:
        return ""
    iface = routes[0].get("dev", "")
    bridge = Path("/sys/class/net") / iface / "brif"
    if bridge.is_dir():
        ports = sorted(bridge.iterdir())
        iface = ports[0].name if ports else ""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,15}", iface):
        return ""
    try:
        capabilities = run("ethtool", iface)
    except RuntimeError:
        return ""
    supported = re.search(r"Supports Wake-on:\s*(\S+)", capabilities)
    if not supported or "g" not in supported[1]:
        return ""
    run("ethtool", "-s", iface, "wol", "g")
    write(Path("/etc/network/if-up.d/pxl-wol"), f'#!/bin/sh\n[ "$IFACE" = "{iface}" ] || exit 0\nexec ethtool -s {iface} wol g\n', 0o755)
    return (Path("/sys/class/net") / iface / "address").read_text().strip()


def provision(settings, state):
    if not shutil.which("pveum"):
        raise RuntimeError("Proxmox is not installed")
    node = run("hostname", "-s").strip()
    if node != settings["fqdn"].split(".")[0]:
        raise RuntimeError("Node hostname differs from the pairing bundle")
    wait_for_proxmox(node)
    configure_repositories()
    configure_bridge()
    if not (state / "wifi-done").exists():
        configure_wifi(settings)
        write(state / "wifi-done", "done")
    key = settings.get("ssh_public_key")
    if key:
        ssh = Path("/root/.ssh")
        ssh.mkdir(mode=0o700, exist_ok=True)
        authorized = ssh / "authorized_keys"
        existing = authorized.read_text() if authorized.exists() else ""
        if key not in existing.splitlines():
            write(authorized, existing.rstrip() + "\n" + key + "\n")
    user = f"pxl-{settings['id']}@pve"
    users = json.loads(run("pveum", "user", "list", "--output-format", "json"))
    if not any(u.get("userid") == user for u in users):
        run("pveum", "user", "add", user, "--comment", "Agent lab onboarding")
    credential = state / "api-token.json"
    if not credential.exists():
        result = json.loads(run("pveum", "user", "token", "add", user, "controller", "--privsep", "1", "--output-format", "json"))
        write(credential, json.dumps(result))
    token = json.loads(credential.read_text())["value"]
    for target in (("--users", user), ("--tokens", user + "!controller")):
        for path, role in (("/vms", "PVEVMAdmin"), ("/storage", "PVEDatastoreAdmin"), (f"/nodes/{node}", "PVEAuditor")):
            run("pveum", "acl", "modify", path, *target, "--roles", role)
    wol = ""
    if settings["mode"] in ("iso", "existing"):
        roles = json.loads(run("pveum", "role", "list", "--output-format", "json"))
        role = "PXLOnboardingPower"
        if not any(r.get("roleid") == role for r in roles):
            run("pveum", "role", "add", role, "--privs", "Sys.PowerMgmt")
        for target in (("--users", user), ("--tokens", user + "!controller")):
            run("pveum", "acl", "modify", f"/nodes/{node}", *target, "--roles", "PVEAuditor," + role)
        wol = configure_wol()
    payload = {"id": settings["id"], "mode": settings["mode"], "node": node,
               "host": settings["api_host"] or management_ip(settings), "token_user": user,
               "token_name": "controller", "token_secret": token,
               "ca": Path("/etc/pve/pve-root-ca.pem").read_text(), "wol_mac": wol}
    write(state / "enrollment.json", json.dumps(payload))
    return payload


def beacon(settings, payload):
    public = {"id": settings["id"], "host": payload["host"], "node": payload["node"], "time": int(time.time())}
    encoded = json.dumps(public, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(settings["token"].encode(), encoded, hashlib.sha256).hexdigest()
    return json.dumps({"payload": public, "signature": signature}).encode()


def announce(settings, payload):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.sendto(beacon(settings, payload), ("255.255.255.255", 8844))
    except OSError:
        pass  # Discovery is advisory; authenticated HTTPS completes pairing.


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def callback(settings, payload):
    context = ssl.create_default_context(cadata=settings["controller_ca"])
    opener = request.build_opener(request.ProxyHandler({}), request.HTTPSHandler(context=context), NoRedirect())
    body = json.dumps(payload).encode()
    deadline = min(time.time() + 1800, settings["expires_at"])
    while time.time() < deadline:
        if settings["mode"] == "iso":
            announce(settings, payload)
        try:
            req = request.Request(settings["callback_url"], data=body, method="POST",
                                  headers={"Authorization": "Bearer " + settings["token"], "Content-Type": "application/json"})
            with opener.open(req, timeout=15) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(10)
    raise RuntimeError("Pairing callback timed out; start the receiver and restart the onboarding service before expiry")


def entry(settings):
    state = ROOT / settings["id"]
    try:
        if os.geteuid() != 0:
            raise RuntimeError("Run as root on the intended target host")
        if time.time() >= settings["expires_at"]:
            raise RuntimeError("Pairing bundle expired; generate a new bundle")
        resume = "--resume" in sys.argv
        if settings["mode"] == "vps" and not resume:
            if not {"--host-change-authorized", "--reboot-authorized"}.issubset(sys.argv):
                raise RuntimeError("VPS installation requires --host-change-authorized --reboot-authorized")
            vps_preflight()
        if settings["mode"] == "existing" and not resume:
            if "--host-change-authorized" not in sys.argv:
                raise RuntimeError("Existing-host onboarding requires --host-change-authorized")
            if not shutil.which("pveum"):
                raise RuntimeError("Proxmox is not installed on this host")
        ROOT.mkdir(mode=0o700, exist_ok=True)
        state.mkdir(mode=0o700, exist_ok=True)
        if not resume:
            if SERVICE.exists():
                raise RuntimeError("An onboarding service already exists; inspect it before replacing it")
            saved = state / "host-setup.py"
            write(saved, Path(__file__).read_text(), 0o700)
            write(SERVICE, f'''[Unit]
Description=Proxmox agent lab onboarding
Wants=network-online.target
After=network-online.target pve-cluster.service pvedaemon.service pveproxy.service
ConditionPathExists=!{state}/complete

[Service]
Type=oneshot
Environment=DEBIAN_FRONTEND=noninteractive
ExecStart=/usr/bin/python3 {saved} --resume
TimeoutStartSec=7200
UMask=0077

[Install]
WantedBy=multi-user.target
''', 0o644)
            run("systemctl", "daemon-reload")
            run("systemctl", "enable", SERVICE.name)
            # Return promptly from the Proxmox first-boot hook. The actual
            # provisioning runs under a resumable systemd unit after services.
            run("systemctl", "start", "--no-block", SERVICE.name)
            print("Onboarding service installed; check systemctl status proxmox-agent-lab-onboarding")
            return
        if settings["mode"] == "vps":
            stage = (state / "stage").read_text() if (state / "stage").exists() else "new"
            if stage == "new":
                install_kernel(settings, state)
                return
            if stage == "kernel-installed":
                install_proxmox(state)
        # "existing" skips the Debian→Proxmox install entirely and goes
        # straight to provisioning the API principal, bridge and pairing.
        payload_path = state / "enrollment.json"
        payload = json.loads(payload_path.read_text()) if payload_path.exists() else provision(settings, state)
        callback(settings, payload)
        write(state / "complete", "enrollment delivered; controller must verify the API")
        # No pairing secret or API token remains in the resumable state after success.
        for name in ("host-setup.py", "api-token.json", "enrollment.json"):
            (state / name).unlink(missing_ok=True)
        run("systemctl", "disable", SERVICE.name)
        print("Enrollment delivered to the paired controller")
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
        # Do not dump command output, settings or credential-bearing exceptions.
        print(f"Onboarding failed: {str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
