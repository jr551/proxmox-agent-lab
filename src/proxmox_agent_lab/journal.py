"""The journal: reading the audit events out of the local lab store.

Every audited action is one row in ``<state dir>/lab.db`` (see ``store.py``
and ``audit.py``); this module is the read side, used by ``proxmox-lab
journal`` and anything else that needs the event history. Rework plan §C is
explicit that this is a fresh start: the old ``journal.db`` files and jsonl
ledgers are never imported, and there is no spool or migration machinery at
all -- what is in the store is the history.
"""

from __future__ import annotations

import json
from typing import Any

from . import config as config_module
from . import store as store_module


def query_events(
    *, lease: str | None = None, since: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    """Recent events, newest first, optionally filtered.

    Rows carry the legacy column names verbatim (``id``, ``timestamp``,
    ``event``, ``lease``, ``vmid``, ``data``); ``data`` is the redacted JSON
    object the action recorded.
    """
    root = config_module.state_dir()
    root.mkdir(parents=True, exist_ok=True)
    handle = store_module.Store(root / "lab.db")
    try:
        return handle.query_events(lease=lease, since=since, limit=limit)
    finally:
        handle.close()


def format_events(rows: list[dict[str, Any]]) -> str:
    """The ``proxmox-lab journal`` output: the rows as one JSON document."""
    return json.dumps(rows, indent=2, sort_keys=True, default=str)
