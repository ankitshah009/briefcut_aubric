"""Turn a validated plan into compile ops, always in the same order.

The order is the contract in ``docs/EDITOR_ARCHITECTURE.md``. Each stage is one
small function that reads ``ctx.edit`` and returns the ops it needs, so a stage
can change without touching its neighbours. ``select_ranges`` decides the clock;
every stage after it works on the output clock and never changes timing.

A stage only produces data. The compiler is still the only thing that turns an
op into a command, and it says so in a plain warning when this machine's FFmpeg
can only do part of what an op asks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from vedit.edit_spec import asset_ids, default_edit
from vedit.ir import Canvas
from vedit.looks import is_neutral
from vedit.motion import zoom_moves
from vedit.presets import ExportPreset, export_preset_name, get_preset
from vedit.timeline import plan_timeline, segment_speed

FOOTAGE = "footage"


@dataclass
class RenderContext:
    """Everything a stage may read. Stages only write to ``warnings``."""

    plan: dict[str, Any]
    view: dict[str, Any]
    edit: dict[str, Any]
    request: dict[str, Any]
    aspect: str
    canvas: Canvas
    work: Path
    has_audio: bool
    preset: str = "shorts_9x16"
    assets: dict[str, Any] = field(default_factory=dict)
    tracking: list[dict[str, Any]] | None = None
    matte: Path | None = None
    hero_matte: Path | None = None  # the subject over the hero word's window only, from ``vedit.hero_word.window_matte``
    subtitle: Path | None = None
    effect: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)


def context(plan: dict[str, Any], *, request: dict[str, Any], aspect: str, canvas: Canvas, work: Path, has_audio: bool, **extra: Any) -> RenderContext:
    """Build a context from a plan, resolving the clock once."""
    edit = plan.get("edit") if isinstance(plan.get("edit"), dict) else default_edit()
    return RenderContext(plan=plan, view=plan_timeline(plan), edit=edit, request=request, aspect=aspect,
                         canvas=canvas, work=work, has_audio=has_audio, **extra)


def _asset_file(ctx: RenderContext, ident: Any) -> str | None:
    record = ctx.assets.get(ident) if isinstance(ident, str) else None
    path = record.get("path") if isinstance(record, dict) else None
    return path if isinstance(path, str) and Path(path).is_file() else None


def asset_path(ctx: RenderContext, ident: Any, what: str) -> str | None:
    """A brief source name for an asset, or None with a warning when its file is gone."""
    if _asset_file(ctx, ident):
        return ident
    ctx.warnings.append(f"{what} could not be found, so this version leaves it out.")
    return None


def brief_sources(ctx: RenderContext, source: Path) -> dict[str, str]:
    """Every media file the ops name, so ``brief.yaml`` stays a complete record."""
    sources = {FOOTAGE: str(source)}
    for ident in asset_ids(ctx.edit):
        path = _asset_file(ctx, ident)
        if path:
            sources[ident] = path
    return sources


def _wants_cutout(ctx: RenderContext) -> bool:
    return ctx.edit["cutout"]["mode"] != "none" and ctx.matte is not None


def _wants_hero(ctx: RenderContext) -> bool:
    return bool(ctx.edit.get("hero_word")) and ctx.hero_matte is not None


def assembly_op(view: dict[str, Any], edit: dict[str, Any]) -> dict[str, Any]:
    """The one ``select_ranges`` a plan means. Renders and tracking proxies share it."""
    mute = bool(edit.get("audio", {}).get("mute_source"))
    ranges: list[dict[str, Any]] = []
    for segment in view["segments"]:
        item: dict[str, Any] = {"start": segment["start"], "end": segment["end"]}
        if segment.get("keep"):
            item["keep"] = segment["keep"]
        speed = segment_speed(segment)
        if speed != 1:
            item["speed"] = speed
        volume = 0.0 if mute else segment.get("volume")
        if volume is not None and volume != 1:
            item["volume"] = volume
        ranges.append(item)
    op: dict[str, Any] = {"op": "select_ranges", "ranges": ranges}
    transition = edit.get("transition") or {}
    if view["transition_seconds"] > 0 and transition.get("type") not in (None, "none"):
        op["transition"] = {"type": transition["type"], "seconds": view["transition_seconds"]}
    return op


def reframe_center(ctx: RenderContext) -> list[dict[str, Any]]:
    """Center framing fuses into assembly unless a later stage needs source geometry."""
    if ctx.tracking or _wants_cutout(ctx) or _wants_hero(ctx):
        return []
    return [{"op": "reframe", "aspect": ctx.aspect, "mode": "fill"}]


def assembly(ctx: RenderContext) -> list[dict[str, Any]]:
    return [assembly_op(ctx.view, ctx.edit)]


def cutout(ctx: RenderContext) -> list[dict[str, Any]]:
    settings = ctx.edit["cutout"]
    if settings["mode"] == "none":
        return []
    if ctx.matte is None:
        ctx.warnings.append("The cutout needs a subject mask, which this version does not have, so the background is unchanged.")
        return []
    op = {"op": "cutout", "matte": str(ctx.matte), "mode": settings["mode"], "color": settings["color"], "blur": settings["blur"]}
    if settings["mode"] == "image":
        image = asset_path(ctx, settings["asset"], "The cutout background image")
        if image is None:
            return []
        op["image"] = image
    return [op]


def _hero_focus(ctx: RenderContext, start: float, end: float) -> list[float] | None:
    """Where a tracked crop is centred during the window, so the word is laid out inside what stays on screen."""
    boxes = [point for point in ctx.tracking or [] if isinstance(point, dict) and start - 0.5 <= float(point.get("time", -1)) <= end + 0.5]
    if not boxes:
        return None
    x = sum(float(box["x"]) + float(box["width"]) / 2 for box in boxes) / len(boxes)
    y = sum(float(box["y"]) + float(box["height"]) / 2 for box in boxes) / len(boxes)
    return [round(min(1.0, max(0.0, x)), 4), round(min(1.0, max(0.0, y)), 4)]


def hero_word(ctx: RenderContext) -> list[dict[str, Any]]:
    """The hero word, on the assembled picture before any reframe, so its matte lands on the source geometry."""
    settings = ctx.edit.get("hero_word")
    if not settings:
        return []
    if ctx.hero_matte is None:
        ctx.warnings.append("The hero word needs a subject mask for its moment, which this version does not have, so it is left out.")
        return []
    start, end = settings["start"], settings["end"]
    inside = [span for span in ctx.view["spans"] if span.output_start - 1e-3 <= start and end <= span.output_end + 1e-3]
    overlap = ctx.view["transition_seconds"]
    if not inside or (overlap > 0 and any(span is not inside[0] and span.output_start < end and start < span.output_end for span in ctx.view["spans"])):
        ctx.warnings.append("The hero word's moment no longer sits inside one shot of this cut, so it is left out. Pick it again.")
        return []
    op = {"op": "hero_word", "matte": str(ctx.hero_matte), "word": settings["word"], "start": start, "end": end,
          "side": settings["side"], "font": settings["font"], "color": settings["color"], "aspect": ctx.aspect}
    focus = _hero_focus(ctx, start, end)
    if focus is not None:
        op["focus"] = focus
    return [op]


def reframe_subject(ctx: RenderContext) -> list[dict[str, Any]]:
    """Framing that must see the assembled picture: a tracked subject, or a cutout's result."""
    if ctx.tracking:
        return [{"op": "track_reframe", "aspect": ctx.aspect, "points": ctx.tracking}]
    if _wants_cutout(ctx) or _wants_hero(ctx):
        return [{"op": "reframe", "aspect": ctx.aspect, "mode": "fill"}]
    return []


