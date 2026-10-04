"""Memflow policy: lease ownership before any helper call, and no guest bytes audited."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import bootstrap  # noqa: E402,F401

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from proxmox_agent_lab import leases as leases_module  # noqa: E402
from proxmox_agent_lab import memflow as memflow_module  # noqa: E402
from proxmox_agent_lab import store as store_module  # noqa: E402
from proxmox_agent_lab.errors import LabError  # noqa: E402
from proxmox_agent_lab.ssh import MEMFLOW_HELPER, MEMFLOW_SETUP, PolicyError  # noqa: E402
from support.fakessh import FakeSSH  # noqa: E402

LEASE = "memflow-lease"
VMID = 9001


class MemflowTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.fake = FakeSSH()
        self.audits: list[tuple[str, dict]] = []
        self.lab = mock.Mock()
        self.lab.STATE_ROOT = self.root
        self.lab.LabError = LabError
        self.lab.ssh = self.fake
        self.lab.audit = lambda event, **fields: self.audits.append((event, fields))
        self.lab.load_lease = lambda lease_id, active=True: leases_module.load_lease(
            self.root / "leases", lease_id, active=active
        )
        with store_module.Store(self.root / "lab.db") as store:
            store.create_lease(LEASE, purpose="memflow", expires_at=1_800_000_000)
            store.register_resource(LEASE, "qemu", VMID, name="probe")

    def _running(self) -> None:
        self.fake.add(r"qm status", stdout=b"status: running\n")

    def test_unowned_guest_is_refused_before_ssh(self) -> None:
        with self.assertRaises(LabError):
            memflow_module.cmd_processes(
                self.lab, argparse.Namespace(lease=LEASE, vmid=9002)
            )
        self.assertEqual(self.fake.calls, [])

    def test_stopped_guest_never_reaches_the_helper(self) -> None:
        self.fake.add(r"qm status", stdout=b"status: stopped\n")
        with self.assertRaises(LabError):
            memflow_module.cmd_read(
                self.lab,
                argparse.Namespace(lease=LEASE, vmid=VMID, addr="0x1000", len=16),
            )
        self.assertEqual(
            [call["argv"][0] for call in self.fake.calls], ["qm"]
        )

    def test_read_audits_the_fact_not_the_bytes(self) -> None:
        self._running()
        self.fake.add(
            r"pxl-memflow-run read",
            stdout=b'{"addr": "0x1000", "len": 2, "hex": "4142"}',
        )
        result = memflow_module.cmd_read(
            self.lab,
            argparse.Namespace(lease=LEASE, vmid=VMID, addr="0x1000", len=2),
        )
        self.assertEqual(result["hex"], "4142")
        event, fields = self.audits[-1]
        self.assertEqual(event, "memflow-read")
        self.assertNotIn("4142", json.dumps(fields))
        self.assertEqual(fields["length"], 2)

    def test_len_cap_refuses_before_ssh(self) -> None:
        with self.assertRaises(LabError):
            memflow_module.cmd_read(
                self.lab,
                argparse.Namespace(
                    lease=LEASE, vmid=VMID, addr="0x1000", len=32 * 1024 * 1024
                ),
            )
        self.assertEqual(self.fake.calls, [])

    def test_write_without_flag_spawns_nothing(self) -> None:
        with self.assertRaises(LabError):
            memflow_module.cmd_write(
                self.lab,
                argparse.Namespace(
                    lease=LEASE, vmid=VMID, addr="0x1000", hex="9090",
                    i_understand=False,
                ),
            )
        self.assertEqual(self.fake.calls, [])

    def test_write_passes_the_seam_flag_and_omits_bytes_from_audit(self) -> None:
        self._running()
        self.fake.add(
            r"phys-write",
            stdout=b'{"addr": "0x1000", "written": 2}',
        )
        memflow_module.cmd_phys_write(
            self.lab,
            argparse.Namespace(
                lease=LEASE, vmid=VMID, addr="0x1000", hex="9090",
                i_understand=True,
            ),
        )
        helper = [
            call for call in self.fake.calls if call["argv"][0] == MEMFLOW_HELPER
        ]
        self.assertEqual(len(helper), 1)
        self.assertTrue(helper[0]["memory_write"])
        self.assertEqual(helper[0]["argv"][1], "phys-write")
        self.assertNotIn("9090", json.dumps(self.audits[-1][1]))

    def test_missing_helper_names_host_setup(self) -> None:
        self._running()
        self.fake.add(r"pxl-memflow-run", returncode=127, stderr=b"not found")
        with self.assertRaises(LabError) as ctx:
            memflow_module.cmd_registers(
                self.lab, argparse.Namespace(lease=LEASE, vmid=VMID)
            )
        self.assertIn("host-setup", str(ctx.exception))

    def test_host_setup_print_does_not_touch_the_host(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = memflow_module.cmd_host_setup(
                self.lab,
                argparse.Namespace(
                    print_only=True, host_change_authorized=False, timeout=30
                ),
            )
        self.assertEqual(result, {"printed": True})
        self.assertIn("pxl-memflow-run", buf.getvalue())
        self.assertNotIn("pxl-ghidra", buf.getvalue())
        self.assertEqual(self.fake.calls, [])

    def test_host_setup_refuses_without_authorization(self) -> None:
        with self.assertRaises(PolicyError):
            memflow_module.cmd_host_setup(
                self.lab,
                argparse.Namespace(
                    print_only=False, host_change_authorized=False, timeout=30
                ),
            )
        self.assertEqual(self.fake.calls, [])

    def test_host_setup_installs_then_runs_the_exact_script(self) -> None:
        self.fake.add(r"tee ", stdout=b"")
        self.fake.add(r"install ", stdout=b"")
        self.fake.add(r"rm ", stdout=b"")
        self.fake.add(MEMFLOW_SETUP, stdout=b"installed\n")
        memflow_module.cmd_host_setup(
            self.lab,
            argparse.Namespace(
                print_only=False, host_change_authorized=True, timeout=15
            ),
        )
        argv = [call["argv"] for call in self.fake.calls]
        self.assertEqual(argv[0][0], "tee")
        self.assertEqual(argv[1][:2], ["install", "-m"])
        self.assertEqual(argv[1][-1], MEMFLOW_SETUP)
        self.assertEqual(argv[2][:2], ["rm", "-f"])
        self.assertEqual(argv[3], [MEMFLOW_SETUP])
        self.assertTrue(all(call["host_change"] for call in self.fake.calls))
        staged = self.fake.calls[0]["stdin"]
        self.assertIn(b"pxl-memflow-run", staged)
        self.assertNotIn(b"pxl-ghidra", staged)

    def test_doctor_stops_when_the_host_is_down(self) -> None:
        with self.assertRaises(LabError):
            memflow_module.cmd_doctor(
                self.lab, argparse.Namespace(vmid=None, lease=None)
            )
        self.assertEqual(self.fake.calls[0]["argv"], ["true"])
        self.assertEqual(len(self.fake.calls), 1)

    def test_boot_diagnose_reports_a_wedged_guest_without_auditing_text(self) -> None:
        self._running()
        self.fake.add(r"registers", stdout=b'{"RIP": "ffffffff81000000"}')
        panic = "Kernel panic - not syncing".encode().hex()
        self.fake.add(
            panic,
            stdout=b'{"hits": ["0x1000"], "needle_len": 4}',
        )
        self.fake.add(r"scan ", stdout=b'{"hits": [], "needle_len": 4}')
        result = memflow_module.cmd_boot_diagnose(
            self.lab,
            argparse.Namespace(
                lease=LEASE, vmid=VMID, settle=0, max_hits=4, timeout=5
            ),
        )
        self.assertEqual(result["cpu_state"], "wedged")
        self.assertEqual(result["signatures_found"][0]["signature"], "linux-panic")
        event, fields = self.audits[-1]
        self.assertEqual(event, "memflow-boot-diagnose")
        self.assertNotIn("Kernel panic", json.dumps(fields))
        self.assertEqual(fields["categories"], ["linux"])

    def test_processes_do_not_audit_names(self) -> None:
        self._running()
        self.fake.add(
            r"process-list",
            stdout=b'[{"pid": 4, "name": "secret-name"}]',
        )
        result = memflow_module.cmd_processes(
            self.lab, argparse.Namespace(lease=LEASE, vmid=VMID)
        )
        self.assertEqual(result["process_count"], 1)
        self.assertNotIn("secret-name", json.dumps(self.audits[-1][1]))
        self.assertEqual(self.audits[-1][1]["count"], 1)


if __name__ == "__main__":
    unittest.main()
