"""Run host setup against fake commands; never change this host."""
from pathlib import Path
import json
import os
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class TailscaleSetupTests(unittest.TestCase):
    def run_setup(self, choice, state="Running", up_status=0):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = root / "calls"
            scripts = {
                "id": '#!/bin/sh\necho 0\n',
                "hostname": '#!/bin/sh\necho fixture-node\n',
                "pveversion": '#!/bin/sh\necho fixture-pve\n',
                "pveum": '#!/bin/sh\ncase "$*" in "user list"*) echo \'["agent@pve"]\';; "user token list"*) echo \'["lab"]\';; esac\n',
                "ip": '#!/bin/sh\nexit 0\n',
                "systemctl": '#!/bin/sh\necho "systemctl $*" >> "$TEST_CALLS"\n',
                "timeout": '#!/bin/sh\necho "timeout $*" >> "$TEST_CALLS"\nshift\nexec "$@"\n',
                "tailscale": '''#!/bin/sh
printf 'tailscale %s\n' "$*" >> "$TEST_CALLS"
case "$1" in
 status) printf '{"BackendState":"%s"}\n' "$TEST_STATE" ;;
 ip) echo '100.64.0.7' ;;
 up) exit "$TEST_UP_STATUS" ;;
esac
''',
            }
            for name, body in scripts.items():
                path = root / name
                path.write_text(body)
                path.chmod(0o700)
            # Use this interpreter rather than assuming a system python3.
            import sys
            (root / "python3").symlink_to(sys.executable)
            env = {**os.environ, "PATH": tmp + ":/usr/bin:/bin", "PXL_TAILSCALE": choice,
                   "TEST_CALLS": str(calls), "TEST_STATE": state, "TEST_UP_STATUS": str(up_status)}
            run = subprocess.run(["bash", str(ROOT / "proxmox-host-setup.sh")], env=env,
                                 text=True, capture_output=True, timeout=10)
            return run, calls.read_text() if calls.exists() else ""

    def test_declining_does_not_touch_tailscale(self):
        run, calls = self.run_setup("no")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(calls, "")
        self.assertIn("Skipped", run.stdout)

    def test_existing_connection_is_not_reconfigured(self):
        run, calls = self.run_setup("yes")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("systemctl enable --now tailscaled", calls)
        self.assertNotIn("tailscale up", calls)
        self.assertIn('host = "100.64.0.7"', run.stdout)
        self.assertIn("cannot wake a powered-off host", run.stdout)

    def test_login_is_bounded_and_does_not_enable_routes_or_ssh(self):
        run, calls = self.run_setup("yes", "NeedsLogin", 1)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("timeout 150 tailscale up --accept-dns=false --timeout=2m", calls)
        self.assertNotIn("--advertise", calls)
        self.assertNotIn("--ssh", calls)
        self.assertIn("login is incomplete", run.stdout)
        self.assertNotIn('host = "100.64.0.7"', run.stdout)

    def test_invalid_choice_fails_before_changes(self):
        run, calls = self.run_setup("maybe")
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(calls, "")
        self.assertIn("PXL_TAILSCALE must", run.stderr)
