"""Network capture, SSL inspection and MITM for lab guests.

Three related jobs, all aimed at *seeing and shaping what a guest puts on the
wire* -- driver work, protocol reverse-engineering, watching an installer phone
home, or forcing an app down a path it would not normally take.

1. Passive capture (`capture`).
   A Proxmox QEMU guest's NIC appears on the host as a tap interface
   (``tap<vmid>i0``). The host can therefore ``tcpdump`` that interface and hand
   back a standard pcap -- no agent, nothing installed in the guest, and it sees
   every frame the guest sends or receives. This is the network analogue of
   ``usb sniff`` and, like it, runs on the hypervisor over the shared
   ``[memflow]`` SSH channel.

2. SSL inspection + MITM relay (`mitm-setup`, `ca`, `intercept`).
   Passive capture of TLS is just ciphertext. To read it you terminate TLS in
   the middle, which means a proxy the guest trusts. That proxy runs in a
   **disposable LXC** -- the very same container pattern the Ghidra analysis box
   uses -- built by ``mitm-setup``, registered to the lease, and destroyed with
   it. ``ca`` hands you the proxy's CA certificate plus a ready-to-paste install
   helper for Windows, Linux, macOS or Android, so the guest will trust the
   interception. ``intercept`` then runs mitmproxy for a bounded window,
   decrypts the HTTPS flows, and -- because a relay that can read can also
   rewrite -- optionally *changes* requests and responses on the way through
   (swap a header, patch a response body, remap a host).

Trust boundary and safety
--------------------------
Everything host-resident here reaches the host over the same opt-in SSH channel
memflow uses (the ``[memflow]`` connection), the one exception to the API-token
architecture, so it is off until that is configured. Capture reads a guest's
traffic and interception decrypts it: both are gated behind an active lease and
audited by the *fact* of the capture only -- packet contents and decrypted
flows go to a local file you name, never to the ledger.
"""

from __future__ import annotations

import base64
import json
import os
import re
from typing import Any

from . import memflow as _mf


# --------------------------------------------------------------------------- #
# Host channel (shared with memflow/usb: same host, same trust boundary).
# --------------------------------------------------------------------------- #

NOT_ENABLED = (
    "Network capture runs on the Proxmox host over SSH -- the same host "
    "connection memflow and usb use -- so it is off until you set "
    "[memflow] enabled = true and ssh_host. See docs/netcap.md."
)


def _require_enabled(lab: Any) -> None:
    _mf.require_host_ssh(lab, NOT_ENABLED)


_ssh = _mf.host_run


def _ssh_ok(lab: Any, argv: list[str], *, timeout: int = 60):
    proc = _ssh(lab, argv, timeout=timeout)
    if proc.returncode not in (0, None):
        raise lab.LabError(
            f"host command failed: {(proc.stderr or proc.stdout or '').strip()[:300]}"
        )
    return proc


def _pct(lab: Any, lxc: int, remote_argv: list[str], *, timeout: int = 120):
    """Run a command inside the LXC via `pct exec`, args safely quoted."""
    return _ssh(lab, ["pct", "exec", str(lxc), "--", *remote_argv], timeout=timeout)


def _running_qemu(lab: Any, api: Any, vmid: int) -> None:
    status = api.call("GET", f"/nodes/{lab.NODE}/qemu/{vmid}/status/current")
    if status.get("status") != "running":
        raise lab.LabError(
            f"VMID {vmid} is not running; there is no tap interface to capture "
            "on until the guest is powered on"
        )


