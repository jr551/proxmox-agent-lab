"""Network capture / SSL inspection / MITM relay. Capture and interception run
on the host over the shared [memflow] SSH channel and require a lease; each
guard gets a test that fails if it is removed."""

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
import subprocess  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402
from unittest import mock  # noqa: E402

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from proxmox_agent_lab import cli as LAB  # noqa: E402
from proxmox_agent_lab import memflow as MF  # noqa: E402
from proxmox_agent_lab import netcap  # noqa: E402


class Args:
    def __init__(self, **kw: object) -> None:
        self.__dict__.update(kw)


def completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(["ssh"], returncode, stdout, stderr)


class EnableGuardTests(unittest.TestCase):
    def test_off_without_host_ssh(self) -> None:
        with mock.patch.object(MF, "ENABLED", False):
            with self.assertRaises(LAB.LabError):
                netcap._require_enabled(LAB)


class IfaceResolveTests(unittest.TestCase):
    LINKS = (
        "1: lo: <LOOPBACK>\n"
        "2: eth0: <BROADCAST>\n"
        "7: tap9001i0: <BROADCAST,MULTICAST,UP>\n"
        "9: fwln9001i0@fwpr9001p0: <BROADCAST>\n"
    )

    def test_resolves_tap_from_nic(self) -> None:
        with mock.patch.object(netcap, "_ssh",
                               return_value=completed(0, self.LINKS)):
            self.assertEqual(
                netcap._resolve_iface(LAB, 9001, "net0", None), "tap9001i0")

    def test_missing_iface_errors(self) -> None:
        with mock.patch.object(netcap, "_ssh",
                               return_value=completed(0, self.LINKS)):
            with self.assertRaises(LAB.LabError):
                netcap._resolve_iface(LAB, 9999, "net0", None)

    def test_explicit_override_wins(self) -> None:
        links = self.LINKS + "11: veth5i0: <BROADCAST>\n"
        with mock.patch.object(netcap, "_ssh", return_value=completed(0, links)):
            self.assertEqual(
                netcap._resolve_iface(LAB, 5, "net0", "veth5i0"), "veth5i0")


class ModifyArgsTests(unittest.TestCase):
    def test_set_header_maps_to_request_modifier(self) -> None:
        out = netcap._modify_args(LAB, Args(
            set_header=["X-Test: 1"], set_response_header=None,
            replace=None, map_remote=None))
        self.assertEqual(out, ["--modify-headers", "/~q/X-Test/1"])

    def test_replace_maps_to_body_modifier(self) -> None:
        out = netcap._modify_args(LAB, Args(
            set_header=None, set_response_header=None,
            replace=["foo/bar"], map_remote=None))
        self.assertEqual(out, ["--modify-body", "/~s/foo/bar"])

    def test_malformed_header_rejected(self) -> None:
        with self.assertRaises(LAB.LabError):
            netcap._modify_args(LAB, Args(
                set_header=["no-colon-here"], set_response_header=None,
                replace=None, map_remote=None))


class HarSummaryTests(unittest.TestCase):
    def test_summarises_entries(self) -> None:
        har = {
            "log": {"entries": [
                {"request": {"method": "GET", "url": "https://x/a"},
                 "response": {"status": 200,
                              "content": {"mimeType": "text/html", "size": 12}}},
            ]}
        }
        import json
        b64 = base64.b64encode(json.dumps(har).encode()).decode()
        rows = netcap._summarise_har(b64)
        self.assertEqual(rows[0]["status"], 200)
        self.assertEqual(rows[0]["url"], "https://x/a")

    def test_empty_is_no_flows(self) -> None:
        self.assertEqual(netcap._summarise_har(""), [])


class CaptureTests(unittest.TestCase):
    def test_capture_requires_lease_and_writes_pcap(self) -> None:
        blob = b"\xd4\xc3\xb2\xa1netbytes"
        links = "7: tap42i0: <UP>\n"
        payload = "PKTS=99\n" + base64.b64encode(blob).decode()

        def fake_ssh(lab, argv, *, timeout=60):
            if argv[:2] == ["ip", "-o"]:
                return completed(0, links)
            return completed(0, payload)  # the tcpdump script

        out = Path(tempfile.mkdtemp()) / "net.pcap"
        audited: dict = {}
        with mock.patch.object(MF, "ENABLED", True), \
             mock.patch.object(LAB, "ProxmoxAPI") as api, \
             mock.patch.object(LAB, "load_lease", return_value={}) as ll, \
             mock.patch.object(netcap, "_ssh", side_effect=fake_ssh), \
             mock.patch.object(netcap, "_running_qemu"), \
             mock.patch.object(LAB, "audit",
                               lambda e, *, sync=True, **f: audited.update(
                                   {"event": e, "fields": f})), \
             mock.patch("builtins.print"):
            api.return_value = mock.Mock()
            netcap.cmd_capture(LAB, Args(lease="L", vmid=42, nic="net0",
                                         iface=None, seconds=2, count=0,
                                         filter=None, out=str(out)))
        ll.assert_called_once()  # a lease is mandatory
        self.assertEqual(out.read_bytes(), blob)
        self.assertEqual(audited["event"], "netcap-capture")
        self.assertEqual(audited["fields"]["packets"], 99)


if __name__ == "__main__":
    unittest.main()
