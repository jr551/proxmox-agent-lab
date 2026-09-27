"""Per-(client, endpoint, resolution) coordinate calibration.

Provenance: ported to Python from `vnc-mcp` (https://github.com/tuaris/vnc-mcp),
`src/calibration.zig`, BSD 2-Clause, Copyright (c) 2026 The Daniel Morante
Company, Inc. The transform, the least-squares solve, the degeneracy check
and the record shape are the author's; only the language and the storage
format were adapted. See NOTICE.

Why it exists here. An agent reads a guest's screen as an image its viewer
has downscaled or cropped, but a click has to land on a real framebuffer
pixel. Estimating one from the other is where GUI automation goes wrong --
the click lands near the target and hits the wrong control. So instead of
asking the model to guess, we measure the mapping empirically once per
(client, endpoint, resolution) and apply it exactly.

The calibration is transport-independent: it is arithmetic over marker
positions, and knows nothing about RFB, QMP or Proxmox. That is why it is
worth taking even though the rest of vnc-mcp is not a fit -- ``console.py``
does the same job over ``qm monitor`` and QMP, with no daemon, no extra
port and no dependency.

Standards: :mod:`json` for the record, :func:`hashlib.sha256` for the
identity, :func:`os.replace` for the atomic rewrite. No third-party imports.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config as config_module

#: Client identities too generic to calibrate against. A calibration keyed
#: on "client" or "unknown" would be shared by every session using that
#: name, and the transform depends on how *that* viewer scales images -- so
#: a generic identity is worse than no calibration at all.
GENERIC_NAMES: frozenset[str] = frozenset(
    {"", "test", "mcp", "client", "unknown"}
)

#: Where records live, under this tool's own config dir. The Zig original
#: used ``~/.config/vnc-mcp``; this is deliberately *not* that path, so the
#: two tools never read or overwrite each other's state.
FILENAME = "calibration.json"

#: Residual RMS above this (in framebuffer pixels) means the fit is not
#: trustworthy: the client's image scaling changed mid-capture, or the
#: marker readings were guessed rather than measured.
ACCEPTABLE_RMSE = 4.0

#: Markers laid down by ``console calibrate --action start``.
MARKER_IDS: tuple[str, ...] = ("M1", "M2", "M3", "M4", "M5", "M6", "M7", "M8", "M9")


@dataclass(frozen=True)
class Marker:
    """One numbered marker: where we put it, and where the client saw it."""

    id: str
    fb_x: float
    fb_y: float
    obs_x: float
    obs_y: float


@dataclass(frozen=True)
class Fit:
    """``target = a * observed + b`` on one axis."""

    a: float
    b: float


@dataclass(frozen=True)
class Solution:
    """The solved transform, plus how much to trust it."""

    x_a: float
    x_b: float
    y_a: float
    y_b: float
    rmse: float
    residuals: tuple[float, ...]

    @property
    def trustworthy(self) -> bool:
        return self.rmse <= ACCEPTABLE_RMSE


@dataclass(frozen=True)
class Record:
    """A saved calibration for one (client, endpoint, resolution)."""

    client_id: str
    client_name: str
    endpoint_id: str
    width: int
    height: int
    x_a: float
    x_b: float
    y_a: float
    y_b: float
    rmse: float
    rounds: int
    created_at: int
    updated_at: int

    def to_framebuffer(self, ax: float, ay: float) -> tuple[int, int]:
        """Map a point from image space to framebuffer pixels, clamped."""
        fx = round(self.x_a * ax + self.x_b)
        fy = round(self.y_a * ay + self.y_b)
        return (
            max(0, min(int(fx), self.width - 1)),
            max(0, min(int(fy), self.height - 1)),
        )

    @property
    def trustworthy(self) -> bool:
        """Is this fit good enough to click on?

        A high residual means the client's image scaling changed since this
        was measured, so the stored transform describes a display that no
        longer exists. Clicking off it is the wrong-pixel failure this whole
        module exists to prevent, so a poor fit is refused.
        """
        return self.rmse <= ACCEPTABLE_RMSE


def normalize_client_id(raw: str) -> str:
    """Fold a client-supplied name into a stable id.

    Lowercased, runs of space/underscore/dot collapsed to ``-``, ends
    trimmed -- so ``"My IDE v2"`` and ``"my_ide.v2"`` agree, while a
    version bump inside the name does not silently change the id.
    """
    lowered = raw.strip().lower()
    out: list[str] = []
    pending_sep = False
    for char in lowered:
        if char in " _.":
            pending_sep = bool(out)
            continue
        if pending_sep:
            out.append("-")
            pending_sep = False
        out.append(char)
    return "".join(out)


def is_usable_client(client_id: str) -> bool:
    """False for identities too generic to calibrate against."""
    return client_id not in GENERIC_NAMES


def record_id(client_id: str, endpoint_id: str, width: int, height: int) -> str:
    """Stable 16-hex id for one (client, endpoint, resolution) triple.

    Deliberately excludes the client *version*: a client upgrade that does
    not change how it scales images should keep the existing calibration.
    """
    payload = f"{client_id}|{endpoint_id}|{width}x{height}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _fit_linear(target: list[float], observed: list[float]) -> Fit | None:
    """Least squares ``target = a*observed + b``; ``None`` if degenerate."""
    count = len(target)
    sum_o = 0.0
    sum_t = 0.0
    sum_oo = 0.0
    sum_ot = 0.0
    for t, o in zip(target, observed, strict=True):
        sum_o += o
        sum_t += t
        sum_oo += o * o
        sum_ot += o * t
    denominator = count * sum_oo - sum_o * sum_o
    if abs(denominator) < 1e-9:
        return None  # every observation identical: nothing to fit
    a = (count * sum_ot - sum_o * sum_t) / denominator
    b = (sum_t - a * sum_o) / count
    return Fit(a=a, b=b)


def solve(markers: list[Marker]) -> Solution | None:
    """Solve the per-axis transform, or ``None`` if the markers can't say.

    Two independent axis fits -- image scaling is per-axis (aspect-ratio
    letterboxing makes x and y scale differently), so one shared factor
    would be wrong for any non-square downscale.
    """
    if len(markers) < 2:
        return None
    fb_x = [m.fb_x for m in markers]
    fb_y = [m.fb_y for m in markers]
    obs_x = [m.obs_x for m in markers]
    obs_y = [m.obs_y for m in markers]

    x_fit = _fit_linear(fb_x, obs_x)
    y_fit = _fit_linear(fb_y, obs_y)
    if x_fit is None or y_fit is None:
        return None

    residuals: list[float] = []
    sum_sq = 0.0
    for i in range(len(markers)):
        ex = (x_fit.a * obs_x[i] + x_fit.b) - fb_x[i]
        ey = (y_fit.a * obs_y[i] + y_fit.b) - fb_y[i]
        residuals.append(math.sqrt(ex * ex + ey * ey))
        sum_sq += ex * ex + ey * ey
    rmse = math.sqrt(sum_sq / (len(markers) * 2))
    return Solution(
        x_a=x_fit.a, x_b=x_fit.b, y_a=y_fit.a, y_b=y_fit.b,
        rmse=rmse, residuals=tuple(residuals),
    )


def path() -> Path:
    """Where the calibration store lives."""
    return config_module.state_dir() / FILENAME


def load() -> dict[str, Any]:
    """The stored document; an absent file is an empty store, not an error.

    Never raises on a corrupt file -- calibration is a convenience, and a
    broken record must not take the console down with it. A file that does
    not parse is reported as empty, with the reason in ``"error"``.
    """
    empty: dict[str, Any] = {"version": 1, "calibrations": {}}
    try:
        raw = path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return empty
    except OSError as exc:
        return {**empty, "error": str(exc)}
    try:
        document = json.loads(raw)
    except ValueError as exc:
        return {**empty, "error": f"unparseable: {exc}"}
    if not isinstance(document, dict) or not isinstance(
        document.get("calibrations"), dict
    ):
        return {**empty, "error": "unrecognised calibration document"}
    return document


def _persist(document: dict[str, Any]) -> None:
    """Write the store atomically (temp file + rename).

    A half-written calibration file would read as corrupt on the next run
    and silently disable calibration for every endpoint, so the swap has
    to be all-or-nothing.
    """
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, target)


def upsert(record: Record) -> None:
    """Save a record, replacing any existing one with the same key.

    ``created_at`` is preserved across replacement so a re-calibration shows
    as an update, not a fresh install.
    """
    document = load()
    calibrations = dict(document.get("calibrations") or {})
    key = record_id(record.client_id, record.endpoint_id, record.width, record.height)
    previous = calibrations.get(key)
    created = record.created_at
    if isinstance(previous, dict) and previous.get("created_at"):
        created = int(previous["created_at"])
    calibrations[key] = {
        "client_id": record.client_id,
        "client_name": record.client_name,
        "endpoint_id": record.endpoint_id,
        "width": record.width,
        "height": record.height,
        "x_a": record.x_a,
        "x_b": record.x_b,
        "y_a": record.y_a,
        "y_b": record.y_b,
        "rmse": record.rmse,
        "rounds": record.rounds,
        "created_at": created,
        "updated_at": int(time.time()),
    }
    _persist({"version": 1, "calibrations": calibrations})


def find(client_id: str, endpoint_id: str, width: int, height: int) -> Record | None:
    """The exact record for this client, endpoint and resolution, if any."""
    entry = (load().get("calibrations") or {}).get(
        record_id(client_id, endpoint_id, width, height)
    )
    if not isinstance(entry, dict):
        return None
    try:
        return Record(
            client_id=str(entry["client_id"]),
            client_name=str(entry.get("client_name") or ""),
            endpoint_id=str(entry["endpoint_id"]),
            width=int(entry["width"]),
            height=int(entry["height"]),
            x_a=float(entry["x_a"]),
            x_b=float(entry["x_b"]),
            y_a=float(entry["y_a"]),
            y_b=float(entry["y_b"]),
            rmse=float(entry["rmse"]),
            rounds=int(entry.get("rounds") or 1),
            created_at=int(entry.get("created_at") or 0),
            updated_at=int(entry.get("updated_at") or 0),
        )
    except (KeyError, TypeError, ValueError):
        return None  # a malformed record is a miss, not a crash


def any_for_client(client_id: str) -> list[Record]:
    """Every record belonging to this client, newest first."""
    found: list[Record] = []
    for entry in (load().get("calibrations") or {}).values():
        if not isinstance(entry, dict) or entry.get("client_id") != client_id:
            continue
        record = find(
            str(entry.get("client_id", "")),
            str(entry.get("endpoint_id", "")),
            int(entry.get("width") or 0),
            int(entry.get("height") or 0),
        )
        if record is not None:
            found.append(record)
    found.sort(key=lambda r: r.updated_at, reverse=True)
    return found


def remove(client_id: str, endpoint_id: str, width: int, height: int) -> bool:
    """Forget one calibration. Returns whether anything was removed."""
    document = load()
    calibrations = dict(document.get("calibrations") or {})
    key = record_id(client_id, endpoint_id, width, height)
    if key not in calibrations:
        return False
    del calibrations[key]
    _persist({"version": 1, "calibrations": calibrations})
    return True


def marker_positions(width: int, height: int) -> list[tuple[str, float, float]]:
    """Where ``console calibrate --action start`` puts each marker.

    A 3x3 grid inset from the edges: inset so a marker never lands on a
    border the client may crop, and spread so the fit has real leverage at
    both ends of each axis rather than clustered in the middle.
    """
    xs = [width * fraction for fraction in (0.15, 0.5, 0.85)]
    ys = [height * fraction for fraction in (0.15, 0.5, 0.85)]
    return [
        (marker_id, x, y)
        for row, y in enumerate(ys)
        for column, x in enumerate(xs)
        for marker_id in (MARKER_IDS[row * 3 + column],)
    ]


def parse_submission(raw: Any) -> list[Marker]:
    """Parse the client's ``samples`` into markers, dropping unusable ones.

    Tolerant on purpose: a client that reports eight of nine markers, or
    rounds a coordinate to an integer, should still calibrate. A marker
    without a usable id or coordinates is dropped rather than guessed at.
    """
    if not isinstance(raw, list):
        return []
    markers: list[Marker] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        marker_id = str(item.get("id") or "")
        if not re.fullmatch(r"M[1-9]", marker_id):
            continue
        try:
            obs_x = float(item["x"])
            obs_y = float(item["y"])
        except (KeyError, TypeError, ValueError):
            continue
        markers.append(
            Marker(id=marker_id, fb_x=0.0, fb_y=0.0, obs_x=obs_x, obs_y=obs_y)
        )
    return markers


def build_markers(
    width: int, height: int, samples: list[Any]
) -> list[Marker]:
    """Pair the client's observed positions with the grid we laid down."""
    observed: dict[str, tuple[float, float]] = {}
    for marker in parse_submission(samples):
        observed[marker.id] = (marker.obs_x, marker.obs_y)
    markers: list[Marker] = []
    for marker_id, fb_x, fb_y in marker_positions(width, height):
        point = observed.get(marker_id)
        if point is None:
            continue
        markers.append(
            Marker(id=marker_id, fb_x=fb_x, fb_y=fb_y, obs_x=point[0], obs_y=point[1])
        )
    return markers


