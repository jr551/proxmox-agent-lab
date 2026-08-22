"""The Proxmox `diskwrite` counter has been seen reading 0 for a whole
session on a guest that was demonstrably writing, so every part of the
cross-check that replaces it gets a test: the exact endpoints and monitor
command used, the parsing of a real `info blockstats` transcript, the delta
arithmetic, what happens when a signal is switched off or refused, and the
disagreement that is the whole point of the output."""

from __future__ import annotations

import os
from pathlib import Path

os.environ["PROXMOX_AGENT_LAB_CONFIG"] = str(
    Path(__file__).parent / "fixtures" / "config.toml"
)

import shutil
import tempfile

# A disposable state directory: a test must never write into the developer's
# real controller state.
_TEST_STATE = Path(tempfile.gettempdir()) / "proxmox-agent-lab-test-state"
shutil.rmtree(_TEST_STATE, ignore_errors=True)
_TEST_STATE.mkdir(parents=True, exist_ok=True)
os.environ["PROXMOX_AGENT_LAB_STATE"] = str(_TEST_STATE)

import subprocess  # noqa: E402
import sys  # noqa: E402
import unittest  # noqa: E402
from unittest import mock  # noqa: E402

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from proxmox_agent_lab import cli as LAB  # noqa: E402
from proxmox_agent_lab import diskactivity  # noqa: E402
from proxmox_agent_lab import memflow  # noqa: E402


VMID = 9001
VOLID = "local:9001/vm-9001-disk-0.qcow2"
IMAGE = "/var/lib/vz/images/9001/vm-9001-disk-0.qcow2"

# A real HMP transcript shape: one active drive, an empty CD-ROM that carries
# no counters, an EFI disk, and QEMU's indented per-node breakdown, which must
# not be folded into the device totals.
BLOCKSTATS = """\
drive-scsi0: rd_bytes=4194304 wr_bytes={written} rd_operations=512 \
wr_operations=1024 flush_operations=64 wr_total_time_ns=1234567 \
rd_total_time_ns=2345678 flush_total_time_ns=345678 rd_merged=0 wr_merged=0 \
idle_time_ns=98765432
    backing_file=/var/lib/vz/images/9001/base.qcow2 rd_bytes=1024 \
wr_bytes=555555 rd_operations=2 wr_operations=0 flush_operations=0 \
wr_total_time_ns=0 rd_total_time_ns=0 flush_total_time_ns=0
drive-ide2: [not inserted]
    Removable device: not locked, tray closed
drive-efidisk0: rd_bytes=131072 wr_bytes=0 rd_operations=32 wr_operations=0 \
flush_operations=0 wr_total_time_ns=0 rd_total_time_ns=0 \
flush_total_time_ns=0 rd_merged=0 wr_merged=0 idle_time_ns=1
"""


def completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(["ssh"], returncode, stdout, stderr)


class FakeAPI:
    """Answers exactly the endpoints the probe is allowed to use, and records
    every request so a test can assert the path and the payload rather than
    just the result."""

    def __init__(
        self, *,
        diskwrite: list[int] | None = None,
        blockstats: list[str] | None = None,
        monitor_error: str | None = None,
        config: dict | None = None,
        volume_path: str | None = IMAGE,
        status: str = "running",
    ) -> None:
        self.calls: list[tuple[str, str, object]] = []
        self._diskwrite = list(diskwrite if diskwrite is not None else [0, 0])
        self._blockstats = list(blockstats or [])
        self._monitor_error = monitor_error
        self._config = config if config is not None else {
            "scsi0": f"{VOLID},discard=on,size=32G",
            "ide2": "local:iso/reactos.iso,media=cdrom",
        }
        self._volume_path = volume_path
        self._status = status

    def call(self, method: str, path: str, data: object = None, **_: object):
        self.calls.append((method, path, data))
        if path.endswith("/status/current"):
            written = (self._diskwrite.pop(0) if self._diskwrite
                       else 0)
            return {"status": self._status, "diskwrite": written,
                    "uptime": 99999}
        if path.endswith("/monitor"):
            if self._monitor_error is not None:
                raise LAB.LabError(self._monitor_error)
            return self._blockstats.pop(0) if self._blockstats else ""
        if path.endswith(f"/qemu/{VMID}/config"):
            return dict(self._config)
        if "/storage/" in path and "/content/" in path:
            if self._volume_path is None:
                return {"size": 1}
            return {"path": self._volume_path, "format": "qcow2"}
        raise AssertionError(f"unexpected request: {method} {path}")

    def paths(self, method: str) -> list[str]:
        return [call[1] for call in self.calls if call[0] == method]


