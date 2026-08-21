"""Offline tests for the virtio driver-porting diagnostics."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

SRC = Path(__file__).parents[1] / "src"
sys.path.insert(0, str(SRC))

from proxmox_agent_lab import virtio  # noqa: E402


class _Lab:
    LabError = RuntimeError
    NODE = "aipve"

    def __init__(self, api: mock.Mock) -> None:
        self._api = api
        self.audits: list[tuple[str, dict]] = []

    def ProxmoxAPI(self) -> mock.Mock:
        return self._api

    def load_lease(self, lease_id: str) -> dict:
        return {"id": lease_id}

    def audit(self, event: str, *, sync: bool = True, **fields: object) -> None:
        self.audits.append((event, fields))


def _args(lab: _Lab, *argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    virtio.register(parser.add_subparsers(), lab)
    return parser.parse_args(list(argv))


class DecodeFeatureTests(unittest.TestCase):
    def test_version_1_and_indirect_desc_are_named(self) -> None:
        # bits 28 and 32 set.
        value = (1 << 28) | (1 << 32)
        names = {b["name"] for b in virtio.decode_features(value) if b["set"]}
        self.assertIn("VIRTIO_RING_F_INDIRECT_DESC", names)
        self.assertIn("VIRTIO_F_VERSION_1", names)

    def test_device_specific_bits_use_the_device_table(self) -> None:
        # virtio-net MQ is bit 22.
        value = 1 << 22
        net = {b["bit"]: b for b in virtio.decode_features(value, "net")}
        self.assertTrue(net[22]["set"])
        self.assertEqual(net[22]["name"], "VIRTIO_NET_F_MQ")
        # The same bit means something different for blk (MQ is bit 12 there),
        # so bit 22 for blk is unnamed but still reported as set.
        blk = {b["bit"]: b for b in virtio.decode_features(value, "blk")}
        self.assertTrue(blk[22]["set"])
        self.assertIn("unknown", blk[22]["name"])

    def test_unset_known_bits_are_still_listed_for_porting(self) -> None:
        # A driver author wants to see the whole menu, set or not.
        bits = virtio.decode_features(0, "blk")
        flush = [b for b in bits if b["name"] == "VIRTIO_BLK_F_FLUSH"]
        self.assertEqual(len(flush), 1)
        self.assertFalse(flush[0]["set"])

    def test_decode_command_lists_set_feature_names(self) -> None:
        lab = _Lab(mock.Mock())
        args = _args(lab, "virtio", "decode", "--value", "0x110000000",
                     "--device", "net")
        with mock.patch("builtins.print") as printed:
            virtio.cmd_decode(lab, args)
        payload = json.loads(printed.call_args[0][0])
        self.assertEqual(payload["value"], "0x110000000")
        self.assertIn("VIRTIO_F_VERSION_1", payload["set_feature_names"])

    def test_decode_rejects_non_integer(self) -> None:
        lab = _Lab(mock.Mock())
        args = _args(lab, "virtio", "decode", "--value", "banana")
        with self.assertRaisesRegex(RuntimeError, "must be an integer"):
            virtio.cmd_decode(lab, args)


class MonitorAllowlistTests(unittest.TestCase):
    def test_mutating_monitor_command_is_refused(self) -> None:
        lab = _Lab(mock.Mock())
        with self.assertRaisesRegex(RuntimeError, "read-only"):
            virtio._monitor(lab, lab._api, 9001, "system_powerdown")
        lab._api.call.assert_not_called()

    def test_non_info_query_is_refused(self) -> None:
        lab = _Lab(mock.Mock())
        with self.assertRaisesRegex(RuntimeError, "read-only"):
            virtio._monitor(lab, lab._api, 9001, "info registers")
        lab._api.call.assert_not_called()

    def test_allowlisted_info_is_sent(self) -> None:
        api = mock.Mock()
        api.call.return_value = "device list"
        lab = _Lab(api)
        out = virtio._monitor(lab, api, 9001, "info virtio")
        self.assertEqual(out, "device list")
        api.call.assert_called_once_with(
            "POST", "/nodes/aipve/qemu/9001/monitor",
            {"command": "info virtio"},
        )


class InspectTests(unittest.TestCase):
    def _running_api(self) -> mock.Mock:
        api = mock.Mock()

        def call(method: str, path: str, data: dict | None = None):
            if path.endswith("/status/current"):
                return {"status": "running"}
            if path.endswith("/config"):
                return {
                    "virtio0": "local-lvm:vm-9001-disk-0,size=8G",
                    "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0",
                    "scsihw": "virtio-scsi-pci",
                }
            if path.endswith("/monitor"):
                command = (data or {}).get("command", "")
                if command == "info virtio":
                    return "/machine/peripheral/virtio-net-0 [virtio-net]"
                if command.startswith("info virtio-status"):
                    return "guest features: 0x110000000\nhost features: 0x1"
            return None

        api.call.side_effect = call
        return api

    def test_inspect_reports_configured_and_live_devices(self) -> None:
        lab = _Lab(self._running_api())
        args = _args(lab, "virtio", "inspect", "--vmid", "9001", "--lease", "L1")
        with mock.patch("builtins.print") as printed:
            virtio.cmd_inspect(lab, args)
        payload = json.loads(printed.call_args[0][0])
        kinds = {d["kind"] for d in payload["configured_devices"]}
        self.assertEqual(kinds, {"virtio-blk", "virtio-net", "virtio-scsi"})
        self.assertEqual(len(payload["live_devices"]), 1)
        device = payload["live_devices"][0]
        self.assertEqual(device["device_type"], "net")
        # 0x110000000 has VERSION_1 (bit 32) set; decoded against the net table.
        names = {
            b["name"]
            for decoded in device["decoded_features"]
            for b in decoded["features"] if b["set"]
        }
        self.assertIn("VIRTIO_F_VERSION_1", names)
        self.assertEqual(lab.audits[0][0], "virtio-inspect")

    def test_inspect_refuses_a_stopped_guest(self) -> None:
        api = mock.Mock()
        api.call.return_value = {"status": "stopped"}
        lab = _Lab(api)
        args = _args(lab, "virtio", "inspect", "--vmid", "9001")
        with self.assertRaisesRegex(RuntimeError, "not a running"):
            virtio.cmd_inspect(lab, args)


if __name__ == "__main__":
    unittest.main()
