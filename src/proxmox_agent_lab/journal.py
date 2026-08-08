"""The audit ledger.

Every action appends one event: what happened, to which guest, under which
lease. Secrets never reach here -- callers redact before writing, and only
counts, exit codes and object keys are recorded.

Two backends:

* `sqlite` (default) -- one file, atomic appends, and actually queryable.
  `proxmox-lab journal` can answer "what did the last lease do?" without
  anyone writing a JSONL parser.
* `jsonl` -- one append-only file per day. Plain text, easy to tail, and the
  right choice if you want to commit the ledger to git.

Both are append-only in practice: nothing here updates or deletes.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3
import subprocess
from typing import Any

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


def database_path(journal_dir: Path) -> Path:
    return journal_dir / "journal.db"


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    # The watchdog and an interactive run can write at the same moment.
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(SCHEMA)
    return connection


def append(journal_dir: Path, backend: str, record: dict[str, Any]) -> None:
    """Write one already-redacted event."""
    if backend == "jsonl":
        path = journal_dir / f"{record['timestamp'][:10]}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        return
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

    fetched = git("fetch", "origin", branch, check=False)
    if fetched.returncode == 0:
        git("rebase", f"origin/{branch}")
    git("push", "origin", f"HEAD:refs/heads/{branch}")


def query(
    journal_dir: Path,
    *,
    limit: int = 50,
    lease: str | None = None,
    event: str | None = None,
    since: str | None = None,
) -> list[dict[str, Any]]:
    """Recent events, newest first."""
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


def summary(journal_dir: Path) -> dict[str, Any]:
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
