import re
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
CANONICAL_SKILL = ROOT / "SKILL.md"
OMP_SKILL = ROOT / ".agents/skills/proxmox-agent-lab/SKILL.md"

# The slim surface the rewritten SKILL.md must describe (rework-plan A.5):
# ssh-copy-id setup, the lease shape, doctor, the MCP server and the host GC.
REQUIRED_MENTIONS = ("ssh-copy-id", "lease-begin", "lease-end", "doctor",
                     "mcp", "gc")
# Subsystems the rework removed; the skill must not describe them at all.
BANNED_MENTIONS = ("mariadb", "onboard", "token")


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

    def test_skill_describes_the_slim_surface(self):
        text = CANONICAL_SKILL.read_text(encoding="utf-8")

        for needle in REQUIRED_MENTIONS:
            with self.subTest(mention=needle):
                self.assertIn(needle, text)

        lowered = text.lower()
        for needle in BANNED_MENTIONS:
            with self.subTest(banned=needle):
                self.assertIsNone(
                    re.search(rf"{re.escape(needle)}", lowered),
                    f"SKILL.md still mentions {needle!r}",
                )


if __name__ == "__main__":
    unittest.main()
