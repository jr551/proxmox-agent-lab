"""Tests for the ssh seam: quoting, the argv allowlist, and bounded execution.

Everything runs against an injected ``runner`` -- no real ``ssh``, no network,
no host -- plus the scripted ``FakeSSH`` the layers above the seam use.
"""

from __future__ import annotations

from pathlib import Path
import sys  # noqa: E402

# Shared bootstrap: fixture configuration plus a per-process state directory,
# applied before any proxmox_agent_lab import. `support` sits beside this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import bootstrap  # noqa: E402,F401

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import shlex  # noqa: E402
import subprocess  # noqa: E402
import unittest  # noqa: E402

from proxmox_agent_lab import ssh as ssh_module  # noqa: E402
from proxmox_agent_lab.errors import LabError  # noqa: E402
from support.fakessh import FakeSSH  # noqa: E402


class RecordingRunner:
    """Stands in for ``subprocess.run``: records invocations, answers as scripted.

    Each call lands in ``calls`` as ``(argv, kwargs)`` so a test can assert
    what would have been spawned -- and that nothing was. ``raise_exc``
    simulates the failures ``subprocess.run`` raises itself.
    """

    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
        raise_exc: BaseException | None = None,
    ) -> None:
        self.calls: list[tuple[list[str], dict]] = []
        self._returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._raise_exc = raise_exc

    def __call__(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        self.calls.append((list(argv), dict(kwargs)))
        if self._raise_exc is not None:
            raise self._raise_exc
        return subprocess.CompletedProcess(
            argv, self._returncode, self._stdout, self._stderr
        )


def make_ssh(runner: RecordingRunner) -> ssh_module.SSH:
    return ssh_module.SSH("root@pve", runner=runner)


class BuildArgvTests(unittest.TestCase):
    def test_invocation_shape(self) -> None:
        client = ssh_module.SSH(
            "root@pve",
            ssh_binary="/usr/bin/ssh",
            base_opts=("-o", "BatchMode=yes", "-o", "ConnectTimeout=5"),
        )
        self.assertEqual(
            client.build_argv(["qm", "status", "101"]),
            [
                "/usr/bin/ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=5",
                "root@pve",
                "qm status 101",
            ],
        )

    def test_metacharacters_survive_as_literal_remote_text(self) -> None:
        """ssh flattens argv into one remote shell command; quoting is what
        stops `;`, `$(...)` and newlines from running as shell syntax."""
        argv = [
            "qm",
            "sendkey",
            "101",
            "a; rm -rf /",
            "$(whoami)",
            "`id`",
            "two words",
            "line\nbreak",
        ]
        remote = make_ssh(RecordingRunner()).build_argv(argv)[-1]
        self.assertEqual(shlex.split(remote), argv)
        self.assertIn("'a; rm -rf /'", remote)
        self.assertIn("'$(whoami)'", remote)
        self.assertIn("'two words'", remote)

    def test_unicode_arguments_survive_quoting(self) -> None:
        """shlex.quote wraps unsafe characters in ASCII quotes and leaves the
        argument's own bytes untouched."""
        argv = ["pvesh", "get", "/nodes/pve/status", "--output-format", "json", "héllo wörld"]
        remote = make_ssh(RecordingRunner()).build_argv(argv)[-1]
        self.assertEqual(shlex.split(remote), argv)
        self.assertIn("'héllo wörld'", remote)
        self.assertNotIn("'", shlex.split(remote)[-1])


class CheckAllowedTests(unittest.TestCase):
    """The module-level policy gate the CLI also inspects before building calls."""

    def test_errors_are_operational(self) -> None:
        self.assertTrue(issubclass(ssh_module.PolicyError, LabError))
        self.assertTrue(issubclass(ssh_module.TransportError, LabError))

    def test_empty_argv_is_refused(self) -> None:
        with self.assertRaises(ssh_module.PolicyError):
            ssh_module.check_allowed([])

    def test_host_change_flag_gates_the_subset(self) -> None:
        ssh_module.check_allowed(["shutdown", "-h", "now"], host_change=True)
        with self.assertRaises(ssh_module.PolicyError):
            ssh_module.check_allowed(["shutdown", "-h", "now"])

    def test_cat_is_confined_to_pxl_temp(self) -> None:
        ssh_module.check_allowed(["cat", "/tmp/pxl-shot.ppm"])
        with self.assertRaises(ssh_module.PolicyError):
            ssh_module.check_allowed(["cat", "/etc/shadow"])


class RunPolicyTests(unittest.TestCase):
    def test_refusals_raise_policy_error_and_spawn_nothing(self) -> None:
        cases = {
            "unknown command": ["bash", "-c", "id"],
            "rm": ["rm", "-rf", "/"],
            "shutdown without flag": ["shutdown", "-h", "now"],
            "crontab without flag": ["crontab", "-"],
            "install without flag": [
                "install",
                "-m",
                "0755",
                "/tmp/pxl-gc",
                "/usr/local/sbin/pxl-gc",
            ],
            "cat outside pxl temp": ["cat", "/etc/shadow"],
            "cat mixing pxl and other paths": ["cat", "/tmp/pxl-x", "/etc/shadow"],
            "empty argv": [],
        }
        for name, argv in cases.items():
            with self.subTest(name=name):
                runner = RecordingRunner()
                with self.assertRaises(ssh_module.PolicyError):
                    make_ssh(runner).run(argv)
                self.assertEqual(runner.calls, [])

    def test_cat_pxl_temp_is_allowed(self) -> None:
        runner = RecordingRunner(stdout=b"PPM")
        result = make_ssh(runner).run(["cat", "/tmp/pxl-shot.ppm"])
        self.assertTrue(result.ok)
        self.assertEqual(len(runner.calls), 1)

    def test_host_change_commands_run_with_the_flag(self) -> None:
        for argv in (
            ["shutdown", "-h", "now"],
            ["crontab", "-"],
            ["install", "-m", "0755", "/tmp/pxl-gc", "/usr/local/sbin/pxl-gc"],
        ):
            with self.subTest(argv=argv):
                runner = RecordingRunner()
                self.assertTrue(make_ssh(runner).run(argv, host_change=True).ok)
                self.assertEqual(len(runner.calls), 1)


class RunTransportTests(unittest.TestCase):
    def test_timeout_is_transport_error(self) -> None:
        runner = RecordingRunner(
            raise_exc=subprocess.TimeoutExpired(cmd="ssh", timeout=30.0)
        )
        with self.assertRaises(ssh_module.TransportError):
            make_ssh(runner).run(["qm", "status", "101"])
        self.assertEqual(len(runner.calls), 1)

    def test_spawn_failure_is_transport_error(self) -> None:
        runner = RecordingRunner(raise_exc=FileNotFoundError("no ssh binary"))
        with self.assertRaises(ssh_module.TransportError):
            make_ssh(runner).run(["true"])

    def test_nonzero_remote_exit_is_a_result_not_an_exception(self) -> None:
        runner = RecordingRunner(returncode=3, stdout=b"out", stderr=b"err")
        result = make_ssh(runner).run(["qm", "status", "101"])
        self.assertFalse(result.ok)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout, b"out")
        self.assertEqual(result.stderr, b"err")

    def test_result_captures_argv_as_tuple(self) -> None:
        runner = RecordingRunner()
        argv = ["qm", "status", "101"]
        result = make_ssh(runner).run(argv)
        argv.append("mutated-after-the-fact")
        self.assertEqual(result.argv, ("qm", "status", "101"))
        self.assertEqual(runner.calls[0][0][-1], "qm status 101")

    def test_stdin_is_piped_and_timeout_is_bounded(self) -> None:
        runner = RecordingRunner()
        payload = b"* * * * * /usr/local/sbin/pxl-gc\n"
        make_ssh(runner).run(["crontab", "-"], stdin=payload, host_change=True)
        _, kwargs = runner.calls[0]
        self.assertEqual(kwargs["input"], payload)
        self.assertTrue(kwargs["capture_output"])
        self.assertEqual(kwargs["timeout"], 30.0)  # default_timeout

        make_ssh(runner).run(["qm", "status", "101"], timeout=5)
        self.assertEqual(runner.calls[1][1]["timeout"], 5)


