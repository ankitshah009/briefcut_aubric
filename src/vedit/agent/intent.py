"""Deterministic English goal → Intent. Unknown words are ignored."""

from __future__ import annotations

import re
from pathlib import Path

from vedit.agent.model import Intent

_AUDIO_EXT = frozenset({".mp3", ".wav", ".m4a", ".aac", ".flac", ".aiff", ".aif"})
_IMAGE_EXT = frozenset({".png", ".jpg", ".jpeg", ".webp"})
_MEDIA_EXT = "aiff|aif|flac|jpeg|webp|m4a|m4v|webm|mkv|mp4|mov|avi|mp3|wav|aac|png|jpg"
_PUNCT = "\"'`()[]{}<>,;:!?“”‘’"
_PATH_RE = re.compile(
    rf"[^\s\"'`<>()\[\],;!?]+(?:/|\\)[^\s\"'`<>()\[\],;!?]+"
    rf"|[^\s\"'`<>()\[\],;!?]+\.(?:{_MEDIA_EXT})\b",
    re.IGNORECASE,
)

_PLATFORM_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "shorts",
        # The plural, the YouTube product name, or the singular used as a noun ("a short under 30s").
        # The adjective ("a short product video") says nothing about the frame.
        re.compile(
            r"\bshorts\b|\byoutube\s+shorts?\b|\breels\b|\btiktok\b|\bvertical\b"
            r"|(?<!\d)9:16(?!\d)|(?<!\d)9x16(?!\d)"
            r"|\bshort\b(?=\s*(?:(?:under|of|from|for|about|with|at|in|less|max)\b|\d|$|[,.;:!?]))",
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
_CAPTION_WORD = r"(?:captions?|subtitles?|subs|transcripts?|transcription)"
_NO_CAPTIONS = re.compile(
    rf"\b(?:no|without|skip|drop|remove)\s+(?:the\s+|any\s+)?{_CAPTION_WORD}\b"
    rf"|\b(?:don['’]?t|do\s+not|dont|never)\s+(?:add|want|burn|need|include|put|show|use)\b[^.,;\n]{{0,40}}?\b{_CAPTION_WORD}\b"
    rf"|\b{_CAPTION_WORD}\s+(?:off|disabled)\b",
    re.IGNORECASE,
)
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


def parse_intent(goal: str, *, sources: dict[str, str] | None = None, primary: str | None = None) -> Intent:
    """``primary`` names the source being edited, so it is never also picked as the music bed."""
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
        music=_music(text, src, primary),
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
    return platform != "preview" and not _KEEP_SILENCE.search(text)


def _captions(text: str, platform: str) -> bool:
    """An explicit no wins, then an explicit yes; otherwise every platform but a preview is captioned."""
    if _NO_CAPTIONS.search(text):
        return False
    if _YES_CAPTIONS.search(text):
        return True
    return platform != "preview"


def _loudnorm(text: str, platform: str) -> bool:
    return platform != "preview" and not _NO_LOUDNORM.search(text)


def _highpass(text: str, platform: str) -> bool:
    return platform != "preview" and not _NO_HIGHPASS.search(text)


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


def _same_file(candidate: str, primary_path: str | None) -> bool:
    """Whether a token names the primary source: the same path, or the same file name from the goal."""
    if not primary_path:
        return False
    if candidate == primary_path:
        return True
    try:
        if Path(candidate).expanduser().resolve() == Path(primary_path).expanduser().resolve():
            return True
    except OSError:
        pass
    return Path(candidate).name == Path(primary_path).name


def _music(text: str, sources: dict[str, str], primary: str | None = None) -> str | None:
    if primary is None and "main" in sources:
        primary = "main"  # the inventory's name for the source being edited
    primary_path = (str(sources.get(primary, "")).strip() or None) if primary else None
    if "music" in sources and str(sources["music"]).strip() and primary != "music":
        return str(sources["music"])
    for token in _paths(text):
        if _ext(token) in _AUDIO_EXT and not _same_file(token, primary_path):
            return token
    for name, value in sources.items():
        raw = str(value).strip()
        if name == primary or _same_file(raw, primary_path):
            continue  # an audio-only primary is the program, not a bed to mix under itself
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
