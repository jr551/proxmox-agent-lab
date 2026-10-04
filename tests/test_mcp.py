"""Tests for the MCP server (``mcp.py``): the 34-tool surface over stdio.

Two harnesses, both speaking real newline-delimited JSON-RPC bytes:

* ``_InProcessMcp`` -- ``mcp.serve`` on a daemon thread wired to real
  ``os.pipe`` pairs, with a ``FakeLab`` facade and ``FakeSSH`` seam so the
  mutating tools and the idle-shutdown sweep run end to end with no host.
* ``_SubprocessMcp`` -- ``python3 -m proxmox_agent_lab mcp`` spawned for
  real, proving the entry point and the wire format over actual stdio.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Shared bootstrap: fixture configuration plus a per-process state directory,
# applied before any proxmox_agent_lab import. `support` sits beside this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import bootstrap  # noqa: E402,F401

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import json  # noqa: E402
import os  # noqa: E402
import select  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from proxmox_agent_lab import cleanup as cleanup_module  # noqa: E402
from proxmox_agent_lab import guest as guest_module  # noqa: E402
from proxmox_agent_lab import leases as leases_module  # noqa: E402
from proxmox_agent_lab import mcp  # noqa: E402
from proxmox_agent_lab import proxmox as proxmox_module  # noqa: E402
from proxmox_agent_lab import store as store_module  # noqa: E402
from support.fakessh import FakeSSH  # noqa: E402

#: The pinned §E surface: exactly these names, in tools/list order. The six
#: pointer/capture tools are ported from vnc-mcp (BSD 2-Clause, see NOTICE)
#: and ride the same seam; the server itself stays stdio-only, never a daemon.
TOOL_NAMES = [
    "lease_begin",
    "lease_heartbeat",
    "lease_end",
    "lease_list",
    "lease_destroy",
    "lease_register",
    "guest_create",
    "guest_clone",
    "guest_media",
    "guest_start",
    "guest_stop",
    "guest_destroy",
    "guest_probe",
    "guest_list",
    "guest_snapshot",
    "guest_template",
    "guest_run",
    "push_file",
    "pull_file",
    "console_move",
    "console_click",
    "console_drag",
    "console_calibrate",
    "console_grid",
    "console_burst",
    "console_screenshot",
    "console_type",
    "console_keys",
    "cleanup_expired",
    "journal_query",
    "doctor",
    "power_status",
    "storage_status",
    "net_capture",
]

READ_TIMEOUT = 10.0


def _config() -> object:
    """The minimal config the facade and feature modules read."""
    import types

    return types.SimpleNamespace(
        ssh=types.SimpleNamespace(target="fixture-host"),
        pve=types.SimpleNamespace(node="pve", template_vmid=9000),
        lease=types.SimpleNamespace(
            ttl_seconds=7200, idle_shutdown_seconds=28800
        ),
        power=types.SimpleNamespace(
            mac="aa:bb:cc:dd:ee:ff", broadcast="255.255.255.255", port=9
        ),
    )


class FakeLab:
    """The `lab` surface mcp.py needs: config, state root, audit, power."""

    def __init__(self, state_root: Path) -> None:
        self.STATE_ROOT = Path(state_root)
        self.CONFIG = _config()
        self.audits: list[tuple[str, dict]] = []
        self.idle_threshold = 28800
        self.power_status_payload = {
            "reachable": False,
            "powered_on": False,
            "last_power_event": None,
            "active_leases": 0,
            "running_guests": None,
        }
        self.shutdown_result = True

    def audit(self, event: str, **fields) -> None:
        self.audits.append((event, fields))

    def record_mcp_activity(self, tool_name: str) -> None:
        leases_module.record_mcp_activity(
            Path(self.STATE_ROOT), tool_name, audit=self.audit
        )

    def mcp_idle_shutdown_due(self) -> bool:
        return leases_module.mcp_idle_shutdown_due(
            Path(self.STATE_ROOT), idle_shutdown_seconds=self.idle_threshold
        )

    def shutdown_host(self, api=None) -> bool:
        return self.shutdown_result

    def power_status(self) -> dict:
        return dict(self.power_status_payload)

    def audit_names(self) -> list[str]:
        return [event for event, _ in self.audits]

    def tool_calls(self) -> list[dict]:
        return [
            fields for event, fields in self.audits
            if event == "mcp-tool-call"
        ]


class _LineChannel:
    """Reads newline-delimited JSON-RPC responses off an fd, with timeouts."""

    def __init__(self, read_fd: int) -> None:
        self.read_fd = read_fd
        self._pending = bytearray()
    def _block(self) -> None:
        ready, _, _ = select.select([self.read_fd], [], [], READ_TIMEOUT)
        if not ready:
            raise AssertionError("timed out waiting for a response line")
        chunk = os.read(self.read_fd, 65536)
        if not chunk:
            raise AssertionError("server closed its output")
        self._pending += chunk

    def _buffered_line(self) -> bytes | None:
        cut = self._pending.find(b"\n")
        if cut < 0:
            return None
        line = bytes(self._pending[:cut])
        del self._pending[: cut + 1]
        return line

    def read_message(self, timeout: float = READ_TIMEOUT) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            line = self._buffered_line()
            if line is not None:
                return json.loads(line.decode("utf-8"))
            if time.monotonic() >= deadline:
                raise AssertionError("timed out waiting for a response line")
            self._block()

    def has_message(self, grace: float = 0.4) -> bool:
        if self._buffered_line() is not None:
            return True
        ready, _, _ = select.select([self.read_fd], [], [], grace)
        return bool(ready)


class _InProcessMcp:
    """mcp.serve on a daemon thread over real os.pipe pairs."""

    def __init__(self, lab: FakeLab) -> None:
        self.lab = lab
        self.server_r, self.server_w = os.pipe()   # client writes, server reads
        self.client_r, self.client_w = os.pipe()   # server writes, client reads
        self.channel = _LineChannel(self.client_r)
        self.thread = threading.Thread(
            target=self._run, daemon=True, name="mcp-test-server"
        )

    def _run(self) -> None:
        in_f = os.fdopen(self.server_r, "rb", closefd=True)
        out_f = os.fdopen(self.client_w, "wb", closefd=True)
        try:
            mcp.serve(self.lab, None, stdin=in_f, stdout=out_f)
        except Exception as exc:  # noqa: BLE001 - surface it in the test
            self.error = exc
        finally:
            # Own the pipe ends: serve returning means EOF, so closing the
            # wrappers here keeps the suite warning-clean.
            for handle in (in_f, out_f):
                try:
                    handle.close()
                except OSError:
                    pass

    def start(self) -> "_InProcessMcp":
        self.thread.start()
        return self

    def send(self, message: dict | str) -> None:
        line = message if isinstance(message, str) else json.dumps(message)
        os.write(self.server_w, line.encode("utf-8") + b"\n")

    def request(self, message: dict) -> dict:
        self.send(message)
        return self.channel.read_message()

    def call(self, name: str, arguments: dict | None = None,
             request_id: int = 1) -> dict:
        return self.request({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        })

    def close(self) -> None:
        os.close(self.server_w)
        self.thread.join(timeout=READ_TIMEOUT)
        os.close(self.client_r)

    def __enter__(self) -> "_InProcessMcp":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()


class _SubprocessMcp:
    """The real ``python3 -m proxmox_agent_lab mcp`` over real stdio."""

    def __init__(self) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "proxmox_agent_lab", "mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        self.channel = _LineChannel(self.proc.stdout.fileno())

    def send(self, message: dict | str) -> None:
        line = message if isinstance(message, str) else json.dumps(message)
        self.proc.stdin.write(line.encode("utf-8") + b"\n")
        self.proc.stdin.flush()

    def request(self, message: dict) -> dict:
        self.send(message)
        return self.channel.read_message()

    def call(self, name: str, arguments: dict | None = None,
             request_id: int = 1) -> dict:
        return self.request({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        })

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except BrokenPipeError:
            pass
        try:
            self.proc.wait(timeout=READ_TIMEOUT)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        for pipe in (self.proc.stdout, self.proc.stderr):
            if pipe is not None:
                pipe.close()

    def __enter__(self) -> "_SubprocessMcp":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class McpTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_root = Path(self.tmp.name)
        self.lab = FakeLab(self.state_root)
    def db(self) -> store_module.Store:
        return store_module.Store(self.state_root / "lab.db")

    def last_activity(self) -> float | None:
        with self.db() as database:
            return database.last_mcp_activity()

    def result_payload(self, response: dict) -> dict:
        self.assertIn("result", response)
        content = response["result"].get("content")
        self.assertIsInstance(content, list)
        self.assertEqual(len(content), 1)
        self.assertEqual(content[0]["type"], "text")
        return json.loads(content[0]["text"])


class SubprocessProtocolTests(unittest.TestCase):
    """The canonical smoke: the real entry point over real stdio bytes."""

    def test_initialize_and_tools_list_over_real_stdio(self):
        with _SubprocessMcp() as server:
            hello = server.request({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            })
            self.assertEqual(hello["id"], 1)
            result = hello["result"]
            self.assertEqual(result["protocolVersion"], "2024-11-05")
            self.assertEqual(result["capabilities"], {"tools": {}})
            self.assertEqual(result["serverInfo"]["name"], "proxmox-agent-lab")

            listed = server.request({
                "jsonrpc": "2.0", "id": 2, "method": "tools/list",
            })
            tools = listed["result"]["tools"]
            self.assertEqual([t["name"] for t in tools], TOOL_NAMES)

    def test_protocol_errors_over_real_stdio(self):
        with _SubprocessMcp() as server:
            server.send("{not json")
            bad = server.channel.read_message()
            self.assertEqual(bad["error"]["code"], -32700)

            missing = server.request({
                "jsonrpc": "2.0", "id": 9, "method": "no/such/method",
            })
            self.assertEqual(missing["error"]["code"], -32601)
            schema = server.request({
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {"name": "guest_probe",
                           "arguments": {"vmid": "abc"}},
            })
            self.assertEqual(schema["error"]["code"], -32602)
            self.assertIn("vmid", schema["error"]["message"])

    def test_tools_call_lease_list_over_real_stdio(self):
        # Read-only, no seam needed: proves tools/call dispatches through the
        # real cli facade to a store-backed handler in the child process.
        with _SubprocessMcp() as server:
            response = server.call("lease_list", {}, request_id=4)
        self.assertEqual(response["id"], 4)
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertIn("leases", payload)


class InProcessProtocolTests(McpTestCase):
    """The same protocol checks in-process, where FakeSSH can be injected."""

    def test_initialize_shape(self):
        with _InProcessMcp(self.lab) as server:
            response = server.request({
                "jsonrpc": "2.0",
                "id": 7,
                "method": "initialize",
                "params": {},
            })
        result = response["result"]
        self.assertEqual(result["protocolVersion"], "2024-11-05")
        self.assertEqual(
            result["serverInfo"]["name"], "proxmox-agent-lab"
        )
        self.assertIn("version", result["serverInfo"])
        self.assertEqual(result["capabilities"], {"tools": {}})

    def test_tools_list_is_exactly_the_34_tools_with_schemas(self):
        with _InProcessMcp(self.lab) as server:
            listed = server.request({
                "jsonrpc": "2.0", "id": 1, "method": "tools/list",
            })
        tools = listed["result"]["tools"]
        self.assertEqual(len(tools), 34)
        self.assertEqual([t["name"] for t in tools], TOOL_NAMES)
        for tool in tools:
            schema = tool["inputSchema"]
            self.assertEqual(schema["type"], "object")
            self.assertFalse(schema["additionalProperties"])
            self.assertIn("properties", schema)
            for prop in schema.get("required", ()):
                self.assertIn(prop, schema["properties"])

    def test_malformed_json_is_parse_error(self):
        with _InProcessMcp(self.lab) as server:
            server.send("this is not json")
            response = server.channel.read_message()
        self.assertEqual(response["id"], None)
        self.assertEqual(response["error"]["code"], -32700)

    def test_unknown_method_is_method_not_found(self):
        with _InProcessMcp(self.lab) as server:
            response = server.request({
                "jsonrpc": "2.0", "id": 3, "method": "resources/list",
            })
        self.assertEqual(response["id"], 3)
        self.assertEqual(response["error"]["code"], -32601)

    def test_non_object_message_is_invalid_request(self):
        with _InProcessMcp(self.lab) as server:
            server.send('["a", "batch", "is", "unsupported"]')
            response = server.channel.read_message()
        self.assertEqual(response["error"]["code"], -32600)

    def test_notifications_get_no_response(self):
        with _InProcessMcp(self.lab) as server:
            server.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            server.send({"jsonrpc": "2.0", "method": "initialize"})
            # A follow-up request must get exactly one response -- its own.
            response = server.request({
                "jsonrpc": "2.0", "id": 11, "method": "tools/list",
            })
            self.assertEqual(response["id"], 11)
            self.assertFalse(server.channel.has_message())

    def test_tools_call_notification_dispatches_but_says_nothing(self):
        with _InProcessMcp(self.lab) as server:
            server.send({
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "journal_query", "arguments": {}},
            })
            self.assertFalse(server.channel.has_message())
            self.assertIsNotNone(self.last_activity())
            calls = self.lab.tool_calls()
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["tool"], "journal_query")


class ParamsErrorTests(McpTestCase):
    """-32602: schema violations, confirm gate, bad key names -- no dispatch."""

    def test_unknown_tool_names_the_tool(self):
        with _InProcessMcp(self.lab) as server:
            response = server.call("no_such_tool", {}, request_id=5)
        self.assertEqual(response["error"]["code"], -32602)
        self.assertIn("no_such_tool", response["error"]["message"])

    def test_missing_required_field_names_it(self):
        with _InProcessMcp(self.lab) as server:
            response = server.call("guest_start", {"lease_id": "abc"})
        self.assertEqual(response["error"]["code"], -32602)
        self.assertIn("'vmid'", response["error"]["message"])

    def test_unknown_field_is_rejected(self):
        with _InProcessMcp(self.lab) as server:
            response = server.call(
                "guest_probe", {"vmid": 1, "surprise": True}
            )
        self.assertEqual(response["error"]["code"], -32602)
        self.assertIn("'surprise'", response["error"]["message"])

    def test_wrong_type_names_field_not_value(self):
        with _InProcessMcp(self.lab) as server:
            response = server.call(
                "guest_probe", {"vmid": "topsecret-string"}
            )
        self.assertEqual(response["error"]["code"], -32602)
        message = response["error"]["message"]
        self.assertIn("'vmid'", message)
        self.assertNotIn("topsecret-string", message)

    def test_boolean_is_not_an_integer(self):
        with _InProcessMcp(self.lab) as server:
            response = server.call("guest_probe", {"vmid": True})
        self.assertEqual(response["error"]["code"], -32602)

    def test_enum_violation(self):
        with _InProcessMcp(self.lab) as server:
            response = server.call(
                "lease_register",
                {"lease": "x", "kind": "weird", "vmid": 100},
            )
        self.assertEqual(response["error"]["code"], -32602)
        self.assertIn("'kind'", response["error"]["message"])

    def test_confirm_absent_and_false_both_rejected(self):
        for arguments in ({}, {"confirm": False}, {"confirm": "yes"}):
            with self.subTest(arguments=arguments):
                with _InProcessMcp(self.lab) as server:
                    response = server.call(
                        "guest_destroy",
                        {"lease_id": "x", "vmid": 100, **arguments},
                    )
                self.assertEqual(response["error"]["code"], -32602)
                self.assertIn("confirm", response["error"]["message"])

    def test_params_must_be_an_object(self):
        with _InProcessMcp(self.lab) as server:
            response = server.request({
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": "journal_query",
            })
        self.assertEqual(response["error"]["code"], -32602)

    def test_bad_params_are_still_audited(self):
        with _InProcessMcp(self.lab) as server:
            server.call("guest_probe", {"vmid": "abc"})
        calls = self.lab.tool_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["tool"], "guest_probe")
        self.assertFalse(calls[0]["ok"])

    def test_console_keys_unknown_name_is_invalid_params(self):
        with self.db() as database:
            database.create_lease(
                "leasetest-01", purpose="t", expires_at=9999999999
            )
            database.register_resource(
                "leasetest-01", "qemu", 100, name="vm100"
            )
        with _InProcessMcp(self.lab) as server:
            response = server.call(
                "console_keys",
                {"lease_id": "leasetest-01", "vmid": 100,
                 "keys": ["not-a-real-key-name!!"]},
            )
        self.assertEqual(response["error"]["code"], -32602)
        self.assertIn("'keys'", response["error"]["message"])


class ToolCallTests(McpTestCase):
    """Dispatch onto the shared cmd_* handlers behind FakeSSH."""

    def test_journal_query_wraps_events(self):
        # journal.query_events reads the process config's state dir, not the
        # lab facade's -- seed the event where the reader will look.
        from proxmox_agent_lab import config as config_module

        with store_module.Store(
            config_module.state_dir() / "lab.db"
        ) as database:
            database.record(
                "test-event", lease="leasetest-01", vmid=100,
                data={"note": "hello"},
            )
        with _InProcessMcp(self.lab) as server:
            response = server.call(
                "journal_query", {"lease_id": "leasetest-01"}
            )
        payload = self.result_payload(response)
        events = payload["events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "test-event")
        self.assertEqual(events[0]["lease"], "leasetest-01")
        self.assertEqual(events[0]["vmid"], 100)

    def test_lease_list_reports_contract_fields(self):
        with self.db() as database:
            database.create_lease(
                "leasetest-01", purpose="demo", expires_at=9999999999
            )
            database.create_lease(
                "leasetest-02", purpose="old", expires_at=1
            )
            database.set_lease_state("leasetest-02", "ended", ended=True)
        with _InProcessMcp(self.lab) as server:
            active = self.result_payload(
                server.call("lease_list", {})
            )
            everything = self.result_payload(
                server.call("lease_list", {"include_ended": True})
            )
        self.assertEqual(
            [row["id"] for row in active["leases"]], ["leasetest-01"]
        )
        self.assertEqual(len(everything["leases"]), 2)
        first = everything["leases"][0]
        for field in ("id", "kind", "purpose", "state",
                      "created_at", "expires_at", "heartbeat_at"):
            self.assertIn(field, first)

    def test_guest_start_mutates_through_fakessh(self):
        fake = FakeSSH()
        fake.add("^true$")                      # any probe
        fake.add("^qm start 100$")
        fake.add("^qm status 100$", stdout=b"status: running")
        seam = proxmox_module.Proxmox(fake, "pve")
        with self.db() as database:
            database.create_lease(
                "leasetest-01", purpose="t", expires_at=9999999999
            )
            database.register_resource(
                "leasetest-01", "qemu", 100, name="vm100"
            )
        with mock.patch.object(
            guest_module, "_make_proxmox", lambda config: seam
        ), _InProcessMcp(self.lab) as server:
            response = server.call(
                "guest_start", {"lease_id": "leasetest-01", "vmid": 100}
            )
        payload = self.result_payload(response)
        self.assertEqual(payload["state"], "running")
        self.assertEqual(payload["vmid"], 100)
        self.assertEqual(
            [call["argv"] for call in fake.calls],
            [["qm", "start", "100"], ["qm", "status", "100"]],
        )
        calls = self.lab.tool_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["tool"], "guest_start")
        self.assertTrue(calls[0]["ok"])
        self.assertEqual(calls[0]["vmid"], 100)
        self.assertEqual(calls[0]["target"], 100)
        self.assertEqual(calls[0]["actor"], "mcp")

    def test_action_failure_is_internal_error_and_audited(self):
        # vmid 999 is registered to nobody: require_owned refuses it.
        with _InProcessMcp(self.lab) as server:
            response = server.call(
                "guest_start", {"lease_id": "leasetest-01", "vmid": 999}
            )
        self.assertEqual(response["error"]["code"], -32603)
        self.assertIn("guest_start", response["error"]["message"])
        calls = self.lab.tool_calls()
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0]["ok"])
        self.assertEqual(calls[0]["target"], 999)

    def test_every_call_refreshes_the_idle_clock(self):
        self.assertIsNone(self.last_activity())
        with _InProcessMcp(self.lab) as server:
            server.call("lease_list", {})
            server.call("journal_query", {})
            # A failing call still counts: guest_start refuses vmid 999 at
            # the registry gate (never reaching ssh).
            server.call(
                "guest_start", {"lease_id": "nobody", "vmid": 999}
            )
        self.assertIsNotNone(self.last_activity())
        self.assertGreaterEqual(self.last_activity(), time.time() - 60)
        self.assertEqual(len(self.lab.tool_calls()), 3)

    def test_console_type_sends_sendkey_and_audits_no_text(self):
        # Mutating console input over FakeSSH: the typed text reaches the
        # guest as qm sendkey argv and never lands in an audit field.
        from proxmox_agent_lab import console as console_module

        fake = FakeSSH()
        fake.add(r"^qm sendkey 100")
        with self.db() as database:
            database.create_lease(
                "leasetest-01", purpose="t", expires_at=9999999999
            )
            database.register_resource(
                "leasetest-01", "qemu", 100, name="vm100"
            )
        with mock.patch.object(
            console_module, "_make_ssh", lambda config: fake
        ), _InProcessMcp(self.lab) as server:
            response = server.call(
                "console_type",
                {"lease_id": "leasetest-01", "vmid": 100,
                 "text": "hunter2"},
            )
        payload = self.result_payload(response)
        self.assertEqual(payload["chars"], len("hunter2"))
        self.assertTrue(payload["enter"])
        # One sendkey call per character, then the trailing `ret`.
        sendkey = [call["argv"] for call in fake.calls]
        self.assertEqual(len(sendkey), len("hunter2") + 1)
        for argv in sendkey:
            self.assertEqual(argv[:3], ["qm", "sendkey", "100"])
        self.assertEqual(sendkey[-1], ["qm", "sendkey", "100", "ret"])
        for _, fields in self.lab.audits:
            self.assertNotIn("hunter2", json.dumps(fields))


class IdleShutdownTests(McpTestCase):
    """The second power-off net: fires on the periodic self-wake."""

    def test_idle_sweep_fires_verified_shutdown_without_client_traffic(self):
        # Idle long ago, no leases, host unreachable -> the verified path
        # confirms power-off without ever issuing `shutdown`.
        fake = FakeSSH()  # no rules: probe() answers False
        seam = proxmox_module.Proxmox(fake, "pve")
        self.lab.idle_threshold = 0  # any recorded activity reads as stale
        self.lab.CONFIG.power.auto_shutdown = True
        self.lab.shutdown_host = (
            lambda api=None: cleanup_module.shutdown_host(self.lab, api)
        )
        with mock.patch.object(
            cleanup_module, "_make_proxmox", lambda config: seam
        ), mock.patch.object(mcp, "IDLE_CHECK_SECONDS", 0.05), \
                _InProcessMcp(self.lab) as server:
            server.send({"jsonrpc": "2.0", "method": "initialized"})
            deadline = time.monotonic() + READ_TIMEOUT
            while (time.monotonic() < deadline
                   and "mcp-idle-shutdown-triggered"
                   not in self.lab.audit_names()):
                time.sleep(0.02)
            server.call("journal_query", {})
        self.assertIn("mcp-idle-shutdown-triggered", self.lab.audit_names())
        self.assertIn("lab-power-off-already-verified", self.lab.audit_names())

    def test_idle_sweep_stays_quiet_while_a_lease_is_active(self):
        fake = FakeSSH()
        seam = proxmox_module.Proxmox(fake, "pve")
        self.lab.idle_threshold = 0
        self.lab.shutdown_host = (
            lambda api=None: cleanup_module.shutdown_host(self.lab, api)
        )
        with self.db() as database:
            database.create_lease(
                "leasetest-01", purpose="t", expires_at=9999999999
            )
        with mock.patch.object(
            cleanup_module, "_make_proxmox", lambda config: seam
        ), mock.patch.object(mcp, "IDLE_CHECK_SECONDS", 0.05), \
                _InProcessMcp(self.lab) as server:
            server.send({"jsonrpc": "2.0", "method": "initialized"})
            time.sleep(0.3)
            server.call("journal_query", {})
        self.assertNotIn(
            "mcp-idle-shutdown-triggered", self.lab.audit_names()
        )
        self.assertEqual(fake.calls, [])  # host never touched


class PowerStatusTests(McpTestCase):
    def test_power_status_uses_the_shared_callable(self):
        self.lab.power_status_payload = {
            "reachable": True,
            "powered_on": True,
            "last_power_event": {"event": "lab-power-off-verified",
                                 "timestamp": "2026-09-25T00:00:00Z"},
            "active_leases": 2,
            "running_guests": [100, 101],
        }
        with _InProcessMcp(self.lab) as server:
            response = server.call("power_status", {})
        self.assertEqual(
            self.result_payload(response), self.lab.power_status_payload
        )


if __name__ == "__main__":
    unittest.main()