class HostRuns:
    """Records host commands and replays scripted `du` output."""

    def __init__(self, *sizes: int, returncode: int = 0,
                 stderr: str = "") -> None:
        self.argv: list[list[str]] = []
        self._sizes = list(sizes)
        self._returncode = returncode
        self._stderr = stderr

    def __call__(self, lab, argv, *, timeout: int = 60):
        self.argv.append(list(argv))
        if argv[:1] == ["pvesm"]:
            return completed(stdout=IMAGE + "\n")
        size = self._sizes.pop(0) if self._sizes else 0
        return completed(self._returncode, f"{size}\t{IMAGE}\n", self._stderr)


class BlockstatsParsingTests(unittest.TestCase):
    def test_a_real_transcript_yields_only_devices_with_a_medium(self) -> None:
        devices = diskactivity.parse_blockstats(
            BLOCKSTATS.format(written=8388608)
        )
        self.assertEqual(sorted(devices), ["drive-efidisk0", "drive-scsi0"])
        self.assertEqual(devices["drive-scsi0"]["wr_bytes"], 8388608)
        self.assertEqual(devices["drive-scsi0"]["rd_bytes"], 4194304)
        self.assertEqual(devices["drive-scsi0"]["wr_operations"], 1024)

    def test_the_indented_backing_node_is_not_counted_twice(self) -> None:
        """QEMU prints the backing file's own counters underneath the device.
        Adding them to the total would invent writes that never happened."""
        devices = diskactivity.parse_blockstats(
            BLOCKSTATS.format(written=8388608)
        )
        self.assertNotIn("backing_file=/var/lib/vz/images/9001/base.qcow2",
                         devices)
        self.assertEqual(
            diskactivity.written_from_blockstats(devices), 8388608
        )

    def test_a_refusal_parses_as_no_devices_rather_than_as_zero(self) -> None:
        for body in ("unknown command: 'info blockstatz'", "", "\n",
                     "Permission check failed (/vms/9001, Sys.Audit)"):
            self.assertEqual(diskactivity.parse_blockstats(body), {})

    def test_du_output_is_read_as_allocated_bytes(self) -> None:
        self.assertEqual(
            diskactivity.parse_du(f"1048576\t{IMAGE}\n4096\t/tmp/other\n"),
            {IMAGE: 1048576, "/tmp/other": 4096},
        )
        self.assertEqual(diskactivity.parse_du(""), {})


class ImageVolumeTests(unittest.TestCase):
    def test_only_writable_disks_are_measured(self) -> None:
        volumes = diskactivity.image_volumes({
            "scsi0": f"{VOLID},size=32G",
            "virtio1": "local-lvm:vm-9001-disk-1,size=8G",
            "efidisk0": "local-lvm:vm-9001-disk-2,efitype=4m,size=1M",
            "ide2": "local:iso/reactos.iso,media=cdrom",
            "unused0": "local:9001/vm-9001-disk-9.qcow2",
            "sata0": "none,media=cdrom",
            "name": "reactos",
            "memory": 2048,
        })
        self.assertEqual(
            volumes,
            [("efidisk0", "local-lvm:vm-9001-disk-2"),
             ("scsi0", VOLID),
             ("virtio1", "local-lvm:vm-9001-disk-1")],
        )

    def test_a_config_with_no_disks_yields_nothing(self) -> None:
        self.assertEqual(diskactivity.image_volumes({"memory": 1024}), [])


