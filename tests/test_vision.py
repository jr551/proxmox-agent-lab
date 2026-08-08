from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

os.environ["PROXMOX_AGENT_LAB_CONFIG"] = str(
    Path(__file__).parent / "fixtures" / "config.toml"
)
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

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
                mock.Mock(), self.PNG, width=800, height=600, timeout=10
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

    def test_polls_a_202_request_without_exposing_the_key(self) -> None:
        done = {"choices": [{"message": {"content": "visible screen"}}]}
        with mock.patch.object(vision.secrets_store, "get",
                               return_value="test-key"), \
             mock.patch.object(vision, "_http_json", side_effect=[
                 (202, {"requestId": "request-123"}), (200, done)
             ]) as http, \
             mock.patch.object(vision.time, "sleep"):
            result = vision.analyze_png(
                mock.Mock(), self.PNG, width=2, height=2, timeout=10
            )

        poll = http.call_args_list[1].args[0]
        self.assertEqual(poll.full_url, vision.STATUS_ENDPOINT.format(
            request_id="request-123"
        ))
        self.assertEqual(result["analysis"], "visible screen")
        self.assertFalse(result["structured"])

    def test_rejects_non_png_before_reading_a_secret(self) -> None:
        with mock.patch.object(vision.secrets_store, "get") as secret:
            with self.assertRaises(vision.VisionError):
                vision.analyze_png(
                    mock.Mock(), b"not an image", width=2, height=2
                )
        secret.assert_not_called()


if __name__ == "__main__":
    unittest.main()
