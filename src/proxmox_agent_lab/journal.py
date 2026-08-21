"""The audit ledger.

Every action appends one event: what happened, to which guest, under which
lease. Secrets never reach here -- callers redact before writing, and only
counts, exit codes and object keys are recorded.

Two backends:

* `sqlite` (default) -- one file, atomic appends, and actually queryable.
  `proxmox-lab journal` can answer "what did the last lease do?" without
  anyone writing a JSONL parser.
* `jsonl` -- one append-only file per day. Plain text, easy to tail, and the
  right choice if you want to commit the ledger to git. `query` and `summary`
  read these files directly, so a JSONL install answers the same questions.

Both are append-only in practice: nothing here updates or deletes.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
from typing import Any
from .pocketbase import Client as PocketBaseClient

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT    NOT NULL,
    event     TEXT    NOT NULL,
    lease     TEXT,
    vmid      INTEGER,
    data      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS events_timestamp ON events (timestamp);
CREATE INDEX IF NOT EXISTS events_lease     ON events (lease);
CREATE INDEX IF NOT EXISTS events_event     ON events (event);
"""

SAFE_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*\Z")

# Concurrent CLI processes can push to the audit repository at the same
# moment. A push rejected as non-fast-forward (the rebase ran against a
# stale origin) is retried: refetch, rebase again and push once more.
SYNC_GIT_ATTEMPTS = 3
SYNC_GIT_RETRY_DELAY = 1.0


def database_path(journal_dir: Path) -> Path:
    return journal_dir / "journal.db"


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    # The watchdog and an interactive run can write at the same moment.
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(SCHEMA)
    return connection


def append(
    journal_dir: Path,
    backend: str,
    record: dict[str, Any],
    *,
    pocketbase: PocketBaseClient | None = None,
) -> None:
    """Write one already-redacted event."""
    if backend == "pocketbase":
        if pocketbase is None:
            raise ValueError("PocketBase backend has no configured client")
        pocketbase.create_event(record)
        return
    if backend == "jsonl":
        path = journal_dir / f"{record['timestamp'][:10]}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        return
    if backend != "sqlite":
        raise ValueError(f"unsupported journal backend: {backend}")
    connection = _connect(database_path(journal_dir))
    try:
        with connection:
            connection.execute(
                "INSERT INTO events (timestamp, event, lease, vmid, data) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    record.get("timestamp"),
                    record.get("event"),
                    record.get("lease"),
                    record.get("vmid"),
                    json.dumps(record, sort_keys=True),
                ),
            )
    finally:
        connection.close()


def migrate_sqlite_to_pocketbase(
    journal_dir: Path,
    client: PocketBaseClient,
    *,
    controller: str,
) -> dict[str, Any]:
    """Copy the SQLite ledger to PocketBase in source order."""
    source_path = database_path(journal_dir)
    if not source_path.is_file():
        raise ValueError(f"SQLite audit database does not exist: {source_path}")
    if not controller:
        raise ValueError("PocketBase import controller is empty")
    connection = sqlite3.connect(
        source_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10
    )
    source_events = imported = already_present = 0
    digest = hashlib.sha256()
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    try:
        connection.execute("BEGIN")
        rows = connection.execute(
            "SELECT id, timestamp, event, lease, vmid, data "
            "FROM events ORDER BY id ASC"
        )
        for source_id, timestamp, event, lease, vmid, raw_data in rows:
            try:
                record = json.loads(raw_data)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError(
                    f"SQLite audit row {source_id} has invalid JSON"
                ) from exc
            if not isinstance(record, dict):
                raise ValueError(
                    f"SQLite audit row {source_id} JSON must be an object"
                )
            if record.get("timestamp") != timestamp or record.get("event") != event:
                raise ValueError(
                    f"SQLite audit row {source_id} metadata disagrees with JSON"
                )
            canonical = json.dumps(
                record, sort_keys=True, separators=(",", ":")
            ).encode()
            event_id = hashlib.sha256(
                b"proxmox-agent-lab/sqlite-audit/v1\0"
                + str(source_id).encode()
                + b"\0"
                + canonical
            ).hexdigest()
            digest.update(canonical)
            digest.update(b"\n")
            source_events += 1
            if first_timestamp is None:
                first_timestamp = timestamp
            last_timestamp = timestamp
            if client.event_exists(event_id):
                already_present += 1
                continue
            client.create_imported_event(
                record,
                event_id=event_id,
                controller=controller,
                lease=lease,
                vmid=vmid,
            )
            imported += 1
    except sqlite3.Error as exc:
        raise ValueError(f"could not read SQLite audit database: {exc}") from exc
    finally:
        connection.close()
    return {
        "source_database": str(source_path),
        "source_events": source_events,
        "imported": imported,
        "already_present": already_present,
        "first_timestamp": first_timestamp,
        "last_timestamp": last_timestamp,
        "source_sha256": digest.hexdigest(),
    }


