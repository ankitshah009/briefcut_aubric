"""The director's loop: edit by conversation, find moments, script to video.

Models propose data and this module decides what that data may say. It holds
the settings a note can change, the plain words shown for every change, the
transcript search that costs nothing, the rule for when a refining loop stops,
and how a written script becomes scenes. Everything here is pure: no process,
no model. A script video is caption-led. Meta offers no text-to-speech, so
Briefcut adds no synthetic voice.
"""

from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Iterable

from vedit.edit_spec import (
    CAPTION_POSITIONS,
    CAPTION_STYLES,
    LOOKS,
    TEXT_POSITIONS,
    TEXT_STYLES,
    TRANSITIONS,
    ZOOM_MODES,
)

MAX_REFINE_ROUNDS = 3
MAX_FOUND = 8
MAX_SCENES = 16
MAX_SCRIPT_WORDS = 450
MAX_QUERY_CHARS = 200
SCENE_WORDS = 24
SCENE_MAX_WORDS = 60
READING_WORDS_PER_SECOND = 2.5
SILENT_SCENE_SECONDS = 2.5
SCRIPT_EDIT: dict[str, Any] = {"zoom": {"mode": "slow", "strength": 0.6}}  # a still needs movement to read as video

IMAGE_SIZES = {"9:16": "1024x1792", "16:9": "1792x1024", "1:1": "1024x1024", "4:5": "1024x1280"}
IMAGE_PRICE_USD = 0.01
IMAGE_RULE = "No text, letters, numbers, captions, signs, logos, or watermarks anywhere in the image."

_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


# ---------------------------------------------------------------------------
# What a note can change
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EditField:
    """One setting a patch may change. ``kind``: choice, number, flag, color, or append."""

    path: str
    name: str
    kind: str
    choices: tuple[str, ...] = ()
    names: tuple[tuple[str, str], ...] = ()
    low: float = 0.0
    high: float = 1.0
    unit: str = ""

    def label(self, value: Any) -> str:
        if self.kind == "flag":
            return "on" if value else "off"
        if self.kind == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return f"{float(value):g}{self.unit}"
        return dict(self.names).get(value, value) if isinstance(value, str) else str(value)


def _pick(path: str, name: str, rows: Iterable[tuple[str, str, str]]) -> EditField:
    rows = tuple(rows)
    return EditField(path, name, "choice", tuple(row[0] for row in rows), tuple((row[0], row[1]) for row in rows))


def _number(path: str, name: str, low: float, high: float, unit: str = "") -> EditField:
    return EditField(path, name, "number", low=low, high=high, unit=unit)


def _flag(path: str, name: str) -> EditField:
    return EditField(path, name, "flag")


def _color(path: str, name: str) -> EditField:
    return EditField(path, name, "color")


_FIELDS = (
    _pick("look.preset", "Look", LOOKS),
    _number("look.intensity", "Look strength", 0.0, 1.0),
    _number("look.brightness", "Brightness", -1.0, 1.0),
    _number("look.contrast", "Contrast", -1.0, 1.0),
    _number("look.saturation", "Saturation", -1.0, 1.0),
    _number("look.temperature", "Color temperature", -1.0, 1.0),
    _number("look.vignette", "Vignette", 0.0, 1.0),
    _number("look.sharpen", "Sharpening", 0.0, 1.0),
    _pick("transition.type", "Transition", TRANSITIONS),
    _number("transition.seconds", "Transition length", 0.1, 2.0, " s"),
    _pick("captions.style", "Caption style", CAPTION_STYLES),
    _number("captions.scale", "Caption size", 0.5, 2.0, "×"),
    EditField("captions.position", "Caption position", "choice", CAPTION_POSITIONS),
    _color("captions.color", "Caption color"),
    _color("captions.highlight", "Caption highlight"),
    _pick("zoom.mode", "Zoom", ZOOM_MODES),
    _number("zoom.strength", "Zoom strength", 0.0, 1.0),
    _flag("trim_pauses", "Trim pauses"),
    _flag("remove_fillers", "Remove filler words"),
    _flag("stabilize", "Stabilize"),
    _flag("denoise_picture", "Picture noise reduction"),
    _flag("audio.enhance_voice", "Voice enhancement"),
    _flag("audio.denoise", "Background noise removal"),
    _number("music.volume_db", "Music level", -40.0, 0.0, " dB"),
    _number("fade.in", "Fade in", 0.0, 5.0, " s"),
    _number("fade.out", "Fade out", 0.0, 5.0, " s"),
    EditField("texts", "On-screen text", "append"),
)
EDIT_FIELDS: dict[str, EditField] = {field.path: field for field in _FIELDS}
TEXT_DEFAULTS = {"duration": 3.0, "style": "label", "position": "center"}


