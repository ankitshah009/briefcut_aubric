"""What a campaign request or story plan may say: editorial values only, bounded, never executable.

Every value a client or a model sends passes through here before the manager
keeps it. Nothing in this module touches a process, a file or a model, so the
whole layer is pure and unit-testable on its own.
"""

from __future__ import annotations

import math
import re
from typing import Any

from vedit.beats import snap_to_music
from vedit.brief_ops import bounded_strings, card_text
from vedit.captions import TRANSCRIBER, preview_cues, word_records
from vedit.director import MAX_REFINE_ROUNDS
from vedit.edit_spec import TEMPLATES, EditError, merge_edit, validate_edit
from vedit.lint import MAX_MOMENTS
from vedit.timeline import MAX_SPEED, MIN_SPEED, plan_timeline

_MAX_SEGMENTS = MAX_MOMENTS
_MAX_STORY_SECONDS = 180
_MAX_CUES = 6_000  # a two-hour talk's transcript, kept whole for find, captions and the lint
_MAX_TRANSCRIPT_BYTES = 480_000
_MAX_SCENES = 2_400
_MAX_SPEECH = 4_000
_MAX_NOTES = 8
_MAX_HASHTAGS = 15
_MIN_SEGMENT_SECONDS = 0.1
_LONGEST_EDIT = 86_400.0
_KIND = re.compile(r"^[a-z][a-z_-]{0,23}$")
_METHOD = re.compile(r"[a-z][a-z0-9-]{0,39}")
DIRECTORS = ("spark", "wonder")


class CampaignError(Exception):
    """An actionable message safe to return to a local client."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Single values
# ---------------------------------------------------------------------------


def _number(value: Any, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CampaignError(f"{name} must be a number.")
    try:
        number = float(value)
    except OverflowError as exc:
        raise CampaignError(f"{name} must be a finite number.") from exc
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise CampaignError(f"{name} must be between {minimum:g} and {maximum:g}.")
    return number


def _source_end(value: Any, name: str, duration: float) -> float:
    """An end time on the source clock.

    Plans keep milliseconds and clips rarely end on one, so the last moment of
    a 33.366667 s clip arrives as 33.367. That is the end of the clip, not an error.
    """
    return min(_number(value, name, 0, duration + 0.001), duration)


def _text(value: Any, name: str, limit: int, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise CampaignError(f"{name} must be text.")
    value = value.strip()
    if len(value) > limit or (required and not value):
        raise CampaignError(f"{name} must contain {'1–' if required else 'at most '}{limit} characters.")
    if any(ord(character) < 32 and character not in "\n\t" for character in value):
        raise CampaignError(f"{name} contains unsupported control characters.")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise CampaignError(f"{name} contains invalid Unicode text.")
    return value


def _boolean(payload: dict[str, Any], name: str, default: bool) -> bool:
    value = payload.get(name, default)
    if not isinstance(value, bool):
        raise CampaignError(f"{name} must be true or false.")
    return value


def _rounds(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_REFINE_ROUNDS:
        raise CampaignError(f"Auto-refine takes a whole number of rounds from 0 to {MAX_REFINE_ROUNDS}.")
    return value


def _template(value: Any) -> str:
    if not isinstance(value, str) or value not in {row["id"] for row in TEMPLATES}:
        raise CampaignError("Choose a template from the available list.")
    return value


def _director_choice(value: Any) -> str:
    """Who reviews the finished cut: ``spark`` (Muse Spark, the default) or ``wonder`` (Muse Wonder, which also listens)."""
    if not isinstance(value, str) or value not in DIRECTORS:
        raise CampaignError("Choose spark or wonder as the director.")
    return value


def _finite_pair(start: Any, end: Any) -> tuple[float, float] | None:
    """Two finite numbers as floats, or None. ``bool`` is an ``int`` subclass and never a time here."""
    pair: list[float] = []
    for value in (start, end):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            return None
        pair.append(float(value))
    return pair[0], pair[1]


def _plan_analysis(plan: dict[str, Any]) -> dict[str, Any]:
    """The plan's measurement record, or an empty one when a plan has none."""
    analysis = plan.get("analysis")
    return analysis if isinstance(analysis, dict) else {}


# ---------------------------------------------------------------------------
# The edit
# ---------------------------------------------------------------------------


def _chosen_settings(request: dict[str, Any]) -> dict[str, Any]:
    """What a template and the request add on top of the brief's own settings."""
    template = request.get("template", "none")
    row = next(item for item in TEMPLATES if item["id"] == template)
    return merge_edit(dict(row["edit"], template=template), request.get("edit") or {})


