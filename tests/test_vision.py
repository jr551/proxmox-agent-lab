from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

os.environ["PROXMOX_AGENT_LAB_CONFIG"] = str(
    Path(__file__).parent / "fixtures" / "config.toml"
)

import shutil
import tempfile
# ...and at a disposable state directory: a test must never write into the
# developer's real controller state. Cleared here so a previous run cannot
# leak into this one; imports all happen before any test runs.
_TEST_STATE = Path(tempfile.gettempdir()) / "proxmox-agent-lab-test-state"
shutil.rmtree(_TEST_STATE, ignore_errors=True)
_TEST_STATE.mkdir(parents=True, exist_ok=True)
os.environ["PROXMOX_AGENT_LAB_STATE"] = str(_TEST_STATE)
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from urllib import error  # noqa: E402

from proxmox_agent_lab import vision  # noqa: E402


class VisionApiTests(unittest.TestCase):
    PNG = b"\x89PNG\r\n\x1a\nfixture"

    def test_sends_png_to_the_documented_nvidia_endpoint(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = json.dumps({
            "choices": [{"message": {"content": json.dumps({
                "screen": "installer", "controls": [],
                "recommended_action": {"kind": "wait"}
            })}}],
            "usage": {"total_tokens": 20},
        }).encode()

        with mock.patch.object(vision.secrets_store, "get",
                               return_value="test-key"), \
             mock.patch.object(vision.request, "urlopen",
                               return_value=response) as opened:
            result = vision.analyze_png(
                mock.Mock(), self.PNG, width=800, height=600, timeout=10,
                provider="nvidia",
            )

        req = opened.call_args.args[0]
        payload = json.loads(req.data)
        self.assertEqual(req.full_url, vision.ENDPOINT)
        self.assertEqual(req.get_header("Authorization"), "Bearer test-key")
        self.assertEqual(payload["model"], vision.MODEL)
        image_url = payload["messages"][1]["content"][1]["image_url"]["url"]
        self.assertTrue(image_url.startswith("data:image/png;base64,"))
        self.assertNotIn("test-key", json.dumps(result))
        self.assertTrue(result["structured"])
        self.assertEqual(result["analysis"]["screen"], "installer")
        self.assertTrue(result["validation"]["actionable"])

    def test_duplicate_click_coordinates_are_not_actionable(self) -> None:
        analysis = {
            "screen": "installer",
            "controls": [
                {"label": "Install", "x": 10, "y": 10},
                {"label": "Try", "x": 10, "y": 10},
            ],
            "recommended_action": {"kind": "click", "value": "10,10"},
        }
        checked = vision._validate_analysis(analysis, 100, 100)
        self.assertFalse(checked["actionable"])
        self.assertFalse(checked["structurally_valid"])
        self.assertTrue(checked["requires_cursor_calibration"])
        self.assertTrue(any("share coordinate" in item
                            for item in checked["warnings"]))

    def test_missing_action_is_never_accepted_as_structured(self) -> None:
        checked = vision._validate_analysis(
            {"screen": "desktop", "controls": []}, 100, 100
        )
        self.assertFalse(checked["structurally_valid"])
        self.assertFalse(checked["actionable"])
        self.assertIn("recommended_action is not an object", checked["warnings"])

    def test_singleton_object_array_is_normalized_but_multiple_are_rejected(self) -> None:
        checkpoint = {"screen": "desktop"}
        self.assertEqual(
            vision._parse_analysis(json.dumps([checkpoint])), checkpoint
        )
        self.assertIsNone(
            vision._parse_analysis(json.dumps([checkpoint, checkpoint]))
        )

    def test_polls_a_202_request_without_exposing_the_key(self) -> None:
        done = {"choices": [{"message": {"content": json.dumps({
            "screen": "visible screen", "controls": [],
            "recommended_action": {"kind": "wait"},
        })}}]}
        with mock.patch.object(vision.secrets_store, "get",
                               return_value="test-key"), \
             mock.patch.object(vision, "_http_json", side_effect=[
                 (202, {"requestId": "request-123"}), (200, done)
             ]) as http, \
             mock.patch.object(vision.time, "sleep"):
            result = vision.analyze_png(
                mock.Mock(), self.PNG, width=2, height=2, timeout=10,
                provider="nvidia",
            )

        poll = http.call_args_list[1].args[0]
        self.assertEqual(poll.full_url, vision.STATUS_ENDPOINT.format(
            request_id="request-123"
        ))
        self.assertEqual(result["analysis"]["screen"], "visible screen")
        self.assertTrue(result["structured"])

    def test_auto_races_all_providers_and_selects_first_valid(self) -> None:
        valid = {
            "provider": "openrouter", "requested_model": vision.OPENROUTER_FREE_MODEL,
            "model": "some/free-vision-model", "structured": True,
            "analysis": {"screen": "installer"},
            "validation": {"structurally_valid": True},
        }
        called = []
        def openrouter(*args, model, **kwargs):
            called.append(model)
            if model == vision.OPENROUTER_MODEL:
                raise vision.VisionError("rate limited")
            return valid
        with mock.patch.object(vision, "_nvidia",
                               side_effect=vision.VisionError("offline")), \
             mock.patch.object(vision, "_openrouter",
                               side_effect=openrouter):
            result = vision.analyze_png(
                mock.Mock(), self.PNG, width=2, height=2, timeout=10
            )

        self.assertEqual(set(called), {
            vision.OPENROUTER_MODEL, vision.OPENROUTER_FREE_MODEL,
        })
        self.assertEqual(result["provider_chain"][-1]["status"], "selected")
        self.assertEqual(result["strategy"], "parallel-first-valid")
        self.assertIn("elapsed_ms", result)

    def test_structurally_invalid_result_falls_through(self) -> None:
        invalid = {
            "provider": "nvidia", "structured": True,
            "validation": {"structurally_valid": False},
        }
        valid = {
            "provider": "openrouter", "structured": True,
            "validation": {"structurally_valid": True},
        }
        with mock.patch.object(vision, "_nvidia", return_value=invalid), \
             mock.patch.object(vision, "_openrouter", return_value=valid) as fallback:
            result = vision.analyze_png(
                mock.Mock(), self.PNG, width=2, height=2, timeout=10
            )
        self.assertTrue(any(item["status"] == "rejected"
                            for item in result["provider_chain"]))
        self.assertGreaterEqual(fallback.call_count, 1)

    def test_project_openrouter_secret_wins_over_stale_shell_value(self) -> None:
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "stale-shell"}), \
             mock.patch.object(vision.secrets_store, "get",
                               return_value="project-key"):
            self.assertEqual(vision._openrouter_key(mock.Mock()), "project-key")

    def test_openrouter_request_contains_image_and_exact_model(self) -> None:
        response = {
            "model": vision.OPENROUTER_MODEL,
            "choices": [{"message": {"content": json.dumps({
                "screen": "desktop", "controls": [],
                "recommended_action": {"kind": "wait"},
            })}}],
        }
        with mock.patch.object(vision.secrets_store, "get",
                               return_value="project-key"), \
             mock.patch.object(vision, "_http_json",
                               return_value=(200, response)) as http:
            result = vision._openrouter(
                mock.Mock(), self.PNG, "inspect", model=vision.OPENROUTER_MODEL,
                width=2, height=2, timeout=10, max_tokens=100,
            )
        req = http.call_args.args[0]
        payload = json.loads(req.data)
        self.assertEqual(req.full_url, vision.OPENROUTER_ENDPOINT)
        self.assertEqual(req.get_header("Authorization"), "Bearer project-key")
        self.assertEqual(payload["model"], vision.OPENROUTER_MODEL)
        self.assertTrue(payload["messages"][0]["content"][1]
                        ["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertTrue(payload["reasoning"]["enabled"])
        self.assertEqual(payload["response_format"]["type"], "json_object")
        self.assertEqual(payload["plugins"], [{"id": "response-healing"}])
        self.assertNotIn("provider", payload)
        self.assertTrue(result["validation"]["structurally_valid"])

    def test_kilo_request_targets_the_gateway_with_the_router_model(self) -> None:
        response = {
            "model": "some-vendor/some-vision-model",
            "choices": [{"message": {"content": json.dumps({
                "screen": "installer", "controls": [],
                "recommended_action": {"kind": "wait"},
            })}}],
        }
        with mock.patch.object(vision.secrets_store, "get",
                               return_value="project-key"), \
             mock.patch.object(vision, "_http_json",
                               return_value=(200, response)) as http:
            result = vision._kilo(
                mock.Mock(), self.PNG, "inspect", width=2, height=2,
                timeout=10, max_tokens=100,
            )
        req = http.call_args.args[0]
        payload = json.loads(req.data)
        self.assertEqual(req.full_url, vision.KILO_ENDPOINT)
        self.assertEqual(req.get_header("Authorization"), "Bearer project-key")
        self.assertEqual(req.get_header("Content-type"), "application/json")
        self.assertEqual(payload["model"], "kilo-auto/balanced")
        self.assertTrue(payload["messages"][0]["content"][1]
                        ["image_url"]["url"].startswith("data:image/png;base64,"))
        # The router may resolve to a reasoning model, which would otherwise
        # spend the whole budget thinking and answer with empty content.
        self.assertEqual(payload["response_format"]["type"], "json_object")
        self.assertFalse(payload["reasoning"]["enabled"])
        # The router id is what was asked for; the response says who answered.
        self.assertEqual(result["provider"], "kilo")
        self.assertEqual(result["requested_model"], vision.KILO_MODEL)
        self.assertEqual(result["model"], "some-vendor/some-vision-model")
        self.assertTrue(result["validation"]["structurally_valid"])

    def test_project_kilo_secret_wins_over_stale_shell_value(self) -> None:
        with mock.patch.dict(os.environ, {"KILO_API_KEY": "stale-shell"}), \
             mock.patch.object(vision.secrets_store, "get",
                               return_value="project-key"):
            self.assertEqual(vision._kilo_key(mock.Mock()), "project-key")

    def test_the_kilo_env_var_is_the_compatibility_fallback(self) -> None:
        with mock.patch.dict(os.environ, {"KILO_API_KEY": "shell-key"}), \
             mock.patch.object(vision.secrets_store, "get", return_value=""):
            self.assertEqual(vision._kilo_key(mock.Mock()), "shell-key")

    def test_a_missing_kilo_key_says_how_to_store_one(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(vision.secrets_store, "get", return_value=""):
            with self.assertRaises(vision.secrets_store.SecretError) as caught:
                vision._kilo_key(mock.Mock())
        message = str(caught.exception)
        self.assertIn("secrets set kilo-api-key", message)
        self.assertIn("KILO_API_KEY", message)

    def test_a_leaked_kilo_key_is_redacted_out_of_an_error(self) -> None:
        """A Kilo key is a three-segment JWT; the shape must never survive."""
        jwt = (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
            ".eyJzdWIiOiJsYWItdGVzdCIsImV4cCI6OTk5OTk5OTk5OX0"
            ".ZmFrZS1zaWduYXR1cmUtZm9yLXRoaXMtdW5pdC10ZXN0Xw"
        )
        for prefix in ("nvapi-abcdef0123456789", "sk-or-v1-abcdef0123456789"):
            self.assertEqual(
                vision.API_KEY_SHAPE.sub("[REDACTED]", f"denied for {prefix}"),
                "denied for [REDACTED]",
            )
        self.assertEqual(
            vision.API_KEY_SHAPE.sub("[REDACTED]", f"bad token {jwt} rejected"),
            "bad token [REDACTED] rejected",
        )
        # Conservative: ordinary prose and a lone word must survive untouched.
        for innocent in ("eyJ is how base64 JSON starts",
                         "a.b.c", "the screen shows an installer"):
            self.assertEqual(
                vision.API_KEY_SHAPE.sub("[REDACTED]", innocent), innocent
            )

    def test_a_kilo_http_error_body_cannot_carry_the_key_out(self) -> None:
        jwt = (
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJsYWItdGVzdCJ9"
            ".c2lnbmF0dXJlLXBsYWNlaG9sZGVy"
        )
        failure = error.HTTPError(
            vision.KILO_ENDPOINT, 401, "Unauthorized", {},
            io.BytesIO(json.dumps(
                {"error": {"message": f"invalid key {jwt}"}}
            ).encode()),
        )
        self.addCleanup(failure.close)
        with mock.patch.object(vision.request, "urlopen", side_effect=failure):
            with self.assertRaises(vision.VisionError) as caught:
                vision._http_json(
                    vision.request.Request(vision.KILO_ENDPOINT), 5, "Kilo"
                )
        message = str(caught.exception)
        self.assertIn("Kilo vision HTTP 401", message)
        self.assertIn("[REDACTED]", message)
        self.assertNotIn(jwt, message)

    def test_empty_content_after_reasoning_says_why(self) -> None:
        """The Kilo router can pick a model that thinks away its whole budget."""
        with self.assertRaises(vision.VisionError) as caught:
            vision._content({"choices": [{"message": {
                "content": "", "reasoning": "thinking at length...",
            }}]})
        self.assertIn("token budget on reasoning", str(caught.exception))

    def test_auto_races_kilo_alongside_the_other_providers(self) -> None:
        valid = {
            "provider": "kilo", "requested_model": vision.KILO_MODEL,
            "model": "some-vendor/some-vision-model", "structured": True,
            "analysis": {"screen": "installer"},
            "validation": {"structurally_valid": True},
        }
        with mock.patch.object(vision, "_nvidia",
                               side_effect=vision.VisionError("offline")), \
             mock.patch.object(vision, "_openrouter",
                               side_effect=vision.VisionError("rate limited")), \
             mock.patch.object(vision, "_kilo", return_value=valid) as kilo:
            result = vision.analyze_png(
                mock.Mock(), self.PNG, width=2, height=2, timeout=10
            )
        kilo.assert_called_once()
        self.assertEqual(result["provider"], "kilo")
        self.assertEqual(result["strategy"], "parallel-first-valid")
        selected = [item for item in result["provider_chain"]
                    if item["status"] == "selected"]
        self.assertEqual([item["provider"] for item in selected], ["kilo"])

    def test_kilo_is_selectable_by_name_and_an_unknown_name_is_not(self) -> None:
        valid = {
            "provider": "kilo", "structured": True,
            "validation": {"structurally_valid": True},
        }
        with mock.patch.object(vision, "_kilo", return_value=valid):
            result = vision.analyze_png(
                mock.Mock(), self.PNG, width=2, height=2, timeout=10,
                provider="kilo",
            )
        self.assertEqual(result["strategy"], "single-provider")
        with self.assertRaises(vision.VisionError):
            vision.analyze_png(
                mock.Mock(), self.PNG, width=2, height=2, provider="kilo-fast"
            )

    def test_provider_keys_reports_each_route_without_revealing_one(self) -> None:
        with mock.patch.dict(os.environ, {"KILO_API_KEY": "shell-key"},
                             clear=True), \
             mock.patch.object(vision.secrets_store, "get", return_value=""):
            keys = vision.provider_keys(mock.Mock())
        self.assertEqual(keys, {"nvidia": False, "openrouter": False,
                                "kilo": True})
        self.assertNotIn("shell-key", json.dumps(keys))
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(vision.secrets_store, "get", return_value=""):
            self.assertFalse(vision.available(mock.Mock()))

    def test_rejects_non_png_before_reading_a_secret(self) -> None:
        with mock.patch.object(vision.secrets_store, "get") as secret:
            with self.assertRaises(vision.VisionError):
                vision.analyze_png(
                    mock.Mock(), b"not an image", width=2, height=2
                )
        secret.assert_not_called()

    def test_target_verification_requires_label_and_coordinate_agreement(self) -> None:
        result = {
            "width": 800, "height": 600,
            "analysis": {
                "controls": [
                    {"label": "Installer", "bbox": [390, 30, 402, 46]},
                ],
                "recommended_action": {"kind": "click", "value": "396,38"},
            },
            "validation": {"structurally_valid": True},
        }
        self.assertTrue(
            vision.verifies_target(result, "Installer", 400, 40)[0]
        )
        self.assertFalse(
            vision.verifies_target(result, "Trash", 400, 40)[0]
        )
        self.assertFalse(
            vision.verifies_target(result, "Installer", 700, 700)[0]
        )

    def test_target_verification_rejects_click_outside_control_bbox(self) -> None:
        """Regression: a coordinate ~100px off the control used to pass."""
        result = {
            "width": 1920, "height": 1200,
            "analysis": {
                "controls": [{"label": "OK", "bbox": [400, 300, 500, 340]}],
                "recommended_action": {"kind": "click", "value": "450,320"},
            },
            "validation": {"structurally_valid": True},
        }
        accepted, reason = vision.verifies_target(result, "OK", 640, 560)
        self.assertFalse(accepted)
        self.assertIn("outside the matched control bounding box", reason)

    def test_target_verification_rejects_degenerate_or_oversized_bbox(self) -> None:
        result = {
            "width": 800, "height": 600,
            "analysis": {
                "controls": [{"label": "OK", "bbox": [400, 300, 400, 340]}],
                "recommended_action": {"kind": "click", "value": "400,320"},
            },
            "validation": {"structurally_valid": True},
        }
        accepted, reason = vision.verifies_target(result, "OK", 400, 320)
        self.assertFalse(accepted)
        self.assertIn("degenerate", reason)
        huge = {
            "width": 800, "height": 600,
            "analysis": {
                "controls": [{"label": "OK", "bbox": [0, 0, 799, 599]}],
                "recommended_action": {"kind": "click", "value": "400,300"},
            },
            "validation": {"structurally_valid": True},
        }
        accepted, reason = vision.verifies_target(huge, "OK", 400, 300)
        self.assertFalse(accepted)
        self.assertIn("more than 60%", reason)

    def test_target_verification_rejects_non_click_and_duplicate_labels(self) -> None:
        result = {
            "analysis": {
                "controls": [
                    {"label": "Install", "x": 10, "y": 10},
                    {"label": "Install now", "x": 20, "y": 20},
                ],
                "recommended_action": {"kind": "stop", "value": ""},
            },
            "validation": {"structurally_valid": True},
        }
        accepted, reason = vision.verifies_target(result, "Install", 10, 10)
        self.assertFalse(accepted)
        self.assertIn("2 controls", reason)

    def test_rejection_summary_includes_validation_reason(self) -> None:
        invalid = {
            "provider": "nvidia", "model": "some/vision-model",
            "structured": True,
            "validation": {
                "structurally_valid": False,
                "warnings": ["screen is not a non-empty string"],
            },
        }
        with mock.patch.object(vision, "_nvidia", return_value=invalid):
            with self.assertRaises(vision.VisionError) as caught:
                vision.analyze_png(
                    mock.Mock(), self.PNG, width=2, height=2, timeout=10,
                    provider="nvidia",
                )
        message = str(caught.exception)
        self.assertIn("some/vision-model", message)
        self.assertIn("screen is not a non-empty string", message)


if __name__ == "__main__":
    unittest.main()