class DisagreementTests(unittest.TestCase):
    def test_zero_against_non_zero_is_the_disagreement_that_matters(self) -> None:
        found = diskactivity.disagreements({
            "proxmox_diskwrite": 0,
            "qmp_blockstats": 1048576,
        })
        self.assertEqual(
            found,
            ["proxmox_diskwrite saw no write while qmp_blockstats saw "
             "1048576 bytes"],
        )

    def test_differing_magnitudes_are_not_a_disagreement(self) -> None:
        """qcow2 allocates in clusters and the page cache delays writeback, so
        the numbers are expected to differ. Only zero-versus-something means
        one of the signals cannot see what the other can."""
        self.assertEqual(
            diskactivity.disagreements({
                "host_image_du": 65536,
                "proxmox_diskwrite": 4096,
                "qmp_blockstats": 1048576,
            }),
            [],
        )

    def test_every_disagreeing_pair_is_named(self) -> None:
        found = diskactivity.disagreements({
            "host_image_du": 0,
            "proxmox_diskwrite": 0,
            "qmp_blockstats": 4096,
        })
        self.assertEqual(len(found), 2)
        self.assertTrue(all("qmp_blockstats saw 4096" in item
                            for item in found))


class MonitorChannelTests(unittest.TestCase):
    def test_the_only_monitor_command_is_the_blockstats_query(self) -> None:
        api = FakeAPI(blockstats=[BLOCKSTATS.format(written=1)])
        devices, problem = diskactivity.blockstats(LAB, api, VMID)
        self.assertIsNone(problem)
        method, path, data = api.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(path, f"/nodes/{LAB.NODE}/qemu/{VMID}/monitor")
        self.assertEqual(data, {"command": "info blockstats"})
        self.assertIn("drive-scsi0", devices)

    def test_a_refusal_in_the_body_is_reported_not_read_as_zero(self) -> None:
        """The monitor answers with text, refusals included, so a body that is
        not a blockstats transcript must never become 'wrote nothing'."""
        api = FakeAPI(blockstats=["unknown command: 'info blockstatz'"])
        devices, problem = diskactivity.blockstats(LAB, api, VMID)
        self.assertEqual(devices, {})
        self.assertIn("no block devices", problem)
        self.assertIn("unknown command", problem)

    def test_a_refused_endpoint_costs_the_signal_not_the_run(self) -> None:
        """The lab token is PVEVMAdmin-scoped and the monitor endpoint wants
        Sys.Audit|Sys.Modify, so this is the expected outcome on a real node,
        not an exceptional one."""
        api = FakeAPI(monitor_error="Proxmox HTTP 403: Permission check failed")
        devices, problem = diskactivity.blockstats(LAB, api, VMID)
        self.assertEqual(devices, {})
        self.assertIn("monitor endpoint unavailable", problem)
        self.assertIn("403", problem)


class HostMeasurementTests(unittest.TestCase):
    def test_du_measures_allocated_bytes_of_the_resolved_image(self) -> None:
        runs = HostRuns(1048576)
        with mock.patch.object(memflow, "host_run", runs):
            sizes, problem = diskactivity.host_image_sizes(
                LAB, {VOLID: IMAGE}
            )
        self.assertIsNone(problem)
        self.assertEqual(sizes, {IMAGE: 1048576})
        # --block-size=1, because `ls` reports the apparent size of a sparse
        # image and would call an untouched qcow2 gigabytes of I/O.
        self.assertEqual(runs.argv, [["du", "--block-size=1", "--", IMAGE]])

    def test_the_ssh_channel_being_off_is_reported_not_raised(self) -> None:
        with mock.patch.object(memflow, "ENABLED", False):
            sizes, problem = diskactivity.host_image_sizes(
                LAB, {VOLID: IMAGE}
            )
        self.assertEqual(sizes, {})
        self.assertIn("host SSH channel is off", problem)

    def test_a_block_device_is_reported_as_unmeasurable(self) -> None:
        """There is no file to grow on LVM or ZFS, so du would silently
        report zero for a guest writing at full speed."""
        with mock.patch.object(memflow, "host_run", HostRuns(0)) as run:
            sizes, problem = diskactivity.host_image_sizes(
                LAB, {"local-lvm:vm-9001-disk-0": "/dev/pve/vm-9001-disk-0"}
            )
        self.assertEqual(sizes, {})
        self.assertIn("block devices", problem)
        self.assertEqual(run.argv, [])

    def test_a_partial_du_failure_keeps_what_was_measured(self) -> None:
        runs = HostRuns(4096, returncode=1, stderr="du: cannot read '/x'")
        with mock.patch.object(memflow, "host_run", runs):
            sizes, problem = diskactivity.host_image_sizes(
                LAB, {VOLID: IMAGE}
            )
        self.assertEqual(sizes, {IMAGE: 4096})
        self.assertIn("reported a problem", problem)

    def test_the_image_path_comes_from_the_storage_content_endpoint(self) -> None:
        api = FakeAPI()
        paths, problem = diskactivity.image_paths(LAB, api, VMID)
        self.assertIsNone(problem)
        self.assertEqual(paths, {VOLID: IMAGE})
        self.assertIn(
            f"/nodes/{LAB.NODE}/storage/local/content/{VOLID}",
            api.paths("GET"),
        )

    def test_pvesm_resolves_a_volume_the_api_cannot(self) -> None:
        api = FakeAPI(volume_path=None)
        runs = HostRuns()
        with mock.patch.object(memflow, "host_run", runs):
            paths, _ = diskactivity.image_paths(LAB, api, VMID)
        self.assertEqual(paths, {VOLID: IMAGE})
        self.assertEqual(runs.argv, [["pvesm", "path", VOLID]])

    def test_an_unresolvable_image_is_a_reason_not_an_exception(self) -> None:
        api = FakeAPI(volume_path=None)
        with mock.patch.object(memflow, "ENABLED", False):
            paths, problem = diskactivity.image_paths(LAB, api, VMID)
        self.assertEqual(paths, {})
        self.assertIn("no disk image resolved", problem)