def _partial_edit(value: Any) -> dict[str, Any]:
    """Check a request's partial edit and keep only the keys it set, with cleaned values."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise CampaignError("The edit must be an object.")
    try:
        clean = validate_edit(value, duration=_LONGEST_EDIT)
    except EditError as exc:
        raise CampaignError(str(exc)) from exc

    def known(raw: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        return {
            key: known(item, checked[key])
            if isinstance(item, dict) and isinstance(checked[key], dict)
            else checked[key]
            for key, item in raw.items()
            if key in checked
        }

    return known(value, clean)


def _fit_timed(edit: dict[str, Any], story: float) -> None:
    """Keep timed items inside a story that got shorter, so none silently never plays."""
    for item in [*edit["texts"], *edit["overlays"]]:
        item["duration"] = round(min(item["duration"], max(0.1, story)), 4)
        if item["start"] >= story:
            item["start"] = round(max(0.0, story - item["duration"]), 4)


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


def _motion_point(value: Any, name: str) -> dict[str, float]:
    if not isinstance(value, dict):
        raise CampaignError(f"{name} needs a scale and a position.")
    return {
        "scale": round(_number(value.get("scale", 1), f"{name} scale", 1, 3), 4),
        "x": round(_number(value.get("x", 0.5), f"{name} x", 0, 1), 4),
        "y": round(_number(value.get("y", 0.5), f"{name} y", 0, 1), 4),
    }


def _segment_score(value: Any) -> float | None:
    """The planner's confidence, kept only when it is a number from 0 to 1."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    score = float(value)
    if not math.isfinite(score) or not 0 <= score <= 1:
        return None
    return round(score, 3)


def _clean_segment(index: int, segment: Any, duration: float) -> dict[str, Any]:
    """One story moment as the client sent it: times on the source clock, labels, and optional editorial metadata."""
    if not isinstance(segment, dict):
        raise CampaignError(f"Segment {index} must be an object.")
    start = _number(segment.get("start"), f"Segment {index} start", 0, duration)
    end = _source_end(segment.get("end"), f"Segment {index} end", duration)
    if end - start < _MIN_SEGMENT_SECONDS:
        raise CampaignError(
            f"Segment {index} must last at least {_MIN_SEGMENT_SECONDS} seconds and end after its start."
        )
    item: dict[str, Any] = {
        "start": round(start, 6),
        "end": round(end, 6),
        "label": _text(segment.get("label", f"Moment {index}"), f"Segment {index} label", 100),
        "reason": _text(segment.get("reason", "Selected by you."), f"Segment {index} reason", 600),
    }
    # Editorial metadata from the planner: optional, bounded, never executable.
    score = _segment_score(segment.get("score"))
    if score is not None:
        item["score"] = score
    kind = segment.get("kind")
    if isinstance(kind, str) and _KIND.fullmatch(kind):
        item["kind"] = kind
    if segment.get("speed") is not None:
        item["speed"] = round(_number(segment["speed"], f"Segment {index} speed", MIN_SPEED, MAX_SPEED), 4)
    if segment.get("volume") is not None:
        item["volume"] = round(_number(segment["volume"], f"Segment {index} volume", 0, 2), 4)
    if segment.get("motion") is not None:
        motion = segment["motion"]
        if not isinstance(motion, dict):
            raise CampaignError(f"Segment {index} motion needs a start and an end.")
        item["motion"] = {
            "from": _motion_point(motion.get("from"), f"Segment {index} motion start"),
            "to": _motion_point(motion.get("to"), f"Segment {index} motion end"),
        }
    return item


def _timeline_view(plan: dict[str, Any], view: dict[str, Any]) -> dict[str, Any]:
    """The JSON a client reads: spans on the output clock and the captions that will burn on them."""
    return {
        "spans": [span.as_dict() for span in view["spans"]],
        "duration": view["duration"],
        "transition_seconds": view["transition_seconds"],
        "captions": preview_cues(plan, view),
    }