def _resolve_iface(lab: Any, vmid: int, nic: str, override: str | None) -> str:
    """The host interface carrying a guest's NIC.

    A running QEMU guest's ``netN`` shows up on the host as ``tap<vmid>i<N>``;
    an LXC's as ``veth<vmid>i<N>``. Resolve by probing what actually exists
    rather than assuming, so a stopped guest or a wrong NIC fails with a clear
    message instead of an empty capture.
    """
    if override:
        # The interface name reaches a root shell command on the host; only
        # accept plain interface names (audit 2026-08-24).
        if not re.fullmatch(r"[A-Za-z0-9.@:_-]{1,15}", override):
            raise lab.LabError(
                "--iface must be a plain interface name (letters, digits, "
                "dot, colon, dash, underscore)"
            )
        candidates = [override]
    else:
        m = re.fullmatch(r"net(\d+)", nic)
        if not m:
            raise lab.LabError("--nic must be netN (e.g. net0)")
        idx = m.group(1)
        candidates = [f"tap{vmid}i{idx}", f"veth{vmid}i{idx}"]
    present = _ssh(lab, ["ip", "-o", "link", "show"], timeout=30).stdout
    names = set(re.findall(r"^\d+:\s+([^:@]+)", present, re.M))
    for cand in candidates:
        if cand in names:
            return cand
    raise lab.LabError(
        f"no host interface for VMID {vmid} {nic} (tried {', '.join(candidates)}). "
        "Is the guest running, and is that the right NIC? "
        "Pass --iface to name it explicitly."
    )


# --------------------------------------------------------------------------- #
# 1. Passive capture.
# --------------------------------------------------------------------------- #

def cmd_capture(lab: Any, args: Any) -> None:
    """Capture a guest's network traffic to a local pcap via the host tap.

    Fully passive: taps the guest's interface on the hypervisor, so it needs
    nothing installed in the guest and sees traffic regardless of the guest OS.
    A BPF ``--filter`` narrows what is written; ``--count`` and ``--seconds``
    bound it. TLS is captured as ciphertext -- use ``intercept`` to read it.
    """
    _require_enabled(lab)
    api = lab.ProxmoxAPI()
    lab.load_lease(args.lease)
    _running_qemu(lab, api, args.vmid)
    iface = _resolve_iface(lab, args.vmid, args.nic, args.iface)
    remote_pcap = f"/tmp/pxl-net-{args.vmid}-{iface}.pcap"
    limit = f"-c {int(args.count)}" if args.count else ""
    # A BPF expression is kept as one shell word so it reaches tcpdump intact.
    # It is user-supplied and runs under root on the host, so it is POSIX
    # single-quote escaped rather than interpolated raw (audit 2026-08-24).
    bpf = args.filter or ""
    bpf_sh = "'" + bpf.replace("'", "'\\''") + "'"
    script = (
        f"timeout {args.seconds} tcpdump -i {iface} -w {remote_pcap} {limit} "
        f"-U {bpf_sh} >/dev/null 2>&1 || true; "
        f"pkts=$(tcpdump -r {remote_pcap} 2>/dev/null | wc -l); "
        f"echo \"PKTS=$pkts\"; base64 {remote_pcap}; rm -f {remote_pcap}"
    )
    proc = _ssh(lab, ["bash", "-c", script], timeout=args.seconds + 60)
    if proc.returncode not in (0, None):
        raise lab.LabError(f"capture failed on the host: {(proc.stderr or '').strip()[:300]}")
    pkts, data = _mf.decode_capture_output(proc.stdout or "")
    out = os.path.expanduser(args.out)
    with open(out, "wb") as fh:
        fh.write(data)
    lab.audit("netcap-capture", lease=args.lease, vmid=args.vmid, iface=iface,
              seconds=args.seconds, packets=pkts, sync=False)
    print(json.dumps(
        {"vmid": args.vmid, "iface": iface, "packets": pkts, "out": out,
         "note": "open in Wireshark; TLS is ciphertext -- see 'netcap intercept'"},
        indent=2, sort_keys=True,
    ))


# --------------------------------------------------------------------------- #
# 2. SSL inspection + MITM relay, in a disposable LXC (the Ghidra pattern).
# --------------------------------------------------------------------------- #

