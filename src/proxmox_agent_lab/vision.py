"""Optional NVIDIA vision analysis for graphical lab screenshots.

The runtime remains standard-library only. Images are sent only by the
explicit ``console inspect`` command; merely taking a screenshot never uploads
it. The API key is fetched from the configured secret backend and is never
included in output or errors.
"""

from __future__ import annotations

import base64
import json
import os
import queue
import re
import threading
import time
from typing import Any
from urllib import error, request

from . import secrets_store

NVIDIA_ENDPOINT = "https://integrate.api.nvidia.com/v1/chat/completions"
ENDPOINT = NVIDIA_ENDPOINT  # compatibility for callers
STATUS_ENDPOINT = "https://integrate.api.nvidia.com/v1/status/{request_id}"
NVIDIA_MODEL = "nvidia/nemotron-nano-12b-v2-vl"
NVIDIA_SECRET_ACCOUNT = "nvidia-api-key"
OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"
OPENROUTER_FREE_MODEL = "openrouter/free"
OPENROUTER_SECRET_ACCOUNT = "openrouter-api-key"
MODEL = NVIDIA_MODEL  # compatibility for callers and older audit assertions
SECRET_ACCOUNT = NVIDIA_SECRET_ACCOUNT
REQUEST_ID = re.compile(r"^[A-Za-z0-9-]{1,36}$")
API_KEY_SHAPE = re.compile(
    r"(?:nvapi-|sk-or-v1-)[A-Za-z0-9_-]+", re.IGNORECASE
)

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
    return bool(
        secrets_store.get(config, NVIDIA_SECRET_ACCOUNT, required=False)
        or os.environ.get("OPENROUTER_API_KEY")
        or secrets_store.get(config, OPENROUTER_SECRET_ACCOUNT, required=False)
    )


def _http_json(req: request.Request, timeout: float,
               provider: str) -> tuple[int, dict[str, Any]]:
    try:
        with request.urlopen(req, timeout=timeout) as response:
            status = response.status
            body = response.read()
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        detail = API_KEY_SHAPE.sub("[REDACTED]", detail)
        raise VisionError(f"{provider} vision HTTP {exc.code}: {detail}") from None
    except (error.URLError, TimeoutError, OSError) as exc:
        raise VisionError(f"{provider} vision unavailable: {exc}") from None
    try:
        value = json.loads(body)
    except (TypeError, ValueError):
        raise VisionError(f"{provider} vision returned invalid JSON") from None
    if not isinstance(value, dict):
        raise VisionError(f"{provider} vision returned a non-object response")
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
        raise VisionError("vision response has no assistant content") from None
    if not isinstance(content, str) or not content.strip():
        raise VisionError("vision response has empty assistant content")
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
    if isinstance(value, dict):
        return value
    # Some free vision models wrap the one requested checkpoint in a JSON
    # array even in json_object mode. A singleton object is unambiguous; never
    # guess which result to trust when there is zero or more than one.
    if (isinstance(value, list) and len(value) == 1
            and isinstance(value[0], dict)):
        return value[0]
    return None


