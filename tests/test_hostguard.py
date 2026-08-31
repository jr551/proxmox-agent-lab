"""The host-side lease guard.

The guard runs unattended, as root, and can power the host off. That is a lot
of authority for a component with no operator watching, so the decisions it
makes -- "is this lease over", "is this host idle", and "does a long-term
lease pin the host on" -- are pinned here.

The script is executed as source rather than imported: it is shipped as a
string and written to /usr/local/lib/pxl-hostguard.py on the Proxmox host, so
testing the string is testing what actually runs.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
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


class LongTermLeaseTests(unittest.TestCase):
    """A long-term lease never heartbeats by design, so silence must not read
    as abandonment -- and only a recorded end ends one."""

    def setUp(self) -> None:
        self.guard = _guard()

    def test_a_silent_long_term_lease_is_not_abandoned(self) -> None:
        """The pre-fix behaviour: a live long-term guest stopped as abandoned
        after the grace window, because nothing ever heartbeats it."""
        events = {"lease-begin": _stamp(300)}
        self.assertIsNone(self.guard.is_over(events, 90, long_term=True))

    def test_a_released_long_term_lease_is_ended(self) -> None:
        events = {"lease-begin": _stamp(300),
                  "long-term-released": _stamp(10)}
        self.assertEqual(
            self.guard.is_over(events, 90, long_term=True), "ended"
        )

    def test_a_destroyed_long_term_lease_is_ended(self) -> None:
        events = {"lease-begin": _stamp(300),
                  "long-term-destroyed": _stamp(10)}
        self.assertEqual(
            self.guard.is_over(events, 90, long_term=True), "ended"
        )

    def test_an_ordinary_lease_still_goes_abandoned(self) -> None:
        events = {"lease-begin": _stamp(300)}
        self.assertEqual(self.guard.is_over(events, 90), "abandoned")


class LeaseKindTests(unittest.TestCase):
    """The kind arrives inside the begin event's JSON payload, so a lease
    this guard cannot classify must read as an ordinary session lease."""

    def setUp(self) -> None:
        self.guard = _guard()

    @staticmethod
    def _connection(rows):
        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, *args, **kwargs):
                pass

            def fetchall(self):
                return rows

        class Connection:
            def cursor(self):
                return Cursor()

        return Connection()

    def test_kind_is_read_from_the_begin_event_data(self) -> None:
        rows = [
            {"lease": "lt", "data": json.dumps({"kind": "long-term"})},
            {"lease": "s", "data": json.dumps({"kind": "session"})},
        ]
        self.assertEqual(
            self.guard.lease_kinds(self._connection(rows)),
            {"lt": "long-term", "s": "session"},
        )

    def test_an_unparseable_or_kindless_begin_reads_as_absent(self) -> None:
        rows = [
            {"lease": "broken", "data": "not json"},
            {"lease": "empty", "data": None},
            {"lease": "kindless", "data": json.dumps({"purpose": "x"})},
        ]
        self.assertEqual(
            self.guard.lease_kinds(self._connection(rows)), {}
        )

    def test_the_shipped_query_knows_the_long_term_endings(self) -> None:
        """Without them a destroyed long-term lease would pin the host on
        forever: its end never arrives as a lease-end event."""
        for event in ("long-term-destroyed", "long-term-released"):
            self.assertIn(event, hostguard.GUARD_SCRIPT)


class PinTests(unittest.TestCase):
    """An active long-term lease keeps the host on with nothing running --
    the one ledger consult the power-off makes, and the only safe one: no
    controller old enough to lack the feature can create the pin."""

    def setUp(self) -> None:
        self.guard = _guard()

    def test_an_active_long_term_lease_pins_the_host(self) -> None:
        leases = {
            "lt": {"lease-begin": _stamp()},
            "s": {"lease-begin": _stamp()},
        }
        kinds = {"lt": "long-term", "s": "session"}
        self.assertEqual(self.guard.pinned_leases(leases, kinds), ["lt"])

    def test_an_ended_long_term_lease_does_not_pin(self) -> None:
        for end_event in ("long-term-destroyed", "long-term-released",
                          "lease-end", "lease-abandoned"):
            leases = {"lt": {"lease-begin": _stamp(300), end_event: _stamp(5)}}
            self.assertEqual(
                self.guard.pinned_leases(leases, {"lt": "long-term"}), [],
                f"end event {end_event} did not lift the pin",
            )

    def test_a_session_lease_never_pins(self) -> None:
        leases = {"s": {"lease-begin": _stamp()}}
        self.assertEqual(self.guard.pinned_leases(leases, {"s": "session"}), [])

    def test_an_unclassified_lease_never_pins(self) -> None:
        """No kind in the ledger means no pin: an un-upgraded controller's
        world must look exactly as it did before the pin existed."""
        leases = {"s": {"lease-begin": _stamp()}}
        self.assertEqual(self.guard.pinned_leases(leases, {}), [])


class GuardStateTests(unittest.TestCase):
    """The idle counter survives between sweeps in a file, so a torn write
    must never leave it half-written."""

    def setUp(self) -> None:
        self.guard = _guard()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.guard.STATE_PATH = str(Path(tmp.name) / "state.json")

    def test_write_then_read_round_trips(self) -> None:
        self.guard.write_state({"idle_checks": 2})
        self.assertEqual(self.guard.read_state(), {"idle_checks": 2})

    def test_an_unreadable_state_file_reads_as_zero(self) -> None:
        Path(self.guard.STATE_PATH).write_text("{broken", encoding="utf-8")
        self.assertEqual(self.guard.read_state(), {"idle_checks": 0})

    def test_a_rewrite_leaves_no_temp_file_behind(self) -> None:
        self.guard.write_state({"idle_checks": 2})
        self.guard.write_state({"idle_checks": 3})
        self.assertEqual(self.guard.read_state(), {"idle_checks": 3})
        self.assertFalse(Path(self.guard.STATE_PATH + ".tmp").exists())


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
