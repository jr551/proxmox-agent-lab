"""The unattended Windows install must not depend on a human catching the UEFI
'press any key to boot from CD' prompt, so the auto-tap helper is guarded."""

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
import io  # noqa: E402
import sys  # noqa: E402
import re  # noqa: E402
import time  # noqa: E402
import unittest  # noqa: E402
from unittest import mock  # noqa: E402

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from proxmox_agent_lab import cli as LAB  # noqa: E402
from proxmox_agent_lab import windows  # noqa: E402


class WindowsTemplateTests(unittest.TestCase):
    def test_configured_template_is_used(self) -> None:
        self.assertEqual(windows._template_vmid(LAB, "2025"), 910)

    def test_command_line_override_wins(self) -> None:
        self.assertEqual(windows._template_vmid(LAB, "2025", 999), 999)

    def test_missing_template_is_refused_before_clone(self) -> None:
        from proxmox_agent_lab import config

        class UnconfiguredLab:
            CONFIG = config.defaults()
            LabError = RuntimeError

        with self.assertRaisesRegex(RuntimeError, "template_2022_vmid"):
            windows._template_vmid(UnconfiguredLab, "2022")

    def test_driver_branch_follows_the_selected_version(self) -> None:
        self.assertEqual(windows._driver_branch("2022"), "2k22")
        self.assertEqual(windows._driver_branch("2025"), "2k25")
        self.assertEqual(windows._driver_branch("2022", "custom"), "custom")


class BootPromptTests(unittest.TestCase):
    def test_taps_enter_across_the_boot_window(self) -> None:
        api = mock.Mock()
        with mock.patch("time.sleep"):
            sent = windows._tap_boot_prompt(LAB, api, 9040, taps=5, delay=0)
        self.assertEqual(sent, 5)
        # Every tap is an Enter (qemu qcode "ret") via the sendkey endpoint.
        for call in api.call.call_args_list:
            self.assertEqual(call.args[0], "PUT")
            self.assertTrue(call.args[1].endswith("/qemu/9040/sendkey"))
            self.assertEqual(call.args[2], {"key": "ret"})

    def test_a_sendkey_failure_never_fails_the_install(self) -> None:
        api = mock.Mock()
        api.call.side_effect = LAB.LabError("sendkey not permitted")
        with mock.patch("time.sleep"):
            sent = windows._tap_boot_prompt(LAB, api, 9040, taps=5, delay=0)
        # Swallowed: it stops rather than propagating, so the install proceeds.
        self.assertEqual(sent, 0)


if __name__ == "__main__":
    unittest.main()


class FakeAPI:
    """Counts sendkey presses and replays a scripted diskwrite series."""

    def __init__(self, writes: list[int]) -> None:
        self.writes = list(writes)
        self.keys = 0
        self.last_write = 0

    def call(self, method, path, data=None):
        if path.endswith("/sendkey"):
            self.keys += 1
            return {}
        if path.endswith("/status/current"):
            if self.writes:
                self.last_write = self.writes.pop(0)
            return {"diskwrite": self.last_write}
        return {}


class FakeLab:
    NODE = "pve"
    LabError = RuntimeError


class DismissSetupUITests(unittest.TestCase):
    """Setup's language page waits forever; one Enter gets past it."""

    def test_it_presses_until_the_disk_starts_filling(self) -> None:
        installing = windows.STALL_WRITE_BYTES * 2
        api = FakeAPI([0, 0, 0, installing, installing])
        sent = windows._dismiss_setup_ui(FakeLab(), api, 9060, budget=60, delay=0)
        self.assertGreater(sent, 0)
        self.assertLessEqual(sent, 8)

    def test_it_sends_the_next_buttons_accelerator(self) -> None:
        """Enter is swallowed by the focused combo box; Alt+N is not."""
        installing = windows.STALL_WRITE_BYTES * 2
        # baseline, then one quiet poll, then it starts installing
        api = FakeAPI([0, 0, installing, installing])
        api.keys_sent = []
        original = api.call

        def record(method, path, data=None):
            if path.endswith("/sendkey"):
                api.keys_sent.append(data["key"])
            return original(method, path, data)

        api.call = record
        windows._dismiss_setup_ui(FakeLab(), api, 9060, budget=60, delay=0)
        self.assertIn("alt-n", api.keys_sent)

    def test_it_never_presses_at_a_running_installer(self) -> None:
        """A guest already writing gigabytes must not be typed at."""
        api = FakeAPI([0, windows.STALL_WRITE_BYTES * 10])
        sent = windows._dismiss_setup_ui(FakeLab(), api, 9060, budget=60, delay=0)
        self.assertEqual(sent, 0)

    def test_it_gives_up_inside_its_budget(self) -> None:
        """A guest that never starts installing must not hang the install."""
        api = FakeAPI([])  # diskwrite stays 0 forever
        started = time.monotonic()
        windows._dismiss_setup_ui(FakeLab(), api, 9060, budget=0.2, delay=0.01)
        self.assertLess(time.monotonic() - started, 5)

    def test_a_broken_api_cannot_fail_the_install(self) -> None:
        class Broken(FakeAPI):
            def call(self, method, path, data=None):
                if path.endswith("/sendkey"):
                    raise FakeLab.LabError("gone")
                return {"diskwrite": 0}
        sent = windows._dismiss_setup_ui(FakeLab(), Broken([]), 9060,
                                         budget=5, delay=0)
        self.assertEqual(sent, 0)


