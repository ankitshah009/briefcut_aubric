"""Curated, local motion treatments for video footage.

These effects animate a flat video plane. They do not infer scene depth or
reconstruct a three-dimensional subject.
"""

from __future__ import annotations

import math
from typing import Any


class EffectError(ValueError):
    """An effect cannot safely be applied to the approved edit."""


REQUIRED_EFFECT_FILTERS = ("perspective", "overlay", "pad", "lut", "boxblur", "drawbox", "fps", "format", "split", "crop")


_CATALOGUE = (
    ("tilt", "Perspective sway", "A gentle perspective turn on a slate backdrop."),
    ("float", "Floating frame", "Lift the footage into a floating frame with a soft shadow."),
    ("push", "Push-in reveal", "A framed reveal that smoothly moves back into the footage."),
)


def effect_catalogue() -> list[dict[str, Any]]:
    """Return fresh, JSON-safe metadata without exposing filter expressions."""
    return [{"id": key, "label": label, "description": description,
             "default_intensity": 0.55, "default_duration": 3.0}
            for key, label, description in _CATALOGUE]


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EffectError(f"Effect {field} must be a finite number.")
    try:
        number = float(value)
    except OverflowError as exc:
        raise EffectError(f"Effect {field} must be a finite number.") from exc
    if not math.isfinite(number):
        raise EffectError(f"Effect {field} must be a finite number.")
    return number


def _subject_points(raw: Any, clip_duration: float | None) -> list[dict[str, float]]:
    """Validate a SAM-style subject track: normalized boxes at increasing times."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= 512:
        raise EffectError("Subject tracking needs 1–512 points.")
    points: list[dict[str, float]] = []
    previous = -1.0
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or set(item) - {"time", "x", "y", "width", "height"}:
            raise EffectError(f"Subject point {index} must be a time and bounding box.")
        values = {key: _number(item.get(key), f"points[{index}].{key}")
                  for key in ("time", "x", "y", "width", "height")}
        time, x, y, width, height = (values[key] for key in ("time", "x", "y", "width", "height"))
        if time < 0 or time - previous < 0.000001:
            raise EffectError("Subject point times must be non-negative and strictly increasing.")
        if clip_duration is not None and time > clip_duration + 0.001:
            raise EffectError(f"Subject point {index} falls after the approved video.")
        if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1.000001 or y + height > 1.000001:
            raise EffectError(f"Subject point {index} must be a positive box inside the frame.")
        points.append(values)
        previous = time
    return points


def validate_effect(effect: Any, *, clip_duration: float | None = None) -> dict[str, Any] | None:
    """Validate seconds against the *edited tape*, without silently clamping.

    Only an omitted duration is shortened to fit a short clip. An explicit
    interval outside the approved edit is an error requiring user correction.
    """
    if effect is None:
        return None
    if not isinstance(effect, dict):
        raise EffectError("Choose an effect from the available presets.")
    extras = set(effect) - {"op", "preset", "intensity", "start", "duration", "points"}
    if extras:
        raise EffectError("The effect contains unsupported settings.")
    preset = effect.get("preset", "none")
    if preset == "none":
        return None
    if not isinstance(preset, str) or preset not in {row[0] for row in _CATALOGUE}:
        raise EffectError("Choose Perspective sway, Floating frame, or Push-in reveal.")
    raw_points = effect.get("points")
    if raw_points is not None and preset != "float":
        raise EffectError("Only Floating frame can follow a tracked subject.")
    intensity = _number(effect.get("intensity", 0.55), "intensity")
    if not 0 <= intensity <= 1:
        raise EffectError("Effect intensity must be between 0 and 1.")
    start = _number(effect.get("start", 0.0), "start")
    if start < 0:
        raise EffectError("Effect start must be zero or later.")
    if clip_duration is not None:
        clip_duration = _number(clip_duration, "clip duration")
        if clip_duration <= 0 or start >= clip_duration:
            raise EffectError("Effect start must fall inside the approved video.")
    default_duration = min(3.0, clip_duration - start) if clip_duration is not None else 3.0
    duration = _number(effect.get("duration", default_duration), "duration")
    if duration < 0.000001:
        raise EffectError("Effect duration must be greater than zero.")
    if clip_duration is not None and start + duration > clip_duration + 0.000001:
        raise EffectError("The effect ends after the approved video. Shorten its duration or move its start.")
    result: dict[str, Any] = {"preset": preset, "intensity": intensity, "start": start, "duration": duration}
    if raw_points is not None:
        result["points"] = _subject_points(raw_points, clip_duration)
    return result
