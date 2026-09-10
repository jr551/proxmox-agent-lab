"""Offline tests for exact-build crash symbolization."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import bootstrap  # noqa: E402,F401

SRC = Path(__file__).parents[1] / "src"
sys.path.insert(0, str(SRC))

from proxmox_agent_lab import crash  # noqa: E402


class CrashTests(unittest.TestCase):
    def make_manifest(self, directory: Path, *, base: str = "0x400000",
                      size: str = "0x1000") -> tuple[Path, Path]:
        binary = directory / "kernel.bin"
        binary.write_bytes(b"exact build bytes")
        manifest = crash.create_manifest(
            {"kernel": str(binary)}, {"kernel": base}, {"kernel": size},
            {}, {"source_revision": "abc123", "reference": "test build", "supplied_by": "test"},
        )
        path = directory / "manifest.json"
        crash._atomic_json(path, manifest)
        return path, binary

    def test_relocated_address_uses_object_relative_input_and_preserves_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest, binary = self.make_manifest(Path(temporary))
            called: dict[str, object] = {}

            def run(argv, **kwargs):
                called["argv"] = argv
                called["input"] = kwargs["input"]
                return subprocess.CompletedProcess(argv, 0, '[{"Symbol": [{"FunctionName": "BugCheck"}]}]', "")

            with mock.patch.object(crash.shutil, "which", return_value="/tools/llvm-symbolizer"), mock.patch.object(crash.subprocess, "run", side_effect=run):
                report = crash.symbolize(str(manifest), [crash.parse_frame("0x400123")], "llvm-symbolizer", 3)

            self.assertEqual(called["input"], "0x123\n")
            self.assertEqual(called["argv"][:5], ["/tools/llvm-symbolizer", "--relative-address", "--obj", str(binary.resolve()), "--output-style=JSON"])
            frame = report["frames"][0]
            self.assertEqual(frame["state"], "symbolized")
            self.assertEqual(frame["runtime_address"], 0x400123)
            self.assertEqual(frame["object_address"], 0x123)
            self.assertEqual(frame["symbolizer_response"]["Symbol"][0]["FunctionName"], "BugCheck")
            self.assertEqual(report["provenance"]["source_revision"], "abc123")

    def test_module_offset_frame_is_mapped_without_guessing_a_load_base(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest, _binary = self.make_manifest(Path(temporary))
            with mock.patch.object(crash.shutil, "which", return_value="/llvm"), mock.patch.object(crash.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "[{}]", "")):
                report = crash.symbolize(str(manifest), [crash.parse_frame("kernel!0x7f")], "llvm-symbolizer", 3)
            frame = report["frames"][0]
            self.assertEqual(frame["runtime_address"], 0x40007F)
            self.assertEqual(frame["object_address"], 0x7F)
            self.assertEqual(frame["state"], "symbolized")

    def test_hash_mismatch_refuses_before_symbolizer_starts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest, binary = self.make_manifest(Path(temporary))
            binary.write_bytes(b"not the recorded build")
            with mock.patch.object(crash.subprocess, "run") as run:
                with self.assertRaisesRegex(crash.CrashError, "SHA-256 mismatch"):
                    crash.symbolize(str(manifest), [crash.parse_frame("0x400001")], "llvm-symbolizer", 3)
            run.assert_not_called()

    def test_unknown_and_overlapping_mappings_are_unresolved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest, binary = self.make_manifest(root)
            document = json.loads(manifest.read_text())
            duplicate = dict(document["modules"][0])
            duplicate["name"] = "overlap"
            duplicate["runtime_base"] = 0x400800
            document["modules"].append(duplicate)
            manifest.write_text(json.dumps(document))
            with mock.patch.object(crash.shutil, "which", return_value="/llvm"), mock.patch.object(crash.subprocess, "run") as run:
                report = crash.symbolize(str(manifest), [crash.parse_frame("0x400900"), crash.parse_frame("missing+0x4"), crash.parse_frame("0x700000")], "llvm-symbolizer", 3)
            self.assertEqual([frame["reason"] for frame in report["frames"]], ["ambiguous_module_mapping", "unknown_module", "address_not_in_manifest"])
            run.assert_not_called()
            self.assertEqual(binary.read_bytes(), b"exact build bytes")

    def test_unavailable_symbolizer_is_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest, _binary = self.make_manifest(Path(temporary))
            with mock.patch.object(crash.shutil, "which", return_value=None):
                with self.assertRaisesRegex(crash.CrashError, "unavailable"):
                    crash.symbolize(str(manifest), [crash.parse_frame("0x400001")], "llvm-symbolizer", 3)

    def test_timeout_and_malformed_json_become_unresolved_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest, _binary = self.make_manifest(Path(temporary))
            with mock.patch.object(crash.shutil, "which", return_value="/llvm"), mock.patch.object(crash.subprocess, "run", side_effect=subprocess.TimeoutExpired(["llvm"], 3)):
                timed_out = crash.symbolize(str(manifest), [crash.parse_frame("0x400001")], "llvm-symbolizer", 3)
            self.assertEqual(timed_out["frames"][0]["reason"], "symbolizer_failure")
            self.assertIn("timed out", timed_out["frames"][0]["detail"])
            with mock.patch.object(crash.shutil, "which", return_value="/llvm"), mock.patch.object(crash.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "not json", "")):
                malformed = crash.symbolize(str(manifest), [crash.parse_frame("0x400001")], "llvm-symbolizer", 3)
            self.assertIn("malformed JSON", malformed["frames"][0]["detail"])

    def test_pdb_hash_is_checked_but_report_does_not_claim_identity_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "program.exe"
            pdb = root / "program.pdb"
            binary.write_bytes(b"pe")
            pdb.write_bytes(b"pdb")
            manifest = crash.create_manifest({"program": str(binary)}, {"program": "0x1000"}, {"program": "0x100"}, {"program": str(pdb)}, {"source_revision": "r", "reference": "x", "supplied_by": "test"})
            path = root / "manifest.json"
            crash._atomic_json(path, manifest)
            with mock.patch.object(crash.shutil, "which", return_value="/llvm"), mock.patch.object(crash.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "[{}]", "")) as run:
                report = crash.symbolize(str(path), [crash.parse_frame("0x1001")], "llvm-symbolizer", 3)
            self.assertIn("--pdb", run.call_args.args[0])
            self.assertTrue(report["verification"][0]["pdb_hash_verified_only"])
            self.assertIn("does not prove", report["limitations"][1])

    @unittest.skipUnless(crash.shutil.which("llvm-symbolizer"), "llvm-symbolizer not installed")
    def test_installed_llvm_symbolizer_accepts_real_elf_invocation(self) -> None:
        # This is deliberately modest: it verifies our stdin/JSON protocol with
        # a real installed tool, without claiming that the interpreter has DWARF.
        executable = Path(sys.executable).resolve()
        manifest = crash.create_manifest({"python": str(executable)}, {"python": "0"}, {"python": str(executable.stat().st_size)}, {}, {"source_revision": "local", "reference": "interpreter", "supplied_by": "test"})
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manifest.json"
            crash._atomic_json(path, manifest)
            report = crash.symbolize(str(path), [crash.parse_frame("0x0")], "llvm-symbolizer", 10)
        self.assertEqual(report["frames"][0]["state"], "symbolized")


if __name__ == "__main__":
    unittest.main()
