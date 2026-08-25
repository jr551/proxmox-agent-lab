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

HOST_SETUP_SCRIPT = r"""
set -euo pipefail
CTID="__CTID__"; STORAGE="__STORAGE__"; BRIDGE="__BRIDGE__"
DBNAME="__DBNAME__"; DBUSER="__DBUSER__"; DBPASS="__DBPASS__"
HOSTIP="$(hostname -I | awk '{print $1}')"

command -v pct >/dev/null || { echo "pct not found: not a Proxmox host" >&2; exit 1; }

if pct status "$CTID" >/dev/null 2>&1; then
  echo "ledger-exists $CTID"
else
  # Match the host's architecture explicitly. `sort | tail -1` alone picks
  # arm64 over amd64 -- it sorts later -- and the container then refuses to
  # start on an amd64 host with a bare "Failed to spawn container".
  ARCH=$(dpkg --print-architecture)
  TPL=""
  for SERIES in debian-13-standard debian-12-standard; do
    TPL=$(pveam available --section system 2>/dev/null \
          | awk -v s="$SERIES" -v a="_${ARCH}.tar" '$2 ~ s && index($2, a) {print $2}' \
          | sort -V | tail -1)
    [ -n "$TPL" ] && break
  done
  [ -n "$TPL" ] || { echo "no debian $ARCH template available" >&2; exit 1; }
  pveam download local "$TPL" >/dev/null 2>&1 || true
  # nesting: Debian 13 ships systemd 257, which will not boot in an
  # unprivileged container without it.
  pct create "$CTID" "local:vztmpl/$TPL" \
    --hostname pxl-ledger --cores 1 --memory 1024 --swap 512 \
    --rootfs "$STORAGE:8" --net0 "name=eth0,bridge=$BRIDGE,ip=dhcp" \
    --features nesting=1 \
    --unprivileged 1 --onboot 1 --tags codex-lab-infra >/dev/null
  echo "ledger-created $CTID"
fi

pct status "$CTID" | grep -q running || pct start "$CTID"
for _ in $(seq 1 60); do pct exec "$CTID" -- true 2>/dev/null && break; sleep 2; done

pct exec "$CTID" -- bash -c '
  set -euo pipefail
  export DEBIAN_FRONTEND=noninteractive
  if ! command -v mariadbd >/dev/null 2>&1 && ! command -v mysqld >/dev/null 2>&1; then
    apt-get update -qq
    apt-get install -y -qq mariadb-server >/dev/null
  fi
  printf "%s\n" "[mysqld]" "bind-address = 0.0.0.0" \
    > /etc/mysql/mariadb.conf.d/60-pxl.cnf
  systemctl enable --now mariadb >/dev/null 2>&1 || true
  systemctl restart mariadb
'

pct exec "$CTID" -- mariadb -e "
  CREATE DATABASE IF NOT EXISTS \`$DBNAME\`
    CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
  CREATE USER IF NOT EXISTS '$DBUSER'@'%' IDENTIFIED BY '$DBPASS';
  ALTER USER '$DBUSER'@'%' IDENTIFIED BY '$DBPASS';
  GRANT ALL PRIVILEGES ON \`$DBNAME\`.* TO '$DBUSER'@'%';
  FLUSH PRIVILEGES;"

CTIP=""
for _ in $(seq 1 30); do
  CTIP=$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}') || true
  [ -n "$CTIP" ] && break; sleep 2
done
[ -n "$CTIP" ] || { echo "container has no IP yet" >&2; exit 1; }

# Publish the ledger on the hypervisor's own address, so every controller uses
# the same host it already talks Proxmox API to. Persisted so it survives a
# reboot; the container is onboot, the rule must be too.
add_rule() {
  iptables -t nat -C "$@" 2>/dev/null || iptables -t nat -A "$@"
}
add_rule PREROUTING -p tcp --dport 3306 -j DNAT --to-destination "$CTIP:3306"
add_rule OUTPUT -o lo -p tcp --dport 3306 -j DNAT --to-destination "$CTIP:3306"
add_rule POSTROUTING -p tcp -d "$CTIP" --dport 3306 -j MASQUERADE
mkdir -p /etc/network/if-up.d
cat > /etc/network/if-up.d/pxl-ledger-dnat <<EOF
#!/bin/sh
iptables -t nat -C PREROUTING -p tcp --dport 3306 -j DNAT --to-destination $CTIP:3306 2>/dev/null || \
  iptables -t nat -A PREROUTING -p tcp --dport 3306 -j DNAT --to-destination $CTIP:3306
iptables -t nat -C OUTPUT -o lo -p tcp --dport 3306 -j DNAT --to-destination $CTIP:3306 2>/dev/null || \
  iptables -t nat -A OUTPUT -o lo -p tcp --dport 3306 -j DNAT --to-destination $CTIP:3306
iptables -t nat -C POSTROUTING -p tcp -d $CTIP --dport 3306 -j MASQUERADE 2>/dev/null || \
  iptables -t nat -A POSTROUTING -p tcp -d $CTIP --dport 3306 -j MASQUERADE
EOF
chmod +x /etc/network/if-up.d/pxl-ledger-dnat

# The lease guard: enforces cleanup from the host itself, so a controller that
# goes away does not leave guests running for days.
apt-get install -y -qq python3-pymysql >/dev/null 2>&1 || \
  pip3 install --quiet --break-system-packages PyMySQL >/dev/null 2>&1 || true
cat > /etc/pxl-hostguard.json <<EOF
{"host": "$CTIP", "port": 3306, "database": "$DBNAME",
 "user": "$DBUSER", "password": "$DBPASS",
 "grace_minutes": 90, "ledger_ctid": "$CTID",
 "controller_id": "pxl-hostguard"}
EOF
chmod 600 /etc/pxl-hostguard.json
__GUARD_INSTALL__

echo "ledger-ready ctid=$CTID container_ip=$CTIP host_ip=$HOSTIP port=3306"
"""
