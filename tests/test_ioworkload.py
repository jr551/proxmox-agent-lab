"""Offline tests for portable scratch-file I/O evidence and replay."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import bootstrap  # noqa: E402,F401
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from proxmox_agent_lab import ioworkload


class IoWorkloadTests(unittest.TestCase):
    def record(self, root: Path) -> tuple[Path, Path]:
        trace, scratch = root / "case.trace", root / "case.io-scratch"
        result = ioworkload.record_workload(
            trace, scratch, seed=71, file_size=16 * 1024, operations=17,
            block_size=1024,
        )
        self.assertEqual(result["status"], "recorded")
        return trace, scratch

    def test_record_analyze_and_replay_new_regular_scratch_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            trace, original = self.record(root)
            evidence = trace.read_bytes()
            analysis = ioworkload.analyze_trace(trace)
            self.assertTrue(analysis["valid"])
            self.assertGreater(analysis["operations"]["write"], 0)
            self.assertGreater(analysis["operations"]["read"], 0)
            self.assertGreater(analysis["operations"]["flush"], 0)

            replay_scratch, report = root / "replay.io-scratch", root / "replay.result"
            replay = ioworkload.replay_trace(
                trace, replay_scratch, report, create_scratch=True,
                confirm_disposable=True,
            )
            self.assertEqual(replay["status"], "ok")
            self.assertTrue(replay_scratch.is_file())
            self.assertEqual(trace.read_bytes(), evidence)
            report_data = json.loads(report.read_text())
            self.assertEqual(report_data["status"], "ok")
            self.assertEqual(original.stat().st_size, replay_scratch.stat().st_size)

    def test_recorded_operation_failure_is_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            trace, scratch = root / "failure.trace", root / "failure.io-scratch"
            with mock.patch.object(ioworkload, "_run_operation", side_effect=OSError("injected failure")):
                result = ioworkload.record_workload(
                    trace, scratch, seed=7, file_size=4096, operations=3,
                    block_size=512,
                )
            self.assertEqual(result["status"], "failed")
            analysis = ioworkload.analyze_trace(trace)
            self.assertEqual(analysis["recorded_failures"], 1)
            self.assertEqual(analysis["intent_count"], 1)

    def test_replay_checksum_failure_writes_separate_failure_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            trace, _ = self.record(root)
            lines = [json.loads(line) for line in trace.read_text().splitlines()]
            read_outcome = next(
                line for index, line in enumerate(lines)
                if line["record"] == "outcome" and lines[index - 1]["operation"] == "read"
            )
            read_outcome["checksum"] = "f" * 64
            trace.write_text("".join(json.dumps(line) + "\n" for line in lines))
            report = root / "failed-replay.result"
            replay = ioworkload.replay_trace(
                trace, root / "replay.io-scratch", report, create_scratch=True,
                confirm_disposable=True,
            )
            self.assertEqual(replay["status"], "failed")
            self.assertIn("checksum differs", replay["error"])
            self.assertEqual(json.loads(report.read_text())["status"], "failed")

    def test_malformed_trace_and_unsafe_targets_are_refused_before_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            trace = root / "bad.trace"
            trace.write_text(json.dumps({
                "record": "header", "format": ioworkload.TRACE_FORMAT,
                "version": 1, "mode": "plan",
                "initial_state": {"size": 1, "sha256": "0" * 64},
                "workload": {"seed": 1, "file_size": 1, "operations": 1, "block_size": 1},
            }) + "\n" + json.dumps({"record": "erase", "sequence": 0}) + "\n")
            with self.assertRaisesRegex(ioworkload.TraceError, "unknown"):
                ioworkload.analyze_trace(trace)
            with self.assertRaisesRegex(ioworkload.TraceError, "absolute"):
                ioworkload.generate_trace("../trace", seed=1, file_size=1,
                                          operations=1, block_size=1)

    def test_replay_needs_confirmation_and_matching_initial_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            trace, _ = self.record(root)
            scratch = root / "not-baseline.io-scratch"
            scratch.write_bytes(b"user data")
            report = root / "unused.result"
            with self.assertRaisesRegex(ioworkload.TraceError, "confirm-disposable"):
                ioworkload.replay_trace(trace, scratch, report, create_scratch=False,
                                        confirm_disposable=False)
            with self.assertRaisesRegex(ioworkload.TraceError, "initial state"):
                ioworkload.replay_trace(trace, scratch, report, create_scratch=False,
                                        confirm_disposable=True)
            self.assertFalse(report.exists())

    def test_plan_is_replayable_without_outcome_records(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            trace = root / "plan.trace"
            ioworkload.generate_trace(trace, seed=1, file_size=4096,
                                      operations=12, block_size=512)
            self.assertEqual(ioworkload.analyze_trace(trace)["mode"], "plan")
            result = ioworkload.replay_trace(
                trace, root / "plan.io-scratch", root / "plan.result",
                create_scratch=True, confirm_disposable=True,
            )
            self.assertEqual(result["status"], "ok")
