"""Offline tests for the optional DHCP/TFTP server spawners."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

SRC = Path(__file__).parents[1] / "src"
import sys

sys.path.insert(0, str(SRC))

import base64
import io

from proxmox_agent_lab import console as lab_console  # noqa: E402
from proxmox_agent_lab import netgw as lab_netgw  # noqa: E402


def _lab(tmp: str) -> mock.Mock:
    lab = mock.Mock()
    lab.LabError = RuntimeError
    lab.NODE = "aipve"
    lab.load_lease.return_value = {
        "resources": [],
        "initial_vmids": [100, 101, 102],
    }
    lab.wait_task = mock.Mock()
    lab.register_resource = mock.Mock()
    return lab


class DhcpTftpTests(unittest.TestCase):
    def _api(self) -> mock.Mock:
        api = mock.Mock()
        api.call.return_value = {"status": "stopped"}
        return api

    def test_dhcp_create_clones_configures_and_provisions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lab = _lab(tmp)
            api = self._api()
            lab.ProxmoxAPI.return_value = api
            with mock.patch.object(lab_netgw, "require_bridge"), \
                 mock.patch.object(
                     lab_console, "agent_ready", return_value=True
                 ), \
                 mock.patch.object(
                     lab_netgw, "_exec",
                     return_value={"exitcode": 0, "stdout": "provisioned",
                                   "stderr": ""},
                 ) as execute:
                lab_netgw.cmd_dhcp_create(
                    lab, _args(lab, "net", "dhcp-create",
                               "--lease", "L1", "--vmid", "9100")
                )
            paths = [c.args[1] for c in api.call.call_args_list]
            self.assertIn(
                f"/nodes/aipve/qemu/{lab_netgw.GATEWAY_TEMPLATE_VMID}/clone",
                paths,
            )
            written = {
                c.args[2].get("file", ""): c.args[2].get("content", "")
                for c in api.call.call_args_list
                if c.args[1].endswith("/agent/file-write")
            }
            self.assertIn("/tmp/lab-server.conf", written)
            self.assertIn("/tmp/lab-server-provision.sh", written)
            conf = base64.b64decode(
                written.get("/tmp/lab-server.conf", "")
            ).decode()
            self.assertIn("dhcp-range=", conf)
            self.assertIn("interface=__LAB_IF__", conf)
            provision = base64.b64decode(
                written.get("/tmp/lab-server-provision.sh", "")
            ).decode()
            self.assertIn("ss -ulnp | grep -q ':67 '", provision)
            self.assertEqual(
                api.call.call_args_list[-1].args[2],
                {"delete": "cipassword"},
            )
            tagged = [
                c.args[2] for c in api.call.call_args_list
                if len(c.args) > 2 and isinstance(c.args[2], dict)
                and "tags" in c.args[2]
            ]
            self.assertTrue(tagged)
            self.assertEqual(tagged[0]["tags"], "codex-lab;lease-L1;dhcp")
            self.assertIn("ipconfig0", tagged[0])
            lab.register_resource.assert_called_once()
            lab.audit.assert_called_once()

    def test_dhcp_create_pxe_bootfile_adds_dhcp_boot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lab = _lab(tmp)
            api = self._api()
            lab.ProxmoxAPI.return_value = api
            with mock.patch.object(lab_netgw, "require_bridge"), \
                 mock.patch.object(
                     lab_console, "agent_ready", return_value=True
                 ), \
                 mock.patch.object(
                     lab_netgw, "_exec",
                     return_value={"exitcode": 0, "stdout": "provisioned",
                                   "stderr": ""},
                 ):
                lab_netgw.cmd_dhcp_create(
                    lab, _args(lab, "net", "dhcp-create",
                               "--lease", "L1", "--vmid", "9100",
                               "--bootfile", "pxelinux.0")
                )
            conf = ""
            for c in api.call.call_args_list:
                if c.args[1].endswith("/agent/file-write") and \
                        c.args[2].get("file", "").endswith("lab-server.conf"):
                    conf = base64.b64decode(
                        c.args[2].get("content", "")
                    ).decode()
            self.assertIn("dhcp-boot=pxelinux.0,10.66.0.3", conf)

    def test_tftp_push_verifies_size_and_rejects_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lab = _lab(tmp)
            api = self._api()
            lab.ProxmoxAPI.return_value = api
            source = Path(tmp) / "pxelinux.0"
            source.write_bytes(b"PXEBOOT" * 100)
            with mock.patch.object(
                lab_netgw, "_exec",
                return_value={"exitcode": 0,
                              "stdout": str(source.stat().st_size),
                              "stderr": ""},
            ):
                lab_netgw.cmd_tftp_push(
                    lab, _args(lab, "net", "tftp-push",
                               "--lease", "L1", "--vmid", "9100",
                               "--file", str(source))
                )
            lab.audit.assert_called_once()
            with self.assertRaises(RuntimeError):
                lab_netgw.cmd_tftp_push(
                    lab, _args(lab, "net", "tftp-push",
                               "--lease", "L1", "--vmid", "9100",
                               "--file", str(source),
                               "--name", "../evil.bin")
                )


class LeakTestPasswordTests(unittest.TestCase):
    """A stock lab image can have no console password; that is still a login."""

    def _run(self, stdin: str) -> mock.Mock:
        lab = _lab(tempfile.gettempdir())
        term = mock.MagicMock()
        term.__enter__.return_value = term
        term.run.return_value = "UNREACHABLE"
        with mock.patch.object(lab_netgw.console, "TermSession",
                               return_value=term), \
             mock.patch.object(lab_netgw, "_controller_public_ip",
                               return_value="203.0.113.9"), \
             mock.patch.object(sys, "stdin", io.StringIO(stdin)), \
             mock.patch("builtins.print"):
            # The fake guest reports no egress, so the check itself fails at
            # the end. Irrelevant here: the login has already happened.
            with self.assertRaises(RuntimeError):
                lab_netgw.cmd_leak_test(
                    lab,
                    _args(lab, "net", "leak-test", "--lease", "L1",
                          "--vmid", "9001", "--password-stdin",
                          "--no-install-tools"),
                )
        return term

    def test_an_explicit_empty_password_is_accepted(self) -> None:
        term = self._run("\n")
        term.login.assert_called_once_with("alpine", "")

    def test_a_real_password_is_still_forwarded(self) -> None:
        term = self._run("hunter2\n")
        term.login.assert_called_once_with("alpine", "hunter2")


def _args(lab: mock.Mock, *argv: str):
    import argparse

    parser = argparse.ArgumentParser()
    lab_netgw.register(parser.add_subparsers(), lab)
    return parser.parse_args(list(argv))


if __name__ == "__main__":
    unittest.main()
