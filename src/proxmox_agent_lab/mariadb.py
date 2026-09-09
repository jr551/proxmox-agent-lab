"""The audit ledger, stored in MariaDB on the Proxmox host.

One shared database, so every controller that drives this lab appends to the
same ledger and can read back what the others did. That is the whole reason
this replaced the previous per-controller SQLite file: two machines running
the same lab produced two partial histories that never met.

The database lives in an OCI container on the Proxmox host, published on the
hypervisor's own address (see ``mariadb-host-setup.sh`` and
``proxmox-lab journal host-setup``). That host is powered off between leases
by design, so the ledger is *not* always reachable: callers write through
``journal.record``, which spools locally when the database is down and
uploads the backlog with ``proxmox-lab journal --flush-spool``.

Events are append-only. Nothing here updates or deletes, and ``event_id`` is
unique, so replaying a spool or re-running a migration is idempotent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:  # pragma: no cover - exercised by the import-error path in tests
    import pymysql
    from pymysql.cursors import DictCursor
except ModuleNotFoundError as exc:  # pragma: no cover
    pymysql = None  # type: ignore[assignment]
    DictCursor = None  # type: ignore[assignment]
    _IMPORT_ERROR: ModuleNotFoundError | None = exc
else:
    _IMPORT_ERROR = None

DEFAULT_PORT = 3306
DEFAULT_DATABASE = "proxmox_lab"
DEFAULT_USER = "proxmox_lab"
DEFAULT_TIMEOUT = 10

# utf8mb4 throughout: guest names and lease purposes are free text and have
# carried emoji before now. `data` keeps the whole redacted record as JSON so
# a new field never needs a migration; the promoted columns exist only to be
# indexed.
SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id         BIGINT       NOT NULL AUTO_INCREMENT,
    event_id   CHAR(36)     NOT NULL,
    controller VARCHAR(190) NOT NULL,
    timestamp  VARCHAR(32)  NOT NULL,
    event      VARCHAR(200) NOT NULL,
    lease      VARCHAR(190)     NULL,
    vmid       INT              NULL,
    data       LONGTEXT     NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY events_event_id (event_id),
    KEY events_timestamp (timestamp),
    KEY events_lease (lease),
    KEY events_event (event),
    KEY events_controller (controller)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

# Who has already carried their old local ledger into this one. Every
# controller migrates its own history exactly once; this table is how the
# second machine to upgrade can tell that the first already ran, report the
# difference honestly, and not present a no-op as a failure.
MIGRATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS migrations (
    id           BIGINT       NOT NULL AUTO_INCREMENT,
    controller   VARCHAR(190) NOT NULL,
    migrated_at  VARCHAR(32)  NOT NULL,
    source_events INT         NOT NULL,
    uploaded     INT          NOT NULL,
    already_present INT       NOT NULL,
    detail       LONGTEXT     NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY migrations_controller (controller)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

# Two controllers upgrading at the same moment must not interleave their
# imports. The lock is advisory and connection-scoped: if a controller dies
# mid-migration MariaDB drops it with the connection, and the next run
# resumes safely because every insert is INSERT IGNORE on a content hash.
MIGRATION_LOCK = "proxmox_lab_journal_migration"
MIGRATION_LOCK_TIMEOUT = 120

# Shared secrets, so a second controller needs exactly one credential to join
# the lab: the password for this database. Everything else -- WireGuard keys,
# vision API keys, tunnel tokens -- is handed out from here, which is what
# makes adding a machine a one-liner instead of a re-run of the whole setup.
#
# Consequence worth being explicit about: that one password is now the key to
# all the others. The database listens on the lab LAN only and the container
# is unprivileged, but treat the bootstrap password as the master secret.
SECRETS_SCHEMA = """
CREATE TABLE IF NOT EXISTS secrets (
    name       VARCHAR(190) NOT NULL,
    value      LONGTEXT     NOT NULL,
    updated_at VARCHAR(32)  NOT NULL,
    updated_by VARCHAR(190) NOT NULL,
    PRIMARY KEY (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""


class MariaDBError(RuntimeError):
    """The audit database could not be reached, or refused a statement."""


class Settings:
    """Where the ledger lives and how to authenticate to it."""

    __slots__ = ("host", "port", "database", "user", "password", "timeout")

    def __init__(self, host: str, *, port: int = DEFAULT_PORT,
                 database: str = DEFAULT_DATABASE, user: str = DEFAULT_USER,
                 password: str = "", timeout: int = DEFAULT_TIMEOUT) -> None:
        if not host:
            raise MariaDBError(
                "no audit database host configured. Set [audit] host in the "
                "config file, or run 'proxmox-lab journal host-setup' to "
                "provision MariaDB on the Proxmox host."
            )
        self.host = host
        self.port = int(port or DEFAULT_PORT)
        self.database = database or DEFAULT_DATABASE
        self.user = user or DEFAULT_USER
        self.password = password
        self.timeout = int(timeout or DEFAULT_TIMEOUT)

    def describe(self) -> str:
        """A safe identity for errors and doctor output. Never the password."""
        return f"{self.user}@{self.host}:{self.port}/{self.database}"


def _require_driver() -> None:
    if pymysql is None:  # pragma: no cover - only without the dependency
        raise MariaDBError(
            "the MariaDB driver is missing. Reinstall the controller so its "
            f"dependencies are present (pip install proxmox-agent-lab): {_IMPORT_ERROR}"
        )


def connect(settings: Settings) -> Any:
    """Open one connection. Callers are responsible for closing it."""
    _require_driver()
    try:
        return pymysql.connect(
            host=settings.host,
            port=settings.port,
            user=settings.user,
            password=settings.password,
            database=settings.database,
            connect_timeout=settings.timeout,
            read_timeout=settings.timeout,
            write_timeout=settings.timeout,
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=True,
        )
    except Exception as exc:  # pymysql raises a family of errors
        raise MariaDBError(f"{settings.describe()}: {exc}") from None


def ensure_schema(settings: Settings) -> None:
    """Create the tables if they are not there yet. Safe to re-run, and safe
    to run from two controllers at once."""
    connection = connect(settings)
    try:
        with connection.cursor() as cursor:
            cursor.execute(SCHEMA)
            cursor.execute(MIGRATIONS_SCHEMA)
            cursor.execute(SECRETS_SCHEMA)
    except Exception as exc:
        raise MariaDBError(f"{settings.describe()}: {exc}") from None
    finally:
        connection.close()


def migrations(settings: Settings) -> list[dict[str, Any]]:
    """Which controllers have already imported their old local ledger."""
    connection = connect(settings)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT controller, migrated_at, source_events, uploaded, "
                "already_present FROM migrations ORDER BY migrated_at ASC"
            )
            return list(cursor.fetchall() or [])
    except Exception as exc:
        raise MariaDBError(f"{settings.describe()}: {exc}") from None
    finally:
        connection.close()


def record_migration(settings: Settings, controller: str, detail: dict[str, Any],
                     *, migrated_at: str) -> None:
    """Note that this controller has carried its history over. Last write for
    a given controller wins, so a re-run updates rather than duplicating."""
    connection = connect(settings)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO migrations (controller, migrated_at, source_events, "
                "uploaded, already_present, detail) VALUES (%s, %s, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE migrated_at=VALUES(migrated_at), "
                "source_events=VALUES(source_events), uploaded=VALUES(uploaded), "
                "already_present=VALUES(already_present), detail=VALUES(detail)",
                (
                    controller, migrated_at,
                    int(detail.get("unique_events") or 0),
                    int(detail.get("uploaded") or 0),
                    int(detail.get("already_present") or 0),
                    json.dumps(detail, sort_keys=True),
                ),
            )
    except Exception as exc:
        raise MariaDBError(f"{settings.describe()}: {exc}") from None
    finally:
        connection.close()


class migration_lock:
    """Serialise migrations across controllers.

    Held for the life of one connection. On timeout the caller is told to try
    again rather than importing concurrently with another machine.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._connection: Any = None

    def __enter__(self) -> "migration_lock":
        self._connection = connect(self._settings)
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT GET_LOCK(%s, %s) AS got",
                           (MIGRATION_LOCK, MIGRATION_LOCK_TIMEOUT))
            row = cursor.fetchone() or {}
        if not row.get("got"):
            self._connection.close()
            raise MariaDBError(
                "another controller is migrating its journal right now "
                f"(waited {MIGRATION_LOCK_TIMEOUT}s). Try again in a moment."
            )
        return self

    def __exit__(self, *exc_info: Any) -> None:
        try:
            with self._connection.cursor() as cursor:
                cursor.execute("SELECT RELEASE_LOCK(%s)", (MIGRATION_LOCK,))
        except Exception:  # pragma: no cover - releasing is best effort
            pass
        finally:
            self._connection.close()


# A reachability probe, not a query. The lab host is off between leases, so
# doctor asks this constantly and must not stall on the statement timeout.
PING_TIMEOUT = 2


def ping(settings: Settings, *, timeout: int = PING_TIMEOUT) -> bool:
    """Is the ledger reachable and does it have its table?"""
    probe = Settings(
        settings.host, port=settings.port, database=settings.database,
        user=settings.user, password=settings.password, timeout=timeout,
    )
    try:
        connection = connect(probe)
    except MariaDBError:
        return False
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM events LIMIT 1")
            cursor.fetchall()
        return True
    except Exception:
        return False
    finally:
        connection.close()


def _insert(cursor: Any, record: dict[str, Any]) -> int:
    """INSERT IGNORE one record. Returns rows written (0 if already present).

    IGNORE, not REPLACE: a replayed spool entry or a re-run migration must be
    a no-op, and an event already in the ledger must never be rewritten.
    """
    vmid = record.get("vmid")
    try:
        vmid = int(vmid) if vmid is not None else None
    except (TypeError, ValueError):
        vmid = None
    cursor.execute(
        "INSERT IGNORE INTO events "
        "(event_id, controller, timestamp, event, lease, vmid, data) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (
            str(record.get("event_id") or ""),
            str(record.get("controller") or ""),
            str(record.get("timestamp") or ""),
            str(record.get("event") or ""),
            record.get("lease"),
            vmid,
            json.dumps(record, sort_keys=True),
        ),
    )
    return int(cursor.rowcount or 0)


def append(settings: Settings, record: dict[str, Any]) -> None:
    """Write one already-redacted event."""
    connection = connect(settings)
    try:
        with connection.cursor() as cursor:
            _insert(cursor, record)
    except Exception as exc:
        raise MariaDBError(f"{settings.describe()}: {exc}") from None
    finally:
        connection.close()


def append_many(settings: Settings, records: list[dict[str, Any]]) -> int:
    """Write a batch in one connection. Returns how many were new."""
    if not records:
        return 0
    connection = connect(settings)
    written = 0
    try:
        with connection.cursor() as cursor:
            for record in records:
                written += _insert(cursor, record)
    except Exception as exc:
        raise MariaDBError(f"{settings.describe()}: {exc}") from None
    finally:
        connection.close()
    return written


def query(
    settings: Settings,
    *,
    limit: int = 50,
    lease: str | None = None,
    event: str | None = None,
    since: str | None = None,
    controller: str | None = None,
) -> list[dict[str, Any]]:
    """Recent events, newest first. ``event`` accepts ``*`` as a wildcard."""
    clauses: list[str] = []
    params: list[Any] = []
    if lease:
        clauses.append("lease = %s")
        params.append(lease)
    if event:
        clauses.append("event LIKE %s")
        params.append(event.replace("*", "%"))
    if since:
        clauses.append("timestamp >= %s")
        params.append(since)
    if controller:
        clauses.append("controller = %s")
        params.append(controller)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    connection = connect(settings)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT data FROM events{where} ORDER BY id DESC LIMIT %s",
                (*params, int(limit)),
            )
            rows = cursor.fetchall()
    except Exception as exc:
        raise MariaDBError(f"{settings.describe()}: {exc}") from None
    finally:
        connection.close()
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            out.append(json.loads(row["data"]))
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
    return out


def summary(settings: Settings) -> dict[str, Any]:
    """Counts and bounds, for `proxmox-lab journal --summary` and doctor."""
    connection = connect(settings)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS total, MIN(timestamp) AS first_event, "
                "MAX(timestamp) AS last_event, "
                "COUNT(DISTINCT lease) AS distinct_leases, "
                "COUNT(DISTINCT controller) AS distinct_controllers "
                "FROM events"
            )
            head = cursor.fetchone() or {}
            cursor.execute(
                "SELECT event, COUNT(*) AS c FROM events "
                "GROUP BY event ORDER BY c DESC LIMIT 10"
            )
            top = cursor.fetchall() or []
    except Exception as exc:
        raise MariaDBError(f"{settings.describe()}: {exc}") from None
    finally:
        connection.close()
    return {
        "database": settings.describe(),
        "exists": True,
        "events": int(head.get("total") or 0),
        "first_event": head.get("first_event"),
        "last_event": head.get("last_event"),
        "distinct_leases": int(head.get("distinct_leases") or 0),
        "distinct_controllers": int(head.get("distinct_controllers") or 0),
        "most_common": {row["event"]: int(row["c"]) for row in top},
    }


def count(settings: Settings) -> int:
    """How many events the ledger holds. Used to verify a migration."""
    connection = connect(settings)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS c FROM events")
            row = cursor.fetchone() or {}
    except Exception as exc:
        raise MariaDBError(f"{settings.describe()}: {exc}") from None
    finally:
        connection.close()
    return int(row.get("c") or 0)


# --- shared secrets -------------------------------------------------------


def get_secret(settings: Settings, name: str) -> str | None:
    """One shared secret, or None. Never raises for a missing table."""
    connection = connect(settings)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT value FROM secrets WHERE name = %s", (name,))
            row = cursor.fetchone()
    except Exception:
        return None
    finally:
        connection.close()
    return str(row["value"]) if row else None


def put_secret(settings: Settings, name: str, value: str, *,
               updated_by: str, updated_at: str) -> None:
    """Store or replace one shared secret."""
    connection = connect(settings)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO secrets (name, value, updated_at, updated_by) "
                "VALUES (%s, %s, %s, %s) ON DUPLICATE KEY UPDATE "
                "value=VALUES(value), updated_at=VALUES(updated_at), "
                "updated_by=VALUES(updated_by)",
                (name, value, updated_at, updated_by),
            )
    except Exception as exc:
        raise MariaDBError(f"{settings.describe()}: {exc}") from None
    finally:
        connection.close()


def list_secrets(settings: Settings) -> list[dict[str, Any]]:
    """Which shared secrets exist. Names and metadata only, never values."""
    connection = connect(settings)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT name, updated_at, updated_by FROM secrets ORDER BY name"
            )
            return list(cursor.fetchall() or [])
    except Exception:
        return []
    finally:
        connection.close()


def delete_secret(settings: Settings, name: str) -> bool:
    connection = connect(settings)
    try:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM secrets WHERE name = %s", (name,))
            return bool(cursor.rowcount)
    except Exception as exc:
        raise MariaDBError(f"{settings.describe()}: {exc}") from None
    finally:
        connection.close()


# --- host provisioning ----------------------------------------------------

HOST_SETUP_SCRIPT = (Path(__file__).parent / "resources" / "ledger-host-setup.sh").read_text()
