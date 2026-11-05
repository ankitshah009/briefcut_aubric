"""The creative settings of an edit, as data.

``plan["edit"]`` holds everything a creator chooses beyond which moments play:
look, transitions, captions, music, text, logo, cleanup, cutout, and export.
It is editorial data only. Values come from closed catalogues or bounded
numbers, and media is referenced by asset id, never by path. The compiler is
still the only thing that turns any of it into executable steps, so a model or
an API client can propose an edit but can never inject a command.

The catalogues here are also what Studio and agents show as the available
choices, so adding a look or a transition starts in this file.
"""

from __future__ import annotations

import copy
import math
import re
import unicodedata
from typing import Any

MAX_TEXTS = 24
MAX_OVERLAYS = 12
MAX_REMOVED = 400
MAX_CAPTION_EDITS = 600  # one story's caption lines, with room to spare
MAX_CAPTION_EDIT_CHARS = 300
LONGEST_SOURCE_SECONDS = 86_400.0  # a day of footage: the widest range a removed cut or a card start may name
MAX_TEXT_CHARS = 160
MAX_EMPHASIS = 240  # punch words across a whole story
MAX_EMPHASIS_PER_CUE = 2
MAX_EMPHASIS_INDEX = 159  # a caption line holds at most 300 characters, so far fewer words than this
MAX_EMPHASIS_WORD = 64
EMPHASIS_COLOR = "#3CE88C"  # an accent apart from the spoken-word highlight, so the two never read as one
ASSET_ID = re.compile(r"^[a-f0-9]{8,32}$")
CUE_KEY = re.compile(r"\d{1,8}")
COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
LANGUAGE = re.compile(
    r"^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,2}$"
)  # en, pt-BR, es-419, zh-Hant-TW; the same tags emphasis accepts

LOOKS = (
    ("none", "Original", "Keep the footage as shot."),
    ("auto", "Auto adjust", "Balance exposure, contrast, and color from this footage."),
    ("warm", "Warm", "Golden light for people, food, and handmade work."),
    ("cool", "Cool", "Clean blue-leaning tones for tech and product shots."),
    ("vivid", "Vivid", "Richer color with a little more contrast."),
    ("cinematic", "Cinematic", "Teal shadows, warm skin, and a soft contrast curve."),
    ("film", "Film", "Lifted blacks and gentle grain."),
    ("mono", "Mono", "Clean black and white."),
    ("noir", "Noir", "High-contrast black and white with deep shadows."),
    ("vintage", "Vintage", "Faded color with warm highlights."),
)
TRANSITIONS = (
    ("none", "Cut", "Hard cuts between moments."),
    ("fade", "Crossfade", "A soft blend between moments."),
    ("fadeblack", "Dip to black", "Fade through black."),
    ("fadewhite", "Dip to white", "Fade through white."),
    ("dissolve", "Dissolve", "A textured dissolve."),
    ("slideleft", "Slide left", "The next moment pushes in from the right."),
    ("slideup", "Slide up", "The next moment pushes in from below."),
    ("wipeleft", "Wipe", "A clean wipe across the frame."),
    ("circleopen", "Iris", "A circle opens onto the next moment."),
    ("zoomin", "Zoom", "Zooms through into the next moment."),
    ("hblur", "Blur", "A horizontal blur between moments."),
)
CAPTION_STYLES = (
    ("boxed", "Boxed", "White text on a dark box. Readable on anything."),
    ("minimal", "Minimal", "Small clean text with a soft shadow."),
    ("bold", "Bold", "Large heavy text with a strong outline."),
    ("karaoke", "Karaoke", "Each word lights up as it is spoken."),
    ("pop", "Pop", "One short phrase at a time, the current word highlighted."),
)
CAPTION_POSITIONS = ("bottom", "middle", "top")
ZOOM_MODES = (
    ("none", "No zoom", "Keep the framing steady."),
    ("slow", "Slow push", "A slow push-in on every moment."),
    ("punch", "Punch-in", "A quick reframe at the start of alternating moments."),
)
TEXT_STYLES = (
    ("title", "Title", "Large centered text."),
    ("label", "Label", "A small boxed label."),
    ("lower_third", "Lower third", "A name or a place, low in the frame."),
    ("callout", "Callout", "Bold text with a colored box."),
)
TEXT_POSITIONS = ("top", "center", "bottom")
OVERLAY_POSITIONS = ("top-left", "top-right", "bottom-left", "bottom-right", "center")
CUTOUT_MODES = (
    ("none", "Off", "Keep the original background."),
    ("blur", "Background blur", "Keep the subject sharp and blur everything else."),
    ("dim", "Spotlight", "Darken everything except the subject."),
    ("mono", "Color pop", "Keep the subject in color and turn the rest black and white."),
    ("color", "Solid background", "Put the subject on a solid color."),
    ("image", "New background", "Put the subject on an image you choose or generate."),
)
EXPORT_QUALITIES = ("720p", "1080p", "4k")
# "source" keeps the footage's own rate, snapped to the nearest standard one and capped at 60.
EXPORT_FPS = ("source", 24, 30, 60)
EXPORT_FORMATS = ("mp4", "mov", "webm", "gif")
# A hero word: one or two spoken words, huge, behind the subject for a moment.
HERO_SIDES = ("center", "top", "left", "right")
HERO_FONTS = (
    ("heavy", "Heavy", "A wide, heavy sans for short words."),
    ("condensed", "Condensed", "Tall and narrow, so a longer word stays big."),
    ("rounded", "Rounded", "Heavy with soft corners."),
)
HERO_COLORS = ("#FFFFFF", "#FFD60A", "#FF453A", "#FF9F0A", "#30D158", "#0A84FF", "#101014")
HERO_MIN_SECONDS = 0.5
HERO_MAX_SECONDS = 3.0
HERO_MAX_CHARS = 18
HERO_MAX_WORDS = 2
# Letters and digits in any script, joined by spaces, apostrophes or hyphens, with one closing mark.
HERO_WORD = re.compile(r"^[^\W_]+(?:['’\- ][^\W_]+)*[!?.]?$")