def sync_git(
    repo: Path,
    record: dict[str, Any],
    message: str,
    branch: str = "logs",
) -> None:
    """Append one record to a dedicated private git logging repository.

    This deliberately refuses a dirty or mixed-purpose checkout. Only the
    generated ``journal/YYYY-MM-DD.jsonl`` path is ever staged, so enabling
    audit sync cannot sweep source edits into a logging commit.
    """
    if not SAFE_BRANCH.fullmatch(branch) or ".." in branch or "@{" in branch:
        raise ValueError(f"unsafe audit git branch {branch!r}")
    repo = repo.expanduser().resolve()

    def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=90,
            check=False,
        )
        if check and result.returncode:
            raise RuntimeError(result.stdout.strip()[:300])
        return result

    top = git("rev-parse", "--show-toplevel").stdout.strip()
    if Path(top).resolve() != repo:
        raise RuntimeError(f"audit git_repo must be a repository root: {repo}")

    status = git("status", "--porcelain", "--untracked-files=all").stdout
    if status:
        raise RuntimeError("audit git_repo is dirty; refusing to mix log sync")

    timestamp = str(record.get("timestamp", ""))
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T.*", timestamp):
        raise ValueError("audit record has no ISO timestamp")
    relative = Path("journal") / f"{timestamp[:10]}.jsonl"
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")

    git("add", "--", relative.as_posix())
    safe_message = re.sub(r"[^A-Za-z0-9_.:-]+", "-", message)[:120]
    git("commit", "-m", f"lab: {safe_message}", "--", relative.as_posix())

    for attempt in range(SYNC_GIT_ATTEMPTS):
        try:
            fetched = git("fetch", "origin", branch, check=False)
            if fetched.returncode == 0:
                git("rebase", f"origin/{branch}")
            git("push", "origin", f"HEAD:refs/heads/{branch}")
            return
        except RuntimeError:
            if attempt == SYNC_GIT_ATTEMPTS - 1:
                raise
            # A concurrent process pushed between our fetch and our push, so
            # the rebase ran against a stale origin and the push was rejected
            # as non-fast-forward. Refetch, rebase onto the new head, retry.
            time.sleep(SYNC_GIT_RETRY_DELAY)


def git_sync_status(repo: Path, branch: str = "logs") -> dict[str, Any]:
    """Can the configured audit mirror actually receive a record?

    The same preconditions `sync_git` enforces, checked without writing
    anything: the path exists, is a repository root, is clean, and is
    writable. Every mutating command only *warns* when a push fails, so
    without this check a broken private mirror is invisible to a health check
    -- the local ledger keeps filling while nothing is exported.
    """
    result: dict[str, Any] = {
        "repo": str(repo),
        "branch": branch,
        "ok": False,
        "problem": None,
    }
    if not SAFE_BRANCH.fullmatch(branch) or ".." in branch or "@{" in branch:
        result["problem"] = f"unsafe audit git branch {branch!r}"
        return result
    try:
        resolved = repo.expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        result["problem"] = f"path could not be resolved: {exc}"
        return result
    result["repo"] = str(resolved)
    if not resolved.is_dir():
        result["problem"] = "directory does not exist"
        return result

    def git(*args: str) -> subprocess.CompletedProcess[str] | None:
        try:
            return subprocess.run(
                ["git", "-C", str(resolved), *args],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=30, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            result["problem"] = f"git could not be run: {exc}"
            return None

    top = git("rev-parse", "--show-toplevel")
    if top is None:
        return result
    if top.returncode:
        result["problem"] = "not a git repository"
        return result
    try:
        is_root = Path(top.stdout.strip()).resolve() == resolved
    except (OSError, RuntimeError):
        is_root = False
    if not is_root:
        result["problem"] = "not the root of its git repository"
        return result
    status = git("status", "--porcelain", "--untracked-files=all")
    if status is None:
        return result
    if status.returncode:
        result["problem"] = f"git status failed: {status.stdout.strip()[:200]}"
        return result
    if status.stdout.strip():
        result["problem"] = (
            "checkout is dirty; log sync refuses to mix commits"
        )
        return result
    if not os.access(resolved, os.W_OK):
        result["problem"] = "directory is not writable"
        return result
    result["ok"] = True
    return result


def _jsonl_paths(journal_dir: Path) -> list[Path]:
    """The daily JSONL files, oldest first."""
    try:
        return sorted(journal_dir.glob("*.jsonl"))
    except OSError:
        return []


def _like_matches(pattern: str, value: str) -> bool:
    """SQL LIKE semantics, so `event=` filters the same on either backend."""
    translated = re.escape(pattern.replace("*", "%"))
    translated = translated.replace("%", ".*").replace("_", ".")
    return re.fullmatch(translated, value, re.IGNORECASE) is not None


def _jsonl_records(
    journal_dir: Path,
    *,
    limit: int | None = None,
    lease: str | None = None,
    event: str | None = None,
    since: str | None = None,
) -> list[dict[str, Any]]:
    """Matching JSONL events, newest first.

    Files are named by date and appended to in order, so walking them in
    reverse is the same ordering SQLite's `ORDER BY id DESC` gives. Reading
    stops as soon as `limit` is satisfied, so a long-running ledger does not
    have to be loaded in full to answer "what just happened?".
    """
    out: list[dict[str, Any]] = []
    for path in reversed(_jsonl_paths(journal_dir)):
        try:
            lines = path.read_text().splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            if lease and record.get("lease") != lease:
                continue
            if event and not _like_matches(event, str(record.get("event", ""))):
                continue
            if since and str(record.get("timestamp", "")) < since:
                continue
            out.append(record)
            if limit is not None and len(out) >= limit:
                return out
    return out


def _jsonl_summary(journal_dir: Path) -> dict[str, Any]:
    paths = _jsonl_paths(journal_dir)
    if not paths:
        return {
            "backend": "jsonl",
            "journal_dir": str(journal_dir),
            "exists": False,
            "events": 0,
        }
    records = _jsonl_records(journal_dir)
    counts: dict[str, int] = {}
    leases: set[str] = set()
    timestamps: list[str] = []
    for record in records:
        counts[str(record.get("event"))] = counts.get(str(record.get("event")), 0) + 1
        if record.get("lease"):
            leases.add(str(record["lease"]))
        if record.get("timestamp"):
            timestamps.append(str(record["timestamp"]))
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:10]
    return {
        "backend": "jsonl",
        "journal_dir": str(journal_dir),
        "files": len(paths),
        "exists": True,
        "events": len(records),
        "first_event": min(timestamps) if timestamps else None,
        "last_event": max(timestamps) if timestamps else None,
        "distinct_leases": len(leases),
        "most_common": dict(ranked),
    }


