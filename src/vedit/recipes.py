"""Named recipes for the jobs people actually run every day."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from vedit.errors import BriefError
from vedit.ir import Brief, Canvas

Recipe = dict[str, Any]


def _base(name: str, sources: dict[str, str], ops: list[dict[str, Any]], **kwargs: Any) -> Brief:
    return Brief(name=name, sources=sources, ops=ops, canvas=kwargs.get("canvas", Canvas()), presets=kwargs.get("presets", []), hw=kwargs.get("hw", False))


def recipe_podcast(talk: str, *, music: str | None = None, name: str | None = None) -> Brief:
    sources = {"talk": str(Path(talk).expanduser().resolve())}
    ops: list[dict[str, Any]] = [
        {"op": "cut_silence", "threshold_db": -34, "min_silence": 0.5, "pad": 0.14},
        {"op": "highpass", "hz": 80},
        {"op": "captions", "burn": True, "style": "lower-third"},
    ]
    if music:
        sources["music"] = str(Path(music).expanduser().resolve())
        ops.append({"op": "music", "source": "music", "duck": True, "volume_db": -18})
    ops += [
        {"op": "loudnorm", "i": -16, "tp": -1.5, "lra": 11},
        {"op": "fade_in", "duration": 0.25},
        {"op": "fade_out", "duration": 0.6},
        {"op": "thumbnail", "at": 2.0},
        {"op": "export", "presets": ["podcast_video", "podcast_audio"]},
    ]
    return _base(name or Path(talk).stem, sources, ops, presets=["podcast_video"])


def recipe_youtube(talk: str, *, title: str | None = None, name: str | None = None) -> Brief:
    sources = {"main": str(Path(talk).expanduser().resolve())}
    ops: list[dict[str, Any]] = [
        {"op": "cut_silence"},
        {"op": "captions", "burn": True},
    ]
    if title:
        ops.insert(0, {"op": "title", "text": title})
    ops += [
        {"op": "loudnorm", "i": -14},
        {"op": "fade_in"},
        {"op": "fade_out"},
        {"op": "thumbnail", "at": 1.5},
        {"op": "export", "presets": ["youtube_1080p"]},
    ]
    return _base(name or Path(talk).stem, sources, ops, presets=["youtube_1080p"])


def recipe_shorts(talk: str, *, name: str | None = None) -> Brief:
    sources = {"main": str(Path(talk).expanduser().resolve())}
    ops = [
        {"op": "cut_silence", "min_silence": 0.35, "pad": 0.08},
        {"op": "reframe", "aspect": "9:16", "mode": "fill"},
        {"op": "captions", "burn": True, "style": "boxed"},
        {"op": "loudnorm", "i": -13},
        {"op": "export", "presets": ["shorts_9x16"]},
    ]
    return _base(name or f"{Path(talk).stem}_shorts", sources, ops, presets=["shorts_9x16"])


def recipe_talking_head(talk: str, *, logo: str | None = None, name: str | None = None) -> Brief:
    sources = {"talk": str(Path(talk).expanduser().resolve())}
    ops: list[dict[str, Any]] = [
        {"op": "cut_silence"},
        {"op": "highpass", "hz": 80},
        {"op": "denoise", "strength": 2},
        {"op": "captions", "burn": True},
    ]
    if logo:
        sources["logo"] = str(Path(logo).expanduser().resolve())
        ops.append({"op": "watermark", "source": "logo", "opacity": 0.9, "scale": 0.12})
    ops += [{"op": "loudnorm", "i": -14}, {"op": "export", "presets": ["youtube_1080p", "shorts_9x16"]}]
    return _base(name or Path(talk).stem, sources, ops)


def recipe_slideshow(images: list[str], *, music: str | None = None, hold: float = 3.0, name: str = "slideshow") -> Brief:
    sources = {f"img{i}": str(Path(p).expanduser().resolve()) for i, p in enumerate(images)}
    ops: list[dict[str, Any]] = [
        {"op": "slideshow", "images": list(sources.keys()), "duration": hold, "fade": 0.45},
    ]
    if music:
        sources["music"] = str(Path(music).expanduser().resolve())
        ops.append({"op": "replace_audio", "source": "music"})
        ops.append({"op": "loudnorm", "i": -14})
    ops.append({"op": "export", "presets": ["youtube_1080p"]})
    return _base(name, sources, ops)


RECIPES = {
    "podcast": recipe_podcast,
    "youtube": recipe_youtube,
    "shorts": recipe_shorts,
    "talking_head": recipe_talking_head,
    "slideshow": recipe_slideshow,
}


def list_recipes() -> dict[str, str]:
    return {
        "podcast": "Silence-cut talking-head + captions + optional ducked music + video/audio masters",
        "youtube": "Silence-cut + captions + EBU-ish loudness + 1080p YouTube",
        "shorts": "Silence-cut + 9:16 fill crop + boxed captions",
        "talking_head": "Podcast-grade voice + captions + optional watermark + 16:9 and 9:16",
        "slideshow": "Ken-burns-free stills with xfade + optional music bed",
    }


def apply_recipe(name: str, *, inp: str | None = None, music: str | None = None, images: list[str] | None = None, logo: str | None = None, title: str | None = None) -> Brief:
    key = name.strip().lower().replace("-", "_")
    if key == "podcast":
        if not inp:
            raise BriefError("recipe podcast requires --in")
        return recipe_podcast(inp, music=music)
    if key == "youtube":
        if not inp:
            raise BriefError("recipe youtube requires --in")
        return recipe_youtube(inp, title=title)
    if key == "shorts":
        if not inp:
            raise BriefError("recipe shorts requires --in")
        return recipe_shorts(inp)
    if key == "talking_head":
        if not inp:
            raise BriefError("recipe talking_head requires --in")
        return recipe_talking_head(inp, logo=logo)
    if key == "slideshow":
        if not images:
            raise BriefError("recipe slideshow requires --images")
        return recipe_slideshow(images, music=music)
    raise BriefError(f"unknown recipe '{name}'. Known: {', '.join(sorted(RECIPES))}")