def hero_word_ok(word: str) -> bool:
    """True for a hero word in any script. Combining marks (Hindi and Thai vowel signs, Arabic vowels) are allowed
    right after a letter, digit or another mark; a mark anywhere else is refused. The rest must match HERO_WORD."""
    kept: list[str] = []
    for char in word:
        if unicodedata.category(char).startswith("M"):
            if not kept or not (kept[-1].isalnum() or kept[-1] == "\u0000"):
                return False
            kept.append("\u0000")  # stands in for the mark, so a following mark still sees a mark before it
            continue
        kept.append(char)
    return bool(HERO_WORD.fullmatch("".join(char for char in kept if char != "\u0000")))


# A 3D motion card: words in a closed set of materials, rendered by Briefcut's own Blender template.
MOTION_CARD_KINDS = (
    ("intro_title", "Intro title", "The title over the opening seconds."),
    ("end_card", "End card", "A closing line over the last seconds."),
    ("lower_third", "Lower third", "A name or a place, low in the frame."),
)
MOTION_CARD_STYLES = (
    ("brushed_metal", "Brushed metal", "Bevelled steel letters with a clear coat."),
    ("warm_wood", "Warm wood", "Oiled hardwood letters with long grain."),
    ("soft_clay", "Soft clay", "Rounded, matte letters with soft light."),
    ("neon_glass", "Neon glass", "Clear glass letters traced by a lit tube."),
    ("paper_cut", "Paper cut", "Stacked paper sheets in the palette's colors."),
)
MOTION_CARD_CAMERAS = (
    ("dolly_in", "Dolly in", "The camera glides toward the words."),
    ("orbit", "Orbit", "The camera swings around the words."),
    ("rise", "Rise", "The camera rises from below to meet the words."),
    ("push_tilt", "Push and tilt", "A push in that levels off as it lands."),
)
# Each palette is (primary, accent, shadow): the letters, the subtitle and rim light, and the deepest tone.
MOTION_CARD_PALETTES = {
    "oak": ("#C98B55", "#F3DDBB", "#3A2414"),
    "walnut": ("#8A5A3B", "#E8C9A0", "#24160E"),
    "steel": ("#D7DCE2", "#8FB8FF", "#10141B"),
    "gold": ("#E2B35A", "#FFF1C9", "#3B2A0C"),
    "ember": ("#FF7A3D", "#FFD2A6", "#2B0F08"),
    "ocean": ("#4FB3E8", "#D4F1FF", "#0A2233"),
    "mint": ("#62D6A6", "#EFFFF6", "#0C2B21"),
    "rose": ("#F07AA0", "#FFE1EA", "#2E0E19"),
    "violet": ("#9B7CFF", "#E6DDFF", "#160F33"),
    "mono": ("#F2F2F2", "#B8B8B8", "#111111"),
}
MOTION_CARD_MIN_SECONDS = 1.5
MOTION_CARD_MAX_SECONDS = 4.0
MOTION_CARD_MAX_CHARS = 28
MOTION_CARD_MAX_WORDS = 5
MOTION_CARD_SUBTITLE_CHARS = 48
MOTION_CARD_ASPECTS = ("9:16", "16:9", "1:1", "4:5")

