"""Durable local campaign jobs, with approval between planning and rendering.

The model supplies editorial data; only the compiler supplies executable steps.
SQLite is the source of truth, so closing the browser never owns a render's life.
"""

from __future__ import annotations

import hashlib
import fcntl
import json
import math
import os
import queue
import re
import shutil
import sqlite3
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from vedit import settings
from vedit.brief_ops import bounded_strings, brief_edit, card_text, shape_mismatch
from vedit.compile import REQUIRED_CUTOUT_FILTERS, compile_brief
from vedit.edit_spec import TEMPLATES, EditError, catalogue, merge_edit, validate_edit
from vedit.effects import REQUIRED_EFFECT_FILTERS, EffectError, effect_catalogue, validate_effect
from vedit.errors import RenderCancelled, VeditError
from vedit.ir import Brief, Canvas, brief_dumps
from vedit.captions import Cue, build_caption_file, concat_cues, cue_key, preview_cues, translation_target, word_records, write_caption_files
from vedit.captions import TRANSCRIBER, transcribe as transcribe_speech
from vedit.media import check_playable, export_problem, has_filter, plain_failure, probe, resolve_ffmpeg, resolve_ffprobe, source_notes, source_problem, standard_rate
from vedit.pipeline import Pipeline, execute, write_codegen, write_report
from vedit.presets import get_preset, verification
from vedit import project_media
from vedit.beats import snap_to_music
from vedit.render_plan import assembly_op, brief_sources, build_ops, delivery_preset, has_sound
from vedit.render_plan import context as render_context
from vedit.director import (
    EDIT_FIELDS, IMAGE_PRICE_USD, IMAGE_SIZES, MAX_QUERY_CHARS, MAX_REFINE_ROUNDS, MAX_SCRIPT_WORDS, SCRIPT_EDIT, count_words, describe_patch, find_in_transcript,
    merge_moments, place_script_words, reading_seconds, refine_stop, split_script,
)
from vedit.lint import (MAX_MOMENTS, PATCH_OPS, apply_patches, brief_bookends, hero_word_check, lint_plan, snap_to_clips, subject_cutout_check,
                        subject_in_frame_check)
from vedit.timeline import MAX_SPEED, MIN_SPEED, build_timeline, output_duration, plan_timeline

_ACTIVE = {"queued", "analyzing", "rendering"}
_ASPECTS = {"9:16": "shorts_9x16", "16:9": "youtube_1080p", "1:1": "square_1x1", "4:5": "portrait_4x5"}
_REVIEW_PRESETS = {"9:16": "web_preview_9x16", "16:9": "web_preview", "1:1": "web_preview_1x1"}
_META_OPERATIONS = ("story", "tracking", "cover", "transcribe", "review", "patch", "translate")
_RENDER_OPERATIONS = ("tracking", "transcribe", "cover", "review")
_CAPTION_PROVIDERS = {"local", "meta"}
_MAX_SOURCE_BYTES = project_media.MAX_SOURCE_BYTES
_MAX_SOURCE_SECONDS = project_media.MAX_SOURCE_SECONDS
_MAX_JOBS = 12
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
_MAX_SEGMENTS = MAX_MOMENTS
_MAX_STORY_SECONDS = 180
_META_TRANSCRIBE_MAX_SECONDS = 600.0  # one Meta call hears or watches at most this much; longer sources go in windows
_MAX_CUES = 6_000  # a two-hour talk's transcript, kept whole for find, captions and the lint
_MAX_TRANSCRIPT_BYTES = 480_000
_LONG_POOL = 3  # windows read by Meta at once on a long source
# Briefcut calls only Meta models. Meta Voice Transcribe is speech-to-text only, so nothing narrates a script.
NO_NARRATION = "Script videos are caption-led. Meta offers no text-to-speech, so Briefcut adds no synthetic voice."
CAPTIONS_NEED_META = "Captions are unavailable: captions use Meta Voice Transcribe, which a local edit does not call. Choose Meta mode to caption this video."
_LONGEST_EDIT = 86_400.0
_ID = re.compile(r"^[a-f0-9]{32}$")
_LANGUAGE = re.compile(r"^(?:auto|[a-z]{2,3}(?:-[A-Za-z]{2,8})?)$")
_KIND = re.compile(r"^[a-z][a-z_-]{0,23}$")
FOUND_THUMBNAILS = "verified-find"  # inside the project's meta folder, beside the ledger SAM's check spends on
_FOUND_NAME = re.compile(r"[a-z0-9-]{1,80}\.jpg")  # verify_find's names: no dot or slash before the suffix


class CampaignError(Exception):
    """An actionable message safe to return to a local client."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class _Cancelled(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(_json(value) + "\n", encoding="utf-8")
    temporary.replace(path)


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


def _chosen_settings(request: dict[str, Any]) -> dict[str, Any]:
    """What a template and the request add on top of the brief's own settings."""
    template = request.get("template", "none")
    row = next(item for item in TEMPLATES if item["id"] == template)
    return merge_edit(dict(row["edit"], template=template), request.get("edit") or {})


def _template(value: Any) -> str:
    if not isinstance(value, str) or value not in {row["id"] for row in TEMPLATES}:
        raise CampaignError("Choose a template from the available list.")
    return value


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
        return {key: known(item, checked[key]) if isinstance(item, dict) and isinstance(checked[key], dict) else checked[key]
                for key, item in raw.items() if key in checked}

    return known(value, clean)


DIRECTORS = ("spark", "wonder")


def _director_choice(value: Any) -> str:
    """Who reviews the finished cut: ``spark`` (Muse Spark, the default) or ``wonder`` (Muse Wonder, which also listens)."""
    if not isinstance(value, str) or value not in DIRECTORS:
        raise CampaignError("Choose spark or wonder as the director.")
    return value


