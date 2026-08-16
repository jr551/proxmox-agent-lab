import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
CANONICAL_SKILL = ROOT / "SKILL.md"
OMP_SKILL = ROOT / ".agents/skills/proxmox-agent-lab/SKILL.md"


class OmpSkillPackagingTests(unittest.TestCase):
    def test_omp_skill_entry_resolves_to_canonical_skill(self):
        self.assertTrue(OMP_SKILL.is_symlink())
        self.assertEqual(OMP_SKILL.resolve(), CANONICAL_SKILL.resolve())

    def test_sdist_includes_omp_skill_layout(self):
        with (ROOT / "pyproject.toml").open("rb") as package_file:
            config = tomllib.load(package_file)

        included = config["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
        self.assertIn("/.agents", included)

    def test_wheel_forces_omp_skill_layout_into_package(self):
        with (ROOT / "pyproject.toml").open("rb") as package_file:
            config = tomllib.load(package_file)

        forced = config["tool"]["hatch"]["build"]["targets"]["wheel"][
            "force-include"
        ]
        self.assertEqual(forced[".agents"], ".agents")


if __name__ == "__main__":
    unittest.main()
