"""Read-only storage status over the proxmox seam."""

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
from proxmox_agent_lab import proxmox as proxmox_module  # noqa: E402
from proxmox_agent_lab import storage as storage_module  # noqa: E402
from support.fakessh import FakeSSH  # noqa: E402


class StorageStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeSSH()
        self.prox = proxmox_module.Proxmox(self.fake, "pve", sleep=lambda _s: None)
        patcher = mock.patch.object(
            storage_module.guest_module, "_make_proxmox", lambda config: self.prox
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.lab = mock.Mock()
        self.lab.CONFIG = config_module.get()

    def test_status_reports_free_space_and_does_not_mutate(self) -> None:
        self.fake.add(
            r"^pvesh get /nodes/pve/storage",
            stdout=(
                b'[{"storage":"local-lvm","type":"lvmthin","active":1,'
                b'"enabled":1,"used":10,"avail":90,"total":100,'
                b'"content":"images,rootdir"},'
                b'{"storage":"secret-store","type":"dir","password":"nope",'
                b'"active":1,"used":1,"avail":2,"total":3,"content":"iso"}]'
            ),
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            payload = storage_module.cmd_status(self.lab, mock.Mock())
        self.assertEqual(json.loads(out.getvalue()), payload)
        self.assertEqual(payload["node"], "pve")
        self.assertEqual(payload["storage"][0]["avail"], 90)
        self.assertNotIn("password", payload["storage"][1])
        self.assertEqual(
            [call["argv"][0] for call in self.fake.calls], ["pvesh"]
        )
        self.assertNotIn("set", self.fake.calls[0]["argv"])


if __name__ == "__main__":
    unittest.main()
