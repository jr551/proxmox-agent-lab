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
import posixpath
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
        "cat",
        "base64",
        "true",
    }
)

#: The subset that changes the host itself. Runnable, but only with
#: ``host_change=True`` (plumbed from the CLI authorization flags) on top of
#: the allowlist membership. ``ethtool`` is here because §D names it in the
#: host-changing subset (it can *set* link parameters, not just read them).
#: ``tee`` and ``rm`` write and remove host files and are path-confined by
#: ``check_allowed`` (``tee`` to the pxl temp namespace, ``rm`` to it or to
#: the ``/usr/local/sbin/pxl-*`` install namespace) -- the GC install/
#: uninstall flow needs them because the seam's quoting leaves no shell
#: redirect to stage the script.
HOST_CHANGE_COMMANDS: frozenset[str] = frozenset(
    {"shutdown", "crontab", "install", "ethtool", "tee", "rm"}
)

#: The only host paths ``cat`` may read (host temp hygiene).
_PXL_TEMP_PREFIX = "/tmp/pxl-"

#: The install namespace ``rm`` may delete from (currently the GC script).
_PXL_INSTALL_PREFIX = "/usr/local/sbin/pxl-"

#: The pxl log namespace ``base64`` may read (the GC's cron log).
_PXL_LOG_PREFIX = "/var/log/pxl-"

#: The GC state namespace ``install -d`` may create (the clear-stamp dir).
_PXL_STATE_PREFIX = "/var/lib/pxl-"

#: ``pvesh`` verbs that only read. ``pvesh get`` is the API read the seam uses
#: everywhere; ``usage``/``help`` print text and change nothing. Every other
#: verb (``set``, ``create``, ``delete``, ``start`` …) mutates host state and
#: is refused unless the caller passes ``host_change=True``.
_PVESH_READ_VERBS: frozenset[str] = frozenset({"get", "usage", "help"})

#: ``install`` flags that take a separate value operand. Their values are
#: modes/owners, not paths, so they are never path-confined.
_INSTALL_VALUE_FLAGS: frozenset[str] = frozenset(
    {"-m", "-o", "-g", "-t", "--mode", "--owner", "--group", "--target-directory"}
)


def _confine(
    command: str, argument: str, prefixes: tuple[str, ...], verb: str
) -> None:
    """Raise ``PolicyError`` unless ``argument`` resolves inside ``prefixes``.

    Two layers, because the seam runs as root and a bare ``startswith`` check
    is bypassable by traversal: ``..`` anywhere in the argument is refused
    outright (it can be glued inside a segment, as in ``/tmp/pxl-../evil``,
    so a segment-exact match is not enough), and the path is
    ``posixpath.normpath``-ed before the prefix test, so
    ``/tmp/pxl-../../etc/shadow`` normalizes out of the namespace (to
    ``/tmp/etc/shadow`` here; with the extra slash, ``/tmp/pxl-/../../etc``
    reaches ``/etc``) and is refused cleanly.
    """
    if ".." in argument:
        raise PolicyError(
            f"refused: {command} path contains '..': {argument!r}"
        )
    normalized = posixpath.normpath(argument)
    if not normalized.startswith(prefixes):
        allowed = " or ".join(f"{prefix}*" for prefix in prefixes)
        raise PolicyError(
            f"refused: {command} {verb} only {allowed}, not {argument!r}"
        )


