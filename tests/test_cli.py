"""Regression guard: the CLI parser must build and expose its surface.

A broken HEAD once shipped because no test ever called ``cli.parser()`` after
``longterm.py`` was deleted while its registration survived (2026-09-25). This
file exists so that class of breakage fails the suite instead of the first
human who runs ``--help``.

The pinned top-level surface (docs/rework-plan.md §A.5) is asserted for the
commands this phase owns; ``mcp``, ``gc`` and the ``power wake|status|shutdown``
groups join the assertions when they land (phases 4-6).
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import bootstrap  # noqa: E402,F401
import unittest  # noqa: E402

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from proxmox_agent_lab import cli  # noqa: E402

PINNED_TOP_LEVEL = [
    "init",
    "doctor",
    "journal",
    "cleanup-expired",
    "lease-begin",
    "lease-heartbeat",
    "lease-end",
    "lease-list",
    "lease-destroy",
    "lease-register",
    "guest",
    "push",
    "pull",
    "console",
    "power-on",
]


def _top_level_names(parser: argparse.ArgumentParser) -> list[str]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return sorted(action.choices)
    return []


class ParserBuildTests(unittest.TestCase):
    def test_parser_builds_without_raising(self) -> None:
        parser = cli.parser()
        self.assertIsNotNone(parser)

    def test_every_registered_top_level_answers_help(self) -> None:
        parser = cli.parser()
        names = _top_level_names(parser)
        self.assertTrue(names)
        for name in names:
            with self.subTest(name=name):
                with self.assertRaises(SystemExit) as ctx:
                    parser.parse_args([name, "--help"])
                self.assertEqual(ctx.exception.code, 0)

    def test_pinned_top_level_commands_are_registered(self) -> None:
        names = set(_top_level_names(cli.parser()))
        for name in PINNED_TOP_LEVEL:
            with self.subTest(name=name):
                self.assertIn(name, names)

    def test_pinned_console_and_power_subcommands_parse(self) -> None:
        parser = cli.parser()
        for argv in (
            ["console", "screenshot", "--help"],
            ["console", "type", "--help"],
            ["console", "keys", "--help"],
            ["guest", "probe", "--help"],
            ["guest", "run", "--help"],
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit) as ctx:
                    parser.parse_args(argv)
                self.assertEqual(ctx.exception.code, 0)
