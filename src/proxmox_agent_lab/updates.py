"""The daily, fail-open upstream release check.

At most once per 24 hours the CLI asks GitHub for the latest release and
prints a one-line notice on stderr when a newer version exists. The check is
deliberately fail-open: a GitHub outage must never block lab work, and the
result is cached under the state directory so the network is not paid for on
every invocation.
"""

from __future__ import annotations

import json
import re
import sys
import time
from typing import Any
from urllib import request

from .state import json_dump

UPDATE_CHECK_URL = (
    "https://api.github.com/repos/jr551/proxmox-agent-lab/releases/latest"
)
UPDATE_CHECK_INTERVAL_SECONDS = 86400


def check_for_updates(state_root: Any, version: str,
                      *, now: float | None = None
                      ) -> dict[str, Any]:
    """Check GitHub at most daily; network failure must never block the lab."""
    checked_at = time.time() if now is None else now
    cache = state_root / "github-update-check.json"
    try:
        previous = json.loads(cache.read_text())
    except (OSError, ValueError, TypeError):
        previous = {}
    last = previous.get("checked_at", 0)
    if (
        previous.get("current") == version
        and isinstance(last, (int, float))
        and checked_at - last < UPDATE_CHECK_INTERVAL_SECONDS
    ):
        return {**previous, "cached": True}

    result: dict[str, Any] = {
        "checked_at": checked_at,
        "current": version,
        "latest": None,
        "update_available": False,
        "cached": False,
    }
    try:
        req = request.Request(
            UPDATE_CHECK_URL,
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": f"proxmox-agent-lab/{version}"},
        )
        with request.urlopen(req, timeout=3) as response:
            payload = json.load(response)
        tag = str(payload.get("tag_name", "")).strip()
        latest = tag.removeprefix("v")
        if re.fullmatch(r"\d+(?:\.\d+){1,3}", latest):
            result["latest"] = latest
            current_parts = tuple(int(x) for x in version.split("."))
            latest_parts = tuple(int(x) for x in latest.split("."))
            result["update_available"] = latest_parts > current_parts
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        result["error"] = "github update check unavailable"
    try:
        json_dump(cache, result)
    except OSError:
        pass
    return result


def update_notice(state_root: Any, version: str) -> None:
    result = check_for_updates(state_root, version)
    if result.get("update_available"):
        print(
            f"notice: proxmox-agent-lab {result['latest']} is available on "
            "GitHub; update before starting new work when practical",
            file=sys.stderr,
        )
