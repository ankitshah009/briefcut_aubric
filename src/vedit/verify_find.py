"""Search that two Meta models check: SAM 3.1 looks for what Muse Spark found.

A JSON schema guarantees that a search answer parses, not that it is right.
Muse Spark's ``find`` always returns well-formed moments; whether the thing the
user asked for is on screen there is a separate question, and SAM 3.1 can
answer it with pixels.

For a query that names a visible thing ("the red mug", "hands sanding wood"),
each found moment gets one short window of the clip, sampled so the window
fits inside SAM's 50-frame minimum bill. SAM masks the named thing, and the
evidence is the share of sampled frames that hold a mask larger than a small
part of the picture. A moment SAM never saw is flagged by default and ranked
last (or dropped, and then still listed under ``dropped`` with its reason).

Spoken queries ("where I talk about pricing") and queries that name no
visible thing skip SAM and say so: SAM sees pictures, so a missing mask proves
nothing about what was said.

Only public functions do the work. ``meta.subject_masks`` makes the paid call
through the project's ledger and cache, ``meta.read_mask`` decodes with Meta's
public decoder, and ``matte.paste`` places a mask on the frame. No model but
the two Meta models is involved. The noun phrase is Muse Spark's ``subject``
from the same search call when it names one, and otherwise a small fixed rule.
"""

from __future__ import annotations

import hashlib
import math
import multiprocessing
import re
import tempfile
import unicodedata
import uuid
from collections.abc import Iterable
from concurrent.futures import BrokenExecutor, ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from typing import Any

from vedit import meta
# Model ids and prices live in one place. Only constants are read; masks go only through meta.read_mask.
from vedit.meta import _DIRECTOR_LABELS, _FRAME_PRICE, _SAM_MODEL
from vedit.errors import VeditError
from vedit.matte import Area, paste
from vedit.media import probe, run_ffmpeg

SAM_MODEL = _SAM_MODEL
FOUND_BY = _DIRECTOR_LABELS["spark"]
MAX_MOMENTS = 5
WINDOW_SECONDS = 3.0
MIN_WINDOW_SECONDS = 1.0
# SAM bills at least 50 frames a call (meta.py bills max(50, frames)), and the request reserves two frames of
# head room, so 46 sampled frames is the most a window can hold for the minimum.
MAX_FRAMES = 46
BILLED_MINIMUM = 50
FRAME_PRICE = _FRAME_PRICE
MAX_SAMPLE_FPS = 15.0
WINDOW_HEIGHT = 540  # SAM bills frames, not pixels, so a small or distant subject gets the larger picture for free
MIN_AREA = 0.001
MIN_COVERAGE = 0.2
UNSEEN = ("drop", "flag")
OUTLINE = "0x39FF14"
_DECODE_INLINE = 8

# --- The noun phrase: a fixed rule, no model -------------------------------------------------

_SPOKEN = re.compile(
    r"\b(?:talk(?:s|ed|ing)?|say(?:s|ing)?|said|mention(?:s|ed|ing)?|discuss(?:es|ed|ing)?|explain(?:s|ed|ing)?|"
    r"speak(?:s|ing)?|spoke|tell(?:s|ing)?|told|ask(?:s|ed|ing)?|describ(?:e|es|ed|ing)|quot(?:e|es|ed|ing)|"
    r"joke(?:s|d)?|joking|thank(?:s|ed|ing)?|introduc(?:e|es|ed|ing)|answer(?:s|ed|ing)?|"
    r"read(?:s|ing)? out|words?|phrase|sentence|transcript|subtitles?|captions?)\b")
_QUOTED = re.compile(r"[\"“”«»]|(?:^|\s)'[^']+'(?:\s|$)")
_HEARD = {"music", "song", "sound", "sounds", "noise", "silence", "voice", "voiceover", "narration", "laugh", "laughs",
          "laughter", "applause", "clapping", "audio", "beat", "beats", "cheer", "cheering", "scream", "screaming"}
_ABSTRACT = {"moment", "moments", "part", "parts", "bit", "bits", "section", "sections", "segment", "segments", "scene",
             "scenes", "shot", "shots", "clip", "clips", "footage", "frame", "frames", "intro", "introduction", "outro",
             "ending", "end", "beginning", "start", "middle", "highlight", "highlights", "best", "worst", "funny",
             "funniest", "exciting", "interesting", "important", "good", "great", "key", "climax", "transition",
             "transitions", "idea", "ideas", "topic", "topics", "story", "point", "points", "time", "times", "pricing",
             "price", "prices", "cost", "costs", "reason", "reasons", "summary", "recap", "conclusion", "anything",
             "something", "everything", "nothing", "stuff", "action"}
