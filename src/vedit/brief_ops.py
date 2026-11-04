"""Make the English brief drive the campaign edit.

``brief_edit`` is pure: it reads the goal with the deterministic intent parser
and returns edit settings (the same ``plan["edit"]`` a creator changes in
Studio), plus one plain sentence per applied decision and one per request that
could not be honoured. Unknown words are ignored silently; that is the parser's
design. Text is passed through untouched; the compile layer escapes it.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from vedit.agent.intent import parse_intent
from vedit.edit_spec import merge_edit
from vedit.timeline import PAUSE_SECONDS, plan_timeline

TITLE_SECONDS = 2.5
HOOK_SECONDS = 3.0
CTA_SECONDS = 3.0
FADE_SECONDS = 0.5
MAX_TITLE_CHARS = 80
MAX_ITEMS = 12
MAX_ITEM_CHARS = 200

_CUT_PAUSES = re.compile(
    r"\b(?:trim|cut|remove|drop|tighten|strip|lose|kill|skip|no)\b(?:\s+(?:the|all|any|out|up|those|long|long\s+|awkward|dead))*"
    r"\s*(?:pauses?|silences?|silent\s+parts?|dead\s+air|gaps|dead\s+space)\b"
    r"|\bcut\s+silence\b|\bsilence\s+cut\b|\bjump\s*-?\s*cut\s+the\s+(?:pauses|silence)\b",
    re.IGNORECASE,
)
_FADE_IN = re.compile(r"\bfade\s*-?\s*in\b|\bfade\s+(?:up\s+)?(?:from\s+black\s+)?(?:at\s+the\s+)?(?:start|open|opening|beginning)\b", re.IGNORECASE)
_FADE_OUT = re.compile(r"\bfade\s*-?\s*out\b|\bfade\s+(?:to\s+black\s+)?(?:at\s+the\s+)?(?:end|ending|close)\b|\bfade\s+to\s+black\b", re.IGNORECASE)
_FADE_ANY = re.compile(r"\bfades?\b", re.IGNORECASE)
_TITLE_CARD = re.compile(
    r"\btitle\s*-?\s*cards?\b|\b(?:add|with|put|show|include|open(?:\s+with)?|start\s+with)\s+(?:a|an|the|its|our|my)?\s*(?:short\s+|quick\s+|simple\s+)?title\b"
    r"|\btitle\s+(?:at|on)\s+the\s+(?:start|top|open|beginning)\b|\btitled\b",
    re.IGNORECASE,
)
_TITLE_QUOTED = re.compile(r"\btitle(?:\s*-?\s*card)?\s*[:=-]?\s*([\"'\u201c\u2018])(.*?)[\"'\u201d\u2019]", re.IGNORECASE | re.DOTALL)
_NO_LOUDNORM = re.compile(
    r"\bno\s+loudnorm\b|don['’]t\s+normali[sz]e\b|\bdont\s+normali[sz]e\b|\bdo\s+not\s+normali[sz]e\b|\bno\s+(?:loudness\s+)?normali[sz]ation\b|\bkeep\s+the\s+original\s+(?:loudness|levels?)\b",
    re.IGNORECASE,
)


# Only words that name a frame: "landscape" and "portrait" are often the subject.
_SHAPE_WORDS = (
    ("1:1", re.compile(r"\b1:1\b|\bsquare\s+(?:clip|video|cut|crop|edit|format|frame|post|version)\b", re.I)),
    ("9:16", re.compile(r"\b9:16\b|\bvertical\b", re.I)),
    ("16:9", re.compile(r"\b16:9\b|\bwidescreen\b|\bhorizontal\b", re.I)),
)


def shape_mismatch(goal: str, aspect: str) -> str | None:
    """One sentence when the goal names a frame shape the project is not set to."""
    asked = [shape for shape, words in _SHAPE_WORDS if words.search(goal)]
    if len(asked) == 1 and asked[0] != aspect:
        return f"Your goal asks for {asked[0]}, but this project is set to {aspect}. Change the frame shape if you meant {asked[0]}."
    return None


def _one_line(text: str, limit: int) -> str:
    cleaned = " ".join(str(text).split())
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1].rstrip() + "…"
    return cleaned


def card_text(title: str) -> str:
    """The words of a title card made from a story title."""
    return _one_line(title, MAX_TITLE_CHARS)


def _quote(text: str) -> str:
    return "‘" + text.replace("\n", " ") + "’"


def _pause_seconds(plan: dict[str, Any], edit: dict[str, Any]) -> float:
    """Source seconds the edit cuts from inside the moments, so a receipt never claims a cut that found nothing."""
    view = plan_timeline(dict(plan, edit=edit))
    whole = sum(item["end"] - item["start"] for item in view["segments"] if isinstance(item.get("start"), (int, float)) and isinstance(item.get("end"), (int, float)))
    return round(whole - sum(span.source_end - span.source_start for span in view["spans"]), 3)


def _has_text(edit: dict[str, Any], words: str) -> bool:
    return any(isinstance(item, dict) and item.get("text") == words for item in edit.get("texts") or [])


def brief_edit(
    goal: str,
    *,
    plan: dict[str, Any],
    has_audio: bool,
    alongside: dict[str, Any] | None = None,
    final: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return ``{"edit": {...}, "decisions": [...], "ignored": [...]}``.

    ``edit`` is a partial edit holding what the brief asked for. ``alongside`` is
    what a template and the request add on top of it, which only matters here for
    timing text against the end of the story. ``final`` is the approved edit: each
    sentence is then checked against what will really render, so a request the
    creator changed in the editor is reported that way and not as applied.
    """
    text = str(goal or "")
    intent = parse_intent(text)
    settings: dict[str, Any] = {}
    items: list[dict[str, Any]] = []
    decisions: list[str] = []
    ignored: list[str] = []

    def settle(sentence: str, label: str, honoured: Callable[[dict[str, Any]], Any]) -> None:
        if final is None or honoured(final):
            decisions.append(sentence)
        else:
            ignored.append(f"{label}: changed in the editor")

    if _CUT_PAUSES.search(text) is not None and intent.cut_silence:
        if has_audio:
            settings["trim_pauses"] = True
            found = _pause_seconds(plan, merge_edit(settings, alongside or {}) if final is None else final) > 0
            sentence = f"Removed pauses longer than {PAUSE_SECONDS:g} s inside the selected moments" if found else f"Found no pauses longer than {PAUSE_SECONDS:g} s inside the selected moments"
            settle(sentence, "Pause trimming", lambda chosen: chosen.get("trim_pauses"))
        else:
            ignored.append("Pause trimming: this footage has no audio track to find pauses in")

    if intent.denoise:
        settings["denoise_picture"] = True
        settle("Reduced picture noise", "Picture noise reduction", lambda chosen: chosen.get("denoise_picture"))

    title_text = intent.title
    if title_text is None:
        quoted = _TITLE_QUOTED.search(text)
        if quoted and quoted.group(2).strip():
            title_text = quoted.group(2)
        elif _TITLE_CARD.search(text):
            title_text = str(plan.get("title") or "")
    if title_text is not None:
        title_text = card_text(title_text)
        if title_text:
            items.append({"text": title_text, "start": 0.0, "duration": TITLE_SECONDS, "style": "title", "position": "center"})
            settle(f"Title card {_quote(title_text)} for {TITLE_SECONDS:g} s", "Title card", lambda chosen: _has_text(chosen, title_text))
        else:
            ignored.append("Title card: the brief and the plan give no title text")

    if intent.hook:
        hook = _one_line(intent.hook, MAX_TITLE_CHARS)
        start = TITLE_SECONDS if title_text else 0.0
        items.append({"text": hook, "start": start, "duration": HOOK_SECONDS, "style": "label", "position": "top"})
        settle(f"Hook {_quote(hook)} for {HOOK_SECONDS:g} s from {start:g} s", "Hook", lambda chosen: _has_text(chosen, hook))

    if intent.cta:
        cta = _one_line(intent.cta, MAX_TITLE_CHARS)
        clock = final if final is not None else merge_edit(settings, alongside or {})
        length = plan_timeline(dict(plan, edit=clock))["duration"]
        if length <= 0:
            ignored.append(f"Call to action {_quote(cta)}: the story length is not known yet")
        else:
            items.append({"text": cta, "start": round(max(0.0, length - CTA_SECONDS), 3), "duration": min(CTA_SECONDS, length), "style": "callout", "position": "bottom"})
            settle(f"Call to action {_quote(cta)} for the last {CTA_SECONDS:g} s", "Call to action", lambda chosen: _has_text(chosen, cta))

    if items:
        settings["texts"] = items

    fade_in = _FADE_IN.search(text) is not None
    fade_out = _FADE_OUT.search(text) is not None
    if intent.fade and not fade_in and not fade_out and _FADE_ANY.search(text):
        fade_in = fade_out = True
    if fade_in:
        settings.setdefault("fade", {})["in"] = FADE_SECONDS
        settle(f"Fade in {FADE_SECONDS:g} s", "Fade in", lambda chosen: (chosen.get("fade") or {}).get("in"))
    if fade_out:
        settings.setdefault("fade", {})["out"] = FADE_SECONDS
        settle(f"Fade out {FADE_SECONDS:g} s", "Fade out", lambda chosen: (chosen.get("fade") or {}).get("out"))

    if _NO_LOUDNORM.search(text) is not None and has_audio:
        settings["audio"] = {"keep_loudness": True}
        settle("Kept the original loudness (no normalization)", "Original loudness", lambda chosen: (chosen.get("audio") or {}).get("keep_loudness"))

    if intent.watermark:
        ignored.append("Watermark: the campaign render takes one video; upload the logo as a separate step")
    if intent.music:
        ignored.append("Music bed: the campaign render takes one video; add music as a separate step")

    return {
        "edit": settings,
        "decisions": [_one_line(item, MAX_ITEM_CHARS) for item in decisions[:MAX_ITEMS]],
        "ignored": [_one_line(item, MAX_ITEM_CHARS) for item in ignored[:MAX_ITEMS]],
    }


def bounded_strings(value: Any, *, limit: int = MAX_ITEMS, chars: int = MAX_ITEM_CHARS) -> list[str]:
    """Display-only list of sentences from an untrusted plan payload."""
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip() and not any(ord(ch) < 32 and ch not in "\n\t" for ch in item):
            out.append(_one_line(item, chars))
        if len(out) >= limit:
            break
    return out
