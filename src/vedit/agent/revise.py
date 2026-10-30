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


def revise_brief(brief: Brief, critique: Critique, intent: Intent) -> tuple[Brief, tuple[str, ...]]:
    data = copy.deepcopy(brief_to_dict(brief))
    fails = [item for item in critique.findings if item.severity == "fail"]
    if critique.ok or not fails:
        return brief_from_mapping(data), ()

    ops: list[dict[str, Any]] = data["ops"]
    notes: list[str] = []
    for item in fails:
        code = item.code
        if code == "captions_missing":
            _ensure_captions(ops, intent)
            notes.append("added captions")
        elif code == "duration_over":
            notes.append(_fix_duration(ops, intent))
        elif code == "loudness":
            _ensure_loudnorm(ops)
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


def _min_silence(op: dict[str, Any]) -> float:
    raw = op.get("min_silence")
    try:
        return float(raw or 0.45)
    except (TypeError, ValueError):
        return 0.45


def _ensure_captions(ops: list[dict[str, Any]], intent: Intent) -> None:
    if _first_op(ops, "captions") is not None:
        return
    style = "boxed" if intent.platform == "shorts" else "lower-third"
    _insert_before(ops, ("export",), {"op": "captions", "burn": True, "style": style})


def _ensure_loudnorm(ops: list[dict[str, Any]]) -> None:
    if _first_op(ops, "loudnorm") is not None:
        return
    _insert_before(ops, ("export",), {"op": "loudnorm"})


def _fix_duration(ops: list[dict[str, Any]], intent: Intent) -> str:
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
        loose["threshold_db"] = -30
        loose["min_silence"] = 0.25
        loose["pad"] = 0.08
        return "tightened silence cut"
    if has_silence and intent.max_duration:
        if _first_op(ops, "trim") is None:
            _insert_before(
                ops,
                ("export",),
                {"op": "trim", "start": 0, "end": intent.max_duration},
            )
        return "trimmed to max duration"
    ops.insert(
        0,
        {"op": "cut_silence", "threshold_db": -32, "min_silence": 0.3, "pad": 0.08},
    )
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
