"""Deterministic English goal → Intent. Unknown words are ignored."""

from __future__ import annotations

import re
from pathlib import Path

from vedit.agent.model import Intent

_AUDIO_EXT = frozenset({".mp3", ".wav", ".m4a", ".aac", ".flac", ".aiff", ".aif"})
_IMAGE_EXT = frozenset({".png", ".jpg", ".jpeg", ".webp"})
_MEDIA_EXT = (
    "aiff|aif|flac|jpeg|webp|m4a|m4v|webm|mkv|mp4|mov|avi|mp3|wav|aac|png|jpg"
)
_PUNCT = "\"'`()[]{}<>,;:!?“”‘’"
_PATH_RE = re.compile(
    rf"[^\s\"'`<>()\[\],;!?]+(?:/|\\)[^\s\"'`<>()\[\],;!?]+"
    rf"|[^\s\"'`<>()\[\],;!?]+\.(?:{_MEDIA_EXT})\b",
    re.IGNORECASE,
)

_PLATFORM_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "shorts",
        re.compile(
            r"\bshorts?\b|\breels\b|\btiktok\b|\bvertical\b|(?<!\d)9:16(?!\d)|(?<!\d)9x16(?!\d)",
            re.IGNORECASE,
        ),
    ),
    ("podcast", re.compile(r"\bpodcast\b|\binterview\b", re.IGNORECASE)),
    ("linkedin", re.compile(r"\blinkedin\b", re.IGNORECASE)),
    ("preview", re.compile(r"\bpreview\b|rough\s+cut|\bdraft\b", re.IGNORECASE)),
    (
        "square",
        re.compile(r"\bsquare\b|(?<!\d)1:1(?!\d)|(?<!\d)1x1(?!\d)", re.IGNORECASE),
    ),
)

_DUR_PREFIX = re.compile(
    r"\b(?:under|less\s+than|at\s+most|max)\s+(\d+(?:\.\d+)?)\s*(?:seconds|second|secs|sec|s)\b",
    re.IGNORECASE,
)
_DUR_WORD = re.compile(r"\b(\d+(?:\.\d+)?)\s*-?\s*seconds?\b", re.IGNORECASE)
_DUR_BARE = re.compile(r"\b(\d+(?:\.\d+)?)(?:secs|sec|s)\b", re.IGNORECASE)

_KEEP_SILENCE = re.compile(
    r"keep\s+silence\b|keep\s+the\s+pauses\b|don['’]t\s+cut\b|\bdont\s+cut\b|\bdo\s+not\s+cut\b|\bslideshow\b",
    re.IGNORECASE,
)
_NO_CAPTIONS = re.compile(r"\bno\s+captions?\b|\bwithout\s+captions?\b", re.IGNORECASE)
_YES_CAPTIONS = re.compile(r"caption|subtitle|transcri", re.IGNORECASE)
_NO_LOUDNORM = re.compile(
    r"\bno\s+loudnorm\b|don['’]t\s+normalize\b|\bdont\s+normalize\b|\bdo\s+not\s+normalize\b",
    re.IGNORECASE,
)
_NO_HIGHPASS = re.compile(r"\bno\s+highpass\b", re.IGNORECASE)
_DENOISE = re.compile(r"denoise|noisy|noise\s+reduction", re.IGNORECASE)
_FADE = re.compile(r"fade\s+in|fade\s+out|\bfade\b", re.IGNORECASE)
_TITLE = re.compile(r"\btitle\b\s*[:=-]?\s*([\"'])(.*?)\1", re.IGNORECASE | re.DOTALL)
_HOOK = re.compile(r"\bhook\s*:\s*([^,\n]*)", re.IGNORECASE)
_CTA = re.compile(r"(?:\bcta\s*:|\bcall\s+to\s+action\b\s*:?)\s*([^,\n]*)", re.IGNORECASE)
_MARK = re.compile(r"\b(?:watermark|logo)\b", re.IGNORECASE)


