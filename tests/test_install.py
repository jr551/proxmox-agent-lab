from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).parents[1]


class GuidedSetupTests(unittest.TestCase):
    def test_scripts_have_valid_bash_syntax(self) -> None:
        for script in ("install.sh", "pocketbase-host-setup.sh"):
            subprocess.run(["bash", "-n", str(ROOT / script)], check=True)

    def test_noninteractive_pocketbase_setup_writes_a_safe_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            config = root / "config.toml"
            (bin_dir / "pipx").write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = environment ]; then printf '%s\\n' \"$PIPX_BIN_DIR\"; fi\n"
            )
            (bin_dir / "proxmox-lab").write_text(
                "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  --version) echo proxmox-lab-test ;;\n"
                "  init) cat > \"$3\" <<'EOF'\n"
                "[proxmox]\n"
                "host = \"\"\nnode = \"\"\ntoken_user = \"\"\ntoken_name = \"\"\n"
                "[power]\nmac = \"\"\nbroadcast = \"\"\n"
                "[audit]\nbackend = \"sqlite\"\npocketbase_url = \"\"\n"
                "pocketbase_collection = \"proxmox_lab_events\"\n"
                "pocketbase_token_secret = \"audit-token\"\nEOF\n"
                "  ;;\n"
                "  secrets) printf '{\"proxmox-token\": true, \"audit-token\": true}\\n' ;;\n"
                "  doctor) exit 0 ;;\n"
                "esac\n"
            )
            for path in bin_dir.iterdir():
                path.chmod(0o755)
            env = {
                **os.environ,
                "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
                "PIPX_BIN_DIR": str(bin_dir),
                "PROXMOX_AGENT_LAB_CONFIG": str(config),
                "PXL_HOST": "192.0.2.9",
                "PXL_NODE": "pve",
                "PXL_TOKEN_USER": "agent@pve",
                "PXL_TOKEN_NAME": "lab",
                "PXL_MAC": "aa:bb:cc:dd:ee:ff",
                "PXL_AUDIT_BACKEND": "pocketbase",
                "PXL_PB_URL": "https://pocketbase.example",
                "PXL_PB_COLLECTION": "audit_events",
                "PXL_PB_TOKEN_NAME": "audit-token",
            }
            result = subprocess.run(
                ["/bin/bash", str(ROOT / "install.sh"), "--yes"],
                cwd=ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            text = config.read_text()

        self.assertIn('host = "192.0.2.9"', text)
        self.assertIn('token_user = "agent@pve"', text)
        self.assertIn('backend = "pocketbase"', text)
        self.assertIn('pocketbase_url = "https://pocketbase.example"', text)
        self.assertIn('pocketbase_collection = "audit_events"', text)
        self.assertNotIn("PXL_PB_TOKEN_SECRET", text)


if __name__ == "__main__":
    unittest.main()
