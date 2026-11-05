"""Apply critique failures to a copy of a brief."""

from __future__ import annotations

import copy
from typing import Any

from vedit.agent.model import Critique, Intent
from vedit.ir import Brief, brief_from_mapping, brief_to_dict

_ASPECT_SIZE = {
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "16:9": (1920, 1080),
    "4:5": (1080, 1350),
}

_PLATFORM_PRESETS = {
    "shorts": ["shorts_9x16"],
    "square": ["square_1x1"],
    "preview": ["web_preview"],
    "podcast": ["podcast_video", "podcast_audio"],
}

# Ops that create the tape. A silence cut belongs after them, never before.
_TAPE_OPS = ("concat", "slideshow")
# A tighter silence line never rises more than this, and never above the cap, so quiet speech keeps its words.
_THRESHOLD_NUDGE_DB = 3.0
_THRESHOLD_CAP_DB = -26.0


def revise_brief(
    brief: Brief,
    critique: Critique,
    intent: Intent,
    *,
    has_audio: bool | None = None,
) -> tuple[Brief, tuple[str, ...]]:
    """A revised copy of ``brief`` and one note per change. An unchanged brief comes back with no notes.

    ``has_audio`` says whether the primary source has a sound track. ``None`` means unknown, which
    keeps the older behaviour of adding a silence cut when the critique asks for a shorter program.
    """
    data = copy.deepcopy(brief_to_dict(brief))
    fails = [item for item in critique.findings if item.severity == "fail"]
    if critique.ok or not fails:
        return brief_from_mapping(data), ()

    ops: list[dict[str, Any]] = data["ops"]
    notes: list[str] = []
    for item in fails:
        code = item.code
        if code == "captions_missing":
            if _ensure_captions(ops, intent):
                notes.append("added captions")
        elif code == "duration_over":
            note = _fix_duration(ops, intent, has_audio=has_audio)
            if note:
                notes.append(note)
        elif code == "loudness":
            if _ensure_loudnorm(ops):
                notes.append("added loudnorm")
        elif code == "frame_size":
            notes.append(_fix_frame(data, ops, intent))
        else:
            notes.append(f"no automatic fix for {code}")
    return brief_from_mapping(data), tuple(notes)


def _first_op(ops: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for op in ops:
        if op.get("op") == name:
            return op
    return None


def _insert_before(ops: list[dict[str, Any]], names: tuple[str, ...], new_op: dict[str, Any]) -> None:
    for index, op in enumerate(ops):
        if op.get("op") in names:
            ops.insert(index, new_op)
            return
    ops.append(new_op)


def _insert_after_tape(ops: list[dict[str, Any]], new_op: dict[str, Any]) -> None:
    """Place an op right after the op that creates the tape, or first when the source is the tape."""
    for index, op in enumerate(ops):
        if op.get("op") in _TAPE_OPS:
            ops.insert(index + 1, new_op)
            return
    ops.insert(0, new_op)


def _min_silence(op: dict[str, Any]) -> float:
    raw = op.get("min_silence")
    try:
        return float(raw or 0.45)
    except (TypeError, ValueError):
        return 0.45


def _ensure_captions(ops: list[dict[str, Any]], intent: Intent) -> bool:
    if _first_op(ops, "captions") is not None:
        return False
    style = "boxed" if intent.platform == "shorts" else "lower-third"
    _insert_before(ops, ("export",), {"op": "captions", "burn": True, "style": style})
    return True


def _ensure_loudnorm(ops: list[dict[str, Any]]) -> bool:
    if _first_op(ops, "loudnorm") is not None:
        return False
    _insert_before(ops, ("export",), {"op": "loudnorm"})
    return True


def _ensure_trim(ops: list[dict[str, Any]], intent: Intent) -> str | None:
    if not intent.max_duration:
        return None
    if _first_op(ops, "trim") is None:
        _insert_before(ops, ("export",), {"op": "trim", "start": 0, "end": intent.max_duration})
    return "trimmed to max duration"


def _fix_duration(ops: list[dict[str, Any]], intent: Intent, *, has_audio: bool | None = None) -> str | None:
    loose: dict[str, Any] | None = None
    has_silence = False
    for op in ops:
        if op.get("op") != "cut_silence":
            continue
        has_silence = True
        if _min_silence(op) > 0.25:
            loose = op
            break
    if loose is not None:
        # Only the timing tightens. The threshold was set relative to how loud this recording is,
        # so it moves by a few dB at most and never above the cap.
        loose["min_silence"] = 0.25
        loose["pad"] = 0.08
        changed = "min_silence 0.25 s, pad 0.08 s"
        threshold = loose.get("threshold_db")
        if isinstance(threshold, (int, float)) and not isinstance(threshold, bool):
            nudged = min(_THRESHOLD_CAP_DB, float(threshold) + _THRESHOLD_NUDGE_DB)
            if nudged > float(threshold):
                loose["threshold_db"] = nudged
                changed += f", threshold {nudged:g} dB"
        return f"tightened silence cut ({changed})"
    if has_silence or has_audio is False:
        # The silence cut is already tight, or the source has nothing to cut: only a trim can shorten it.
        return _ensure_trim(ops, intent)
    _insert_after_tape(ops, {"op": "cut_silence", "threshold_db": -32, "min_silence": 0.3, "pad": 0.08})
    return "added silence cut"


def _default_aspect(platform: str) -> str:
    if platform == "shorts":
        return "9:16"
    if platform == "square":
        return "1:1"
    return "16:9"


def _fix_frame(data: dict[str, Any], ops: list[dict[str, Any]], intent: Intent) -> str:
    aspect = intent.reframe or _default_aspect(intent.platform)
    if _first_op(ops, "reframe") is None:
        _insert_before(
            ops,
            ("captions", "export"),
            {"op": "reframe", "aspect": aspect, "mode": "fill"},
        )
    size = _ASPECT_SIZE.get(str(aspect))
    if size is not None:
        canvas = data.setdefault("canvas", {})
        canvas["width"], canvas["height"] = size
    presets = list(_PLATFORM_PRESETS.get(intent.platform, ["youtube_1080p"]))
    data["presets"] = list(presets)
    for op in ops:
        if op.get("op") == "export":
            op["presets"] = list(presets)
    return f"changed aspect to {aspect}"