_DEFAULT: dict[str, Any] = {
    "template": "none",
    "look": {
        "preset": "none",
        "intensity": 1.0,
        "brightness": 0.0,
        "contrast": 0.0,
        "saturation": 0.0,
        "temperature": 0.0,
        "vignette": 0.0,
        "sharpen": 0.0,
    },
    "transition": {"type": "none", "seconds": 0.35},
    "captions": {
        "style": "boxed",
        "position": "bottom",
        "scale": 1.0,
        "color": "#FFFFFF",
        "highlight": "#FFD60A",
        "language": None,
        "edits": {},
        "emphasize": False,
        "emphasis": [],
        "emphasis_color": EMPHASIS_COLOR,
    },
    "trim_pauses": False,
    "remove_fillers": False,
    "removed": [],
    "audio": {"enhance_voice": False, "denoise": False, "keep_loudness": False, "mute_source": False},
    "music": None,
    "zoom": {"mode": "none", "strength": 0.5},
    "stabilize": False,
    "denoise_picture": False,
    "texts": [],
    "logo": None,
    "overlays": [],
    "fade": {"in": 0.0, "out": 0.0},
    "cutout": {"mode": "none", "subject": "", "color": "#101418", "asset": None, "blur": 0.6},
    "hero_word": None,
    "motion_card": None,
    "export": {"quality": "1080p", "fps": "source", "format": "mp4"},
}

TEMPLATES = (
    {"id": "none", "name": "Blank", "description": "Start from your footage with nothing applied.", "edit": {}},
    {
        "id": "product_story",
        "name": "Product story",
        "description": "Warm color, soft transitions, and clean captions for showing something you made.",
        "edit": {
            "look": {"preset": "warm"},
            "transition": {"type": "fade", "seconds": 0.3},
            "captions": {"style": "boxed"},
            "trim_pauses": True,
            "zoom": {"mode": "slow", "strength": 0.4},
            "fade": {"out": 0.5},
        },
    },
    {
        "id": "punchy_short",
        "name": "Punchy short",
        "description": "Fast cuts, word-by-word captions, and punch-ins for short feeds.",
        "edit": {
            "look": {"preset": "vivid"},
            "captions": {"style": "pop"},
            "trim_pauses": True,
            "remove_fillers": True,
            "zoom": {"mode": "punch", "strength": 0.6},
        },
    },
    {
        "id": "talking_head",
        "name": "Talking head",
        "description": "Clean speech, removed fillers, and karaoke captions.",
        "edit": {
            "look": {"preset": "auto"},
            "captions": {"style": "karaoke"},
            "trim_pauses": True,
            "remove_fillers": True,
            "audio": {"enhance_voice": True, "denoise": True},
        },
    },
    {
        "id": "cinematic",
        "name": "Cinematic",
        "description": "A graded look, dip-to-black transitions, and slow pushes.",
        "edit": {
            "look": {"preset": "cinematic"},
            "transition": {"type": "fadeblack", "seconds": 0.5},
            "captions": {"style": "minimal"},
            "zoom": {"mode": "slow", "strength": 0.5},
            "fade": {"in": 0.5, "out": 0.8},
        },
    },
    {
        "id": "recap",
        "name": "Event recap",
        "description": "Bright color and quick slides that keep a day moving.",
        "edit": {
            "look": {"preset": "vivid"},
            "transition": {"type": "slideleft", "seconds": 0.3},
            "captions": {"style": "bold"},
        },
    },
    {
        "id": "tutorial",
        "name": "How-to",
        "description": "Steady framing, big readable captions, and clear speech.",
        "edit": {
            "look": {"preset": "auto"},
            "captions": {"style": "bold", "scale": 1.15},
            "trim_pauses": True,
            "audio": {"enhance_voice": True},
            "transition": {"type": "fade", "seconds": 0.25},
        },
    },
)


class EditError(ValueError):
    """An edit setting is not one Briefcut can apply."""


def _ids(rows: tuple[tuple[str, str, str], ...]) -> tuple[str, ...]:
    return tuple(row[0] for row in rows)


def _rows(rows: tuple[tuple[str, str, str], ...]) -> list[dict[str, str]]:
    return [{"id": ident, "name": name, "description": description} for ident, name, description in rows]


def default_edit() -> dict[str, Any]:
    return copy.deepcopy(_DEFAULT)


