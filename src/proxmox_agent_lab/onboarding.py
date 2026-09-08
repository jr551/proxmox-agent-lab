"""Generate private installer bundles and receive authenticated host enrollment."""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import shutil
import ssl
import subprocess
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import parse, request

from . import onboarding_host


def private_write(path: Path, text: str, mode: int = 0o600) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".pxl-", dir=path.parent)
    try:
        os.chmod(temporary, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)  # Atomic publication, refusing existing paths.
    finally:
        os.unlink(temporary)



def hostname(value: str) -> str:
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        if len(value) > 253 or not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", p) for p in value.split(".")):
            raise ValueError("Use a plain IP address or DNS hostname") from None
        return value.lower()


def authority(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def read_bundle(directory: Path, *, require_live: bool = True) -> dict:
    if os.name == "posix" and directory.stat().st_mode & 0o077:
        raise ValueError("Pairing directory must be private (chmod 700)")
    settings = json.loads((directory / "pairing.json").read_text())
    if require_live and time.time() >= settings["expires_at"]:
        raise ValueError("Pairing bundle expired; generate a new bundle")
    return settings


def answer_file(settings: dict, password_hash: str, serial: str, country: str, timezone: str, keyboard: str, email: str) -> str:
    if not serial or any(c in serial for c in '*?[]\r\n'):
        raise ValueError("An exact disk ID_SERIAL is required; wildcard disk selection is refused")
    if not re.fullmatch(r"\$(?:6|y)\$[^\s]+", password_hash):
        raise ValueError("Use a SHA-512 crypt or yescrypt root password hash file")
    values = {"keyboard": keyboard, "country": country, "fqdn": settings["fqdn"],
              "mailto": email, "timezone": timezone, "root-password-hashed": password_hash}
    return "[global]\n" + "".join(f"{k} = {json.dumps(v)}\n" for k, v in values.items()) + '''
[network]
source = "from-dhcp"

[disk-setup]
filesystem = "ext4"
filter-match = "all"
''' + f"filter.ID_SERIAL = {json.dumps(serial)}\n" + '''
[first-boot]
source = "from-iso"
ordering = "fully-up"
'''


def prepare(args) -> dict:
    controller = hostname(args.controller_host)
    fqdn = hostname(args.fqdn)
    if "." not in fqdn or ":" in fqdn:
        raise ValueError("Use a fully qualified DNS name for the Proxmox host")
    try:
        ipaddress.ip_address(fqdn)
    except ValueError:
        pass
    else:
        raise ValueError("Proxmox fqdn must be a DNS name, not an IP address")
    if not 1 <= args.port <= 65535 or not 1 <= args.hours <= 168:
        raise ValueError("Port must be 1–65535 and pairing lifetime 1–168 hours")
    settings = {"id": secrets.token_hex(12), "token": secrets.token_urlsafe(32),
                "expires_at": int(time.time() + args.hours * 3600), "mode": args.mode,
                "fqdn": fqdn, "api_host": hostname(args.api_host) if args.api_host else "",
                "callback_url": f"https://{authority(controller, args.port)}/enroll", "port": args.port}
    if args.mode == "vps" and not settings["api_host"]:
        raise ValueError("VPS enrollment requires --api-host (address reachable from the controller)")
    wifi = [args.wifi_ssid, args.wifi_password_file, args.wifi_interface]
    if any(wifi):
        if not all(wifi) or args.mode != "iso":
            raise ValueError("Wi-Fi needs SSID, password file and interface, and is supported only for ISO first boot")
        password = Path(args.wifi_password_file).read_text().rstrip("\r\n")
        if not 8 <= len(password.encode()) <= 63 or not 1 <= len(args.wifi_ssid.encode()) <= 32:
            raise ValueError("Use a WPA2 passphrase of 8–63 bytes and an SSID of 1–32 bytes")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,15}", args.wifi_interface):
            raise ValueError("Invalid Wi-Fi interface name")
        settings["wifi"] = {"ssid_hex": args.wifi_ssid.encode().hex(), "interface": args.wifi_interface,
                            "psk": hashlib.pbkdf2_hmac("sha1", password.encode(), args.wifi_ssid.encode(), 4096, 32).hex()}
    if args.ssh_public_key_file:
        key = Path(args.ssh_public_key_file).read_text().strip()
        if not re.fullmatch(r"(?:ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256) [A-Za-z0-9+/=]+(?: [^\r\n]*)?", key):
            raise ValueError("Expected one SSH public key, without authorized_keys options")
        settings["ssh_public_key"] = key
    answer = None
    if args.mode == "iso":
        if not args.disk_serial or not args.root_password_hash_file or not args.wipe_confirmed:
            raise ValueError("ISO setup requires --disk-serial, --root-password-hash-file and --wipe-confirmed")
        answer = answer_file(settings, Path(args.root_password_hash_file).read_text().strip(),
                             args.disk_serial, args.country, args.timezone, args.keyboard, args.email)
    elif any((args.disk_serial, args.root_password_hash_file, args.wipe_confirmed)):
        raise ValueError("VPS setup does not partition disks; omit ISO disk/password options")
    directory = Path(args.directory).expanduser().resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    try:
        san = f"IP:{controller}" if _is_ip(controller) else f"DNS:{controller}"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:3072", "-nodes", "-days", "8",
                        "-subj", "/CN=proxmox-agent-lab-pairing", "-addext", f"subjectAltName={san}",
                        "-keyout", str(directory / "receiver.key"), "-out", str(directory / "receiver.crt")],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        (directory / "receiver.key").chmod(0o600)
        settings["controller_ca"] = (directory / "receiver.crt").read_text()
        private_write(directory / "pairing.json", json.dumps(settings))
        source = Path(onboarding_host.__file__).read_text()
        script = "#!/usr/bin/python3\n" + source + "\nif __name__ == '__main__':\n    entry(json.loads(" + repr(json.dumps(settings)) + "))\n"
        private_write(directory / "host-setup.py", script, 0o700)
        if answer:
            private_write(directory / "answer.toml", answer)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"Could not generate pairing bundle ({type(exc).__name__}); remove the incomplete directory before retrying") from None
    return {"bundle": str(directory), "mode": args.mode, "expires_at": settings["expires_at"],
            "next": "onboard build-iso" if answer else "copy host-setup.py to the VPS and run with --host-change-authorized --reboot-authorized"}


