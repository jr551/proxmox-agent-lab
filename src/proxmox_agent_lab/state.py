"""Local file persistence and process locking for the lab controller.

This module owns *mechanics only*: atomic JSON writes, advisory file locks,
and UTC timestamp helpers. It knows nothing about leases, audit, or config
semantics -- the state root and lock path are supplied by the caller (today
``cli`` derives them from configuration and keeps them patchable for tests).
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

try:
    import fcntl  # POSIX advisory locks; absent on Windows
except ImportError:  # pragma: no cover - Windows only
    fcntl = None  # type: ignore[assignment]


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


def json_dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def _lock_file(handle: Any) -> None:
    """Take an exclusive advisory lock, blocking until it is ours.

    POSIX gets flock. Windows has no equivalent that blocks the same way, so
    it takes the non-blocking one and continues either way: the lock exists to
    stop two controllers on *one* machine interleaving, and a Windows box
    without it is no worse off than it was before Windows was supported.
    """
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return
    try:  # pragma: no cover - Windows only
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    except (ImportError, OSError):
        pass


def _try_lock_file(handle: Any) -> bool:
    """Take the lock if it is free. False when someone else holds it."""
    if fcntl is not None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False
    try:  # pragma: no cover - Windows only
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False
    except ImportError:
        # Neither flock nor msvcrt: not a platform that exists today. Proceed
        # rather than report the lock permanently held -- this gate guards the
        # backup sweep, and silently never running it is worse than the
        # theoretical double-run it prevents.
        return True


@contextlib.contextmanager
def controller_lock(state_root: Path, lock_path: Path) -> Any:
    """Serialize lease/state mutations across controllers on this machine."""
    state_root.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        _lock_file(handle)
        yield


@contextlib.contextmanager
def sweep_lock(state_root: Path, name: str) -> Any:
    """A non-blocking lock for work that runs long and must not stack up.

    A backup can run for hours. It must not hold the controller lock, or every
    lease operation queues behind it, and a watchdog firing every five minutes
    must not start a second copy of the same vzdump. So this is separate and
    non-blocking: yields False when a previous sweep still holds it.
    """
    state_root.mkdir(parents=True, exist_ok=True)
    with (state_root / f"{name}.lock").open("a+") as handle:
        if not _try_lock_file(handle):
            yield False
            return
        yield True
