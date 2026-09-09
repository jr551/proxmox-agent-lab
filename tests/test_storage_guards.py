"""Upload TLS, slow-storage and default storage guards."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import bootstrap  # noqa: E402,F401
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from proxmox_agent_lab import cli as LAB  # noqa: E402


class UploadTlsTests(unittest.TestCase):
    """Found live: upload always passed curl --insecure, so an operator who had
    turned certificate verification on still got an unverified upload."""

    def _argv(self, verify: bool) -> list[str]:
        with mock.patch.object(LAB, "VERIFY_TLS", verify):
            return LAB.upload_curl_argv(
                "/tmp/curl.conf", Path("/tmp/answer.iso"), "iso", "local"
            )

    def test_verification_on_means_no_insecure_flag(self) -> None:
        argv = self._argv(True)
        self.assertNotIn("--insecure", argv)
        self.assertIn("--config", argv)

    def test_the_self_signed_opt_out_is_still_available(self) -> None:
        self.assertIn("--insecure", self._argv(False))

    def test_the_token_is_never_in_the_argv(self) -> None:
        for verify in (True, False):
            joined = " ".join(self._argv(verify))
            self.assertNotIn("PVEAPIToken", joined)
            self.assertIn("/storage/local/upload", joined)


class SlowStorageGuardTests(unittest.TestCase):
    """Found live: both ReactOS benchmark guests had their disks on the USB
    bulk store, which measured ~25 MB/s -- the benchmark measured the cable."""

    def test_a_guest_disk_on_bulk_storage_is_flagged(self) -> None:
        with mock.patch.object(LAB.CONFIG.storage, "_values",
                               {"bulk_storage": "usb-bulk",
                                "upload_storages": ["local"]}):
            self.assertEqual(
                LAB.slow_storage_disks({"scsi0": "usb-bulk:100", "memory": "4096"}),
                ["scsi0=usb-bulk:100"],
            )

    def test_an_iso_mounted_from_bulk_storage_is_fine(self) -> None:
        """The docs recommend exactly this; only guest disks are the problem."""
        with mock.patch.object(LAB.CONFIG.storage, "_values",
                               {"bulk_storage": "usb-bulk"}):
            self.assertEqual(
                LAB.slow_storage_disks(
                    {"ide2": "usb-bulk:iso/reactos.iso,media=cdrom"}
                ),
                [],
            )

    def test_fast_storage_is_not_flagged(self) -> None:
        with mock.patch.object(LAB.CONFIG.storage, "_values",
                               {"bulk_storage": "usb-bulk"}):
            self.assertEqual(
                LAB.slow_storage_disks({"scsi0": "local-lvm:32"}), []
            )

    def test_every_disk_bus_is_covered(self) -> None:
        with mock.patch.object(LAB.CONFIG.storage, "_values",
                               {"bulk_storage": "usb-bulk"}):
            flagged = LAB.slow_storage_disks({
                "virtio0": "usb-bulk:32", "sata1": "usb-bulk:32",
                "ide0": "usb-bulk:32", "rootfs": "usb-bulk:8",
                "efidisk0": "usb-bulk:1", "net0": "virtio,bridge=vmbr1",
            })
        self.assertEqual(len(flagged), 5)
        self.assertNotIn("net0", " ".join(flagged))


class UploadStorageDefaultTests(unittest.TestCase):
    """Big images belong on bulk, not on the hypervisor's root filesystem.

    Found live: 50 GB of ISOs had accumulated on the Proxmox root filesystem,
    taking it to 96% full. A full root takes the hypervisor down with it, which
    is a much worse failure than a slow ISO read.
    """

    def _default(self, upload: tuple, bulk: str) -> str:
        """The rule cli.py applies at import time."""
        return bulk if bulk in upload else (upload[0] if upload else "local")

    def test_bulk_is_preferred_when_it_is_an_upload_target(self) -> None:
        self.assertEqual(
            self._default(("local", "usb-bulk"), "usb-bulk"), "usb-bulk"
        )

    def test_falls_back_when_bulk_is_not_an_upload_target(self) -> None:
        """A config that never allowed bulk uploads must still work."""
        self.assertEqual(self._default(("local",), "usb-bulk"), "local")

    def test_the_shipped_default_is_not_the_root_filesystem(self) -> None:
        """local is /var/lib/vz on the Proxmox root; with the fixture's
        config the default must actually be the bulk store."""
        self.assertIn(LAB.DEFAULT_UPLOAD_STORAGE, LAB.UPLOAD_STORAGES)
        self.assertNotEqual(LAB.DEFAULT_UPLOAD_STORAGE, "local")

    def test_the_parser_survives_an_empty_upload_storages_config(self) -> None:
        """choices=() would make argparse refuse every value including the
        default; with nothing configured the argument must stay usable and
        cmd_upload's own check has to produce the readable error."""
        with mock.patch.object(LAB, "UPLOAD_STORAGES", ()), \
             mock.patch.object(LAB, "DEFAULT_UPLOAD_STORAGE", "local"):
            args = LAB.parser().parse_args(
                ["upload", "--lease", "l", "--file", "/tmp/x.iso"]
            )
        self.assertEqual(args.storage, "local")