_DETERMINERS = {"the", "a", "an", "this", "that", "these", "those", "my", "our", "your", "his", "her", "its", "their",
                "some", "any", "all", "every", "each", "one", "two", "three"}
_PRONOUNS = {"i", "me", "you", "we", "us", "he", "him", "she", "they", "them", "someone", "somebody", "anyone",
             "anybody", "everyone", "everybody"}
# After "someone waving" a preposition starts a place, not the thing; a particle ends a phrasal verb ("pick up").
_PREPOSITIONS = {"with", "on", "in", "at", "near", "under", "over", "beside", "behind", "inside", "from", "into", "onto", "for",
                 "to", "toward", "towards", "across", "through", "around", "by", "of", "about"}
_PARTICLES = {"up", "down", "out", "off", "away", "back"}
# "something red" or "red things" names a colour, and SAM needs a thing to outline: "red object".
_VAGUE = {"something", "anything", "thing", "things", "stuff", "object", "objects"}
_COLOURS = {"red", "orange", "yellow", "green", "blue", "purple", "violet", "pink", "brown", "black", "white", "gray", "grey",
            "silver", "gold", "golden", "beige", "teal", "cyan", "magenta", "maroon", "navy", "turquoise", "dark", "light", "bright"}
# Common words of other languages written in Latin letters. SAM reads English names, so these searches need Spark's subject.
_FOREIGN = {"el", "los", "las", "una", "unos", "unas", "del", "donde", "cuando", "que", "por", "para", "les", "une", "où",
            "quand", "avec", "pour", "dans", "sur", "der", "das", "ein", "eine", "und", "wo", "wann", "mit", "für", "uma", "onde",
            "quando", "com", "gli", "dove", "della", "nel", "mujer", "hombre", "perro", "gato", "femme", "homme", "chien", "frau",
            "mann", "hund", "mulher", "homem", "cachorro"}
_PHRASE_FROM = {"muse-spark": "Muse Spark named", "caller": "The caller named"}
# Nouns that end in -ing and are not a verb splitting the phrase.
_ING_NOUNS = {"ring", "rings", "king", "wing", "string", "spring", "swing", "sling", "thing", "ceiling", "building",
              "painting", "drawing", "clothing", "pudding", "icing", "frosting", "wedding", "awning", "railing",
              "siding", "stuffing", "filling", "lighting", "sibling", "seedling", "earring", "bearing", "shaving",
              "shavings", "carving", "carvings", "lining", "fitting", "coating", "ending", "morning", "evening",
              "dumpling", "sapling", "duckling", "herring", "sterling", "darling", "railing", "skiing", "bowling",
              "parking", "housing", "packaging", "wiring", "plumbing", "flooring", "decking", "bedding", "sing"}
_LEAD = [
    re.compile(r"^(?:please\s+)?(?:find|show|search(?:\s+for)?|look(?:\s+for)?|locate|get|give|cut\s+to|jump\s+to|go\s+to)\s+(?:me\s+)?"),
    re.compile(r"^(?:i\s+(?:want|need)|can\s+(?:you|i)\s+(?:find|see|get))\s+"),
    re.compile(r"^(?:the\s+|a\s+|any\s+|all\s+|every\s+)?(?:moments?|parts?|bits?|times?|clips?|shots?|scenes?|footage|frames?|"
               r"sections?|segments?)\s+(?:of\s+|with\s+|where\s+|when\s+|that\s+(?:shows?\s+)?|showing\s+|featuring\s+|in\s+which\s+)?(?=\S)"),
    re.compile(r"^(?:where|when)\s+(?:(?:is|are|was|were|does|do|did)\s+|(?:you|we|i)\s+(?:can\s+)?see\s+|can\s+(?:i|you|we)\s+see\s+|"
               r"there\s+(?:is|are|was|were)\s+|there's\s+)?"),
    re.compile(r"^(?:there\s+(?:is|are)|there's)\s+"),
    # "a wide shot of the city", "close-up of hands": the framing is not the thing.
    re.compile(r"^(?:the\s+|a\s+|an\s+)?(?:extreme\s+|wide\s+|long\s+|medium\s+|aerial\s+)?(?:close-?\s?ups?|shots?|views?|pictures?|images?|photos?)\s+of\s+"),
]
_TRAIL = re.compile(r"\s+(?:appears?|appearing|shows?\s+up|showing\s+up|comes?\s+(?:in|on|into\s+view)|enters?|is\s+shown|are\s+shown|"
                    r"starts?|begins?|ends?|stops?|kicks?\s+in|drops?|plays?|"
                    r"is\s+visible|are\s+visible|visible|is|are|on\s+screen|in\s+(?:the\s+)?(?:frame|shot|video|clip|footage|picture))\b.*$")
