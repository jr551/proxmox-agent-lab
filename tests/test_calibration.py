"""Tests for the ported coordinate calibration.

Ported from vnc-mcp (BSD 2-Clause); see NOTICE. These pin the parts that
decide whether a click lands on target: the per-axis least-squares solve,
its degenerate-input guard, the RMSE trust threshold, the tolerant sample
parser, the stable identity/record keys, and the atomic store.

The interesting property is that an *exact* transform round-trips to the
pixel, and that a bad submission is refused rather than silently fitted to
noise. Everything runs in a temp state root -- no network, no host.
"""

from __future__ import annotations

from pathlib import Path
import sys

# Shared bootstrap: fixture configuration plus a per-process state directory,
# applied before any proxmox_agent_lab import. `support` sits beside this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import bootstrap  # noqa: E402,F401
import tempfile  # noqa: E402
import unittest  # noqa: E402

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from proxmox_agent_lab import calibration as cal  # noqa: E402


class SolveTests(unittest.TestCase):
    def test_an_exact_transform_round_trips_to_the_pixel(self) -> None:
        # A client showing a 1920x1080 framebuffer at exactly half scale.
        # fb = 2 * obs must return the original pixel, not an approximation.
        markers = [
            cal.Marker(id=f"M{i}", fb_x=x, fb_y=y, obs_x=x / 2, obs_y=y / 2)
            for i, (x, y) in enumerate(
                [(0, 0), (100, 0), (200, 0), (0, 100), (100, 100)], start=1
            )
        ]
        solution = cal.solve(markers)
        self.assertIsNotNone(solution)
        assert solution is not None
        self.assertAlmostEqual(solution.x_a, 2.0)
        self.assertAlmostEqual(solution.y_a, 2.0)
        self.assertAlmostEqual(solution.rmse, 0.0)
        self.assertTrue(solution.trustworthy)

    def test_axes_are_solved_independently(self) -> None:
        # Letterboxed: x at half scale, y at quarter. One shared factor
        # would be wrong on the y axis.
        markers = [
            cal.Marker(id="M1", fb_x=100, fb_y=100, obs_x=50, obs_y=25),
            cal.Marker(id="M2", fb_x=200, fb_y=200, obs_x=100, obs_y=50),
            cal.Marker(id="M3", fb_x=300, fb_y=300, obs_x=150, obs_y=75),
        ]
        solution = cal.solve(markers)
        assert solution is not None
        self.assertAlmostEqual(solution.x_a, 2.0)
        self.assertAlmostEqual(solution.y_a, 4.0)

    def test_a_noisy_fit_reports_its_error_and_is_not_trusted(self) -> None:
        markers = [
            cal.Marker(id="M1", fb_x=100, fb_y=100, obs_x=50, obs_y=50),
            cal.Marker(id="M2", fb_x=200, fb_y=200, obs_x=100, obs_y=110),
            cal.Marker(id="M3", fb_x=300, fb_y=300, obs_x=150, obs_y=150),
        ]
        solution = cal.solve(markers)
        assert solution is not None
        self.assertGreater(solution.rmse, cal.ACCEPTABLE_RMSE)
        self.assertFalse(solution.trustworthy)

    def test_one_marker_cannot_determine_a_transform(self) -> None:
        self.assertIsNone(
            cal.solve([cal.Marker(id="M1", fb_x=1, fb_y=1, obs_x=1, obs_y=1)])
        )

    def test_identical_observations_are_degenerate_not_a_division_by_zero(self) -> None:
        # Every client reading the same point: no slope can be recovered,
        # and a fit must be refused rather than returned as garbage.
        markers = [
            cal.Marker(id="M1", fb_x=10, fb_y=10, obs_x=5, obs_y=5),
            cal.Marker(id="M2", fb_x=20, fb_y=20, obs_x=5, obs_y=5),
        ]
        self.assertIsNone(cal.solve(markers))


class RecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(cal.config_module.reset_cache)
        import os

        self._env = os.environ.get("PROXMOX_AGENT_LAB_STATE")
        os.environ["PROXMOX_AGENT_LAB_STATE"] = self._tmp.name
        self.addCleanup(self._restore_env)

    def _restore_env(self) -> None:
        import os

        if self._env is None:
            os.environ.pop("PROXMOX_AGENT_LAB_STATE", None)
        else:
            os.environ["PROXMOX_AGENT_LAB_STATE"] = self._env

    def test_client_ids_fold_separators_and_case(self) -> None:
        for raw in ("My IDE", "my_ide", "My.Ide", "  MY IDE  "):
            self.assertEqual(cal.normalize_client_id(raw), "my-ide")

    def test_generic_identities_are_refused(self) -> None:
        # A calibration keyed on "client" would be shared by every session
        # using that name, but the transform depends on how that viewer
        # scales images.
        for name in ("", "  ", "test", "MCP", "client", "unknown"):
            self.assertFalse(cal.is_usable_client(cal.normalize_client_id(name)))

    def test_record_id_ignores_the_client_version(self) -> None:
        # A client upgrade that does not change image scaling must keep the
        # existing calibration rather than silently going stale.
        first = cal.record_id("my-ide", "9101", 1920, 1080)
        self.assertEqual(first, cal.record_id("my-ide", "9101", 1920, 1080))
        self.assertNotEqual(first, cal.record_id("my-ide", "9101", 1280, 720))
        self.assertNotEqual(first, cal.record_id("other", "9101", 1920, 1080))

    def test_a_saved_record_round_trips_and_preserves_created_at(self) -> None:
        first = cal.Record(
            client_id="my-ide", client_name="My IDE", endpoint_id="9101",
            width=1920, height=1080, x_a=2.0, x_b=0.0, y_a=2.0, y_b=0.0,
            rmse=0.4, rounds=1, created_at=1000, updated_at=1000,
        )
        cal.upsert(first)
        loaded = cal.find("my-ide", "9101", 1920, 1080)
        assert loaded is not None
        self.assertEqual(loaded.x_a, 2.0)
        self.assertEqual(loaded.created_at, 1000)

        cal.upsert(
            cal.Record(
                client_id="my-ide", client_name="My IDE", endpoint_id="9101",
                width=1920, height=1080, x_a=1.5, x_b=1.0, y_a=1.5, y_b=1.0,
                rmse=0.2, rounds=2, created_at=0, updated_at=2000,
            )
        )
        replaced = cal.find("my-ide", "9101", 1920, 1080)
        assert replaced is not None
        self.assertEqual(replaced.x_a, 1.5)
        self.assertEqual(replaced.created_at, 1000, "created_at must survive")
        self.assertEqual(len(cal.any_for_client("my-ide")), 1)

    def test_a_miss_is_a_miss_not_a_crash(self) -> None:
        self.assertIsNone(cal.find("nobody", "9999", 640, 480))
        self.assertEqual(cal.any_for_client("nobody"), [])

    def test_a_corrupt_store_is_reported_not_fatal(self) -> None:
        cal.path().parent.mkdir(parents=True, exist_ok=True)
        cal.path().write_text("{not json", encoding="utf-8")
        self.assertIn("error", cal.load())
        self.assertIsNone(cal.find("my-ide", "9101", 1920, 1080))
        self.assertIn("UNKNOWN", cal.status_line("My IDE", "9101", 1920, 1080))

    def test_remove_deletes_exactly_one(self) -> None:
        for endpoint, width in (("9101", 1920), ("9102", 1280)):
            cal.upsert(
                cal.Record(
                    client_id="my-ide", client_name="My IDE",
                    endpoint_id=endpoint, width=width, height=1080,
                    x_a=1.0, x_b=0.0, y_a=1.0, y_b=0.0, rmse=0.1, rounds=1,
                    created_at=1, updated_at=1,
                )
            )
        self.assertTrue(cal.remove("my-ide", "9101", 1920, 1080))
        self.assertFalse(cal.remove("my-ide", "9101", 1920, 1080))
        self.assertIsNone(cal.find("my-ide", "9101", 1920, 1080))
        self.assertIsNotNone(cal.find("my-ide", "9102", 1280, 1080))

    def test_the_store_is_never_half_written(self) -> None:
        # A truncated calibration file would read as corrupt on the next
        # run and silently disable calibration everywhere.
        cal.upsert(
            cal.Record(
                client_id="my-ide", client_name="My IDE", endpoint_id="9101",
                width=1920, height=1080, x_a=2.0, x_b=0.0, y_a=2.0, y_b=0.0,
                rmse=0.1, rounds=1, created_at=1, updated_at=1,
            )
        )
        self.assertFalse(cal.path().with_name(cal.path().name + ".tmp").exists())
        self.assertIn("calibrations", cal.path().read_text(encoding="utf-8"))