class AnswerIsoShredTests(unittest.TestCase):
    """autounattend.xml holds the Administrator password in plain text."""

    class API:
        def __init__(self, config):
            self.config = config
            self.calls = []

        def call(self, method, path, data=None):
            self.calls.append((method, path, data))
            if method == "GET" and path.endswith("/config"):
                return self.config
            return {}

    def test_it_detaches_and_deletes_this_guests_answer_iso(self) -> None:
        api = self.API({
            "sata2": "local:iso/autounattend-9060.iso,media=cdrom,size=900K",
            "scsi0": "local-lvm:vm-9060-disk-0,size=80G",
        })
        volume = windows._shred_answer_iso(FakeLab(), api, 9060)
        self.assertEqual(volume, "local:iso/autounattend-9060.iso")
        self.assertIn(("PUT", "/nodes/pve/qemu/9060/config", {"delete": "sata2"}),
                      api.calls)
        self.assertIn(
            ("DELETE",
             "/nodes/pve/storage/local/content/local:iso/autounattend-9060.iso",
             None),
            api.calls)

    def test_it_leaves_another_guests_answer_iso_alone(self) -> None:
        api = self.API({
            "sata2": "local:iso/autounattend-9041.iso,media=cdrom,size=900K"})
        self.assertIsNone(windows._shred_answer_iso(FakeLab(), api, 9060))
        self.assertEqual([c for c in api.calls if c[0] != "GET"], [])

    def test_it_leaves_the_windows_installer_iso_alone(self) -> None:
        api = self.API({
            "sata0": "local:iso/windows-server-2022-eval-en-us.iso,media=cdrom",
            "sata1": "local:iso/virtio-win-0.1.285.iso,media=cdrom"})
        self.assertIsNone(windows._shred_answer_iso(FakeLab(), api, 9060))
        self.assertEqual([c for c in api.calls if c[0] != "GET"], [])

    def test_a_failed_delete_does_not_raise(self) -> None:
        class Broken(self.API):
            def call(self, method, path, data=None):
                if method == "GET":
                    return self.config
                raise FakeLab.LabError("denied")
        api = Broken({"sata2": "local:iso/autounattend-9060.iso,media=cdrom"})
        self.assertIsNone(windows._shred_answer_iso(FakeLab(), api, 9060))


class WaitAgentStallTests(unittest.TestCase):
    """--stall-after 0 must disable the check, not trigger it immediately."""

    class Args:
        def __init__(self, stall_after):
            self.vmid = 9060
            self.timeout = 0.3
            self.interval = 0.01
            self.stall_after = stall_after

    class Lab:
        NODE = "pve"
        LabError = RuntimeError

        @staticmethod
        def ProxmoxAPI():
            class API:
                def call(self, method, path, data=None):
                    return {"diskwrite": 0}
            return API()

    def _run(self, stall_after):
        import proxmox_agent_lab.console as console_mod
        original = console_mod.agent_ready
        console_mod.agent_ready = lambda *a, **k: False
        try:
            with self.assertRaises(RuntimeError) as caught:
                windows.cmd_wait_agent(self.Lab(), self.Args(stall_after))
            return str(caught.exception)
        finally:
            console_mod.agent_ready = original

    def test_a_quiet_guest_is_warned_about_but_never_abandoned(self) -> None:
        """The heuristic misfires on a healthy first boot, so it only warns."""
        import contextlib, io
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            message = self._run(0.01)
        self.assertIn("did not respond within", message)   # ran to timeout
        self.assertIn("may be waiting for input", captured.getvalue())

    def test_zero_disables_the_warning_entirely(self) -> None:
        import contextlib, io
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            self._run(0)
        self.assertEqual(captured.getvalue(), "")


