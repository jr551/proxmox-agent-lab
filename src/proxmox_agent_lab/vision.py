"""Optional NVIDIA vision analysis for graphical lab screenshots.

The runtime remains standard-library only. Images are sent only by the
explicit ``console inspect`` command; merely taking a screenshot never uploads
it. The API key is fetched from the configured secret backend and is never
included in output or errors.
"""

from __future__ import annotations

import base64
import json
import re
import time
from typing import Any
from urllib import error, request

from . import secrets_store

ENDPOINT = "https://integrate.api.nvidia.com/v1/chat/completions"
STATUS_ENDPOINT = "https://integrate.api.nvidia.com/v1/status/{request_id}"
MODEL = "nvidia/nemotron-nano-12b-v2-vl"
SECRET_ACCOUNT = "nvidia-api-key"
REQUEST_ID = re.compile(r"^[A-Za-z0-9-]{1,36}$")
API_KEY_SHAPE = re.compile(r"nvapi-[A-Za-z0-9_-]+")

DEFAULT_PROMPT = """Analyze this operating-system GUI screenshot. Return only JSON:
{
  "screen": "short checkpoint name",
  "summary": "what is visibly happening",
  "controls": [
    {"label": "visible control", "x": 0, "y": 0, "confidence": 0.0}
  ],
  "recommended_action": {
    "kind": "key|click|wait|stop",
    "value": "key name or x,y",
    "reason": "why this is the safest single next action"
  },
  "expected_change": "screen expected after that one action",
  "warnings": []
}
Coordinates must be the visual center of each control in pixels in the original
image. Never give two different controls the same coordinates. Do not invent
unreadable labels or controls. Use kind=stop when uncertain or when a
destructive disk choice cannot be verified from the image."""


class VisionError(RuntimeError):
    pass


def available(config: Any) -> bool:
    return bool(secrets_store.get(config, SECRET_ACCOUNT, required=False))


def _http_json(req: request.Request, timeout: float) -> tuple[int, dict[str, Any]]:
    try:
        with request.urlopen(req, timeout=timeout) as response:
            status = response.status
            body = response.read()
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        detail = API_KEY_SHAPE.sub("[REDACTED]", detail)
        raise VisionError(f"NVIDIA vision HTTP {exc.code}: {detail}") from None
    except (error.URLError, TimeoutError, OSError) as exc:
        raise VisionError(f"NVIDIA vision unavailable: {exc}") from None
    try:
        value = json.loads(body)
    except (TypeError, ValueError):
        raise VisionError("NVIDIA vision returned invalid JSON") from None
    if not isinstance(value, dict):
        raise VisionError("NVIDIA vision returned a non-object response")
    return status, value


def _request_id(value: dict[str, Any]) -> str:
    candidate = value.get("requestId") or value.get("request_id") or ""
    if not isinstance(candidate, str) or not REQUEST_ID.fullmatch(candidate):
        raise VisionError("NVIDIA vision returned 202 without a valid requestId")
    return candidate


def _content(value: dict[str, Any]) -> str:
    try:
        content = value["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise VisionError("NVIDIA vision response has no assistant content") from None
    if not isinstance(content, str) or not content.strip():
        raise VisionError("NVIDIA vision returned empty assistant content")
    return content.strip()


def _parse_analysis(content: str) -> dict[str, Any] | None:
    candidate = content.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        candidate = "\n".join(lines)
    try:
        value = json.loads(candidate)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _validate_analysis(value: dict[str, Any], width: int,
                       height: int) -> dict[str, Any]:
    warnings: list[str] = []
    positions: dict[tuple[int, int], list[str]] = {}
    controls = value.get("controls")
    if not isinstance(controls, list):
        controls = []
        warnings.append("controls is not a list")
    for control in controls:
        if not isinstance(control, dict):
            warnings.append("control is not an object")
            continue
        x, y = control.get("x"), control.get("y")
        label = str(control.get("label") or "unlabelled")
        if not isinstance(x, int) or not isinstance(y, int):
            warnings.append(f"{label}: coordinates are not integers")
            continue
        if not (0 <= x < width and 0 <= y < height):
            warnings.append(f"{label}: coordinates are outside the framebuffer")
            continue
        positions.setdefault((x, y), []).append(label)
    for (x, y), labels in positions.items():
        if len(set(labels)) > 1:
            warnings.append(
                f"different controls share coordinate {x},{y}: "
                + ", ".join(labels)
            )

    action = value.get("recommended_action")
    click = isinstance(action, dict) and action.get("kind") == "click"
    if click:
        match = re.fullmatch(r"\s*(\d+)\s*,\s*(\d+)\s*", str(action.get("value", "")))
        if not match:
            warnings.append("recommended click is not an x,y coordinate")
        else:
            point = (int(match.group(1)), int(match.group(2)))
            if not (0 <= point[0] < width and 0 <= point[1] < height):
                warnings.append("recommended click is outside the framebuffer")
            elif len(positions.get(point, [])) != 1:
                warnings.append(
                    "recommended click does not identify exactly one control"
                )
    return {
        "structurally_valid": not warnings,
        "actionable": not warnings and not click,
        "requires_cursor_calibration": click,
        "warnings": warnings,
    }


def analyze_png(config: Any, image: bytes, *, width: int, height: int,
                prompt: str | None = None, timeout: int = 120,
                max_tokens: int = 1024) -> dict[str, Any]:
    if not image.startswith(b"\x89PNG\r\n\x1a\n"):
        raise VisionError("NVIDIA vision wrapper accepts PNG screenshots only")
    if not 1 <= max_tokens <= 8192:
        raise VisionError("max_tokens must be between 1 and 8192")
    api_key = secrets_store.get(config, SECRET_ACCOUNT)
    task = (prompt or DEFAULT_PROMPT) + (
        f"\nThe original framebuffer is {width}x{height} pixels."
    )
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "/no_think"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": task},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,"
                            + base64.b64encode(image).decode("ascii")
                        },
                    },
                ],
            },
        ],
        "max_tokens": max_tokens,
        "temperature": 0.1,
        "stream": False,
    }
    req = request.Request(
        ENDPOINT,
        data=json.dumps(payload, separators=(",", ":")).encode(),
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    deadline = time.monotonic() + timeout
    status, value = _http_json(req, timeout)
    while status == 202:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise VisionError(f"NVIDIA vision did not finish within {timeout}s")
        request_id = _request_id(value)
        time.sleep(min(2.0, remaining))
        poll = request.Request(
            STATUS_ENDPOINT.format(request_id=request_id),
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="GET",
        )
        status, value = _http_json(poll, remaining)
    content = _content(value)
    parsed = _parse_analysis(content)
    result: dict[str, Any] = {
        "provider": "nvidia",
        "model": MODEL,
        "analysis": parsed if parsed is not None else content,
        "structured": parsed is not None,
    }
    if parsed is not None:
        result["validation"] = _validate_analysis(parsed, width, height)
    else:
        result["validation"] = {
            "structurally_valid": False,
            "actionable": False,
            "requires_cursor_calibration": False,
            "warnings": ["response was not structured JSON"],
        }
    usage = value.get("usage")
    if isinstance(usage, dict):
        result["usage"] = usage
    return result