def cmd_mitm_setup(lab: Any, args: Any) -> None:
    """Prepare a disposable LXC running mitmproxy, and generate its CA.

    Creates the container if absent, installs the standalone mitmproxy build,
    generates the interception CA, and registers the LXC to the lease so it is
    destroyed on lease-end. Idempotent: re-running against a ready box is a
    no-op that just re-reports its address.
    """
    _require_enabled(lab)
    api = lab.ProxmoxAPI()
    lab.load_lease(args.lease)
    script = (MITM_SETUP_SCRIPT
              .replace("__LXC__", str(args.lxc))
              .replace("__BRIDGE__", args.bridge))
    proc = _mf._ssh(lab, ["bash", "-s"], timeout=args.timeout, stdin=script)
    if proc.stdout:
        print(proc.stdout, end="" if proc.stdout.endswith("\n") else "\n")
    if proc.returncode not in (0, None):
        raise lab.LabError(
            "mitm-setup did not complete: "
            + (proc.stderr or proc.stdout).strip()[-600:]
        )
    try:
        lab.register_resource(lab.load_lease(args.lease), "lxc", args.lxc,
                              "delete", "mitm-lab")
    except Exception:  # pragma: no cover - best-effort registration
        pass
    ip = _pct(lab, args.lxc,
              ["bash", "-c", "hostname -I 2>/dev/null | awk '{print $1}'"],
              timeout=30).stdout.strip()
    lab.audit("netcap-mitm-setup", lease=args.lease, lxc=args.lxc, sync=False)
    print(json.dumps(
        {"lxc": args.lxc, "prepared": True, "proxy_ip": ip or None,
         "proxy_port": args.port,
         "next": [
             f"install the CA in the guest: proxmox-lab netcap ca --lease "
             f"{args.lease} --lxc {args.lxc} --os <windows|linux|macos|android>",
             f"point the guest's proxy at {ip or '<proxy_ip>'}:{args.port}, then "
             f"proxmox-lab netcap intercept --lease {args.lease} --lxc {args.lxc}",
         ]},
        indent=2, sort_keys=True,
    ))


# The install helper for each guest OS. These are the "helpers for common OS":
# the exact command that makes a guest trust the interception CA. {pem}/{cer}
# are filled with the filename this tool wrote locally.
_CA_HELPERS = {
    "linux": (
        "# Debian/Ubuntu/RHEL: trust the CA system-wide\n"
        "sudo cp {pem} /usr/local/share/ca-certificates/mitmproxy.crt\n"
        "sudo update-ca-certificates    # RHEL: cp to /etc/pki/ca-trust/source/anchors && update-ca-trust"
    ),
    "windows": (
        "REM Run as Administrator (cmd):\n"
        "certutil -addstore -f Root {cer}\n"
        "REM  or PowerShell:  Import-Certificate -FilePath {cer} "
        "-CertStoreLocation Cert:\\LocalMachine\\Root"
    ),
    "macos": (
        "# Adds to the System keychain as a trusted root (needs admin):\n"
        "sudo security add-trusted-cert -d -r trustRoot "
        "-k /Library/Keychains/System.keychain {pem}"
    ),
    "android": (
        "# User CA (Settings path), works for apps that trust user CAs:\n"
        "#   Settings > Security > Encryption & credentials > Install a certificate > CA\n"
        "# Rooted / emulator system store (survives, trusted by all apps):\n"
        "HASH=$(openssl x509 -inform PEM -subject_hash_old -in {pem} | head -1)\n"
        "adb root && adb remount\n"
        "adb push {pem} /system/etc/security/cacerts/$HASH.0\n"
        "adb shell chmod 644 /system/etc/security/cacerts/$HASH.0 && adb reboot"
    ),
}