def _validate_analysis(value: dict[str, Any], width: int,
                       height: int) -> dict[str, Any]:
    warnings: list[str] = []
    if not isinstance(value.get("screen"), str) or not value["screen"].strip():
        warnings.append("screen is not a non-empty string")
    positions: dict[tuple[int, int], list[str]] = {}
    boxes: list[tuple[tuple[int, int, int, int], str]] = []
    controls = value.get("controls")
    if not isinstance(controls, list):
        controls = []
        warnings.append("controls is not a list")
    for control in controls:
        if not isinstance(control, dict):
            warnings.append("control is not an object")
            continue
        label = str(control.get("label") or "unlabelled")
        bbox = control.get("bbox")
        if bbox is not None:
            if (not isinstance(bbox, list) or len(bbox) != 4
                    or not all(isinstance(v, int) for v in bbox)):
                warnings.append(f"{label}: bounding box is not [x0, y0, x1, y1]")
                continue
            x0, y0, x1, y1 = bbox
            if not (x0 < x1 and y0 < y1):
                warnings.append(f"{label}: bounding box is degenerate")
                continue
            if not (0 <= x0 and 0 <= y0 and x1 <= width and y1 <= height):
                warnings.append(f"{label}: bounding box is outside the framebuffer")
                continue
            boxes.append(((x0, y0, x1, y1), label))
            positions.setdefault(((x0 + x1) // 2, (y0 + y1) // 2), []).append(label)
            continue
        x, y = control.get("x"), control.get("y")
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
    if not isinstance(action, dict):
        warnings.append("recommended_action is not an object")
        action = {}
    kind = action.get("kind")
    if kind not in ("key", "click", "wait", "stop"):
        warnings.append("recommended action kind is invalid")
    click = kind == "click"
    if click:
        match = re.fullmatch(r"\s*(\d+)\s*,\s*(\d+)\s*", str(action.get("value", "")))
        if not match:
            warnings.append("recommended click is not an x,y coordinate")
        else:
            point = (int(match.group(1)), int(match.group(2)))
            if not (0 <= point[0] < width and 0 <= point[1] < height):
                warnings.append("recommended click is outside the framebuffer")
            else:
                containing = [
                    label for (x0, y0, x1, y1), label in boxes
                    if x0 < point[0] < x1 and y0 < point[1] < y1
                ]
                identified = positions.get(point, []) + containing
                if len(set(identified)) != 1:
                    warnings.append(
                        "recommended click does not identify exactly one control"
                    )
    return {
        "structurally_valid": not warnings,
        "actionable": not warnings and not click,
        "requires_cursor_calibration": click,
        "warnings": warnings,
    }


def _payload(image: bytes, task: str, model: str,
             max_tokens: int) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [
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


def _result(value: dict[str, Any], *, provider: str, requested_model: str,
            width: int, height: int) -> dict[str, Any]:
    content = _content(value)
    parsed = _parse_analysis(content)
    model = value.get("model")
    result: dict[str, Any] = {
        "provider": provider,
        "requested_model": requested_model,
        "model": model if isinstance(model, str) and model else requested_model,
        "analysis": parsed if parsed is not None else content,
        "structured": parsed is not None,
        "width": width,
        "height": height,
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


def _nvidia(config: Any, image: bytes, task: str, *, width: int,
            height: int, timeout: int, max_tokens: int) -> dict[str, Any]:
    api_key = secrets_store.get(config, NVIDIA_SECRET_ACCOUNT)
    payload = _payload(image, task, NVIDIA_MODEL, max_tokens)
    payload["messages"].insert(0, {"role": "system", "content": "/no_think"})
    req = request.Request(
        NVIDIA_ENDPOINT,
        data=json.dumps(payload, separators=(",", ":")).encode(),
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    deadline = time.monotonic() + timeout
    status, value = _http_json(req, timeout, "NVIDIA")
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
        status, value = _http_json(poll, remaining, "NVIDIA")
    return _result(
        value, provider="nvidia", requested_model=NVIDIA_MODEL,
        width=width, height=height,
    )


def _openrouter_key(config: Any) -> str:
    # A project-scoped key deliberately stored through `proxmox-lab secrets`
    # wins over an inherited shell value, which may be stale or belong to a
    # different tool. The conventional variable remains a compatibility
    # fallback for installs whose selected secret backend has no stored key.
    stored = secrets_store.get(
        config, OPENROUTER_SECRET_ACCOUNT, required=False
    )
    fallback = os.environ.get("OPENROUTER_API_KEY")
    key = stored or fallback
    if not key:
        raise secrets_store.SecretError(
            "secret 'openrouter-api-key' is not stored; run "
            "'proxmox-lab secrets set openrouter-api-key' or export "
            "OPENROUTER_API_KEY"
        )
    return key


def _openrouter(config: Any, image: bytes, task: str, *, model: str,
                width: int, height: int, timeout: int,
                max_tokens: int) -> dict[str, Any]:
    payload = _payload(image, task, model, max_tokens)
    # The free router may have no vision endpoint that supports strict JSON
    # Schema. JSON-object mode plus response healing is broadly compatible;
    # `_validate_analysis` remains the authoritative fail-closed schema and
    # coordinate check after healing.
    payload["response_format"] = {"type": "json_object"}
    payload["plugins"] = [{"id": "response-healing"}]
    if model == OPENROUTER_MODEL:
        payload["reasoning"] = {"enabled": True}
    req = request.Request(
        OPENROUTER_ENDPOINT,
        data=json.dumps(payload, separators=(",", ":")).encode(),
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {_openrouter_key(config)}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    status, value = _http_json(req, timeout, "OpenRouter")
    if status != 200:
        raise VisionError(f"OpenRouter vision returned HTTP {status}")
    return _result(
        value, provider="openrouter", requested_model=model,
        width=width, height=height,
    )


def _accepted(result: dict[str, Any]) -> bool:
    validation = result.get("validation")
    return bool(
        result.get("structured")
        and isinstance(validation, dict)
        and validation.get("structurally_valid") is True
    )


def _matching_controls(analysis: Any, target: str) -> list[dict[str, Any]]:
    """Every control whose label matches ``target``, in response order."""
    if not isinstance(analysis, dict):
        return []
    wanted = " ".join(target.casefold().split())
    matches = []
    for control in analysis.get("controls", []):
        if not isinstance(control, dict):
            continue
        label = " ".join(str(control.get("label", "")).casefold().split())
        if wanted and (wanted in label or label in wanted):
            matches.append(control)
    return matches


def matched_control(result: dict[str, Any], target: str) -> dict[str, Any] | None:
    """The single control labelled ``target``, or None when ambiguous/absent."""
    matches = _matching_controls(result.get("analysis"), target)
    return matches[0] if len(matches) == 1 else None


def verifies_target(result: dict[str, Any], target: str, x: int, y: int,
                    tolerance: int = 48) -> tuple[bool, str]:
    """Accept a click only when vision independently names the same target.

    Structural validity alone is deliberately insufficient: the provider must
    identify exactly one labelled control matching ``target``, bound it with a
    sane bounding box, recommend a click close to the proposed coordinate, and
    have the actual click point land strictly inside that bounding box.  This
    turns calibration into a machine-enforced checkpoint instead of a model
    self-attestation flag.
    """
    validation = result.get("validation")
    analysis = result.get("analysis")
    if not isinstance(validation, dict) or not validation.get("structurally_valid"):
        return False, "vision response was not structurally valid"
    if not isinstance(analysis, dict):
        return False, "vision response contained no structured analysis"
    matches = _matching_controls(analysis, target)
    if len(matches) != 1:
        return False, f"vision identified {len(matches)} controls matching {target!r}"
    action = analysis.get("recommended_action")
    if not isinstance(action, dict) or action.get("kind") != "click":
        return False, "vision did not recommend a click"
    match = re.fullmatch(r"\s*(\d+)\s*,\s*(\d+)\s*", str(action.get("value", "")))
    if not match:
        return False, "vision did not return a click coordinate"
    proposed = (int(match.group(1)), int(match.group(2)))
    bbox = matches[0].get("bbox")
    if (not isinstance(bbox, list) or len(bbox) != 4
            or not all(isinstance(v, int) for v in bbox)):
        return False, (
            f"matched control has no valid bounding box "
            f"(expected [x0, y0, x1, y1])"
        )
    x0, y0, x1, y1 = bbox
    if not (x0 < x1 and y0 < y1):
        return False, f"matched control bounding box is degenerate: {bbox}"
    if x1 - x0 < 8 or y1 - y0 < 8:
        return False, (
            f"matched control bounding box is too small to click reliably: "
            f"{x1 - x0}x{y1 - y0} (min 8x8)"
        )
    screen_area = (result.get("width") or 0) * (result.get("height") or 0)
    if screen_area and (x1 - x0) * (y1 - y0) > 0.6 * screen_area:
        return False, (
            f"matched control bounding box covers more than 60% of the screen"
        )
    if not (x0 < x < x1 and y0 < y < y1):
        return False, (
            f"click point ({x}, {y}) is outside the matched control bounding "
            f"box {bbox}"
        )
    if abs(proposed[0] - x) > tolerance or abs(proposed[1] - y) > tolerance:
        return False, (
            f"vision coordinate {proposed} does not agree with requested "
            f"coordinate ({x}, {y}) within {tolerance}px"
        )
    return True, "independent vision matched the named target and coordinate"


def analyze_png(config: Any, image: bytes, *, width: int, height: int,
                prompt: str | None = None, timeout: int = 120,
                max_tokens: int = 1024, provider: str = "auto") -> dict[str, Any]:
    if not image.startswith(b"\x89PNG\r\n\x1a\n"):
        raise VisionError("vision wrapper accepts PNG screenshots only")
    if not 1 <= max_tokens <= 8192:
        raise VisionError("max_tokens must be between 1 and 8192")
    task = (prompt or DEFAULT_PROMPT) + (
        f"\nThe original framebuffer is {width}x{height} pixels."
    )
    providers = {
        "nvidia": lambda: _nvidia(
            config, image, task, width=width, height=height,
            timeout=timeout, max_tokens=max_tokens,
        ),
        "openrouter-nemotron": lambda: _openrouter(
            config, image, task, model=OPENROUTER_MODEL, width=width,
            height=height, timeout=timeout, max_tokens=max_tokens,
        ),
        "openrouter-free": lambda: _openrouter(
            config, image, task, model=OPENROUTER_FREE_MODEL, width=width,
            height=height, timeout=timeout, max_tokens=max_tokens,
        ),
    }
    if provider != "auto" and provider not in providers:
        raise VisionError(f"unknown vision provider {provider!r}")
    order = list(providers) if provider == "auto" else [provider]
    if provider == "auto":
        return _race_providers(providers, timeout)
    attempts: list[dict[str, Any]] = []
    for name in order:
        started = time.monotonic()
        try:
            result = providers[name]()
        except (VisionError, secrets_store.SecretError) as exc:
            attempts.append({
                "provider": name, "status": "failed", "error": str(exc),
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            })
            continue
        if not _accepted(result):
            attempts.append({
                "provider": name,
                "status": "rejected",
                "validation": result.get("validation"),
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            })
            continue
        elapsed_ms = round((time.monotonic() - started) * 1000)
        attempts.append({
            "provider": name, "status": "selected",
            "elapsed_ms": elapsed_ms,
        })
        result["strategy"] = "single-provider"
        result["elapsed_ms"] = elapsed_ms
        result["provider_chain"] = attempts
        return result
    summary = "; ".join(
        f"{item['provider']}: {item['status']}"
        + (f" ({item['error']})" if item.get("error") else "")
        for item in attempts
    )
    raise VisionError(f"no vision provider returned a valid analysis: {summary}")


def _race_providers(providers: dict[str, Any], timeout: int) -> dict[str, Any]:
    """Return the first valid provider result from one bounded parallel race."""
    finished: queue.Queue[tuple[str, dict[str, Any] | None, Exception | None,
                                float]] = queue.Queue()
    started = time.monotonic()

    def run(name: str, call: Any) -> None:
        try:
            finished.put((name, call(), None, time.monotonic() - started))
        except Exception as exc:  # provider workers must always report completion
            finished.put((name, None, exc, time.monotonic() - started))

    for name, call in providers.items():
        threading.Thread(target=run, args=(name, call), daemon=True).start()

    attempts: list[dict[str, Any]] = []
    deadline = started + timeout
    remaining = len(providers)
    while remaining:
        wait = deadline - time.monotonic()
        if wait <= 0:
            break
        try:
            name, result, exc, elapsed = finished.get(timeout=wait)
        except queue.Empty:
            break
        remaining -= 1
        elapsed_ms = round(elapsed * 1000)
        if exc is not None:
            attempts.append({
                "provider": name, "status": "failed", "error": str(exc),
                "elapsed_ms": elapsed_ms,
            })
            continue
        assert result is not None
        if not _accepted(result):
            attempts.append({
                "provider": name, "status": "rejected",
                "validation": result.get("validation"),
                "elapsed_ms": elapsed_ms,
            })
            continue
        attempts.append({
            "provider": name, "status": "selected", "elapsed_ms": elapsed_ms,
        })
        result["provider_chain"] = attempts
        result["strategy"] = "parallel-first-valid"
        result["elapsed_ms"] = elapsed_ms
        return result
    for name in providers:
        if not any(item["provider"] == name for item in attempts):
            attempts.append({
                "provider": name, "status": "timed_out",
                "elapsed_ms": round(timeout * 1000),
            })
    summary = "; ".join(
        f"{item['provider']}: {item['status']}"
        + (f" ({item['error']})" if item.get("error") else "")
        for item in attempts
    )
    raise VisionError(f"no vision provider returned a valid analysis: {summary}")