def query(
    journal_dir: Path,
    *,
    limit: int = 50,
    lease: str | None = None,
    event: str | None = None,
    since: str | None = None,
    backend: str = "sqlite",
    pocketbase: PocketBaseClient | None = None,
) -> list[dict[str, Any]]:
    """Recent events, newest first."""
    if backend == "pocketbase":
        if pocketbase is None:
            raise ValueError("PocketBase backend has no configured client")
        return pocketbase.query(
            limit=limit, lease=lease, event=event, since=since
        )
    if backend not in {"sqlite", "jsonl"}:
        raise ValueError(f"unsupported journal backend: {backend}")
    if backend == "jsonl":
        # Read what this backend actually wrote. Reading journal.db here made a
        # configured JSONL ledger look empty, which during an incident review
        # is worse than an error.
        return _jsonl_records(
            journal_dir, limit=limit, lease=lease, event=event, since=since
        )
    path = database_path(journal_dir)
    if not path.exists():
        return []
    clauses: list[str] = []
    params: list[Any] = []
    if lease:
        clauses.append("lease = ?")
        params.append(lease)
    if event:
        clauses.append("event LIKE ?")
        params.append(event.replace("*", "%"))
    if since:
        clauses.append("timestamp >= ?")
        params.append(since)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    connection = _connect(path)
    try:
        rows = connection.execute(
            f"SELECT data FROM events{where} ORDER BY id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
    finally:
        connection.close()
    return [json.loads(row[0]) for row in rows]


def summary(
    journal_dir: Path,
    *,
    backend: str = "sqlite",
    pocketbase: PocketBaseClient | None = None,
) -> dict[str, Any]:
    if backend == "pocketbase":
        if pocketbase is None:
            raise ValueError("PocketBase backend has no configured client")
        return pocketbase.summary()
    if backend not in {"sqlite", "jsonl"}:
        raise ValueError(f"unsupported journal backend: {backend}")
    if backend == "jsonl":
        return _jsonl_summary(journal_dir)
    path = database_path(journal_dir)
    if not path.exists():
        return {"database": str(path), "exists": False, "events": 0}
    connection = _connect(path)
    try:
        total = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        first, last = connection.execute(
            "SELECT MIN(timestamp), MAX(timestamp) FROM events"
        ).fetchone()
        top = connection.execute(
            "SELECT event, COUNT(*) c FROM events GROUP BY event "
            "ORDER BY c DESC LIMIT 10"
        ).fetchall()
        leases = connection.execute(
            "SELECT COUNT(DISTINCT lease) FROM events WHERE lease IS NOT NULL"
        ).fetchone()[0]
    finally:
        connection.close()
    return {
        "database": str(path),
        "exists": True,
        "events": total,
        "first_event": first,
        "last_event": last,
        "distinct_leases": leases,
        "most_common": {name: count for name, count in top},
    }


def import_jsonl(journal_dir: Path) -> int:
    """Load any legacy per-day JSONL files into the database."""
    imported = 0
    for path in sorted(journal_dir.glob("*.jsonl")):
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            append(journal_dir, "sqlite", record)
            imported += 1
    return imported
