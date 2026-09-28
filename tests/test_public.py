import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts/check-public.py"


class PublicContentCheckTests(unittest.TestCase):
    def make_tree(self, marker: str,
                  config_text: str | None = None) -> tuple[Path, Path]:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        parent = Path(temp.name)
        root = parent / "repo"
        root.mkdir()
        source = root / "tests/test_node.py"
        source.parent.mkdir()
        source.write_text(f'NODE = "{marker}"\n')
        config = parent / "config.toml"
        if config_text is None:
            config_text = f'[pve]\nnode = "{marker}"\n'
        config.write_text(config_text)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        return root, config

    def run_check(self, root: Path, config: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(root)],
            capture_output=True,
            text=True,
            env=os.environ | {"PROXMOX_AGENT_LAB_CONFIG": str(config)},
        )

    def test_ignores_generic_local_node_matching_committed_test_data(self):
        root, config = self.make_tree("testnode")

        result = self.run_check(root, config)

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_retains_distinctive_local_node_detection(self):
        root, config = self.make_tree("lab-node-42")

        result = self.run_check(root, config)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("matches local [pve] node", result.stderr)

    def test_retains_distinctive_ssh_target_detection(self):
        root, config = self.make_tree(
            "lab-node-42",
            config_text='[ssh]\ntarget = "lab-node-42"\n',
        )

        result = self.run_check(root, config)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("matches local [ssh] target", result.stderr)

    def test_ignores_generic_ssh_alias(self):
        root, config = self.make_tree(
            "proxmox",
            config_text='[ssh]\ntarget = "proxmox"\n',
        )

        result = self.run_check(root, config)

        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