def parse_intent(goal: str, *, sources: dict[str, str] | None = None) -> Intent:
    text = goal.strip()
    src = sources or {}
    platform = _platform(text)
    watermark = _watermark(text, src)
    return Intent(
        goal=text,
        platform=platform,
        max_duration=_max_duration(text),
        cut_silence=_cut_silence(text, platform),
        captions=_captions(text, platform),
        loudnorm=_loudnorm(text, platform),
        music=_music(text, src),
        title=_quoted(_TITLE, text),
        hook=_phrase(_HOOK, text),
        cta=_phrase(_CTA, text),
        reframe=_reframe(platform),
        denoise=_DENOISE.search(text) is not None,
        highpass=_highpass(text, platform),
        fade=_FADE.search(text) is not None,
        watermark=watermark,
        images=_images(text, watermark),
        name=_name(text),
    )


def _platform(text: str) -> str:
    for name, pattern in _PLATFORM_RULES:
        if pattern.search(text):
            return name
    return "youtube"


def _reframe(platform: str) -> str | None:
    if platform == "shorts":
        return "9:16"
    if platform == "square":
        return "1:1"
    if platform == "preview":
        return None
    return "16:9"


def _max_duration(text: str) -> float | None:
    for pattern in (_DUR_PREFIX, _DUR_WORD, _DUR_BARE):
        match = pattern.search(text)
        if match:
            return float(match.group(1))
    return None


def _cut_silence(text: str, platform: str) -> bool:
    if platform == "preview":
        return False
    if _KEEP_SILENCE.search(text):
        return False
    return True


def _captions(text: str, platform: str) -> bool:
    if _NO_CAPTIONS.search(text):
        return False
    if _YES_CAPTIONS.search(text):
        return True
    if platform == "preview":
        return False
    return True


def _loudnorm(text: str, platform: str) -> bool:
    if platform == "preview" or _NO_LOUDNORM.search(text):
        return False
    return True


def _highpass(text: str, platform: str) -> bool:
    if platform == "preview" or _NO_HIGHPASS.search(text):
        return False
    return True


def _quoted(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    if not match:
        return None
    value = match.group(2).strip()
    return value or None


def _phrase(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    if not match:
        return None
    value = match.group(1).strip()
    return value or None


def _ext(path: str) -> str:
    return Path(path).suffix.lower()


def _clean_path(raw: str) -> str:
    token = raw.strip(_PUNCT)
    while token.endswith(".") and _ext(token) == ".":
        token = token[:-1]
    return token


def _paths(text: str) -> list[str]:
    found: list[str] = []
    for match in _PATH_RE.finditer(text):
        token = _clean_path(match.group(0))
        if token:
            found.append(token)
    return found


def _music(text: str, sources: dict[str, str]) -> str | None:
    if "music" in sources and str(sources["music"]).strip():
        return str(sources["music"])
    for token in _paths(text):
        if _ext(token) in _AUDIO_EXT:
            return token
    for value in sources.values():
        raw = str(value).strip()
        if raw and _ext(raw) in _AUDIO_EXT:
            return raw
    return None


def _watermark(text: str, sources: dict[str, str]) -> str | None:
    for key in ("logo", "watermark"):
        if key in sources and str(sources[key]).strip():
            return str(sources[key])
    mark = _MARK.search(text)
    if not mark:
        return None
    for token in _paths(text[mark.end() :]):
        if _ext(token) in _IMAGE_EXT:
            return token
    return None


def _images(text: str, watermark: str | None) -> tuple[str, ...]:
    found: list[str] = []
    seen: set[str] = set()
    for token in _paths(text):
        if _ext(token) not in _IMAGE_EXT:
            continue
        if watermark is not None and token == watermark:
            continue
        if token in seen:
            continue
        seen.add(token)
        found.append(token)
    return tuple(found)


def _name(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    slug = "-".join(words[:4])[:32].strip("-")
    return slug or "edit"
