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



class GuestRunArgvTests(unittest.TestCase):
    def test_agent_runs_exact_argv_without_a_shell_reparse(self) -> None:
        lab = mock.Mock()
        api = mock.Mock()
        session = lab_guest.GuestSession(
            lab, api, 7,
            capabilities=lab_guest.GuestCapabilities(7, "qemu", agent=True),
        )
        command = [
            "bash", "-lc", "ls -l /tmp/t1 /tmp/t2 2>&1\nprintf '%s' \"two words\"",
        ]
        with mock.patch.object(
            lab_console, "agent_exec",
            return_value={"exitcode": 0, "stdout": "", "stderr": ""},
        ) as execute:
            result = session.run_argv(command, timeout=120)

        execute.assert_called_once_with(lab, api, 7, command, timeout=120)
        self.assertTrue(result.ok)

    def test_cmd_run_passes_parser_argv_to_the_guest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lab = _lab(tmp)
            command = ["bash", "-lc", "printf '%s' 'two words'\n"]
            with mock.patch.object(lab_guest, "GuestSession") as session_class:
                session = session_class.return_value.__enter__.return_value
                session.run_argv.return_value = lab_guest.CommandResult(
                    stdout="", stderr="", exit_code=0, channel="agent",
                )
                lab_guest.cmd_run(
                    lab,
                    _args(lab, "guest", "run", "--lease", "L1",
                          "--vmid", "7", "--", *command),
                )

            session.run_argv.assert_called_once_with(command, timeout=300)


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
                lab_guest.cmd_run(lab, _args(
                    lab, "guest", "run", "--lease", "L1", "--vmid", "7",
                    "--detach", "--", "bash", "-lc",
                    "printf '%s' 'two words'\n",
                ))
            agent_command = execute.call_args.args[3]
            self.assertIn('nohup "$@"', agent_command[2])
            self.assertIn("grun-exit:$?", agent_command[2])
            self.assertEqual(
                agent_command[3:],
                ["guest-run", "bash", "-lc", "printf '%s' 'two words'\n"],
            )
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

    def test_snapshot_create_lists_and_rollback(self) -> None:
        import json as _json

        with tempfile.TemporaryDirectory() as tmp:
            lab = _lab(tmp)
            api = mock.Mock()
            lab.ProxmoxAPI.return_value = api
            lab.wait_task = mock.Mock()

            # create
            api.call.return_value = "UPID:aipve:00000001:00000001:1:qmsnapshot:7:"
            lab_guest.cmd_snapshot(lab, _args(lab, "guest", "snapshot",
                                             "--lease", "L1", "--vmid", "7",
                                             "--mode", "create",
                                             "--name", "before-kernel"))
            api.call.assert_any_call(
                "POST", "/nodes/aipve/qemu/7/snapshot",
                {"snapname": "before-kernel", "description": ""},
            )
            lab.wait_task.assert_called_once()

            # rollback requires stopped
            api.call.side_effect = None
            api.call.return_value = {"status": "running"}
            with self.assertRaises(RuntimeError) as caught:
                lab_guest.cmd_snapshot(lab, _args(lab, "guest", "snapshot",
                                                 "--lease", "L1", "--vmid", "7",
                                                 "--mode", "rollback",
                                                 "--name", "before-kernel"))
            self.assertIn("must be stopped", str(caught.exception))

            # rollback on stopped guest posts to the right path
            api.call.side_effect = None
            api.call.return_value = "UPID:aipve:00000001:00000002:1:qmsnapshot:7:"
            status_calls = {"count": 0}
            def rollback_call(method, path, data=None):
                if path.endswith("/status/current"):
                    return {"status": "stopped"}
                return "UPID:aipve:00000001:00000003:1:qmsnapshot:7:"
            api.call.side_effect = rollback_call
            lab.wait_task.reset_mock()
            lab_guest.cmd_snapshot(lab, _args(lab, "guest", "snapshot",
                                             "--lease", "L1", "--vmid", "7",
                                             "--mode", "rollback",
                                             "--name", "before-kernel"))
            api.call.assert_any_call(
                "POST", "/nodes/aipve/qemu/7/snapshot/before-kernel/rollback"
            )
            lab.audit.assert_any_call(
                "guest-snapshot-rollback", lease="L1", kind="qemu", vmid=7,
                name="before-kernel", sync=False,
            )


if __name__ == "__main__":
    unittest.main()
