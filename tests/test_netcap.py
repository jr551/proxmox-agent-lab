"""Passive tap capture: lease-owned qemu VMs only, pcap stays local."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import bootstrap  # noqa: E402,F401

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from proxmox_agent_lab import config as config_module  # noqa: E402
from proxmox_agent_lab import errors  # noqa: E402
from proxmox_agent_lab import netcap  # noqa: E402
from proxmox_agent_lab import proxmox as proxmox_module  # noqa: E402
from proxmox_agent_lab import ssh as ssh_module  # noqa: E402
from proxmox_agent_lab import store as store_module  # noqa: E402
from proxmox_agent_lab import transfer as transfer_module  # noqa: E402
from support.fakessh import FakeSSH  # noqa: E402

LEASE = "netcap-lease"
PCAP = b"\xd4\xc3\xb2\xa1" + b"\x00" * 20


class CaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.lab = mock.Mock()
        self.lab.STATE_ROOT = self.root
        self.lab.CONFIG = config_module.get()
        self.fake = FakeSSH()
        self.prox = proxmox_module.Proxmox(
            self.fake, "pve", sleep=lambda _s: None
        )
        patcher = mock.patch.object(
            netcap.guest_module, "_make_proxmox", lambda config: self.prox
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        with store_module.Store(self.root / "lab.db") as store:
            store.create_lease(
                LEASE, kind="ordinary", purpose="capture", expires_at=1_800_000_000
            )

    def register(self, kind: str, vmid: int) -> None:
        with store_module.Store(self.root / "lab.db") as store:
            store.register_resource(LEASE, kind, vmid, name="guest", policy="disposable")

    def test_argv_is_allowlisted_and_names_only_this_vms_tap(self) -> None:
        iface, argv = netcap.capture_argv(101, "net0", 15, 0, "tcp port 443")
        self.assertEqual(iface, "tap101i0")
        ssh_module.check_allowed(argv)
        self.assertEqual(
            argv,
            [
                "timeout", "--signal=TERM", "15", "tcpdump", "-n", "-i",
                "tap101i0", "-w", "-", "-U", "tcp", "port", "443",
            ],
        )

    def test_a_shellish_filter_is_refused_before_the_seam(self) -> None:
        with self.assertRaises(errors.LabError):
            netcap.capture_argv(101, "net0", 15, 0, "tcp; id")
        self.assertEqual(self.fake.calls, [])

    def test_an_unregistered_guest_is_not_captured(self) -> None:
        with self.assertRaises(errors.LabError):
            netcap.cmd_capture(self.lab, mock.Mock(
                lease=LEASE, vmid=101, out=str(self.root / "x.pcap"),
                nic="net0", seconds=15, count=0, filter=None,
            ))
        self.assertEqual(self.fake.calls, [])

    def test_an_lxc_is_refused(self) -> None:
        self.register("lxc", 102)
        with self.assertRaises(errors.LabError) as caught:
            netcap.cmd_capture(self.lab, mock.Mock(
                lease=LEASE, vmid=102, out=str(self.root / "x.pcap"),
                nic="net0", seconds=15, count=0, filter=None,
            ))
        self.assertIn("qemu", str(caught.exception))
        self.assertEqual(self.fake.calls, [])

    def test_capture_writes_a_pcap_and_audits_no_frames(self) -> None:
        self.register("qemu", 101)
        self.fake.add(r"^qm status 101", stdout=b"status: running\n")
        self.fake.add(
            r"^ip -o link show",
            stdout=b"2: tap101i0: <BROADCAST,UP> mtu 1500\n",
        )
        self.fake.add(
            r"^timeout ",
            returncode=124,
            stdout=PCAP,
            stderr=b"3 packets captured\n",
        )
        out = self.root / "guest.pcap"
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            payload = netcap.cmd_capture(self.lab, mock.Mock(
                lease=LEASE, vmid=101, out=str(out),
                nic="net0", seconds=15, count=0, filter="tcp port 443",
            ))
        self.assertEqual(json.loads(buffer.getvalue())["packets"], 3)
        self.assertEqual(payload["iface"], "tap101i0")
        self.assertEqual(out.read_bytes(), PCAP)
        argv = self.fake.calls[-1]["argv"]
        self.assertEqual(argv[0], "timeout")
        self.assertIn("tap101i0", argv)
        self.assertNotIn("vmbr0", argv)
        audited = self.lab.audit.call_args
        self.assertEqual(audited.args[0], "netcap-capture")
        self.assertNotIn("tcp port 443", audited.kwargs.values())
        self.assertNotIn("filter", audited.kwargs)

    def test_a_stopped_guest_is_not_dumped(self) -> None:
        self.register("qemu", 101)
        self.fake.add(r"^qm status 101", stdout=b"status: stopped\n")
        with self.assertRaises(errors.LabError):
            netcap.cmd_capture(self.lab, mock.Mock(
                lease=LEASE, vmid=101, out=str(self.root / "x.pcap"),
                nic="net0", seconds=15, count=0, filter=None,
            ))
        self.assertTrue(all(call["argv"][0] != "timeout" for call in self.fake.calls))

    def test_mcp_cannot_write_the_pcap_outside_the_scratch_dir(self) -> None:
        self.register("qemu", 101)
        transfer_module.set_unconfined(True)
        self.addCleanup(transfer_module.set_unconfined, False)
        with self.assertRaises(errors.LabError) as caught:
            netcap.cmd_capture(self.lab, mock.Mock(
                lease=LEASE, vmid=101, out="/tmp/not-scratch.pcap",
                nic="net0", seconds=15, count=0, filter=None,
            ))
        self.assertIn("scratch", str(caught.exception))
        self.assertEqual(self.fake.calls, [])


if __name__ == "__main__":
    unittest.main()