_SPLIT = re.compile(r"\s+(?:with|on|in|at|near|under|over|beside|behind|inside|that|which|who|while|being|when|where|as|and|or|from|into|onto|for)\s+")


def _ing_verb(word: str) -> bool:
    return len(word) > 4 and word.endswith("ing") and word not in _ING_NOUNS


def _latin(text: str) -> bool:
    """True when every letter is a Latin letter, accented or not."""
    return all(unicodedata.name(char, "").startswith("LATIN") for char in text if char.isalpha())


def query_subject(query: str) -> dict[str, Any]:
    """What SAM should look for, by a fixed rule: ``{kind, phrase, reason}``.

    ``kind`` is ``visual`` with a short noun phrase for SAM, ``spoken`` when the
    query asks for something said or heard, or ``abstract`` when it names no
    visible thing, names only an action, or is not in English (SAM reads
    English names; Muse Spark's ``subject`` covers those searches). No model is
    asked: the same query always gives the same phrase.
    """
    text = " ".join(unicodedata.normalize("NFKC", str(query or "")).replace("’", "'").split())
    lowered = text.lower()
    if not lowered:
        return {"kind": "abstract", "phrase": None, "reason": "The search is empty, so SAM was not asked."}
    if _QUOTED.search(text) or _SPOKEN.search(lowered):
        return {"kind": "spoken", "phrase": None, "reason": "The search asks for something said, so SAM, which sees pictures, was not asked."}
    words = re.findall(r"[^\W_][\w'-]*", lowered)
    if not _latin(lowered) or any(word in _FOREIGN for word in words):
        return {"kind": "abstract", "phrase": None,
                "reason": "The search is not in English and no English subject was named for it, so SAM, which reads English names, was not asked."}
    phrase = " ".join(words)
    for _ in range(4):
        before = phrase
        for pattern in _LEAD:
            phrase = pattern.sub("", phrase)
        if phrase == before:
            break
    phrase = _TRAIL.sub("", phrase)
    tokens = phrase.split()
    while tokens and tokens[0] in _DETERMINERS:
        tokens.pop(0)
    if tokens and tokens[0] in {"about", "regarding"}:
        return {"kind": "abstract", "phrase": None, "reason": "The search names a topic, not a thing on screen, so SAM was not asked."}
    # "me holding the bowl" names the bowl: drop the person and the verb. "someone waving at the camera" names a person.
    if tokens and tokens[0] in _PRONOUNS:
        rest = tokens[2:] if len(tokens) > 2 and tokens[1] not in _PREPOSITIONS else []
        while rest and (rest[0] in _DETERMINERS or rest[0] in _PARTICLES):
            rest.pop(0)
        tokens = rest if rest and rest[0] not in _PREPOSITIONS else ["person"]
    phrase = _SPLIT.split(" " + " ".join(tokens) + " ", maxsplit=1)[0].strip()
    tokens = phrase.split()
    for index, word in enumerate(tokens):
        if index and _ing_verb(word):
            tokens = tokens[:index]
            break
    while tokens and tokens[0] in _DETERMINERS:
        tokens.pop(0)
    tokens = tokens[-4:]
    if len(tokens) == 1 and _ing_verb(tokens[0]):
        return {"kind": "abstract", "phrase": None, "reason": "The search names an action and nothing doing it, so SAM, which outlines things, was not asked."}
    # "something red", "red things", "red": SAM needs a thing to outline.
    colours = [word for word in tokens if word not in _VAGUE]
    if colours and all(word in _COLOURS for word in colours) and (len(colours) < len(tokens) or len(tokens) <= 2):
        tokens = colours + ["object"]
    if not tokens:
        return {"kind": "abstract", "phrase": None, "reason": "The search names no visible thing, so SAM was not asked."}
    if tokens[-1] in _HEARD or tokens[0] in _HEARD:
        return {"kind": "spoken", "phrase": None, "reason": "The search asks for a sound, so SAM, which sees pictures, was not asked."}
    if tokens[-1] in _ABSTRACT or all(word in _ABSTRACT for word in tokens):
        return {"kind": "abstract", "phrase": None, "reason": "The search names no visible thing, so SAM was not asked."}
    return {"kind": "visual", "phrase": " ".join(tokens), "reason": f"SAM looked for “{' '.join(tokens)}”."}