def cmd_ca(lab: Any, args: Any) -> None:
    """Fetch the interception CA and print a helper to trust it on the guest OS.

    Without the CA installed, the guest rejects the intercepted TLS and you see
    nothing but errors. This writes the CA locally (PEM, plus DER/.cer for
    Windows) and prints the exact command to trust it on the chosen OS.
    """
    _require_enabled(lab)
    lab.load_lease(args.lease)
    conf = "/root/.mitmproxy"
    pem = _pct(lab, args.lxc,
               ["cat", f"{conf}/mitmproxy-ca-cert.pem"], timeout=30)
    if pem.returncode not in (0, None) or "BEGIN CERTIFICATE" not in pem.stdout:
        raise lab.LabError(
            f"no CA in LXC {args.lxc}; run 'proxmox-lab netcap mitm-setup' first"
        )
    out_pem = os.path.expanduser(args.out)
    with open(out_pem, "w") as fh:
        fh.write(pem.stdout)
    written = {"pem": out_pem}
    # mitmproxy also emits the CA as a .cer (PEM content, .cer extension) --
    # the filename certutil expects on Windows.
    cer = _pct(lab, args.lxc,
               ["base64", f"{conf}/mitmproxy-ca-cert.cer"], timeout=30)
    if cer.returncode in (0, None) and cer.stdout.strip():
        out_cer = os.path.splitext(out_pem)[0] + ".cer"
        with open(out_cer, "wb") as fh:
            fh.write(base64.b64decode(cer.stdout))
        written["cer"] = out_cer
    helper = _CA_HELPERS[args.os].format(
        pem=written["pem"], cer=written.get("cer", written["pem"]))
    lab.audit("netcap-ca", lease=args.lease, lxc=args.lxc, os=args.os, sync=False)
    print(json.dumps(
        {"lxc": args.lxc, "os": args.os, "written": written,
         "install_helper": helper},
        indent=2, sort_keys=True,
    ))


def _modify_args(lab: Any, args: Any) -> list[str]:
    """Translate the friendly change flags into mitmdump options.

    A relay that terminates TLS can rewrite as well as read, so these turn the
    proxy from an observer into an active MITM. Kept to the few rewrites that
    are easy to reason about and to verify.
    """
    extra: list[str] = []
    for spec in args.set_header or []:
        if ":" not in spec:
            raise lab.LabError(f"--set-header must be 'Name: value', not {spec!r}")
        name, value = (s.strip() for s in spec.split(":", 1))
        extra += ["--modify-headers", f"/~q/{name}/{value}"]
    for spec in args.set_response_header or []:
        if ":" not in spec:
            raise lab.LabError(
                f"--set-response-header must be 'Name: value', not {spec!r}")
        name, value = (s.strip() for s in spec.split(":", 1))
        extra += ["--modify-headers", f"/~s/{name}/{value}"]
    for spec in args.replace or []:
        if "/" not in spec:
            raise lab.LabError(
                f"--replace must be 'REGEX/REPLACEMENT', not {spec!r}")
        pat, repl = spec.split("/", 1)
        extra += ["--modify-body", f"/~s/{pat}/{repl}"]
    for spec in args.map_remote or []:
        extra += ["--map-remote", spec]
    return extra


def cmd_intercept(lab: Any, args: Any) -> None:
    """Run the MITM proxy for a bounded window and return the decrypted flows.

    Starts mitmproxy in the LXC, captures for ``--seconds``, exports the flows
    as HAR (parsed here into a compact JSON summary) and as a raw mitmproxy dump
    you can reopen. Any ``--set-header`` / ``--replace`` / ``--map-remote`` rules
    actively rewrite traffic as it passes. With ``--probe URL`` the container
    drives one request through the proxy itself, so you get an immediate
    end-to-end demonstration without needing a guest wired up yet.
    """
    _require_enabled(lab)
    lab.load_lease(args.lease)
    extra = _modify_args(lab, args)
    # The runner script was pushed into the LXC by mitm-setup; user-supplied
    # rewrite specs are passed as argv (quoted by ssh), never interpolated.
    argv = ["pct", "exec", str(args.lxc), "--", "/root/pxl-mitm.sh",
            str(args.seconds), str(args.port), args.probe or ""]
    argv += extra
    proc = _ssh(lab, argv, timeout=args.seconds + 90)
    if proc.returncode not in (0, None):
        raise lab.LabError(
            "intercept failed in the LXC: "
            + (proc.stderr or proc.stdout or "").strip()[-400:]
            + " (run 'proxmox-lab netcap mitm-setup' if the box is not ready)"
        )
    probe_status = None
    for line in proc.stdout.splitlines():
        if line.startswith("PROBE_STATUS="):
            probe_status = line.split("=", 1)[1].strip() or None
    # Pull the HAR back and summarise it locally with the standard library.
    har_b64 = _pct(lab, args.lxc,
                   ["bash", "-c", "base64 /root/out.har 2>/dev/null || true"],
                   timeout=60).stdout
    flows = _summarise_har(har_b64)
    saved = {}
    if args.out:
        out = os.path.expanduser(args.out)
        raw = _pct(lab, args.lxc,
                   ["bash", "-c", "base64 /root/flows.mitm 2>/dev/null || true"],
                   timeout=120).stdout
        with open(out, "wb") as fh:
            fh.write(base64.b64decode(raw or ""))
        saved["flows"] = out
    if args.har:
        har = os.path.expanduser(args.har)
        with open(har, "wb") as fh:
            fh.write(base64.b64decode(har_b64 or ""))
        saved["har"] = har
    lab.audit("netcap-intercept", lease=args.lease, lxc=args.lxc,
              seconds=args.seconds, flows=len(flows),
              modified=bool(extra), sync=False)
    print(json.dumps(
        {"lxc": args.lxc, "port": args.port, "flow_count": len(flows),
         "modified": bool(extra), "probe_status": probe_status,
         "flows": flows[:args.max_flows], "saved": saved,
         "note": ("point a guest's proxy at this LXC and install the CA "
                  "(netcap ca) to intercept its HTTPS")},
        indent=2, sort_keys=True,
    ))


