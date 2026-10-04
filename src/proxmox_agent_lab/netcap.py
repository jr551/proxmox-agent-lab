"""Passive capture of one lease-owned qemu VM's traffic.

A running QEMU guest's NIC is a tap on the host (``tap<vmid>i<n>``).
``tcpdump`` on that tap sees the frames that guest sends and receives,
including traffic the guest OS cannot hide, and nothing else: the seam
refuses any interface that is not a tap, and this module only names the
tap of the VM the lease owns. There is no ``--iface`` override, so a
bridge or another guest's NIC cannot be substituted.

The capture is bounded (``timeout`` plus an optional packet count), writes
the pcap to stdout rather than a file on the host, and is audited as the
fact of the capture -- interface, duration, packet count, byte length --
never the frames and never the BPF text. TLS stays ciphertext. The old
MITM relay (a proxy LXC, a forged CA, request rewriting) is not this
command.
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import guest as guest_module
from . import ssh as ssh_module
from . import transfer as transfer_module
from .errors import LabError

_NIC = re.compile(r"^net([0-9]{1,2})$")
_PACKETS = re.compile(rb"(\d+) packets captured")
_PCAP_MAGIC = (
    b"\xd4\xc3\xb2\xa1",
    b"\xa1\xb2\xc3\xd4",
    b"\x4d\x3c\xb2\xa1",
    b"\xa1\xb2\x3c\x4d",
)
DEFAULT_SECONDS = 15


def _emit(payload: dict) -> dict:
    print(json.dumps(payload, indent=2, sort_keys=True))
    return payload


def _text(raw: Any) -> str:
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw).decode("utf-8", "replace")
    return raw if isinstance(raw, str) else ""


def _bytes(raw: Any) -> bytes:
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw)
    if isinstance(raw, str):
        return raw.encode("utf-8", "replace")
    return b""


def _filter_tokens(expression: str | None) -> list[str]:
    if expression is None or expression.strip() == "":
        return []
    tokens = expression.split()
    if len(tokens) > ssh_module.MAX_CAPTURE_FILTER_TOKENS:
        raise LabError(
            "capture filter is too long "
            f"({ssh_module.MAX_CAPTURE_FILTER_TOKENS} words maximum)"
        )
    for token in tokens:
        if (
            token.startswith("-")
            or ssh_module._BPF_TOKEN.fullmatch(token) is None
        ):
            raise LabError(
                "capture filter must be plain BPF words "
                "(for example 'tcp port 443'); flags and shell syntax are refused"
            )
    return tokens


def capture_argv(
    vmid: int, nic: str, seconds: int, count: int, expression: str | None
) -> tuple[str, list[str]]:
    """``(iface, remote argv)`` for one capture. Pure: no I/O."""
    match = _NIC.fullmatch(nic)
    if match is None:
        raise LabError("--nic must be netN (for example net0)")
    if not 1 <= int(seconds) <= ssh_module.MAX_CAPTURE_SECONDS:
        raise LabError(
            f"--seconds must be 1..{ssh_module.MAX_CAPTURE_SECONDS}"
        )
    if count and not 1 <= int(count) <= ssh_module.MAX_CAPTURE_PACKETS:
        raise LabError(
            f"--count must be 1..{ssh_module.MAX_CAPTURE_PACKETS}, or 0 to "
            "stop only when the duration ends"
        )
    iface = f"tap{int(vmid)}i{match.group(1)}"
    argv = [
        "timeout", "--signal=TERM", str(int(seconds)),
        "tcpdump", "-n", "-i", iface, "-w", "-", "-U",
    ]
    if count:
        argv += ["-c", str(int(count))]
    argv += _filter_tokens(expression)
    return iface, argv


def cmd_capture(lab: Any, args: Any) -> dict:
    """Write a local pcap of one lease-owned running qemu VM."""
    lease_id, vmid = str(args.lease), int(args.vmid)
    row = guest_module.require_owned(lab, lease_id, None, vmid)
    kind = str(row["kind"])
    if kind != "qemu":
        raise LabError(
            f"{kind} {vmid} has no qemu tap to capture. "
            "netcap capture is for VMs; an LXC does not appear as tap<vmid>i<n>."
        )
    seconds = int(getattr(args, "seconds", None) or DEFAULT_SECONDS)
    count = int(getattr(args, "count", None) or 0)
    nic = str(getattr(args, "nic", None) or "net0")
    iface, argv = capture_argv(
        vmid, nic, seconds, count, getattr(args, "filter", None)
    )
    # The interface name embeds the owned vmid, so the argv cannot name
    # another guest's tap even if the filter or nic is surprising.
    if not iface.startswith(f"tap{vmid}i"):
        raise LabError(f"refusing to capture {iface}: it is not {vmid}'s tap")
    # Resolve the local path before any host call, so an MCP client that
    # names a path outside the scratch directory never starts a capture.
    out = transfer_module._confined_path(args.out)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise LabError(f"cannot write {out}: {exc}") from exc
    prox = guest_module._make_proxmox(lab.CONFIG)
    if prox.status("qemu", vmid) != "running":
        raise LabError(
            f"qemu {vmid} is not running; there is no tap to capture until it is"
        )
    seam = guest_module._ssh_of(prox)
    link = seam.run(["ip", "-o", "link", "show"], timeout=30)
    if not getattr(link, "ok", False):
        raise LabError(
            "could not list host interfaces: " + _text(link.stderr).strip()[:300]
        )
    names = set(re.findall(r"^\d+:\s+([^:@\s]+)", _text(link.stdout), re.M))
    if iface not in names:
        raise LabError(
            f"no host interface {iface} for qemu {vmid} {nic}. "
            "Is that NIC attached, and is the guest still running?"
        )
    result = seam.run(argv, timeout=float(seconds) + 30)
    # 124 is `timeout` firing on schedule. 0 is tcpdump hitting -c first.
    if result.returncode not in (0, 124):
        raise LabError(
            "capture failed on the host: " + _text(result.stderr).strip()[:300]
        )
    data = _bytes(result.stdout)
    if not data.startswith(_PCAP_MAGIC):
        raise LabError(
            "capture returned no pcap"
            + (": " + _text(result.stderr).strip()[:200] if result.stderr else "")
        )
    transfer_module._write_private(out, data)
    matched = _PACKETS.search(_bytes(result.stderr))
    packets = int(matched.group(1)) if matched else None
    lab.audit(
        "netcap-capture",
        lease=lease_id,
        vmid=vmid,
        iface=iface,
        seconds=seconds,
        packets=packets,
        nbytes=len(data),
    )
    return _emit({
        "lease_id": lease_id,
        "vmid": vmid,
        "iface": iface,
        "seconds": seconds,
        "packets": packets,
        "bytes": len(data),
        "out": str(out),
        "note": "pcap of this VM's tap only; TLS is ciphertext",
    })


def register(sub: Any, lab: Any) -> None:
    from .cli import _bind

    net = sub.add_parser(
        "netcap", help="capture one lease-owned VM's traffic to a local pcap"
    )
    commands = net.add_subparsers(dest="netcap_command", required=True)
    capture = commands.add_parser(
        "capture",
        help="tcpdump that VM's tap (passive; TLS stays ciphertext)",
    )
    capture.add_argument("--lease", required=True)
    capture.add_argument("--vmid", type=int, required=True)
    capture.add_argument("--out", required=True, help="local pcap path")
    capture.add_argument("--nic", default="net0", help="guest NIC (netN)")
    capture.add_argument(
        "--seconds", type=int, default=DEFAULT_SECONDS,
        help=f"stop after this many seconds (1..{ssh_module.MAX_CAPTURE_SECONDS})",
    )
    capture.add_argument(
        "--count", type=int, default=0,
        help="stop after this many packets (0 = duration only)",
    )
    capture.add_argument(
        "--filter",
        help="BPF expression, for example 'tcp port 443'",
    )
    capture.set_defaults(func=_bind(lab, cmd_capture))
