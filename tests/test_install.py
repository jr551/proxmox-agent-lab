"""Tests for the guided setup scripts (install.sh and friends).

These run the real shell scripts against stub `pipx`/`proxmox-lab` binaries.
The stub's `init` writes the package's real config template
(`proxmox_agent_lab.config.TEMPLATE` -- the §G schema), so install.sh's
placeholder substitution is always checked against whatever template text
config.py actually generates, never a drifting copy of it.
"""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).parents[1]

# The stub `proxmox-lab init` renders the real template through the pinned
# interpreter, with the checkout's src/ on its path.
_INIT_STUB = (
    "import pathlib,sys;"
    "sys.path.insert(0, sys.argv[2]);"
    "from proxmox_agent_lab import config;"
    "pathlib.Path(sys.argv[1]).write_text(config.TEMPLATE)"
)


class GuidedSetupTests(unittest.TestCase):
    def test_scripts_have_valid_bash_syntax(self) -> None:
        for script in ("install.sh", "minio-host-setup.sh"):
            subprocess.run(["bash", "-n", str(ROOT / script)], check=True)

    def _stub_bin_dir(self, root: Path, *, secrets_json: str) -> Path:
        bin_dir = root / "bin"
        bin_dir.mkdir()
        (bin_dir / "pipx").write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = environment ]; then printf '%s\\n' \"$PIPX_BIN_DIR\"; fi\n"
        )
        (bin_dir / "proxmox-lab").write_text(
            "#!/bin/sh\n"
            "case \"$1\" in\n"
            "  --version) echo proxmox-lab-test ;;\n"
            f"  init) {shlex.quote(sys.executable)} -c "
            f"{shlex.quote(_INIT_STUB)} \"$3\" "
            f"{shlex.quote(str(ROOT / 'src'))} ;;\n"
            f"  secrets) printf '{secrets_json}\\n' ;;\n"
            "  doctor) exit 0 ;;\n"
            "esac\n"
        )
        for path in bin_dir.iterdir():
            path.chmod(0o755)
        return bin_dir

    def _env(self, bin_dir: Path, config: Path, **extra: str) -> dict[str, str]:
        return {
            **os.environ,
            "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
            "PIPX_BIN_DIR": str(bin_dir),
            "PROXMOX_AGENT_LAB_CONFIG": str(config),
            "PXL_HOST": "192.0.2.9",
            "PXL_NODE": "pve",
            "PXL_TOKEN_USER": "agent@pve",
            "PXL_TOKEN_NAME": "lab",
            "PXL_MAC": "aa:bb:cc:dd:ee:ff",
            **extra,
        }

    def _install(self, root: Path, **extra: str) -> str:
        """Run install.sh non-interactively; return the config text written."""
        config = root / "config.toml"
        bin_dir = self._stub_bin_dir(
            root, secrets_json=extra.pop("secrets_json", '{"proxmox-token": true}')
        )
        result = subprocess.run(
            ["/bin/bash", str(ROOT / "install.sh"), "--yes"],
            cwd=ROOT,
            env=self._env(bin_dir, config, **extra),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return config.read_text()

    def test_noninteractive_setup_writes_a_safe_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            text = self._install(Path(tmp))

        self.assertIn('host = "192.0.2.9"', text)
        self.assertIn('token_user = "agent@pve"', text)
        self.assertIn('backend = "file"', text)
        self.assertIn('secrets.toml"', text)
        # The §G shape the template carries survives the substitution.
        for marker in ("[ssh]", "[pve]", "[power]", "[state]", "[lease]",
                       "template_vmid = ", "ttl_seconds = ",
                       "idle_shutdown_seconds = ", "dir = "):
            self.assertIn(marker, text)
        # The install no longer asks about audit backends at all: the ledger is
        # provisioned once with 'journal host-setup' and shared from there.
        self.assertNotIn("pocketbase", text)

    def test_placeholders_substituted(self) -> None:
        """Every answer install.sh collected landed in the §G config template:
        no blank placeholder survives for a key the script substituted."""
        with tempfile.TemporaryDirectory() as tmp:
            text = self._install(Path(tmp))

        for placeholder in ('host = ""', 'node = ""', 'token_user = ""',
                            'token_name = ""', 'mac = ""', 'file_path = ""'):
            self.assertNotIn(placeholder, text)
        self.assertIn('host = "192.0.2.9"', text)
        self.assertIn('mac = "aa:bb:cc:dd:ee:ff"', text)
        # Broadcast is derived from the host's /24, not left at the template
        # placeholder.
        self.assertIn('broadcast = "192.0.2.255"', text)
        self.assertNotIn('broadcast = "255.255.255.255"', text)
        self.assertIn('file_path = ', text)
        self.assertIn('secrets.toml"', text)

    def test_noninteractive_existing_s3_backend_writes_a_safe_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            text = self._install(
                Path(tmp),
                secrets_json='{"proxmox-token": true, "s3-key-id": true, "s3-secret-key": true}',
                PXL_S3_BACKEND="existing",
                PXL_S3_ENDPOINT="https://s3.example",
                PXL_S3_BUCKET="lab-scratch",
                PXL_S3_REGION="us-east-1",
            )

        self.assertIn("enabled = true", text)
        self.assertIn('endpoint = "https://s3.example"', text)
        self.assertIn('bucket = "lab-scratch"', text)
        self.assertIn('region = "us-east-1"', text)

    def test_lxc_s3_backend_prints_host_setup_and_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.toml"
            bin_dir = self._stub_bin_dir(root, secrets_json='{"proxmox-token": true}')
            env = self._env(bin_dir, config, PXL_S3_BACKEND="lxc")
            result = subprocess.run(
                ["/bin/bash", str(ROOT / "install.sh"), "--yes"],
                cwd=ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("minio-host-setup.sh", result.stdout)
        self.assertFalse(config.exists())


if __name__ == "__main__":
    unittest.main()