def _is_ip(value):
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def build_iso(args) -> dict:
    directory = Path(args.bundle).expanduser().resolve()
    settings = read_bundle(directory)
    if settings["mode"] != "iso":
        raise ValueError("This bundle is for VPS setup, not an ISO")
    if not re.fullmatch(r"[a-fA-F0-9]{64}", args.sha256):
        raise ValueError("Supply the official ISO SHA-256 checksum")
    output = directory / "proxmox-agent-lab.iso"
    if output.exists():
        raise ValueError("Output ISO already exists; refusing to overwrite")
    assistant = shutil.which("proxmox-auto-install-assistant")
    if not assistant:
        raise ValueError("Install proxmox-auto-install-assistant on a Debian/PVE build machine, then run this command there")
    if getattr(args, "url", None):
        parsed = parse.urlsplit(args.url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Use a public HTTPS ISO URL without credentials or query parameters")
        source = directory / "source.iso"
        if source.exists():
            raise ValueError("Downloaded source.iso already exists; use --source to reuse it")
        deadline = time.monotonic() + 1800
        downloaded = 0
        try:
            opener = request.build_opener(onboarding_host.NoRedirect())
            with opener.open(args.url, timeout=30) as response, source.open("xb") as target:
                while chunk := response.read(1024 * 1024):
                    downloaded += len(chunk)
                    if downloaded > 8 * 1024**3 or time.monotonic() > deadline:
                        raise ValueError("ISO download exceeded its size/time budget")
                    target.write(chunk)
        except (OSError, ValueError):
            source.unlink(missing_ok=True)
            raise ValueError("ISO download failed; check the public URL and try again") from None
    else:
        source = Path(args.source).expanduser().resolve()
    with source.open("rb") as handle:
        actual = hashlib.file_digest(handle, "sha256").hexdigest()
    if not hmac.compare_digest(actual, args.sha256.lower()):
        if getattr(args, "url", None):
            source.unlink()
        raise ValueError("Source ISO checksum mismatch")
    try:
        subprocess.run([assistant, "validate-answer", str(directory / "answer.toml")], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        # The private directory protects the output while the external builder writes it.
        subprocess.run([assistant, "prepare-iso", str(source), "--fetch-from", "iso", "--answer-file",
                        str(directory / "answer.toml"), "--on-first-boot", str(directory / "host-setup.py"),
                        "--output", str(output)], check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=1800)
        output.chmod(0o600)
    except (OSError, subprocess.SubprocessError):
        output.unlink(missing_ok=True)
        raise ValueError("ISO preparation failed; check assistant compatibility and answer-file settings") from None
    with output.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    return {"iso": str(output), "sha256": digest, "contains_credentials": True}


def validate_enrollment(payload: dict, settings: dict) -> dict:
    if not isinstance(payload, dict) or payload.get("id") != settings["id"] or payload.get("mode") != settings["mode"]:
        raise ValueError("Enrollment does not match this bundle")
    host = hostname(payload.get("host", ""))
    if settings["api_host"] and host != settings["api_host"]:
        raise ValueError("Unexpected API host")
    node = payload.get("node", "")
    if node != settings["fqdn"].split(".")[0]:
        raise ValueError("Unexpected Proxmox node")
    token_user = f"pxl-{settings['id']}@pve"
    if payload.get("token_user") != token_user or payload.get("token_name") != "controller":
        raise ValueError("Unexpected API principal")
    if not re.fullmatch(r"[a-fA-F0-9-]{36}", payload.get("token_secret", "")):
        raise ValueError("Invalid API token")
    ssl.PEM_cert_to_DER_cert(payload.get("ca", ""))
    mac = payload.get("wol_mac", "")
    if mac and not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", mac):
        raise ValueError("Invalid wake-on-LAN address")
    return {key: payload[key] for key in ("id", "mode", "host", "node", "token_user", "token_name", "token_secret", "ca", "wol_mac")}


def receiver(directory: Path, bind: str):
    settings = read_bundle(directory)
    receipt = directory / "enrollment.json"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Headers and payloads contain credentials.

        def do_POST(self):
            if self.path != "/enroll":
                self.send_error(404)
                return
            expected = "Bearer " + settings["token"]
            if time.time() >= settings["expires_at"] or not hmac.compare_digest(self.headers.get("Authorization", ""), expected):
                self.send_error(403)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 16384 or self.headers.get("Transfer-Encoding"):
                    raise ValueError("Invalid body length")
                payload = validate_enrollment(json.loads(self.rfile.read(size)), settings)
                # Serialize acceptance: a token can enroll exactly one host;
                # identical retries are acknowledged after a lost response.
                with self.server.enrollment_lock:
                    if receipt.exists():
                        if json.loads(receipt.read_text()) != payload:
                            self.send_error(409)
                            return
                    else:
                        private_write(receipt, json.dumps(payload))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()
                self.server.enrolled = True
            except (ValueError, TypeError, KeyError, OSError):
                self.send_error(400)

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        address_family = socket.AF_INET6 if ":" in bind else socket.AF_INET

        def get_request(self):
            raw, addr = self.socket.accept()
            raw.settimeout(5)
            try:
                return self.context.wrap_socket(raw, server_side=True), addr
            except BaseException:
                raw.close()
                raise

    import threading
    server = Server((bind, settings["port"]), Handler)
    server.enrollment_lock = threading.Lock()
    server.enrolled = False
    server.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.context.load_cert_chain(directory / "receiver.crt", directory / "receiver.key")
    server.timeout = 0.5
    return server


def accept(directory: Path, output: Path) -> dict:
    settings = read_bundle(directory, require_live=False)
    payload = validate_enrollment(json.loads((directory / "enrollment.json").read_text()), settings)
    output = output.expanduser().resolve()
    ca_path = output.with_suffix(".ca.pem")
    secret_path = output.with_suffix(".secrets.toml")
    if any(p.exists() for p in (output, ca_path, secret_path)):
        raise ValueError("Config or companion files already exist; choose a new --config-out")
    # Verify certificate, hostname, credentials and node before publishing config.
    context = ssl.create_default_context(cadata=payload["ca"])
    req = request.Request(f"https://{authority(payload['host'], 8006)}/api2/json/nodes/{payload['node']}/status",
                          headers={"Authorization": f"PVEAPIToken={payload['token_user']}!controller={payload['token_secret']}"})
    try:
        opener = request.build_opener(request.ProxyHandler({}), request.HTTPSHandler(context=context), onboarding_host.NoRedirect())
        with opener.open(req, timeout=15) as response:
            status = json.load(response).get("data")
        if not isinstance(status, dict) or "uptime" not in status:
            raise ValueError("Invalid node status")
    except (OSError, ValueError):
        raise ValueError("Enrollment received, but authenticated API verification failed; check routing, port 8006 and host certificate, then retry onboard accept") from None
    q = json.dumps
    mode = "lxc-only" if settings["mode"] == "vps" else "all"
    wol = payload["wol_mac"] if mode == "all" else ""
    config_text = f'''[proxmox]
host = {q(payload['host'])}
node = {q(payload['node'])}
token_user = {q(payload['token_user'])}
token_name = "controller"
verify_tls = true
ca_file = {q(str(ca_path))}
guest_mode = {q(mode)}

[power]
mode = {q('wake-on-lan' if wol else 'none')}
mac = {q(wol)}

[secrets]
backend = "file"
file_path = {q(str(secret_path))}
'''
    output.parent.mkdir(parents=True, exist_ok=True)
    created = []
    try:
        for path, text in [(ca_path, payload["ca"]), (secret_path, 'proxmox-token = ' + q(payload["token_secret"]) + '\n'), (output, config_text)]:
            private_write(path, text)
            created.append(path)
    except OSError:
        for path in created:
            path.unlink()
        raise ValueError("Could not write controller configuration") from None
    return {"config": str(output), "api_verified": True, "guest_mode": mode,
            "next": "set PROXMOX_AGENT_LAB_CONFIG to this path and run doctor"}


def verify_beacon(packet: bytes, settings: dict) -> dict:
    envelope = json.loads(packet)
    payload = envelope["payload"]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    expected = hmac.new(settings["token"].encode(), encoded, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, envelope["signature"]):
        raise ValueError("Invalid discovery signature")
    if (payload.get("id") != settings["id"]
            or payload.get("node") != settings["fqdn"].split(".")[0]
            or abs(time.time() - payload["time"]) > 60):
        raise ValueError("Wrong or stale discovery beacon")
    return {"host": hostname(payload["host"]), "node": payload["node"], "paired": False}


def discover(directory: Path, timeout: int) -> dict:
    if not 1 <= timeout <= 3600:
        raise ValueError("Discovery timeout must be 1–3600 seconds")
    settings = read_bundle(directory)
    deadline = min(time.time() + timeout, settings["expires_at"])
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("", 8844))
        while time.time() < deadline:
            sock.settimeout(min(1, max(0.01, deadline - time.time())))
            try:
                packet, _ = sock.recvfrom(4096)
                return verify_beacon(packet, settings)
            except (ValueError, TypeError, KeyError, OSError):
                continue
    raise ValueError("No matching host announced itself; check the HTTPS receiver or LAN broadcast reachability")


def command(lab, args):
    try:
        if args.onboard_action == "prepare":
            result = prepare(args)
        elif args.onboard_action == "build-iso":
            result = build_iso(args)
        elif args.onboard_action == "accept":
            result = accept(Path(args.bundle).expanduser(), Path(args.config_out))
        elif args.onboard_action == "discover":
            result = discover(Path(args.bundle).expanduser(), args.timeout)
        else:
            if not 1 <= args.timeout <= 86400:
                raise ValueError("Receiver timeout must be 1–86400 seconds")
            directory = Path(args.bundle).expanduser()
            if (directory / "enrollment.json").exists():
                print(json.dumps(accept(directory, Path(args.config_out)), indent=2))
                return
            server = receiver(directory, args.bind)
            print(json.dumps({"listening": server.server_address, "pairing": "waiting"}), flush=True)
            deadline = time.monotonic() + args.timeout
            try:
                while not server.enrolled and time.monotonic() < deadline:
                    server.handle_request()
                if not server.enrolled:
                    raise ValueError("Timed out waiting for host enrollment; rerun serve to continue")
            finally:
                server.server_close()
            result = accept(directory, Path(args.config_out))
        print(json.dumps(result, indent=2))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise lab.LabError(str(exc)) from None


def register(sub, lab):
    root = sub.add_parser("onboard", help="Generate an installer and pair a new lab host (experimental)")
    actions = root.add_subparsers(dest="onboard_action", required=True)
    p = actions.add_parser("prepare")
    p.add_argument("--mode", choices=("iso", "vps"), required=True)
    p.add_argument("--directory", required=True)
    p.add_argument("--controller-host", required=True)
    p.add_argument("--port", type=int, default=8843)
    p.add_argument("--hours", type=int, default=24)
    p.add_argument("--fqdn", required=True)
    p.add_argument("--api-host")
    p.add_argument("--disk-serial")
    p.add_argument("--root-password-hash-file")
    p.add_argument("--wipe-confirmed", action="store_true")
    p.add_argument("--country", default="gb")
    p.add_argument("--timezone", default="Europe/London")
    p.add_argument("--keyboard", default="en-gb")
    p.add_argument("--email", default="root@example.invalid")
    p.add_argument("--wifi-ssid")
    p.add_argument("--wifi-password-file")
    p.add_argument("--wifi-interface")
    p.add_argument("--ssh-public-key-file")
    p = actions.add_parser("build-iso")
    p.add_argument("--bundle", required=True)
    source_group = p.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--source")
    source_group.add_argument("--url", help="Download a public HTTPS source ISO")
    p.add_argument("--sha256", required=True)
    for name in ("serve", "accept"):
        p = actions.add_parser(name)
        p.add_argument("--bundle", required=True)
        p.add_argument("--config-out", required=True)
        if name == "serve":
            p.add_argument("--bind", default="0.0.0.0")
            p.add_argument("--timeout", type=int, default=3600)
    p = actions.add_parser("discover")
    p.add_argument("--bundle", required=True)
    p.add_argument("--timeout", type=int, default=60)
    for p in actions.choices.values():
        p.set_defaults(func=lab._bind(lab, command))
