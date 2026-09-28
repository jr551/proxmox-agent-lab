"""Smoke tests for ``scripts/proxmox-lab``, the thin checkout runner.

The runner exists so a checkout works without installation: it pins a Python
3.11+ interpreter and execs ``python -m proxmox_agent_lab`` with ``src`` on
``PYTHONPATH``. These tests run the real script, not an in-process parser --
a broken shebang, an unexecutable bit, or a missing interpreter all fail here
and nowhere else.
"""

from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
RUNNER = ROOT / "scripts" / "proxmox-lab"


def run_runner(*args: str, env: dict[str, str] | None = None) -> (
    subprocess.CompletedProcess[str]
):
    return subprocess.run(
        [str(RUNNER), *args],
        capture_output=True,
        text=True,
        timeout=60,
        env=os.environ | (env or {}),
    )


class ProxmoxLabRunnerTests(unittest.TestCase):
    def test_runner_is_executable_shell_script(self):
        self.assertTrue(os.access(RUNNER, os.X_OK))
        first_line = RUNNER.read_text().splitlines()[0]
        self.assertTrue(
            first_line.startswith("#!") and first_line.endswith("sh"),
            first_line,
        )

    def test_help_exits_zero_without_config(self):
        # --help must work on a broken or missing config: it is the first
        # thing a user runs on a fresh install.
        result = run_runner(
            "--help",
            env={"PROXMOX_AGENT_LAB_CONFIG": "/nonexistent/pal-config.toml"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage", result.stdout.lower())
        # Pinned surface spot-check: the top-level listing names core commands.
        for command in ("doctor", "lease-begin"):
            self.assertIn(command, result.stdout)


if __name__ == "__main__":
    unittest.main()
