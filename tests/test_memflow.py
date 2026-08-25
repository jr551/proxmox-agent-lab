"""memflow introspection reaches the host over a separate, opt-in SSH channel,
so each guard that keeps it opt-in, lease-bound and host-change-gated gets a
test that fails if the guard is removed."""

from __future__ import annotations

import os
from pathlib import Path

os.environ["PROXMOX_AGENT_LAB_CONFIG"] = str(
    Path(__file__).parent / "fixtures" / "config.toml"
)

import shutil
import tempfile
# ...and at a disposable state directory: a test must never write into the
# developer's real controller state. Cleared here so a previous run cannot
# leak into this one; imports all happen before any test runs.
_TEST_STATE = Path(tempfile.gettempdir()) / "proxmox-agent-lab-test-state"
shutil.rmtree(_TEST_STATE, ignore_errors=True)
_TEST_STATE.mkdir(parents=True, exist_ok=True)
os.environ["PROXMOX_AGENT_LAB_STATE"] = str(_TEST_STATE)

import json  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import unittest  # noqa: E402
from unittest import mock  # noqa: E402

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from proxmox_agent_lab import cli as LAB  # noqa: E402
from proxmox_agent_lab import memflow  # noqa: E402


class Args:
    def __init__(self, **kw: object) -> None:
        self.__dict__.update(kw)


def completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(["ssh"], returncode, stdout, stderr)


class OptInGuardTests(unittest.TestCase):
    def test_disabled_config_refuses(self) -> None:
        with mock.patch.object(memflow, "ENABLED", False):
            with self.assertRaises(LAB.LabError):
                memflow._require_enabled(LAB)

    def test_missing_host_refuses_even_when_enabled(self) -> None:
        with mock.patch.object(memflow, "ENABLED", True), \
             mock.patch.object(memflow, "SSH_HOST", ""):
            with self.assertRaises(LAB.LabError):
                memflow._require_enabled(LAB)


class SshChannelTests(unittest.TestCase):
    def test_key_is_referenced_by_path_never_inlined(self) -> None:
        with mock.patch.object(memflow, "SSH_KEY", "/example/keys/pxl_vmi"):
            argv = memflow._ssh_argv("id -un")
        self.assertIn("-i", argv)
        self.assertEqual(argv[argv.index("-i") + 1], "/example/keys/pxl_vmi")

    def test_batchmode_prevents_password_hangs(self) -> None:
        self.assertIn("BatchMode=yes", memflow._ssh_argv("id -un"))

    def test_transport_failure_is_not_a_remote_failure(self) -> None:
        with mock.patch("subprocess.run",
                        return_value=completed(255, stderr="conn refused")):
            with self.assertRaises(LAB.LabError) as ctx:
                memflow._ssh(LAB, ["id"])
        self.assertIn("cannot SSH", str(ctx.exception))

    def test_missing_helper_points_at_host_setup(self) -> None:
        with mock.patch("subprocess.run", return_value=completed(127)):
            with self.assertRaises(LAB.LabError) as ctx:
                memflow._helper(LAB, ["doctor"])
        self.assertIn("host-setup", str(ctx.exception))


class HostChangeGateTests(unittest.TestCase):
    def test_setup_refuses_without_authorization(self) -> None:
        with self.assertRaises(LAB.LabError) as ctx:
            memflow.cmd_host_setup(LAB, Args(host_change_authorized=False,
                                             print_only=False, timeout=60))
        self.assertIn("--host-change-authorized", str(ctx.exception))

    def test_print_needs_no_authorization(self) -> None:
        buf: list[str] = []
        with mock.patch("builtins.print", lambda *a, **k: buf.append(str(a[0]))):
            memflow.cmd_host_setup(LAB, Args(host_change_authorized=False,
                                             print_only=True, timeout=60))
        printed = "\n".join(buf)
        self.assertIn("pxl-memflow", printed)
        self.assertIn("cargo build", printed)