def named_subject(phrase: Any, *, by: str = "caller") -> dict[str, Any] | None:
    """A phrase someone else named for SAM, such as Muse Spark's ``subject``, checked like the rule's own. Pure.

    Returns ``{kind, phrase, reason}``, or None when the phrase is missing or
    empty, so the caller falls back to ``query_subject``. A named sound or an
    abstract word is not sent to SAM.
    """
    if not isinstance(phrase, str):
        return None
    clean = " ".join("".join(char for char in phrase if char.isprintable()).split())[:160]
    if not clean:
        return None
    last = clean.lower().split()[-1]
    named = _PHRASE_FROM.get(by, "The caller named")
    if last in _HEARD:
        return {"kind": "spoken", "phrase": None, "reason": f"{named} a sound, “{clean}”, so SAM, which sees pictures, was not asked."}
    if last in _ABSTRACT or last in _VAGUE:
        return {"kind": "abstract", "phrase": None, "reason": f"{named} “{clean}”, which is not a thing SAM can outline, so SAM was not asked."}
    return {"kind": "visual", "phrase": clean, "reason": f"{named} what SAM should look for: “{clean}”."}


# --- Windows, sampling and the evidence score -------------------------------------------------

def window_for(start: float, end: float, duration: float, *, seconds: float = WINDOW_SECONDS,
               minimum: float = MIN_WINDOW_SECONDS) -> tuple[float, float]:
    """One window per moment: its middle, at most ``seconds`` long and at least ``minimum``, inside the clip."""
    length = min(seconds, max(minimum, end - start), duration)
    middle = (start + end) / 2
    first = min(max(0.0, middle - length / 2), max(0.0, duration - length))
    return round(first, 3), round(min(duration, first + length), 3)


def sample_rate(window: float, source_fps: float, *, max_frames: int = MAX_FRAMES, ceiling: float = MAX_SAMPLE_FPS) -> float:
    """The densest sampling that keeps the window inside SAM's minimum bill."""
    if window <= 0 or source_fps <= 0:
        raise ValueError("A window needs a positive length and frame rate.")
    return round(max(1.0, min(source_fps, ceiling, max_frames / window)), 3)


def coverage(areas: Iterable[float], frames: int, *, min_area: float = MIN_AREA) -> float:
    """The share of ``frames`` sampled frames whose mask covers at least ``min_area`` of the picture."""
    if frames <= 0:
        return 0.0
    return round(min(frames, sum(1 for area in areas if area >= min_area)) / frames, 4)


def billed_usd(frames: int) -> float:
    """What one SAM call on ``frames`` frames reserves, as the adapter reserves it."""
    return round(max(BILLED_MINIMUM, frames + 2) * FRAME_PRICE, 8)


# --- Media: the sampled window and the thumbnail ----------------------------------------------

def _identity(source: Path) -> str:
    stat = source.stat()
    return f"{source}|{stat.st_size}|{stat.st_mtime_ns}"