def merge_edit(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge a partial edit. Lists and scalars replace; objects merge."""
    merged = copy.deepcopy(base)
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_edit(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def template_edit(template: str) -> dict[str, Any]:
    """A full edit with one template's choices applied."""
    for row in TEMPLATES:
        if row["id"] == template:
            return merge_edit(merge_edit(default_edit(), row["edit"]), {"template": template})
    raise EditError(f"Unknown template: {template}.")


def catalogue() -> dict[str, Any]:
    """What Studio and agents can choose from."""
    return {
        "looks": _rows(LOOKS),
        "transitions": _rows(TRANSITIONS),
        "caption_styles": _rows(CAPTION_STYLES),
        "caption_positions": list(CAPTION_POSITIONS),
        "zoom_modes": _rows(ZOOM_MODES),
        "text_styles": _rows(TEXT_STYLES),
        "text_positions": list(TEXT_POSITIONS),
        "overlay_positions": list(OVERLAY_POSITIONS),
        "cutout_modes": _rows(CUTOUT_MODES),
        "motion_card": {
            "kinds": _rows(MOTION_CARD_KINDS),
            "styles": _rows(MOTION_CARD_STYLES),
            "cameras": _rows(MOTION_CARD_CAMERAS),
            "palettes": {name: list(colors) for name, colors in MOTION_CARD_PALETTES.items()},
            "min_seconds": MOTION_CARD_MIN_SECONDS,
            "max_seconds": MOTION_CARD_MAX_SECONDS,
            "max_chars": MOTION_CARD_MAX_CHARS,
            "subtitle_chars": MOTION_CARD_SUBTITLE_CHARS,
        },
        "hero_word": {
            "sides": list(HERO_SIDES),
            "fonts": _rows(HERO_FONTS),
            "colors": list(HERO_COLORS),
            "min_seconds": HERO_MIN_SECONDS,
            "max_seconds": HERO_MAX_SECONDS,
            "max_chars": HERO_MAX_CHARS,
            "max_words": HERO_MAX_WORDS,
        },
        "templates": [
            {"id": row["id"], "name": row["name"], "description": row["description"], "edit": row["edit"]}
            for row in TEMPLATES
        ],
        "export": {"qualities": list(EXPORT_QUALITIES), "fps": list(EXPORT_FPS), "formats": list(EXPORT_FORMATS)},
        "defaults": default_edit(),
    }


def _number(value: Any, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise EditError(f"{name} must be a number.")
    number = float(value)
    if not low <= number <= high:
        raise EditError(f"{name} must be between {low:g} and {high:g}.")
    return round(number, 4)


def _choice(value: Any, name: str, allowed: tuple[Any, ...]) -> Any:
    if value not in allowed:
        raise EditError(f"{name} is not one of the available choices.")
    return value


def _flag(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise EditError(f"{name} must be true or false.")
    return value


def _color(value: Any, name: str) -> str:
    if not isinstance(value, str) or not COLOR.fullmatch(value):
        raise EditError(f"{name} must be a color like #FFD60A.")
    return value.upper()


def _text(value: Any, name: str, limit: int = MAX_TEXT_CHARS) -> str:
    if not isinstance(value, str):
        raise EditError(f"{name} must be text.")
    cleaned = " ".join(value.split())
    if len(cleaned) > limit:
        raise EditError(f"{name} can be at most {limit} characters.")
    if any(ord(ch) < 32 for ch in cleaned):
        raise EditError(f"{name} contains control characters.")
    return cleaned


def _asset(value: Any, name: str, assets: dict[str, Any] | None, kinds: tuple[str, ...]) -> str:
    if not isinstance(value, str) or not ASSET_ID.fullmatch(value):
        raise EditError(f"{name} must be an uploaded asset.")
    if assets is not None:
        record = assets.get(value)
        if not isinstance(record, dict):
            raise EditError(f"{name} is not an asset of this project.")
        if record.get("kind") not in kinds:
            raise EditError(f"{name} must be {' or '.join(kinds)}.")
    return value


def _timed(item: dict[str, Any], name: str, duration: float) -> tuple[float, float]:
    start = _number(item.get("start", 0.0), f"{name} start", 0.0, max(0.0, duration))
    length = _number(item.get("duration", 3.0), f"{name} length", 0.1, max(0.1, duration))
    return start, length


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EditError(f"{name} must be an object.")
    return value


def validate_emphasis(value: Any) -> list[dict[str, Any]]:
    """Punch words, as ``[{cue_key, word_index, word}]``: bounded, unique, and at most two in a line.

    ``word`` is the word as it was written when it was chosen. The renderer draws a
    word only while the line still has that word at that place, so a caption edit
    or a translation can never move the emphasis onto a different word.
    """
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_EMPHASIS:
        raise EditError(f"Caption emphasis must be a list of at most {MAX_EMPHASIS} words.")
    clean: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    per_cue: dict[str, int] = {}
    for item in value:
        pick = _object(item, "Each emphasised word")
        key, index, word = pick.get("cue_key"), pick.get("word_index"), pick.get("word")
        if not isinstance(key, str) or not CUE_KEY.fullmatch(key):
            raise EditError("Caption emphasis is keyed by the cue's start time in milliseconds.")
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= MAX_EMPHASIS_INDEX:
            raise EditError(f"An emphasised word's place must be a whole number from 0 to {MAX_EMPHASIS_INDEX}.")
        if (
            not isinstance(word, str)
            or not word
            or len(word) > MAX_EMPHASIS_WORD
            or word != word.strip()
            or any(ch.isspace() or ord(ch) < 32 for ch in word)
        ):
            raise EditError(f"An emphasised word must be one word of at most {MAX_EMPHASIS_WORD} characters.")
        if (key, index) in seen:
            raise EditError("A word can be emphasised only once.")
        per_cue[key] = per_cue.get(key, 0) + 1
        if per_cue[key] > MAX_EMPHASIS_PER_CUE:
            raise EditError(f"A caption line can emphasise at most {MAX_EMPHASIS_PER_CUE} words.")
        seen.add((key, index))
        clean.append({"cue_key": key, "word_index": index, "word": word})
    return clean


def validate_edit(value: Any, *, duration: float, assets: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a complete, bounded edit, or raise :class:`EditError`.

    ``duration`` is the finished story's length. Unknown keys are dropped.
    ``assets`` maps asset ids to records; ``None`` skips the membership check
    for callers that only need the shape.
    """
    raw = merge_edit(default_edit(), _object(value if value is not None else {}, "The edit"))
    clean = default_edit()
    template_ids = tuple(row["id"] for row in TEMPLATES)
    clean["template"] = _choice(raw.get("template"), "Template", template_ids)
    clean["look"] = _validate_look(_object(raw["look"], "Look"))
    transition = _object(raw["transition"], "Transition")
    clean["transition"] = {
        "type": _choice(transition.get("type"), "Transition", _ids(TRANSITIONS)),
        "seconds": _number(transition.get("seconds", 0.35), "Transition length", 0.1, 2.0),
    }
    clean["captions"] = _validate_captions(_object(raw["captions"], "Captions"))
    clean["trim_pauses"] = _flag(raw.get("trim_pauses"), "Trim pauses")
    clean["remove_fillers"] = _flag(raw.get("remove_fillers"), "Remove fillers")
    clean["stabilize"] = _flag(raw.get("stabilize"), "Stabilize")
    clean["denoise_picture"] = _flag(raw.get("denoise_picture"), "Picture denoise")
    clean["removed"] = _validate_removed(raw.get("removed"))
    audio = _object(raw["audio"], "Audio")
    clean["audio"] = {
        key: _flag(audio.get(key, False), f"Audio setting {key}")
        for key in ("enhance_voice", "denoise", "keep_loudness", "mute_source")
    }
    if raw.get("music") is not None:
        clean["music"] = _validate_music(_object(raw["music"], "Music"), assets)
    zoom = _object(raw["zoom"], "Zoom")
    clean["zoom"] = {
        "mode": _choice(zoom.get("mode"), "Zoom", _ids(ZOOM_MODES)),
        "strength": _number(zoom.get("strength", 0.5), "Zoom strength", 0.0, 1.0),
    }
    clean["texts"] = _validate_texts(raw.get("texts"), duration)
    if raw.get("logo") is not None:
        clean["logo"] = _validate_logo(_object(raw["logo"], "Logo"), assets)
    clean["overlays"] = _validate_overlays(raw.get("overlays"), duration, assets)
    fade = _object(raw["fade"], "Fade")
    clean["fade"] = {
        "in": _number(fade.get("in", 0.0), "Fade in", 0.0, 5.0),
        "out": _number(fade.get("out", 0.0), "Fade out", 0.0, 5.0),
    }
    clean["cutout"] = _validate_cutout(_object(raw["cutout"], "Cutout"), assets)
    export = _object(raw["export"], "Export")
    clean["export"] = {
        "quality": _choice(export.get("quality"), "Export quality", EXPORT_QUALITIES),
        "fps": _choice(export.get("fps"), "Export frame rate", EXPORT_FPS),
        "format": _choice(export.get("format"), "Export format", EXPORT_FORMATS),
    }
    clean["hero_word"] = validate_hero_word(raw.get("hero_word"), duration=duration)
    clean["motion_card"] = validate_motion_card(raw.get("motion_card"), duration=duration, assets=assets)
    return clean


def _validate_look(look: dict[str, Any]) -> dict[str, Any]:
    return {
        "preset": _choice(look.get("preset"), "Look", _ids(LOOKS)),
        "intensity": _number(look.get("intensity", 1.0), "Look strength", 0.0, 1.0),
        "brightness": _number(look.get("brightness", 0.0), "Brightness", -1.0, 1.0),
        "contrast": _number(look.get("contrast", 0.0), "Contrast", -1.0, 1.0),
        "saturation": _number(look.get("saturation", 0.0), "Saturation", -1.0, 1.0),
        "temperature": _number(look.get("temperature", 0.0), "Temperature", -1.0, 1.0),
        "vignette": _number(look.get("vignette", 0.0), "Vignette", 0.0, 1.0),
        "sharpen": _number(look.get("sharpen", 0.0), "Sharpen", 0.0, 1.0),
    }


def _validate_captions(captions: dict[str, Any]) -> dict[str, Any]:
    """Caption settings, with the per-line text edits keyed by the cue's start in milliseconds."""
    language = captions.get("language")
    if language is not None and (not isinstance(language, str) or not LANGUAGE.fullmatch(language)):
        raise EditError("Caption language must be a language code such as es or pt-BR.")
    edits: dict[str, str] = {}
    raw_edits = captions.get("edits") or {}
    if not isinstance(raw_edits, dict) or len(raw_edits) > MAX_CAPTION_EDITS:
        raise EditError("Caption edits must be a bounded object.")
    for key, text in raw_edits.items():
        if not isinstance(key, str) or not CUE_KEY.fullmatch(key):
            raise EditError("Caption edits are keyed by the cue's start time in milliseconds.")
        edits[key] = _text(text, "Caption text", MAX_CAPTION_EDIT_CHARS)
    return {
        "style": _choice(captions.get("style"), "Caption style", _ids(CAPTION_STYLES)),
        "position": _choice(captions.get("position"), "Caption position", CAPTION_POSITIONS),
        "scale": _number(captions.get("scale", 1.0), "Caption size", 0.5, 2.0),
        "color": _color(captions.get("color", "#FFFFFF"), "Caption color"),
        "highlight": _color(captions.get("highlight", "#FFD60A"), "Caption highlight"),
        "language": language,
        "edits": edits,
        "emphasize": _flag(captions.get("emphasize", False), "Caption emphasis"),
        "emphasis": validate_emphasis(captions.get("emphasis")),
        "emphasis_color": _color(captions.get("emphasis_color", EMPHASIS_COLOR), "Caption emphasis color"),
    }


def _validate_removed(value: Any) -> list[list[float]]:
    """Source ranges the edit cuts out, each ``[start, end]`` in seconds."""
    removed = value or []
    if not isinstance(removed, list) or len(removed) > MAX_REMOVED:
        raise EditError("Removed ranges must be a bounded list.")
    clean: list[list[float]] = []
    for item in removed:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise EditError("Each removed range needs a start and an end.")
        low = _number(item[0], "Removed start", 0.0, LONGEST_SOURCE_SECONDS)
        high = _number(item[1], "Removed end", 0.0, LONGEST_SOURCE_SECONDS)
        if high <= low:
            raise EditError("A removed range must end after it starts.")
        clean.append([low, high])
    return clean


def _validate_music(music: dict[str, Any], assets: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "asset": _asset(music.get("asset"), "Music", assets, ("audio",)),
        "volume_db": _number(music.get("volume_db", -18.0), "Music volume", -40.0, 0.0),
        "duck": _flag(music.get("duck", True), "Music ducking"),
        "fade_out": _number(music.get("fade_out", 1.5), "Music fade", 0.0, 10.0),
        "start": _number(music.get("start", 0.0), "Music start", 0.0, 3600.0),
        "beat_sync": _flag(music.get("beat_sync", False), "Beat sync"),
    }


def _validate_texts(value: Any, duration: float) -> list[dict[str, Any]]:
    texts = value or []
    if not isinstance(texts, list) or len(texts) > MAX_TEXTS:
        raise EditError(f"A story can hold at most {MAX_TEXTS} text items.")
    clean: list[dict[str, Any]] = []
    for index, item in enumerate(texts, 1):
        record = _object(item, f"Text {index}")
        body = _text(record.get("text"), f"Text {index}")
        if not body:
            raise EditError(f"Text {index} is empty.")
        start, length = _timed(record, f"Text {index}", duration)
        clean.append(
            {
                "text": body,
                "start": start,
                "duration": length,
                "style": _choice(record.get("style", "label"), f"Text {index} style", _ids(TEXT_STYLES)),
                "position": _choice(record.get("position", "center"), f"Text {index} position", TEXT_POSITIONS),
                "scale": _number(record.get("scale", 1.0), f"Text {index} size", 0.5, 2.5),
                "color": _color(record.get("color", "#FFFFFF"), f"Text {index} color"),
            }
        )
    return clean


def _validate_logo(logo: dict[str, Any], assets: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "asset": _asset(logo.get("asset"), "Logo", assets, ("image",)),
        "position": _choice(logo.get("position", "top-right"), "Logo position", OVERLAY_POSITIONS),
        "scale": _number(logo.get("scale", 0.14), "Logo size", 0.03, 0.6),
        "opacity": _number(logo.get("opacity", 0.9), "Logo opacity", 0.1, 1.0),
    }


def _validate_overlays(value: Any, duration: float, assets: dict[str, Any] | None) -> list[dict[str, Any]]:
    overlays = value or []
    if not isinstance(overlays, list) or len(overlays) > MAX_OVERLAYS:
        raise EditError(f"A story can hold at most {MAX_OVERLAYS} overlays.")
    clean: list[dict[str, Any]] = []
    for index, item in enumerate(overlays, 1):
        record = _object(item, f"Overlay {index}")
        start, length = _timed(record, f"Overlay {index}", duration)
        clean.append(
            {
                "asset": _asset(record.get("asset"), f"Overlay {index}", assets, ("image",)),
                "start": start,
                "duration": length,
                "position": _choice(record.get("position", "center"), f"Overlay {index} position", OVERLAY_POSITIONS),
                "scale": _number(record.get("scale", 0.3), f"Overlay {index} size", 0.03, 1.0),
            }
        )
    return clean


def _validate_cutout(cutout: dict[str, Any], assets: dict[str, Any] | None) -> dict[str, Any]:
    """A subject cutout needs a named subject; only the ``image`` mode carries a background asset."""
    mode = _choice(cutout.get("mode"), "Cutout", _ids(CUTOUT_MODES))
    subject = _text(cutout.get("subject", ""), "Cutout subject", 150)
    if mode != "none" and not subject:
        raise EditError("Name the subject to cut out, such as “the person on the left”.")
    image = _asset(cutout.get("asset"), "Cutout background", assets, ("image",)) if mode == "image" else None
    return {
        "mode": mode,
        "subject": subject,
        "color": _color(cutout.get("color", "#101418"), "Cutout color"),
        "asset": image,
        "blur": _number(cutout.get("blur", 0.6), "Background blur", 0.0, 1.0),
    }


def validate_hero_word(value: Any, *, duration: float) -> dict[str, Any] | None:
    """``edit.hero_word``: a short spoken word, its window on the output clock, and closed style choices.

    The window must sit inside the cut and last 0.5 to 3 seconds. Whether the
    word is really spoken there, and inside one shot, is ``vedit.hero_word``'s
    check, because only it holds the transcript and the timeline.
    """
    if value is None:
        return None
    item = _object(value, "Hero word")
    word = _text(item.get("word"), "Hero word", HERO_MAX_CHARS)
    if not word:
        raise EditError("The hero word is empty.")
    if len(word.split()) > HERO_MAX_WORDS:
        raise EditError(f"The hero word can be at most {HERO_MAX_WORDS} words.")
    if not hero_word_ok(word):
        raise EditError("The hero word can hold only letters, digits, spaces, apostrophes and hyphens.")
    start = _number(item.get("start"), "Hero word start", 0.0, max(0.0, duration))
    end = _number(item.get("end"), "Hero word end", 0.0, max(0.0, duration))
    if not HERO_MIN_SECONDS - 1e-6 <= end - start <= HERO_MAX_SECONDS + 1e-6:
        raise EditError(f"The hero word shows for {HERO_MIN_SECONDS:g} to {HERO_MAX_SECONDS:g} seconds.")
    subject = _text(item.get("subject", ""), "Hero word subject", 150)
    if not subject:
        raise EditError("Name the subject the word goes behind, such as “the speaker”.")
    color = _color(item.get("color", HERO_COLORS[0]), "Hero word color")
    if color not in HERO_COLORS:
        raise EditError("The hero word color is not one of the available choices.")
    clean = {
        "word": word,
        "start": start,
        "end": end,
        "subject": subject,
        "side": _choice(item.get("side", "center"), "Hero word side", HERO_SIDES),
        "font": _choice(item.get("font", "heavy"), "Hero word font", _ids(HERO_FONTS)),
        "color": color,
        "reason": _text(item.get("reason", ""), "Hero word reason", 300),
    }
    # Where SAM measured the subject during the window, as shares of the frame, so the word can sit on its edge.
    box = item.get("subject_box")
    if box is not None:
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            raise EditError("The hero word's subject box is four numbers between 0 and 1.")
        box = [_number(value, "Hero word subject box", 0.0, 1.0) for value in box]
        if not (box[0] < box[2] and box[1] < box[3]):
            raise EditError("The hero word's subject box must have a size.")
        clean["subject_box"] = box
    return clean


def _card_text(value: Any, name: str, limit: int) -> str:
    """Words for a motion card: printable letters, digits, punctuation and spaces, nothing else.

    The text is only ever set as a text body in Blender, so code-looking input such
    as ``__import__('os')`` stays words on screen. Emoji, symbols the fonts lack, and
    control characters are refused rather than drawn as boxes.
    """
    text = _text(value, name, limit)
    for char in text:
        category = unicodedata.category(char)
        if category[0] == "M":
            raise EditError(
                f"{name} cannot hold vowel signs or other combining marks: Blender's text cannot place them. Use a title without them."
            )
        if not (category[0] in "LNP" or char == " " or char in "&+#%$€£¥@/=~°"):
            raise EditError(f"{name} can hold letters, digits, punctuation and spaces only.")
    return text


def validate_motion_card(
    value: Any, *, duration: float | None = None, assets: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """``edit.motion_card``: a 3D title card as closed choices and short words.

    Returns None for no card. ``duration`` is the finished story's length; a card
    never lasts longer than the story, and ``start`` (seconds on the output clock,
    for lower thirds) must leave room for the card. The card overlays the picture
    and never changes timing. ``backdrop`` is an image asset, like an overlay.
    """
    if value is None:
        return None
    return motion_card_spec(_object(value, "Motion card"), duration=duration, assets=assets)


def motion_card_spec(
    item: dict[str, Any], *, duration: float | None = None, assets: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The bounded card an object describes; ``validate_motion_card`` states the rules. Never None."""
    text = _card_text(item.get("text"), "Motion card text", MOTION_CARD_MAX_CHARS)
    if not text:
        raise EditError("The motion card needs a few words.")
    if len(text.split()) > MOTION_CARD_MAX_WORDS:
        raise EditError(f"A motion card holds at most {MOTION_CARD_MAX_WORDS} words; move the rest to the subtitle.")
    subtitle = _card_text(item.get("subtitle", ""), "Motion card subtitle", MOTION_CARD_SUBTITLE_CHARS)
    palette = _choice(item.get("palette", "steel"), "Motion card palette", tuple(MOTION_CARD_PALETTES))
    seconds = _number(item.get("duration", 3.0), "Motion card length", MOTION_CARD_MIN_SECONDS, MOTION_CARD_MAX_SECONDS)
    if duration is not None and seconds > duration + 1e-6:
        raise EditError(f"The motion card lasts {seconds:g} s, longer than this {duration:g} s story.")
    kind = _choice(item.get("kind", "intro_title"), "Motion card kind", _ids(MOTION_CARD_KINDS))
    clean: dict[str, Any] = {
        "kind": kind,
        "text": text,
        "subtitle": subtitle,
        "style": _choice(item.get("style", "brushed_metal"), "Motion card style", _ids(MOTION_CARD_STYLES)),
        "palette": palette,
        "camera": _choice(item.get("camera", "dolly_in"), "Motion card camera", _ids(MOTION_CARD_CAMERAS)),
        "duration": seconds,
        "reason": _text(item.get("reason", ""), "Motion card reason", 300),
    }
    aspect = item.get("aspect")
    if aspect is not None:
        clean["aspect"] = _choice(aspect, "Motion card aspect", MOTION_CARD_ASPECTS)
    start = item.get("start")
    if start is not None:
        limit = max(0.0, (duration if duration is not None else LONGEST_SOURCE_SECONDS) - seconds)
        clean["start"] = _number(start, "Motion card start", 0.0, limit)
    backdrop = item.get("backdrop")
    if backdrop is not None:
        if kind == "lower_third":
            raise EditError("A lower third sits over the footage, so it cannot have a backdrop.")
        clean["backdrop"] = _asset(backdrop, "Motion card backdrop", assets, ("image",))
    return clean


def asset_ids(edit: dict[str, Any]) -> list[str]:
    """Every asset an edit refers to, for packaging and validation."""
    holders = [edit.get("music"), edit.get("logo"), edit.get("cutout"), *(edit.get("overlays") or [])]
    found = [holder["asset"] for holder in holders if isinstance(holder, dict) and isinstance(holder.get("asset"), str)]
    card = edit.get("motion_card")
    if isinstance(card, dict) and isinstance(card.get("backdrop"), str):
        found.append(card["backdrop"])
    return found