class ReadPathGuardTests(unittest.TestCase):
    def _fake_api(self, status: str):
        api = mock.Mock()
        api.call.return_value = {"status": status}
        return api

    def test_stopped_guest_is_refused(self) -> None:
        with mock.patch.object(memflow, "ENABLED", True), \
             mock.patch.object(LAB, "ProxmoxAPI",
                               return_value=self._fake_api("stopped")), \
             mock.patch.object(LAB, "load_lease", return_value={}):
            with self.assertRaises(LAB.LabError) as ctx:
                memflow.cmd_processes(LAB, Args(lease="L", vmid=9040))
        self.assertIn("not running", str(ctx.exception))

    def test_read_requires_a_lease(self) -> None:
        with mock.patch.object(memflow, "ENABLED", True), \
             mock.patch.object(LAB, "ProxmoxAPI",
                               return_value=self._fake_api("running")), \
             mock.patch.object(LAB, "load_lease",
                               side_effect=LAB.LabError("no such lease")):
            with self.assertRaises(LAB.LabError):
                memflow.cmd_processes(LAB, Args(lease="bogus", vmid=9040))

    def test_successful_read_audits_a_count_not_the_contents(self) -> None:
        audited: dict = {}

        def fake_audit(event, *, sync=True, **fields):
            audited["event"] = event
            audited["fields"] = fields

        rows = [{"pid": 4, "name": "System"}, {"pid": 404, "name": "smss.exe"}]
        with mock.patch.object(memflow, "ENABLED", True), \
             mock.patch.object(LAB, "ProxmoxAPI",
                               return_value=self._fake_api("running")), \
             mock.patch.object(LAB, "load_lease", return_value={}), \
             mock.patch.object(LAB, "audit", fake_audit), \
             mock.patch.object(memflow, "_helper_json", return_value=rows), \
             mock.patch("builtins.print") as printed:
            memflow.cmd_processes(LAB, Args(lease="L", vmid=9040))
        self.assertEqual(audited["event"], "memflow-processes")
        self.assertEqual(audited["fields"].get("count"), 2)
        # The audit records a count, never the process names themselves.
        self.assertNotIn("System", json.dumps(audited["fields"]))
        self.assertNotIn("smss.exe", json.dumps(audited["fields"]))
        # ...but the caller still sees the data.
        self.assertIn("smss.exe", printed.call_args[0][0])


class WriteGateTests(unittest.TestCase):
    """Writing live guest memory is hard-gated on top of the lease."""

    def _fake_api(self, status: str = "running"):
        api = mock.Mock()
        api.call.return_value = {"status": status}
        return api

    def test_write_refuses_without_i_understand(self) -> None:
        with mock.patch.object(memflow, "ENABLED", True):
            with self.assertRaises(LAB.LabError) as ctx:
                memflow.cmd_write(LAB, Args(lease="L", vmid=9040,
                                            addr="0x1000", hex="9090",
                                            i_understand=False))
        self.assertIn("--i-understand", str(ctx.exception))

    def test_write_rejects_bad_hex_before_touching_the_guest(self) -> None:
        # Odd-length / non-hex must fail before any lease load or helper call.
        with mock.patch.object(memflow, "ENABLED", True), \
             mock.patch.object(LAB, "load_lease") as load, \
             mock.patch.object(memflow, "_helper_json") as helper:
            with self.assertRaises(LAB.LabError):
                memflow.cmd_write(LAB, Args(lease="L", vmid=9040,
                                            addr="0x1000", hex="zzz",
                                            i_understand=True))
        load.assert_not_called()
        helper.assert_not_called()

    def test_write_audits_length_not_the_bytes(self) -> None:
        audited: dict = {}

        def fake_audit(event, *, sync=True, **fields):
            audited.update({"event": event, "fields": fields})

        with mock.patch.object(memflow, "ENABLED", True), \
             mock.patch.object(LAB, "ProxmoxAPI", return_value=self._fake_api()), \
             mock.patch.object(LAB, "load_lease", return_value={}), \
             mock.patch.object(LAB, "audit", fake_audit), \
             mock.patch.object(memflow, "_helper_json",
                               return_value={"written": 2}), \
             mock.patch("builtins.print"):
            memflow.cmd_write(LAB, Args(lease="L", vmid=9040, addr="0x1000",
                                        hex="dead", i_understand=True))
        self.assertEqual(audited["event"], "memflow-write")
        self.assertEqual(audited["fields"].get("length"), 2)
        # The bytes written are never recorded.
        self.assertNotIn("dead", json.dumps(audited["fields"]))


