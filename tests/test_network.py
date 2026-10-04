"""Read-only bridge list over the proxmox seam."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import bootstrap  # noqa: E402,F401

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from proxmox_agent_lab import config as config_module  # noqa: E402
from proxmox_agent_lab import network as network_module  # noqa: E402
from proxmox_agent_lab import proxmox as proxmox_module  # noqa: E402
from support.fakessh import FakeSSH  # noqa: E402


class NetworkBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeSSH()
        self.prox = proxmox_module.Proxmox(self.fake, "pve", sleep=lambda _s: None)
        patcher = mock.patch.object(
            network_module.guest_module, "_make_proxmox", lambda config: self.prox
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.lab = mock.Mock()
        self.lab.CONFIG = config_module.get()

    def test_bridges_keeps_vmbr_and_drops_other_ifaces(self) -> None:
        self.fake.add(
            r"^pvesh get /nodes/pve/network",
            stdout=(
                b'[{"iface":"vmbr0","type":"bridge","active":1,'
                b'"address":"10.0.0.1","bridge_ports":"eno1"},'
                b'{"iface":"eno1","type":"eth","active":1},'
                b'{"iface":"vmbr1","type":"OVSBridge","active":1}]'
            ),
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            payload = network_module.cmd_bridges(self.lab, mock.Mock())
        self.assertEqual(json.loads(out.getvalue()), payload)
        self.assertEqual(
            [row["iface"] for row in payload["bridges"]], ["vmbr0", "vmbr1"]
        )
        self.assertEqual(payload["bridges"][0]["address"], "10.0.0.1")
        argv = self.fake.calls[0]["argv"]
        self.assertEqual(argv[0], "pvesh")
        self.assertIn("any_bridge", argv)
        self.assertNotIn("set", argv)


if __name__ == "__main__":
    unittest.main()
