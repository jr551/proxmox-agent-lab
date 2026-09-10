"""Offline tests for the virtio driver-porting diagnostics."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

SRC = Path(__file__).parents[1] / "src"
sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import bootstrap  # noqa: E402,F401

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


QUEUE_PATH = "/machine/peripheral/virtio-net-0"


def _queue_raw(*, used: int, avail: int = 10, inuse: int = 2) -> str:
    return (
        f"{QUEUE_PATH}:\n"
        "  device_name:          virtio-net\n"
        "  queue_index:          0\n"
        f"  inuse:                {inuse}\n"
        f"  used_idx:             {used}\n"
        "  signalled_used:       9\n"
        "  signalled_used_valid: true\n"
        f"  last_avail_idx:       {avail}\n"
        "  VRing:\n"
        "    num:          256\n"
        "    num_default:  256\n"
        "    align:        4096\n"
        "    desc:         0x0000000012340000\n"
        "    avail:        0x0000000012350000\n"
        "    used:         0x0000000012360000\n"
        "  future_field:         retained\n"
    )


class QueueTests(unittest.TestCase):
    def _api(self, responses: list[str]) -> mock.Mock:
        api = mock.Mock()
        iterator = iter(responses)

        def call(method: str, path: str, data: dict | None = None):
            if path.endswith("/status/current"):
                return {"status": "running"}
            if path.endswith("/monitor"):
                command = (data or {}).get("command", "")
                if command.startswith("info virtio-queue-status"):
                    return next(iterator)
                if command.startswith("info virtio-queue-element"):
                    return ("/machine/peripheral/virtio-net-0:\n"
                            "  device_name: virtio-net\n  index: 2\n"
                            "  desc:\n    descs:\n"
                            "        addr 0xff66000 len 1518 (write),\n"
                            "  avail:\n    flags: 1\n    idx: 4\n    ring: 0\n"
                            "  used:\n    flags: 1\n    idx: 3\n")
            raise AssertionError(f"unexpected API call {method} {path} {data}")

        api.call.side_effect = call
        return api

    def test_parser_retains_raw_unknown_fields_and_known_vring_fields(self) -> None:
        parsed = virtio.parse_queue_status(_queue_raw(used=4))
        self.assertTrue(parsed["available"])
        self.assertEqual(parsed["fields"]["used_idx"], 4)
        self.assertEqual(parsed["fields"]["vring_desc"], 0x12340000)
        self.assertEqual(parsed["unknown_fields"][0]["key"], "vring_future_field")

    def test_split_wrap_is_one_and_reset_is_unavailable(self) -> None:
        old = {"parsed": virtio.parse_queue_status(_queue_raw(used=65535))}
        new = {"parsed": virtio.parse_queue_status(_queue_raw(used=0))}
        delta = virtio._queue_deltas([old, new], "split")[0]
        self.assertEqual(delta["used_idx"], 1)
        self.assertTrue(delta["fields"]["used_idx"]["wrapped"])
        reset = virtio._counter_delta(100, 2, "split")
        self.assertIsNone(reset["delta"])
        self.assertTrue(reset["reset"])

    def test_packed_does_not_use_split_counter_arithmetic(self) -> None:
        old = {"parsed": virtio.parse_queue_status(_queue_raw(used=10))}
        new = {"parsed": virtio.parse_queue_status(_queue_raw(used=11))}
        delta = virtio._queue_deltas([old, new], "packed")[0]
        self.assertNotIn("used_idx", delta)
        self.assertIsNone(delta["fields"]["used_idx"]["delta"])
        self.assertEqual(delta["fields"]["used_idx"]["available"], False)

    def test_sampling_uses_exact_status_queries_and_reports_candidate(self) -> None:
        api = self._api([_queue_raw(used=10), _queue_raw(used=10)])
        lab = _Lab(api)
        result = virtio.sample_queues(
            lab, api, 9001, path=QUEUE_PATH, queue=0, samples=2,
            interval=0, deadline=5, ring_format="split",
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["interpretation"]["stalled_candidate"], True)
        self.assertEqual(result["interpretation"]["status"], "candidate")
        self.assertEqual(result["snapshots"][0]["raw"], _queue_raw(used=10))
        calls = [c for c in api.call.call_args_list if c.args[0] == "POST"]
        self.assertEqual(
            [c.args[2]["command"] for c in calls],
            [f"info virtio-queue-status {QUEUE_PATH} 0"] * 2,
        )

    def test_unsupported_status_is_explicitly_unavailable(self) -> None:
        api = mock.Mock()
        api.call.side_effect = lambda method, path, data=None: (
            {"status": "running"} if path.endswith("/status/current")
            else (_ for _ in ()).throw(RuntimeError("HMP command unavailable"))
        )
        result = virtio.sample_queues(
            _Lab(api), api, 9001, path=QUEUE_PATH, queue=0,
            samples=2, interval=0, deadline=5, ring_format="split",
        )
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["interpretation"]["stalled_candidate"])

    def test_path_injection_is_rejected_before_any_api_call(self) -> None:
        api = mock.Mock()
        lab = _Lab(api)
        with self.assertRaisesRegex(RuntimeError, "absolute virtio"):
            virtio.sample_queues(
                lab, api, 9001,
                path=f"{QUEUE_PATH}; system_powerdown", queue=0,
            )
        api.call.assert_not_called()

    def test_optional_element_uses_bounded_read_only_command(self) -> None:
        api = self._api([_queue_raw(used=10)])
        result = virtio.sample_queues(
            _Lab(api), api, 9001, path=QUEUE_PATH, queue=0, samples=1,
            interval=0, deadline=5, ring_format="split", element_index=2,
        )
        self.assertEqual(result["element"]["status"], "ok")
        element_fields = result["element"]["parsed"]["fields"]
        self.assertEqual(element_fields["avail_idx"], 4)
        self.assertEqual(element_fields["used_idx"], 3)
        self.assertEqual(element_fields["index"], 2)
        self.assertIn("addr 0xff66000 len 1518 (write),",
                      result["element"]["parsed"]["unknown_lines"][0])
        self.assertIn(
            f"info virtio-queue-element {QUEUE_PATH} 0 2",
            [c.args[2]["command"] for c in api.call.call_args_list if c.args[0] == "POST"],
        )


if __name__ == "__main__":
    unittest.main()
