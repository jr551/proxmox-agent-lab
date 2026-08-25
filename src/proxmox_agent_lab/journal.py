"""The audit ledger.

Every action appends one event: what happened, to which guest, under which
lease. Secrets never reach here -- callers redact before writing, and only
counts, exit codes and object keys are recorded.

One backend: MariaDB on the Proxmox host (see `mariadb.py`). It is shared, so
every controller driving this lab writes to the same history instead of each
keeping a partial one of its own.

That host is powered off between leases by design, so the ledger is regularly
unreachable. `record()` therefore never fails an action: when the database is
down the already-redacted event is appended to a local spool, and
`proxmox-lab journal --flush-spool` uploads the backlog once the host is back.
Nothing here updates or deletes, and `event_id` is unique, so replaying a
spool or re-running a migration is a no-op.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from . import mariadb

# Legacy on-disk ledgers this controller may still be carrying from before the
# MariaDB ledger existed. They are imported once, automatically, on upgrade.
LEGACY_SQLITE = "journal.db"
MIGRATION_MARKER = ".migrated-to-mariadb"
SPOOL_NAME = "spool.jsonl"


def _utc_now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def spool_path(journal_dir: Path) -> Path:
    return journal_dir / SPOOL_NAME


def legacy_database_path(journal_dir: Path) -> Path:
    return journal_dir / LEGACY_SQLITE


def event_id_for(record: dict[str, Any]) -> str:
    """A stable id for one event.

    Events written before the MariaDB ledger have no id of their own, so one
    is derived from the content. Deterministic on purpose: re-running a
    migration, or flushing a spool twice, must land on the same id and be
    ignored by the unique index rather than duplicating history.
    """
    existing = record.get("event_id")
    if existing:
        return str(existing)[:36]
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:36]


def _prepared(record: dict[str, Any], controller: str) -> dict[str, Any]:
    """Fill in the fields the ledger indexes, without touching the payload."""
    out = dict(record)
    out["event_id"] = event_id_for(record)
    if not out.get("controller"):
        out["controller"] = controller
    return out


# --- writing --------------------------------------------------------------


def spool(journal_dir: Path, record: dict[str, Any]) -> Path:
    """Keep an already-redacted event locally until the ledger is reachable."""
    path = spool_path(journal_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    return path


def record(
    settings: mariadb.Settings | None,
    journal_dir: Path,
    entry: dict[str, Any],
    *,
    controller: str = "",
) -> str:
    """Write one event. Returns "written" or "spooled".

    Never raises for an unreachable ledger: the lab host is off between
    leases, and losing the ability to act because the history is unavailable
    would be worse than recording it late.
    """
    prepared = _prepared(entry, controller)
    if settings is None:
        spool(journal_dir, prepared)
        return "spooled"
    try:
        mariadb.append(settings, prepared)
        return "written"
    except mariadb.MariaDBError:
        spool(journal_dir, prepared)
        return "spooled"


def read_spool(journal_dir: Path) -> list[dict[str, Any]]:
    path = spool_path(journal_dir)
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def flush_spool(
    settings: mariadb.Settings, journal_dir: Path, *, controller: str = ""
) -> dict[str, Any]:
    """Upload the spooled backlog. The spool is cleared only on success."""
    pending = read_spool(journal_dir)
    if not pending:
        return {"spooled": 0, "uploaded": 0, "already_present": 0, "cleared": True}
    prepared = [_prepared(item, controller) for item in pending]
    written = mariadb.append_many(settings, prepared)
    path = spool_path(journal_dir)
    try:
        path.unlink()
        cleared = True
    except OSError:
        cleared = False
    return {
        "spooled": len(pending),
        "uploaded": written,
        "already_present": len(pending) - written,
        "cleared": cleared,
    }


# --- reading --------------------------------------------------------------


def query(
    settings: mariadb.Settings,
    *,
    limit: int = 50,
    lease: str | None = None,
    event: str | None = None,
    since: str | None = None,
    controller: str | None = None,
) -> list[dict[str, Any]]:
    """Recent events, newest first."""
    return mariadb.query(
        settings, limit=limit, lease=lease, event=event, since=since,
        controller=controller,
    )


def summary(settings: mariadb.Settings) -> dict[str, Any]:
    return mariadb.summary(settings)


# --- migration off the legacy ledgers -------------------------------------


def _legacy_sqlite_records(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(
            "SELECT data FROM events ORDER BY id ASC"
        ).fetchall()
    except sqlite3.DatabaseError:
        return []
    finally:
        connection.close()
    out: list[dict[str, Any]] = []
    for (blob,) in rows:
        try:
            out.append(json.loads(blob))
        except (json.JSONDecodeError, TypeError):
            continue
    return out


def _legacy_jsonl_records(journal_dir: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in sorted(journal_dir.glob("*.jsonl")):
        if path.name == SPOOL_NAME:
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def legacy_counts(journal_dir: Path) -> dict[str, int]:
    """How many events are still sitting in the pre-MariaDB ledgers."""
    return {
        "sqlite": len(_legacy_sqlite_records(legacy_database_path(journal_dir))),
        "jsonl": len(_legacy_jsonl_records(journal_dir)),
    }


def migration_done(journal_dir: Path) -> bool:
    return (journal_dir / MIGRATION_MARKER).is_file()


def mark_migrated(journal_dir: Path, detail: dict[str, Any]) -> None:
    journal_dir.mkdir(parents=True, exist_ok=True)
    (journal_dir / MIGRATION_MARKER).write_text(
        json.dumps(detail, indent=2, sort_keys=True) + "\n"
    )


def migrate_legacy(
    settings: mariadb.Settings,
    journal_dir: Path,
    *,
    controller: str = "",
    mark: bool = True,
) -> dict[str, Any]:
    """Import every pre-MariaDB event into the shared ledger.

    Idempotent: ids are derived from content, so an event already in MariaDB
    is ignored rather than duplicated. The legacy files are left on disk --
    this reads them, it never deletes the only copy of a history.
    """
    who = controller or "legacy"
    sqlite_records = _legacy_sqlite_records(legacy_database_path(journal_dir))
    jsonl_records = _legacy_jsonl_records(journal_dir)
    mariadb.ensure_schema(settings)

    # Serialise against any other controller upgrading right now. Without this
    # two machines can interleave and each report a confusing partial count,
    # even though the inserts themselves are safe.
    with mariadb.migration_lock(settings):
        before = mariadb.count(settings)
        prior = {row["controller"]: row for row in mariadb.migrations(settings)}

        seen: set[str] = set()
        batch: list[dict[str, Any]] = []
        for item in (*sqlite_records, *jsonl_records):
            prepared = _prepared(item, who)
            if prepared["event_id"] in seen:
                continue
            seen.add(prepared["event_id"])
            batch.append(prepared)

        uploaded = mariadb.append_many(settings, batch)
        after = mariadb.count(settings)
        detail = {
            "controller": who,
            "sqlite_events": len(sqlite_records),
            "jsonl_events": len(jsonl_records),
            "unique_events": len(batch),
            "uploaded": uploaded,
            "already_present": len(batch) - uploaded,
            "ledger_before": before,
            "ledger_after": after,
            "source_files_kept": True,
            # What the other machines already put there. A second controller
            # seeing uploaded=0 has not failed -- it has nothing new to add.
            "migrated_by_others": sorted(
                name for name in prior if name != who
            ),
            "repeat_migration": who in prior,
        }
        mariadb.record_migration(
            settings, who, detail,
            migrated_at=_utc_now_iso(),
        )
    if mark:
        mark_migrated(journal_dir, detail)
    return detail


def auto_migrate(
    settings: mariadb.Settings | None,
    journal_dir: Path,
    *,
    controller: str = "",
) -> dict[str, Any] | None:
    """Port a controller upgraded from an older release, once, in the background.

    Returns the migration detail when it ran, None when there was nothing to
    do or the ledger was unreachable. Never raises: an upgrade must not turn
    into a failed command, and the next invocation will simply retry.
    """
    if settings is None or migration_done(journal_dir):
        return None
    counts = legacy_counts(journal_dir)
    if not counts["sqlite"] and not counts["jsonl"]:
        # Nothing to carry over; record that so this never runs again.
        mark_migrated(journal_dir, {"legacy_events": 0, **counts})
        return None
    try:
        return migrate_legacy(settings, journal_dir, controller=controller)
    except mariadb.MariaDBError:
        return None


def settings_from_config(config: Any, secret: str = "") -> mariadb.Settings | None:
    """Build ledger settings from [audit]. None when nothing is configured yet.

    Defaults to the Proxmox host, because that is where the ledger container
    runs; an explicit [audit] host wins when it lives somewhere else.
    """
    audit = getattr(config, "audit", {}) or {}
    host = str(audit.get("host") or "").strip()
    if not host:
        host = str((getattr(config, "proxmox", {}) or {}).get("host") or "").strip()
    if not host:
        return None
    password = secret or os.environ.get("PROXMOX_AGENT_LAB_MARIADB_PASSWORD", "")
    try:
        return mariadb.Settings(
            host,
            port=int(audit.get("port") or mariadb.DEFAULT_PORT),
            database=str(audit.get("database") or mariadb.DEFAULT_DATABASE),
            user=str(audit.get("user") or mariadb.DEFAULT_USER),
            password=password,
            timeout=int(audit.get("timeout_seconds") or mariadb.DEFAULT_TIMEOUT),
        )
    except mariadb.MariaDBError:
        return None
