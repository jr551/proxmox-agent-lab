"""Host hardware inspection parses real hwmon/net sysfs output."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import bootstrap  # noqa: E402,F401
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from proxmox_agent_lab import hostinfo  # noqa: E402


class HostInfoTests(unittest.TestCase):
    def _lab(self):
        lab = mock.Mock()
        lab.LabError = RuntimeError
        return lab

    def test_sensors_parses_hwmon_and_thermal(self):
        lab = self._lab()
        proc = mock.Mock(returncode=0, stderr="", stdout=(
            "nvme\ttemp1_input\t60850\n"
            "k10temp\ttemp1_input\t87000\n"
            "amdgpu\ttemp1_input\t87000\n"
            "thermal\tiwlwifi_1\t\n"  # empty value skipped
        ))
        with mock.patch.object(hostinfo.host_transport, "host_run",
                               return_value=proc):
            import io, contextlib
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                hostinfo.cmd_sensors(lab, mock.Mock())
        data = json.loads(out.getvalue())
        self.assertEqual(len(data["sensors"]), 3)
        self.assertEqual(data["sensors"][1]["chip"], "k10temp")
        self.assertEqual(data["sensors"][1]["degree_c"], 87.0)

    def test_sensors_empty_output_is_empty_list(self):
        lab = self._lab()
        proc = mock.Mock(returncode=0, stderr="", stdout="")
        with mock.patch.object(hostinfo.host_transport, "host_run",
                               return_value=proc):
            import io, contextlib
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                hostinfo.cmd_sensors(lab, mock.Mock())
        self.assertEqual(json.loads(out.getvalue())["sensors"], [])

    def test_sensors_nonzero_exit_raises(self):
        lab = self._lab()
        proc = mock.Mock(returncode=1, stderr="boom", stdout="")
        with mock.patch.object(hostinfo.host_transport, "host_run",
                               return_value=proc):
            with self.assertRaises(RuntimeError):
                hostinfo.cmd_sensors(lab, mock.Mock())

    def test_macs_marks_wireless_and_skips_lo(self):
        lab = self._lab()
        proc = mock.Mock(returncode=0, stderr="", stdout=(
            "eth0\taa:bb:cc:dd:ee:01\tno\tup\n"
            "wlan0\taa:bb:cc:dd:ee:02\tyes\tdown\n"
            "vmbr0\taa:bb:cc:dd:ee:01\tno\tup\n"
        ))
        with mock.patch.object(hostinfo.host_transport, "host_run",
                               return_value=proc):
            import io, contextlib
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                hostinfo.cmd_macs(lab, mock.Mock())
        data = json.loads(out.getvalue())
        self.assertEqual(len(data["interfaces"]), 3)
        wifi = [i for i in data["interfaces"] if i["wireless"]]
        self.assertEqual(wifi[0]["mac"], "aa:bb:cc:dd:ee:02")
        self.assertEqual(wifi[0]["interface"], "wlan0")


if __name__ == "__main__":
    unittest.main()
