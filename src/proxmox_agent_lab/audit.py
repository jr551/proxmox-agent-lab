"""The audit facade: one redacted event row per action.

Every action in the lab appends one event: what happened, to which guest,
under which lease. The row lands in the local SQLite store (``<state dir>
/lab.db``, see ``store.py``); everything richer than the legacy columns lives
inside ``data`` as a JSON object -- redacted *before* it is ever inserted.

Auditing must never fail the action being audited: when an event cannot be
recorded, one warning goes to stderr and the action continues. The store
handle is opened lazily, on first use, so importing this module -- and
running with a missing or broken config -- never touches the filesystem.
"""

from __future__ import annotations

import re
import sys
from typing import Any

from . import config as config_module
from . import store as store_module

SENSITIVE_KEY = re.compile(
    r"(pass(word)?|token|secret|authorization|private.?key|cipassword|ssh.?keys?)",
    re.IGNORECASE,
)


def redact(value: Any, key: str = "") -> Any:
    if SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        if "PVEAPIToken=" in value or "Bearer " in value:
            return "[REDACTED]"
        return value[:1000]
    return value


_STORE: store_module.Store | None = None


def _store() -> store_module.Store:
    """The process-wide store handle, opened on first use (never at import)."""
    global _STORE
    if _STORE is None:
        root = config_module.state_dir()
        root.mkdir(parents=True, exist_ok=True)
        _STORE = store_module.Store(root / "lab.db")
    return _STORE


def audit(
    event: str, *, lease: str | None = None, vmid: int | None = None, **fields: Any
) -> None:
    """Append one redacted event row. Never raises into the action.

    The row keeps the legacy ``events`` columns (``timestamp``, ``event``,
    ``lease``, ``vmid``, ``data``). ``data`` is the JSON object
    ``{actor, tool, ok, target, **fields}`` with every value redacted first:
    ``actor`` is ``"cli"``, ``tool`` defaults to the event name, ``ok`` to
    ``True`` and ``target`` to ``None`` -- callers may override any of them
    through ``fields``.
    """
    try:
        data: dict[str, Any] = {
            "actor": "cli",
            "tool": event,
            "ok": True,
            "target": None,
        }
        data.update(redact(fields))
        _store().record(event, lease=lease, vmid=vmid, data=data)
    except Exception as exc:  # noqa: BLE001 - auditing never fails the action
        print(
            f"warning: audit event {event!r} could not be recorded: {exc}",
            file=sys.stderr,
        )
