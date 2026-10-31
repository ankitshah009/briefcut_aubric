"""Make the English brief drive the campaign edit.

``edit_decisions`` is pure: it reads the goal with the deterministic intent
parser and returns compile ops to insert after the story assembly (and before
captions), plus one plain sentence per applied decision and one per request
that could not be honoured. Unknown words are ignored silently; that is the
parser's design. Text is passed through untouched — the compile layer escapes.
"""

from __future__ import annotations

import re
import textwrap
from typing import Any

from vedit.agent.intent import parse_intent

TITLE_SECONDS = 2.5
HOOK_SECONDS = 3.0
CTA_SECONDS = 3.0
FADE_SECONDS = 0.5
PAUSE_SECONDS = 0.6
PAUSE_THRESHOLD_DB = -32.0
PAUSE_PAD = 0.12
MAX_TITLE_CHARS = 80
MAX_ITEMS = 12
MAX_ITEM_CHARS = 200
_WRAP_AT = 26

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


def _one_line(text: str, limit: int) -> str:
    cleaned = " ".join(str(text).split())
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1].rstrip() + "…"
    return cleaned


def _wrap(text: str, width: int = _WRAP_AT) -> str:
    """Break a long card into at most three lines; drawtext renders newlines."""
    lines = textwrap.wrap(text, width=width, break_long_words=True, max_lines=3, placeholder="…")
    return "\n".join(lines) if lines else text


def _font_size(text: str, canvas_width: int, ratio: float, *, floor: int = 30) -> int:
    longest = max((len(line) for line in text.split("\n")), default=1) or 1
    wanted = round(canvas_width * ratio)
    fits = int(0.86 * canvas_width / (0.56 * longest))
    return max(floor, min(wanted, fits))


def _quote(text: str) -> str:
    return "‘" + text.replace("\n", " ") + "’"


def edit_decisions(
    goal: str,
    *,
    plan: dict[str, Any],
    has_audio: bool,
    canvas: tuple[int, int] | None = None,
) -> dict[str, Any]:
    """Return ``{"ops": [...], "decisions": [...], "ignored": [...], "loudnorm": bool}``.

    ``ops`` go after the assembled cut and before captions: pause trimming
    first (captions are then transcribed from the trimmed clip), then picture
    filters and timed text, then fades. ``loudnorm`` is False when the brief
    asks to keep the original loudness.
    """
    text = str(goal or "")
    width, height = canvas if canvas else (1080, 1920)
    width, height = max(16, int(width)), max(16, int(height))
    intent = parse_intent(text)
    ops: list[dict[str, Any]] = []
    decisions: list[str] = []
    ignored: list[str] = []

    wants_cut = _CUT_PAUSES.search(text) is not None and intent.cut_silence
    cut_applied = False
    if wants_cut:
        if has_audio:
            ops.append({"op": "cut_silence", "threshold_db": PAUSE_THRESHOLD_DB, "min_silence": PAUSE_SECONDS, "pad": PAUSE_PAD})
            decisions.append(f"Removed pauses longer than {PAUSE_SECONDS:g} s inside the selected moments")
            cut_applied = True
        else:
            ignored.append("Pause trimming: this footage has no audio track to find pauses in")

    if intent.denoise:
        ops.append({"op": "denoise", "strength": 3})
        decisions.append("Reduced picture noise")

    title_text = intent.title
    if title_text is None:
        quoted = _TITLE_QUOTED.search(text)
        if quoted and quoted.group(2).strip():
            title_text = quoted.group(2)
        elif _TITLE_CARD.search(text):
            title_text = str(plan.get("title") or "")
    if title_text is not None:
        title_text = _one_line(title_text, MAX_TITLE_CHARS)
        if title_text:
            card = _wrap(title_text)
            ops.append({
                "op": "title", "text": card, "start": 0, "duration": TITLE_SECONDS,
                "y_ratio": 0.5, "size": _font_size(card, width, 0.075, floor=36), "box": True,
            })
            decisions.append(f"Title card {_quote(title_text)} for {TITLE_SECONDS:g} s")
        else:
            ignored.append("Title card: the brief and the plan give no title text")

    if intent.hook:
        hook = _one_line(intent.hook, MAX_TITLE_CHARS)
        start = TITLE_SECONDS if title_text else 0.0
        ops.append({
            "op": "title", "text": _wrap(hook, 30), "start": start, "duration": HOOK_SECONDS,
            "y_ratio": 0.2, "size": _font_size(_wrap(hook, 30), width, 0.046), "box": True,
        })
        decisions.append(f"Hook {_quote(hook)} for {HOOK_SECONDS:g} s from {start:g} s")

    if intent.cta:
        cta = _one_line(intent.cta, MAX_TITLE_CHARS)
        length = plan.get("duration")
        if cut_applied:
            ignored.append(f"Call to action {_quote(cta)}: end-timed text needs the final length, which pause trimming decides at render")
        elif not isinstance(length, (int, float)) or isinstance(length, bool) or length <= 0:
            ignored.append(f"Call to action {_quote(cta)}: the story length is not known yet")
        else:
            start = max(0.0, float(length) - CTA_SECONDS)
            ops.append({
                "op": "title", "text": _wrap(cta, 30), "start": round(start, 3), "duration": CTA_SECONDS,
                "y_ratio": 0.7, "size": _font_size(_wrap(cta, 30), width, 0.046), "box": True,
            })
            decisions.append(f"Call to action {_quote(cta)} for the last {CTA_SECONDS:g} s")

    fade_in = _FADE_IN.search(text) is not None
    fade_out = _FADE_OUT.search(text) is not None
    if intent.fade and not fade_in and not fade_out and _FADE_ANY.search(text):
        fade_in = fade_out = True
    if fade_in:
        ops.append({"op": "fade_in", "duration": FADE_SECONDS})
        decisions.append(f"Fade in {FADE_SECONDS:g} s")
    if fade_out:
        ops.append({"op": "fade_out", "duration": FADE_SECONDS})
        decisions.append(f"Fade out {FADE_SECONDS:g} s")

    loudnorm = _NO_LOUDNORM.search(text) is None
    if not loudnorm and has_audio:
        decisions.append("Kept the original loudness (no normalization)")

    if intent.watermark:
        ignored.append("Watermark: the campaign render takes one video; upload the logo as a separate step")
    if intent.music:
        ignored.append("Music bed: the campaign render takes one video; add music as a separate step")

    return {
        "ops": ops,
        "decisions": [_one_line(item, MAX_ITEM_CHARS) for item in decisions[:MAX_ITEMS]],
        "ignored": [_one_line(item, MAX_ITEM_CHARS) for item in ignored[:MAX_ITEMS]],
        "loudnorm": loudnorm,
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
