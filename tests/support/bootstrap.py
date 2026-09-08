"""Shared bootstrap for the offline suite.

Importing this module points the package at the fixture configuration and at
a *per-process* temporary state directory, before any ``proxmox_agent_lab``
module is imported. Every test module imports it first so that:

* site values come from ``tests/fixtures/config.toml``, never from whatever a
  developer happens to have configured locally, and
* runtime state lands in a fresh directory owned by this test process, so two
  concurrent suite runs cannot delete or observe each other's files and a
  previous run can never leak into this one.

Per-test isolation still comes from ``TemporaryDirectory`` and the module
attribute patching individual tests already perform; this bootstrap only
guarantees the import-time root is unique to the process.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

os.environ["PROXMOX_AGENT_LAB_CONFIG"] = str(_FIXTURES / "config.toml")

TEST_STATE = Path(tempfile.mkdtemp(prefix="proxmox-agent-lab-test-state-"))
os.environ["PROXMOX_AGENT_LAB_STATE"] = str(TEST_STATE)