def _summarise_har(har_b64: str) -> list[dict[str, Any]]:
    """One row per HTTP(S) transaction from a mitmproxy HAR export."""
    if not har_b64.strip():
        return []
    try:
        har = json.loads(base64.b64decode(har_b64).decode("utf-8", "replace"))
        entries = har.get("log", {}).get("entries", [])
    except (ValueError, KeyError):
        return []
    rows = []
    for e in entries:
        req = e.get("request", {})
        resp = e.get("response", {})
        rows.append({
            "method": req.get("method"),
            "url": req.get("url"),
            "status": resp.get("status"),
            "mime": (resp.get("content", {}) or {}).get("mimeType"),
            "resp_bytes": (resp.get("content", {}) or {}).get("size"),
        })
    return rows


def cmd_doctor(lab: Any, args: Any) -> None:
    """Prove the MITM LXC is ready: mitmdump present, CA generated, IP known."""
    _require_enabled(lab)
    lab.load_lease(args.lease)
    checks: dict[str, Any] = {}
    ver = _pct(lab, args.lxc,
               ["bash", "-c", "/opt/mitmproxy/mitmdump --version 2>/dev/null "
                "| head -1 || true"], timeout=40)
    checks["mitmdump"] = (ver.stdout.strip() or None)
    ca = _pct(lab, args.lxc,
              ["bash", "-c", "test -f /root/.mitmproxy/mitmproxy-ca-cert.pem "
               "&& echo yes || echo no"], timeout=30)
    checks["ca_present"] = ca.stdout.strip() == "yes"
    runner = _pct(lab, args.lxc,
                  ["bash", "-c", "test -x /root/pxl-mitm.sh && echo yes || echo no"],
                  timeout=30)
    checks["runner_present"] = runner.stdout.strip() == "yes"
    ip = _pct(lab, args.lxc,
              ["bash", "-c", "hostname -I 2>/dev/null | awk '{print $1}'"],
              timeout=30)
    checks["proxy_ip"] = ip.stdout.strip() or None
    healthy = bool(checks["mitmdump"] and checks["ca_present"]
                   and checks["runner_present"])
    lab.audit("netcap-doctor", lease=args.lease, lxc=args.lxc,
              healthy=healthy, sync=False)
    print(json.dumps({"lxc": args.lxc, "healthy": healthy, "checks": checks},
                     indent=2, sort_keys=True))
    if not healthy:
        raise lab.LabError(
            "the MITM LXC is not ready; run 'proxmox-lab netcap mitm-setup'")


# --------------------------------------------------------------------------- #
# Host-side assets, embedded so the feature is self-contained.
# --------------------------------------------------------------------------- #

