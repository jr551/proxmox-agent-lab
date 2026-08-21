"""Device profiles and the provisioning scripts that realise them."""

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
import re  # noqa: E402
import sys  # noqa: E402
import unittest  # noqa: E402

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from proxmox_agent_lab import android  # noqa: E402


class ProfileTests(unittest.TestCase):
    def test_the_s20_matches_the_real_phone(self) -> None:
        s20 = android.profile("galaxy-s20")
        self.assertEqual((s20["width"], s20["height"]), (1440, 3200))
        self.assertEqual(s20["density"], 560)
        self.assertEqual(s20["model"], "SM-G980F")
        self.assertEqual(s20["manufacturer"], "samsung")

    def test_an_unknown_profile_lists_the_known_ones(self) -> None:
        with self.assertRaises(android.AndroidError) as caught:
            android.profile("nokia-3310")
        self.assertIn("galaxy-s20", str(caught.exception))

    def test_every_profile_is_complete(self) -> None:
        required = {"label", "width", "height", "density", "ram_mb",
                    "heap_mb", "storage_mb", "api", "model", "manufacturer",
                    "brand", "device"}
        for name, spec in android.PROFILES.items():
            self.assertEqual(required - set(spec), set(), f"{name} is missing keys")


class ScriptTests(unittest.TestCase):
    def test_no_placeholder_survives_rendering(self) -> None:
        """A leftover __TOKEN__ would reach the guest as literal shell."""
        spec = android.profile("galaxy-s20")
        for body in (android.setup_script(33, "x86_64"),
                     android.avd_script("galaxy-s20", spec, 33, "x86_64"),
                     android.launch_script("galaxy-s20", spec, 5037)):
            self.assertEqual(re.findall(r"__[A-Z_]+__", body), [])

    def test_a_missing_placeholder_is_caught_not_shipped(self) -> None:
        with self.assertRaises(android.AndroidError) as caught:
            android._script("01-install-sdk.sh", SDK="/opt")   # API, ABI absent
        self.assertIn("placeholders", str(caught.exception))

    def test_the_avd_is_pinned_to_the_device_geometry(self) -> None:
        spec = android.profile("galaxy-s20")
        body = android.avd_script("galaxy-s20", spec, 33, "x86_64")
        self.assertIn("hw.lcd.width 1440", body)
        self.assertIn("hw.lcd.height 3200", body)
        self.assertIn("hw.lcd.density 560", body)
        self.assertIn("hw.ramSize 8192", body)

    def test_the_emulator_reports_the_right_identity(self) -> None:
        """Apps read ro.product.model; a profile that skips this is a lie."""
        spec = android.profile("galaxy-s20")
        body = android.launch_script("galaxy-s20", spec, 5037)
        self.assertIn('ro.product.model="SM-G980F"', body)
        self.assertIn('ro.product.manufacturer="samsung"', body)

    def test_the_system_image_matches_the_requested_abi(self) -> None:
        for abi in ("x86_64", "arm64-v8a"):
            body = android.setup_script(33, abi)
            self.assertIn(f"system-images;android-33;google_apis;{abi}", body)

    def test_scripts_are_shipped_as_files(self) -> None:
        """They are meant to be readable and runnable by hand."""
        for name in ("01-install-sdk.sh", "02-create-avd.sh",
                     "03-launch-emulator.sh"):
            path = android.SCRIPTS / name
            self.assertTrue(path.is_file(), name)
            self.assertTrue(path.read_text().startswith("#!/bin/bash"), name)

    def test_the_emulator_draws_on_the_vm_console(self) -> None:
        """That is what lets console screenshot and share work unchanged."""
        spec = android.profile("galaxy-s20")
        body = android.launch_script("galaxy-s20", spec, 5037)
        self.assertIn("xinit", body)
        self.assertIn("pxl-android.service", body)


if __name__ == "__main__":
    unittest.main()