class PhysMemoryGuardTests(unittest.TestCase):
    """Physical RAM read/scan/inject: injection is hard-gated like write."""

    def _fake_api(self, status: str = "running"):
        api = mock.Mock()
        api.call.return_value = {"status": status}
        return api

    def test_phys_write_refuses_without_i_understand(self) -> None:
        with mock.patch.object(memflow, "ENABLED", True):
            with self.assertRaises(LAB.LabError) as ctx:
                memflow.cmd_phys_write(LAB, Args(lease="L", vmid=9072,
                                                 addr="0x1000", hex="00",
                                                 i_understand=False))
        self.assertIn("--i-understand", str(ctx.exception))

    def test_phys_write_rejects_bad_hex_before_touching_the_guest(self) -> None:
        with mock.patch.object(memflow, "ENABLED", True), \
             mock.patch.object(LAB, "load_lease") as load, \
             mock.patch.object(memflow, "_helper_json") as helper:
            with self.assertRaises(LAB.LabError):
                memflow.cmd_phys_write(LAB, Args(lease="L", vmid=9072,
                                                 addr="0x1000", hex="xy",
                                                 i_understand=True))
        load.assert_not_called()
        helper.assert_not_called()

    def test_phys_write_audits_length_not_bytes(self) -> None:
        audited: dict = {}
        with mock.patch.object(memflow, "ENABLED", True), \
             mock.patch.object(LAB, "ProxmoxAPI", return_value=self._fake_api()), \
             mock.patch.object(LAB, "load_lease", return_value={}), \
             mock.patch.object(LAB, "audit",
                               lambda e, *, sync=True, **f: audited.update(
                                   {"event": e, "fields": f})), \
             mock.patch.object(memflow, "_helper_json",
                               return_value={"written": 1}), \
             mock.patch("builtins.print"):
            memflow.cmd_phys_write(LAB, Args(lease="L", vmid=9072, addr="0x1a",
                                             hex="ab", i_understand=True))
        self.assertEqual(audited["event"], "memflow-phys-write")
        self.assertEqual(audited["fields"].get("length"), 1)
        self.assertNotIn("ab", json.dumps(audited["fields"]))

    def test_scan_requires_running_guest(self) -> None:
        with mock.patch.object(memflow, "ENABLED", True), \
             mock.patch.object(LAB, "ProxmoxAPI",
                               return_value=self._fake_api("stopped")), \
             mock.patch.object(LAB, "load_lease", return_value={}):
            with self.assertRaises(LAB.LabError) as ctx:
                memflow.cmd_scan(LAB, Args(lease="L", vmid=9072, hex="deadbeef",
                                           max_hits=8, timeout=60))
        self.assertIn("not running", str(ctx.exception))

    def test_scan_audits_hit_count_not_addresses(self) -> None:
        audited: dict = {}
        with mock.patch.object(memflow, "ENABLED", True), \
             mock.patch.object(LAB, "ProxmoxAPI", return_value=self._fake_api()), \
             mock.patch.object(LAB, "load_lease", return_value={}), \
             mock.patch.object(LAB, "audit",
                               lambda e, *, sync=True, **f: audited.update(
                                   {"event": e, "fields": f})), \
             mock.patch.object(memflow, "_helper_json",
                               return_value={"hits": ["0x1", "0x2"]}), \
             mock.patch("builtins.print"):
            memflow.cmd_scan(LAB, Args(lease="L", vmid=9072, hex="cafe",
                                       max_hits=8, timeout=60))
        self.assertEqual(audited["event"], "memflow-scan")
        self.assertEqual(audited["fields"].get("hits"), 2)


