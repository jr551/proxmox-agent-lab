"""The audit facade: redaction, ledger caching, spool and migration.

Every action in the lab appends one redacted event. The durable store is the
shared MariaDB ledger (``journal``/``mariadb`` own the storage details); when
the lab host is off the events spool locally under the journal directory and
are uploaded later. This module coordinates that without owning the wire
protocol, the schema, or the command surface.

Configuration and the journal root are supplied by the caller: ``cli`` binds
the process-wide configuration and keeps the path patchable for tests. The
ledger settings and the one-shot flags are cached *here*, for the life of the
process, for the same reason they used to be module globals on the CLI.
"""

from __future__ import annotations

import re
import socket
import sys
import uuid
from typing import Any

from . import journal as journal_module
from . import mariadb as mariadb_module  # noqa: F401  (settings types/ledger)
from . import secrets_store
from .state import utc_now

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


def controller_id(config: Any) -> str:
    """This machine's name in the shared ledger."""
    return str(config.audit.get("controller_id") or socket.gethostname())


_LEDGER_CACHE: Any = False


def ledger(config: Any) -> Any:
    """Settings for the shared MariaDB ledger, or None if not configured yet.

    Cached for the life of the process: this is consulted on every audited
    action, and rebuilding it each time would re-read the bootstrap secret.
    """
    global _LEDGER_CACHE
    if _LEDGER_CACHE is False:
        try:
            secret = secrets_store.get(
                config, secrets_store.BOOTSTRAP_SECRET, required=False
            )
        except secrets_store.SecretError:
            secret = ""
        _LEDGER_CACHE = journal_module.settings_from_config(config, secret)
    return _LEDGER_CACHE


def prime_ledger_cache(settings: Any) -> None:
    """Record freshly provisioned settings so this process uses them now."""
    global _LEDGER_CACHE
    _LEDGER_CACHE = settings


_AUTO_MIGRATED = False


def auto_migrate_once(config: Any, journal_root: Any) -> None:
    """Carry a controller upgraded from an older release into the shared ledger.

    Runs at most once per process, and at most once per machine (a marker file
    records it). Silent and non-fatal: an upgrade must not turn the first
    command after it into a failure.
    """
    global _AUTO_MIGRATED
    if _AUTO_MIGRATED:
        return
    _AUTO_MIGRATED = True
    settings = ledger(config)
    if settings is None or journal_module.migration_done(journal_root):
        return
    detail = journal_module.auto_migrate(
        settings, journal_root, controller=controller_id(config)
    )
    if detail and detail.get("uploaded"):
        print(
            f"notice: carried {detail['uploaded']} event(s) from this "
            "controller's previous local ledger into the shared MariaDB "
            "ledger. The old files were left in place.",
            file=sys.stderr,
        )


_SPOOL_NOTICE_SHOWN = False


def _note_spooling(journal_root: Any) -> None:
    """Say once per run that the ledger is unreachable and events are queued."""
    global _SPOOL_NOTICE_SHOWN
    _SPOOL_NOTICE_SHOWN = True
    print(
        "notice: the audit ledger is unreachable; events are being spooled to "
        f"{journal_module.spool_path(journal_root)}. Upload them with "
        "'proxmox-lab journal --flush-spool' once the lab host is up.",
        file=sys.stderr,
    )


def audit(config: Any, journal_root: Any, event: str, **fields: Any) -> None:
    """Append one redacted event to the shared ledger.

    Never fails the action being audited. The lab host is powered off between
    leases by design, so an unreachable ledger spools locally and is uploaded
    later by 'proxmox-lab journal --flush-spool'.
    """
    now = utc_now()
    record = {
        "timestamp": now.isoformat().replace("+00:00", "Z"),
        "event": event,
        "event_id": uuid.uuid4().hex,
        "controller": controller_id(config),
        **redact(fields),
    }
    auto_migrate_once(config, journal_root)
    outcome = journal_module.record(
        ledger(config), journal_root, record, controller=controller_id(config)
    )
    if outcome == "spooled" and not _SPOOL_NOTICE_SHOWN:
        _note_spooling(journal_root)
