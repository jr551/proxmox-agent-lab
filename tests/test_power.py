"""Tests for host power control: magic-packet bytes and verified shutdown.

Everything runs against injected senders, probes and a fake clock -- no real
sockets, no network, no sleeps. The golden packet is asserted byte for byte,
and shutdown verification is asserted on its counting rules: both-probe
failures, reset on any success, the window gate, and the loud timeout.
"""

from __future__ import annotations

from pathlib import Path
import sys  # noqa: E402

# Shared bootstrap: fixture configuration plus a per-process state directory,
# applied before any proxmox_agent_lab import. `support` sits beside this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import bootstrap  # noqa: E402,F401

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from types import SimpleNamespace  # noqa: E402
import unittest  # noqa: E402

from proxmox_agent_lab import power  # noqa: E402
from proxmox_agent_lab.errors import LabError  # noqa: E402


def stub_config(mac: str = "aa:bb:cc:dd:ee:ff", broadcast: str = "203.0.113.255",
                port: int = 7) -> SimpleNamespace:
    """The three `[power]` values `wake` reads; nothing else is consulted."""
    return SimpleNamespace(power=SimpleNamespace(mac=mac, broadcast=broadcast, port=port))


class FakeClock:
    """Injected `now`/`sleep`: time advances only when a round sleeps.

    Each sleep advances the clock by a fixed `step` regardless of the pacing
    argument, so a test owns the probe cadence exactly.
    """

    def __init__(self, step: float) -> None:
        self.step = step
        self.t = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += self.step


class BuildMagicPacketTests(unittest.TestCase):
    def test_golden_bytes(self) -> None:
        packet = power.build_magic_packet("aa:bb:cc:dd:ee:ff")
        expected = b"\xff" * 6 + b"\xaa\xbb\xcc\xdd\xee\xff" * 16
        self.assertEqual(packet, expected)
        self.assertEqual(len(packet), 102)

    def test_malformed_mac_raises_power_error(self) -> None:
        cases = (
            "",
            "aa:bb:cc:dd:ee",
            "aa:bb:cc:dd:ee:ff:11",
            "gg:bb:cc:dd:ee:ff",
            "not-a-mac",
            None,
        )
        for mac in cases:
            with self.subTest(mac=mac):
                with self.assertRaises(power.PowerError):
                    power.build_magic_packet(mac)

    def test_power_error_is_operational(self) -> None:
        # The CLI's `_expected_errors` catches LabError; a bad MAC must be one.
        self.assertTrue(issubclass(power.PowerError, LabError))


class WakeTests(unittest.TestCase):
    def test_injected_sender_gets_packet_and_config_address(self) -> None:
        seen: list[tuple[object, bytes, tuple[str, int]]] = []

        def sender(sock: object, packet: bytes, addr: tuple[str, int]) -> None:
            seen.append((sock, packet, addr))

        result = power.wake(stub_config(), sender=sender)

        self.assertEqual(len(seen), 1)
        _, packet, addr = seen[0]
        self.assertEqual(packet, b"\xff" * 6 + b"\xaa\xbb\xcc\xdd\xee\xff" * 16)
        self.assertEqual(addr, ("203.0.113.255", 7))
        self.assertEqual(
            result,
            {
                "sent": True,
                "mac": "aa:bb:cc:dd:ee:ff",
                "broadcast": "203.0.113.255",
                "port": 7,
            },
        )

    def test_bad_config_mac_sends_nothing(self) -> None:
        seen: list[object] = []

        def sender(sock: object, packet: bytes, addr: tuple[str, int]) -> None:
            seen.append((sock, packet, addr))

        with self.assertRaises(power.PowerError):
            power.wake(stub_config(mac="nope"), sender=sender)
        self.assertEqual(seen, [])


class ShutdownVerifiedTests(unittest.TestCase):
    def test_verified_after_all_fail_rounds_reach_window(self) -> None:
        clock = FakeClock(step=6.0)
        kinds: list[str] = []

        def probe(kind: str) -> bool:
            kinds.append(kind)
            return False

        result = power.shutdown_verified(
            request_fn=lambda: None,
            probe_fn=probe,
            window=30.0,
            min_failures=6,
            sleep=clock.sleep,
            now=clock.now,
        )

        self.assertEqual(
            result, {"host_powered_off": True, "failures": 6, "elapsed": 30.0}
        )
        # One round = one of each probe, so the call order alternates ssh/tcp.
        self.assertEqual(kinds, ["ssh", "tcp"] * 6)

    def test_failures_inside_window_do_not_succeed_early(self) -> None:
        clock = FakeClock(step=1.0)
        kinds: list[str] = []

        def probe(kind: str) -> bool:
            kinds.append(kind)
            return False

        result = power.shutdown_verified(
            request_fn=lambda: None,
            probe_fn=probe,
            window=30.0,
            min_failures=6,
            timeout=120.0,
            sleep=clock.sleep,
            now=clock.now,
        )

        # Six all-fail rounds land at t=5s: far too early. The verdict waits
        # for the window -- and only then, never at six failures alone.
        self.assertTrue(result["host_powered_off"])
        self.assertGreater(result["failures"], 6)
        self.assertEqual(result["elapsed"], 30.0)
        self.assertGreater(len(kinds), 12)

    def test_success_resets_the_failure_count(self) -> None:
        clock = FakeClock(step=5.0)
        calls = 0

        def probe(kind: str) -> bool:
            nonlocal calls
            calls += 1
            return (calls + 1) // 2 == 5  # the host answers again in round 5

        result = power.shutdown_verified(
            request_fn=lambda: None,
            probe_fn=probe,
            window=30.0,
            min_failures=6,
            timeout=35.0,
            sleep=clock.sleep,
            now=clock.now,
        )

        # Without the reset the count would reach 7 all-fail rounds and win at
        # t=30s; the mid-way success wipes it, so the timeout is what reports.
        self.assertEqual(
            result, {"host_powered_off": False, "failures": 2, "elapsed": 35.0}
        )

    def test_timeout_reports_not_powered_off(self) -> None:
        clock = FakeClock(step=5.0)

        result = power.shutdown_verified(
            request_fn=lambda: None,
            probe_fn=lambda kind: False,
            window=30.0,
            min_failures=6,
            timeout=10.0,
            sleep=clock.sleep,
            now=clock.now,
        )

        # Loud failure: the caller turns this into a non-zero exit. Never
        # assumed off, never force-off -- just the truth.
        self.assertEqual(
            result, {"host_powered_off": False, "failures": 2, "elapsed": 10.0}
        )

    def test_request_fn_called_exactly_once(self) -> None:
        clock = FakeClock(step=5.0)
        requests: list[int] = []

        power.shutdown_verified(
            request_fn=lambda: requests.append(1),
            probe_fn=lambda kind: False,
            timeout=10.0,
            sleep=clock.sleep,
            now=clock.now,
        )

        self.assertEqual(len(requests), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
