"""The host-side lease guard.

The guard runs unattended, as root, and can power the host off. That is a lot
of authority for a component with no operator watching, so the two decisions
it makes -- "is this lease over" and "is this host idle" -- are pinned here.

The script is executed as source rather than imported: it is shipped as a
string and written to /usr/local/lib/pxl-hostguard.py on the Proxmox host, so
testing the string is testing what actually runs.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import types
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from proxmox_agent_lab import hostguard  # noqa: E402


def _guard() -> types.ModuleType:
    module = types.ModuleType("pxl_hostguard")
    module.__dict__["__name__"] = "pxl_hostguard"
    exec(compile(hostguard.GUARD_SCRIPT, "pxl-hostguard.py", "exec"),
         module.__dict__)
    return module


def _stamp(minutes_ago: int = 0) -> str:
    when = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return when.isoformat().replace("+00:00", "Z")


class GuardScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.guard = _guard()

    def test_the_shipped_script_is_valid_python(self) -> None:
        self.assertTrue(hostguard.GUARD_SCRIPT.startswith("#!/usr/bin/env python3"))
        self.assertIn("def main(", hostguard.GUARD_SCRIPT)


class LeaseIsOverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.guard = _guard()

    def test_an_ended_lease_is_over(self) -> None:
        self.assertEqual(
            self.guard.is_over({"lease-end": _stamp()}, 90), "ended"
        )

    def test_an_abandoned_lease_is_over(self) -> None:
        self.assertEqual(
            self.guard.is_over({"lease-abandoned": _stamp()}, 90), "ended"
        )

    def test_a_live_heartbeat_is_not_over(self) -> None:
        events = {"lease-begin": _stamp(300), "lease-heartbeat": _stamp(1)}
        self.assertIsNone(self.guard.is_over(events, 90))

    def test_a_silent_lease_past_the_grace_window_is_abandoned(self) -> None:
        """The eight-day lease this guard exists for."""
        events = {"lease-begin": _stamp(300), "lease-heartbeat": _stamp(200)}
        self.assertEqual(self.guard.is_over(events, 90), "abandoned")

    def test_a_lease_just_begun_is_not_touched(self) -> None:
        self.assertIsNone(self.guard.is_over({"lease-begin": _stamp()}, 90))

    def test_a_lease_with_no_events_is_not_touched(self) -> None:
        self.assertIsNone(self.guard.is_over({}, 90))

    def test_an_unparseable_timestamp_does_not_end_a_lease(self) -> None:
        """Fail towards leaving the guest alone, not towards stopping it."""
        self.assertIsNone(self.guard.is_over({"lease-begin": "not-a-date"}, 90))


class HostIdleTests(unittest.TestCase):
    """When the guard may power the host off.

    Deliberately "is anything running", not "does the ledger know of an open
    lease". A controller that has not been upgraded writes nowhere this guard
    can read, so its guests would look like nobody's work.
    """

    def setUp(self) -> None:
        self.guard = _guard()

    def test_the_ledger_container_alone_counts_as_idle(self) -> None:
        guests = [{"vmid": 9310, "status": "running"},
                  {"vmid": 9001, "status": "stopped"}]
        self.assertEqual(self.guard.anything_running(guests, 9310), [])

    def test_another_controllers_guest_keeps_the_host_up(self) -> None:
        """The property that stops this powering off someone else's work."""
        guests = [{"vmid": 9310, "status": "running"},
                  {"vmid": 9271, "status": "running"}]
        busy = self.guard.anything_running(guests, 9310)
        self.assertEqual([g["vmid"] for g in busy], [9271])

    def test_the_ledger_id_may_be_a_string(self) -> None:
        """It arrives from JSON config, so its type is not guaranteed."""
        guests = [{"vmid": 9310, "status": "running"}]
        self.assertEqual(self.guard.anything_running(guests, "9310"), [])

    def test_an_empty_host_is_idle(self) -> None:
        self.assertEqual(self.guard.anything_running([], 9310), [])

    def test_stopped_guests_do_not_keep_the_host_up(self) -> None:
        guests = [{"vmid": 9001, "status": "stopped"},
                  {"vmid": 9002, "status": "stopped"}]
        self.assertEqual(self.guard.anything_running(guests, 9310), [])


if __name__ == "__main__":
    unittest.main()