class MeasurementTests(unittest.TestCase):
    """The counter is only meaningful as a delta, and the delta is only
    meaningful next to the signals that can contradict it."""

    def _measure(self, api, **kw):
        return diskactivity.measure(
            LAB, api, VMID, interval=0, deadline=5, **kw
        )

    def test_the_counter_is_sampled_twice_and_reported_as_a_delta(self) -> None:
        api = FakeAPI(diskwrite=[1000, 5096])
        result = self._measure(api)
        signal = result["signals"]["proxmox_diskwrite"]
        self.assertTrue(signal["available"])
        self.assertEqual(signal["first_bytes"], 1000)
        self.assertEqual(signal["second_bytes"], 5096)
        self.assertEqual(signal["written_bytes"], 4096)
        self.assertEqual(
            api.paths("GET"),
            [f"/nodes/{LAB.NODE}/qemu/{VMID}/status/current"] * 2,
        )

    def test_without_ground_truth_a_still_counter_proves_nothing(self) -> None:
        """The bug this exists for: 0 bytes over the interval is exactly what a
        writing qcow2 guest reports, so 'idle' is not an available answer."""
        result = self._measure(FakeAPI(diskwrite=[0, 0]))
        self.assertIsNone(result["writing"])
        self.assertEqual(result["disagreement"], [])
        self.assertIn("cannot tell an idle guest", result["note"])

    def _ground_truth(self, *, first_wr: int, second_wr: int,
                      diskwrite: list[int], du: tuple[int, int]):
        api = FakeAPI(
            diskwrite=diskwrite,
            blockstats=[BLOCKSTATS.format(written=first_wr),
                        BLOCKSTATS.format(written=second_wr)],
        )
        runs = HostRuns(*du)
        with mock.patch.object(memflow, "host_run", runs):
            return api, runs, self._measure(api, ground_truth=True)

    def test_all_three_signals_are_reported_side_by_side(self) -> None:
        api, runs, result = self._ground_truth(
            first_wr=8388608, second_wr=9437184,
            diskwrite=[0, 0], du=(1048576, 1114112),
        )
        self.assertEqual(
            result["written_bytes"],
            {"proxmox_diskwrite": 0,
             "qmp_blockstats": 1048576,
             "host_image_du": 65536},
        )
        self.assertEqual(
            result["signals"]["qmp_blockstats"]["devices"],
            {"drive-efidisk0": 0, "drive-scsi0": 1048576},
        )
        self.assertEqual(
            result["signals"]["host_image_du"]["paths"], {IMAGE: 65536}
        )
        self.assertTrue(result["writing"])
        self.assertEqual(len(runs.argv), 2)

    def test_the_disagreement_is_named_explicitly(self) -> None:
        _, _, result = self._ground_truth(
            first_wr=8388608, second_wr=9437184,
            diskwrite=[4096, 4096], du=(1048576, 1048576),
        )
        self.assertEqual(
            result["disagreement"],
            ["host_image_du saw no write while qmp_blockstats saw "
             "1048576 bytes",
             "proxmox_diskwrite saw no write while qmp_blockstats saw "
             "1048576 bytes"],
        )
        self.assertIn("signals disagree", result["note"])

    def test_agreeing_signals_produce_no_disagreement(self) -> None:
        _, _, result = self._ground_truth(
            first_wr=8388608, second_wr=8388608,
            diskwrite=[4096, 4096], du=(1048576, 1048576),
        )
        self.assertEqual(result["disagreement"], [])
        self.assertFalse(result["writing"])

    def test_a_missing_monitor_still_returns_the_other_signals(self) -> None:
        api = FakeAPI(
            diskwrite=[0, 0],
            monitor_error="Proxmox HTTP 403: Permission check failed",
        )
        runs = HostRuns(1048576, 2097152)
        with mock.patch.object(memflow, "host_run", runs):
            result = self._measure(api, ground_truth=True)
        self.assertFalse(result["signals"]["qmp_blockstats"]["available"])
        self.assertIn("403", result["signals"]["qmp_blockstats"]["reason"])
        self.assertTrue(result["signals"]["host_image_du"]["available"])
        self.assertEqual(result["written_bytes"]["host_image_du"], 1048576)
        self.assertTrue(result["writing"])

    def test_a_missing_ssh_channel_still_returns_the_other_signals(self) -> None:
        api = FakeAPI(
            diskwrite=[0, 0],
            blockstats=[BLOCKSTATS.format(written=1024),
                        BLOCKSTATS.format(written=2048)],
        )
        with mock.patch.object(memflow, "ENABLED", False):
            result = self._measure(api, ground_truth=True)
        self.assertFalse(result["signals"]["host_image_du"]["available"])
        self.assertIn("host SSH channel is off",
                      result["signals"]["host_image_du"]["reason"])
        self.assertEqual(result["written_bytes"]["qmp_blockstats"], 1024)

    def test_neither_extra_signal_available_is_still_not_an_error(self) -> None:
        api = FakeAPI(diskwrite=[0, 0], monitor_error="HTTP 403")
        with mock.patch.object(memflow, "ENABLED", False):
            result = self._measure(api, ground_truth=True)
        self.assertEqual(list(result["written_bytes"]), ["proxmox_diskwrite"])
        self.assertIsNone(result["writing"])

    def test_the_remedy_is_not_the_flag_that_was_already_passed(self) -> None:
        """Observed on the lab node, where the monitor is 403 for the lab
        token and every disk is LVM: the note told the operator to rerun with
        --ground-truth on a run that already had it."""
        api = FakeAPI(diskwrite=[0, 0], monitor_error="HTTP 403")
        with mock.patch.object(memflow, "ENABLED", False):
            asked = self._measure(api, ground_truth=True)
        self.assertNotIn("rerun with --ground-truth", asked["note"])
        self.assertIn("Sys.Audit", asked["note"])
        # ...and the plain run must still name the flag that would help.
        self.assertIn(
            "rerun with --ground-truth",
            self._measure(FakeAPI(diskwrite=[0, 0]))["note"],
        )

    def test_a_counter_that_went_backwards_is_flagged(self) -> None:
        result = self._measure(FakeAPI(diskwrite=[9000, 10]))
        signal = result["signals"]["proxmox_diskwrite"]
        self.assertTrue(signal["counter_reset"])
        self.assertEqual(signal["written_bytes"], 0)

    def test_a_stopped_guest_is_refused_before_the_second_sample(self) -> None:
        api = FakeAPI(status="stopped")
        with self.assertRaisesRegex(LAB.LabError, "not running"):
            self._measure(api)
        self.assertEqual(len(api.calls), 1)

    def test_an_absurd_interval_is_refused(self) -> None:
        with self.assertRaises(LAB.LabError):
            diskactivity.measure(LAB, FakeAPI(), VMID, interval=-1)
        with self.assertRaises(LAB.LabError):
            diskactivity.measure(LAB, FakeAPI(), VMID, interval=99999)