class MarkerTests(unittest.TestCase):
    def test_the_grid_is_inset_and_spread(self) -> None:
        grid = cal.marker_positions(1920, 1080)
        self.assertEqual(len(grid), 9)
        self.assertEqual(len({mid for mid, _, _ in grid}), 9)
        xs = sorted({x for _, x, _ in grid})
        ys = sorted({y for _, _, y in grid})
        self.assertEqual(len(xs), 3)
        self.assertEqual(len(ys), 3)
        # inset from every edge, so a marker is never on a croppable border
        self.assertGreater(min(xs), 0)
        self.assertLess(max(xs), 1920)
        self.assertGreater(min(ys), 0)
        self.assertLess(max(ys), 1080)

    def test_build_markers_pairs_readings_with_the_grid(self) -> None:
        samples = [
            {"id": "M1", "x": 144, "y": 81},
            {"id": "M5", "x": 960, "y": 540},
        ]
        markers = cal.build_markers(1920, 1080, samples)
        self.assertEqual([m.id for m in markers], ["M1", "M5"])
        first = markers[0]
        self.assertAlmostEqual(first.fb_x, 1920 * 0.15)
        self.assertAlmostEqual(first.obs_x, 144)

    def test_unusable_samples_are_dropped_not_guessed(self) -> None:
        samples = [
            {"id": "M1", "x": 10, "y": 10},
            {"id": "bogus", "x": 1, "y": 1},
            {"id": "M2"},                      # no coordinates
            {"id": "M3", "x": "left", "y": 2},  # not a number
            "not a dict",
        ]
        markers = cal.build_markers(1920, 1080, samples)
        self.assertEqual([m.id for m in markers], ["M1"])
        self.assertEqual(cal.build_markers(1920, 1080, "nonsense"), [])

    def test_a_partial_submission_still_calibrates(self) -> None:
        # A client that reports eight of nine markers should still work.
        samples = [{"id": f"M{i}", "x": i * 10.0, "y": i * 10.0} for i in range(1, 9)]
        markers = cal.build_markers(1920, 1080, samples)
        self.assertEqual(len(markers), 8)
        self.assertIsNotNone(cal.solve(markers))


class StatusLineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        import os

        self._env = os.environ.get("PROXMOX_AGENT_LAB_STATE")
        os.environ["PROXMOX_AGENT_LAB_STATE"] = self._tmp.name
        self.addCleanup(self._restore_env)

    def _restore_env(self) -> None:
        import os

        if self._env is None:
            os.environ.pop("PROXMOX_AGENT_LAB_STATE", None)
        else:
            os.environ["PROXMOX_AGENT_LAB_STATE"] = self._env

    def test_absent_names_the_endpoint_and_says_clicks_are_unsafe(self) -> None:
        line = cal.status_line("My IDE", "9101", 1920, 1080)
        self.assertIn("ABSENT", line)
        self.assertIn("9101", line)
        self.assertIn("INACCURATE", line)

    def test_a_saved_record_reads_active_with_its_error(self) -> None:
        cal.upsert(
            cal.Record(
                client_id="my-ide", client_name="My IDE", endpoint_id="9101",
                width=1920, height=1080, x_a=2.0, x_b=0.0, y_a=2.0, y_b=0.0,
                rmse=0.8, rounds=1, created_at=1, updated_at=1,
            )
        )
        line = cal.status_line("My IDE", "9101", 1920, 1080)
        self.assertIn("ACTIVE", line)
        self.assertIn("0.8", line)

    def test_a_resolution_change_reads_stale_not_active(self) -> None:
        # The same guest at a different resolution is a different transform.
        cal.upsert(
            cal.Record(
                client_id="my-ide", client_name="My IDE", endpoint_id="9101",
                width=1920, height=1080, x_a=2.0, x_b=0.0, y_a=2.0, y_b=0.0,
                rmse=0.5, rounds=1, created_at=1, updated_at=1,
            )
        )
        line = cal.status_line("My IDE", "9101", 1280, 720)
        self.assertIn("STALE", line)
        self.assertIn("1280x720", line)

    def test_a_generic_client_is_told_to_use_the_reported_resolution(self) -> None:
        line = cal.status_line("mcp", "9101", 1920, 1080)
        self.assertIn("DISABLED", line)

    def test_an_unidentified_client_is_unknown(self) -> None:
        self.assertIn("UNKNOWN", cal.status_line("", "9101", 1920, 1080))


class MappingTests(unittest.TestCase):
    def test_a_mapped_point_is_clamped_into_the_framebuffer(self) -> None:
        record = cal.Record(
            client_id="my-ide", client_name="My IDE", endpoint_id="9101",
            width=1920, height=1080, x_a=2.0, x_b=0.0, y_a=2.0, y_b=0.0,
            rmse=0.1, rounds=1, created_at=1, updated_at=1,
        )
        self.assertEqual(record.to_framebuffer(100, 50), (200, 100))
        # never out of bounds, whatever the client asks for
        self.assertEqual(record.to_framebuffer(-9999, -9999), (0, 0))
        self.assertEqual(record.to_framebuffer(99999, 99999), (1919, 1079))

    def test_offset_is_applied_not_just_scaled(self) -> None:
        # A cropped client is a scale *and* an offset.
        record = cal.Record(
            client_id="my-ide", client_name="My IDE", endpoint_id="9101",
            width=1920, height=1080, x_a=1.0, x_b=100.0, y_a=1.0, y_b=50.0,
            rmse=0.1, rounds=1, created_at=1, updated_at=1,
        )
        self.assertEqual(record.to_framebuffer(10, 10), (110, 60))


if __name__ == "__main__":
    unittest.main()
