"""Offline guards for experimental OCI-to-LXC provisioning."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

SRC = Path(__file__).parents[1] / "src"
sys.path.insert(0, str(SRC))

from proxmox_agent_lab import oci as lab_oci  # noqa: E402


class _Lock:
    def __enter__(self) -> "_Lock":
        return self

    def __exit__(self, *_: object) -> None:
        return None


def _lab(tmp: str) -> mock.Mock:
    lab = mock.Mock()
    lab.LabError = RuntimeError
    lab.NODE = "node1"
    lab.controller_lock.return_value = _Lock()
    lab.is_long_term.return_value = False
    lab.load_lease.return_value = {
        "id": "L1",
        "initial_vmids": [],
        "resources": [],
    }
    return lab


def _args(lab: mock.Mock, *argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    lab_oci.register(parser.add_subparsers(), lab)
    return parser.parse_args(list(argv))


class OciPullTests(unittest.TestCase):
    def test_pull_requires_host_storage_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lab = _lab(tmp)
            args = _args(
                lab, "oci", "pull", "--lease", "L1",
                "--reference", "docker.io/library/busybox:1.37.0",
                "--allow-mutable-reference",
            )
            with self.assertRaisesRegex(RuntimeError, "host template storage"):
                lab_oci.cmd_pull(lab, args)
            lab.ProxmoxAPI.assert_not_called()

    def test_pull_requires_explicit_mutable_reference_consent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lab = _lab(tmp)
            args = _args(
                lab, "oci", "pull", "--lease", "L1",
                "--reference", "docker.io/library/busybox:1.37.0",
                "--host-change-authorized",
            )
            with self.assertRaisesRegex(RuntimeError, "mutable tags"):
                lab_oci.cmd_pull(lab, args)
            lab.ProxmoxAPI.assert_not_called()

    def test_pull_rejects_digest_reference_that_pve_cannot_use(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lab = _lab(tmp)
            args = _args(
                lab, "oci", "pull", "--lease", "L1",
                "--reference", "docker.io/library/busybox@sha256:" + "a" * 64,
                "--host-change-authorized", "--allow-mutable-reference",
            )
            with self.assertRaisesRegex(RuntimeError, "does not accept digest"):
                lab_oci.cmd_pull(lab, args)
            lab.ProxmoxAPI.assert_not_called()

    def test_pull_audits_mutable_image_and_returns_template_volume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lab = _lab(tmp)
            api = mock.Mock()
            api.call.side_effect = [[], "UPID:node1:pull"]
            lab.ProxmoxAPI.return_value = api
            lab.wait_task.return_value = {"exitstatus": "OK"}
            args = _args(
                lab, "oci", "pull", "--lease", "L1",
                "--reference", "docker.io/library/busybox:1.37.0",
                "--host-change-authorized", "--allow-mutable-reference",
            )
            lab_oci.cmd_pull(lab, args)

            api.call.assert_has_calls([
                mock.call("GET", "/nodes/node1/storage/local/content"),
                mock.call(
                    "POST", "/nodes/node1/storage/local/oci-registry-pull",
                    {"reference": "docker.io/library/busybox:1.37.0"},
                ),
            ])
            lab.wait_task.assert_called_once_with(api, "UPID:node1:pull", timeout=1800)
            self.assertEqual(lab.audit.call_args_list[0].args[0], "oci-pull-intent")
            self.assertEqual(lab.audit.call_args_list[1].args[0], "oci-pulled")
            self.assertTrue(lab.audit.call_args_list[1].kwargs["mutable_reference"])

    def test_pull_refuses_to_overwrite_existing_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lab = _lab(tmp)
            api = mock.Mock()
            api.call.return_value = [{
                "volid": "local:vztmpl/busybox_1.37.0.tar",
            }]
            lab.ProxmoxAPI.return_value = api
            args = _args(
                lab, "oci", "pull", "--lease", "L1",
                "--reference", "docker.io/library/busybox:1.37.0",
                "--host-change-authorized", "--allow-mutable-reference",
            )
            with self.assertRaisesRegex(RuntimeError, "refusing to overwrite"):
                lab_oci.cmd_pull(lab, args)
            self.assertEqual(api.call.call_count, 1)
            lab.audit.assert_not_called()


class OciReferenceGrammarTests(unittest.TestCase):
    def test_accepts_separators_in_every_repository_component(self) -> None:
        for reference in (
            "ghcr.io/home-assistant/home-assistant:stable",
            "docker.io/hello-world:latest",
            "registry.example.com:5000/my-app:1.0",
            "my_image:latest",
        ):
            with self.subTest(reference=reference):
                self.assertIsNotNone(
                    lab_oci._REFERENCE_RE.fullmatch(reference)
                )

    def test_rejects_malformed_references(self) -> None:
        for reference in (
            "Busybox:latest",       # upper-case repository
            "busybox",              # missing tag
            "busybox:",             # empty tag
            "busybox:é",       # non-ASCII tag
            "ghcr.io/-bad/name:tag",  # component starts with separator
            "img:" + "t" * 129,     # tag longer than 128 characters
        ):
            with self.subTest(reference=reference):
                self.assertIsNone(
                    lab_oci._REFERENCE_RE.fullmatch(reference)
                )


class OciValidateTests(unittest.TestCase):
    def test_validate_is_offline_and_reports_template_volume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lab = _lab(tmp)
            args = _args(
                lab, "oci", "validate",
                "--reference", "ghcr.io/home-assistant/home-assistant:stable",
            )
            with mock.patch("builtins.print") as printed:
                lab_oci.cmd_validate(lab, args)
            lab.ProxmoxAPI.assert_not_called()
            lab.audit.assert_not_called()
            output = printed.call_args[0][0]
            self.assertIn(
                '"template": "local:vztmpl/home-assistant_stable.tar"', output
            )

    def test_validate_rejects_digest_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lab = _lab(tmp)
            args = _args(
                lab, "oci", "validate",
                "--reference", "docker.io/library/busybox@sha256:" + "a" * 64,
            )
            with self.assertRaisesRegex(RuntimeError, "does not accept digest"):
                lab_oci.cmd_validate(lab, args)


class OciCreateTests(unittest.TestCase):
    def test_create_refuses_long_term_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lab = _lab(tmp)
            lab.is_long_term.return_value = True
            args = _args(
                lab, "oci", "create", "--lease", "L1", "--vmid", "44",
                "--template", "local:vztmpl/busybox_1.37.0.tar",
                "--rootfs-storage", "local-lvm", "--disk-gb", "1",
            )
            with self.assertRaisesRegex(RuntimeError, "ordinary leases only"):
                lab_oci.cmd_create(lab, args)
            lab.ProxmoxAPI.assert_not_called()

    def test_create_refuses_vmid_present_before_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lab = _lab(tmp)
            lab.load_lease.return_value["initial_vmids"] = [44]
            args = _args(
                lab, "oci", "create", "--lease", "L1", "--vmid", "44",
                "--template", "local:vztmpl/busybox_1.37.0.tar",
                "--rootfs-storage", "local-lvm", "--disk-gb", "1",
            )
            with self.assertRaisesRegex(RuntimeError, "existed before this lease"):
                lab_oci.cmd_create(lab, args)
            lab.ProxmoxAPI.assert_not_called()

    def test_create_is_unprivileged_registered_and_can_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lab = _lab(tmp)
            api = mock.Mock()
            api.call.side_effect = ["UPID:node1:create", "UPID:node1:start"]
            lab.ProxmoxAPI.return_value = api
            lab.wait_task.side_effect = [
                {"exitstatus": "OK", "type": "vzcreate"},
                {"exitstatus": "OK", "type": "vzstart"},
            ]
            args = _args(
                lab, "oci", "create", "--lease", "L1", "--vmid", "45",
                "--template", "local:vztmpl/busybox_1.37.0.tar",
                "--rootfs-storage", "local-lvm", "--disk-gb", "2",
                "--memory", "256", "--swap", "0", "--start",
            )
            lab_oci.cmd_create(lab, args)

            api.call.assert_has_calls([
                mock.call("POST", "/nodes/node1/lxc", {
                    "vmid": 45,
                    "hostname": "oci-45",
                    "ostemplate": "local:vztmpl/busybox_1.37.0.tar",
                    "rootfs": "local-lvm:2",
                    "memory": 256,
                    "swap": 0,
                    "unprivileged": 1,
                    "onboot": 0,
                    "tags": "codex-lab;lease-L1",
                }),
                mock.call("POST", "/nodes/node1/lxc/45/status/start"),
            ])
            lab.register_resource.assert_called_once_with(
                lab.load_lease.return_value, "lxc", 45, "delete", "oci-45"
            )
            self.assertEqual(lab.audit.call_args_list[0].args[0], "oci-create-intent")
            self.assertEqual(lab.audit.call_args_list[-1].args[0], "oci-created")

    def test_create_refuses_non_oci_template_volume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lab = _lab(tmp)
            args = _args(
                lab, "oci", "create", "--lease", "L1", "--vmid", "45",
                "--template", "local:vztmpl/debian-12.tar.zst",
                "--rootfs-storage", "local-lvm", "--disk-gb", "2",
            )
            with self.assertRaisesRegex(RuntimeError, "OCI template"):
                lab_oci.cmd_create(lab, args)
            lab.ProxmoxAPI.assert_not_called()


if __name__ == "__main__":
    unittest.main()