class CommandGuardTests(unittest.TestCase):
    """--ground-truth crosses the monitor endpoint and the host SSH boundary,
    so it is lease-bound exactly like 'console screenshot --via monitor'."""

    def _args(self, **overrides):
        import argparse

        defaults = dict(vmid=VMID, lease=None, ground_truth=False,
                        interval=0.0, timeout=5.0)
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def _lab(self):
        lab = mock.Mock()
        lab.LabError = RuntimeError
        lab.NODE = LAB.NODE
        return lab

    def test_ground_truth_without_a_lease_is_refused_before_any_call(self) -> None:
        lab = self._lab()
        with self.assertRaisesRegex(RuntimeError, "requires --lease"):
            diskactivity.cmd_disk_activity(
                lab, self._args(ground_truth=True)
            )
        lab.ProxmoxAPI.assert_not_called()
        lab.load_lease.assert_not_called()

    def test_a_guest_the_lease_does_not_own_is_refused(self) -> None:
        lab = self._lab()
        lab.require_lease_resource.side_effect = RuntimeError("not registered")
        with self.assertRaisesRegex(RuntimeError, "not registered"):
            diskactivity.cmd_disk_activity(
                lab, self._args(ground_truth=True, lease="20260822-abcd")
            )
        lab.ProxmoxAPI.assert_not_called()

    def test_the_read_only_default_needs_no_lease(self) -> None:
        lab = self._lab()
        lab.ProxmoxAPI.return_value = FakeAPI(diskwrite=[7, 7])
        diskactivity.cmd_disk_activity(lab, self._args())
        lab.require_lease_resource.assert_not_called()
        self.assertEqual(lab.audit.call_args.args[0], "guest-disk-activity")

    def test_the_measurement_is_audited_by_its_numbers(self) -> None:
        lab = self._lab()
        lab.ProxmoxAPI.return_value = FakeAPI(diskwrite=[0, 4096])
        diskactivity.cmd_disk_activity(lab, self._args())
        fields = lab.audit.call_args.kwargs
        self.assertEqual(fields["vmid"], VMID)
        self.assertEqual(fields["proxmox_bytes"], 4096)
        self.assertIsNone(fields["blockstats_bytes"])
        self.assertEqual(fields["disagreements"], 0)