def status_line(client_name: str, endpoint_id: str, width: int, height: int) -> str:
    """One line about calibration state, appended to spatial tool output.

    Always names the endpoint and resolution, so the note cannot be
    misapplied to a different guest. The states mirror the Zig original's
    because the failure mode they warn about is the same: an agent that
    does not know a coordinate is uncalibrated will confidently click the
    wrong pixel.
    """
    client_id = normalize_client_id(client_name)
    if not client_id:
        return (
            "Calibration: UNKNOWN (client did not identify itself) — "
            "compute coordinates from the reported resolution."
        )
    if not is_usable_client(client_id):
        return (
            f"Calibration: DISABLED (client identity {client_id!r} is generic) "
            "— compute coordinates from the reported resolution."
        )
    document = load()
    if document.get("error"):
        return (
            "Calibration: UNKNOWN (calibration store unreadable) — compute "
            "coordinates from the reported resolution."
        )
    record = find(client_id, endpoint_id, width, height)
    if record is not None:
        state = "ACTIVE" if record.rmse <= ACCEPTABLE_RMSE else "ACTIVE (poor fit)"
        return (
            f"Calibration: {state} (id {record_id(client_id, endpoint_id, width, height)}, "
            f"rmse {record.rmse:.1f}px, endpoint {endpoint_id} {width}x{height}) — "
            "image-space coordinates are mapped to framebuffer pixels for you."
        )
    stale = [
        r for r in any_for_client(client_id) if r.endpoint_id == endpoint_id
    ]
    if stale:
        other = stale[0]
        return (
            f"Calibration: STALE (saved for {endpoint_id} at {other.width}x{other.height}, "
            f"current is {width}x{height}) — re-run `console calibrate`; do NOT "
            "trust image-space coordinates until then."
        )
    return (
        f"Calibration: ABSENT (endpoint {endpoint_id} at {width}x{height}) — "
        "clicks are INACCURATE until calibrated; run `console calibrate`, or "
        "compute coordinates from the reported resolution."
    )