class FirstLogonCommandTests(unittest.TestCase):
    """Setup runs SynchronousCommands by Order; duplicates or gaps misbehave."""

    def _block(self):
        xml = windows.render_unattend(
            admin_password="p", hostname="H", locale="en-US", timezone="UTC",
            owner="o", image_index=2, driver_branch="2k22")
        return xml[xml.index("<FirstLogonCommands>"):
                   xml.index("</FirstLogonCommands>")]

    def test_orders_are_a_clean_sequence(self) -> None:
        orders = [int(o) for o in re.findall(r"<Order>(\d+)</Order>",
                                            self._block())]
        self.assertEqual(orders, list(range(1, len(orders) + 1)))

    def test_the_signing_ca_is_trusted_before_the_msi_runs(self) -> None:
        """A silent install from an untrusted publisher has nobody to approve."""
        block = self._block()
        self.assertLess(block.index("Virtio_Win_Red_Hat_CA"),
                        block.index("qemu-ga-x86_64.msi"))

    def test_no_drive_letter_is_hardcoded_for_the_msi(self) -> None:
        self.assertNotIn("E:\\guest-agent", self._block())


class DriverPathTests(unittest.TestCase):
    """The guest agent is useless without a channel to talk on."""

    def _paths(self):
        xml = windows.render_unattend(
            admin_password="p", hostname="H", locale="en-US", timezone="UTC",
            owner="o", image_index=2, driver_branch="2k22")
        return re.findall(r"<Path>([^<]+)</Path>", xml)

    def test_vioserial_is_injected(self) -> None:
        """Measured: without it, QEMU-GA runs but Proxmox cannot reach it."""
        self.assertIn("E:\\vioserial\\2k22\\amd64", self._paths())

    def test_every_driver_needed_to_boot_and_talk_is_present(self) -> None:
        paths = " ".join(self._paths())
        for driver in ("vioscsi", "NetKVM", "Balloon", "vioserial"):
            self.assertIn(driver, paths)

    def test_driver_keys_are_unique(self) -> None:
        xml = windows.render_unattend(
            admin_password="p", hostname="H", locale="en-US", timezone="UTC",
            owner="o", image_index=2, driver_branch="2k22")
        keys = re.findall(r'wcm:keyValue="(\d+)"', xml)
        self.assertEqual(len(keys), len(set(keys)))


class AdministratorPasswordTests(unittest.TestCase):
    """The answer file *creates* the account, so an empty password is refused.

    That is deliberately the opposite of a guest console login, where an empty
    password is a fact about a guest that already has none.
    """

    def _args(self, **overrides: object) -> mock.Mock:
        defaults = dict(
            lease="L1", vmid=9060, version="2022", template_vmid=None,
            name=None, driver_branch=None, unattended=True,
            password_stdin=True,
        )
        defaults.update(overrides)
        return mock.Mock(**defaults)

    def _lab(self) -> mock.Mock:
        lab = mock.Mock()
        lab.LabError = RuntimeError
        lab.NODE = "aipve"
        lab.CONFIG = LAB.CONFIG
        lab.load_lease.return_value = {"resources": [], "initial_vmids": []}
        return lab

    def test_an_empty_password_is_refused_before_the_clone(self) -> None:
        lab = self._lab()
        with mock.patch.object(windows, "_clone") as clone, \
             mock.patch.object(sys, "stdin", io.StringIO("\n")):
            with self.assertRaises(RuntimeError) as caught:
                windows.cmd_install(lab, self._args())
        self.assertIn("empty Administrator password", str(caught.exception))
        clone.assert_not_called()
        lab.register_resource.assert_not_called()

    def test_omitting_the_flag_still_generates_a_password(self) -> None:
        lab = self._lab()
        with mock.patch.object(windows, "_clone",
                               side_effect=RuntimeError("clone reached")), \
             mock.patch.object(sys, "stdin", io.StringIO("")):
            with self.assertRaises(RuntimeError) as caught:
                windows.cmd_install(lab, self._args(password_stdin=False))
        self.assertEqual(str(caught.exception), "clone reached")