class ReclaimGuardDiskCounterTests(unittest.TestCase):
    """The reclaim guard decides whether somebody's guest keeps running, so
    the misleading counter must not be able to reach that decision -- in
    either direction."""

    def _api(self, tasks: list | None = None, uptime: int = 999_999):
        api = mock.Mock()

        def call(method: str, path: str, data: object = None):
            if path.endswith("/status/current"):
                return {"status": "running", "uptime": uptime}
            if path.endswith("/tasks"):
                return list(tasks or [])
            raise AssertionError(f"unexpected request: {path}")

        api.call.side_effect = call
        return api

    def test_a_zero_disk_counter_cannot_make_a_busy_guest_look_idle(self) -> None:
        activity = LAB.recent_guest_activity(
            self._api(), "qemu", VMID,
            record={"cpu": 0.42, "diskwrite": 0, "mem": 1024, "netin": 8},
        )
        self.assertEqual(activity["signal"], "busy")
        self.assertEqual(activity["disk_written_bytes"], 0)

    def test_a_zero_disk_counter_cannot_veto_a_recent_task(self) -> None:
        import time as _time

        activity = LAB.recent_guest_activity(
            self._api([{"type": "vncproxy",
                        "starttime": int(_time.time()) - 30}]),
            "qemu", VMID,
            record={"cpu": 0.0, "diskwrite": 0},
        )
        self.assertEqual(activity["signal"], "vncproxy")

    def test_the_counter_is_reported_but_never_decisive(self) -> None:
        """A cumulative counter says the guest wrote at some point since boot,
        not that it is writing now, so a large value must not keep an
        abandoned guest alive either."""
        activity = LAB.recent_guest_activity(
            self._api(), "qemu", VMID,
            record={"cpu": 0.001, "diskwrite": 10_000_000_000},
        )
        self.assertIsNone(activity)
        self.assertEqual(
            LAB.guest_load({"cpu": 0.001, "diskwrite": 10_000_000_000})[
                "disk_written_bytes"],
            10_000_000_000,
        )


if __name__ == "__main__":
    unittest.main()