def validate_plan(
    value: Any, duration: float, *, original: dict[str, Any] | None = None, assets: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Accept editorial values only, never paths, operations, or provider claims.

    ``keep`` is computed from the edit and never read from a client. ``duration``
    and ``timeline`` in the result always come from ``plan_timeline``.
    """
    if not isinstance(value, dict):
        raise CampaignError("The story plan must be an object.")
    segments = value.get("segments")
    if not isinstance(segments, list) or not 1 <= len(segments) <= _MAX_SEGMENTS:
        raise CampaignError(f"Choose between 1 and {_MAX_SEGMENTS} story segments.")
    clean = [_clean_segment(index, segment, duration) for index, segment in enumerate(segments, 1)]
    hashtags = value.get("hashtags", [])
    if not isinstance(hashtags, list) or len(hashtags) > _MAX_HASHTAGS:
        raise CampaignError(f"Use at most {_MAX_HASHTAGS} hashtags.")
    hashtags = [_text(tag, "Hashtag", 80) for tag in hashtags]
    trusted = original or {}
    analysis = _analysis(trusted.get("analysis"), duration)
    try:
        # Timed items are bounded by the finished length, which itself depends
        # on the edit, so they are fitted to the story once the clock is known.
        edit = validate_edit(
            value["edit"] if "edit" in value else trusted.get("edit"), duration=_LONGEST_EDIT, assets=assets
        )
    except EditError as exc:
        raise CampaignError(str(exc)) from exc
    title = _text(value.get("title", "Your campaign"), "Title", 120, required=True)
    earlier = card_text(trusted.get("title") or "")
    for item in edit["texts"]:
        # A title card made from the story title follows it until someone rewrites the card.
        if item["style"] == "title" and earlier and item["text"] == earlier:
            item["text"] = card_text(title)
    clean = snap_to_music(clean, edit, analysis, assets, source_duration=duration)
    view = plan_timeline({"segments": clean, "edit": edit, "analysis": analysis})
    if view["duration"] > _MAX_STORY_SECONDS + 0.05:
        raise CampaignError(
            f"The finished story may be at most {_MAX_STORY_SECONDS} seconds. Shorten or remove a segment."
        )
    _fit_timed(edit, view["duration"])
    plan = {
        "title": title,
        "summary": _text(value.get("summary", ""), "Summary", 1500),
        "segments": clean,
        "caption": _text(value.get("caption", ""), "Post copy", 2200),
        "hashtags": hashtags,
        "cover_prompt": _text(value.get("cover_prompt", ""), "Cover instructions", 1200),
        "provider": trusted.get("provider", "local"),
        "usage": trusted.get("usage", {}),
        "source_duration": duration,
        "duration": view["duration"],
        "warnings": trusted.get("warnings", []),
        "script": trusted.get("script"),
        "analysis": analysis,
        "edit": edit,
        # Display-only sentences describing which brief instructions the render
        # applies. Recomputed from the goal at render; never executable.
        "decisions": bounded_strings(value.get("decisions")),
        "ignored_edits": bounded_strings(value.get("ignored_edits")),
    }
    bookends = _bookends(value["bookends"] if "bookends" in value else trusted.get("bookends"), duration)
    if bookends:
        plan["bookends"] = bookends
    plan["timeline"] = _timeline_view(plan, view)
    return plan


def _bookends(value: Any, duration: float) -> dict[str, Any]:
    """The moments the planner placed first and last because the brief named an opening or an ending. Display and lint data only."""
    out: dict[str, Any] = {}
    for key in ("opening", "ending"):
        record = value.get(key) if isinstance(value, dict) else None
        if not isinstance(record, dict):
            continue
        pair = _finite_pair(record.get("start"), record.get("end"))
        if pair is None:
            continue
        start, end = max(0.0, pair[0]), min(float(duration), pair[1])
        if end > start:
            label = record.get("label")
            out[key] = {
                "label": " ".join(label.split())[:100] if isinstance(label, str) else "",
                "start": round(start, 3),
                "end": round(end, 3),
            }
    return out


# ---------------------------------------------------------------------------
# The measurement record a planner left on the plan
# ---------------------------------------------------------------------------


def _analysis_ranges(items: Any, duration: float, name: str) -> list[dict[str, float]]:
    """Measured ranges on the source clock, such as speech bands, each at least a moment long."""
    ranges: list[dict[str, float]] = []
    for item in (items or [])[:_MAX_SPEECH]:
        if not isinstance(item, dict):
            continue
        start = _number(item.get("start"), f"{name} start", 0, duration)
        end = _source_end(item.get("end"), f"{name} end", duration)
        if end > start:
            ranges.append({"start": round(start, 3), "end": round(end, 3)})
    return ranges


def _analysis_lines(items: Any, duration: float, name: str, limit: int) -> list[dict[str, Any]]:
    """Timed text lines (transcript cues or sentences), bounded in count and in total bytes."""
    lines: list[dict[str, Any]] = []
    size = 0
    for item in (items or [])[:_MAX_CUES]:
        if not isinstance(item, dict):
            continue
        start = _number(item.get("start"), f"{name} start", 0, duration)
        end = _source_end(item.get("end"), f"{name} end", duration)
        text = _text(item.get("text", ""), f"{name} text", limit)
        size += len(text.encode("utf-8"))
        if end > start and text and size <= _MAX_TRANSCRIPT_BYTES:
            lines.append({"start": round(start, 3), "end": round(end, 3), "text": text})
    return lines


def _analysis(value: Any, duration: float) -> dict[str, Any] | None:
    """Bounded, display-only measurement data from a trusted planner result."""
    if not isinstance(value, dict):
        return None
    method = value.get("method", "local-analysis")
    if not isinstance(method, str) or not _METHOD.fullmatch(method):
        method = "local-analysis"
    try:
        scenes = sorted(
            {round(_number(item, "Scene time", 0, duration), 3) for item in (value.get("scenes") or [])[:_MAX_SCENES]}
        )
        analysis: dict[str, Any] = {
            "method": method,
            "scenes": scenes,
            "speech": _analysis_ranges(value.get("speech"), duration, "Speech"),
            "transcript": _analysis_lines(value.get("transcript"), duration, "Cue", 300),
            "sentences": _analysis_lines(value.get("sentences"), duration, "Sentence", 600),
            "duration": round(duration, 3),
            "notes": [
                _text(note, "Analysis note", 300)
                for note in (value.get("notes") or [])[:_MAX_NOTES]
                if isinstance(note, str)
            ],
        }
    except CampaignError:
        return None
    words = word_records(value["words"], duration) if isinstance(value.get("words"), list) else []
    if words:
        analysis["words"] = words
    if value.get("transcribed") is True:
        analysis["transcribed"] = True
    if value.get("transcribed_by") == TRANSCRIBER:
        analysis["transcribed_by"] = TRANSCRIBER
    return analysis


# ---------------------------------------------------------------------------
# Reading a plan and its request
# ---------------------------------------------------------------------------


def _caption_provider(request: dict[str, Any]) -> str:
    """Who hears the speech for captions. A Meta-mode project always uses Meta Voice Transcribe; a script's captions are its own words."""
    if request.get("script"):
        return "local"
    if request.get("provider") == "meta":
        return "meta"
    return request.get("caption_provider", "local")


def _heard_by_meta(plan: dict[str, Any]) -> bool:
    """Meta Voice Transcribe heard this source at planning, so captions are cut from that transcript and nothing is heard twice."""
    analysis = _plan_analysis(plan)
    return analysis.get("transcribed_by") == TRANSCRIBER and bool(analysis.get("transcribed"))


def _listened(plan: dict[str, Any]) -> bool:
    """A transcriber has heard this source. An empty transcript then means no speech, so nothing needs to listen again."""
    analysis = _plan_analysis(plan)
    return bool(analysis.get("transcribed") or analysis.get("transcript") or analysis.get("words"))


# ---------------------------------------------------------------------------
# Geometry a model returned
# ---------------------------------------------------------------------------


def remap_proxy_points(
    points: Any, *, source_width: int, source_height: int, proxy_width: int, proxy_height: int, duration: float
) -> list[dict[str, float]]:
    """Undo a fitted proxy's letterbox; keep coordinates in the original frame."""
    if not isinstance(points, list) or not points or min(source_width, source_height, proxy_width, proxy_height) <= 0:
        raise CampaignError("The subject track has no usable geometry. Try a clearer subject description.")
    scale = min(proxy_width / source_width, proxy_height / source_height)
    fitted_width, fitted_height = round(source_width * scale), round(source_height * scale)
    left, top = (proxy_width - fitted_width) / 2, (proxy_height - fitted_height) / 2
    result: list[dict[str, float]] = []
    for point in points:
        if not isinstance(point, dict):
            raise CampaignError("The subject track contains invalid geometry.")
        time = _number(point.get("time"), "Tracking timestamp", 0, duration + 0.15)
        if time >= duration:
            continue
        x = _number(point.get("x"), "Tracked x", 0, 1) * proxy_width
        y = _number(point.get("y"), "Tracked y", 0, 1) * proxy_height
        width = _number(point.get("width"), "Tracked width", 0.000001, 1) * proxy_width
        height = _number(point.get("height"), "Tracked height", 0.000001, 1) * proxy_height
        x1, y1 = max(left, x), max(top, y)
        x2, y2 = min(left + fitted_width, x + width), min(top + fitted_height, y + height)
        area = max(0, x2 - x1) * max(0, y2 - y1)
        if area < width * height * 0.5:
            raise CampaignError(
                "The detected subject falls mostly outside the picture. Refine the subject description or turn tracking off."
            )
        result.append(
            {
                "time": time,
                "x": max(0, (x1 - left) / fitted_width),
                "y": max(0, (y1 - top) / fitted_height),
                "width": (x2 - x1) / fitted_width,
                "height": (y2 - y1) / fitted_height,
            }
        )
    if not result:
        raise CampaignError("No subject track overlaps the selected story. Adjust the selection or turn tracking off.")
    return result
