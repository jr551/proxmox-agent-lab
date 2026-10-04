"""A scripted stand-in for :mod:`proxmox_agent_lab.ssh`.

``FakeSSH`` duck-types :class:`proxmox_agent_lab.ssh.SSH` -- the same ``run``
signature, the same :class:`~proxmox_agent_lab.ssh.CommandResult` return type,
the same ``probe`` -- so layers above the seam can be tested with no network,
no real ``ssh``, and no host.

Every call is recorded in ``calls`` (remote argv, stdin, host_change, timeout)
so a test asserts the *call sequence*, not just the results it got back.
Outputs are scripted with :meth:`add`, keyed by a regex over
``" ".join(argv)``; rules are applied in insertion order with ``times``
consumption. A call no rule matches answers ``returncode=1`` with
``stderr=b"fake: no rule"`` -- a missing script fails loudly instead of
silently looking like success.

The fake records and scripts only; it deliberately does not re-implement the
allowlist (``check_allowed`` has its own tests).
"""

from __future__ import annotations

import re
from typing import Sequence

from proxmox_agent_lab.ssh import CommandResult


class FakeSSH:
    """Records every call; scripted outputs keyed by regex over " ".join(argv)."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._rules: list[dict] = []

    def add(
        self,
        pattern: str,
        *,
        returncode: int = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
        times: int | None = None,
    ) -> None:
        """Script the matching calls.

        ``pattern`` is a regex searched in ``" ".join(argv)``. The rule answers
        the first ``times`` calls it matches (``None``: unlimited) and is
        skipped once exhausted; rules are consulted in the order they were
        added, so the first rule still holding ``times`` wins.
        """
        self._rules.append(
            {
                "regex": re.compile(pattern),
                "returncode": returncode,
                "stdout": stdout,
                "stderr": stderr,
                "times": times,
            }
        )

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float | None = None,
        stdin: bytes | None = None,
        host_change: bool = False,
        memory_write: bool = False,
    ) -> CommandResult:
        """Record the call, then answer it from the first matching rule."""
        self.calls.append(
            {
                "argv": list(argv),
                "stdin": stdin,
                "host_change": host_change,
                "memory_write": memory_write,
                "timeout": timeout,
            }
        )
        joined = " ".join(argv)
        for rule in self._rules:
            if rule["times"] is not None and rule["times"] <= 0:
                continue
            if not rule["regex"].search(joined):
                continue
            if rule["times"] is not None:
                rule["times"] -= 1
            return CommandResult(
                rule["returncode"], rule["stdout"], rule["stderr"], tuple(argv)
            )
        return CommandResult(1, b"", b"fake: no rule", tuple(argv))

    def probe(self) -> bool:
        """``ssh <target> true`` on the fake: add a ``"true"`` rule for True."""
        return self.run(["true"]).ok