MITM_SETUP_SCRIPT = r'''#!/usr/bin/env bash
# Prepare a disposable LXC running mitmproxy for SSL inspection and MITM relay.
# Streamed to the Proxmox host by 'proxmox-lab netcap mitm-setup'. Runs as root.
# Idempotent. Same container pattern as the Ghidra analysis box.
set -euo pipefail
LXC=__LXC__
BRIDGE=__BRIDGE__

if ! pct status "$LXC" >/dev/null 2>&1; then
  TMPL=$(pveam list local 2>/dev/null | awk '/debian-1[23]-standard/{print $1}' | head -1)
  if [ -z "$TMPL" ]; then
    pveam update >/dev/null 2>&1 || true
    NAME=$(pveam available --section system 2>/dev/null | awk '/debian-12-standard/{print $2}' | tail -1)
    pveam download local "$NAME" >/dev/null
    TMPL="local:vztmpl/$NAME"
  fi
  pct create "$LXC" "$TMPL" --hostname mitm-lab --cores 2 --memory 1024 \
    --swap 512 --rootfs local-lvm:8 --net0 name=eth0,bridge="$BRIDGE",ip=dhcp \
    --unprivileged 1 --features nesting=1 --onboot 0 --tags codex-lab >/dev/null
fi
pct start "$LXC" >/dev/null 2>&1 || true
for i in $(seq 1 30); do
  pct exec "$LXC" -- getent hosts downloads.mitmproxy.org >/dev/null 2>&1 && break
  sleep 2
done

if ! pct exec "$LXC" -- test -x /opt/mitmproxy/mitmdump 2>/dev/null; then
  pct exec "$LXC" -- bash -c '
    set -e; export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq || true
    apt-get install -y -qq curl ca-certificates openssl >/dev/null
    VER=$(curl -s https://api.github.com/repos/mitmproxy/mitmproxy/releases/latest \
      | grep -o "\"name\": \"mitmproxy [0-9.]*\"" | grep -o "[0-9][0-9.]*" | head -1)
    [ -n "$VER" ] || VER=12.2.3
    URL="https://downloads.mitmproxy.org/$VER/mitmproxy-$VER-linux-x86_64.tar.gz"
    curl -fsSL "$URL" -o /tmp/mitm.tgz
    mkdir -p /opt/mitmproxy
    tar -xzf /tmp/mitm.tgz -C /opt/mitmproxy
    rm -f /tmp/mitm.tgz
  '
fi

# Generate the interception CA by running mitmdump briefly against a dead port.
pct exec "$LXC" -- bash -c '
  if [ ! -f /root/.mitmproxy/mitmproxy-ca-cert.pem ]; then
    timeout 6 /opt/mitmproxy/mitmdump -q -p 8080 --set confdir=/root/.mitmproxy \
      >/dev/null 2>&1 || true
  fi
  test -f /root/.mitmproxy/mitmproxy-ca-cert.pem
'

# The runner invoked by 'netcap intercept'. Rewrite specs arrive as argv from
# the controller (quoted by ssh), so nothing user-supplied is interpolated into
# this script.
pct exec "$LXC" -- bash -c 'cat > /root/pxl-mitm.sh' <<'RUNNER'
#!/usr/bin/env bash
# args: SECONDS PORT PROBE_URL [extra mitmdump options...]
set -u
SEC="${1:-15}"; PORT="${2:-8080}"; PROBE="${3:-}"; shift 3 2>/dev/null || true
CONF=/root/.mitmproxy
rm -f /root/out.har /root/flows.mitm
timeout "$SEC" /opt/mitmproxy/mitmdump -q --listen-port "$PORT" \
  --set confdir="$CONF" --set hardump=/root/out.har \
  -w /root/flows.mitm "$@" >/root/mitm.log 2>&1 &
MPID=$!
sleep 2
if [ -n "$PROBE" ]; then
  CODE=$(https_proxy="http://127.0.0.1:$PORT" http_proxy="http://127.0.0.1:$PORT" \
    curl -s -o /dev/null -w '%{http_code}' --max-time 20 \
    --cacert "$CONF/mitmproxy-ca-cert.pem" "$PROBE" 2>/dev/null || echo 000)
  echo "PROBE_STATUS=$CODE"
fi
wait "$MPID" 2>/dev/null || true
exit 0
RUNNER
pct exec "$LXC" -- chmod 0755 /root/pxl-mitm.sh
echo "mitm-lxc-ready $LXC"
'''


