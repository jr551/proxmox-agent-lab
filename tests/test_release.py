import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts/check-release.py"


class ReleaseMetadataTests(unittest.TestCase):
    def make_tree(self, version="1.2.3", package_version="1.2.3"):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        (root / "src/proxmox_agent_lab").mkdir(parents=True)
        (root / "pyproject.toml").write_text(
            f'[project]\nname = "example"\nversion = "{version}"\n'
        )
        (root / "src/proxmox_agent_lab/__init__.py").write_text(
            f'__version__ = "{package_version}"\n'
        )
        (root / "CHANGELOG.md").write_text(
            "# Changelog\n\n"
            "## Unreleased\n\n"
            f"## {version} - 2026-08-08\n\n"
            "### Added\n\n- A guarded release.\n"
        )
        self.addCleanup(temp.cleanup)
        return root

    def run_check(self, root, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--root", str(root), *args],
            capture_output=True,
            text=True,
        )

    def test_matching_release_writes_notes(self):
        root = self.make_tree()
        notes = root / "notes.md"
        result = self.run_check(
            root, "--tag", "v1.2.3", "--notes-output", str(notes)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("A guarded release", notes.read_text())

    def test_rejects_mismatched_package_version(self):
        root = self.make_tree(package_version="9.9.9")
        result = self.run_check(root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match", result.stderr)

    def test_rejects_mismatched_tag(self):
        root = self.make_tree()
        result = self.run_check(root, "--tag", "v1.2.4")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("tag 'v1.2.4' does not match 'v1.2.3'", result.stderr)

    def test_rejects_mismatched_bootstrap_version(self):
        root = self.make_tree()
        (root / "bootstrap.sh").write_text('REQUIRED_VERSION="1.2.2"\n')
        result = self.run_check(root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("bootstrap required version", result.stderr)

    def test_rejects_missing_release_notes(self):
        root = self.make_tree()
        (root / "CHANGELOG.md").write_text("# Changelog\n\n## Unreleased\n")
        result = self.run_check(root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no dated section", result.stderr)


if __name__ == "__main__":
    unittest.main()