def stabilize(ctx: RenderContext) -> list[dict[str, Any]]:
    return [{"op": "stabilize"}] if ctx.edit["stabilize"] else []


def denoise(ctx: RenderContext) -> list[dict[str, Any]]:
    return [{"op": "denoise", "strength": 3}] if ctx.edit["denoise_picture"] else []


def zoom(ctx: RenderContext) -> list[dict[str, Any]]:
    """Per-segment moves on the output clock: explicit ``motion`` first, then the zoom mode."""
    moves = zoom_moves(ctx.view, ctx.edit["zoom"])
    return [{"op": "zoom", "moves": moves, "blend": ctx.view["transition_seconds"]}] if moves else []


def spatial_effect(ctx: RenderContext) -> list[dict[str, Any]]:
    return [{"op": "spatial_effect", **ctx.effect}] if ctx.effect is not None else []


def look(ctx: RenderContext) -> list[dict[str, Any]]:
    settings = ctx.edit["look"]
    return [] if is_neutral(settings) else [{"op": "look", **settings}]


def voice(ctx: RenderContext) -> list[dict[str, Any]]:
    audio = ctx.edit["audio"]
    if not ctx.has_audio or audio["mute_source"] or not (audio["enhance_voice"] or audio["denoise"]):
        return []
    return [{"op": "voice", "enhance": audio["enhance_voice"], "denoise": audio["denoise"]}]