def _rounds(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_REFINE_ROUNDS:
        raise CampaignError(f"Auto-refine takes a whole number of rounds from 0 to {MAX_REFINE_ROUNDS}.")
    return value


def _boolean(payload: dict[str, Any], name: str, default: bool) -> bool:
    value = payload.get(name, default)
    if not isinstance(value, bool):
        raise CampaignError(f"{name} must be true or false.")
    return value


# Briefcut runs on Meta models only. Studio, the MCP server, and the CLI hand
# every new project to the manager through ``product_request``, which fills in
# Meta and refuses anything else. The manager itself still accepts "local": it
# is the model-free path the test suite drives directly, and no product entry
# point can reach it.
PRODUCT_PROVIDER = "meta"
LOCAL_MODE_REMOVED = "Briefcut runs on Meta models only, so there is no local mode. Leave {name} out, or set it to meta."


def product_request(value: Any, *, fields: tuple[str, ...] = ("provider", "caption_provider")) -> Any:
    """A new project as a product entry point may send it: Meta by default, never local."""
    if not isinstance(value, dict):
        return value
    checked = dict(value)
    for name in fields:
        chosen = checked.get(name)
        if chosen is None or chosen == "":
            checked[name] = PRODUCT_PROVIDER
        elif chosen != PRODUCT_PROVIDER:
            raise CampaignError(LOCAL_MODE_REMOVED.format(name=name))
    return checked


def capabilities() -> dict[str, Any]:
    """Inspect what this machine and Meta can do, without a network call or exposing credentials."""
    try:
        ffmpeg, ffprobe = resolve_ffmpeg(), resolve_ffprobe()
        ready = all(os.access(path, os.X_OK) for path in (ffmpeg, ffprobe))
        reason = "Rendering is ready." if ready else "Install FFmpeg and ffprobe. Setup shows the exact command."
    except VeditError:
        ready, reason = False, "Install FFmpeg and ffprobe. Setup shows the exact command."
    from vedit.onboarding import host_capabilities

    try:
        from vedit import meta

        cloud = meta.capabilities()
    except ImportError:
        cloud = {"configured": False, "enabled": False, "reason": "Meta integration is unavailable in this installation."}
    return {
        # FFmpeg renders on this machine. Every model call, captions included, is Meta.
        "render": {"ready": ready, "reason": reason},
        "meta": cloud,
        # Muse Glimmer on this computer runs the note, translation and script steps; Studio names it where it applies.
        "private_mode": _private_mode(),
        "host": host_capabilities(),
        "effects": _effect_support(),
        "editor": _editor_support(),
        "script": _script_support(cloud),
        "director": {"auto_refine_rounds": MAX_REFINE_ROUNDS, "patch_ops": list(PATCH_OPS), "edit_paths": list(EDIT_FIELDS), "choices": list(DIRECTORS)},
        "motion_cards": _motion_cards(),
        "limits": {"upload_bytes": _MAX_SOURCE_BYTES, "source_seconds": _MAX_SOURCE_SECONDS, "segments": _MAX_SEGMENTS, "target_seconds": _MAX_STORY_SECONDS,
                   # One Meta call watches at most this much; a longer source is planned in windows of this length.
                   "meta_window_seconds": _META_TRANSCRIBE_MAX_SECONDS,
                   "clips": project_media.MAX_ITEMS, "photo_seconds": project_media.PHOTO_SECONDS, "assets": project_media.MAX_ASSETS},
    }


def _private_mode() -> bool:
    """Whether private mode (Muse Glimmer on this computer) is on. A bad setting counts as off here; the model call itself refuses it."""
    from vedit import open_weights

    try:
        return open_weights.enabled()
    except open_weights.OpenWeightsError:
        return False


def _motion_cards() -> bool:
    """Whether Blender can render 3D motion cards here. Studio and agents hide the feature otherwise."""
    try:
        from vedit import blender_cards

        return blender_cards.blender_available()
    except (ImportError, OSError):
        return False


def _script_support(cloud: dict[str, Any]) -> dict[str, Any]:
    """What Studio shows beside the script box. Silent is a real mode, so it is said plainly."""
    pictures = bool(cloud.get("features", {}).get("script"))
    return {
        "available": True, "generates_images": pictures, "narration": False,
        "voices": [], "default_voice": None, "reason": NO_NARRATION,
        "image_reason": "Muse Image draws one picture per scene." if pictures else "Muse Image draws the scene pictures once Meta is connected. Add your Meta API key in Settings.",
    }


def _effect_support() -> dict[str, Any]:
    try:
        missing = [name for name in REQUIRED_EFFECT_FILTERS if not has_filter(name)]
    except VeditError:
        missing = list(REQUIRED_EFFECT_FILTERS)
    return {
        "supported": not missing,
        "presets": effect_catalogue(),
        "missing_filters": missing,
        "reason": "Local motion effects are ready." if not missing else "Motion effects need FFmpeg filters: " + ", ".join(missing) + ". Install ffmpeg-full and restart Studio.",
    }


# Each feature runs when any one of its filter sets is present.
_EDITOR_FILTERS: dict[str, tuple[tuple[str, ...], ...]] = {
    "transitions": (("xfade", "acrossfade"),),
    "text": (("ass",), ("drawtext",)),
    "captions": (("ass",), ("subtitles",)),
    "overlays": (("overlay",),),
    "cutout": (REQUIRED_CUTOUT_FILTERS,),
    "zoom": (("perspective",),),
    "stabilize": (("vidstabdetect", "vidstabtransform"), ("deshake",)),
    "look": (("lut3d",), ("eq", "colorbalance")),
    "voice": (("highpass", "equalizer"),),
    "music": (("amix", "alimiter"),),
}


def _editor_support() -> dict[str, Any]:
    """The editor's choices, with what this machine's FFmpeg can really run."""
    editor = catalogue()
    try:
        features = {name: any(all(has_filter(item) for item in option) for option in options) for name, options in _EDITOR_FILTERS.items()}
    except VeditError:
        features = {name: False for name in _EDITOR_FILTERS}
    editor["features"] = features
    for rows, feature, plain in (("transitions", "transitions", "none"), ("zoom_modes", "zoom", "none"), ("cutout_modes", "cutout", "none"), ("looks", "look", "none")):
        for row in editor[rows]:
            row["available"] = row["id"] == plain or features[feature]
    return editor


def _motion_point(value: Any, name: str) -> dict[str, float]:
    if not isinstance(value, dict):
        raise CampaignError(f"{name} needs a scale and a position.")
    return {
        "scale": round(_number(value.get("scale", 1), f"{name} scale", 1, 3), 4),
        "x": round(_number(value.get("x", 0.5), f"{name} x", 0, 1), 4),
        "y": round(_number(value.get("y", 0.5), f"{name} y", 0, 1), 4),
    }


def _fit_timed(edit: dict[str, Any], story: float) -> None:
    """Keep timed items inside a story that got shorter, so none silently never plays."""
    for item in [*edit["texts"], *edit["overlays"]]:
        item["duration"] = round(min(item["duration"], max(0.1, story)), 4)
        if item["start"] >= story:
            item["start"] = round(max(0.0, story - item["duration"]), 4)


def _timeline_view(plan: dict[str, Any], view: dict[str, Any]) -> dict[str, Any]:
    """The JSON a client reads: spans on the output clock and the captions that will burn on them."""
    return {
        "spans": [span.as_dict() for span in view["spans"]],
        "duration": view["duration"],
        "transition_seconds": view["transition_seconds"],
        "captions": preview_cues(plan, view),
    }


def validate_plan(value: Any, duration: float, *, original: dict[str, Any] | None = None, assets: dict[str, Any] | None = None) -> dict[str, Any]:
    """Accept editorial values only, never paths, operations, or provider claims.

    ``keep`` is computed from the edit and never read from a client. ``duration``
    and ``timeline`` in the result always come from ``plan_timeline``.
    """
    if not isinstance(value, dict):
        raise CampaignError("The story plan must be an object.")
    segments = value.get("segments")
    if not isinstance(segments, list) or not 1 <= len(segments) <= _MAX_SEGMENTS:
        raise CampaignError(f"Choose between 1 and {_MAX_SEGMENTS} story segments.")
    clean = []
    for index, segment in enumerate(segments, 1):
        if not isinstance(segment, dict):
            raise CampaignError(f"Segment {index} must be an object.")
        start = _number(segment.get("start"), f"Segment {index} start", 0, duration)
        end = _source_end(segment.get("end"), f"Segment {index} end", duration)
        if end - start < 0.1:
            raise CampaignError(f"Segment {index} must last at least 0.1 seconds and end after its start.")
        item = {
            "start": round(start, 6), "end": round(end, 6),
            "label": _text(segment.get("label", f"Moment {index}"), f"Segment {index} label", 100),
            "reason": _text(segment.get("reason", "Selected by you."), f"Segment {index} reason", 600),
        }
        # Editorial metadata from the planner: optional, bounded, never executable.
        score = segment.get("score")
        if isinstance(score, (int, float)) and not isinstance(score, bool) and math.isfinite(float(score)) and 0 <= float(score) <= 1:
            item["score"] = round(float(score), 3)
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
            item["motion"] = {"from": _motion_point(motion.get("from"), f"Segment {index} motion start"),
                              "to": _motion_point(motion.get("to"), f"Segment {index} motion end")}
        clean.append(item)
    hashtags = value.get("hashtags", [])
    if not isinstance(hashtags, list) or len(hashtags) > 15:
        raise CampaignError("Use at most 15 hashtags.")
    hashtags = [_text(tag, "Hashtag", 80) for tag in hashtags]
    trusted = original or {}
    analysis = _analysis(trusted.get("analysis"), duration)
    try:
        # Timed items are bounded by the finished length, which itself depends
        # on the edit, so they are fitted to the story once the clock is known.
        edit = validate_edit(value["edit"] if "edit" in value else trusted.get("edit"), duration=_LONGEST_EDIT, assets=assets)
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
        raise CampaignError(f"The finished story may be at most {_MAX_STORY_SECONDS} seconds. Shorten or remove a segment.")
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
        start, end = record.get("start"), record.get("end")
        if not all(isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(float(item)) for item in (start, end)):
            continue
        start, end = max(0.0, float(start)), min(float(duration), float(end))
        if end > start:
            label = record.get("label")
            out[key] = {"label": " ".join(label.split())[:100] if isinstance(label, str) else "", "start": round(start, 3), "end": round(end, 3)}
    return out


def _analysis(value: Any, duration: float) -> dict[str, Any] | None:
    """Bounded, display-only measurement data from a trusted planner result."""
    if not isinstance(value, dict):
        return None
    try:
        method = value.get("method", "local-analysis")
        if not isinstance(method, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", method):
            method = "local-analysis"
        scenes = sorted({round(_number(item, "Scene time", 0, duration), 3) for item in (value.get("scenes") or [])[:2400]})
        speech = []
        for item in (value.get("speech") or [])[:4000]:
            if isinstance(item, dict):
                start = _number(item.get("start"), "Speech start", 0, duration)
                end = _source_end(item.get("end"), "Speech end", duration)
                if end > start:
                    speech.append({"start": round(start, 3), "end": round(end, 3)})
        transcript, size = [], 0
        for item in (value.get("transcript") or [])[:_MAX_CUES]:
            if not isinstance(item, dict):
                continue
            start = _number(item.get("start"), "Cue start", 0, duration)
            end = _source_end(item.get("end"), "Cue end", duration)
            text = _text(item.get("text", ""), "Cue text", 300)
            size += len(text.encode("utf-8"))
            if end > start and text and size <= _MAX_TRANSCRIPT_BYTES:
                transcript.append({"start": round(start, 3), "end": round(end, 3), "text": text})
        sentences, size = [], 0
        for item in (value.get("sentences") or [])[:_MAX_CUES]:
            if not isinstance(item, dict):
                continue
            start = _number(item.get("start"), "Sentence start", 0, duration)
            end = _source_end(item.get("end"), "Sentence end", duration)
            text = _text(item.get("text", ""), "Sentence text", 600)
            size += len(text.encode("utf-8"))
            if end > start and text and size <= _MAX_TRANSCRIPT_BYTES:
                sentences.append({"start": round(start, 3), "end": round(end, 3), "text": text})
        words = word_records(value["words"], duration) if isinstance(value.get("words"), list) else []
        notes = [_text(note, "Analysis note", 300) for note in (value.get("notes") or [])[:8] if isinstance(note, str)]
        analysis = {"method": method, "scenes": scenes, "speech": speech, "transcript": transcript, "sentences": sentences, "duration": round(duration, 3), "notes": notes}
        if words:
            analysis["words"] = words
        if value.get("transcribed") is True:
            analysis["transcribed"] = True
        if value.get("transcribed_by") == TRANSCRIBER:
            analysis["transcribed_by"] = TRANSCRIBER
        return analysis
    except CampaignError:
        return None


def _caption_provider(request: dict[str, Any]) -> str:
    """Who hears the speech for captions. A Meta-mode project always uses Meta Voice Transcribe; a script's captions are its own words."""
    if request.get("script"):
        return "local"
    if request.get("provider") == "meta":
        return "meta"
    return request.get("caption_provider", "local")


def _heard_by_meta(plan: dict[str, Any]) -> bool:
    """Meta Voice Transcribe heard this source at planning, so captions are cut from that transcript and nothing is heard twice."""
    analysis = plan.get("analysis") if isinstance(plan.get("analysis"), dict) else {}
    return analysis.get("transcribed_by") == TRANSCRIBER and bool(analysis.get("transcribed"))


def _listened(plan: dict[str, Any]) -> bool:
    """A transcriber has heard this source. An empty transcript then means no speech, so nothing needs to listen again."""
    analysis = plan.get("analysis") if isinstance(plan.get("analysis"), dict) else {}
    return bool(analysis.get("transcribed") or analysis.get("transcript") or analysis.get("words"))


def _effect(value: Any, *, duration: float | None = None) -> dict[str, Any] | None:
    try:
        effect = validate_effect(value, clip_duration=duration)
        if effect is not None and effect["intensity"] > 0:
            support = _effect_support()
            if not support["supported"]:
                raise CampaignError(support["reason"])
        # Preserve an omitted duration until the approved story length is known;
        # the shared validator can then choose a fitting default for short clips.
        if effect is not None and duration is None and isinstance(value, dict) and "duration" not in value:
            effect.pop("duration", None)
        return effect
    except EffectError as exc:
        raise CampaignError(str(exc)) from exc


def _background_name(prompt: str) -> str:
    """What the media bin calls a generated background: the start of its prompt."""
    words = " ".join(prompt.split())
    return (words[:40].rstrip() + "\u2026" if len(words) > 40 else words) + ".png"


def remap_proxy_points(points: Any, *, source_width: int, source_height: int, proxy_width: int, proxy_height: int, duration: float) -> list[dict[str, float]]:
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
            raise CampaignError("The detected subject falls mostly outside the picture. Refine the subject description or turn tracking off.")
        result.append({"time": time, "x": max(0, (x1 - left) / fitted_width), "y": max(0, (y1 - top) / fitted_height), "width": (x2 - x1) / fitted_width, "height": (y2 - y1) / fitted_height})
    if not result:
        raise CampaignError("No subject track overlaps the selected story. Adjust the selection or turn tracking off.")
    return result


class CampaignManager:
    """One bounded worker for the local machine, durable state for every action."""

    def __init__(self, workspace: Path | str | None = None, *, start_worker: bool = True):
        self.workspace = Path(workspace or Path.cwd()).resolve()
        self.sessions = self.workspace / "vedit-sessions"
        self.root = self.sessions / "campaigns"
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "campaigns.sqlite3"
        self._lease = (self.root / ".worker.lock").open("a+")
        try:
            fcntl.flock(self._lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lease.close()
            raise CampaignError("Another Studio is already processing this workspace. Use that window or stop it before restarting.", 409) from exc
        self._lock = threading.RLock()
        self._plain_video_locks: dict[str, threading.Lock] = {}
        self._events: dict[str, threading.Event] = {}
        self._active_processes = 0
        self._idle = threading.Event()
        self._idle.set()
        self._queue: queue.Queue[str | None] = queue.Queue(maxsize=_MAX_JOBS + 1)
        self._closed = False
        self._worker: threading.Thread | None = None
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, data TEXT NOT NULL)")
            connection.execute("CREATE INDEX IF NOT EXISTS jobs_fingerprint ON jobs(fingerprint)")
            connection.execute("CREATE INDEX IF NOT EXISTS jobs_updated ON jobs(updated_at DESC)")
            interrupted = connection.execute("SELECT data FROM jobs WHERE status IN ('queued','analyzing','rendering')").fetchall()
            for row in interrupted:
                job = json.loads(row[0])
                if job.get("revision_source") == "auto_refine" and isinstance(job.get("_refine_base"), dict):
                    self._restore_verified(job, "Stopped: Studio closed during auto-refine.")
                else:
                    job.update(status="failed", stage="interrupted", progress=0, message="Studio stopped before this job finished. Retry to continue from the saved plan.", error="This job was interrupted. Your source and approved plan are saved.", cancel_requested=False, updated_at=_now())
                self._save(connection, job)
        if start_worker:
            self._worker = threading.Thread(target=self._work, name="vedit-campaigns", daemon=True)
            self._worker.start()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=10)
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @staticmethod
    def _save(connection: sqlite3.Connection, job: dict[str, Any]) -> None:
        connection.execute("UPDATE jobs SET status=?, updated_at=?, data=? WHERE id=?", (job["status"], job["updated_at"], _json(job), job["id"]))

    @staticmethod
    def _public(job: dict[str, Any]) -> dict[str, Any]:
        result = {key: value for key, value in job.items() if not key.startswith("_")}
        result["assets"] = {ident: {key: value for key, value in record.items() if key != "path"} for ident, record in (job.get("assets") or {}).items()}
        verified = job.get("result")
        if isinstance(verified, dict):
            # Older projects predate explicit result revisions; a completed
            # project's job revision is its last verified revision.
            revision = verified.get("revision", job.get("verified_revision") or job["revision"])
            result["result"] = dict(verified, revision=revision, stale=revision != job["revision"])
            for name in ("video", "cover", "bundle", "brief", "report"):
                if f"{name}_url" in result["result"]:
                    result["result"][f"{name}_url"] = CampaignManager._url(job["id"], name, revision=revision)
            result["verified_revision"] = revision
        else:
            result["verified_revision"] = None
        # One entry per completed render, oldest first; artifact URLs pin ?v=N
        # so an older version stays downloadable after a later revision.
        versions = []
        for entry in job.get("versions") or []:
            if not isinstance(entry, dict) or not isinstance(entry.get("revision"), int):
                continue
            item = dict(entry)
            if item.get("status") == "verified":
                for name in ("video", "cover", "bundle", "brief", "report"):
                    item[f"{name}_url"] = CampaignManager._url(job["id"], name, revision=item["revision"])
            versions.append(item)
        result["versions"] = versions
        return result

    def _load(self, connection: sqlite3.Connection, ident: str) -> dict[str, Any]:
        if not isinstance(ident, str) or not _ID.fullmatch(ident):
            raise CampaignError("Campaign not found.", 404)
        row = connection.execute("SELECT data FROM jobs WHERE id=?", (ident,)).fetchone()
        if row is None:
            raise CampaignError("Campaign not found.", 404)
        return json.loads(row[0])

    def _update(self, ident: str, **changes: Any) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            job = self._load(connection, ident)
            if changes.get("status") in {"planned", "complete"} and (job.get("cancel_requested") or self._event(ident).is_set() or self._closed):
                raise _Cancelled()
            if changes.get("status") == "complete" and isinstance(changes.get("_artifacts"), dict):
                history = dict(job.get("_verified_artifacts", {}))
                previous = self._public(job)
                if previous.get("verified_revision") is not None and job.get("_artifacts"):
                    history[str(previous["verified_revision"])] = job["_artifacts"]
                history[str(changes["verified_revision"])] = changes["_artifacts"]
                changes["_verified_artifacts"] = history
            job.update(changes, updated_at=_now())
            self._save(connection, job)
            return job

    def _source(self, raw: Any, *, what: str = "clip") -> Path:
        """An uploaded file in a session inbox, and nothing else on this machine."""
        if not isinstance(raw, str) or not raw or not Path(raw).is_absolute():
            raise CampaignError(f"Upload a {what} in Studio before creating a campaign.")
        try:
            path = Path(raw).resolve()
        except (OSError, ValueError) as exc:
            raise CampaignError(f"The source path is invalid. Upload the {what} again.") from exc
        try:
            relative = path.relative_to(self.sessions.resolve())
        except ValueError as exc:
            raise CampaignError(f"Use a {what} uploaded through Studio.") from exc
        if len(relative.parts) != 3 or relative.parts[1] != "inbox" or relative.parts[0] == "campaigns" or path.name.startswith("."):
            raise CampaignError(f"Use a {what} uploaded through Studio.")
        if not path.is_file():
            raise CampaignError(f"This {what} is no longer in the project. Upload it again.")
        size = path.stat().st_size
        if size == 0:
            raise CampaignError(f"This {what} is empty. Export or copy it again, then upload the new file.")
        if size > _MAX_SOURCE_BYTES:
            raise CampaignError(f"This {what} is {size / 1_000_000_000:.1f} GB. Studio takes files up to {_MAX_SOURCE_BYTES / 1_000_000_000:g} GB, so shorten or compress it first.")
        return path

    def _project_source(self, job: dict[str, Any]) -> Path:
        """The one file every clock reads: the original video, or the normalized project."""
        name = job.get("_project_file")
        if not name:
            return self._source(job["request"]["paths"][0])
        path = self.root / job["id"] / name
        if not path.is_file():
            raise CampaignError("The combined project file is missing. Create the project again from your clips.", 404)
        return path

    def _request(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise CampaignError("Send a JSON object with a goal and an uploaded clip.")
        paths = value.get("paths")
        if not isinstance(paths, list) or not 1 <= len(paths) <= project_media.MAX_ITEMS:
            raise CampaignError(f"Choose between 1 and {project_media.MAX_ITEMS} uploaded clips or photos for this project.")
        sources = [self._source(path) for path in paths]
        provider = value.get("provider", "local")
        if not isinstance(provider, str) or provider not in {"local", "meta"}:
            raise CampaignError("Choose local or meta as the planning provider.")
        aspect = value.get("aspect", "9:16")
        if not isinstance(aspect, str) or aspect not in _ASPECTS:
            raise CampaignError("Choose 9:16, 16:9, or 1:1 as the frame shape.")
        language = value.get("language", settings.default_language())
        if not isinstance(language, str) or not _LANGUAGE.fullmatch(language):
            raise CampaignError("Choose a caption language code, such as en, es, or auto.")
        caption_provider = value.get("caption_provider", "local")
        if not isinstance(caption_provider, str) or caption_provider not in _CAPTION_PROVIDERS:
            raise CampaignError("Choose local or meta as the caption provider.")
        if provider == "meta":
            caption_provider = "meta"  # Meta mode hears speech only with Meta Voice Transcribe
        request = {
            "paths": [str(source) for source in sources],
            "goal": _text(value.get("goal", ""), "Goal", 2000, required=True),
            "provider": provider,
            "target_duration": _number(value.get("target_duration", 30), "Target duration", 0.5, 180),
            "aspect": aspect,
            "captions": _boolean(value, "captions", False),
            "caption_provider": caption_provider,
            "language": language,
            "subject": _text(value.get("subject", ""), "Subject", 200),
            "budget_usd": _number(value.get("budget_usd", settings.default_budget()), "Campaign budget", 0.01, 5),
            "generate_cover": _boolean(value, "generate_cover", False),
            "effect": _effect(value.get("effect")),
            "review": _boolean(value, "review", False),
            "auto_refine": _rounds(value.get("auto_refine", 0)),
            "template": _template(value.get("template", "none")),
            "edit": _partial_edit(value.get("edit")),
        }
        if "photo_seconds" in value:
            request["photo_seconds"] = _number(value["photo_seconds"], "Photo length", project_media.MIN_PHOTO_SECONDS, project_media.MAX_PHOTO_SECONDS)
        if value.get("director") is not None:
            # Left out, the review follows VEDIT_META_DIRECTOR, else Muse Spark.
            request["director"] = _director_choice(value["director"])
        if request["auto_refine"] and not request["review"]:
            raise CampaignError("Auto-refine acts on the director's review. Turn the review on, or set auto_refine to 0.")
        if provider == "local" and request["generate_cover"]:
            raise CampaignError("Generated covers need Meta. A cover frame from the video is always included.")
        if provider == "meta" or (request["captions"] and caption_provider == "meta") or request["review"]:
            cloud = capabilities()["meta"]
            if not cloud.get("configured") or not cloud.get("enabled"):
                raise CampaignError("Meta is not enabled. Add your Meta API key and allow Meta calls in Settings, then try again.", 409)
        return request

    def create(self, value: Any) -> dict[str, Any]:
        request = self._request(value)
        return self._add_job(request, _text(value.get("idempotency_key", ""), "Request key", 100))

    def _add_job(self, request: dict[str, Any], key: str, *, ident: str | None = None, extras: dict[str, Any] | None = None) -> dict[str, Any]:
        """Store one validated request and queue it. ``extras`` set fields a plain campaign leaves empty."""
        fingerprint = hashlib.sha256((_json(request) + "|" + key).encode()).hexdigest()
        with self._lock, self._connect() as connection:
            if self._closed:
                raise CampaignError("Studio is shutting down. Restart it and try again.", 503)
            existing = connection.execute("SELECT data FROM jobs WHERE fingerprint=? AND status NOT IN ('failed','cancelled') ORDER BY updated_at DESC LIMIT 1", (fingerprint,)).fetchone()
            if existing:
                return self._public(json.loads(existing[0]))
            if connection.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','analyzing','rendering')").fetchone()[0] >= _MAX_JOBS or self._queue.full():
                raise CampaignError("The edit queue is full. Wait for a job to finish, then try again.", 429)
            ident, now = ident or uuid.uuid4().hex, _now()
            job = {
                "id": ident, "status": "queued", "stage": "queued", "progress": 0,
                "message": "Waiting to read your clip.", "created_at": now, "updated_at": now,
                "request": request, "plan": None, "result": None, "source": None, "error": None,
                "source_url": self._url(ident, "source"), "cancel_requested": False, "revision": 0,
                "verified_revision": None,
                "budget": {"cap_usd": request["budget_usd"], "reserved_usd": 0, "remaining_usd": request["budget_usd"]},
                "_action": "plan", "_artifacts": {}, "_fingerprint": fingerprint,
            }
            job.update(extras or {})
            connection.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?)", (ident, fingerprint, job["status"], now, now, _json(job)))
            (self.root / ident).mkdir()
            connection.commit()
            self._events[ident] = threading.Event()
            self._queue.put_nowait(ident)
            return self._public(job)

    def get(self, ident: str) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            job = self._load(connection, ident)
            result = self._public(job)
            if job.get("family"):
                result["family"] = self._family_rows(connection, job)
            return result

    def _family_rows(self, connection: sqlite3.Connection, job: dict[str, Any]) -> list[dict[str, Any]]:
        """The other projects cut from this source, with their current state."""
        rows = []
        for row in job["family"]:
            try:
                other = self._load(connection, row["id"])
            except CampaignError:
                continue
            plan = other.get("plan") if isinstance(other.get("plan"), dict) else {}
            rows.append(dict(row, status=other["status"], title=plan.get("title") or row.get("title")))
        return rows

    def list(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute("SELECT data FROM jobs ORDER BY updated_at DESC LIMIT 50").fetchall()
            return [self._public(json.loads(row[0])) for row in rows]

    def timeline(self, ident: str, value: Any) -> dict[str, Any]:
        """Resolve a draft on the one clock without saving it or touching a process."""
        if not isinstance(value, dict):
            raise CampaignError("Send a JSON object containing the draft plan.")
        with self._lock, self._connect() as connection:
            job = self._load(connection, ident)
        if not job.get("plan") or not job.get("source"):
            raise CampaignError("Create a story plan before editing its timeline.", 409)
        draft = validate_plan(value.get("plan", job["plan"]), job["source"]["duration"], original=job["plan"], assets=job.get("assets"))
        return draft["timeline"]

    def render(self, ident: str, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise CampaignError("Send a JSON object containing the approved plan.")
        with self._lock, self._connect() as connection:
            job = self._load(connection, ident)
            if job["status"] in _ACTIVE:
                if job["_action"] == "render":
                    return self._public(job)
                raise CampaignError("Wait for the story plan before rendering.", 409)
            if not job.get("plan") or not job.get("source"):
                raise CampaignError("Create a story plan before rendering.", 409)
            plan = validate_plan(value.get("plan", job["plan"]), job["source"]["duration"], original=job["plan"])
            effect = _effect(value.get("effect", job["request"].get("effect")), duration=plan["duration"])
            tracking = value.get("tracking", not job["request"].get("tracking_disabled", False))
            if not isinstance(tracking, bool):
                raise CampaignError("tracking must be true or false.")
            if "auto_refine" in value:
                self._choose_refine(job, _rounds(value["auto_refine"]))
            self._choose_output(job, value)
            return self._enqueue_render(connection, job, plan, effect, tracking, revision_source="approved", applied_patches=[], note=None)

    @staticmethod
    def _choose_output(job: dict[str, Any], value: dict[str, Any]) -> None:
        """Captions and the frame are read at render time, so the story keeps its moments when either changes."""
        request = job["request"]
        if value.get("director") is not None:
            request["director"] = _director_choice(value["director"])
        if "captions" in value:
            request["captions"] = _boolean(value, "captions", False)
        if "aspect" in value:
            aspect = value["aspect"]
            if not isinstance(aspect, str) or aspect not in _ASPECTS:
                raise CampaignError("Choose 9:16, 16:9, or 1:1 as the frame shape.")
            request["aspect"] = aspect

    @staticmethod
    def _choose_refine(job: dict[str, Any], rounds: int) -> None:
        """Set this project's rounds at render time. A Meta story may turn its own review on; a local one has no consent to upload."""
        request = job["request"]
        if rounds and not request.get("review"):
            cloud = capabilities()["meta"]
            if request.get("provider") != "meta" or not cloud.get("configured") or not cloud.get("enabled"):
                raise CampaignError("Auto-refine needs Meta's review. Create the story with Meta director to use it.", 409)
            request["review"] = True
        request["auto_refine"] = rounds

    def _enqueue_render(self, connection: sqlite3.Connection, job: dict[str, Any], plan: dict[str, Any], effect: dict[str, Any] | None, tracking: bool, *,
                        revision_source: str, applied_patches: list[dict[str, Any]], note: str | None) -> dict[str, Any]:
        """Queue a new revision of a validated plan; the caller holds the lock and connection.

        The previous verified result stays on the job (marked stale) until the
        new render passes verification, so a revision never replaces a file.
        """
        ident = job["id"]
        plan["effect"] = effect
        if self._queue.full() or self._closed:
            raise CampaignError("The render queue is unavailable. Try again shortly.", 503)
        previous = self._public(job)
        job["result"], job["verified_revision"] = previous["result"], previous["verified_revision"]
        if job["result"]:
            job["result"]["stale"] = True
        job["request"]["effect"] = effect
        # The subject stays on record; only this and later revisions skip tracking.
        job["request"]["tracking_disabled"] = not tracking
        if revision_source == "approved":
            message = "Your approved story is queued for rendering. Your previous verified version remains available." if job["result"] else "Your approved story is queued for rendering."
        else:
            message = f"Version {job['revision'] + 1} is queued with the applied fixes. Version {previous['verified_revision']} remains available."
        if revision_source == "approved":
            # A person approved this render, so any earlier loop is over and a new one may start.
            rounds = int(job["request"].get("auto_refine") or 0)
            job["auto_refine"] = {"limit": rounds, "rounds": 0, "active": True, "stopped": None} if rounds else None
            job.pop("_refine_base", None)
        job.update(plan=plan, status="queued", stage="queued", progress=0, message=message, error=None, cancel_requested=False,
                   revision=job["revision"] + 1, updated_at=_now(), _action="render", recovery=None,
                   revision_source=revision_source, applied_patches=applied_patches, note=note)
        self._save(connection, job)
        connection.commit()
        self._events[ident] = threading.Event()
        self._queue.put_nowait(ident)
        return self._public(job)

    def _revisable(self, connection: sqlite3.Connection, ident: str) -> dict[str, Any]:
        job = self._load(connection, ident)
        if job["status"] in _ACTIVE:
            raise CampaignError("Wait for the current render to finish before revising.", 409)
        if not job.get("plan") or not job.get("source"):
            raise CampaignError("Create a story plan before revising.", 409)
        if not isinstance(job.get("result"), dict):
            raise CampaignError("Render a verified version before revising it.", 409)
        return job

    @staticmethod
    def _patched(plan: dict[str, Any], patches: list[dict[str, Any]], request: dict[str, Any], duration: float,
                 *, origin: str = "fixes", strict: bool = True) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Apply patches, re-lint strictly, and validate the result. Raises when nothing could apply.

        The deterministic lint runs again on the patched story and its fixes go
        last, so a model patch that lands inside a sentence is snapped back to a
        boundary before anything is rendered.
        """
        new_plan, applied = apply_patches(plan, patches, duration=duration)
        if not any(item.get("skipped") is None for item in applied):
            why = next((item.get("skipped") for item in applied if item.get("skipped")), "nothing to change")
            raise CampaignError(f"No {origin} could be applied safely ({why}). The verified version is unchanged.", 409)
        if strict:
            analysis = new_plan.get("analysis") if isinstance(new_plan.get("analysis"), dict) else {}
            relint = lint_plan(new_plan, target_duration=request["target_duration"], duration=duration, sentences=analysis.get("sentences") or None, strict=True)
            if relint.get("patches"):
                new_plan, tidy = apply_patches(new_plan, relint["patches"], duration=duration)
                applied = applied + [item for item in tidy if item.get("skipped") is None]
        return validate_plan(new_plan, duration, original=plan), applied

    def _apply_note(self, ident: str, job: dict[str, Any], duration: float) -> dict[str, Any]:
        """Turn the stored director's note into patches with Meta, then lint and apply them."""
        from vedit import meta

        request, plan = job["request"], job["plan"]
        who = "Muse Glimmer on " + meta.this_computer() if _private_mode() else "Meta"
        self._update(ident, stage="noting", progress=0.03, message=f"Asking {who} to turn your note into changes to the story.")
        analysis = plan.get("analysis") if isinstance(plan.get("analysis"), dict) else {}
        proposal = meta.patch_plan(goal=request["goal"], note=job.get("note") or "", plan=plan, sentences=analysis.get("sentences") or None,
                                   work_dir=self.root / ident / "meta", budget_usd=request["budget_usd"], language=(request.get("language") or "auto"))
        patches = list(proposal.get("patches") or [])
        if not patches:
            raise CampaignError("Meta could not turn that note into a change to the story. Try naming a moment and what should happen to it.")
        new_plan, applied = self._patched(plan, patches, request, duration, origin="fixes from the note")
        new_plan["effect"] = plan.get("effect")
        self._update(ident, plan=new_plan, applied_patches=applied, note_summary=str(proposal.get("summary") or ""), _pending_note=False)
        job["plan"], job["applied_patches"], job["_pending_note"] = new_plan, applied, False
        return new_plan

    def revise(self, ident: str, value: Any) -> dict[str, Any]:
        """Render version N+1 from structured patches; the verified version N is kept.

        ``mode`` is ``director`` (apply Meta's review patches), ``auto`` (apply
        the deterministic lint patches), or ``note`` (turn a free-text note
        into patches with Meta first). Patches are applied by the pure patcher
        and the result is validated like any approved plan.
        """
        if not isinstance(value, dict):
            raise CampaignError("Send a JSON object with a revision mode.")
        mode = value.get("mode")
        if not isinstance(mode, str) or mode not in {"director", "auto", "note"}:
            raise CampaignError("Choose director, auto, or note as the revision mode.")
        # The next version may be reviewed by another director; the request keeps the choice.
        director = _director_choice(value["director"]) if value.get("director") is not None else None
        note: str | None = None
        patches: list[dict[str, Any]] = []
        with self._lock, self._connect() as connection:
            job = self._revisable(connection, ident)
            request = job["request"]
            if mode == "note":
                note = _text(value.get("note", ""), "Director's note", 500, required=True)
                # Private mode reads the note with Muse Glimmer on this computer: no Meta key, nothing sent.
                if not _private_mode():
                    uses_meta = request.get("provider") == "meta" or (request.get("captions") and request.get("caption_provider") == "meta") or request.get("review")
                    if not uses_meta:
                        from vedit.meta import this_computer

                        raise CampaignError(f"Director's notes need Meta mode, or private mode with Muse Glimmer on {this_computer()}.", 409)
                    cloud = capabilities()["meta"]
                    if not cloud.get("configured") or not cloud.get("enabled"):
                        raise CampaignError("Meta is not enabled. Add your API key and turn on Meta in Settings, or turn on private mode.", 409)
            else:
                patches = [patch for patch in (job["result"].get("patches") or []) if isinstance(patch, dict) and patch.get("source") == mode]
                if not patches:
                    raise CampaignError("No director's fixes to apply." if mode == "director" else "No automatic fixes to apply.", 409)
        with self._lock, self._connect() as connection:
            job = self._revisable(connection, ident)
            duration = job["source"]["duration"]
            if director is not None:
                job["request"]["director"] = director
            if mode == "note":
                # The model call happens in the worker with its own progress stage, so the
                # request returns at once and a slow answer never blocks the editor.
                plan = validate_plan(dict(job["plan"]), duration, original=job["plan"])
                effect = _effect(job["request"].get("effect"), duration=plan["duration"])
                tracking = not job["request"].get("tracking_disabled", False)
                job["_pending_note"] = True
                return self._enqueue_render(connection, job, plan, effect, tracking, revision_source="note", applied_patches=[], note=note)
            plan, applied = self._patched(job["plan"], patches, job["request"], duration, strict=mode != "auto")
            effect = _effect(job["request"].get("effect"), duration=plan["duration"])
            tracking = not job["request"].get("tracking_disabled", False)
            return self._enqueue_render(connection, job, plan, effect, tracking, revision_source=mode, applied_patches=applied, note=note)

    def find(self, ident: str, value: Any) -> dict[str, Any]:
        """Find moments by what is said, then by what is seen when this project may use Meta.

        The transcript search is local and free. Meta runs only for a project
        created in Meta mode, so nothing is uploaded without that consent, and its
        result is checked against the source length before anyone sees it.

        With ``verify`` (the default), SAM 3.1 then looks for the searched thing
        in each moment Muse Spark found (``verify_find``), on the same budget.
        Every moment carries ``verified`` (true, false or null), ``coverage``
        (0 to 1 or null), ``flagged``, ``thumbnail`` (a file name) and
        ``thumbnail_url``. ``drop_unseen`` moves moments SAM never saw to
        ``verification.dropped`` instead of flagging them.
        """
        if not isinstance(value, dict):
            raise CampaignError("Send a JSON object with a search.")
        query = _text(value.get("query", ""), "Search", MAX_QUERY_CHARS, required=True)
        verify = _boolean(value, "verify", True)
        drop_unseen = _boolean(value, "drop_unseen", False)
        with self._lock, self._connect() as connection:
            job = self._load(connection, ident)
        if not job.get("source"):
            raise CampaignError("The clip is still being read. Try again shortly.", 409)
        request, root = job["request"], self.root / ident
        analysis = job["plan"].get("analysis") if isinstance(job.get("plan"), dict) and isinstance(job["plan"].get("analysis"), dict) else {}
        sentences = analysis.get("sentences") or []
        heard = find_in_transcript(query, sentences)
        searched, warnings, seen = ["transcript"] if sentences else [], [], []
        verification: dict[str, Any] = {"status": "skipped", "phrase": None, "receipt": None, "dropped": [], "spend": [], "spent_usd": 0.0,
                                        "reason": "Only moments found in the footage are checked, and no footage search ran."}
        if not verify:
            verification["reason"] = "SAM's check was turned off for this search."
        long_source = float(job["source"].get("duration") or 0) > _META_TRANSCRIBE_MAX_SECONDS + 0.05
        if long_source and request.get("provider") == "meta":
            # One Muse Spark call watches at most 10 minutes. On a longer source the whole transcript is searched instead,
            # for free, and nothing is uploaded; SAM checks only moments found in the picture, as on a short source.
            minutes = float(job["source"]["duration"]) / 60
            warnings.append(f"This source runs {minutes:.0f} minutes, so the search read its whole transcript. "
                            f"Searching the picture covers sources up to {_META_TRANSCRIBE_MAX_SECONDS / 60:.0f} minutes.")
            verification["reason"] = "Only moments found in the picture are checked, and a long source is searched by its transcript."
        elif request.get("provider") == "meta" and capabilities()["meta"].get("available"):
            from vedit import meta

            try:
                source = self._project_source(job)
                proxy = self._analysis_proxy(root, source, probe(source))
                answer = meta.find_moments(proxy, query=query, duration=float(job["source"]["duration"]), work_dir=root / "meta",
                                           language=(request.get("language") or "auto"), budget_usd=request["budget_usd"])
                seen = answer["moments"]
                searched.append("footage")
                warnings.extend(answer.get("warnings", []))
                if verify:
                    seen, verification = self._verify_found(seen, query, answer.get("subject"), proxy, root, request["budget_usd"],
                                                            drop_unseen=drop_unseen, warnings=warnings)
            except (CampaignError, meta.MetaError) as exc:
                warnings.append(f"The footage search did not complete: {exc}")
            except VeditError as exc:
                warnings.append(f"The footage search did not complete: {plain_failure(exc)}")
        elif request.get("provider") == "meta":
            warnings.append("Searching the picture needs Meta, which is not connected right now.")
        elif not sentences:
            warnings.append("This project has no transcript to search. Searching the picture needs a story made with Meta director.")
        budget = self._usage_summary(root, request["budget_usd"])
        self._update(ident, budget=budget)
        verification["dropped"] = [self._found_view(ident, root, moment) for moment in verification.get("dropped") or []]
        moments = [self._found_view(ident, root, moment) for moment in merge_moments(heard, seen)]
        return {"moments": moments, "searched": searched, "warnings": warnings, "budget": budget, "verification": verification}

    @staticmethod
    def _verify_found(seen: list[dict[str, Any]], query: str, subject: Any, proxy: Path, root: Path, budget: float, *,
                      drop_unseen: bool, warnings: list[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """SAM 3.1 looks for the searched thing in what Muse Spark found. A failure keeps Spark's moments, unchecked, with a warning."""
        from vedit import meta, verify_find

        # Spark names what to look for. A search for something said skips SAM whatever Spark names.
        phrase = subject if isinstance(subject, str) and subject and verify_find.query_subject(query)["kind"] != "spoken" else None
        try:
            checked = verify_find.verify_moments(seen, query, proxy, root / "meta", budget, phrase=phrase, unseen="drop" if drop_unseen else "flag",
                                                 out_dir=root / "meta" / FOUND_THUMBNAILS, phrase_from="muse-spark" if phrase else "caller")
        except (meta.MetaError, VeditError, OSError, ValueError) as exc:
            message = str(getattr(exc, "plain", "") or exc) if isinstance(exc, (meta.MetaError, ValueError)) else plain_failure(exc) if isinstance(exc, VeditError) else "a file could not be read"
            warnings.append(f"SAM could not check the found moments: {message}")
            return seen, {"status": "failed", "phrase": phrase, "receipt": None, "dropped": [], "spend": [], "spent_usd": 0.0, "reason": message}
        warnings.extend(checked.get("warnings") or [])
        return checked["moments"], {"status": checked["status"], "phrase": checked["phrase"], "receipt": checked.get("receipt"),
                                    "dropped": checked.get("dropped") or [], "spend": checked.get("spend") or [], "spent_usd": checked.get("spent_usd", 0.0),
                                    "reason": checked.get("reason"), "kind": checked.get("kind"), "model": checked.get("model")}

    @staticmethod
    def _found_view(ident: str, root: Path, moment: dict[str, Any]) -> dict[str, Any]:
        """A found moment as a client sees it: SAM's fields always present, and its thumbnail by name and URL, never by path."""
        item = dict(moment)
        item.setdefault("verified", None)
        item.setdefault("coverage", None)
        item["flagged"] = bool(item.get("flagged"))
        folder = (root / "meta" / FOUND_THUMBNAILS).resolve()
        raw, name = item.get("thumbnail"), None
        if isinstance(raw, str) and raw:
            path = Path(raw).resolve()
            if path.parent == folder and _FOUND_NAME.fullmatch(path.name):
                name = path.name
        item["thumbnail"] = name
        item["thumbnail_url"] = f"/api/campaigns/{ident}/find-thumbnail?name={name}" if name else None
        return item

    def found_thumbnail(self, ident: str, name: Any) -> Path:
        """One picture SAM's check drew for a found moment. Only this project's thumbnail folder can answer."""
        if not isinstance(name, str) or not _FOUND_NAME.fullmatch(name):
            raise CampaignError("Thumbnail not found.", 404)
        with self._lock, self._connect() as connection:
            job = self._load(connection, ident)
        folder = (self.root / job["id"] / "meta" / FOUND_THUMBNAILS).resolve()
        path = (folder / name).resolve()
        if path.parent != folder or not path.is_file():
            raise CampaignError("Thumbnail not found.", 404)
        return path

    def shorts(self, ident: str, value: Any) -> list[dict[str, Any]]:
        """Start 2 to 5 sibling projects from this source, each with its own moments.

        Each short is a normal project on the queue with its own plan, render,
        versions, and budget. Asking for shorts is the approval, so each one
        renders when its plan is ready unless ``auto_render`` is false.
        """
        if not isinstance(value, dict):
            raise CampaignError("Send a JSON object with a count.")
        count = value.get("count", 3)
        if isinstance(count, bool) or not isinstance(count, int) or not 2 <= count <= 5:
            raise CampaignError("Choose between 2 and 5 shorts.")
        aspect = value.get("aspect", "9:16")
        if not isinstance(aspect, str) or aspect not in _ASPECTS:
            raise CampaignError("Choose 9:16, 16:9, or 1:1 as the frame shape.")
        with self._lock, self._connect() as connection:
            parent = self._load(connection, ident)
            if not parent.get("plan") or not parent.get("source"):
                raise CampaignError("Wait for the story plan before making shorts.", 409)
            request = parent["request"]
            target = _number(value.get("target_duration", min(request["target_duration"], 60)), "Target duration", 0.5, _MAX_STORY_SECONDS)
            budget = _number(value.get("budget_usd", request["budget_usd"]), "Campaign budget", 0.01, 5)
            each = min(target, float(parent["source"]["duration"]) / count)
            if each < 2:
                raise CampaignError(f"This clip is too short for {count} separate shorts.", 409)
            if request.get("provider") == "meta" or request.get("review"):
                cloud = capabilities()["meta"]
                if not cloud.get("configured") or not cloud.get("enabled"):
                    raise CampaignError("Meta is not enabled. Add your API key and turn on Meta in Settings.", 409)
            active = connection.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','analyzing','rendering')").fetchone()[0]
            if active + count > _MAX_JOBS or self._queue.qsize() + count > self._queue.maxsize:
                raise CampaignError("The edit queue is full. Wait for a job to finish, then try again.", 429)
            family = uuid.uuid4().hex
            members = [uuid.uuid4().hex for _ in range(count)]
            rows = [{"id": member, "role": "short", "index": index, "title": f"Short {index}"} for index, member in enumerate(members, 1)]
            parent["family"] = list(parent.get("family") or []) + rows
            parent["updated_at"] = _now()
            self._save(connection, parent)
        renders = _boolean(value, "auto_render", True)
        home = {"id": ident, "role": "source", "index": 0, "title": parent["plan"].get("title") or "Original story"}
        jobs = []
        for index, member in enumerate(members, 1):
            short = {"parent": ident, "family": family, "index": index, "count": count, "brief": request["goal"]}
            copied = dict(json.loads(_json(request)), goal=f"{request['goal']} This is short {index} of {count}: one moment with its own hook.",
                          target_duration=round(each, 3), aspect=aspect, budget_usd=budget, effect=None, generate_cover=False, auto_refine=0,
                          auto_render=renders, short=short)
            jobs.append(self._add_job(copied, f"{family}:{index}", ident=member, extras={"family": [home] + [row for row in rows if row["id"] != member]}))
        return jobs

    def _used_ranges(self, job: dict[str, Any]) -> list[list[float]]:
        """Source ranges this short must avoid: its siblings' moments, and the original's unless it fills the source."""
        short = job["request"]["short"]
        used: list[list[float]] = []
        with self._lock, self._connect() as connection:
            try:
                parent = self._load(connection, short["parent"])
            except CampaignError:
                return []
            for row in parent.get("family") or []:
                if row["id"] == job["id"]:
                    continue
                try:
                    plan = self._load(connection, row["id"]).get("plan")
                except CampaignError:
                    continue
                if isinstance(plan, dict):
                    used.extend([segment["start"], segment["end"]] for segment in plan["segments"])
        original = [[segment["start"], segment["end"]] for segment in parent["plan"]["segments"]]
        if sum(high - low for low, high in original) <= 0.5 * float(parent["source"]["duration"]):
            used.extend(original)
        return used

    def _short_story(self, job: dict[str, Any], proxy: Path, info: Any, signals: dict[str, Any], used: list[list[float]]) -> dict[str, Any]:
        """This short's story from one Spark answer the whole family shares, so the hooks stay distinct."""
        from vedit import meta

        request, short = job["request"], job["request"]["short"]
        shared = self.root / short["parent"] / f"shorts-{short['family']}.json"
        if shared.is_file():
            found = json.loads(shared.read_text(encoding="utf-8"))
        else:
            found = meta.plan_shorts(proxy, goal=short["brief"], duration=info.duration, count=short["count"], target_duration=min(info.duration, request["target_duration"]),
                                     work_dir=self.root / job["id"] / "meta", language=request["language"], budget_usd=request["budget_usd"],
                                     transcript=signals.get("transcript") or None, used=used)
            _write_json(shared, found)
        stories = found.get("shorts") or []
        if short["index"] > len(stories):
            raise CampaignError(f"Meta found {len(stories)} distinct short{'s' if len(stories) != 1 else ''} in this footage, so this one was not made.")
        return stories[short["index"] - 1]

    def _long_affordable(self, job: dict[str, Any], info: Any, details: dict[str, Any]) -> dict[str, Any]:
        """Refuse a long source this project's budget cannot cover, with the expected cost, before anything is sent to Meta."""
        from vedit import meta
        from vedit.story import long_windows

        request, short = job["request"], job["request"].get("short")
        windows = len(long_windows(info.duration, longest=meta.WINDOW_SECONDS))
        reuse = bool(short) and self._parent_candidates(job) is not None
        # A retry pays only for the windows that did not finish; what they cost is already in ``held``.
        done = min(windows - 1, len(list((self.root / job["id"] / "long-form").glob("window-*.json"))))
        estimate = meta.estimate_long_plan(info.duration * (windows - done) / windows, windows - done,
                                           transcribe=bool(details.get("has_audio")) and not short and not done,
                                           director=request.get("director"), shorts=bool(short))
        expected = estimate["editor_most_usd"] if reuse else estimate["expected_usd"]
        held = self._usage_summary(self.root / job["id"], request["budget_usd"])["reserved_usd"]
        budget = float(request["budget_usd"])
        minutes = info.duration / 60
        if held + expected > budget + 1e-9:
            floor = math.ceil((held + expected) * 100 - 1e-6) / 100
            editor = "Muse Wonder" if estimate["editor_model"] == meta._WONDER_MODEL else "Muse Spark"
            spent = f" ${held:.2f} of it is already used." if held > 0 else ""
            raise CampaignError(f"Planning this {minutes:.0f}-minute source with Meta is expected to cost about ${expected:.2f}: "
                                f"Muse Spark watches it in {windows} parts of up to {meta.WINDOW_SECONDS / 60:.0f} minutes, "
                                f"{'Meta Voice Transcribe hears it, ' if estimate['transcribe_usd'] else ''}and {editor} picks the moments. "
                                f"The project budget is ${budget:.2f}.{spent} Raise the budget to at least ${floor:.2f}, or shorten the source. "
                                "Nothing was sent to Meta.", 402)
        return estimate

    def _parent_candidates(self, job: dict[str, Any]) -> list[dict[str, Any]] | None:
        """The candidate moments the parent story already paid for, when this short cuts the same long source."""
        short = job["request"].get("short")
        if not short:
            return None
        found = sorted((self.root / short["parent"] / "long-form").glob("candidates-*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        for path in found:
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(value, dict) and value.get("brief") == short["brief"] and isinstance(value.get("candidates"), list) and value["candidates"]:
                return value["candidates"]
        return None

    def _long_candidates(self, ident: str, job: dict[str, Any], source: Path, info: Any, signals: dict[str, Any], goal: str) -> tuple[list[dict[str, Any]], list[tuple[float, float]], list[dict[str, Any]]]:
        """Candidate moments from every window of a long source, read by Muse Spark a few windows at a time.

        Each window is cut at a sentence end, a pause or a scene cut, gets its own
        small proxy, and is saved as soon as Meta answers, so a retry reads only
        the windows that did not finish. Cancel stops new windows at once. A
        window the budget could not hold while others ran is read again on its own.
        """
        from vedit import meta
        from vedit.story import long_windows

        request, root = job["request"], self.root / ident
        speech = signals.get("speech") or []
        pauses = [round((float(speech[index]["end"]) + float(speech[index + 1]["start"])) / 2, 3)
                  for index in range(len(speech) - 1) if float(speech[index + 1]["start"]) - float(speech[index]["end"]) >= 0.3]
        windows = long_windows(info.duration, sentences=signals.get("sentences"), pauses=pauses, scenes=signals.get("scenes"), longest=meta.WINDOW_SECONDS)
        transcript = signals.get("transcript") or []
        key = hashlib.sha256(_json({"goal": goal, "windows": windows, "language": request["language"], "model": meta._story_model(),
                                    "transcript": transcript}).encode()).hexdigest()[:16]
        folder = root / "long-form"
        folder.mkdir(parents=True, exist_ok=True)
        whole = folder / f"candidates-{key}.json"
        if whole.is_file():
            saved = json.loads(whole.read_text(encoding="utf-8"))
            return saved["candidates"], [tuple(item) for item in saved["windows"]], saved.get("calls") or []
        parent = self._parent_candidates(job)
        if parent is not None:
            return parent, windows, []
        event = self._event(ident)
        total = len(windows)
        answers: dict[int, dict[str, Any]] = {}
        for index in range(total):
            path = folder / f"window-{index + 1}-{key}.json"
            if path.is_file():
                answers[index] = json.loads(path.read_text(encoding="utf-8"))
        clock = meta._clock

        def read(index: int) -> dict[str, Any]:
            if event.is_set():
                raise _Cancelled()
            start, end = windows[index]
            self._update(ident, stage="planning", progress=round(0.3 + 0.5 * len(answers) / total, 3),
                         message=f"Planning {index + 1} of {total}: {clock(start)} to {clock(end)}")
            proxy = self._analysis_proxy(root, source, info, cancel_event=event, window=(start, end))
            try:
                if event.is_set():
                    raise _Cancelled()
                answer = meta.plan_candidates(proxy, goal=goal, offset=start, duration=round(end - start, 3), source_duration=info.duration,
                                              work_dir=root / "meta", longest=60.0, shortest=3.0, language=request["language"],
                                              budget_usd=request["budget_usd"], transcript=transcript, label=f"{clock(start)} to {clock(end)}")
            finally:
                shutil.rmtree(proxy.parent, ignore_errors=True)  # a window proxy is only for this call
            _write_json(folder / f"window-{index + 1}-{key}.json", answer)
            answers[index] = answer
            return answer

        def short_of_budget(exc: BaseException) -> bool:
            return isinstance(exc, meta.MetaError) and exc.billed is False and "budget" in str(exc)

        estimate = meta.estimate_long_plan(info.duration, total, transcribe=False, director=request.get("director"))
        room = float(request["budget_usd"]) - self._usage_summary(root, request["budget_usd"])["reserved_usd"] - estimate["editor_most_usd"]
        pool = max(1, min(_LONG_POOL, int(room // max(estimate["window_most_usd"], 1e-6))))
        waiting = [index for index in range(total) if index not in answers]
        again: list[int] = []
        with ThreadPoolExecutor(max_workers=pool, thread_name_prefix="briefcut-window") as workers:
            futures = {workers.submit(read, index): index for index in waiting}
            try:
                for future, index in futures.items():
                    try:
                        future.result()
                    except Exception as exc:
                        if not short_of_budget(exc):
                            raise
                        again.append(index)
            except BaseException:
                for future in futures:  # nothing new starts; a window already with Meta finishes and is saved
                    future.cancel()
                raise
        for index in again:  # the windows that ran meanwhile have settled to what Meta billed
            self._check_cancel(ident)
            read(index)
        self._check_cancel(ident)
        candidates: list[dict[str, Any]] = []
        calls: list[dict[str, Any]] = []
        for index in range(total):
            answer = answers[index]
            calls.append({"window": index + 1, "start": windows[index][0], "end": windows[index][1], "moments": len(answer.get("moments") or []),
                          "model": answer.get("model"), "usage": answer.get("usage")})
            for number, moment in enumerate(answer.get("moments") or [], 1):
                candidates.append(dict(moment, id=f"w{index + 1}-{number}", window=index + 1))
        if not candidates:
            raise CampaignError("Muse Spark found no usable moments in any part of this footage. Try a clearer brief.")
        _write_json(whole, {"brief": goal, "windows": [list(item) for item in windows], "candidates": candidates, "calls": calls})
        return candidates, windows, calls

    def _long_story(self, job: dict[str, Any], source: Path, info: Any, signals: dict[str, Any], used: list[list[float]]) -> dict[str, Any]:
        """A story, or one short, from a source longer than one Meta call can watch.

        Muse Spark proposes candidates window by window; one text-only editor
        call (Muse Spark, or Muse Wonder when it is the director) picks and orders
        them by id over the whole transcript. Times come from the candidates;
        the sentence snap, the clip snap and ``validate_plan`` follow as usual.
        """
        from vedit import meta

        ident, request, short = job["id"], job["request"], job["request"].get("short")
        goal = short["brief"] if short else request["goal"]
        target = min(info.duration, request["target_duration"])
        shared = self.root / short["parent"] / f"shorts-{short['family']}.json" if short else None
        if shared is not None and shared.is_file():
            # A sibling already asked for the whole family's shorts; this one takes its own.
            found = json.loads(shared.read_text(encoding="utf-8"))
            stories = found.get("shorts") or []
            if short["index"] > len(stories):
                raise CampaignError(f"Meta found {len(stories)} distinct short{'s' if len(stories) != 1 else ''} in this footage, so this one was not made.")
            return dict(stories[short["index"] - 1], provider="meta", usage={"cost_usd": 0, "calls": 0, "shared": True},
                        long_form={"note": str(found.get("note") or "Muse Spark logged candidate moments; the editor picked these shorts from them.")})
        candidates, windows, calls = self._long_candidates(ident, job, source, info, signals, goal)
        # A candidate longer than this cut is trimmed to it; the sentence snap then finds the nearest sentence end.
        fitted = [dict(item, end=round(min(item["end"], item["start"] + target), 3)) for item in candidates]
        transcript = signals.get("transcript") or None
        editor = meta._director(request.get("director"))
        self._update(ident, stage="planning", progress=0.85, message=f"Picking the {'shorts' if short else 'story'} from {len(fitted)} candidate moments with {meta._DIRECTOR_NAMES[editor]}.")
        self._check_cancel(ident)
        name = meta._DIRECTOR_NAMES[editor]
        if short:
            found = meta.pick_long_shorts(goal=goal, duration=info.duration, count=short["count"], target_duration=target, shortest=min(15.0, target),
                                          candidates=fitted, work_dir=self.root / ident / "meta", language=request["language"],
                                          budget_usd=request["budget_usd"], transcript=transcript, used=used, director=editor)
            found["note"] = (f"Muse Spark watched this {info.duration / 60:.0f}-minute source in {len(windows)} parts and logged {len(candidates)} "
                             f"candidate moments; {name} picked the shorts from them.")
            _write_json(shared, found)
            stories = found.get("shorts") or []
            if short["index"] > len(stories):
                raise CampaignError(f"Meta found {len(stories)} distinct short{'s' if len(stories) != 1 else ''} in this footage, so this one was not made.")
            story = dict(stories[short["index"] - 1])
            story["warnings"] = list(found.get("warnings") or []) + list(story.get("warnings") or [])
            answer = found
        else:
            story = meta.edit_long_story(goal=goal, duration=info.duration, target_duration=target, candidates=fitted, work_dir=self.root / ident / "meta",
                                         language=request["language"], budget_usd=request["budget_usd"], transcript=transcript, director=editor)
            answer = story
        picked = len(story.get("segments") or [])
        note = (f"Muse Spark watched this {info.duration / 60:.0f}-minute source in {len(windows)} parts of up to {meta.WINDOW_SECONDS / 60:.0f} minutes "
                f"and logged {len(candidates)} candidate moments; {name} picked {picked} of them from the whole transcript.")
        story["long_form"] = {"windows": [list(item) for item in windows], "candidates": len(candidates), "picked": [segment.get("candidate") for segment in story.get("segments") or []],
                              "editor": meta._DIRECTOR_LABELS[editor], "editor_model": answer.get("model"), "editor_usage": answer.get("usage"), "window_calls": calls, "note": note}
        (self.root / ident / "long-form").mkdir(parents=True, exist_ok=True)
        _write_json(self.root / ident / "long-form" / "plan-summary.json", story["long_form"])
        costs = [float((call.get("usage") or {}).get("cost_usd") or 0) for call in calls if not (call.get("usage") or {}).get("cached")]
        story["usage"] = {"cost_usd": round(sum(costs) + float((answer.get("usage") or {}).get("cost_usd") or 0), 8), "calls": len(costs) + 1}
        story["provider"] = "meta"
        return story

    def create_script(self, value: Any) -> dict[str, Any]:
        """Start a project from written text. The worker makes the pictures; the captions are the script."""
        if not isinstance(value, dict):
            raise CampaignError("Send a JSON object with a script.")
        text = _text(value.get("script", ""), "Script", 6000, required=True)
        scenes = split_script(text)
        if not scenes:
            raise CampaignError("Write at least one sentence for the script.")
        if count_words(text) > MAX_SCRIPT_WORDS:  # counts Japanese, Chinese and Thai too, which have no spaces
            raise CampaignError(f"Keep the script to {MAX_SCRIPT_WORDS} words, about three minutes of reading.")
        provider = value.get("provider", "meta")
        if not isinstance(provider, str) or provider not in {"local", "meta"}:
            raise CampaignError("Choose local or meta as the provider.")
        aspect = value.get("aspect", "9:16")
        if not isinstance(aspect, str) or aspect not in _ASPECTS or aspect not in IMAGE_SIZES:
            raise CampaignError("Choose 9:16, 16:9, or 1:1 as the frame shape.")
        language = value.get("language", settings.default_language())
        if not isinstance(language, str) or not _LANGUAGE.fullmatch(language):
            raise CampaignError("Choose a language code, such as en, es, or auto.")
        budget = _number(value.get("budget_usd", settings.default_budget()), "Campaign budget", 0.01, 5)
        if value.get("voice") is not None:
            raise CampaignError(NO_NARRATION + " Leave the voice empty.")
        images = value.get("images", [])
        if not isinstance(images, list) or len(images) > 30:
            raise CampaignError("Send up to 30 uploaded images.")
        pictures = [str(self._source(path)) for path in images]
        if any(Path(path).suffix.lower() not in _IMAGE_SUFFIXES for path in pictures):
            raise CampaignError("Script pictures must be PNG, JPEG, or WebP images.")
        cloud = capabilities()["meta"]
        ready = bool(cloud.get("configured") and cloud.get("enabled"))
        review = _boolean(value, "review", False)
        rounds = _rounds(value.get("auto_refine", 0))
        if rounds and not review:
            raise CampaignError("Auto-refine acts on the director's review. Turn the review on, or set auto_refine to 0.")
        if not pictures and (provider == "local" or not ready):
            raise CampaignError("Generating pictures for a script needs Meta. Connect Meta, or add your own images, and try again.", 409)
        if provider == "meta" and not ready:
            # A Meta project never quietly becomes a local one.
            raise CampaignError("Meta is not enabled. Add your Meta API key and allow Meta calls in Settings, then try again.", 409)
        if review and not ready:
            raise CampaignError("Meta is not enabled. Add your Meta API key and allow Meta calls in Settings, or turn the review off.", 409)
        needed = 0.0 if pictures else len(scenes) * IMAGE_PRICE_USD
        if needed > budget:
            raise CampaignError(f"{len(scenes)} scenes need ${needed:.2f} for pictures, which is more than the ${budget:.2f} budget. Raise it or shorten the script.")
        request = {
            "paths": [], "goal": "A caption-led video that tells the writer's script clearly, scene by scene.", "provider": provider,
            "target_duration": _MAX_STORY_SECONDS, "aspect": aspect, "captions": True, "caption_provider": "local", "language": language,
            "subject": "", "budget_usd": budget, "generate_cover": False, "effect": None, "review": review, "auto_refine": rounds,
            "template": _template(value.get("template", "none")), "edit": merge_edit(SCRIPT_EDIT, _partial_edit(value.get("edit"))),
            "script": {"text": text, "scenes": scenes, "voice": None, "images": pictures},
        }
        if value.get("director") is not None:
            request["director"] = _director_choice(value["director"])
        key = _text(value.get("idempotency_key", ""), "Request key", 100)
        return self._add_job(request, key, extras={"_action": "script", "message": "Waiting to start on your script."})

    def _script(self, job: dict[str, Any]) -> dict[str, Any]:
        """Make each scene's picture, then hand the clips on as a normal project. Nothing narrates: see ``NO_NARRATION``."""
        ident, request = job["id"], job["request"]
        root, script = self.root / ident, request["script"]
        scenes = script["scenes"]
        canvas = get_preset(_ASPECTS[request["aspect"]]).canvas
        self._update(ident, status="analyzing", stage="scripting", progress=0.04, message="Choosing a picture for each scene.")
        titles = [""] * len(scenes)
        prompts: list[str | None] = [None] * len(scenes)
        style = ""
        if script["images"]:
            pictures = [Path(script["images"][index % len(script["images"])]) for index in range(len(scenes))]
        else:
            from vedit import meta

            written = meta.script_prompts(scenes, aspect=request["aspect"], work_dir=root / "meta", language=request["language"], budget_usd=request["budget_usd"])
            style = written.get("style", "")
            pictures = []
            for index, scene in enumerate(written["scenes"]):
                self._check_cancel(ident)
                titles[index], prompts[index] = scene["title"], scene["prompt"]
                self._update(ident, stage="imagining", progress=0.08 + 0.35 * index / len(scenes), message=f"Generating the picture for scene {index + 1} of {len(scenes)}.")
                image = meta.generate_image(prompt=scene["prompt"], size=IMAGE_SIZES[request["aspect"]], work_dir=root / "meta", budget_usd=request["budget_usd"])
                pictures.append(Path(image["path"]))
        # Each scene holds for as long as its words take to read.
        lengths = [reading_seconds(text) for text in scenes]
        if sum(lengths) > _MAX_STORY_SECONDS:
            raise CampaignError(f"The script takes {sum(lengths):.0f} seconds to read. Keep a script under {_MAX_STORY_SECONDS} seconds.")
        inbox = self.sessions / f"script-{ident}" / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        clips = []
        for index, (picture, seconds) in enumerate(zip(pictures, lengths), 1):
            self._check_cancel(ident)
            self._update(ident, stage="assembling", progress=0.5 + 0.15 * index / len(scenes), message=f"Building scene {index} of {len(scenes)}.")
            clip = inbox / f"scene-{index:02d}.mp4"
            sources = {"still": str(picture)}
            ops: list[dict[str, Any]] = [{"op": "slideshow", "images": ["still"], "duration": seconds, "fade": 0, "mode": "fill"}]
            ops.append({"op": "export", "preset": _ASPECTS[request["aspect"]], "path": str(clip)})
            self._run_brief(ident, Brief(name=f"scene-{index}", sources=sources, ops=ops, canvas=canvas, work_dir=str(root / "scenes" / str(index))), root / "scenes" / str(index))
            clips.append(str(clip))
        script = dict(script, titles=titles, prompts=prompts, style=style, voice=None, warning=NO_NARRATION, model=None if script["images"] else "muse-image-1.0")
        request = dict(request, paths=clips, script=script, target_duration=round(min(sum(lengths), _MAX_STORY_SECONDS), 3))
        self._update(ident, request=request, _action="plan")
        return dict(job, request=request, _action="plan")

    def _run_brief(self, ident: str, brief: Brief, work: Path) -> None:
        compiler = compile_brief(brief, work_dir=work)
        (work / "brief.yaml").write_text(brief_dumps(brief), encoding="utf-8")
        write_codegen(compiler.pipeline)
        reports = execute(compiler.pipeline, progress=False, cancel_event=self._event(ident))
        write_report(compiler.pipeline, reports, finals=compiler.finals)

    def _script_story(self, job: dict[str, Any], source: Path, info: Any, timing: list[tuple[float, float]], warnings: list[str]) -> dict[str, Any]:
        """One moment per scene, with the script's words timed evenly across it, so it edits like any story."""
        from vedit.story import analyze_signals

        ident, request = job["id"], job["request"]
        script = request["script"]
        scenes = script["scenes"]
        self._update(ident, stage="measuring", progress=0.72, message="Timing the captions scene by scene.")
        signals = analyze_signals(source, work_dir=self.root / ident / "analysis" / "signals")
        signals.pop("warnings", None)  # a caption-led video's only notice would be that it is silent
        signals.pop("has_audio", None)
        # Nothing narrates, so the writer's words share each scene evenly.
        words, cues = place_script_words([], scenes, timing)
        signals.update(words=words, transcript=cues, sentences=cues)
        if not signals.get("speech"):
            signals["speech"] = [{"start": cue["start"], "end": cue["end"]} for cue in cues]
        generated = script.get("model") is not None
        picture = "a picture generated by Muse Image" if generated else "your image"
        segments = [{"start": start, "end": min(end, info.duration), "label": script["titles"][index] or f"Scene {index + 1}",
                     "reason": f"Scene {index + 1} of your script, with {picture}.", "kind": "scene", "score": 1.0}
                    for index, (start, end) in enumerate(timing) if start < info.duration]
        notes = ["The moments are the scenes of your script. The captions are its words, timed evenly across each scene."]
        if generated:
            warnings.append("Every picture in this video was generated by Muse Image from your script. None of it is real footage.")
        if script.get("warning"):
            warnings.append(script["warning"])
        record = {"style": script.get("style", ""), "voice": None, "narrated": False, "model": script.get("model"),
                  "scenes": [{"scene": index + 1, "text": text, "title": script["titles"][index], "image": "generated" if generated else "supplied", "prompt": script["prompts"][index]}
                             for index, text in enumerate(scenes)]}
        first = scenes[0].split(". ")[0][:100]
        return {"title": script["titles"][0] or first or "Your script", "summary": f"A video made from your script: {len(segments)} scene{'s' if len(segments) != 1 else ''}, "
                + ("with Muse Image pictures. " if generated else "with your images. ") + "Caption-led, with no narration.",
                "segments": segments, "caption": scenes[0][:2200], "hashtags": [], "cover_prompt": "", "provider": request["provider"], "usage": {},
                "analysis": dict(signals, method="script", notes=notes), "warnings": [], "script": record}

    def retry(self, ident: str) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            job = self._load(connection, ident)
            if job["status"] in _ACTIVE:
                return self._public(job)
            if job["status"] not in {"failed", "cancelled"}:
                raise CampaignError("This campaign does not need a retry.", 409)
            if job.get("_project_file"):
                self._project_source(job)
            else:
                for raw in job["request"]["paths"]:
                    self._source(raw)
            if self._queue.full() or self._closed:
                raise CampaignError("The edit queue is unavailable. Try again shortly.", 503)
            job.update(status="queued", stage="queued", progress=0, message="Retry queued. Your approved choices are preserved.", error=None, cancel_requested=False, updated_at=_now())
            self._save(connection, job)
            connection.commit()
            self._events[ident] = threading.Event()
            self._queue.put_nowait(ident)
            return self._public(job)

    def cancel(self, ident: str) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            job = self._load(connection, ident)
            if job["status"] in {"complete", "failed", "cancelled"}:
                return self._public(job)
            self._event(ident).set()
            if job["status"] in {"queued", "planned"}:
                job.update(status="cancelled", stage="cancelled", message="Campaign cancelled. Your clip and plan are saved.", cancel_requested=True)
            else:
                job.update(cancel_requested=True, message="Stopping the current processing step. Your verified work remains available.")
            job["updated_at"] = _now()
            self._save(connection, job)
            return self._public(job)

    def artifact(self, ident: str, name: str, *, revision: int | None = None) -> Path:
        if name not in {"source", "video", "cover", "bundle", "brief", "report"}:
            raise CampaignError("Artifact not found.", 404)
        with self._lock, self._connect() as connection:
            job = self._load(connection, ident)
        if name == "source":
            if revision is not None:
                raise CampaignError("The source does not have render revisions.", 400)
            if not job.get("source") or not job["source"].get("has_video"):
                raise CampaignError("The clip is still being checked. Try again shortly.", 409)
            return self.plain_video(job, self._project_source(job))
        if not isinstance(job.get("result"), dict):
            raise CampaignError("This download is available after the render is verified.", 409)
        artifacts = job.get("_artifacts", {})
        if revision is not None:
            if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
                raise CampaignError("Choose a positive verified revision.")
            current = self._public(job)["verified_revision"]
            artifacts = job.get("_verified_artifacts", {}).get(str(revision))
            if artifacts is None and revision == current:
                # Completed projects created before revision history retain
                # their exact current verified path as a compatibility fallback.
                artifacts = job.get("_artifacts", {})
            if artifacts is None:
                raise CampaignError("That verified revision is not available.", 404)
        raw = artifacts.get(name)
        if not raw:
            raise CampaignError("Artifact not found.", 404)
        path = (self.root / ident / raw).resolve()
        try:
            path.relative_to((self.root / ident).resolve())
        except ValueError as exc:
            raise CampaignError("Artifact not found.", 404) from exc
        if not path.is_file():
            raise CampaignError("This output was removed. Render the saved plan again.", 404)
        return path

    def plain_video(self, job: dict[str, Any], source: Path) -> Path:
        """Return a browser-playable MP4 for MPEG-TS sources; leave other media untouched."""
        source = Path(source)
        if source.suffix.lower() != ".ts":
            return source
        ident = str(job["id"])
        with self._lock:
            lock = self._plain_video_locks.setdefault(ident, threading.Lock())
        with lock:
            root = self.root / ident / "source-preview"
            output = root / "source.mp4"
            marker = root / "source.json"
            identity = {"size": source.stat().st_size, "mtime_ns": source.stat().st_mtime_ns}
            if output.is_file() and marker.is_file():
                try:
                    cached = json.loads(marker.read_text(encoding="utf-8"))
                    info = probe(output)
                    if cached == identity and info.has_video and output.suffix == ".mp4":
                        return output
                except (OSError, ValueError, VeditError):
                    pass
            if root.exists():
                shutil.rmtree(root)
            root.mkdir(parents=True)
            preset = "web_preview"
            brief = Brief(name="source-preview", sources={"footage": str(source)},
                          ops=[{"op": "fit"}, {"op": "export", "preset": preset, "path": str(output)}],
                          canvas=get_preset(preset).canvas, hw=False, work_dir=str(root))
            compiler = compile_brief(brief, work_dir=root)
            (root / "brief.yaml").write_text(brief_dumps(brief), encoding="utf-8")
            write_codegen(compiler.pipeline)
            json.loads((root / "project.json").read_text(encoding="utf-8"))
            (root / "render.sh").read_text(encoding="utf-8")
            reports = execute(compiler.pipeline, progress=False)
            write_report(compiler.pipeline, reports, finals=compiler.finals)
            info = probe(output)
            if not info.has_video or output.suffix != ".mp4":
                raise CampaignError("The transport-stream source could not be normalized for browser preview.")
            _write_json(marker, identity)
            return output

    def add_asset(self, ident: str, value: Any) -> dict[str, Any]:
        """Register an uploaded audio or image file, and copy it so it outlives the inbox."""
        if not isinstance(value, dict):
            raise CampaignError("Send a JSON object with the path of an uploaded file.")
        path = self._source(value.get("path"), what="file")
        with self._lock, self._connect() as connection:
            job = self._load(connection, ident)
        if len(job.get("assets") or {}) >= project_media.MAX_ASSETS:
            raise CampaignError(f"A project holds up to {project_media.MAX_ASSETS} audio files and images. Start a new project to add more.", 409)
        try:
            item = project_media.inspect_asset(path)
            if int(shutil.disk_usage(self.root).free) < path.stat().st_size + 16 * 1024 * 1024:
                raise CampaignError("There is not enough free disk space to keep this file in the project.", 507)
            asset_id = project_media.new_asset_id()
            stored = project_media.store_asset(item, self.root / ident / "assets", asset_id)
        except project_media.MediaProblem as exc:
            raise CampaignError(str(exc)) from exc
        beats = project_media.asset_beats(stored) if item.kind == "audio" else None
        record = project_media.asset_record(item, asset_id, beats)
        with self._lock, self._connect() as connection:
            job = self._load(connection, ident)
            job.update(assets=dict(job.get("assets") or {}, **{asset_id: dict(record, path=str(stored))}), updated_at=_now())
            self._save(connection, job)
        return record

    def generate_background(self, ident: str, value: Any) -> dict[str, Any]:
        """Generate a still from a prompt and keep it as an image asset of this project.

        Words only go to Meta, and only for a project made in Meta mode, on that
        project's budget. Its shape follows the footage, because a cutout stands
        on the source picture before anything is reframed.
        """
        if not isinstance(value, dict):
            raise CampaignError("Send a JSON object with a prompt.")
        prompt = _text(value.get("prompt", ""), "Background prompt", 1000, required=True)
        # Spending Meta credit needs an explicit yes. A caller that leaves consent out has not given one.
        if value.get("consent") is not True:
            raise CampaignError("Confirm the cost before generating a background.", 409)
        with self._lock, self._connect() as connection:
            job = self._load(connection, ident)
        if job["request"].get("provider") != "meta" or not capabilities()["meta"].get("available"):
            raise CampaignError("Generated backgrounds need a project made in Meta mode.", 409)
        if len(job.get("assets") or {}) >= project_media.MAX_ASSETS:
            raise CampaignError(f"A project holds up to {project_media.MAX_ASSETS} audio files and images. Start a new project to add more.", 409)
        from vedit import meta

        root = self.root / ident
        source = job.get("source") or {}
        size = meta.background_size(int(source.get("width") or 0), int(source.get("height") or 0))
        try:
            made = meta.generate_background(prompt, size=size, work_dir=root / "meta", budget_usd=job["request"]["budget_usd"])
        except meta.MetaError as exc:
            raise CampaignError(str(exc)) from exc
        try:
            item = project_media.inspect_asset(Path(made["path"]), name=_background_name(prompt))
            asset_id = project_media.new_asset_id()
            stored = project_media.store_asset(item, root / "assets", asset_id)
        except project_media.MediaProblem as exc:
            raise CampaignError(str(exc)) from exc
        record = project_media.asset_record(item, asset_id)
        with self._lock, self._connect() as connection:
            job = self._load(connection, ident)
            job.update(assets=dict(job.get("assets") or {}, **{asset_id: dict(record, path=str(stored))}), updated_at=_now())
            self._save(connection, job)
        return record

    def asset_path(self, job: dict[str, Any], asset_id: str) -> Path:
        """The stored file for an asset id. Only this job's own assets folder can answer."""
        record = (job.get("assets") or {}).get(asset_id)
        folder = (self.root / job["id"] / "assets").resolve()
        path = Path(str(record.get("path") or "")).resolve() if isinstance(record, dict) else None
        if path is None or path.parent != folder or not path.is_file():
            raise CampaignError("That file is not part of this project.", 404)
        return path

    def asset_file(self, ident: str, asset_id: str) -> Path:
        """The file behind one asset, so the live preview can show it without a render."""
        with self._lock, self._connect() as connection:
            job = self._load(connection, ident)
        return self.asset_path(job, asset_id)

    def filmstrip(self, ident: str) -> Path:
        with self._lock, self._connect() as connection:
            job = self._load(connection, ident)
        path = self.root / ident / "filmstrip.jpg"
        if not (job.get("source") or {}).get("filmstrip") or not path.is_file():
            raise CampaignError("This project has no filmstrip yet.", 404)
        return path

    def waveform(self, ident: str) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            job = self._load(connection, ident)
        path = self.root / ident / "waveform.json"
        if not (job.get("source") or {}).get("waveform") or not path.is_file():
            raise CampaignError("This project has no waveform yet.", 404)
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _url(ident: str, name: str, *, revision: int | None = None) -> str:
        suffix = f"&v={revision}" if revision is not None else ""
        return f"/api/campaigns/{ident}/artifact?name={name}{suffix}"

    def _check_cancel(self, ident: str) -> None:
        if self._event(ident).is_set():
            raise _Cancelled()
        with self._lock, self._connect() as connection:
            job = self._load(connection, ident)
        if job.get("cancel_requested") or job["status"] == "cancelled":
            raise _Cancelled()

    def _event(self, ident: str) -> threading.Event:
        with self._lock:
            return self._events.setdefault(ident, threading.Event())

    def _work(self) -> None:
        try:
            while not self._closed:
                try:
                    ident = self._queue.get(timeout=0.25)
                except queue.Empty:
                    continue
                try:
                    if ident is None:
                        return
                    try:
                        self.process(ident)
                    except CampaignError:
                        # Deleted after being cancelled, while it still sat in the queue.
                        pass
                finally:
                    self._queue.task_done()
        finally:
            self._release_lease()

    def process(self, ident: str) -> None:
        """Execute one queued item; exposed for deterministic worker tests."""
        event = self._event(ident)
        job_action, job_revision, job_source, job_versions, job_patches = None, 0, None, [], []
        with self._lock:
            self._active_processes += 1
            self._idle.clear()
        try:
            self._check_cancel(ident)
            with self._lock, self._connect() as connection:
                job = self._load(connection, ident)
            if job["status"] != "queued":
                return
            job_action, job_revision, job_source = job["_action"], job.get("revision", 0), job.get("revision_source")
            job_versions, job_patches = list(job.get("versions") or []), list(job.get("applied_patches") or [])
            if job["_action"] == "render":
                self._render(job)
                self._refine_next(ident)
            else:
                if job["_action"] == "script":
                    job = self._script(job)
                self._plan(job)
                self._render_when_planned(ident)
        except (_Cancelled, RenderCancelled):
            if not self._abort_refine(ident, "Auto-refine was cancelled."):
                self._update(ident, status="cancelled", stage="cancelled", message="Campaign cancelled. Your clip, plan, and any previous verified version are saved.", error=None)
        except Exception as exc:
            # Provider errors are intentionally sanitized by that boundary.
            # Render exceptions can contain full commands and paths: do not expose them.
            if isinstance(exc, CampaignError):
                message = str(exc)
            elif type(exc).__name__ == "MetaError" and type(exc).__module__ == "vedit.meta":
                message = str(exc)
            else:
                # A specific sentence about the file, with no command or path in it.
                message = plain_failure(exc)
            failed = {"status": "failed", "stage": "failed", "message": message, "error": message}
            if job_action == "render":
                # A failed revision stays in the version history so the UI can show which attempt broke.
                failed["versions"] = list(job_versions) + [{"revision": job_revision, "status": "failed", "error": message, "created_at": _now(), "source": job_source or "approved", "passed": None, "total": None, "checks": [], "applied_patches": job_patches}]
            if not self._abort_refine(ident, f"Stopped: version {job_revision} could not be made. {message}", versions=failed.get("versions")):
                self._update(ident, **failed)
        finally:
            try:
                with self._lock, self._connect() as connection:
                    job = self._load(connection, ident)
                self._update(ident, budget=self._usage_summary(self.root / ident, job["request"]["budget_usd"]))
            except CampaignError:
                # An unreadable ledger must block new calls, but must not hide
                # the primary failure which led to the retry screen.
                pass
            finally:
                with self._lock:
                    self._active_processes -= 1
                    if self._active_processes == 0:
                        self._idle.set()
                    if self._events.get(ident) is event:
                        self._events.pop(ident, None)
                    if self._closed and self._worker is None and self._active_processes == 0:
                        self._release_lease()

    def _render_when_planned(self, ident: str) -> None:
        """A short renders without a second approval: asking for it was the approval."""
        with self._lock, self._connect() as connection:
            job = self._load(connection, ident)
        if job["status"] != "planned" or not job["request"].get("auto_render"):
            return
        try:
            self.render(ident, {})
        except CampaignError as exc:
            self._update(ident, message=f"The plan is ready, but the render could not start: {exc}")

    @staticmethod
    def _restore_verified(job: dict[str, Any], reason: str) -> None:
        """Put a job back on its last verified version after a refining round did not finish."""
        base = job.pop("_refine_base")
        result = job.get("result")
        job.update(status="complete", stage="complete", progress=1, message=f"Version {base['revision']} is ready. {reason}", error=None,
                   plan=base["plan"], revision=base["revision"], revision_source=base["source"], applied_patches=base["applied_patches"],
                   note=base["note"], cancel_requested=False, result=dict(result, stale=False) if isinstance(result, dict) else result,
                   auto_refine=dict(job.get("auto_refine") or {}, active=False, stopped=reason), updated_at=_now())

    def _abort_refine(self, ident: str, reason: str, *, versions: list[dict[str, Any]] | None = None) -> bool:
        """End a refining round that failed or was cancelled. The verified version stays current."""
        with self._lock, self._connect() as connection:
            job = self._load(connection, ident)
            if job.get("revision_source") != "auto_refine" or not isinstance(job.get("_refine_base"), dict):
                # The first render of a loop has nothing to fall back on, but the loop is still over.
                state = job.get("auto_refine")
                if isinstance(state, dict) and state.get("active"):
                    job["auto_refine"] = dict(state, active=False, stopped=reason)
                    self._save(connection, job)
                return False
            self._restore_verified(job, reason)
            if versions is not None:
                job["versions"] = versions
            self._save(connection, job)
            return True

    def _refine_next(self, ident: str) -> None:
        """After a verified render, queue the next round or record why the loop stopped.

        Only a render a person approved, or a round of its loop, continues: a note
        or an applied fix is a decision the loop must not second-guess. This runs
        after the render succeeded, so nothing here may fail the project.
        """
        with self._lock, self._connect() as connection:
            job = self._load(connection, ident)
            request = job["request"]
            limit = int(request.get("auto_refine") or 0)
            origin = job.get("revision_source") or "approved"
            if job["status"] != "complete" or not limit or not request.get("review") or origin not in {"approved", "auto_refine"} or not isinstance(job.get("result"), dict):
                return
            state = dict(job.get("auto_refine") or {}, limit=limit)
            rounds = int(state.get("rounds") or 0)
            result = job["result"]
            failed = sum(1 for check in result.get("checks") or [] if isinstance(check, dict) and not check.get("passed"))
            seen = list(job.get("_refine_seen") or []) if origin == "auto_refine" else []
            seen.append(self._cut_key(job["plan"]))
            try:
                budget = self._usage_summary(self.root / ident, request["budget_usd"])
                cost = sum(budget["operations"][name]["reserved_usd"] / budget["operations"][name]["calls"] for name in _RENDER_OPERATIONS if budget["operations"].get(name, {}).get("calls"))
                stop = refine_stop(result, rounds=rounds, limit=limit, worse_than=state.get("failed"), remaining_usd=budget["remaining_usd"], round_cost_usd=cost)
                if stop is None:
                    patches = [dict(patch) for patch in result.get("patches") or [] if isinstance(patch, dict)]
                    plan, applied = self._patched(job["plan"], patches, request, job["source"]["duration"])
                    if self._cut_key(plan) in seen:
                        stop = "Stopped: the remaining fixes lead back to a version already made."
            except CampaignError as exc:
                stop = f"Stopped: {exc}"
            if stop is None:
                base = {"plan": job["plan"], "revision": job["revision"], "source": origin, "applied_patches": job.get("applied_patches") or [], "note": job.get("note")}
                try:
                    job.update(_refine_base=base, _refine_seen=seen, auto_refine=dict(state, rounds=rounds + 1, active=True, stopped=None, failed=failed))
                    effect = _effect(request.get("effect"), duration=plan["duration"])
                    self._enqueue_render(connection, job, plan, effect, not request.get("tracking_disabled", False), revision_source="auto_refine", applied_patches=applied, note=None)
                    return
                except CampaignError as exc:
                    stop = f"Stopped: {exc}"
                    job = self._load(connection, ident)
            job.pop("_refine_base", None)
            versions = [dict(entry) for entry in job.get("versions") or []]
            if versions:
                versions[-1]["stopped"] = stop
            job.update(auto_refine=dict(state, rounds=rounds, active=False, stopped=stop, failed=failed), versions=versions,
                       message=f"Version {job['revision']} is ready. {stop}", updated_at=_now())
            self._save(connection, job)

    @staticmethod
    def _cut_key(plan: dict[str, Any]) -> str:
        """What makes two versions the same cut: the moments and the settings, not the notes on them."""
        moments = [[segment.get("start"), segment.get("end"), segment.get("speed")] for segment in plan.get("segments") or []]
        return hashlib.sha256(_json([moments, plan.get("edit")]).encode()).hexdigest()[:16]

    @staticmethod
    def _tracked_subject(request: dict[str, Any]) -> str | None:
        """The subject string tracking ran on, or None when no tracking was requested."""
        if request.get("tracking_disabled"):
            return None
        return request["subject"] if request.get("provider") == "meta" and request.get("subject") else None

    @staticmethod
    def _usage_summary(root: Path, cap: float) -> dict[str, Any]:
        """Reserve every attempted provider call, including uncertain failures.

        Successful cache reads do not append a new call id. A finished call holds
        what Meta billed (``meta.held_usd``); a running or uncertain one holds its
        reservation, so a retry can never spend it twice.
        ``operations`` splits the same reservations by Meta operation (story,
        tracking, cover) so a reader can see which step held the money.
        """
        from vedit import meta

        reservations: dict[str, float] = {}
        attribution: dict[str, tuple[str, str]] = {}
        try:
            for ledger in root.glob("**/meta-usage.json"):
                data = json.loads(ledger.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or not isinstance(data.get("calls"), list):
                    raise ValueError
                for entry in data["calls"]:
                    if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                        raise ValueError
                    # Validate the recorded numbers, then count what the call holds now, as the meta adapter does.
                    _number(entry.get("reserved_usd"), "Recorded Meta reservation", 0, 100)
                    usage = entry.get("usage", {})
                    if isinstance(usage, dict):
                        _number(usage.get("cost_usd", 0), "Recorded Meta usage", 0, 100)
                    reservations[entry["id"]] = max(reservations.get(entry["id"], 0), meta.held_usd(entry))
                    operation = entry.get("operation")
                    model = entry.get("model")
                    attribution[entry["id"]] = (operation if isinstance(operation, str) and operation else "other", model if isinstance(model, str) else "")
        except (ValueError, OSError, CampaignError) as exc:
            raise CampaignError("The campaign cost record is unreadable. Preserve this project and start a new campaign; no further Meta calls were made.") from exc
        total = round(sum(reservations.values()), 8)
        operations: dict[str, dict[str, Any]] = {name: {"reserved_usd": 0.0, "calls": 0, "model": ""} for name in _META_OPERATIONS}
        for call_id, amount in reservations.items():
            operation, model = attribution[call_id]
            bucket = operations.setdefault(operation, {"reserved_usd": 0.0, "calls": 0, "model": ""})
            bucket["reserved_usd"] = round(bucket["reserved_usd"] + amount, 8)
            bucket["calls"] += 1
            if model:
                bucket["model"] = model
        return {"cap_usd": cap, "reserved_usd": total, "remaining_usd": max(0, round(cap - total, 8)), "operations": operations}

    def _open_source(self, ident: str, job: dict[str, Any]) -> tuple[Path, Any, dict[str, Any]]:
        """The file this project reads, its probe, and what Studio shows about it."""
        request = job["request"]
        if job.get("_project_file") and job.get("source"):
            source = self._project_source(job)
            return source, probe(source), dict(job["source"])
        if len(request["paths"]) == 1:
            source = self._source(request["paths"][0])
            info = probe(source)
            if not project_media.is_still(source, info):
                problem = source_problem(info, min_seconds=0.1, max_seconds=_MAX_SOURCE_SECONDS)
                if problem:
                    raise CampaignError(problem)
                check_playable(info)
                info = probe(source)  # read again: the first look measured any letterbox bars, so this one reports the cropped picture
                width, height = getattr(info, "display_width", info.width), getattr(info, "display_height", info.height)
                if not (request.get("aspect") == "16:9" and height > width):
                    clips = [{"name": source.name, "kind": "video", "start": 0.0, "end": round(info.duration, 3)}]
                    return source, info, {"name": source.name, "duration": info.duration, "width": width, "height": height, "has_audio": info.has_audio, "has_video": info.has_video, "clips": clips}
        return self._build_project(ident, job)

    def _build_project(self, ident: str, job: dict[str, Any]) -> tuple[Path, Any, dict[str, Any]]:
        """Normalize every clip and photo into the job's one project source."""
        request, root = job["request"], self.root / ident
        seconds = request.get("photo_seconds", project_media.PHOTO_SECONDS)
        try:
            items = [project_media.inspect_clip(self._source(raw), _MAX_SOURCE_SECONDS) for raw in request["paths"]]
        except project_media.MediaProblem as exc:
            raise CampaignError(str(exc)) from exc
        total = sum(project_media.frame_count(item, seconds) for item in items) / project_media.FPS
        if total > _MAX_SOURCE_SECONDS:
            raise CampaignError(f"These clips run {total / 60:.0f} minutes. A project holds up to {_MAX_SOURCE_SECONDS / 60:.0f} minutes, so remove a clip and try again.")
        dest = root / project_media.PROJECT_FILE
        parent = (request.get("short") or {}).get("parent")
        if parent and not dest.exists():
            # A short reads the same footage as its parent, so it shares the finished file.
            with suppress(OSError):
                os.link(self.root / parent / project_media.PROJECT_FILE, dest)
        if not dest.is_file():
            portrait_to_wide = (len(items) == 1 and request.get("aspect") == "16:9"
                                and items[0].height > items[0].width)
            canvas = (1920, 1080) if portrait_to_wide else project_media.choose_canvas(items)
            self._disk_preflight("project", total, SimpleNamespace(width=canvas[0], height=canvas[1]))
        else:
            portrait_to_wide = (len(items) == 1 and request.get("aspect") == "16:9"
                                and items[0].height > items[0].width)
            canvas = (1920, 1080) if portrait_to_wide else project_media.choose_canvas(items)

        def progress(index: int, steps: int, label: str) -> None:
            self._check_cancel(ident)
            self._update(ident, stage="combining", progress=round(0.13 + 0.08 * index / steps, 3), message=label + ".")

        try:
            built = project_media.build_project(items, work_dir=root / project_media.PROJECT_FOLDER, dest=dest, photo_seconds=seconds, canvas=canvas,
                                                on_step=progress, cancel_event=self._event(ident))
        except project_media.MediaProblem as exc:
            raise CampaignError(str(exc)) from exc
        info = probe(dest)
        name = items[0].name if len(items) == 1 else f"{items[0].name} and {len(items) - 1} more"
        details = {"name": name, "duration": info.duration, "width": built["width"], "height": built["height"], "has_audio": built["has_audio"], "has_video": True, "clips": built["clips"]}
        self._update(ident, _project_file=project_media.PROJECT_FILE)
        return dest, info, details

    def _timeline_media(self, ident: str, source: Path, info: Any, details: dict[str, Any]) -> list[str]:
        """Make the filmstrip and waveform side by side. Studio can edit without either, so a failure is only a warning."""
        self._check_cancel(ident)
        self._update(ident, stage="timeline", progress=0.93, message="Preparing the timeline.")
        root, event, media, warnings = self.root / ident, self._event(ident), dict(details), []
        with ThreadPoolExecutor(max_workers=2) as pool:
            strip = pool.submit(project_media.make_filmstrip, source, root / "filmstrip.jpg", duration=details["duration"], width=details["width"], height=details["height"], stream=info.video_map, cancel_event=event)
            sound = pool.submit(project_media.make_waveform, source, root / "waveform.json", duration=details["duration"], has_audio=info.has_audio, stream=info.audio_map, cancel_event=event)
        try:
            media["filmstrip"] = dict(strip.result(), url=f"/api/campaigns/{ident}/filmstrip")
        except RenderCancelled:
            raise
        except Exception:
            warnings.append("The timeline picture strip could not be made. Editing still works.")
        try:
            waveform = sound.result()
            media["waveform"] = {"url": f"/api/campaigns/{ident}/waveform", "count": len(waveform["peaks"]), "seconds_per_peak": waveform["seconds_per_peak"]}
        except RenderCancelled:
            raise
        except Exception:
            warnings.append("The timeline sound wave could not be made. Editing still works.")
        self._update(ident, source=media)
        return warnings

    def _hear(self, ident: str, request: dict[str, Any], source: Path, info: Any, root: Path, warnings: list[str],
              *, recorded: bool = True) -> list[Cue] | None:
        """The project's speech, heard once by Meta Voice Transcribe. None when Meta did not listen.

        A failure never falls back to another model. The plan goes on without a
        transcript, says so, and the render asks Meta again for the kept speech.
        A project with no sound of its own, or a silent track, is never sent, so
        the plan records no transcript rather than an empty one Meta never made.
        """
        if not info.has_audio or not recorded:
            return None
        from vedit.analyze import audible

        if not audible(source, track=info.audio_map):
            warnings.append("The sound track is silent, so it was not sent to Meta Voice Transcribe. This plan has no transcript.")
            return None
        if request.get("short"):
            # A short cuts its parent's source; the parent's Meta transcript is already on that clock.
            with self._lock, self._connect() as connection:
                try:
                    parent = self._load(connection, request["short"]["parent"]).get("plan")
                except CampaignError:
                    parent = None
            if isinstance(parent, dict) and _heard_by_meta(parent):
                return [Cue(float(line["start"]), float(line["end"]), str(line["text"])) for line in parent["analysis"].get("transcript") or []]
        self._update(ident, stage="transcribing", progress=0.16, message="Asking Meta to transcribe the speech.")
        try:
            heard = transcribe_speech(source, root / "analysis" / "heard", language=request["language"], budget_usd=request["budget_usd"], meta_dir=root / "meta")
        except RenderCancelled:
            raise
        except VeditError as exc:
            warnings.append(f"Meta Voice Transcribe did not finish ({plain_failure(exc)}). This plan has no transcript, so moments follow scene cuts and speech energy. Captions will be heard by Meta at render.")
            return None
        self._check_cancel(ident)
        return heard

    def _plan(self, job: dict[str, Any]) -> None:
        ident, request = job["id"], job["request"]
        root = self.root / ident
        started = time.monotonic()
        self._update(ident, status="analyzing", stage="reading", progress=0.12, message="Checking the source video.")
        source, info, details = self._open_source(ident, job)
        self._update(ident, source=details)
        self._check_cancel(ident)
        clips = [(clip["start"], clip["end"]) for clip in details["clips"]]
        warnings = []
        if request["captions"] and _caption_provider(request) == "local" and not request.get("script"):
            warnings.append(CAPTIONS_NEED_META + " This render will omit captions.")
        if not details["has_audio"] and request["captions"] and not request.get("script"):
            warnings.append("This source has no audio. Captions cannot be generated.")
        self._update(ident, stage="planning", progress=0.45, message="Finding a story in your footage." if request["provider"] == "meta" else "Preparing an editable local cut.")
        cache = root / "model-plan.json"
        short = request.get("short")
        used = self._used_ranges(job) if short else []
        if request.get("script"):
            proposed = self._script_story(job, source, info, clips, warnings)
        elif request["provider"] == "meta":
            from vedit import meta
            from vedit.story import analyze_signals, annotate_segments, clip_cuts, snap_to_sentences

            long_form = info.duration > meta.WINDOW_SECONDS + 0.05
            if long_form and not cache.is_file():
                # Said before anything is encoded or paid for: the expected cost, against this project's budget.
                self._long_affordable(job, info, details)
            # Meta Voice Transcribe hears the speech once. The planner, the lint,
            # find, and the captions all read this one transcript; FFmpeg measures
            # scene cuts and speech bands. No local model runs. Disk is checked
            # first, so nothing is uploaded for an analysis that cannot finish.
            self._disk_preflight("analysis", info.duration, info)
            heard = self._hear(ident, request, source, info, root, warnings, recorded=bool(details["has_audio"]))
            self._update(ident, stage="measuring", progress=0.22, message="Measuring scene cuts and speech in your footage.")
            signals = analyze_signals(source, work_dir=root / "analysis" / "signals", heard=heard, transcribed_by=TRANSCRIBER if heard is not None else None, cuts=clip_cuts(clips))
            warnings.extend(signals.pop("warnings", []))
            signals.pop("has_audio", None)
            self._check_cancel(ident)
            if cache.is_file():
                proposed = json.loads(cache.read_text(encoding="utf-8"))
            elif long_form:
                proposed = self._long_story(job, source, info, signals, used)
                _write_json(cache, proposed)
            else:
                self._update(ident, stage="preparing", progress=0.3, message="Making a small analysis copy; your original stays unchanged.")
                proxy = self._analysis_proxy(root, source, info, cancel_event=self._event(ident))
                self._check_cancel(ident)
                self._update(ident, stage="planning", progress=0.55, message="Finding a story in your footage.")
                if short:
                    proposed = self._short_story(job, proxy, info, signals, used)
                else:
                    proposed = meta.plan_story(proxy, goal=request["goal"], duration=info.duration, target_duration=min(info.duration, request["target_duration"]), work_dir=root / "meta", language=request["language"], budget_usd=request["budget_usd"], transcript=signals.get("transcript") or None)
                _write_json(cache, proposed)
            proposed["provider"] = "meta"
            segments = list(proposed.get("segments") or [])
            if signals.get("sentences"):
                # The model works from breath-sized speech bands; finish the
                # sentence it chose instead of cutting mid-word. Local, free.
                segments = snap_to_sentences(segments, signals["sentences"], duration=info.duration, target=min(info.duration, request["target_duration"]))
            proposed["segments"] = annotate_segments(segments, signals.get("speech") or [])
            notes = [str(proposed.get("long_form", {}).get("note") or "Muse Spark selected the moments.") + " FFmpeg measured scene cuts and speech bands."]
            if heard is not None:
                notes.append("Meta Voice Transcribe wrote the transcript. It times turns, not words, so a sentence end inside a turn is estimated by length and filler removal has no word times to cut at.")
            proposed["analysis"] = dict(signals, method="meta", notes=notes)
        else:
            from vedit.story import StoryError, plan_local_story

            # Deterministic signal analysis: scene cuts, speech energy, optional
            # local transcript keywords. Labeled as such; it is not AI understanding.
            self._update(ident, stage="planning", progress=0.5, message="Measuring scene cuts and speech in your footage.")
            try:
                proposed = plan_local_story(source, goal=request["goal"], target_duration=request["target_duration"], work_dir=root / "analysis" / "local-story", language=request["language"] if request["language"] != "auto" else "en",
                                            clips=clips, exclude=[(low, high) for low, high in used], variation=short["index"] if short else 0)
            except StoryError as exc:
                raise CampaignError(str(exc)) from exc
            proposed["provider"] = "local"
            if short:
                proposed["title"] = _text(f"Short {short['index']}: {proposed['segments'][0].get('label') or proposed['title']}"[:120], "Title", 120)
            if request["subject"]:
                warnings.append("Subject tracking needs Meta mode. This local cut will use a center crop.")
        warnings.extend(proposed.get("warnings", []))
        if not details["has_audio"]:
            # A project of photos or silent clips plays a generated silent track. Nobody recorded it, so it is not
            # reported as a silent sound track next to "This source has no audio".
            warnings = [warning for warning in warnings if not warning.startswith("This clip's sound track is silent")]
        if len(clips) > 1:
            # A project joins its clips into one file; a moment that strays a few frames into a neighbour is trimmed back.
            proposed["segments"] = snap_to_clips(list(proposed.get("segments") or []), details["clips"])
        ends = brief_bookends(request["goal"])
        if proposed.get("provider") == "meta" and proposed.get("segments") and (ends["opening"] or ends["ending"]):
            # Muse Spark read the brief and ordered the moments, so the first and last moments are its opening and ending.
            picked = {"opening": proposed["segments"][0], "ending": proposed["segments"][-1]}
            proposed["bookends"] = {key: {"label": str(picked[key].get("label") or ""), "start": picked[key].get("start"), "end": picked[key].get("end")}
                                    for key in ("opening", "ending") if ends[key]}
        if request["target_duration"] > info.duration + 0.05:
            # The target is a ceiling, so a short source is not an error, but the receipt says it was not reached.
            held = ""
            if details["clips"] and all(clip.get("kind") == "photo" for clip in details["clips"]):
                seconds = float(request.get("photo_seconds", project_media.PHOTO_SECONDS))
                held = f" Each photo holds for {seconds:g} s. Set a longer photo length to fill more."
            warnings.append(f"You asked for {request['target_duration']:.1f} s, but the footage runs {info.duration:.1f} s, so the cut is at most {info.duration:.1f} s.{held}")
        warnings.extend(source_notes(info))
        shape = shape_mismatch(request["goal"], request["aspect"])
        if shape:
            warnings.append(shape)
        proposed["warnings"] = warnings
        plan = validate_plan(proposed, info.duration, original=proposed)
        # Settings are visible during review; final timing is checked against
        # the user's approved ranges when they ask to render.
        chosen = _chosen_settings(request)
        brief = brief_edit(request["goal"], plan=plan, has_audio=details["has_audio"], alongside=chosen)
        plan = validate_plan(dict(plan, edit=merge_edit(brief["edit"], chosen), decisions=brief["decisions"], ignored_edits=brief["ignored"]),
                             info.duration, original=plan, assets=job.get("assets"))
        plan["effect"] = request.get("effect")
        plan["warnings"] = list(plan.get("warnings") or []) + self._timeline_media(ident, source, info, details)
        _write_json(root / "plan.json", plan)
        self._check_cancel(ident)
        timings = dict(job.get("timings") or {}, plan_seconds=round(time.monotonic() - started, 3))
        self._update(ident, status="planned", stage="review", progress=1, message="Review the story and approve the render.", plan=plan, error=None, timings=timings)

    def _disk_preflight(self, phase: str, duration: float, info: Any, *, effect: bool = False) -> dict[str, int | str]:
        """Estimate new working files, leaving a configurable free-space cushion.

        The estimate includes the selected intermediate, final encode, archive,
        and audio work. Existing accepted revisions are already reflected in the
        volume's free-space reading and are never counted as disposable space.
        """
        try:
            minimum_mb = float(os.environ.get("VEDIT_MIN_FREE_DISK_MB", "64"))
            if not math.isfinite(minimum_mb) or not 0 <= minimum_mb <= 1_048_576:
                raise ValueError
        except ValueError as exc:
            raise CampaignError("VEDIT_MIN_FREE_DISK_MB must be a finite number between 0 and 1048576.") from exc
        duration = _number(duration, "Working duration", 0.01, _MAX_SOURCE_SECONDS)
        if phase == "analysis":
            # Small 30fps analysis MP4 plus its selected-range intermediate.
            bytes_per_second = 2_000_000
        elif phase == "project":
            # High-quality clips and their join, both on disk until the join is verified.
            bytes_per_second = 2 * max(2_000_000, info.width * info.height * 30 * 0.1) + 768_000
        else:
            width = max(1, getattr(info, "display_width", info.width))
            height = max(1, getattr(info, "display_height", info.height))
            input_rate = info.size_bytes / max(0.1, info.duration)
            intermediate_rate = min(60_000_000, max(2_000_000, width * height * 30 * 0.12, input_rate * 2))
            # Delivery master + ZIP copy + possible encode retry, and PCM audio.
            bytes_per_second = intermediate_rate + 3 * 3_000_000 + 768_000
            if effect:
                bytes_per_second += 8_000_000
        required = math.ceil(duration * bytes_per_second + 4 * 1024 * 1024 + minimum_mb * 1024 * 1024)
        try:
            available = int(shutil.disk_usage(self.root).free)
        except OSError as exc:
            raise CampaignError("Free disk space could not be checked. Verify the project drive is available and retry.") from exc
        if available < required:
            raise CampaignError(f"Not enough free disk space for {phase}. Estimated required: {required / 1024**2:.1f} MB; available: {available / 1024**2:.1f} MB. Free space on the project drive and retry; your verified version is preserved.")
        return {"stage": phase, "required_bytes": required, "available_bytes": available}

    def _review_cut(self, ident: str, job: dict[str, Any], work: Path, video: Path, plan: dict[str, Any], duration: float, warnings: list[str]) -> dict[str, Any]:
        """Ask the director (Muse Spark, or Muse Wonder when chosen) for notes on a small copy of the verified output, in its own shape.

        The director must see what a viewer sees, so a vertical video is reviewed
        vertical: fitted into a wide frame it would gain bars and tiny captions
        that the finished file does not have. A failed review names the director
        it asked, and Meta's refusal says what budget a Wonder review needs.
        """
        from vedit import meta

        request, root = job["request"], self.root / ident
        # None lets VEDIT_META_DIRECTOR choose, else Muse Spark.
        choice = request.get("director")
        try:
            label = meta._DIRECTOR_LABELS[meta._director(choice)]
        except meta.MetaError:
            label = None
        name = "Muse Wonder" if label == "muse-wonder" else "Muse Spark"
        self._update(ident, stage="reviewing", progress=0.9, message=f"Asking {name} to review the finished cut.")
        try:
            proxy_dir = work / "review-proxy"
            proxy = proxy_dir / "review.mp4"
            if not (proxy.is_file() and proxy.stat().st_size):
                proxy_dir.mkdir(parents=True, exist_ok=True)
                preset = _REVIEW_PRESETS.get(request.get("aspect"), "web_preview")
                brief = Brief(name="review-proxy", sources={"footage": str(video)}, ops=[{"op": "fit"}, {"op": "export", "preset": preset, "path": str(proxy)}], canvas=get_preset(preset).canvas, hw=False, work_dir=str(proxy_dir))
                compiler = compile_brief(brief, work_dir=proxy_dir)
                write_codegen(compiler.pipeline)
                execute(compiler.pipeline, progress=False, cancel_event=self._event(ident))
            self._check_cancel(ident)
            notes = meta.review_cut(proxy, goal=request["goal"], plan=dict(plan, duration=duration), work_dir=root / "meta", language=(request.get("language") or "auto"),
                                    budget_usd=request["budget_usd"], director=choice)
            warnings.extend(notes.get("warnings", []))
            return notes
        except (_Cancelled, RenderCancelled):
            raise
        except Exception as exc:
            if type(exc).__name__ == "MetaError" and type(exc).__module__ == "vedit.meta":
                message = str(exc)
            elif isinstance(exc, (CampaignError, VeditError)):
                message = "The finished cut could not be prepared for review."
            else:
                message = "The review did not complete."
            warnings.append("Director's notes unavailable: " + message + " The verified video is unaffected.")
            return {"error": message, "director": label} if label else {"error": message}

    def _emphasis(self, ident: str, job: dict[str, Any], plan: dict[str, Any], warnings: list[str]) -> list[dict[str, Any]] | None:
        """Ask Muse Spark for each caption line's punch word when the edit wants them and names none yet.

        The picks are data: ``emphasis.suggest_emphasis`` keeps only words the
        lines really have, and ``validate_edit`` bounds the merged edit. The
        merged edit is saved on the plan, so the next version reuses it. A
        failure is a warning: the video renders with plain captions. Returns the
        picks with Spark's ``why``, or None when nothing was asked.
        """
        request = job["request"]
        captions = plan["edit"].get("captions") or {}
        if not request.get("captions") or not captions.get("emphasize") or captions.get("emphasis"):
            return None
        if translation_target(captions, request, plan=plan):
            # Punch words are not drawn on a translation, so none are bought. The captions stage says so.
            return None
        if request.get("provider") != "meta" and _caption_provider(request) != "meta":
            warnings.append("Punch words are picked by Muse Spark, and this project does not use Meta, so the captions stay plain.")
            return None
        from vedit import emphasis, meta
        from vedit.captions import story_cues

        self._update(ident, stage="emphasis", progress=0.07, message="Asking Muse Spark for the punch word in each caption line.")
        try:
            picked = emphasis.suggest_emphasis(story_cues(plan), request["goal"], (request.get("language") or "auto"), work_dir=self.root / ident / "meta",
                                               budget_usd=request["budget_usd"])
            edit = validate_edit(merge_edit(plan["edit"], {"captions": picked["edit"]}), duration=_LONGEST_EDIT, assets=job.get("assets"))
        except (_Cancelled, RenderCancelled):
            raise
        except (emphasis.EmphasisError, meta.MetaError, EditError) as exc:
            warnings.append(f"Punch words were not added: {exc} The captions stay plain.")
            return None
        except Exception:
            warnings.append("Punch words were not added: the request did not complete. The captions stay plain.")
            return None
        self._check_cancel(ident)
        warnings.extend(picked.get("warnings") or [])
        if not picked["emphasis"]:
            warnings.append("Muse Spark found no word worth a punch in these captions, so they stay plain.")
        plan["edit"] = edit
        self._update(ident, plan=plan, _emphasis_picks=picked["emphasis"], _emphasis_model=picked.get("model"))
        return picked["emphasis"]

    @staticmethod
    def _hero_spoken(plan: dict[str, Any], spoken: Any) -> dict[str, Any] | None:
        """The edit's hero word, with when Spark's pick is said when the word and window are still that pick."""
        hero = plan["edit"].get("hero_word")
        if not hero:
            return None
        if isinstance(spoken, dict) and spoken.get("word") == hero["word"] and isinstance(spoken.get("start"), (int, float)) \
                and abs(float(spoken["start"]) - float(hero["start"])) < 1e-3 and spoken.get("spoken"):
            return dict(hero, spoken=spoken["spoken"])
        return dict(hero)

    @staticmethod
    def _emphasis_result(plan: dict[str, Any], picks: list[dict[str, Any]] | None, request: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """The punch words this render draws, each with the model's ``why`` when a model picked it.

        Translated captions draw none, so with ``request`` a translation reports none.
        """
        captions = plan["edit"].get("captions") or {}
        if not captions.get("emphasize") or (request is not None and translation_target(captions, request, plan=plan)):
            return []
        whys = {(pick.get("cue_key"), pick.get("word_index")): pick.get("why", "") for pick in picks or [] if isinstance(pick, dict)}
        return [dict(pick, why=whys.get((pick["cue_key"], pick["word_index"]), "")) for pick in captions.get("emphasis") or []]

    def _hero_matte(self, job: dict[str, Any], source: Path, plan: dict[str, Any], fps: float, warnings: list[str]) -> tuple[Path | None, dict[str, Any] | None]:
        """SAM's matte of the hero word's subject over its window only, with the side and subject box written back into the edit.

        Only a Meta project masks. Any failure is a warning: the word is left
        out of this version, and its receipt says no matte was made.
        """
        hero, request, ident = plan["edit"].get("hero_word"), job["request"], job["id"]
        if not hero:
            return None, None
        if request.get("provider") != "meta":
            warnings.append("The hero word needs SAM to cut the subject out, and this project does not use Meta, so it is left out.")
            return None, None
        from vedit import hero_word, meta
        from vedit.edit_spec import validate_hero_word

        self._update(ident, stage="hero_word", progress=0.15, message=f"Cutting out {hero['subject']} behind “{hero['word']}”.")
        try:
            # reanchor: a cut re-trimmed since the word was set moves the window to where the word is said now.
            made = hero_word.window_matte(source, plan, hero, work_dir=self.root / ident, budget_usd=request["budget_usd"], fps=fps,
                                          aspect=request.get("aspect"), cancel_event=self._event(ident), reanchor=True)
            # The side, colour and window come from what was measured on this footage, not from the proposal.
            measured = made.get("edit") if isinstance(made.get("edit"), dict) else dict(hero, side=made["side"], subject_box=made["subject_box"],
                                                                                     color=made.get("color") or hero.get("color"))
            placed = validate_hero_word(measured, duration=float(plan["duration"]))
        except (_Cancelled, RenderCancelled):
            raise
        except (hero_word.HeroWordError, meta.MetaError, EditError) as exc:
            warnings.append(f"The hero word was left out: {getattr(exc, 'plain', '') or exc}")
            return None, None
        except VeditError as exc:
            warnings.append(f"The hero word was left out: {plain_failure(exc)}")
            return None, None
        except OSError:
            warnings.append("The hero word was left out: its subject mask could not be saved. Check free disk space and render again.")
            return None, None
        self._check_cancel(ident)
        warnings.extend(made.get("warnings") or [])
        for note in (made.get("side_note"), made.get("color_note")):
            if note:
                warnings.append(note)
        plan["edit"] = dict(plan["edit"], hero_word=placed)
        self._update(ident, plan=plan)
        return Path(made["path"]), dict(made["stats"])

    def suggest_hero_word(self, ident: str, *, subject: str | None = None, font: str = "heavy", color: str = "#FFFFFF") -> dict[str, Any]:
        """Ask Muse Spark for this cut's hero word and return a checked ``edit.hero_word``. Nothing renders.

        Spark reads the cut's caption words only and proposes; ``hero_word``
        checks the word is said, inside one shot, for 0.5 to 3 seconds, and
        ``validate_hero_word`` bounds the edit. SAM runs later, at render, on the
        word's window only. Meta projects only, on the project's budget. The
        client applies ``hero_word`` with a render or an edit.
        """
        from vedit import hero_word, meta
        from vedit.captions import preview_cues
        from vedit.edit_spec import HERO_COLORS, HERO_FONTS

        subject = _text(subject or "", "Hero word subject", 150)
        fonts = [row[0] for row in HERO_FONTS]
        if not isinstance(font, str) or font not in fonts:
            raise CampaignError("Choose a hero word font: " + ", ".join(fonts) + ".")
        if not isinstance(color, str) or color.upper() not in {str(item).upper() for item in HERO_COLORS}:
            raise CampaignError("Choose a hero word color: " + ", ".join(HERO_COLORS) + ".")
        color = next(str(item) for item in HERO_COLORS if str(item).upper() == color.upper())
        with self._lock, self._connect() as connection:
            job = self._load(connection, ident)
        if not job.get("plan") or not job.get("source"):
            raise CampaignError("Create a story plan before picking a hero word.", 409)
        request = job["request"]
        if request.get("provider") != "meta" or not capabilities()["meta"].get("available"):
            raise CampaignError("The hero word needs a project made in Meta mode, with Meta connected.", 409)
        # SAM is never sent a guessed subject: footage with no person would be billed and the word dropped.
        subject = subject or _text(request.get("subject") or "", "Hero word subject", 150)
        if not subject:
            raise CampaignError("Name what the word sits behind, for example the person in the red jacket.")
        plan = job["plan"]
        root = self.root / ident
        try:
            suggestion = hero_word.suggest_hero_word(plan, preview_cues(plan), work_dir=root / "meta", budget_usd=request["budget_usd"],
                                                     aspect=request.get("aspect", "9:16"), language=(request.get("language") or "auto"), subject=subject)
            edit = hero_word.build_hero_word_edit(plan, suggestion, subject=subject, font=font, color=color)
        except (hero_word.HeroWordError, meta.MetaError) as exc:
            raise CampaignError(str(getattr(exc, "plain", "") or exc), 409) from exc
        except EditError as exc:
            raise CampaignError(str(exc)) from exc
        spoken = {"word": edit["word"], "start": edit["start"], "spoken": suggestion.get("spoken")}
        budget = self._usage_summary(root, request["budget_usd"])
        self._update(ident, _hero_spoken=spoken, budget=budget)
        warnings = [] if meta.mask_decoder_ready() else ["Rendering the hero word needs Meta's mask decoder on this machine. Run: uv sync --extra sam"]
        translated = translation_target(plan["edit"].get("captions"), request, plan=plan) if request.get("captions") else None
        if translated:
            warnings.append(f"The hero word is the spoken word, so it stays as said while the captions are in {meta.write_language(translated, translated)}.")
        return {"hero_word": edit, "proposal": suggestion.get("proposal"), "adjusted": list(suggestion.get("adjusted") or []),
                "usage": suggestion.get("usage"), "model": suggestion.get("model"), "spoken": suggestion.get("spoken"),
                "estimated": bool(suggestion.get("estimated")), "budget": budget, "warnings": warnings}

    def suggest_motion_card(self, ident: str, *, kind: str = "intro_title", backdrop: bool = True) -> dict[str, Any]:
        """Ask Muse Spark to design a 3D motion card for this cut, and Muse Image for its backdrop. Nothing renders.

        Spark reads the goal and the cut's caption words only and picks closed
        choices; ``blender_cards`` validates them into ``edit.motion_card``. With
        ``backdrop``, Muse Image paints the plate the card opens on ($0.01) and
        it is kept as an image asset of this project. Meta projects only, on the
        project's budget, and only where Blender can render the card. The client
        applies ``motion_card`` with a render or an edit.
        """
        from vedit import blender_cards, meta
        from vedit.captions import preview_cues
        from vedit.edit_spec import MOTION_CARD_KINDS, validate_motion_card

        kinds = [row[0] for row in MOTION_CARD_KINDS]
        if not isinstance(kind, str) or kind not in kinds:
            raise CampaignError("Choose a motion card kind: " + ", ".join(kinds) + ".")
        if not isinstance(backdrop, bool):
            raise CampaignError("backdrop must be true or false.")
        with self._lock, self._connect() as connection:
            job = self._load(connection, ident)
        if not job.get("plan") or not job.get("source"):
            raise CampaignError("Create a story plan before designing a motion card.", 409)
        request = job["request"]
        if request.get("provider") != "meta" or not capabilities()["meta"].get("available"):
            raise CampaignError("The motion card needs a project made in Meta mode, with Meta connected.", 409)
        if not blender_cards.blender_available():
            raise CampaignError("3D motion cards need Blender on this machine. Install Blender 4.2 or later, or set VEDIT_BLENDER.", 409)
        warnings: list[str] = []
        if backdrop and kind == "lower_third":
            backdrop = False
            warnings.append("A lower third sits over the footage, so it has no backdrop.")
        if backdrop and len(job.get("assets") or {}) >= project_media.MAX_ASSETS:
            raise CampaignError(f"A project holds up to {project_media.MAX_ASSETS} audio files and images, so there is no room for a backdrop. Ask without one.", 409)
        plan, root = job["plan"], self.root / ident
        # The card's words match the captions: a translation target names the language Spark writes in.
        translated = translation_target(plan["edit"].get("captions"), request, plan=plan) if request.get("captions") else None
        if translated:
            warnings.append(f"The card is written in {meta.write_language(translated, translated)} to match the translated captions.")
        try:
            suggestion = blender_cards.suggest_card(plan, preview_cues(plan), goal=request["goal"], aspect=request.get("aspect", "9:16"), work_dir=root / "meta",
                                                    kind=kind, language=translated or (request.get("language") or "auto"), backdrop=backdrop,
                                                    budget_usd=request["budget_usd"])
        except (blender_cards.MotionCardError, meta.MetaError) as exc:
            raise CampaignError(str(getattr(exc, "plain", "") or exc), 409) from exc
        card, kept, backdrop_usage = suggestion["card"], None, None
        if backdrop and suggestion.get("backdrop_prompt"):
            canvas = get_preset(_ASPECTS[request.get("aspect", "9:16")]).canvas
            try:
                made = blender_cards.generate_backdrop(suggestion["backdrop_prompt"], width=canvas.width, height=canvas.height, work_dir=root / "meta",
                                                       budget_usd=request["budget_usd"])
                image = self._keep_image(ident, Path(made["path"]), name="Motion card backdrop")
                card = validate_motion_card(dict(card, backdrop=image["id"]), duration=float(plan["duration"]),
                                            assets=dict(job.get("assets") or {}, **{image["id"]: image}))
                kept, backdrop_usage = image, made.get("usage")
            except (blender_cards.MotionCardError, meta.MetaError, CampaignError, EditError) as exc:
                # Spark's card is already paid for; it stands without the plate.
                warnings.append(f"The backdrop was not added: {exc} The card opens over the footage.")
        elif backdrop:
            warnings.append("Muse Spark described no backdrop, so the card opens over the footage.")
        budget = self._usage_summary(root, request["budget_usd"])
        self._update(ident, budget=budget)
        return {"motion_card": card, "proposal": suggestion.get("proposal"), "notes": list(suggestion.get("notes") or []),
                "usage": suggestion.get("usage"), "backdrop_usage": backdrop_usage, "model": suggestion.get("model"),
                "backdrop": kept, "poster": f"/api/campaigns/{ident}/assets?id={kept['id']}" if kept else None,
                "budget": budget, "warnings": warnings}

    def _keep_image(self, ident: str, path: Path, *, name: str) -> dict[str, Any]:
        """Store a generated picture as an image asset of this project, as a generated background is stored."""
        try:
            item = project_media.inspect_asset(path, name=name)
            asset_id = project_media.new_asset_id()
            stored = project_media.store_asset(item, self.root / ident / "assets", asset_id)
        except project_media.MediaProblem as exc:
            raise CampaignError(str(exc)) from exc
        record = project_media.asset_record(item, asset_id)
        with self._lock, self._connect() as connection:
            job = self._load(connection, ident)
            job.update(assets=dict(job.get("assets") or {}, **{asset_id: dict(record, path=str(stored))}), updated_at=_now())
            self._save(connection, job)
        return record

    def _cutout_matte(self, job: dict[str, Any], source: Path, info: Any, plan: dict[str, Any], assembly: dict[str, Any],
                      view: dict[str, Any], warnings: list[str]) -> tuple[Path | None, dict[str, Any] | None]:
        """The matte this render's cutout needs and what it measured, or why there is none.

        Masks come from the proxy tracking reads. The same words on the same
        proxy are the same paid response, so a cutout adds no second SAM call.
        A subject SAM cannot find offers the same recovery as tracking; taking
        it turns tracking off, and this render then goes without the cutout.
        """
        settings, request, ident = plan["edit"]["cutout"], job["request"], job["id"]
        if settings["mode"] == "none" or request["provider"] != "meta":
            return None, None
        from vedit import matte, meta

        if not meta.mask_decoder_ready():
            warnings.append("Cutouts need Meta's mask decoder on this machine. Run: uv sync --extra sam")
            return None, None
        root = self.root / ident
        self._update(ident, stage="cutout", progress=0.14, message="Cutting your subject out of the background.")
        proxy = self._analysis_proxy(root, source, info, assembly, cancel_event=self._event(ident))
        self._check_cancel(ident)
        proxy_info = probe(proxy)
        tracked = request.get("subject") or ""
        subject = tracked if tracked.casefold() == settings["subject"].casefold() else settings["subject"]
        masks = meta.subject_masks(proxy, subject=subject, duration=proxy_info.duration, work_dir=root / "meta", budget_usd=request["budget_usd"])
        warnings.extend(masks.get("warnings", []))
        if not masks["frames"]:
            if request.get("tracking_disabled"):
                warnings.append(f"Meta could not find \u201c{subject}\u201d, so this version has no cutout.")
                return None, None
            self._update(ident, recovery={
                "option": "center_crop",
                "message": f"Meta could not find \u201c{subject}\u201d. Render this version without the cutout, or name the subject more clearly.",
            })
            raise CampaignError("The cutout subject was not found. Name it more clearly, or render without the cutout.")
        area = matte.picture_area(info.square_width, info.display_height, masks["frame_width"], masks["frame_height"])
        frames, cuts = matte.finished_frames(view, masks["fps"])
        key = hashlib.sha256(_json({"masks": Path(masks["raw"]).name, "area": [area.left, area.top, area.width, area.height],
                                    "frames": frames, "cuts": cuts, "filter": matte.MATTE_FILTER}).encode()).hexdigest()[:20]
        path = root / "matte" / f"{key}.mkv"
        measured = path.with_suffix(".json")
        if path.is_file() and measured.is_file():
            return path, json.loads(measured.read_text(encoding="utf-8"))
        stats = matte.build_matte(masks, path=path, area=area, frames=frames, cuts=cuts, cancel_event=self._event(ident))
        _write_json(measured, stats)
        return path, stats

    def _analysis_proxy(self, root: Path, source: Path, info: Any, assembly: dict[str, Any] | None = None, *, cancel_event: threading.Event | None = None,
                        window: tuple[float, float] | None = None) -> Path:
        """Compile a canonical MP4 proxy without moving any editorial timestamps.

        Story planning reads the whole source, or one ``window`` of a long
        source, whose proxy clock starts at the window's start. Tracking reads
        the same assembly as the render (kept pieces, speed, transitions), so its
        time axis is the finished story's time.
        """
        if window is not None:
            assembly = {"op": "select_ranges", "ranges": [{"start": round(float(window[0]), 3), "end": round(float(window[1]), 3)}]}
        identity = {"size": source.stat().st_size, "mtime_ns": source.stat().st_mtime_ns, "assembly": assembly}
        key = hashlib.sha256(_json(identity).encode()).hexdigest()[:20]
        work = root / "analysis" / key
        proxy = work / "analysis.mp4"
        # SAM bills by the frame and a mask can only be as sharp as what SAM saw, so
        # an upright clip is tracked upright, not in the middle third of a wide frame.
        upright = bool(assembly) and window is None and info.display_height > info.square_width
        size, preset = ((540, 960), "web_preview_9x16") if upright else ((960, 540), "web_preview")
        expected_duration = info.duration
        if assembly:
            overlap = (assembly.get("transition") or {}).get("seconds", 0.0)
            expected_duration = output_duration(build_timeline(assembly["ranges"], transition=overlap))
        if expected_duration > _META_TRANSCRIBE_MAX_SECONDS + 0.05:  # a long story plan reads windows of at most this much, never one proxy
            raise CampaignError("Meta analysis is limited to 10 minutes of footage. Shorten or split the source before using Meta tools.")
        if proxy.is_file() and proxy.stat().st_size:
            checked = probe(proxy)
            if checked.has_video and (checked.width, checked.height) == size and abs(checked.fps - 30) < 0.05 and abs(checked.duration - expected_duration) < 0.3:
                return proxy
        self._disk_preflight("analysis", expected_duration, info)
        work.mkdir(parents=True, exist_ok=True)
        # Fit before selecting so the selection compiler fuses downscaling into
        # its small proxy encode instead of writing a full-resolution temporary.
        ops = [{"op": "fit"}]
        if assembly:
            ops.append(assembly)
        ops.append({"op": "export", "preset": preset, "reframe": "fit", "path": str(proxy)})
        brief = Brief(name="analysis", sources={"footage": str(source)}, ops=ops, canvas=Canvas(width=size[0], height=size[1], fps=30), hw=False, work_dir=str(work))
        compiler = compile_brief(brief, work_dir=work)
        (work / "brief.yaml").write_text(brief_dumps(brief), encoding="utf-8")
        write_codegen(compiler.pipeline)
        json.loads((work / "project.json").read_text(encoding="utf-8"))
        (work / "render.sh").read_text(encoding="utf-8")
        reports = execute(compiler.pipeline, progress=False, cancel_event=cancel_event)
        write_report(compiler.pipeline, reports, finals=compiler.finals)
        checked = probe(proxy)
        if not checked.has_video or (checked.width, checked.height) != size or abs(checked.fps - 30) > 0.05 or abs(checked.duration - expected_duration) > 0.3:
            raise CampaignError("The analysis copy did not match the source. Try another video or use Local edit.")
        return proxy

    def _meta_captions(self, root: Path, source: Path, ctx: Any, *, cancel_event: threading.Event) -> Path | None:
        """Caption with Meta, which hears the kept audio at normal speed; spans place what it heard.

        Returns the ASS file, or None when no speech was heard. Provider failures
        raise their own actionable error; captions never silently switch providers.
        """
        from vedit.captions import transcribe_kept

        spans = ctx.view["spans"]
        if cancel_event.is_set():
            raise _Cancelled()
        heard = transcribe_kept(source, spans, ctx.work, language=ctx.request["language"], budget_usd=ctx.request["budget_usd"], meta_dir=root / "meta")
        if cancel_event.is_set():
            raise _Cancelled()
        edits = ctx.plan.get("edit", {}).get("captions", {}).get("edits", {})
        if edits:
            base_plan = dict(ctx.plan)
            base_edit = dict(ctx.plan.get("edit", {}))
            base_captions = dict(base_edit.get("captions", {}))
            base_captions["edits"] = {}
            base_edit["captions"] = base_captions
            base_plan["edit"] = base_edit
            matched_keys = {cue_key(cue) for cue in concat_cues(heard, base_plan, ctx.view)}
            unmatched = [str(key) for key in edits if str(key) not in matched_keys]
            if unmatched:
                ctx.warnings.append(f"{len(unmatched)} caption edit(s) did not match Meta's transcript and were not applied. Check those lines in Captions and edit again.")
        cues = concat_cues(heard, ctx.plan, ctx.view)
        return write_caption_files(cues, ctx) if cues else None

    def _render(self, job: dict[str, Any]) -> None:
        ident, request, plan = job["id"], job["request"], job["plan"]
        root = self.root / ident
        work = root / f"render-{job['revision']}"
        started = time.monotonic()
        self._update(ident, status="rendering", stage="preparing", progress=0.05, message="Preparing your approved edit.", recovery=None)
        source = self._project_source(job)
        info = probe(source)
        if job.get("_pending_note"):
            plan = self._apply_note(ident, job, info.duration)
        plan = validate_plan(plan, info.duration, original=plan, assets=job.get("assets"))
        effect = _effect(request.get("effect"), duration=plan["duration"])
        plan["effect"] = effect
        warnings = list(plan.get("warnings", []))
        picked = self._emphasis(ident, job, plan, warnings)
        preset = _ASPECTS[request["aspect"]]
        delivery = delivery_preset(preset, plan["edit"])
        target = delivery.canvas
        view = plan_timeline(plan)
        duration = view["duration"]
        if request["captions"] and _caption_provider(request) == "meta" and info.has_audio and not _heard_by_meta(plan):
            selected_audio = sum(span.source_end - span.source_start for span in view["spans"])
            if selected_audio > _META_TRANSCRIBE_MAX_SECONDS:
                raise CampaignError("Meta transcription covers up to 10 minutes of selected audio. Shorten the story or use local captions.")
        assembly = assembly_op(view, plan["edit"])
        storage = self._disk_preflight("render", duration, info, effect=effect is not None and effect["intensity"] > 0)
        self._update(ident, storage=storage)
        self._check_cancel(ident)
        # An interrupted attempt leaves partial files and the compiler reads what it
        # finds. This revision was never verified, so its folder holds nothing to keep.
        if work.is_dir() and job.get("verified_revision") != job["revision"] and str(job["revision"]) not in (job.get("_verified_artifacts") or {}):
            shutil.rmtree(work)
        work.mkdir(exist_ok=True)
        subject_track: list[dict[str, Any]] | None = None
        if request["provider"] == "meta" and request["subject"] and not request.get("tracking_disabled"):
            from vedit import meta

            self._update(ident, stage="tracking", progress=0.12, message="Following your chosen subject through the footage.")
            cache_key = hashlib.sha256(_json(assembly).encode()).hexdigest()[:20]
            cache = root / f"tracking-{cache_key}.json"
            if cache.is_file():
                tracking = json.loads(cache.read_text(encoding="utf-8"))
            else:
                proxy = self._analysis_proxy(root, source, info, assembly, cancel_event=self._event(ident))
                self._check_cancel(ident)
                proxy_info = probe(proxy)
                tracking = meta.track_subject(proxy, subject=request["subject"], duration=proxy_info.duration, work_dir=root / "meta", budget_usd=request["budget_usd"])
                if tracking.get("points"):
                    tracking["points"] = remap_proxy_points(tracking["points"], source_width=getattr(info, "square_width", info.width), source_height=getattr(info, "display_height", info.height), proxy_width=proxy_info.width, proxy_height=proxy_info.height, duration=duration)
                _write_json(cache, tracking)
            if not tracking.get("points"):
                # Persist a user-chosen way forward before failing; the job
                # record, source, and approved plan all stay intact.
                self._update(ident, recovery={
                    "option": "center_crop",
                    "message": f"Meta could not find \u201c{request['subject']}\u201d. Render this version with a center crop instead, or create a new plan with a clearer subject.",
                })
                raise CampaignError("The chosen subject was not found. Create a new plan with a clearer subject description or leave tracking off.")
            subject_track = list(tracking["points"])
            warnings.extend(tracking.get("warnings", []))
        elif request["provider"] == "meta" and request["subject"] and request.get("tracking_disabled"):
            warnings.append("Subject tracking was turned off for this version; a center crop was used.")
        matte_path, cutout_stats = self._cutout_matte(job, source, info, plan, assembly, view, warnings)
        hero_path, hero_stats = self._hero_matte(job, source, plan, float(target.fps or standard_rate(info.fps if info.has_video else None)), warnings)
        # The brief is read again against the approved edit, so the receipts
        # say what this render really does, not what was first asked.
        edits = brief_edit(request["goal"], plan=plan, has_audio=info.has_audio, final=plan["edit"])
        plan["decisions"], plan["ignored_edits"] = edits["decisions"], edits["ignored"]
        if plan["edit"].get("stabilize"):
            from vedit.motion import STABILIZE_SHAKE_WARNING, measure_camera_shake

            measured_shake = measure_camera_shake(source)
            if measured_shake is None:
                warnings.append("Camera shake could not be measured, so stabilization may add crop or drift to steady footage.")
            elif measured_shake < STABILIZE_SHAKE_WARNING:
                warnings.append(f"Measured camera shake is low ({measured_shake:.2f} px at 160x90); stabilization may add crop without a clear benefit.")
        # A project of photos or silent clips plays a silent track; nobody recorded sound to caption.
        # A script video is caption-led: its captions come from the script's own text, not from listening.
        recorded = info.has_audio and (bool((job.get("source") or {}).get("has_audio", True)) or bool(plan.get("script")))
        caption_provider = _caption_provider(request) if request["captions"] and recorded else None
        ctx = render_context(plan, request=request, aspect=request["aspect"], canvas=target, work=work, has_audio=info.has_audio, preset=preset, assets=job.get("assets") or {},
                             tracking=subject_track, matte=matte_path, hero_matte=hero_path, effect=effect)
        if caption_provider == "meta" and _heard_by_meta(plan):
            # Meta heard the whole source at planning; these captions are cut from that transcript at no cost.
            self._update(ident, stage="transcribing", progress=0.16, message="Writing the captions from Meta's transcript.")
            ctx.subtitle = build_caption_file(ctx.plan, ctx.view, ctx)
            warnings = [warning for warning in warnings if not warning.startswith("Captions are unavailable")]
            if ctx.subtitle is None:
                warnings.append("Meta's transcript has no speech in these moments. This video has no captions.")
        elif caption_provider == "meta":
            self._update(ident, stage="transcribing", progress=0.16, message="Asking Meta to transcribe the approved speech.")
            ctx.subtitle = self._meta_captions(root, source, ctx, cancel_event=self._event(ident))
            warnings = [warning for warning in warnings if not warning.startswith("Captions are unavailable")]
            if ctx.subtitle is None:
                # Silence is never sent, so this is either Meta finding no speech or a silent cut that was not sent.
                warnings.append("No speech was found in the kept audio. This video has no captions.")
        elif caption_provider == "local":
            self._update(ident, stage="transcribing", progress=0.16, message="Writing the captions for this edit.")
            if _listened(plan):
                # The plan's own words: a script, or a transcript Meta already wrote. Nothing listens here.
                ctx.subtitle = build_caption_file(ctx.plan, ctx.view, ctx)
                warnings = [warning for warning in warnings if not warning.startswith("Captions are unavailable")]
                if ctx.subtitle is None:
                    warnings.append("No speech was heard in these moments. This video has no captions.")
            elif not any(warning.startswith("Captions are unavailable") for warning in warnings):
                warnings.append(CAPTIONS_NEED_META + " This video has no captions.")
        ops = build_ops(ctx)
        warnings.extend(ctx.warnings)
        brief = Brief(name="campaign", sources=brief_sources(ctx, source), ops=ops, canvas=target, work_dir=str(work))
        (work / "brief.yaml").write_text(brief_dumps(brief), encoding="utf-8")
        compiler = compile_brief(brief, work_dir=work)
        warnings.extend(message for message in compiler.warnings if message not in warnings)
        write_codegen(compiler.pipeline)
        # Verify that the persisted program is readable before any long encode.
        json.loads((work / "project.json").read_text(encoding="utf-8"))
        if not (work / "render.sh").read_text(encoding="utf-8").startswith("#!/usr/bin/env bash"):
            raise CampaignError("The render program could not be saved. Check free disk space and retry.")
        self._check_cancel(ident)
        reports = []
        steps = compiler.pipeline.steps
        for index, step in enumerate(steps):
            self._check_cancel(ident)
            self._update(ident, stage="encoding", progress=0.2 + 0.55 * index / max(1, len(steps)), message=f"Step {index + 1} of {len(steps)}: {step.description}")
            # Each execution directory keeps its own trace without overwriting
            # the complete project.json which a person approved above.
            piece = Pipeline(name=compiler.pipeline.name, work_dir=work / "execution" / f"{index + 1:02d}", steps=[step])
            reports.extend(execute(piece, progress=False, cancel_event=self._event(ident)))
        self._check_cancel(ident)
        self._update(ident, stage="verifying", progress=0.82, message="Checking the actual exported video.")
        report_path = write_report(compiler.pipeline, reports, finals=compiler.finals)
        video = compiler.finals[0]
        output = probe(video)
        checkable = verification(delivery)
        problem = export_problem(output, width=target.width, height=target.height, has_audio=has_sound(ctx) and checkable["audio"], seconds=duration,
                                 tolerance=max(0.3, len(plan["segments"]) / 15), color=checkable["color"])
        if problem:
            raise CampaignError(problem)
        stream = output.video
        proof = {"duration": output.duration, "width": output.width, "height": output.height, "fps": output.fps, "has_audio": output.has_audio, "has_video": output.has_video, "color_space": stream.color_space if stream else None, "color_range": stream.color_range if stream else None, "color_primaries": stream.color_primaries if stream else None, "color_transfer": stream.color_trc if stream else None, "size_bytes": output.size_bytes}
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["proof"] = proof
        report["warnings"] = warnings
        report["approved_plan"] = plan
        report["edit"] = plan["edit"]
        report["timeline"] = plan["timeline"]
        report["effect"] = effect
        review: dict[str, Any] | None = None
        if request.get("review"):
            # Director's notes never change the verified file; a failed review is a warning.
            review = self._review_cut(ident, job, work, video, plan, output.duration, warnings)
            report["review"] = review
        _write_json(report_path, report)
        cover = work / "cover.jpg"
        cover_kind = "frame"
        if request["generate_cover"]:
            from vedit import meta

            self._update(ident, stage="cover", progress=0.88, message="Creating the requested campaign cover.")
            cover_cache = root / f"cover-{job['revision']}.json"
            if cover_cache.is_file():
                generated = json.loads(cover_cache.read_text(encoding="utf-8"))
            else:
                generated = meta.generate_cover(cover, prompt=plan.get("cover_prompt") or plan["title"], work_dir=root / "meta", budget_usd=request["budget_usd"])
                _write_json(cover_cache, generated)
            generated_path = Path(generated["path"]).resolve()
            try:
                generated_path.relative_to((root / "meta").resolve())
            except ValueError as exc:
                raise CampaignError("The cover could not be saved. Retry the render.") from exc
            cover = work / ("generated-cover" + generated_path.suffix.lower())
            shutil.copyfile(generated_path, cover)
            cover_kind = "generated"
            warnings.extend(generated.get("warnings", []))
        self._check_cancel(ident)
        self._update(ident, stage="packaging", progress=0.95, message="Packing your video, cover, copy, and verified edit.")
        report["warnings"] = warnings
        report["budget"] = self._usage_summary(root, request["budget_usd"])
        tracked_subject = self._tracked_subject(request)
        report["tracked_subject"] = tracked_subject
        report["caption_provider"] = caption_provider
        # Deterministic grading of the render, then the director's structured
        # checks and patches. Both are display data until a revision applies them.
        report["cutout"] = cutout_stats
        report["hero_word"] = {"edit": plan["edit"].get("hero_word"), "matte": hero_stats} if plan["edit"].get("hero_word") else None
        emphasis_drawn = self._emphasis_result(plan, picked if picked is not None else job.get("_emphasis_picks"), request)
        report["emphasis"] = emphasis_drawn
        grade = self._grade(plan, request, info.duration, work, review, edits=edits, tracking=subject_track, cutout=cutout_stats,
                            hero=self._hero_spoken(plan, job.get("_hero_spoken")), hero_stats=hero_stats,
                            cut_duration=output.duration, source_size=(getattr(info, "display_width", info.width) or info.width, getattr(info, "display_height", info.height) or info.height),
                            clips=(job.get("source") or {}).get("clips"))
        report.update(checks=grade["checks"], passed=grade["passed"], total=grade["total"], patches=grade["patches"])
        _write_json(report_path, report)
        copy = plan["caption"] + ("\n\n" + " ".join(tag if tag.startswith("#") else "#" + tag for tag in plan["hashtags"]) if plan["hashtags"] else "")
        (work / "post.txt").write_text(copy + "\n", encoding="utf-8")
        _write_json(work / "story.json", plan)
        packed = [(video, "video" + video.suffix.lower()), (cover, "cover" + cover.suffix.lower()), (work / "post.txt", "post.txt"), (work / "story.json", "story.json"), (work / "brief.yaml", "brief.yaml"), (report_path, "report.json")]
        packed += [(work / name, name) for name in ("captions.srt", "captions.ass", *sorted(path.name for path in work.glob("captions.*.srt"))) if (work / name).is_file()]
        # Tamper-evident receipts: every check, the probe and the Meta calls get an evidence id,
        # and one root id covers them and the video's sha256. `vedit verify` re-hashes all of it.
        from vedit.evidence import RECEIPTS, bundle_receipts, write_receipts

        receipts: dict[str, Any] = {"evidence_id": None}
        try:
            receipts = bundle_receipts(dict(grade, proof=proof, revision=job["revision"], caption_provider=caption_provider, applied_patches=job.get("applied_patches") or []),
                                       video, plan=plan, project_root=root, base=work, info=output, files=packed[1:], video_name=packed[0][1], note=job.get("note"))
            write_receipts(receipts, work / RECEIPTS)
            packed.append((work / RECEIPTS, RECEIPTS))
        except (OSError, TypeError, ValueError) as exc:
            # The file already passed its probe; a receipt that cannot be sealed is said out loud, not faked.
            warnings.append(f"The tamper-evident receipts could not be written ({type(exc).__name__}). The video and report are still verified.")
        bundle = work / "campaign.zip"
        pending_bundle = work / "campaign.zip.part"
        with zipfile.ZipFile(pending_bundle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for file, name in packed:
                self._check_cancel(ident)
                archive.write(file, name, compress_type=zipfile.ZIP_STORED if file.suffix.lower() in {".mp4", ".mov", ".webm", ".gif", ".jpg", ".png", ".webp"} else zipfile.ZIP_DEFLATED)
        pending_bundle.replace(bundle)
        artifacts = {"video": str(video.relative_to(root)), "cover": str(cover.relative_to(root)), "bundle": str(bundle.relative_to(root)), "brief": str((work / "brief.yaml").relative_to(root)), "report": str(report_path.relative_to(root))}
        result = {f"{name}_url": self._url(ident, name, revision=job["revision"]) for name in artifacts}
        result.update(proof=proof, warnings=warnings, cover_kind=cover_kind, caption=plan["caption"], hashtags=plan["hashtags"], provider=request["provider"], revision=job["revision"], stale=False, effect=effect, tracked_subject=tracked_subject, caption_provider=caption_provider, review=review)
        result.update(emphasis=emphasis_drawn, hero_word=plan["edit"].get("hero_word"))
        result.update(checks=grade["checks"], passed=grade["passed"], total=grade["total"], patches=grade["patches"])
        result["evidence_id"] = receipts["evidence_id"]
        self._check_cancel(ident)
        timings = dict(job.get("timings") or {}, render_seconds=round(time.monotonic() - started, 3), render_revision=job["revision"])
        notes = review if isinstance(review, dict) and "error" not in review else {}
        version = {
            "revision": job["revision"], "status": "verified", "created_at": _now(),
            "source": job.get("revision_source") or "approved", "proof": proof,
            "checks": grade["checks"], "passed": grade["passed"], "total": grade["total"],
            "hook_score": notes.get("hook_score"), "verdict": notes.get("verdict"),
            "applied_patches": job.get("applied_patches") or [], "note": job.get("note"),
            "timings": {"render_seconds": timings["render_seconds"]}, "evidence_id": receipts["evidence_id"],
        }
        version.update({f"{name}_url": self._url(ident, name, revision=job["revision"]) for name in ("video", "report", "cover")})
        versions = [entry for entry in (job.get("versions") or []) if isinstance(entry, dict) and entry.get("revision") != job["revision"]] + [version]
        self._update(ident, status="complete", stage="complete", progress=1, message="Your campaign is ready. The video passed export verification.", result=result, verified_revision=job["revision"], error=None, _artifacts=artifacts, timings=timings, versions=versions)

    @staticmethod
    def _grade(plan: dict[str, Any], request: dict[str, Any], duration: float, work: Path, review: dict[str, Any] | None,
               *, edits: dict[str, Any] | None = None, tracking: list[dict[str, Any]] | None = None, cutout: dict[str, Any] | None = None,
               hero: dict[str, Any] | None = None, hero_stats: dict[str, Any] | None = None,
               cut_duration: float | None = None, source_size: tuple[float, float] | None = None,
               clips: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Lint the approved plan against the source analysis and merge the director's checks.

        Sentences and speech bands come from the plan's stored analysis when a
        planner measured them; captions are read back from the SRT the render
        actually burned. A check with nothing to measure is left out, not passed.
        ``clips`` are the project's clip ranges, so a moment that crosses one is caught.

        Every check says how it knows (``basis``) and what produced it (``by``).
        Code measured the lint, the requested edits and SAM's boxes (``derived``);
        the director's checks are Muse Spark's or Muse Wonder's judgment (``inferred``). ``inputs``
        names the models whose data a measurement read, only when the plan says so.
        """
        analysis = plan.get("analysis") if isinstance(plan.get("analysis"), dict) else {}
        sentences = [item for item in (analysis.get("sentences") or []) if isinstance(item, dict)]
        if not sentences and analysis.get("transcript"):
            from vedit.story import sentences_from_transcript

            sentences = sentences_from_transcript([item for item in analysis["transcript"] if isinstance(item, dict)], duration=duration)
        speech = [item for item in (analysis.get("speech") or []) if isinstance(item, dict)] or None
        captions = None
        srt = work / "captions.srt"
        if request.get("captions"):
            # Requested and none burned is a cut with no captions, not a check that was never asked for.
            captions = []
            if srt.is_file():
                from vedit.captions import read_srt

                try:
                    captions = [{"start": cue.start, "end": cue.end} for cue in read_srt(srt)]
                except (OSError, ValueError):
                    captions = []
        # With no transcript these bands are only energy, and a music bed would read as talk nobody captioned.
        # A transcript that was made and holds no speech is still a measurement: there was nothing to caption.
        transcribed = bool(analysis.get("transcript")) or (bool(analysis.get("transcribed")) and CampaignManager._heard_by(analysis) is not None)
        heard = (speech or []) if captions is not None and transcribed else None
        heard_by = CampaignManager._heard_by(analysis)
        captioned_by = None
        if captions is not None:
            provider = _caption_provider(request)
            if provider == "meta":
                captioned_by = "meta-voice-transcribe"
            elif provider == "local":
                # The caption check needs the plan's transcript, and local captions are cut from it.
                captioned_by = heard_by
        graded = lint_plan(plan, target_duration=request["target_duration"], duration=duration, sentences=sentences or None, captions=captions, speech=heard,
                           heard_by=heard_by, captioned_by=captioned_by, clips=clips)
        checks = list(graded["checks"])
        patches = list(graded["patches"])
        applied = list((edits or {}).get("decisions") or [])
        ignored = list((edits or {}).get("ignored") or [])
        if applied or ignored:
            note = ("Applied: " + "; ".join(applied) + "." if applied else "Nothing from the brief was applied.")
            if ignored:
                note += " Not applied: " + "; ".join(ignored) + "."
        else:
            note = "The brief asked for no specific edits."
        checks.append({"name": "auto: requested edits applied", "passed": not ignored, "at": None, "note": note, "basis": "derived", "by": "brief-parser"})
        if tracking:
            # SAM's boxes are evidence about the delivered cut; grade them like any other claim.
            width, height = source_size or (16, 9)
            checks.append(subject_in_frame_check(tracking, duration=cut_duration or float(plan["duration"]), aspect=request.get("aspect", "9:16"), source_width=width, source_height=height))
        if cutout:
            checks.append(subject_cutout_check(cutout))
        if hero:
            # Code measured it from Spark's word and SAM's matte: derived, by lint, reading both models.
            checks.append(hero_word_check(hero, hero_stats, duration=float(plan["duration"])))
        if isinstance(review, dict) and "error" not in review:
            # Muse Spark or Muse Wonder reviewed, as the review says. A review from anywhere else keeps its own name.
            director = str(review.get("director") or "muse-spark") if review.get("provider", "meta") == "meta" else str(review.get("provider"))
            for check in review.get("checks") or []:
                if isinstance(check, dict):
                    checks.append({"name": "director: " + str(check.get("name", "")), "passed": bool(check.get("passed")), "at": check.get("at"), "note": str(check.get("note", "")),
                                   "basis": "inferred", "by": director})
            for patch in review.get("patches") or []:
                if isinstance(patch, dict):
                    patches.append(dict(patch, source="director"))
        patches = [dict(patch, summary=describe_patch(patch)) for patch in patches]
        # An advisory note is shown with the checks but is not a check: it never counts as a pass.
        counted = [check for check in checks if not check.get("advisory")]
        return {"checks": checks, "passed": sum(1 for check in counted if check["passed"]), "total": len(counted), "patches": patches}

    @staticmethod
    def _heard_by(analysis: dict[str, Any]) -> str | None:
        """The model that wrote the plan's transcript, or None when no model did.

        Meta Voice Transcribe is the only transcriber, and a plan it heard says so
        in ``transcribed_by``. A script's words are the writer's own. A plan saved
        before Briefcut went Meta-only names nobody; its transcript, if any, was
        written by local Whisper, so that is what the receipt says.
        """
        if analysis.get("transcribed_by") == TRANSCRIBER and analysis.get("transcribed"):
            return TRANSCRIBER
        method = analysis.get("method")
        if method == "script":
            return "local-whisper" if analysis.get("transcribed") else None
        if method in ("local-analysis", "meta"):
            return "local-whisper" if analysis.get("transcribed") or analysis.get("transcript") or analysis.get("words") else None
        return None

    def close(self, *, wait: bool = True) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                with self._connect() as connection:
                    rows = connection.execute("SELECT data FROM jobs WHERE status IN ('queued','analyzing','rendering')").fetchall()
                    for row in rows:
                        job = json.loads(row[0])
                        self._event(job["id"]).set()
                        changes = {"cancel_requested": True, "updated_at": _now(), "message": "Studio is stopping. Your clip, plan, and verified version are saved."}
                        job.update(changes)
                        self._save(connection, job)
        if self._worker is not None and self._worker is not threading.current_thread():
            if wait:
                self._worker.join(timeout=60)
        elif self._worker is None:
            if wait:
                self._idle.wait(timeout=60)
            if self._idle.is_set():
                self._release_lease()

    def _release_lease(self) -> None:
        with self._lock:
            if self._active_processes:
                return
            if self._worker is not None and self._worker.is_alive() and threading.current_thread() is not self._worker:
                return
            if not self._lease.closed:
                fcntl.flock(self._lease.fileno(), fcntl.LOCK_UN)
                self._lease.close()


# Short alias is convenient for native callers and integration tests.
Manager = CampaignManager
_MANAGERS: dict[Path, CampaignManager] = {}
_MANAGERS_LOCK = threading.Lock()


def get_manager() -> CampaignManager:
    workspace = Path.cwd().resolve()
    with _MANAGERS_LOCK:
        manager = _MANAGERS.get(workspace)
        if manager is None or manager._closed:
            manager = CampaignManager(workspace)
            _MANAGERS[workspace] = manager
        return manager