class ScriptSyntaxTests(unittest.TestCase):
    def test_rendered_scripts_are_valid_shell(self) -> None:
        """A broken line continuation shipped a literal backslash to apt,
        which read it as a package name. `bash -n` catches that here rather
        than three minutes into a build on a remote guest."""
        import subprocess
        import tempfile

        spec = android.profile("minimal")
        rendered = {
            "01-install-sdk.sh": android.setup_script(30, "x86_64", "default"),
            "02-create-avd.sh": android.avd_script("minimal", spec, 30, "x86_64"),
            "03-launch-emulator.sh": android.launch_script("minimal", spec, 5037),
        }
        for name, body in rendered.items():
            with tempfile.NamedTemporaryFile("w", suffix=".sh") as handle:
                handle.write(body)
                handle.flush()
                result = subprocess.run(["bash", "-n", handle.name],
                                        capture_output=True, text=True)
            self.assertEqual(result.returncode, 0,
                             f"{name}: {result.stderr[:200]}")

    def test_no_doubled_line_continuations(self) -> None:
        """`\\\\` at end of line is a literal backslash, not a continuation."""
        for path in android.SCRIPTS.glob("*.sh"):
            self.assertNotIn("\\\\\n", path.read_text(), path.name)


class FakeAPI:
    """Records the calls the template conversion makes, in order."""

    def __init__(self, status: dict) -> None:
        self.status = status
        self.calls: list[tuple[str, str]] = []

    def call(self, method: str, path: str, data: dict | None = None):
        self.calls.append((method, path))
        if path.endswith("/status/current"):
            return self.status
        return "UPID:fake"


class FakeLab:
    NODE = "pve"
    LabError = RuntimeError

    def __init__(self) -> None:
        self.saved: dict | None = None

    def wait_task(self, api, upid, timeout=None):
        return {"status": "OK"}

    def save_lease(self, lease):
        self.saved = lease


class TemplateTests(unittest.TestCase):
    """Templating a device that is already built, without a rebuild."""

    def _lease(self):
        return {"id": "L", "resources": [{"vmid": 9050, "policy": "delete"}]}

    def test_a_running_device_is_shut_down_before_templating(self) -> None:
        lab, api = FakeLab(), FakeAPI({"status": "running"})
        lease = self._lease()
        android._make_template(lab, api, lease, 9050)
        paths = [path for _, path in api.calls]
        self.assertLess(paths.index("/nodes/pve/qemu/9050/status/shutdown"),
                        paths.index("/nodes/pve/qemu/9050/template"))

    def test_a_stopped_device_is_not_shut_down_again(self) -> None:
        lab, api = FakeLab(), FakeAPI({"status": "stopped"})
        android._make_template(lab, api, lease := self._lease(), 9050)
        self.assertNotIn("/nodes/pve/qemu/9050/status/shutdown",
                         [path for _, path in api.calls])
        self.assertIn(("POST", "/nodes/pve/qemu/9050/template"), api.calls)
        self.assertEqual(lease["resources"][0]["policy"], "retain")

    def test_templating_survives_cleanup(self) -> None:
        """A template must not be deleted when the lease ends."""
        lab, api = FakeLab(), FakeAPI({"status": "stopped"})
        android._make_template(lab, api, self._lease(), 9050)
        self.assertEqual(lab.saved["resources"][0]["policy"], "retain")

    def test_an_existing_template_is_refused(self) -> None:
        lab, api = FakeLab(), FakeAPI({"status": "stopped", "template": 1})
        with self.assertRaises(android.AndroidError):
            android._make_template(lab, api, self._lease(), 9050)
        self.assertNotIn(("POST", "/nodes/pve/qemu/9050/template"), api.calls)


class HonestyTests(unittest.TestCase):
    def test_the_docs_do_not_promise_a_spoofed_model(self) -> None:
        """Measured: ro.product.model stays the stock image's."""
        doc = (Path(__file__).parents[1] / "docs" / "android.md").read_text()
        self.assertIn("sdk_gphone_x86_64", doc)