class ProbeTests(unittest.TestCase):
    def test_probe_true_when_true_exits_zero(self) -> None:
        runner = RecordingRunner(returncode=0)
        self.assertTrue(make_ssh(runner).probe())
        self.assertEqual(runner.calls[0][0][-1], "true")

    def test_probe_false_on_remote_failure(self) -> None:
        self.assertFalse(make_ssh(RecordingRunner(returncode=1)).probe())

    def test_probe_false_instead_of_raising(self) -> None:
        timed_out = RecordingRunner(
            raise_exc=subprocess.TimeoutExpired(cmd="ssh", timeout=30.0)
        )
        self.assertFalse(make_ssh(timed_out).probe())
        missing = RecordingRunner(raise_exc=OSError("unreachable"))
        self.assertFalse(make_ssh(missing).probe())


class FakeSSHTests(unittest.TestCase):
    def test_records_every_call(self) -> None:
        fake = FakeSSH()
        fake.run(["qm", "status", "101"], timeout=7, stdin=b"x", host_change=True)
        fake.run(["qm", "start", "101"])
        self.assertEqual(
            fake.calls,
            [
                {
                    "argv": ["qm", "status", "101"],
                    "stdin": b"x",
                    "host_change": True,
                    "timeout": 7,
                },
                {
                    "argv": ["qm", "start", "101"],
                    "stdin": None,
                    "host_change": False,
                    "timeout": None,
                },
            ],
        )

    def test_scripts_match_in_order_and_consume_times(self) -> None:
        fake = FakeSSH()
        fake.add("status", stdout=b"first", times=1)
        fake.add("status", stdout=b"second")
        self.assertEqual(fake.run(["qm", "status", "101"]).stdout, b"first")
        self.assertEqual(fake.run(["qm", "status", "101"]).stdout, b"second")
        self.assertEqual(fake.run(["qm", "status", "101"]).stdout, b"second")

    def test_unmatched_call_reports_no_rule(self) -> None:
        fake = FakeSSH()
        result = fake.run(["qm", "destroy", "101"])
        self.assertFalse(result.ok)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, b"fake: no rule")
        self.assertIsInstance(result, ssh_module.CommandResult)

    def test_probe_follows_the_true_script(self) -> None:
        self.assertFalse(FakeSSH().probe())
        fake = FakeSSH()
        fake.add("true", returncode=0)
        self.assertTrue(fake.probe())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
