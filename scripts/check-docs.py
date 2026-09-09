#!/usr/bin/env python3
"""Offline documentation checks: relative links resolve and ``proxmox-lab``
command examples name real subcommands.

Runs with no network and no installed package: the parser is imported from
``src/`` in the same checkout the documentation lives in.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "dist", "build",
             "__pycache__"}
LINK_RE = re.compile(r"\[[^\]]*\]\(\s*([^)\s]+)[^)]*\)")
HEADING_RE = re.compile(r"^#{1,6}\s+(.*)$", re.M)
HTML_ANCHOR_RE = re.compile(r'<a\s+(?:name|id)="([^"]+)"')
CMD_RE = re.compile(r"^(?:\s*(?:sudo|env\s+\S+=\S+|PROXMOX_\S+=\S+)\s+)*"
                    r"proxmox-lab\s+(.+?)(?:\s*\\\\)?\s*$")
# A token that cannot be a subcommand: flag, variable, value, shell syntax.
NOT_A_NAME = re.compile(r"^[-$'\"(<{|&;=*]|^\d|/.*|[A-Z_]+=|\$\{")


def _slug(heading: str) -> str:
    """The anchor GitHub generates for a markdown heading."""
    slug = heading.strip().lower()
    slug = re.sub(r"<[^>]+>", "", slug)          # inline HTML
    slug = re.sub(r"[^\w\- ]", "", slug)         # punctuation out
    return slug.replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    anchors = set(HTML_ANCHOR_RE.findall(text))
    anchors.update(_slug(h) for h in HEADING_RE.findall(text))
    return anchors


def _command_tree() -> dict[tuple[str, ...], object]:
    """Every registered ``proxmox-lab`` subcommand path."""
    os.environ.setdefault("PROXMOX_AGENT_LAB_CONFIG",
                          str(ROOT / "tests" / "fixtures" / "config.toml"))
    sys.path.insert(0, str(ROOT / "src"))
    from proxmox_agent_lab import cli

    tree: dict[tuple[str, ...], object] = {}

    def walk(parser: object, prefix: tuple[str, ...]) -> None:
        subparsers = getattr(parser, "_subparsers", None)
        for action in getattr(subparsers, "_group_actions", []) or []:
            for name, sub in (getattr(action, "choices", None) or {}).items():
                tree[prefix + (name,)] = sub
                if sub is not None:
                    walk(sub, prefix + (name,))

    walk(cli.parser(), ())
    return tree


def _markdown_files() -> list[Path]:
    files = []
    for path in sorted(ROOT.rglob("*.md")):
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        files.append(path)
    return files


def check_links(files: list[Path]) -> list[str]:
    failures = []
    for md in files:
        text = md.read_text(encoding="utf-8", errors="replace")
        for m in LINK_RE.finditer(text):
            target = m.group(1)
            if target.startswith(("http://", "https://", "mailto:", "tel:",
                                  "#", "{")):
                continue
            file_part, _, anchor = target.partition("#")
            if not file_part:
                resolved = md
            elif ".agents" in md.relative_to(ROOT).parts:
                # Bundled skill copies keep repo-root-relative links.
                resolved = (ROOT / unquote(file_part)).resolve()
            else:
                resolved = (md.parent / unquote(file_part)).resolve()
            try:
                resolved.relative_to(ROOT.resolve())
            except ValueError:
                failures.append(
                    f"{md.relative_to(ROOT)}: link escapes the checkout: "
                    f"{target}")
                continue
            if not resolved.exists():
                failures.append(
                    f"{md.relative_to(ROOT)}: dead link {target}")
            elif anchor and resolved.suffix == ".md":
                if anchor not in _anchors(resolved):
                    failures.append(
                        f"{md.relative_to(ROOT)}: missing anchor "
                        f"#{anchor} in {resolved.relative_to(ROOT)}")
    return failures


def check_commands(files: list[Path], tree: dict[tuple[str, ...], object]
                   ) -> list[str]:
    failures = []
    for md in files:
        text = md.read_text(encoding="utf-8", errors="replace")
        in_fence = False
        for lineno, line in enumerate(text.splitlines(), 1):
            if line.strip().startswith("```"):
                in_fence = not in_fence
                continue
            m = CMD_RE.match(line)
            if not m:
                continue
            tokens = m.group(1).split()
            current_prefix: tuple[str, ...] = ()
            for token in tokens:
                token = token.strip(";|&")
                if token in ("proxmox-lab", "sudo") or \
                        NOT_A_NAME.match(token):
                    break
                children = {path[-1]: sub for path, sub in tree.items()
                            if path[:-1] == current_prefix}
                if token in children:
                    current_prefix = current_prefix + (token,)
                    continue
                if children:
                    failures.append(
                        f"{md.relative_to(ROOT)}:{lineno}: 'proxmox-lab "
                        f"{' '.join(current_prefix + (token,))}' -- "
                        f"'{token}' is not a registered subcommand")
                break
    return failures


def main() -> int:
    files = _markdown_files()
    failures = check_links(files)
    failures += check_commands(files, _command_tree())
    for failure in failures:
        print(f"FAIL {failure}")
    print(f"check-docs: {len(files)} markdown files, "
          f"{len(failures)} problem(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