def read_edit(edit: dict[str, Any], path: str) -> Any:
    """The current value at a dotted path, or None."""
    value: Any = edit
    for key in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def write_edit(edit: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    """A copy of ``edit`` with one path changed. ``texts`` appends instead of replacing."""
    changed = copy.deepcopy(edit)
    if path == "texts":
        changed["texts"] = list(changed.get("texts") or []) + [copy.deepcopy(value)]
        return changed
    keys = path.split(".")
    holder = changed
    for key in keys[:-1]:
        inner = holder.get(key)
        if not isinstance(inner, dict):
            raise KeyError(key)
        holder = inner
    holder[keys[-1]] = value
    return changed


def coerce_edit_value(field: EditField, value: Any, *, seconds: Any = None, text: Any = None) -> Any:
    """A model's value in the field's own type, or None when it cannot be one.

    A model may send "1.3" for a number or "true" for a flag. Nothing here accepts
    a value the field could not hold; ``validate_edit`` still has the last word.
    """
    if field.kind == "number":
        if isinstance(value, str):
            try:
                value = float(value)
            except ValueError:
                return None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            return None
        return float(value)
    if field.kind == "flag":
        if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
            return value.strip().lower() == "true"
        return value if isinstance(value, bool) else None
    if field.kind == "choice":
        return value if isinstance(value, str) and value in field.choices else None
    if field.kind == "color":
        return value.upper() if isinstance(value, str) and _COLOR.fullmatch(value) else None
    if field.kind == "append":
        placed = dict(text) if isinstance(text, dict) else {}
        body = placed.get("text", value)
        if not isinstance(body, str) or not body.strip():
            return None
        start = placed.get("start", seconds)
        if isinstance(start, bool) or not isinstance(start, (int, float)) or not math.isfinite(float(start)) or start < 0:
            start = 0.0
        length = placed.get("duration", TEXT_DEFAULTS["duration"])
        if isinstance(length, bool) or not isinstance(length, (int, float)) or not math.isfinite(float(length)) or length <= 0:
            length = TEXT_DEFAULTS["duration"]
        style = placed.get("style") if placed.get("style") in {row[0] for row in TEXT_STYLES} else TEXT_DEFAULTS["style"]
        position = placed.get("position") if placed.get("position") in TEXT_POSITIONS else TEXT_DEFAULTS["position"]
        return {"text": " ".join(body.split()), "start": float(start), "duration": float(length), "style": style, "position": position}
    return None


def edit_vocabulary() -> str:
    """The settings a model may change, one per line, with the exact ids it must use."""
    lines = []
    for field in _FIELDS:
        if field.kind == "choice":
            lines.append(f"{field.path}: one of {', '.join(field.choices)}")
        elif field.kind == "number":
            lines.append(f"{field.path}: number {field.low:g} to {field.high:g}")
        elif field.kind == "flag":
            lines.append(f"{field.path}: true or false")
        elif field.kind == "color":
            lines.append(f"{field.path}: a color such as #FFD60A")
        else:
            lines.append(f"{field.path}: value is the words to add; seconds is when they appear on the finished video; text may set duration, style ({', '.join(row[0] for row in TEXT_STYLES)}) and position ({', '.join(TEXT_POSITIONS)})")
    return "\n".join(lines)


def current_edit(plan: dict[str, Any]) -> dict[str, Any]:
    """The settings of a plan that a note can change, as a flat mapping for the prompt."""
    edit = plan.get("edit") if isinstance(plan, dict) and isinstance(plan.get("edit"), dict) else None
    if edit is None:
        from vedit.edit_spec import default_edit

        edit = default_edit()
    values: dict[str, Any] = {}
    for field in _FIELDS:
        if field.kind == "append":
            values[field.path] = [{"text": item.get("text"), "start": item.get("start")} for item in (edit.get("texts") or []) if isinstance(item, dict)]
        else:
            value = read_edit(edit, field.path)
            if value is not None:
                values[field.path] = value
    return values


def current_edit_json(plan: dict[str, Any]) -> str:
    return json.dumps(current_edit(plan), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def describe_patch(patch: dict[str, Any]) -> str:
    """One plain sentence for a patch, proposed or applied. Never raises."""
    op = patch.get("op")
    moment = f"moment {patch.get('segment')}" if patch.get("segment") is not None else "the story"
    seconds = patch.get("seconds")
    has_seconds = isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and math.isfinite(seconds)
    after = patch.get("after")
    if op == "trim_start":
        edge = after.get("start") if isinstance(after, dict) else seconds if has_seconds else None
        return f"{moment.capitalize()} starts at {edge:.1f} s." if isinstance(edge, (int, float)) else f"Change where {moment} starts."
    if op == "trim_end":
        edge = after.get("end") if isinstance(after, dict) else seconds if has_seconds else None
        return f"{moment.capitalize()} ends at {edge:.1f} s." if isinstance(edge, (int, float)) else f"Change where {moment} ends."
    if op == "remove":
        return f"Remove {moment}."
    if op == "move_before":
        return f"Move {moment} before moment {int(seconds)}." if has_seconds else f"Move {moment}."
    if op == "split":
        return f"Split {moment} at {seconds:.1f} s." if has_seconds else f"Split {moment}."
    if op == "set_speed":
        value = patch.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"Change the speed of {moment}."
        if value == 1:
            return f"{moment.capitalize()} plays at normal speed."
        return f"{moment.capitalize()} plays {'faster' if value > 1 else 'slower'} ({float(value):g}×)."
    if op == "set_edit":
        path = patch.get("path")
        field = EDIT_FIELDS.get(path) if isinstance(path, str) else None
        if field is None:
            return "Change a setting."
        value = patch.get("value")
        if field.kind == "append":
            words = value.get("text") if isinstance(value, dict) else value
            when = value.get("start") if isinstance(value, dict) else None
            where = f" at {when:.1f} s" if isinstance(when, (int, float)) and not isinstance(when, bool) else ""
            return f"Add the text “{words}”{where}."
        before = patch.get("before")
        change = f" (was {field.label(before)})" if before is not None and before != value else ""
        return f"{field.name}: {field.label(value)}{change}."
    return "Change the story."


# ---------------------------------------------------------------------------
# Auto-refine
# ---------------------------------------------------------------------------


def refine_stop(result: dict[str, Any], *, rounds: int, limit: int, worse_than: int | None,
                remaining_usd: float, round_cost_usd: float) -> str | None:
    """Why the loop must stop after this version, or None to render another round.

    ``rounds`` counts refined versions so far. ``worse_than`` is how many checks the
    previous version failed. The result is plain words a person can read.
    """
    checks = [check for check in (result.get("checks") or []) if isinstance(check, dict)]
    failed = sum(1 for check in checks if not check.get("passed"))
    review = result.get("review")
    if isinstance(review, dict) and review.get("error"):
        return f"Stopped: the review of this version did not complete. {review['error']}"
    if not checks or failed == 0:
        return "All checks passed."
    if not [patch for patch in (result.get("patches") or []) if isinstance(patch, dict)]:
        return f"Stopped: {failed} check{'s' if failed != 1 else ''} still fail{'' if failed != 1 else 's'} and no fixes remain."
    if worse_than is not None and failed > worse_than:
        return f"Stopped: this version fails {failed} checks and the one before it failed {worse_than}. Both remain available."
    if rounds >= limit:
        return f"Stopped after {limit} round{'s' if limit != 1 else ''}, the limit set for this project. {failed} check{'s' if failed != 1 else ''} still fail{'' if failed != 1 else 's'}."
    if remaining_usd + 1e-9 < round_cost_usd:
        return f"Stopped: another round needs about ${round_cost_usd:.4f} and ${max(0.0, remaining_usd):.4f} of the budget remains."
    return None


# ---------------------------------------------------------------------------
# Find in footage: the free half
# ---------------------------------------------------------------------------


def _stem(word: str) -> str:
    """A light stem so "sanding", "sanded", and "sand" meet. Not a linguistic claim."""
    if word.endswith("sses") or word.endswith("xes") or word.endswith("ches") or word.endswith("shes"):
        word = word[:-2]
    elif word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        word = word[:-1]
    for suffix in ("ing", "ed"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            word = word[: -len(suffix)]
            break
    if word.endswith("e") and len(word) > 3:
        word = word[:-1]
    if len(word) > 3 and word[-1] == word[-2] and word[-1] not in "aeiou":
        word = word[:-1]
    return word


def _terms(text: str) -> list[str]:
    from vedit.story import tokens

    found: list[str] = []
    for word in tokens(text) or re.findall(r"[^\W_]+", text.lower()):
        stem = _stem(word)
        if stem not in found:
            found.append(stem)
    return found


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def find_in_transcript(query: str, sentences: Iterable[dict[str, Any]], *, limit: int = MAX_FOUND) -> list[dict[str, Any]]:
    """Sentences that say what was asked for, best first. No model, no cost."""
    terms = _terms(query)
    phrase = " ".join(query.lower().split())
    if not terms:
        return []
    needed = math.ceil(len(terms) / 2)
    found: list[dict[str, Any]] = []
    for sentence in sentences:
        if not isinstance(sentence, dict) or not isinstance(sentence.get("text"), str):
            continue
        start, end = sentence.get("start"), sentence.get("end")
        if isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or end <= start:
            continue
        text = " ".join(sentence["text"].split())
        stems = {_stem(word) for word in re.findall(r"[^\W_]+", text.lower())}
        hits = sum(1 for term in terms if term in stems)
        if hits < needed:
            continue
        score = hits / len(terms) * 0.75 + (0.25 if phrase and phrase in text.lower() else 0.0)
        found.append({"start": round(float(start), 3), "end": round(float(end), 3), "reason": f"Said here: “{_clip(text, 160)}”",
                      "label": _clip(text, 60), "source": "transcript", "score": round(score, 3)})
    found.sort(key=lambda item: (-item["score"], item["start"]))
    return found[:limit]


def merge_moments(*groups: Iterable[dict[str, Any]], limit: int = MAX_FOUND) -> list[dict[str, Any]]:
    """Combine searches. A moment two searches agree on is one moment and ranks first."""
    merged: list[dict[str, Any]] = []
    for group in groups:
        for moment in group:
            twin = next((item for item in merged if min(item["end"], moment["end"]) - max(item["start"], moment["start"]) > 0.5 * min(item["end"] - item["start"], moment["end"] - moment["start"])), None)
            if twin is None:
                merged.append(dict(moment))
                continue
            if twin.get("source") != moment.get("source"):
                twin["source"] = "both"
                twin["score"] = round(min(1.0, max(twin.get("score", 0), moment.get("score", 0)) + 0.1), 3)
                twin["reason"] = f"{twin['reason']} {moment['reason']}"[:600]
    merged.sort(key=lambda item: (-item.get("score", 0), item["start"]))
    return merged[:limit]


# ---------------------------------------------------------------------------
# Script to video
# ---------------------------------------------------------------------------

_SCENE_LABEL = re.compile(r"^\s*(?:#{1,6}\s*)?(?:scene|shot|part)\s*\d+\s*[:.\-–—]?\s*", re.IGNORECASE)
_SENTENCE_END = re.compile(r"(?<=[.!?…])[\"'”’)\]]*\s+")
_SENTENCE_CLOSE = re.compile(r"[.!?…][\"'”’)\]]*$")


def _words(text: str) -> int:
    return len(text.split())


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_END.split(text) if part.strip()]


def _shared_out(sentences: list[str], scenes: int) -> list[str]:
    """Group sentences into ``scenes`` runs of about equal length, keeping their order."""
    total = sum(_words(item) for item in sentences)
    share = total / max(1, scenes)
    groups: list[list[str]] = [[]]
    spent = 0
    for index, sentence in enumerate(sentences):
        left = len(sentences) - index
        room = scenes - len(groups)
        if groups[-1] and room > 0 and (spent + _words(sentence) / 2 >= share * len(groups) or left <= room):
            groups.append([])
        groups[-1].append(sentence)
        spent += _words(sentence)
    return [" ".join(group) for group in groups if group]


def split_script(script: str, *, max_scenes: int = MAX_SCENES) -> list[str]:
    """Split a written script into scenes.

    Paragraphs are the writer's own scenes. One block of text is shared out by
    sentences into scenes of about ``SCENE_WORDS`` words. A scene never ends
    mid-sentence, and ``max_scenes`` always holds.
    """
    blocks: list[str] = []
    for block in re.split(r"\n\s*\n", script.strip()):
        lines = [_SCENE_LABEL.sub("", line).strip() for line in block.splitlines()]
        text = " ".join(line for line in lines if line)
        if text:
            blocks.append(text)
    if not blocks:
        return []
    scenes: list[str] = []
    for block in blocks:
        count = _words(block)
        pieces = _sentences(block)
        if len(blocks) == 1:
            scenes.extend(_shared_out(pieces, max(1, min(max_scenes, round(count / SCENE_WORDS)))))
        elif count > SCENE_MAX_WORDS and len(pieces) > 1:
            scenes.extend(_shared_out(pieces, math.ceil(count / SCENE_MAX_WORDS)))
        else:
            scenes.append(block)
    while len(scenes) > max_scenes:
        # The shortest neighbouring pair reads most naturally as one scene.
        index = min(range(len(scenes) - 1), key=lambda at: _words(scenes[at]) + _words(scenes[at + 1]))
        scenes[index:index + 2] = [scenes[index] + " " + scenes[index + 1]]
    return scenes


def reading_seconds(text: str) -> float:
    """How long a silent caption needs on screen."""
    return max(SILENT_SCENE_SECONDS, round(_words(text) / READING_WORDS_PER_SECOND, 2))


def scene_cues(text: str, start: float, end: float) -> list[dict[str, Any]]:
    """Timed cues for a scene, sentence by sentence, by share of its words."""
    sentences = _sentences(text) or [text]
    total = sum(max(1, _words(item)) for item in sentences)
    cues: list[dict[str, Any]] = []
    clock = start
    for sentence in sentences:
        length = (end - start) * max(1, _words(sentence)) / total
        cues.append({"start": round(clock, 3), "end": round(clock + length, 3), "text": sentence})
        clock += length
    return cues


def _word_map(heard: list[dict[str, Any]], written: list[str]) -> list[int | None]:
    """For each heard word, the written word it lines up with, or None."""
    from difflib import SequenceMatcher

    plain = re.compile(r"[^\w']+", re.UNICODE)
    heard_keys = [plain.sub("", str(word.get("text") or "")).lower() for word in heard]
    written_keys = [plain.sub("", word).lower() for word in written]
    mapping: list[int | None] = [None] * len(heard)
    for tag, low, high, first, last in SequenceMatcher(None, heard_keys, written_keys, autojunk=False).get_opcodes():
        if tag == "equal" or (tag == "replace" and high - low == last - first):
            for offset in range(high - low):
                mapping[low + offset] = first + offset
    return mapping


def place_script_words(heard: list[dict[str, Any]], scenes: list[str], timing: list[tuple[float, float]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Give the writer's words a clock. Returns ``(words, sentences)`` on the project clock.

    ``heard`` are words a recognizer timed on a voice track. Briefcut makes no
    narration, so today it is empty and every scene gets evenly timed sentences.
    Where a heard word lines up with the script, the script's spelling and scene
    win; the time only moves to stay in that scene.
    """
    written = [word for text in scenes for word in text.split()]
    owners = [index for index, text in enumerate(scenes) for _ in text.split()]
    words: list[dict[str, Any]] = []
    cues: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    scene = 0
    filled: set[int] = set()

    def close() -> None:
        if current:
            cues.append({"start": current[0]["start"], "end": current[-1]["end"], "text": " ".join(word["text"] for word in current)})
            current.clear()

    for word, place in zip(heard, _word_map(heard, written)):
        if place is not None and owners[place] != scene:
            close()
            scene = owners[place]
        low, high = timing[scene]
        start = min(max(float(word["start"]), low), high)
        placed = dict(word, start=round(start, 3), end=round(min(max(float(word["end"]), start + 0.01), high), 3))
        if place is not None:
            placed["text"] = written[place]
        if placed["end"] <= placed["start"]:
            continue
        words.append(placed)
        current.append(placed)
        filled.add(scene)
        if _SENTENCE_CLOSE.search(str(placed["text"])):
            close()
    close()
    for index, text in enumerate(scenes):
        if index not in filled:
            cues.extend(scene_cues(text, *timing[index]))
    cues.sort(key=lambda cue: cue["start"])
    return words, cues
