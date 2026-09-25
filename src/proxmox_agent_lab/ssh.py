"""The one seam that spawns ``ssh``: argv policy, quoting, bounded execution.

Every remote action in the lab controller -- including ``qm guest`` traffic --
is a single argv executed through this module. ssh flattens its trailing
arguments into one remote shell command, so every argument is ``shlex.quote``d
at this seam; no caller composes remote shell strings by hand.

``check_allowed`` is the least-privilege boundary: a remote command outside the
allowlist is refused before any process is spawned, and the host-changing
subset needs explicit authorization. This replaces the old API-path policy gate
(``host_policy.check_api``) -- the gate moved from URL parsing to argv policy;
the *category* of what needs authorization is unchanged. Lease and ownership
gating (which guest may be touched, which lease owns it) lives above this seam
and is deliberately not this module's concern.

Failure taxonomy: a refusal is ``PolicyError``, a spawn failure or timeout is
``TransportError`` (a timeout is a real error, never a partial silent success),
and a remote non-zero exit is an ordinary ``CommandResult`` -- the command ran
and answered, so nothing raises.
"""

from __future__ import annotations

from dataclasses import dataclass
import shlex
import subprocess
from typing import Any, Callable, Sequence

from proxmox_agent_lab.errors import LabError


class PolicyError(LabError):
    """An argv was refused by the allowlist before any process was spawned."""


class TransportError(LabError):
    """The ssh child could not be spawned or did not finish in time."""


@dataclass(frozen=True)
class CommandResult:
    """What one remote argv produced.

    ``argv`` is the remote argv as requested (captured as a tuple at call
    time), not the full ``ssh`` invocation -- it is what the caller asked the
    host to run.
    """

    returncode: int
    stdout: bytes
    stderr: bytes
    argv: tuple[str, ...]

    @property
    def ok(self) -> bool:
        """Did the remote command exit 0?"""
        return self.returncode == 0


#: Remote executables any caller may run. Together with
#: ``HOST_CHANGE_COMMANDS`` this is the boundary that replaces the old scoped
#: API token: anything outside both sets -- an arbitrary root shell included --
#: is refused.
ALLOWED_COMMANDS: frozenset[str] = frozenset(
    {
        "qm",
        "pct",
        "pvesh",
        "pveversion",
        "hostname",
        "ip",
        "ethtool",
        "cat",
        "base64",
        "true",
    }
)

#: The subset that changes the host itself. Runnable, but only with
#: ``host_change=True`` (plumbed from the CLI authorization flags) on top of
#: the allowlist membership.
HOST_CHANGE_COMMANDS: frozenset[str] = frozenset({"shutdown", "crontab", "install"})

#: The only host paths ``cat`` may read (host temp hygiene).
_PXL_TEMP_PREFIX = "/tmp/pxl-"


def check_allowed(argv: Sequence[str], *, host_change: bool = False) -> None:
    """Raise ``PolicyError`` unless ``argv`` may run on the host.

    The rules, in order: ``argv`` must be non-empty and ``argv[0]`` must be
    allowed (``ALLOWED_COMMANDS`` or ``HOST_CHANGE_COMMANDS``); a command in
    ``HOST_CHANGE_COMMANDS`` additionally requires ``host_change=True``; and
    for ``argv[0] == "cat"`` every argument must start with ``/tmp/pxl-`` so
    the seam cannot read host files outside its own temp namespace.

    This replaces the old API-path policy gate (``host_policy.check_api``):
    the gate moved from URL parsing to argv policy. What the gate never did --
    lease and ownership gating over which guest or host a caller may touch --
    lives above this seam.

    The check runs before any process is spawned; a refusal therefore costs
    nothing and never reaches the host.
    """
    if not argv:
        raise PolicyError("refused: empty remote command")
    command = argv[0]
    if command not in ALLOWED_COMMANDS and command not in HOST_CHANGE_COMMANDS:
        raise PolicyError(f"refused: {command!r} is not on the command allowlist")
    if command in HOST_CHANGE_COMMANDS and not host_change:
        raise PolicyError(
            f"refused: {command!r} changes the host and needs host_change=True"
        )
    if command == "cat":
        for argument in argv[1:]:
            if not argument.startswith(_PXL_TEMP_PREFIX):
                raise PolicyError(
                    f"refused: cat reads only {_PXL_TEMP_PREFIX}* files, not {argument!r}"
                )


class SSH:
    """One argv at a time, over ``ssh``, to one configured target.

    The injected ``runner`` is called exactly like ``subprocess.run`` -- as
    ``runner(build_argv(argv), capture_output=True, input=stdin, timeout=...)``
    and must return an object exposing ``returncode``/``stdout``/``stderr`` --
    so tests drive every failure path without a network or a real ``ssh``.
    """

    def __init__(
        self,
        target: str,
        *,
        default_timeout: float = 30.0,
        ssh_binary: str = "ssh",
        base_opts: tuple[str, ...] = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=5"),
        runner: Callable[..., Any] | None = None,
    ) -> None:
        self._target = target
        self._default_timeout = default_timeout
        self._ssh_binary = ssh_binary
        self._base_opts = base_opts
        self._runner: Callable[..., Any] = (
            runner if runner is not None else subprocess.run
        )

    def build_argv(self, argv: Sequence[str]) -> list[str]:
        """Flatten one remote argv into the local ``ssh`` invocation.

        The remote command is ``" ".join(shlex.quote(a) for a in argv)``: ssh
        hands that single string to the remote shell, so quoting is what makes
        ``;``, ``$(...)`` and spaces survive as literal argument text instead
        of running as shell syntax.
        """
        remote = " ".join(shlex.quote(argument) for argument in argv)
        return [self._ssh_binary, *self._base_opts, self._target, remote]

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float | None = None,
        stdin: bytes | None = None,
        host_change: bool = False,
    ) -> CommandResult:
        """Run one remote argv and return its result.

        ``check_allowed`` runs first: an allowlist refusal raises ``PolicyError``
        and no process is spawned. A spawn failure (``OSError``) and a timeout
        both raise ``TransportError`` -- on timeout the child is killed before
        the error surfaces, never left running and never reported as success.
        ``timeout=None`` uses ``default_timeout``, so every call is bounded.
        ``stdin`` is piped to the remote command. A remote non-zero exit is a
        plain ``CommandResult`` with ``ok`` false, never an exception.
        """
        check_allowed(argv, host_change=host_change)
        limit = self._default_timeout if timeout is None else timeout
        try:
            completed = self._runner(
                self.build_argv(argv),
                capture_output=True,
                input=stdin,
                timeout=limit,
            )
        except subprocess.TimeoutExpired as raised:
            raise TransportError(
                f"ssh to {self._target} timed out after {limit:g}s"
            ) from raised
        except OSError as raised:
            raise TransportError(
                f"ssh to {self._target} could not be started: {raised}"
            ) from raised
        return CommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout if completed.stdout is not None else b"",
            stderr=completed.stderr if completed.stderr is not None else b"",
            argv=tuple(argv),
        )

    def probe(self) -> bool:
        """Is the target answering? ``true`` exits 0.

        Never raises: an unreachable host, a missing ssh binary and a refused
        command are all simply ``False`` -- probing must stay safe to call from
        doctor, lease-begin and the verified-shutdown loop.
        """
        try:
            return self.run(["true"]).ok
        except (PolicyError, TransportError):
            return False
