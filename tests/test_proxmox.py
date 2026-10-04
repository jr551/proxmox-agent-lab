"""Proxmox control wrappers: call sequences and every Q4 fallback branch.

Everything runs against ``FakeSSH`` -- no test spawns a real ssh. The
assertions are on the exact remote argv sequences (and stdin payloads for the
chunked transfer), because that is the contract the seam and the allowlist
consume.
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import bootstrap  # noqa: E402,F401
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from proxmox_agent_lab.proxmox import ExecResult, Proxmox, ProxmoxError  # noqa: E402
from support.fakessh import FakeSSH  # noqa: E402


def _j(value) -> bytes:
    return json.dumps(value).encode()


def _agent_reply(exit_code: int, out: bytes = b"", err: bytes = b"") -> bytes:
    return _j(
        {
            "exitcode": exit_code,
            "out-data": base64.b64encode(out).decode(),
            "err-data": base64.b64encode(err).decode(),
        }
    )


class ProxmoxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ssh = FakeSSH()
        self.sleeps: list[float] = []
        self.pve = Proxmox(self.ssh, "pve1", sleep=self.sleeps.append)

    # -- host introspection -------------------------------------------------

    def test_node_status_parses_pvesh_json(self) -> None:
        self.ssh.add(r"pvesh get /nodes/pve1/status", stdout=_j({"cpu": 0.25, "uptime": 100}))
        self.assertEqual(self.pve.node_status(), {"cpu": 0.25, "uptime": 100})
        call = self.ssh.calls[0]
        self.assertEqual(
            call["argv"],
            ["pvesh", "get", "/nodes/pve1/status", "--output-format", "json"],
        )
        self.assertEqual(call["timeout"], 30.0)

    def test_cluster_nextid_reads_the_json_string(self) -> None:
        self.ssh.add(r"pvesh get /cluster/nextid", stdout=b'"120"\n')
        self.assertEqual(self.pve.cluster_nextid(), 120)
        self.assertEqual(
            self.ssh.calls[0]["argv"],
            ["pvesh", "get", "/cluster/nextid", "--output-format", "json"],
        )

    def test_network_bridges_asks_for_any_bridge(self) -> None:
        self.ssh.add(
            r"pvesh get /nodes/pve1/network",
            stdout=_j([{"iface": "vmbr0", "type": "bridge"}]),
        )
        self.assertEqual(self.pve.network_bridges()[0]["iface"], "vmbr0")
        self.assertEqual(
            self.ssh.calls[0]["argv"],
            [
                "pvesh", "get", "/nodes/pve1/network",
                "--type", "any_bridge", "--output-format", "json",
            ],
        )

    def test_pveversion_returns_stripped_output(self) -> None:
        self.ssh.add("pveversion", stdout=b"pve-manager/8.3.1/abc\n")
        self.assertEqual(self.pve.pveversion(), "pve-manager/8.3.1/abc")
        self.assertEqual(self.ssh.calls[0]["argv"], ["pveversion"])

    def test_command_failure_raises_with_real_stderr(self) -> None:
        self.ssh.add(r"pvesh get", returncode=2, stderr=b"permission denied\n")
        with self.assertRaisesRegex(ProxmoxError, "permission denied"):
            self.pve.node_status()

    def test_task_status_queries_pvesh_task_path(self) -> None:
        self.ssh.add(r"tasks/", stdout=_j({"status": "running"}))
        self.assertEqual(self.pve.task_status("UPID:pve1:0001"), {"status": "running"})
        self.assertEqual(
            self.ssh.calls[0]["argv"],
            [
                "pvesh",
                "get",
                "/nodes/pve1/tasks/UPID:pve1:0001/status",
                "--output-format",
                "json",
            ],
        )

    def test_wait_task_polls_until_stopped(self) -> None:
        self.ssh.add(r"tasks/", stdout=_j({"status": "running"}), times=1)
        self.ssh.add(r"tasks/", stdout=_j({"status": "stopped", "exitstatus": "OK"}))
        state = self.pve.wait_task("UPID:pve1:0001", timeout=300.0, poll=2.0)
        self.assertEqual(state["exitstatus"], "OK")
        self.assertEqual(len(self.ssh.calls), 2)
        self.assertEqual(self.sleeps, [2.0])

    def test_wait_task_deadline_raises(self) -> None:
        self.ssh.add(r"tasks/", stdout=_j({"status": "running"}))
        with self.assertRaisesRegex(ProxmoxError, "did not stop"):
            self.pve.wait_task("UPID:pve1:0002", timeout=0.0)
        self.assertEqual(len(self.ssh.calls), 1)
        self.assertEqual(self.sleeps, [])

    # -- guest lifecycle ----------------------------------------------------

    def test_qemu_create_stamps_tags_and_description_in_one_call(self) -> None:
        self.ssh.add(r"qm create 100")
        self.pve.qemu_create(
            100,
            name="netgw",
            tags="pxl;lease-abc",
            description="pxl-lease=abc pxl-expiry=123",
            scsi0="local-lvm:8",
            memory=2048,
            cores=2,
        )
        self.assertEqual(
            self.ssh.calls[0]["argv"],
            [
                "qm",
                "create",
                "100",
                "--name",
                "netgw",
                "--net0",
                "virtio,bridge=vmbr0",
                "--scsi0",
                "local-lvm:8",
                "--memory",
                "2048",
                "--cores",
                "2",
                "--agent",
                "1",
                "--tags",
                "pxl;lease-abc",
                "--description",
                "pxl-lease=abc pxl-expiry=123",
            ],
        )

    def test_lxc_create_stamps_tags_and_description_in_one_call(self) -> None:
        self.ssh.add(r"pct create 200")
        self.pve.lxc_create(
            200,
            ostemplate="local:vztmpl/debian.tar.zst",
            hostname="db",
            tags="pxl;lease-abc",
            description="pxl-lease=abc pxl-expiry=123",
        )
        self.assertEqual(
            self.ssh.calls[0]["argv"],
            [
                "pct",
                "create",
                "200",
                "local:vztmpl/debian.tar.zst",
                "--hostname",
                "db",
                "--tags",
                "pxl;lease-abc",
                "--description",
                "pxl-lease=abc pxl-expiry=123",
            ],
        )

    def test_lxc_create_falls_back_to_set_on_usage_error(self) -> None:
        self.ssh.add(
            r"pct create 200 .*--tags",
            returncode=1,
            stderr=b"Error: unknown option: tags\n",
            times=1,
        )
        self.ssh.add(r"pct create 200")
        self.ssh.add(r"pct set 200")
        self.pve.lxc_create(
            200,
            ostemplate="local:vztmpl/debian.tar.zst",
            hostname="db",
            tags="pxl;lease-abc",
            description="pxl-lease=abc pxl-expiry=123",
        )
        argvs = [call["argv"] for call in self.ssh.calls]
        self.assertEqual(
            argvs,
            [
                [
                    "pct",
                    "create",
                    "200",
                    "local:vztmpl/debian.tar.zst",
                    "--hostname",
                    "db",
                    "--tags",
                    "pxl;lease-abc",
                    "--description",
                    "pxl-lease=abc pxl-expiry=123",
                ],
                ["pct", "create", "200", "local:vztmpl/debian.tar.zst", "--hostname", "db"],
                [
                    "pct",
                    "set",
                    "200",
                    "--tags",
                    "pxl;lease-abc",
                    "--description",
                    "pxl-lease=abc pxl-expiry=123",
                ],
            ],
        )

    def test_lxc_create_non_usage_failure_raises_without_retry(self) -> None:
        self.ssh.add(r"pct create", returncode=1, stderr=b"template 'x' not found\n")
        with self.assertRaisesRegex(ProxmoxError, "template 'x' not found"):
            self.pve.lxc_create(
                201,
                ostemplate="local:vztmpl/x.tar.zst",
                hostname="bad",
                tags="pxl;lease-abc",
                description="pxl-lease=abc pxl-expiry=123",
            )
        self.assertEqual(len(self.ssh.calls), 1)

    def test_set_metadata_clone_start_stop_argv(self) -> None:
        self.ssh.add(r"qm start")
        self.ssh.add(r"qm stop")
        self.ssh.add(r"pct set 200")
        self.ssh.add(r"qm clone")
        self.ssh.add(r"pct clone")
        self.pve.start("qemu", 100)
        self.pve.stop("qemu", 100)
        self.pve.set_metadata("lxc", 200, description="pxl-lease=abc pxl-expiry=123")
        self.pve.clone("qemu", 9000, 100, name="netgw")
        self.pve.clone("lxc", 9001, 200, name="db")
        self.assertEqual(
            [call["argv"] for call in self.ssh.calls],
            [
                ["qm", "start", "100"],
                ["qm", "stop", "100"],
                ["pct", "set", "200", "--description", "pxl-lease=abc pxl-expiry=123"],
                ["qm", "clone", "9000", "100", "--name", "netgw"],
                ["pct", "clone", "9001", "200", "--hostname", "db"],
            ],
        )

    def test_destroy_uses_purge_for_qemu_only(self) -> None:
        self.ssh.add(r"qm destroy 100 --purge")
        self.ssh.add(r"qm destroy 101")
        self.ssh.add(r"pct destroy 200")
        self.pve.destroy("qemu", 100)
        self.pve.destroy("qemu", 101, purge=False)
        self.pve.destroy("lxc", 200)
        self.assertEqual(
            [call["argv"] for call in self.ssh.calls],
            [
                ["qm", "destroy", "100", "--purge", "1"],
                ["qm", "destroy", "101"],
                ["pct", "destroy", "200"],
            ],
        )

    def test_unknown_kind_is_rejected(self) -> None:
        with self.assertRaisesRegex(ProxmoxError, "unknown guest kind"):
            self.pve.start("bogus", 1)
        self.assertEqual(self.ssh.calls, [])

    # -- status, shutdown, probes ------------------------------------------

    def test_status_parses_state_line(self) -> None:
        self.ssh.add(r"qm status 100", stdout=b"status: running\n", times=1)
        self.ssh.add(r"qm status 100", stdout=b"lock: suspended\n")
        self.assertEqual(self.pve.status("qemu", 100), "running")
        with self.assertRaisesRegex(ProxmoxError, "no status line"):
            self.pve.status("qemu", 100)

    def test_shutdown_lxc_with_timeout_polls_until_stopped(self) -> None:
        self.ssh.add(r"pct shutdown 200 --timeout")
        self.ssh.add(r"pct status 200", stdout=b"status: stopped\n")
        self.assertTrue(self.pve.shutdown("lxc", 200))
        self.assertEqual(
            [call["argv"] for call in self.ssh.calls],
            [
                ["pct", "shutdown", "200", "--timeout", "120"],
                ["pct", "status", "200"],
            ],
        )
        self.assertEqual(self.sleeps, [])

    def test_shutdown_lxc_usage_error_falls_back_to_plain(self) -> None:
        self.ssh.add(
            r"pct shutdown 200 --timeout",
            returncode=1,
            stderr=b"Error: unrecognized option '--timeout'\nUsage: pct shutdown <vmid>\n",
            times=1,
        )
        self.ssh.add(r"pct shutdown 200")
        self.ssh.add(r"pct status 200", stdout=b"status: running\n", times=1)
        self.ssh.add(r"pct status 200", stdout=b"status: stopped\n")
        self.assertTrue(self.pve.shutdown("lxc", 200, timeout=60.0))
        self.assertEqual(
            [call["argv"] for call in self.ssh.calls],
            [
                ["pct", "shutdown", "200", "--timeout", "60"],
                ["pct", "shutdown", "200"],
                ["pct", "status", "200"],
                ["pct", "status", "200"],
            ],
        )
        self.assertEqual(self.sleeps, [2.0])

    def test_shutdown_deadline_reports_false(self) -> None:
        self.ssh.add(r"pct shutdown")
        self.ssh.add(r"pct status 200", stdout=b"status: running\n")
        self.assertFalse(self.pve.shutdown("lxc", 200, timeout=0.0))
        self.assertEqual(len(self.ssh.calls), 2)  # shutdown + one status poll
        self.assertEqual(self.sleeps, [])

    def test_shutdown_qemu_never_retries_and_is_judged_by_status(self) -> None:
        self.ssh.add(r"qm shutdown 100", returncode=1, stderr=b"VM 100 not running\n")
        self.ssh.add(r"qm status 100", stdout=b"status: stopped\n")
        self.assertTrue(self.pve.shutdown("qemu", 100))
        self.assertEqual(self.ssh.calls[0]["argv"], ["qm", "shutdown", "100", "--timeout", "120"])
        self.assertEqual(len(self.ssh.calls), 2)

    def test_guest_ping_reports_agent_channel(self) -> None:
        self.ssh.add(r"qm guest ping 100", times=1)
        self.ssh.add(r"qm guest ping 100", returncode=1, stderr=b"agent not running\n")
        self.assertTrue(self.pve.guest_ping(100))
        self.assertFalse(self.pve.guest_ping(100))

    def test_guest_ip_skips_loopback_and_returns_first_ipv4(self) -> None:
        interfaces = [
            {
                "name": "lo",
                "ip-addresses": [{"ip-address": "127.0.0.1", "ip-address-type": "ipv4"}],
            },
            {
                "name": "eth0",
                "ip-addresses": [
                    {"ip-address": "fe80::1", "ip-address-type": "ipv6"},
                    {"ip-address": "10.0.0.5", "ip-address-type": "ipv4"},
                ],
            },
        ]
        self.ssh.add(r"network-get-interfaces", stdout=_j(interfaces), times=1)
        self.ssh.add(r"network-get-interfaces", stdout=_j([interfaces[0]]))
        self.assertEqual(self.pve.guest_ip(100), "10.0.0.5")
        self.assertIsNone(self.pve.guest_ip(100))

    def test_lxc_interfaces_reads_the_container_list(self) -> None:
        self.ssh.add(
            r"pvesh get /nodes/pve1/lxc/200/interfaces",
            stdout=_j([{"name": "eth0", "inet": "10.2.0.4/24"}]),
        )
        self.assertEqual(self.pve.lxc_interfaces(200)[0]["inet"], "10.2.0.4/24")
        self.assertEqual(
            self.ssh.calls[0]["argv"],
            [
                "pvesh", "get", "/nodes/pve1/lxc/200/interfaces",
                "--output-format", "json",
            ],
        )

    def test_storage_content_names_the_content_type(self) -> None:
        self.ssh.add(
            r"pvesh get /nodes/pve1/storage/local/content",
            stdout=_j([{"volid": "local:iso/debian.iso", "content": "iso"}]),
        )
        rows = self.pve.storage_content("local", "iso")
        self.assertEqual(rows[0]["volid"], "local:iso/debian.iso")
        self.assertIn("--content", self.ssh.calls[0]["argv"])
        self.assertIn("iso", self.ssh.calls[0]["argv"])

    # -- guest execution ----------------------------------------------------

    def test_guest_exec_synchronous_decodes_agent_payload(self) -> None:
        self.ssh.add(
            r"qm guest exec 100 --synchronous",
            stdout=_agent_reply(3, b"hello\n", b"oops\n"),
        )
        result = self.pve.guest_exec(100, ["echo", "hi"], stdin=b"payload")
        self.assertEqual(result, ExecResult(3, b"hello\n", b"oops\n"))
        self.assertFalse(result.ok)
        call = self.ssh.calls[0]
        self.assertEqual(
            call["argv"],
            [
                "qm",
                "guest",
                "exec",
                "100",
                "--synchronous",
                "--timeout",
                "120",
                "--",
                "echo",
                "hi",
            ],
        )
        self.assertEqual(call["stdin"], b"payload")
        self.assertEqual(call["timeout"], 120.0)

    def test_guest_exec_falls_back_to_async_when_synchronous_rejected(self) -> None:
        self.ssh.add(
            r"--synchronous",
            returncode=1,
            stderr=b"Error: unknown option '--synchronous'\n",
            times=1,
        )
        self.ssh.add(r"qm guest exec 100 -- echo", stdout=_j({"pid": 4242}))
        self.ssh.add(r"exec-status 100 4242", stdout=_j({"exited": False}), times=1)
        self.ssh.add(
            r"exec-status 100 4242",
            stdout=_j(
                {
                    "exited": True,
                    "exitcode": 0,
                    "out-data": base64.b64encode(b"done").decode(),
                    "err-data": base64.b64encode(b"").decode(),
                }
            ),
        )
        result = self.pve.guest_exec(100, ["echo", "hi"])
        self.assertTrue(result.ok)
        self.assertEqual(result.stdout, b"done")
        self.assertEqual(result.stderr, b"")
        self.assertEqual(
            [call["argv"] for call in self.ssh.calls],
            [
                [
                    "qm",
                    "guest",
                    "exec",
                    "100",
                    "--synchronous",
                    "--timeout",
                    "120",
                    "--",
                    "echo",
                    "hi",
                ],
                ["qm", "guest", "exec", "100", "--", "echo", "hi"],
                ["qm", "guest", "exec-status", "100", "4242"],
                ["qm", "guest", "exec-status", "100", "4242"],
            ],
        )
        self.assertEqual(self.sleeps, [2.0])

    def test_guest_exec_async_poll_deadline_raises(self) -> None:
        self.ssh.add(
            r"--synchronous",
            returncode=1,
            stderr=b"Error: unknown option '--synchronous'\n",
            times=1,
        )
        self.ssh.add(r"qm guest exec 100 -- sleep", stdout=_j({"pid": 7}))
        self.ssh.add(r"exec-status 100 7", stdout=_j({"exited": False}))
        with self.assertRaisesRegex(ProxmoxError, "did not finish"):
            self.pve.guest_exec(100, ["sleep", "600"], timeout=0.0)
        self.assertEqual(len(self.ssh.calls), 3)

    def test_guest_exec_non_usage_failure_raises_without_fallback(self) -> None:
        self.ssh.add(
            r"qm guest exec 100 --synchronous",
            returncode=1,
            stderr=b"QEMU guest agent is not running\n",
        )
        with self.assertRaisesRegex(ProxmoxError, "QEMU guest agent is not running"):
            self.pve.guest_exec(100, ["true"])
        self.assertEqual(len(self.ssh.calls), 1)

    def test_pct_exec_maps_real_exit_code(self) -> None:
        self.ssh.add(r"pct exec 200", returncode=7, stdout=b"out", stderr=b"err", times=1)
        self.ssh.add(r"pct exec 200", returncode=0, stdout=b"fine")
        failed = self.pve.pct_exec(200, ["false"])
        self.assertEqual(failed, ExecResult(7, b"out", b"err"))
        self.assertFalse(failed.ok)
        passed = self.pve.pct_exec(200, ["true"])
        self.assertTrue(passed.ok)
        self.assertEqual(
            [call["argv"] for call in self.ssh.calls],
            [
                ["pct", "exec", "200", "--", "false"],
                ["pct", "exec", "200", "--", "true"],
            ],
        )

    # -- chunked transfer (guest-exec base64 only) --------------------------

    def test_push_bytes_chunks_truncates_then_appends_and_verifies(self) -> None:
        data = b"0123456789"
        digest = hashlib.sha256(data).hexdigest()
        self.ssh.add(r"base64 -d >>")  # append chunks
        self.ssh.add(r"base64 -d >", times=1)  # first chunk truncates
        self.ssh.add(r"sha256sum", stdout=f"{digest}  /tmp/pxl-a.bin\n".encode())
        self.assertEqual(
            self.pve.push_bytes("lxc", 200, "/tmp/pxl-a.bin", data, chunk=4), digest
        )
        self.assertEqual(
            [call["argv"] for call in self.ssh.calls],
            [
                ["pct", "exec", "200", "--", "sh", "-c", "base64 -d > /tmp/pxl-a.bin"],
                ["pct", "exec", "200", "--", "sh", "-c", "base64 -d >> /tmp/pxl-a.bin"],
                ["pct", "exec", "200", "--", "sh", "-c", "base64 -d >> /tmp/pxl-a.bin"],
                ["pct", "exec", "200", "--", "sha256sum", "/tmp/pxl-a.bin"],
            ],
        )
        self.assertEqual(
            [call["stdin"] for call in self.ssh.calls[:3]],
            [base64.b64encode(data[0:4]), base64.b64encode(data[4:8]), base64.b64encode(data[8:10])],
        )
        joined = " ".join(" ".join(call["argv"]) for call in self.ssh.calls)
        self.assertNotIn("file-write", joined)
        self.assertNotIn("pct push", joined)

    def test_push_bytes_digest_mismatch_raises(self) -> None:
        self.ssh.add(r"base64 -d >", returncode=0, times=1)
        self.ssh.add(r"sha256sum", stdout=b"00  /tmp/pxl-b.bin\n")
        with self.assertRaisesRegex(ProxmoxError, "sha256 mismatch"):
            self.pve.push_bytes("lxc", 200, "/tmp/pxl-b.bin", b"x", chunk=4)
        self.assertEqual(len(self.ssh.calls), 2)

    def test_pull_bytes_reassembles_chunks_via_dd_and_verifies(self) -> None:
        digest = hashlib.sha256(b"0123456789").hexdigest()

        def chunk_reply(piece: bytes) -> bytes:
            return _agent_reply(0, base64.b64encode(piece) + b"\n")

        self.ssh.add(r"skip=0 count", stdout=chunk_reply(b"0123"), times=1)
        self.ssh.add(r"skip=1 count", stdout=chunk_reply(b"4567"), times=1)
        self.ssh.add(r"skip=2 count", stdout=chunk_reply(b"89"), times=1)
        self.ssh.add(
            r"sha256sum",
            stdout=_agent_reply(0, f"{digest}  /tmp/pxl-a.bin\n".encode()),
        )
        self.assertEqual(
            self.pve.pull_bytes("qemu", 100, "/tmp/pxl-a.bin", chunk=4),
            b"0123456789",
        )
        head = ["qm", "guest", "exec", "100", "--synchronous", "--timeout", "120", "--"]
        self.assertEqual(
            [call["argv"] for call in self.ssh.calls],
            [
                head
                + [
                    "sh",
                    "-c",
                    "dd if=/tmp/pxl-a.bin bs=4 skip=0 count=1 2>/dev/null | base64",
                ],
                head
                + [
                    "sh",
                    "-c",
                    "dd if=/tmp/pxl-a.bin bs=4 skip=1 count=1 2>/dev/null | base64",
                ],
                head
                + [
                    "sh",
                    "-c",
                    "dd if=/tmp/pxl-a.bin bs=4 skip=2 count=1 2>/dev/null | base64",
                ],
                head + ["sha256sum", "/tmp/pxl-a.bin"],
            ],
        )

    def test_pull_bytes_digest_mismatch_raises(self) -> None:
        self.ssh.add(
            r"skip=0 count",
            stdout=_agent_reply(0, base64.b64encode(b"hi") + b"\n"),
            times=1,
        )
        self.ssh.add(r"sha256sum", stdout=_agent_reply(0, b"ab  /tmp/pxl-c.bin\n"))
        with self.assertRaisesRegex(ProxmoxError, "sha256 mismatch"):
            self.pve.pull_bytes("qemu", 100, "/tmp/pxl-c.bin", chunk=4)
        self.assertEqual(len(self.ssh.calls), 2)


if __name__ == "__main__":
    unittest.main()
