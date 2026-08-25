"""USB sniffing captures on the host over the shared SSH channel; passthrough
changes are gated. Each guard gets a test that fails if it is removed."""

from __future__ import annotations

import base64
import os
from pathlib import Path

os.environ["PROXMOX_AGENT_LAB_CONFIG"] = str(
    Path(__file__).parent / "fixtures" / "config.toml"
)

import shutil
import tempfile
# ...and at a disposable state directory: a test must never write into the
# developer's real controller state. Cleared here so a previous run cannot
# leak into this one; imports all happen before any test runs.
_TEST_STATE = Path(tempfile.gettempdir()) / "proxmox-agent-lab-test-state"
shutil.rmtree(_TEST_STATE, ignore_errors=True)
_TEST_STATE.mkdir(parents=True, exist_ok=True)
os.environ["PROXMOX_AGENT_LAB_STATE"] = str(_TEST_STATE)

import json  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402
from unittest import mock  # noqa: E402

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from proxmox_agent_lab import cli as LAB  # noqa: E402
from proxmox_agent_lab import memflow as MF  # noqa: E402
from proxmox_agent_lab import usb  # noqa: E402


class Args:
    def __init__(self, **kw: object) -> None:
        self.__dict__.update(kw)


def completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(["ssh"], returncode, stdout, stderr)


LSUSB = (
    "Bus 001 Device 002: ID 04e8:61b6 Samsung M3 Portable\n"
    "Bus 003 Device 003: ID 0d8c:0012 C-Media USB Audio Device\n"
    "Bus 001 Device 001: ID 1d6b:0002 Linux Foundation 2.0 root hub\n"
)


class EnableGuardTests(unittest.TestCase):
    def test_off_without_host_ssh(self) -> None:
        with mock.patch.object(MF, "ENABLED", False):
            with self.assertRaises(LAB.LabError):
                usb._require_enabled(LAB)


class ResolveTests(unittest.TestCase):
    def test_resolve_by_vendor_product(self) -> None:
        with mock.patch.object(usb, "_ssh", return_value=completed(0, LSUSB)):
            d = usb._resolve(LAB, "04e8:61b6")
        self.assertEqual((d["bus"], d["dev"]), (1, 2))

    def test_resolve_by_bus_dev(self) -> None:
        with mock.patch.object(usb, "_ssh", return_value=completed(0, LSUSB)):
            d = usb._resolve(LAB, "3-3")
        self.assertEqual(d["id"], "0d8c:0012")

    def test_unknown_device_errors(self) -> None:
        with mock.patch.object(usb, "_ssh", return_value=completed(0, LSUSB)):
            with self.assertRaises(LAB.LabError):
                usb._resolve(LAB, "dead:beef")


class AttachGateTests(unittest.TestCase):
    def test_attach_refuses_without_authorization(self) -> None:
        # The gate must trip before any host call.
        with mock.patch.object(MF, "ENABLED", True), \
             mock.patch.object(usb, "_ssh") as sshed:
            with self.assertRaises(LAB.LabError) as ctx:
                usb.cmd_attach(LAB, Args(lease="L", vmid=101,
                                         device="0d8c:0012",
                                         host_change_authorized=False))
        self.assertIn("--host-change-authorized", str(ctx.exception))
        sshed.assert_not_called()


class SniffTests(unittest.TestCase):
    def test_sniff_writes_pcap_and_reports_packets(self) -> None:
        blob = b"\xd4\xc3\xb2\xa1pcapbytes"
        payload = "PKTS=1234\n" + base64.b64encode(blob).decode()

        def fake_ssh(lab, argv, *, timeout=60):
            # First call is lsusb (resolve), second is the capture script.
            if argv[:1] == ["lsusb"] or (len(argv) and argv[0] == "lsusb"):
                return completed(0, LSUSB)
            return completed(0, payload)

        out = Path(tempfile.mkdtemp()) / "cap.pcap"
        audited: dict = {}
        with mock.patch.object(MF, "ENABLED", True), \
             mock.patch.object(LAB, "load_lease", return_value={}), \
             mock.patch.object(usb, "_ssh", side_effect=fake_ssh), \
             mock.patch.object(LAB, "audit",
                               lambda e, **f: audited.update(
                                   {"event": e, "fields": f})), \
             mock.patch("builtins.print") as printed:
            usb.cmd_sniff(LAB, Args(lease="L", device="04e8:61b6",
                                    seconds=3, count=0, out=str(out)))
        # The pcap bytes are decoded from base64 and written verbatim.
        self.assertEqual(out.read_bytes(), blob)
        self.assertEqual(audited["event"], "usb-sniff")
        self.assertEqual(audited["fields"]["packets"], 1234)
        printed_json = printed.call_args[0][0]
        self.assertIn("usb.device_address == 2", printed_json)


if __name__ == "__main__":
    unittest.main()