# --------------------------------------------------------------------------- #
# Registration.
# --------------------------------------------------------------------------- #

def register(sub: Any, lab: Any) -> None:
    from .cli import _bind


    net = sub.add_parser(
        "netcap", help="network capture, SSL inspection and MITM relay")
    net_sub = net.add_subparsers(dest="netcap_command", required=True)

    cap = net_sub.add_parser(
        "capture", help="capture a guest's traffic to a local pcap (passive)")
    cap.add_argument("--lease", required=True)
    cap.add_argument("--vmid", type=int, required=True)
    cap.add_argument("--nic", default="net0", help="which guest NIC (netN)")
    cap.add_argument("--iface", help="host interface to capture on (overrides --nic)")
    cap.add_argument("--seconds", type=int, default=15, help="capture duration")
    cap.add_argument("--count", type=int, default=0,
                     help="stop after N packets (0 = until the duration)")
    cap.add_argument("--filter", help="BPF filter, e.g. 'tcp port 443'")
    cap.add_argument("--out", required=True, help="local pcap output file")
    cap.set_defaults(func=_bind(lab, cmd_capture))

    setup = net_sub.add_parser(
        "mitm-setup", help="prepare a disposable LXC running mitmproxy")
    setup.add_argument("--lease", required=True)
    setup.add_argument("--lxc", type=int, required=True,
                       help="VMID for the proxy container")
    setup.add_argument("--bridge", default="vmbr0",
                       help="bridge to attach the proxy LXC to")
    setup.add_argument("--port", type=int, default=8080,
                       help="proxy port the guest should use")
    setup.add_argument("--timeout", type=int, default=1800)
    setup.set_defaults(func=_bind(lab, cmd_mitm_setup))

    ca = net_sub.add_parser(
        "ca", help="fetch the interception CA and print an OS install helper")
    ca.add_argument("--lease", required=True)
    ca.add_argument("--lxc", type=int, required=True)
    ca.add_argument("--os", required=True,
                    choices=("windows", "linux", "macos", "android"),
                    help="the guest OS to print an install helper for")
    ca.add_argument("--out", default="mitmproxy-ca.pem",
                    help="local path for the CA (a .cer is written alongside)")
    ca.set_defaults(func=_bind(lab, cmd_ca))

    icept = net_sub.add_parser(
        "intercept", help="run the MITM proxy, decrypt flows, optionally rewrite")
    icept.add_argument("--lease", required=True)
    icept.add_argument("--lxc", type=int, required=True)
    icept.add_argument("--seconds", type=int, default=20, help="capture window")
    icept.add_argument("--port", type=int, default=8080)
    icept.add_argument("--probe", help="drive one request through the proxy from "
                                       "the LXC itself (end-to-end self-test)")
    icept.add_argument("--set-header", dest="set_header", action="append",
                       help="rewrite a request header, 'Name: value' (repeatable)")
    icept.add_argument("--set-response-header", dest="set_response_header",
                       action="append",
                       help="rewrite a response header, 'Name: value' (repeatable)")
    icept.add_argument("--replace", action="append",
                       help="rewrite response bodies, 'REGEX/REPLACEMENT' (repeatable)")
    icept.add_argument("--map-remote", dest="map_remote", action="append",
                       help="mitmproxy map-remote spec (repeatable)")
    icept.add_argument("--out", help="save the raw mitmproxy flow dump locally")
    icept.add_argument("--har", help="save the HAR export locally")
    icept.add_argument("--max-flows", type=int, default=50,
                       help="how many flow summaries to print")
    icept.set_defaults(func=_bind(lab, cmd_intercept))

    doctor = net_sub.add_parser(
        "doctor", help="prove the MITM LXC is ready")
    doctor.add_argument("--lease", required=True)
    doctor.add_argument("--lxc", type=int, required=True)
    doctor.set_defaults(func=_bind(lab, cmd_doctor))