class DebugGuardTests(unittest.TestCase):
    """Stepping is lease-bound and only touches a running guest."""

    def _fake_api(self, status: str = "running"):
        api = mock.Mock()
        api.call.return_value = {"status": status}
        return api

    def test_trace_requires_running_guest(self) -> None:
        with mock.patch.object(memflow, "ENABLED", True), \
             mock.patch.object(LAB, "ProxmoxAPI",
                               return_value=self._fake_api("stopped")), \
             mock.patch.object(LAB, "load_lease", return_value={}):
            with self.assertRaises(LAB.LabError) as ctx:
                memflow.cmd_trace(LAB, Args(lease="L", vmid=9040,
                                            steps=4, over=False))
        self.assertIn("not running", str(ctx.exception))

    def test_trace_passes_over_flag_and_audits(self) -> None:
        audited: dict = {}
        with mock.patch.object(memflow, "ENABLED", True), \
             mock.patch.object(LAB, "ProxmoxAPI", return_value=self._fake_api()), \
             mock.patch.object(LAB, "load_lease", return_value={}), \
             mock.patch.object(LAB, "audit",
                               lambda e, *, sync=True, **f: audited.update(
                                   {"event": e, "fields": f})), \
             mock.patch.object(memflow, "_helper_json",
                               return_value={"steps": []}) as helper, \
             mock.patch("builtins.print"):
            memflow.cmd_trace(LAB, Args(lease="L", vmid=9040,
                                        steps=6, over=True))
        # The over flag must reach the helper, and the trace is audited.
        self.assertIn("over", helper.call_args[0][1])
        self.assertEqual(audited["event"], "memflow-trace")
        self.assertTrue(audited["fields"].get("over"))