def texts(ctx: RenderContext) -> list[dict[str, Any]]:
    return [{"op": "text", **item} for item in ctx.edit["texts"]]


def logo(ctx: RenderContext) -> list[dict[str, Any]]:
    settings = ctx.edit["logo"]
    if not settings:
        return []
    source = asset_path(ctx, settings["asset"], "The logo")
    if source is None:
        return []
    return [{"op": "logo", "source": source, "position": settings["position"], "scale": settings["scale"], "opacity": settings["opacity"]}]


def overlays(ctx: RenderContext) -> list[dict[str, Any]]:
    ops: list[dict[str, Any]] = []
    for item in ctx.edit["overlays"]:
        source = asset_path(ctx, item["asset"], "An overlay image")
        if source is not None:
            ops.append({"op": "overlay", "source": source, "start": item["start"], "duration": item["duration"],
                        "position": item["position"], "scale": item["scale"]})
    return ops


def captions(ctx: RenderContext) -> list[dict[str, Any]]:
    if ctx.subtitle is not None:
        return [{"op": "captions", "file": str(ctx.subtitle), "burn": True, "style": "boxed"}]
    return []


def fades(ctx: RenderContext) -> list[dict[str, Any]]:
    fade = ctx.edit["fade"]
    ops: list[dict[str, Any]] = []
    if fade["in"] > 0:
        ops.append({"op": "fade_in", "duration": fade["in"]})
    if fade["out"] > 0:
        ops.append({"op": "fade_out", "duration": fade["out"]})
    return ops


def music(ctx: RenderContext) -> list[dict[str, Any]]:
    settings = ctx.edit["music"]
    if not settings:
        return []
    source = asset_path(ctx, settings["asset"], "The music track")
    if source is None:
        return []
    return [{"op": "music", "source": source, "volume_db": settings["volume_db"], "duck": settings["duck"], "fade_out": settings["fade_out"],
             "start": settings["start"], "beat_sync": settings["beat_sync"], "mute_source": ctx.edit["audio"]["mute_source"]}]


def has_sound(ctx: RenderContext) -> bool:
    """True when the finished video will carry sound: its own, or a music bed that really exists."""
    settings = ctx.edit["music"]
    return bool(ctx.has_audio or (settings and _asset_file(ctx, settings["asset"])))


def loudness(ctx: RenderContext) -> list[dict[str, Any]]:
    audio = ctx.edit["audio"]
    if audio["keep_loudness"] or not has_sound(ctx) or (audio["mute_source"] and not ctx.edit["music"]):
        return []
    return [{"op": "loudnorm", "i": -16}]


def thumbnail(ctx: RenderContext) -> list[dict[str, Any]]:
    return [{"op": "thumbnail", "at": min(0.5, ctx.view["duration"] / 3), "path": str(ctx.work / "cover.jpg")}]


def export_choices(edit: dict[str, Any]) -> dict[str, Any]:
    """Only what differs from the defaults, so an untouched edit keeps its preset's behaviour."""
    chosen, defaults = edit["export"], default_edit()["export"]
    return {key: value for key, value in chosen.items() if value != defaults[key]}


def delivery_preset(preset: str, edit: dict[str, Any]) -> ExportPreset:
    """What this edit really delivers: the aspect's preset at the chosen quality, rate, and format."""
    return get_preset(export_preset_name(preset, **export_choices(edit)))


def export(ctx: RenderContext) -> list[dict[str, Any]]:
    container = delivery_preset(ctx.preset, ctx.edit).container
    return [{"op": "export", "preset": ctx.preset, "path": str(ctx.work / f"video.{container}"), **export_choices(ctx.edit)}]


STAGES: tuple[Callable[[RenderContext], list[dict[str, Any]]], ...] = (
    reframe_center, assembly, cutout, hero_word, reframe_subject, stabilize, denoise, zoom, spatial_effect,
    look, voice, texts, logo, overlays, captions, fades, music, loudness, thumbnail, export,
)


def build_ops(ctx: RenderContext) -> list[dict[str, Any]]:
    """The complete compile program for one render."""
    ops: list[dict[str, Any]] = []
    for stage in STAGES:
        ops.extend(stage(ctx))
    return ops
