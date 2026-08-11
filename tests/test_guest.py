"""Offline tests for guest template/clone and detached-run commands."""

from __future__ import annotations

import argparse
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SRC = Path(__file__).parents[1] / "src"
import sys

sys.path.insert(0, str(SRC))

from proxmox_agent_lab import console as lab_console  # noqa: E402
from proxmox_agent_lab import guest as lab_guest  # noqa: E402


def _lab(tmp: str) -> mock.Mock:
    lab = mock.Mock()
    lab.LabError = RuntimeError
    lab.NODE = "aipve"
    lab.STATE_ROOT = str(Path(tmp) / "state")
    lab.iso_now = lambda: "2026-08-11T00:00:00Z"
    lab.load_lease.return_value = {
        "resources": [{"kind": "qemu", "vmid": 7, "policy": "delete",
                       "name": "builder"}],
    }
    lab.controller_lock = mock.MagicMock()
    return lab


def _args(lab: mock.Mock, *argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    lab_guest.register(parser.add_subparsers(), lab)
    return parser.parse_args(list(argv))


class GuestTemplateTests(unittest.TestCase):
    def test_template_requires_lease_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lab = _lab(tmp)
            lab.load_lease.return_value = {"resources": []}
            with self.assertRaises(RuntimeError) as caught:
                lab_guest.cmd_template(lab, _args(lab, "guest", "template",
                                                  "--lease", "L1",
                                                  "--vmid", "7"))
            self.assertIn("not a qemu guest registered", str(caught.exception))

    def test_template_refuses_running_guest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lab = _lab(tmp)
            api = mock.Mock()
            api.call.return_value = {"status": "running"}
            lab.ProxmoxAPI.return_value = api
            with self.assertRaises(RuntimeError) as caught:
                lab_guest.cmd_template(lab, _args(lab, "guest", "template",
                                                  "--lease", "L1",
                                                  "--vmid", "7"))
            self.assertIn("must be stopped", str(caught.exception))

    def test_template_converts_stopped_guest_and_audits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lab = _lab(tmp)
            api = mock.Mock()
            api.call.return_value = {"status": "stopped"}
            lab.ProxmoxAPI.return_value = api
            lab_guest.cmd_template(lab, _args(lab, "guest", "template",
                                              "--lease", "L1",
                                              "--vmid", "7"))
            api.call.assert_any_call(
                "POST", "/nodes/aipve/qemu/7/template"
            )
            lab.audit.assert_called_once_with(
                "guest-template", lease="L1", kind="qemu", vmid=7, sync=False
            )

    def test_clone_registers_new_guest_under_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lab = _lab(tmp)
            api = mock.Mock()
            api.call.return_value = {}
            lab.ProxmoxAPI.return_value = api
            lab_guest.cmd_clone(lab, _args(lab, "guest", "clone",
                                           "--lease", "L1",
                                           "--template", "7",
                                           "--newid", "8",
                                           "--name", "builder-copy"))
            api.call.assert_any_call(
                "POST", "/nodes/aipve/qemu/7/clone",
                {"newid": 8, "name": "builder-copy"},
            )
            lab.register_resource.assert_called_once()
            kind, vmid = lab.register_resource.call_args.args[1], \
                lab.register_resource.call_args.args[2]
            self.assertEqual((kind, vmid), ("qemu", 8))
            lab.audit.assert_called_once_with(
                "guest-clone", lease="L1", kind="qemu", template=7, vmid=8,
                sync=False,
            )


class DetachedRunTests(unittest.TestCase):
    def _record(self, tmp: str) -> str:
        run_dir = Path(tmp) / "state" / "guest-runs"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "vm7-12345.json").write_text(
            '{"vmid": 7, "pid": "12345", "log": "/tmp/grun-abcd.log", '
            '"command": "make -j2", "started_at": "2026-08-11T00:00:00Z"}'
        )
        return "/tmp/grun-abcd.log"

    def test_run_detach_starts_and_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lab = _lab(tmp)
            with mock.patch.object(
                lab_console, "agent_exec",
                return_value={"exitcode": 0, "stdout": "4242\n",
                              "stderr": ""},
            ) as execute:
                lab_guest.cmd_run(lab, _args(lab, "guest", "run",
                                             "--lease", "L1",
                                             "--vmid", "7",
                                             "--detach", "make", "world"))
            script = execute.call_args.args[3][2]
            self.assertIn("nohup sh -c", script)
            self.assertIn("grun-exit:$?", script)
            self.assertIn("make world", script)
            record = Path(tmp) / "state" / "guest-runs" / "vm7-4242.json"
            self.assertTrue(record.is_file())
            lab.audit.assert_called_once_with(
                "guest-run-detached", lease="L1", vmid=7, pid="4242",
                sync=False,
            )

    def test_log_reads_tail_and_stops_on_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lab = _lab(tmp)
            log = self._record(tmp)

            def fake_exec(lab, api, vmid, command, timeout=30):
                script = command[2]
                if "tail -c" in script:
                    return {"exitcode": 0, "stdout": "line one\nline two\n",
                            "stderr": ""}
                if "kill -0" in script:
                    return {"exitcode": 0, "stdout": "1", "stderr": ""}
                return {"exitcode": 0, "stdout": "", "stderr": ""}

            with mock.patch.object(lab_console, "agent_exec",
                                   side_effect=fake_exec):
                lab_guest.cmd_log(lab, _args(lab, "guest", "log",
                                             "--lease", "L1",
                                             "--vmid", "7",
                                             "--pid", "12345"))
            self.assertEqual(log, "/tmp/grun-abcd.log")

    def test_wait_reports_exit_code_from_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lab = _lab(tmp)
            self._record(tmp)

            def fake_exec(lab, api, vmid, command, timeout=30):
                script = command[2]
                if "tail -c 65536" in script:
                    return {"exitcode": 0,
                            "stdout": "build ok\ngrun-exit:0\n", "stderr": ""}
                if "kill -0" in script:
                    return {"exitcode": 0, "stdout": "1", "stderr": ""}
                return {"exitcode": 0, "stdout": "", "stderr": ""}

            with mock.patch.object(lab_console, "agent_exec",
                                   side_effect=fake_exec):
                lab_guest.cmd_wait(lab, _args(lab, "guest", "wait",
                                              "--lease", "L1",
                                              "--vmid", "7",
                                              "--pid", "12345",
                                              "--timeout", "10"))

    def test_log_uses_byte_marker_for_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lab = _lab(tmp)
            self._record(tmp)

            def fake_exec(lab, api, vmid, command, timeout=30):
                script = command[2]
                if "out=$(tail" in script:
                    return {"exitcode": 0,
                            "stdout": "data\n__logb1234__:5\n\n",
                            "stderr": ""}
                if "kill -0" in script:
                    return {"exitcode": 0, "stdout": "1", "stderr": ""}
                return {"exitcode": 0, "stdout": "", "stderr": ""}

            with mock.patch.object(lab_console, "agent_exec",
                                   side_effect=fake_exec):
                lab_guest.cmd_log(lab, _args(lab, "guest", "log",
                                             "--lease", "L1",
                                             "--vmid", "7",
                                             "--pid", "12345"))


if __name__ == "__main__":
    unittest.main()