def _window_clip(source: Path, start: float, end: float, fps: float, height: int, folder: Path) -> Path:
    """The sampled window as a small H.264 file. An existing one is reused, so SAM's cache stays warm."""
    digest = hashlib.sha256(f"{_identity(source)}|{start:.3f}|{end:.3f}|{fps:.3f}|{height}|v1".encode()).hexdigest()[:16]
    out = folder / f"window-{digest}-{round(start * 1000)}-{round(end * 1000)}.mp4"
    if out.is_file() and out.stat().st_size > 0:
        return out
    folder.mkdir(parents=True, exist_ok=True)
    pending = out.with_name(f"{out.stem}.{uuid.uuid4().hex[:8]}.part.mp4")  # moments run in parallel
    run_ffmpeg(["-loglevel", "error", "-ss", f"{start:.3f}", "-i", str(source), "-t", f"{end - start:.3f}",
                "-map", "0:v:0", "-an", "-sn", "-dn", "-vf", f"fps={fps:.3f},scale=-2:{height},format=yuv420p",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-movflags", "+faststart", str(pending)], timeout=120)
    pending.replace(out)
    return out


def _thumbnail(clip: Path, frame: int, mask: bytes | None, size: tuple[int, int], picture: tuple[int, int], out: Path) -> Path:
    """One sampled frame, with the mask's outline and a light fill drawn by FFmpeg when there is a mask."""
    width, height = picture
    pick = f"[0:v]select=eq(n\\,{frame}),setpts=PTS-STARTPTS,scale={width}:{height},format=rgba[f]"
    out.parent.mkdir(parents=True, exist_ok=True)
    if mask is None:
        run_ffmpeg(["-loglevel", "error", "-i", str(clip), "-filter_complex", pick.removesuffix(",format=rgba[f]") + ",format=yuvj420p",
                    "-frames:v", "1", "-q:v", "3", str(out)], timeout=60)
        return out
    with tempfile.NamedTemporaryFile(suffix=".gray", dir=out.parent, delete=False) as handle:
        handle.write(mask)
        raw = Path(handle.name)
    try:
        graph = ";".join([
            pick,
            f"[1:v]format=gray,scale={width}:{height}:flags=neighbor,split=3[m1][m2][m3]",
            "[m1]dilation,dilation[d]", "[m2]erosion,erosion[e]", "[d][e]blend=all_mode=difference[ring]",
            "[m3]lut=c0=val*0.16[tint]",
            f"color=c={OUTLINE}:s={width}x{height}:r=1:d=1,format=rgba,split[c1][c2]",
            "[c1][tint]alphamerge[fill]", "[c2][ring]alphamerge[edge]",
            "[f][fill]overlay=format=auto[a]", "[a][edge]overlay=format=auto,format=yuvj420p[out]",
        ])
        run_ffmpeg(["-loglevel", "error", "-i", str(clip), "-f", "rawvideo", "-pix_fmt", "gray", "-video_size", f"{size[0]}x{size[1]}",
                    "-i", str(raw), "-filter_complex", graph, "-map", "[out]", "-frames:v", "1", "-q:v", "3", str(out)], timeout=60)
    finally:
        raw.unlink(missing_ok=True)
    return out


# --- Masks: area per sampled frame, by Meta's public decoder ----------------------------------

def _decode_all(jobs: list[tuple[str, tuple[int, int]]]) -> list[tuple[bytes, int, int]]:
    """Decode masks in order. The decoder is pure Python, so a larger batch runs in processes."""
    if len(jobs) < _DECODE_INLINE:
        return [meta.read_mask(raw, span) for raw, span in jobs]
    workers = max(1, min(8, (multiprocessing.cpu_count() or 2) - 1))
    try:
        with ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn")) as pool:
            return list(pool.map(meta.read_mask, [raw for raw, _ in jobs], [span for _, span in jobs], chunksize=4))
    except (BrokenExecutor, OSError):
        return [meta.read_mask(raw, span) for raw, span in jobs]


def _entries(masks: dict[str, Any], *, min_confidence: float) -> list[dict[str, Any]]:
    """The selected object's mask records SAM was at least ``min_confidence`` sure of."""
    return [entry for entry in masks.get("frames") or [] if float(entry.get("confidence", 1.0)) >= min_confidence]


def _areas(masks: dict[str, Any], entries: list[dict[str, Any]], decoded: list[tuple[bytes, int, int]] | None) -> dict[int, float]:
    """Each sampled frame's mask area as a share of the picture.

    Without the optional decoder (``decoded`` is None) the box stands in for the
    mask, which can only overstate the area; the evidence says which was measured.
    """
    picture = int(masks.get("frame_width") or 0) * int(masks.get("frame_height") or 0)
    if picture <= 0:
        return {}
    areas: dict[int, float] = {}
    for at, entry in enumerate(entries):
        left, top, right, bottom = entry["box"]
        box = (right - left) * (bottom - top)
        if decoded is None:
            areas[int(entry["frame"])] = box / picture
            continue
        raster, columns, rows = decoded[at]
        areas[int(entry["frame"])] = raster.count(255) * box / max(1, columns * rows) / picture
    return areas


# --- The check --------------------------------------------------------------------------------

def _seen_by_spark(moment: dict[str, Any]) -> bool:
    return moment.get("source", "meta") in {"meta", "both"}


def _receipt(phrase: str | None, note: str, passed: bool, at: float | None, inputs: list[str]) -> dict[str, Any]:
    name = f"sam: “{phrase}” is on screen in the found moments" if phrase else "sam: found moments checked on screen"
    return {"name": name, "passed": passed, "at": at, "note": note, "basis": "derived", "by": SAM_MODEL, "inputs": inputs}


def _pass_through(moments: list[dict[str, Any]], note: str) -> list[dict[str, Any]]:
    return [dict(moment, verified=None, coverage=None, thumbnail=None, verification=note) for moment in moments]


def verify_moments(moments: list[dict[str, Any]], query: str, source: Path, work_dir: Path, budget: float, *,
                   phrase: str | None = None, max_moments: int = MAX_MOMENTS, window_seconds: float = WINDOW_SECONDS,
                   max_frames: int = MAX_FRAMES, min_area: float = MIN_AREA, min_coverage: float = MIN_COVERAGE,
                   min_confidence: float = 0.0, unseen: str = "flag", out_dir: Path | None = None, workers: int = 4,
                   inputs: list[str] | None = None, phrase_from: str = "caller") -> dict[str, Any]:
    """Check found moments with SAM 3.1 and return them annotated, plus what it cost.

    ``moments`` are ``meta.find_moments`` results on ``source``'s clock, best
    first; pass the same file ``find_moments`` read (in a project, the analysis
    proxy). ``work_dir`` is the project's Meta folder, so SAM's calls land on the
    same ledger, cap and cache as the search, and ``budget`` is that cap in USD.

    The first ``max_moments`` moments Muse Spark found each get one window of
    ``window_seconds`` around their middle, sampled to at most ``max_frames``
    frames so each call is billed SAM's 50-frame minimum. A moment is
    ``verified`` when its coverage reaches ``min_coverage``. With ``unseen``
    ``"flag"`` (the default) a moment with zero coverage stays, marked
    ``flagged`` and ranked after the verified ones; with ``"drop"`` it moves to
    ``dropped``. Flagging is the default because SAM can miss a real thing it
    has no good name for: live, it found no "gouge" in 45 frames that plainly
    show the gouge, and a drop would have hidden a correct result.
    Transcript moments, moments past the cap and spoken or abstract queries are
    returned unchanged with ``verified: None`` and a note saying why.

    ``phrase`` is what SAM looks for when someone named it, such as Muse
    Spark's ``subject`` from the same search (``phrase_from="muse-spark"``).
    Without one, the fixed rule in ``query_subject`` names it. The result says
    which under ``phrase_from``.

    Nothing is spent when the whole check would not fit in what is left of the
    budget: the result's ``status`` is ``refused`` and the moments are unchanged.
    """
    if unseen not in UNSEEN:
        raise ValueError("unseen must be 'drop' or 'flag'.")
    if not 1 <= max_moments <= 8 or not 1 <= max_frames <= BILLED_MINIMUM - 2:
        raise ValueError("max_moments must be 1 to 8 and max_frames 1 to 48.")
    work_dir = Path(work_dir).resolve()
    out_dir = Path(out_dir).resolve() if out_dir is not None else work_dir / "verified-find"
    moments = [dict(moment) for moment in moments if isinstance(moment, dict)]
    named = named_subject(phrase, by=phrase_from)
    subject = named or query_subject(query)
    source_of_phrase = (phrase_from if phrase_from in _PHRASE_FROM else "caller") if named else "rule"
    inputs = list(inputs) if inputs is not None else [FOUND_BY]
    settings = {"window_seconds": window_seconds, "max_frames": max_frames, "min_area": min_area, "min_coverage": min_coverage,
                "min_confidence": min_confidence, "unseen": unseen, "max_moments": max_moments}
    result: dict[str, Any] = {"query": query, "kind": subject["kind"], "phrase": subject["phrase"], "phrase_from": source_of_phrase, "status": "skipped",
                              "reason": subject["reason"], "moments": moments, "dropped": [], "spend": [], "spent_usd": 0.0,
                              "warnings": [], "settings": settings, "model": SAM_MODEL}
    if subject["kind"] != "visual":
        result["moments"] = _pass_through(moments, subject["reason"])
        result["receipt"] = _receipt(None, "Not checked: " + subject["reason"], True, None, inputs)
        return result
    phrase = subject["phrase"]
    order = [index for index, moment in enumerate(moments) if _seen_by_spark(moment)][:max_moments]
    if not order:
        note = "No moment found by looking at the footage, so SAM had nothing to check."
        result.update(reason=note, moments=_pass_through(moments, note))
        result["receipt"] = _receipt(phrase, "Not checked: " + note, True, None, inputs)
        return result
    if not meta.capabilities()["available"]:
        note = "SAM needs Meta, which is not connected right now."
        result.update(status="unavailable", reason=note, moments=_pass_through(moments, note))
        result["receipt"] = _receipt(phrase, "Not checked: " + note, True, None, inputs)
        return result

    info = probe(source)
    if not info.has_video or not info.video or not info.fps:
        raise meta.MetaError("SAM needs a clip with a picture to check.")
    duration, source_fps = float(info.duration), float(info.fps)
    height = min(WINDOW_HEIGHT, int(info.video.height or WINDOW_HEIGHT)) & ~1
    plans = []
    for index in order:
        moment = moments[index]
        first, last = window_for(float(moment["start"]), float(moment["end"]), duration, seconds=window_seconds)
        fps = sample_rate(last - first, source_fps, max_frames=max_frames)
        plans.append({"index": index, "window": (first, last), "fps": fps, "frames": max(1, math.ceil((last - first) * fps))})
    needed = round(sum(billed_usd(plan["frames"]) for plan in {(*plan["window"], plan["fps"]): plan for plan in plans}.values()), 8)
    remaining = float(meta.call_log(work_dir, budget_usd=budget)["totals"].get("remaining_usd", budget))
    settings["planned_usd"] = needed
    if needed > remaining + 1e-9:
        note = (f"Checking {len(plans)} moments with SAM needs up to ${needed:.4f}, and ${remaining:.4f} of the ${budget:.4f} budget is left. "
                "Nothing was sent. Raise the cap or check fewer moments.")
        result.update(status="refused", reason=note, moments=_pass_through(moments, note))
        result["receipt"] = _receipt(phrase, "Not checked: " + note, True, None, inputs)
        return result

    def look(plan: dict[str, Any]) -> dict[str, Any]:
        first, last = plan["window"]
        clip = _window_clip(Path(source), first, last, plan["fps"], height, work_dir / "verify-windows")
        made = probe(clip)
        masks = meta.subject_masks(clip, subject=phrase, duration=float(made.duration), work_dir=work_dir, budget_usd=budget,
                                   purpose=f"Check \u201c{phrase[:60]}\u201d in a found moment")
        return {"clip": clip, "sampled": max(1, round(float(made.duration) * float(made.fps or plan["fps"]))), "masks": masks}

    # Overlapping moments can share a window; one call answers all of them, and only the first pays.
    unique: dict[tuple[float, float, float], dict[str, Any]] = {}
    for plan in plans:
        unique.setdefault((*plan["window"], plan["fps"]), plan)
    looked: dict[int, dict[str, Any] | Exception] = {}
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(unique)))) as pool:
        futures = {key: pool.submit(look, plan) for key, plan in unique.items()}
        answers: dict[tuple[float, float, float], dict[str, Any] | Exception] = {}
        for key, future in futures.items():
            try:
                answers[key] = future.result()
            except (meta.MetaError, VeditError, OSError) as exc:
                answers[key] = exc
    for plan in plans:
        key = (*plan["window"], plan["fps"])
        looked[plan["index"]] = answers[key] if unique[key] is plan or isinstance(answers[key], Exception) else dict(answers[key], shared=True)

    decoder = meta.mask_decoder_ready()
    gathered: list[tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], dict[str, Any]]] = []
    for plan in plans:
        index, moment = plan["index"], moments[plan["index"]]
        outcome = looked[index]
        evidence: dict[str, Any] = {"phrase": phrase, "window": list(plan["window"]), "sample_fps": plan["fps"], "basis": "derived",
                                    "by": SAM_MODEL, "inputs": inputs}
        moment["evidence"] = evidence
        if isinstance(outcome, Exception):
            message = str(getattr(outcome, "plain", "") or outcome) or type(outcome).__name__
            moment.update(verified=None, coverage=None, thumbnail=None, verification=f"SAM did not finish: {message}")
            result["warnings"].append(f"SAM could not check the moment at {float(moment['start']):.1f}s: {message}")
            continue
        masks = outcome["masks"]
        usage = {"cached": True, "cost_usd": 0.0} if outcome.get("shared") else masks.get("usage") or {}
        result["spend"].append({"model": SAM_MODEL, "operation": "verify-find", "moment": index, "window": list(plan["window"]),
                                "frames": usage.get("video_frames_processed"),
                                "cost_usd": 0.0 if usage.get("cached") else float(usage.get("cost_usd", 0)),
                                "estimated_max_usd": usage.get("estimated_max_usd"), "cached": bool(usage.get("cached"))})
        result["warnings"].extend(masks.get("warnings") or [])
        gathered.append((plan, outcome, _entries(masks, min_confidence=min_confidence), moment))

    # One decode for every window, so a process pool starts once per search, not once per moment.
    jobs = [(outcome["masks"]["raw"], tuple(entry["span"])) for _, outcome, entries, _ in gathered for entry in entries] if decoder else []
    try:
        decoded_all: list[tuple[bytes, int, int]] | None = _decode_all(jobs) if decoder else None
        failure = None
    except meta.MetaError as exc:
        decoded_all, failure = None, exc
    cursor = 0
    for plan, outcome, entries, moment in gathered:
        evidence = moment["evidence"]
        if failure is not None:
            moment.update(verified=None, coverage=None, thumbnail=None, verification=f"SAM's masks could not be read: {failure}")
            continue
        decoded = decoded_all[cursor:cursor + len(entries)] if decoded_all is not None else None
        cursor += len(entries)
        masks = outcome["masks"]
        areas = _areas(masks, entries, decoded)
        rasters = {int(entry["frame"]): decoded[at] for at, entry in enumerate(entries)} if decoded is not None else {}
        sampled = max(outcome["sampled"], max(areas, default=-1) + 1)
        score = coverage(areas.values(), sampled, min_area=min_area)
        masked = sum(1 for area in areas.values() if area >= min_area)
        confidences = sorted(float(entry["confidence"]) for entry in entries if "confidence" in entry)
        evidence.update(sampled_frames=sampled, masked_frames=masked, measured="mask" if decoder else "box",
                        object_id=masks.get("object_id"), peak_area=round(max(areas.values(), default=0.0), 4),
                        confidence=round(confidences[len(confidences) // 2], 3) if confidences else None)
        thumbnail = None
        try:
            stem = f"{re.sub(r'[^a-z0-9]+', '-', phrase.lower()).strip('-')[:40]}-{round(plan['window'][0] * 1000)}"
            size = (int(masks.get("frame_width") or 0), int(masks.get("frame_height") or 0))
            video = probe(outcome["clip"]).video
            picture = (int(video.width), int(video.height))
            best = max((frame for frame, area in areas.items() if area >= min_area), key=lambda frame: areas[frame], default=None)
            if best is not None and best in rasters and min(size) > 0:
                raster, columns, rows = rasters[best]
                entry = next(item for item in entries if int(item["frame"]) == best)
                canvas = paste(raster, columns, rows, tuple(entry["box"]), Area(0, 0, size[0], size[1]))
                thumbnail = _thumbnail(outcome["clip"], best, bytes(canvas), size, picture, out_dir / f"{stem}.jpg")
            else:
                best = min(sampled - 1, sampled // 2)
                thumbnail = _thumbnail(outcome["clip"], best, None, size, picture, out_dir / f"{stem}-unmasked.jpg")
            evidence["thumbnail_frame"], evidence["thumbnail_time"] = best, round(plan["window"][0] + best / plan["fps"], 3)
        except (VeditError, OSError, StopIteration, AttributeError, TypeError, ValueError) as exc:
            result["warnings"].append(f"The thumbnail for the moment at {float(moment['start']):.1f}s could not be drawn: {exc}")
        verified = masked > 0 and score >= min_coverage
        if verified:
            note = f"SAM masked “{phrase}” in {masked} of {sampled} sampled frames."
        elif masked:
            note = f"SAM masked “{phrase}” in only {masked} of {sampled} sampled frames, under the {min_coverage:.0%} needed."
        else:
            note = f"SAM saw no “{phrase}” in any of {sampled} sampled frames."
        moment.update(verified=verified, coverage=score, thumbnail=str(thumbnail) if thumbnail else None, verification=note)
        if not verified and isinstance(moment.get("score"), (int, float)):
            # director.merge_moments ranks by score, so SAM's doubt has to live there too.
            evidence["spark_score"] = moment["score"]
            moment["score"] = round(float(moment["score"]) * 0.5, 3)
        if not masked:
            moment["flagged"] = True

    checked = set(order)
    for index, moment in enumerate(moments):
        if index in checked:
            continue
        reason = ("Found in the transcript; SAM checks only what was found by looking." if not _seen_by_spark(moment)
                  else f"Not checked: only the first {max_moments} moments are checked, to keep SAM's cost low.")
        moment.update(verified=None, coverage=None, thumbnail=None, verification=reason)
    if unseen == "drop":
        result["dropped"] = [moment for moment in moments if moment.get("flagged")]
        kept = [moment for moment in moments if not moment.get("flagged")]
    else:
        kept = moments
    tier = {True: 0, None: 1, False: 2}
    kept = sorted(kept, key=lambda moment: tier[moment.get("verified")])
    result["moments"] = kept
    result["spent_usd"] = round(sum(item["cost_usd"] for item in result["spend"]), 8)
    result["status"] = "verified"
    verified = [moments[index] for index in order if moments[index].get("verified") is True]
    weak = [moments[index] for index in order if moments[index].get("verified") is False and not moments[index].get("flagged")]
    never = [moments[index] for index in order if moments[index].get("flagged")]
    failed = [moments[index] for index in order if moments[index].get("verified") is None]
    shares = ", ".join(f"{moments[index]['coverage']:.0%}" for index in order if moments[index].get("coverage") is not None)
    note = f"SAM 3.1 looked for “{phrase}” in {len(order)} moments Muse Spark found: {len(verified)} verified"
    note += f", {len(weak)} seen in too few frames" if weak else ""
    note += f", {len(never)} never seen" if never else ""
    note += f", {len(failed)} not checked" if failed else ""
    note += f" (coverage {shares})." if shares else "."
    if result["dropped"]:
        note += f" Dropped {len(result['dropped'])} that SAM never saw; they are listed under dropped."
    elif never:
        note += " Moments SAM never saw are kept and flagged."
    shown_bad = [moment for moment in kept if moment.get("verified") is False]
    first_bad = min((moment["start"] for moment in shown_bad + failed), default=None)
    result["receipt"] = _receipt(phrase, note, not shown_bad and not failed, first_bad, inputs)
    result["reason"] = note
    return result
