#!/usr/bin/env python3
"""Validate release metadata and extract notes for a GitHub release."""

from __future__ import annotations

import argparse
import re
import tomllib
from pathlib import Path


def release_metadata(root: Path, tag: str | None = None) -> tuple[str, str]:
    """Return the validated version and its changelog section."""
    project = tomllib.loads((root / "pyproject.toml").read_text())
    version = project["project"]["version"]

    init_text = (root / "src/proxmox_agent_lab/__init__.py").read_text()
    init_match = re.search(r'^__version__ = "([^"]+)"$', init_text, re.MULTILINE)
    if init_match is None:
        raise ValueError("src/proxmox_agent_lab/__init__.py has no __version__")
    if init_match.group(1) != version:
        raise ValueError(
            f"package version {init_match.group(1)!r} does not match "
            f"pyproject.toml version {version!r}"
        )

    expected_tag = f"v{version}"
    if tag is not None and tag != expected_tag:
        raise ValueError(f"tag {tag!r} does not match {expected_tag!r}")

    changelog = (root / "CHANGELOG.md").read_text()
    heading = re.search(
        rf"^## {re.escape(version)} - \d{{4}}-\d{{2}}-\d{{2}}\s*$",
        changelog,
        re.MULTILINE,
    )
    if heading is None:
        raise ValueError(f"CHANGELOG.md has no dated section for {version}")

    following = changelog[heading.end() :]
    next_release = re.search(r"^## ", following, re.MULTILINE)
    notes = following[: next_release.start() if next_release else None].strip()
    if not notes:
        raise ValueError(f"CHANGELOG.md release notes for {version} are empty")
    return version, notes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--tag")
    parser.add_argument("--notes-output", type=Path)
    args = parser.parse_args()

    try:
        version, notes = release_metadata(args.root.resolve(), args.tag)
    except (KeyError, OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        parser.error(str(exc))

    if args.notes_output is not None:
        args.notes_output.write_text(f"# {version}\n\n{notes}\n")
    print(f"Release metadata valid for v{version}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
