"""The single SQLite store: leases, resources, and the event journal.

One file, ``<state dir>/lab.db``, stdlib ``sqlite3`` only. It replaces the JSON
lease files, the MariaDB ledger, and the journal spool (rework plan §C). The
``events`` table keeps the pre-MariaDB ``journal.db`` DDL verbatim so legacy
databases stay readable; everything richer than the legacy columns lives inside
``data`` as a JSON object, redacted *before* it is ever inserted.

Concurrency model: WAL plus ``busy_timeout=5000`` (readers never block the
single writer), state transitions are compare-and-swap ``UPDATE ... WHERE
state=?`` (never read-modify-write), and multi-statement operations run inside
an explicit ``BEGIN IMMEDIATE`` transaction (:meth:`Store._tx`).

Lease states are ``active``, ``ending``, ``cleanup_failed``, ``ended``,
``destroyed``, ``abandoned``. The terminal states are :data:`TERMINAL_STATES`;
``cleanup_failed`` is deliberately *not* terminal — the lease keeps ownership of
its resources so every later sweep retries the cleanup.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import re
import sqlite3
import time
from typing import Iterator

from .errors import LabError

SCHEMA_VERSION = "1"

#: Lease states that end ownership: nothing they registered is shielded any more.
TERMINAL_STATES = ("ended", "destroyed", "abandoned")

REDACTED = "[REDACTED]"

_REDACT_KEY = re.compile(
    r"pass|secret|token|api[-_]?key|credential|auth|cookie|private",
    re.IGNORECASE,
)

# §C DDL. The events table is the legacy journal.db DDL reused verbatim.
_SCHEMA_DDL = (
    "CREATE TABLE IF NOT EXISTS schema_meta ("
    "key   TEXT PRIMARY KEY,"
    "value TEXT NOT NULL"
    ")",
    "CREATE TABLE IF NOT EXISTS events ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, event TEXT, "
    "lease TEXT, vmid INTEGER, data TEXT"
    ")",
    "CREATE TABLE IF NOT EXISTS leases ("
    "id           TEXT PRIMARY KEY,"
    "kind         TEXT NOT NULL DEFAULT 'ordinary',"
    "purpose      TEXT NOT NULL DEFAULT '',"
    "state        TEXT NOT NULL,"
    "created_at   TEXT NOT NULL,"
    "expires_at   INTEGER NOT NULL,"
    "heartbeat_at TEXT,"
    "ended_at     TEXT,"
    "last_error   TEXT"
    ")",
    "CREATE TABLE IF NOT EXISTS resources ("
    "id           INTEGER PRIMARY KEY AUTOINCREMENT,"
    "lease_id     TEXT NOT NULL REFERENCES leases(id),"
    "kind         TEXT NOT NULL,"
    "vmid         INTEGER,"
    "name         TEXT,"
    "policy       TEXT NOT NULL DEFAULT 'disposable',"
    "created_at   TEXT NOT NULL,"
    "destroyed_at TEXT,"
    "UNIQUE (lease_id, kind, vmid)"
    ")",
    "CREATE INDEX IF NOT EXISTS idx_events_lease   ON events(lease)",
    "CREATE INDEX IF NOT EXISTS idx_events_ts      ON events(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_resources_vmid ON resources(kind, vmid)",
)


class StoreError(LabError):
    """A store-level failure the operator can act on."""


def utc_now() -> str:
    """The current time as an ISO-8601 UTC timestamp (``%Y-%m-%dT%H:%M:%SZ``)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def redact_data(data: dict) -> dict:
    """Return ``data`` with secret-shaped values masked as :data:`REDACTED`.

    A value is masked when its key matches ``(?i)pass|secret|token|api[-_]?key|
    credential|auth|cookie|private``. Nested dicts (and dicts inside lists) are
    redacted recursively; the input is never mutated.
    """
    return _redact(data)


def _redact(value: object) -> object:
    if isinstance(value, dict):
        out: dict = {}
        for key, val in value.items():
            if isinstance(key, str) and _REDACT_KEY.search(key):
                out[key] = REDACTED
            else:
                out[key] = _redact(val)
        return out
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


