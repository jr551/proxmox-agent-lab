#!/usr/bin/env python3
"""Fail when tracked files contain local-site material.

The secret scanner catches credential shapes. This companion check catches the
quieter public-release mistakes: runtime journals, site notes, absolute home
paths, and values copied from the developer's active lab configuration. It
never prints the matched value.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib
from typing import Any


FORBIDDEN_PATHS = {
    "docs/site-notes.md",
    "proxmox-agent-lab.toml",
    "secrets.toml",
}
FORBIDDEN_PREFIXES = ("journal/", "runtime/")
HOME_PATH = re.compile(r"/(?:Users|home)/[^/\s]+/")
LOW_SPECIFICITY_NODE = re.compile(r"[a-z]+")
SITE_FIELDS = {
    "proxmox": ("host", "node"),
    "power": ("mac", "home_assistant_url"),
    "vpn": ("endpoint",),
    "s3": ("endpoint", "bucket"),
    "memflow": ("ssh_host",),
}


def tracked_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        stdout=subprocess.PIPE,
        check=True,
    )
    return [root / item.decode() for item in result.stdout.split(b"\0") if item]


def config_candidates(root: Path) -> list[Path]:
    candidates: list[Path] = []
    override = os.environ.get("PROXMOX_AGENT_LAB_CONFIG")
    if override:
        candidates.append(Path(override).expanduser())
    candidates.append(root / "proxmox-agent-lab.toml")
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    candidates.append(base / "proxmox-agent-lab" / "config.toml")
    return candidates


def is_low_specificity_node(section: str, key: str, value: str) -> bool:
    """Return whether a node value is too generic to identify a local site."""
    return (
        section == "proxmox"
        and key == "node"
        and LOW_SPECIFICITY_NODE.fullmatch(value) is not None
    )


def site_markers(root: Path) -> list[tuple[str, str]]:
    for path in config_candidates(root):
        if not path.is_file():
            continue
        try:
            with path.open("rb") as handle:
                data: dict[str, Any] = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError):
            return []
        markers: list[tuple[str, str]] = []
        for section, keys in SITE_FIELDS.items():
            table = data.get(section, {})
            if not isinstance(table, dict):
                continue
            for key in keys:
                value = table.get(key)
                if (
                    isinstance(value, str)
                    and len(value.strip()) >= 5
                    and not is_low_specificity_node(section, key, value.strip())
                ):
                    markers.append((f"[{section}] {key}", value.strip()))
        return markers
    return []


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    failures: list[str] = []
    files = tracked_files(root)
    markers = site_markers(root)
    for path in files:
        relative = path.relative_to(root).as_posix()
        if relative in FORBIDDEN_PATHS or relative.startswith(FORBIDDEN_PREFIXES):
            failures.append(f"{relative}: tracked local-state path")
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if HOME_PATH.search(line):
                failures.append(f"{relative}:{number}: absolute user home path")
            for label, marker in markers:
                if marker in line:
                    failures.append(f"{relative}:{number}: matches local {label}")
    if failures:
        print("Public-release hygiene check failed:\n" + "\n".join(failures), file=sys.stderr)
        return 1
    print("Public-release hygiene check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