class AnalyzeGuardTests(unittest.TestCase):
    """Ghidra analysis is lease-bound, size-capped, and surfaces failures."""

    def _fake_api(self, status: str = "running"):
        api = mock.Mock()
        api.call.return_value = {"status": status}
        return api

    def test_len_is_capped(self) -> None:
        with mock.patch.object(memflow, "ENABLED", True):
            with self.assertRaises(LAB.LabError):
                memflow.cmd_analyze(LAB, Args(lease="L", vmid=9040, lxc=9041,
                                              addr="0x1000", len=99999999,
                                              base=None, timeout=600))

    def test_base_defaults_to_addr(self) -> None:
        with mock.patch.object(memflow, "ENABLED", True), \
             mock.patch.object(LAB, "ProxmoxAPI", return_value=self._fake_api()), \
             mock.patch.object(LAB, "load_lease", return_value={}), \
             mock.patch.object(LAB, "audit"), \
             mock.patch.object(memflow, "_helper_json",
                               return_value={"function_count": 0}) as helper, \
             mock.patch("builtins.print"):
            memflow.cmd_analyze(LAB, Args(lease="L", vmid=9040, lxc=9041,
                                          addr="0xdeadbeef", len=4096,
                                          base=None, timeout=600))
        # analyze <vmid> <lxc> <addr> <len> <base> -- base defaults to addr.
        passed = helper.call_args[0][1]
        self.assertEqual(passed[-1], "0xdeadbeef")

    def test_ghidra_error_is_raised_not_printed_as_success(self) -> None:
        with mock.patch.object(memflow, "ENABLED", True), \
             mock.patch.object(LAB, "ProxmoxAPI", return_value=self._fake_api()), \
             mock.patch.object(LAB, "load_lease", return_value={}), \
             mock.patch.object(LAB, "audit"), \
             mock.patch.object(memflow, "_helper_json",
                               return_value={"error": "no analysis output",
                                             "log_tail": "boom"}):
            with self.assertRaises(LAB.LabError) as ctx:
                memflow.cmd_analyze(LAB, Args(lease="L", vmid=9040, lxc=9041,
                                              addr="0x1000", len=4096,
                                              base=None, timeout=600))
        self.assertIn("Ghidra analysis failed", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()


class BootDiagnoseTests(unittest.TestCase):
    def _fake_api(self, status: str = "running"):
        api = mock.Mock()
        api.call.return_value = {"status": status}
        return api

    def _run(self, helper_side_effect, **arg_overrides):
        args = Args(lease="L", vmid=9050, settle=0.0, max_hits=4, timeout=30)
        for key, value in arg_overrides.items():
            setattr(args, key, value)
        captured: list[str] = []
        with mock.patch.object(memflow, "ENABLED", True), \
             mock.patch.object(LAB, "ProxmoxAPI", return_value=self._fake_api()), \
             mock.patch.object(LAB, "load_lease", return_value={}), \
             mock.patch.object(LAB, "audit") as audit, \
             mock.patch("time.sleep"), \
             mock.patch.object(memflow, "_helper_json",
                               side_effect=helper_side_effect), \
             mock.patch("builtins.print",
                        lambda *a, **k: captured.append(str(a[0]))):
            memflow.cmd_boot_diagnose(LAB, args)
        return json.loads(captured[-1]), audit

    def test_wedged_cpu_with_panic_text_is_reported(self) -> None:
        def helper(lab, sub, timeout=90):
            if sub[0] == "registers":
                return {"RIP": "ffffffff81000abc"}
            # Only the linux-panic signature has a hit.
            text = "Kernel panic - not syncing".encode().hex()
            if sub[0] == "scan" and sub[2] == text:
                return {"hits": [{"addr": "0x3f120"}]}
            return {"hits": []}

        result, audit = self._run(helper)
        self.assertEqual(result["cpu_state"], "wedged")
        self.assertFalse(result["instruction_pointer_moved"])
        names = [f["signature"] for f in result["signatures_found"]]
        self.assertIn("linux-panic", names)
        self.assertIn("wedged", result["verdict"])
        # The matched RAM text is never audited, only the category.
        self.assertEqual(audit.call_args.kwargs["categories"], ["linux"])
        self.assertNotIn("Kernel panic",
                         json.dumps(audit.call_args.kwargs))

    def test_executing_cpu_with_no_signatures_reads_as_slow_boot(self) -> None:
        ips = ["ffffffff81000abc", "ffffffff81000def"]

        def helper(lab, sub, timeout=90):
            if sub[0] == "registers":
                return {"RIP": ips.pop(0)}
            return {"hits": []}

        result, _ = self._run(helper)
        self.assertEqual(result["cpu_state"], "executing")
        self.assertTrue(result["instruction_pointer_moved"])
        self.assertEqual(result["signatures_found"], [])
        self.assertIn("booting slowly", result["verdict"])

    def test_stopped_guest_is_refused_before_any_scan(self) -> None:
        with mock.patch.object(memflow, "ENABLED", True), \
             mock.patch.object(LAB, "ProxmoxAPI",
                               return_value=self._fake_api("stopped")), \
             mock.patch.object(LAB, "load_lease", return_value={}), \
             mock.patch.object(memflow, "_helper_json") as helper:
            with self.assertRaises(LAB.LabError):
                memflow.cmd_boot_diagnose(
                    LAB, Args(lease="L", vmid=9050, settle=0.0,
                              max_hits=4, timeout=30))
        helper.assert_not_called()


class DumpRegressionTests(unittest.TestCase):
    """`memflow dump` must open the API and check the lease before reading.

    Regression: a refactor once dropped both `api = lab.ProxmoxAPI()` and
    `lab.load_lease(...)` from cmd_dump, leaving an undefined `api` (the
    command raised NameError) and no lease gate. compileall cannot see it.
    """

    def _args(self, out: str) -> mock.Mock:
        return mock.Mock(len=64, addr="0x1000", vmid=101, lease="L1", out=out)

    def test_dump_checks_the_lease_and_writes_bytes(self) -> None:
        lab = mock.Mock()
        lab.LabError = RuntimeError
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "dump.bin")
            with mock.patch.object(memflow, "_require_enabled"), \
                 mock.patch.object(memflow, "_require_running_qemu"), \
                 mock.patch.object(memflow, "_helper_json",
                                   return_value={"hex": "41424344"}):
                memflow.cmd_dump(lab, self._args(out))
            self.assertEqual(Path(out).read_bytes(), b"ABCD")
        lab.load_lease.assert_called_once_with("L1")
        lab.ProxmoxAPI.assert_called_once_with()

    def test_dump_refuses_an_invalid_lease(self) -> None:
        lab = mock.Mock()
        lab.LabError = RuntimeError
        lab.load_lease.side_effect = RuntimeError("no such lease")
        with mock.patch.object(memflow, "_require_enabled"), \
             mock.patch.object(memflow, "_require_running_qemu"), \
             mock.patch.object(memflow, "_helper_json") as helper:
            with self.assertRaises(RuntimeError):
                memflow.cmd_dump(lab, self._args("/dev/null"))
        helper.assert_not_called()