class Store:
    """The lab database: leases, resources, and redacted events."""

    def __init__(self, path: object) -> None:
        self._conn = sqlite3.connect(path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA busy_timeout = 5000")
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._init_schema()
        except BaseException:
            self._conn.close()
            raise

    def _init_schema(self) -> None:
        conn = self._conn
        conn.execute(_SCHEMA_DDL[0])  # schema_meta first: version gate below
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is not None and row["value"] != SCHEMA_VERSION:
            raise StoreError(
                f"lab.db schema version {row['value']!r} does not match this "
                f"build ({SCHEMA_VERSION})"
            )
        with self._tx() as tx:
            for statement in _SCHEMA_DDL:
                tx.execute(statement)
            tx.execute(
                "INSERT OR IGNORE INTO schema_meta (key, value) "
                "VALUES ('schema_version', ?)",
                (SCHEMA_VERSION,),
            )

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """Run a multi-statement operation under ``BEGIN IMMEDIATE``."""
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    def close(self) -> None:
        """Close the database connection (idempotent)."""
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- leases ---------------------------------------------------------

    def create_lease(
        self,
        lease_id: str,
        *,
        kind: str = "ordinary",
        purpose: str = "",
        expires_at: int,
        created_at: str | None = None,
    ) -> None:
        """Create a lease in state ``active``; raises :class:`StoreError` if the id exists."""
        try:
            self._conn.execute(
                "INSERT INTO leases (id, kind, purpose, state, created_at, expires_at) "
                "VALUES (?, ?, ?, 'active', ?, ?)",
                (lease_id, kind, purpose, created_at or utc_now(), expires_at),
            )
        except sqlite3.IntegrityError as exc:
            raise StoreError(f"lease {lease_id!r} already exists") from exc

    def get_lease(self, lease_id: str) -> dict | None:
        """Return the lease row as a dict, or ``None`` if there is no such lease."""
        row = self._conn.execute(
            "SELECT * FROM leases WHERE id = ?", (lease_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def list_leases(
        self, *, state: str | None = None, include_ended: bool = False
    ) -> list[dict]:
        """List leases, hiding terminal states unless asked (or a state is given)."""
        clauses: list[str] = []
        params: list[object] = []
        if state is not None:
            clauses.append("state = ?")
            params.append(state)
        elif not include_ended:
            marks = ", ".join("?" for _ in TERMINAL_STATES)
            clauses.append(f"state NOT IN ({marks})")
            params.extend(TERMINAL_STATES)
        sql = "SELECT * FROM leases"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at, id"
        return [dict(row) for row in self._conn.execute(sql, params)]

    def active_leases(self) -> list[dict]:
        """Leases that still own their world: every non-terminal state."""
        return self.list_leases(include_ended=False)

    def claim_lease(
        self, lease_id: str, *, from_state: str, to_state: str
    ) -> bool:
        """Atomically move a lease between states; ``True`` iff this caller won."""
        cursor = self._conn.execute(
            "UPDATE leases SET state = ? WHERE id = ? AND state = ?",
            (to_state, lease_id, from_state),
        )
        return cursor.rowcount == 1

    def set_lease_state(
        self,
        lease_id: str,
        state: str,
        *,
        error: str | None = None,
        ended: bool = False,
    ) -> None:
        """Set a lease's state, optionally recording ``error`` and/or ``ended_at``.

        ``error=None`` leaves any recorded ``last_error`` untouched; ``ended=True``
        stamps ``ended_at`` with the current time.
        """
        sets = ["state = ?"]
        params: list[object] = [state]
        if error is not None:
            sets.append("last_error = ?")
            params.append(error)
        if ended:
            sets.append("ended_at = ?")
            params.append(utc_now())
        params.append(lease_id)
        cursor = self._conn.execute(
            f"UPDATE leases SET {', '.join(sets)} WHERE id = ?", params
        )
        if cursor.rowcount == 0:
            raise StoreError(f"unknown lease {lease_id!r}")

    def heartbeat(
        self, lease_id: str, *, expires_at: int, now: str | None = None
    ) -> bool:
        """Extend an active lease's expiry; ``False`` unless the lease is ``active``."""
        cursor = self._conn.execute(
            "UPDATE leases SET expires_at = ?, heartbeat_at = ? "
            "WHERE id = ? AND state = 'active'",
            (expires_at, now or utc_now(), lease_id),
        )
        return cursor.rowcount == 1

    # -- resources ------------------------------------------------------

    def register_resource(
        self,
        lease_id: str,
        kind: str,
        vmid: int,
        *,
        name: str | None = None,
        policy: str = "disposable",
        created_at: str | None = None,
    ) -> int:
        """Register a lease-owned resource; returns the resource id."""
        with self._tx() as tx:
            if tx.execute(
                "SELECT 1 FROM leases WHERE id = ?", (lease_id,)
            ).fetchone() is None:
                raise StoreError(f"unknown lease {lease_id!r}")
            try:
                cursor = tx.execute(
                    "INSERT INTO resources "
                    "(lease_id, kind, vmid, name, policy, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (lease_id, kind, vmid, name, policy, created_at or utc_now()),
                )
            except sqlite3.IntegrityError as exc:
                raise StoreError(
                    f"resource ({kind}, {vmid}) already registered to "
                    f"lease {lease_id!r}"
                ) from exc
            return int(cursor.lastrowid)

    def resources_for(self, lease_id: str) -> list[dict]:
        """All resources registered to a lease, oldest first."""
        return [
            dict(row)
            for row in self._conn.execute(
                "SELECT * FROM resources WHERE lease_id = ? ORDER BY id",
                (lease_id,),
            )
        ]

    def mark_destroyed(self, lease_id: str, kind: str, vmid: int) -> bool:
        """Stamp ``destroyed_at`` on a live resource; ``False`` if already destroyed."""
        cursor = self._conn.execute(
            "UPDATE resources SET destroyed_at = ? "
            "WHERE lease_id = ? AND kind = ? AND vmid = ? AND destroyed_at IS NULL",
            (utc_now(), lease_id, kind, vmid),
        )
        return cursor.rowcount == 1

    def owner_elsewhere(self, lease_id: str, kind: str, vmid: int) -> str | None:
        """Another *live* lease owning ``(kind, vmid)``, or ``None``.

        A lease whose state is terminal (:data:`TERMINAL_STATES`) never shields a
        resource; ``cleanup_failed`` does, because ownership has not ended.
        """
        marks = ", ".join("?" for _ in TERMINAL_STATES)
        row = self._conn.execute(
            "SELECT r.lease_id FROM resources AS r "
            "JOIN leases AS l ON l.id = r.lease_id "
            "WHERE r.kind = ? AND r.vmid = ? AND r.lease_id <> ? "
            f"AND l.state NOT IN ({marks}) "
            "ORDER BY r.id LIMIT 1",
            (kind, vmid, lease_id, *TERMINAL_STATES),
        ).fetchone()
        return row["lease_id"] if row is not None else None

    # -- events ---------------------------------------------------------

    def record(
        self,
        event: str,
        *,
        lease: str | None = None,
        vmid: int | None = None,
        data: dict | None = None,
        timestamp: str | None = None,
    ) -> int:
        """Append a journal event; ``data`` is redacted before it is serialized."""
        payload = (
            json.dumps(redact_data(data), sort_keys=True) if data is not None else None
        )
        cursor = self._conn.execute(
            "INSERT INTO events (timestamp, event, lease, vmid, data) "
            "VALUES (?, ?, ?, ?, ?)",
            (timestamp or utc_now(), event, lease, vmid, payload),
        )
        return int(cursor.lastrowid)

    def query_events(
        self,
        *,
        lease: str | None = None,
        since: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Newest-first events (legacy column names), optionally filtered."""
        clauses: list[str] = []
        params: list[object] = []
        if lease is not None:
            clauses.append("lease = ?")
            params.append(lease)
        if since is not None:
            clauses.append("timestamp >= ?")
            params.append(since)
        sql = "SELECT id, timestamp, event, lease, vmid, data FROM events"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [dict(row) for row in self._conn.execute(sql, params)]

    # -- MCP idle clock --------------------------------------------------

    def touch_mcp_activity(self, *, now: float | None = None) -> None:
        """Refresh the ``last_mcp_activity`` epoch (called on every tools/call)."""
        value = str(time.time() if now is None else now)
        self._conn.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('last_mcp_activity', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (value,),
        )

    def last_mcp_activity(self) -> float | None:
        """The last MCP activity epoch, or ``None`` if no tool call happened yet."""
        row = self._conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'last_mcp_activity'"
        ).fetchone()
        return float(row["value"]) if row is not None else None