def check_allowed(argv: Sequence[str], *, host_change: bool = False) -> None:
    """Raise ``PolicyError`` unless ``argv`` may run on the host.

    The rules, in order: ``argv`` must be non-empty and ``argv[0]`` must be
    allowed (``ALLOWED_COMMANDS`` or ``HOST_CHANGE_COMMANDS``); a command in
    ``HOST_CHANGE_COMMANDS`` additionally requires ``host_change=True`` --
    except exactly ``crontab -l``, which only reads the crontab and changes
    nothing, so a status report needs no authorization while ``crontab -``
    and every other invocation stay gated; and for ``"cat"``/``"tee"``/``"rm"``
    / ``"base64"`` every path argument must *resolve* inside its allowed
    prefix -- no ``..`` anywhere in the argument, and posix-normalized before
    the prefix test -- so the seam cannot read, write or remove host files
    outside the pxl namespaces no matter how the path is spelled: ``cat`` and
    ``tee`` stay in ``/tmp/pxl-*``, ``rm`` may also touch
    ``/usr/local/sbin/pxl-*``, and ``base64`` (a reader, so it is confined
    exactly like ``cat``) reads ``/tmp/pxl-*``, ``/usr/local/sbin/pxl-*`` and
    ``/var/log/pxl-*``. Flag arguments (``-d``, ``-f`` …) are skipped for
    ``tee``, ``rm`` and ``base64`` (``cat`` checks every argument, as before).

    Three more shapes are gated because the command itself can write the
    host even though it is not in ``HOST_CHANGE_COMMANDS``. ``pvesh`` is
    restricted to its read verbs (``get``/``usage``/``help``) and is refused
    outright for anything else -- ``pvesh set``/``delete``/``create``
    reconfigure networking, storage and cluster state, and no caller in this
    tool needs a pvesh write, so the seam offers no way to authorize one.
    ``ip`` is confined to the read-only ``ip -br link show`` probe doctor
    needs, refusing ``link set``/``addr add``/``route replace``. ``install``
    is path-confined like ``tee``: its sources must come from the pxl temp
    namespace and its destination must land in the pxl install namespace
    (``install -d`` may create only a pxl temp/log/state directory), with
    value-carrying flags (``-m``, ``-o``, ``-g``, ``-t``) skipped so a mode
    is never mistaken for a path.

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
        if not (command == "crontab" and list(argv[1:]) == ["-l"]):
            raise PolicyError(
                f"refused: {command!r} changes the host and needs host_change=True"
            )
    if command == "cat":
        for argument in argv[1:]:
            _confine("cat", argument, (_PXL_TEMP_PREFIX,), "reads")
    if command == "tee":
        for argument in argv[1:]:
            if argument.startswith("-"):
                continue
            _confine("tee", argument, (_PXL_TEMP_PREFIX,), "writes")
    if command == "rm":
        for argument in argv[1:]:
            if argument.startswith("-"):
                continue
            _confine(
                "rm",
                argument,
                (_PXL_TEMP_PREFIX, _PXL_INSTALL_PREFIX),
                "removes",
            )
    if command == "base64":
        for argument in argv[1:]:
            if argument.startswith("-"):
                continue
            _confine(
                "base64",
                argument,
                (_PXL_TEMP_PREFIX, _PXL_INSTALL_PREFIX, _PXL_LOG_PREFIX),
                "reads",
            )
    if command == "pvesh":
        # `pvesh` reaches every corner of the API -- `pvesh set/delete/create`
        # reconfigures networking, storage and cluster state. Only the read
        # verb is on the ungated list; anything that writes needs the
        # host-change authorization like `shutdown` does.
        verb = argv[1] if argv[1:2] else ""
        if verb not in _PVESH_READ_VERBS:
            allowed = " or ".join(sorted(_PVESH_READ_VERBS))
            raise PolicyError(
                f"refused: pvesh {verb!r} is not a read verb; without "
                f"host_change=True pvesh may only be used as {allowed}"
            )
    if command == "ip":
        # Same shape as pvesh: `ip -br link show` is a read doctor probe,
        # but `ip link set`/`ip addr add`/`ip route replace` reconfigure the
        # host's networking. Confine the ungated form to the read-only
        # `link show` probe doctor needs.
        if argv[1:2] not in (["-br"], ["-brief"], ["-o"]) or (
            "show" not in argv
        ):
            raise PolicyError(
                "refused: ungated ip is limited to the read-only "
                "'ip -br link show' probe"
            )
    if command == "install":
        # `install` writes and chmods: the last unconfined writer. Only the
        # GC script namespace may be written, from the host temp staging
        # path, so a host-change-authorized caller cannot copy arbitrary
        # files to arbitrary destinations.
        # `install` takes value-carrying flags (`-m 0755`, `-o root`, `-g
        # root`), so a flag's *value* must be skipped too -- `-m`'s operand
        # is a mode, not a path, and treating it as one would refuse every
        # legitimate install.
        operands: list[str] = []
        skip_value = False
        for argument in argv[1:]:
            if skip_value:
                skip_value = False
                continue
            if argument in _INSTALL_VALUE_FLAGS:
                skip_value = True
                continue
            if argument.startswith("-"):
                continue
            operands.append(argument)
        # `install -d <dir>` creates a single directory; `install src dst`
        # and `install -t DIR src...` copy sources onto a destination.
        if "-d" in argv[1:] and len(operands) == 1:
            _confine(
                "install", operands[0],
                (_PXL_TEMP_PREFIX, _PXL_LOG_PREFIX, _PXL_STATE_PREFIX),
                "creates",
            )
        else:
            if len(operands) < 2:
                raise PolicyError("refused: install needs a source and a dest")
            for source in operands[:-1]:
                _confine("install", source, (_PXL_TEMP_PREFIX,), "reads")
            _confine(
                "install", operands[-1], (_PXL_INSTALL_PREFIX,), "writes"
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
