"""Deterministic local story planning from measurable signals.

The planner reads scene cuts, speech ranges, and an optional local transcript,
then selects non-overlapping moments that fit a target duration. Every choice
cites the signal that produced it. Nothing here calls a network service or a
language model: this is signal analysis, not AI understanding of the footage.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from vedit.analyze import Range, audible, detect_silence, keep_ranges
from vedit.captions import _TERMINAL_END as TERMINAL_END
from vedit.captions import SENTENCE_ENDS, cue_words, edge_stripped, is_tight, join_words, split_words, text_width, word_records
from vedit.errors import VeditError
from vedit.timeline import subtract
from vedit.media import probe, run_ffmpeg

METHOD = "local-analysis"
NOTE = "Local analysis: scene cuts + speech energy + transcript keywords. Not AI understanding."
NO_TRANSCRIPT = "No transcript: Briefcut hears speech only with Meta Voice Transcribe, which a local plan does not call. Moments follow scene cuts and speech energy."
KINDS = ("hook", "speech", "scene", "opening", "closing")

MAX_SEGMENTS = 8
MIN_SEGMENT = 0.5
MIN_PIECE = 1.5
MAX_PIECE = 12.0
VARIATION_WEIGHT = 0.25
PAD = 0.15
MAX_SCENES = 400
MAX_SPEECH = 400
MAX_CUES = 600
MAX_TRANSCRIPT_BYTES = 60_000
MAX_SENTENCE_CHARS = 600
SENTENCE_GAP = 0.8
SENTENCE_PAD = 0.15
SENTENCE_LEAD = 0.1
SNAP_EXTEND = 1.6
SNAP_SHRINK = 0.8
SNAP_FINISH = 3.0
SNAP_OVERSHOOT = 0.15
CUT_TOLERANCE = 0.25
LABEL_CHARS = 40

_HOOK_HINT = re.compile(r"\b(open with|opening|start with|hook|begin with|first show)\b", re.I)
_PROCESS_HINT = re.compile(
    r"\b(how (it|they|we|this|these|those|things?)('s| is| are| was| were|'re)? (made|built|done|crafted|created|produced|works?)"
    r"|how to|process|steps?|step by step|behind the scenes|making of|from (start|blank|raw|scratch) to|journey|workflow|tutorial|walkthrough)\b",
    re.I,
)
COVERAGE_MILD = 0.15
COVERAGE_STRONG = 1.0
SCENE_THRESHOLD = 0.3
SCENE_FALLBACK = 0.1
_SCENE_SCORE = re.compile(r"pts_time:([0-9.]+)\s*\n\s*lavfi\.scene_score=([0-9.]+)")
_TERMINAL = TERMINAL_END  # one list of sentence ends for every script
_SNAP_EPS = 0.05
# Words that describe the request (format, length, editing verbs) rather than
# what the footage is about. Dropped from titles and hashtags.
_INSTRUCTION_WORDS = frozenset(
    "make create build produce cut edit turn write story stories video videos clip clips reel reels short shorts "
    "second seconds sec secs minute minutes min mins long length vertical horizontal square landscape portrait "
    "open opening start starting begin beginning end ending close closing finish finally first last then next after before "
    "show showing hook intro outro cta caption captions title titles format aspect ratio target duration "
    "how what where why when which who made done using use".split()
)
_ABOUT = re.compile(r"\b(?:about|of|featuring)\s+(?:(the|a|an|my|our|this|these|his|her|their)\s+)?([^.:;!?\n]+)", re.I)
_PHRASE_STOP = re.compile(r"\b(and|then|that|which|with|to|so|where|who|while|but|or|for|from)\b", re.I)
_THE_PHRASE = re.compile(r"\b(?:the|a|an|my|our)\s+((?:[A-Za-z][A-Za-z'\-]*\s*){1,3})", re.I)
_STOPWORDS = frozenset(
    "a an and are as at be been but by can could do does for from had has have how i if in into is it its just "
    "make making me more most my no not of on one only or our out over so some than that the their them then there "
    "these they this those to too up us very was we were what when where which while who will with would you your "
    "video clip story show shows showing keep tell about also like get got into onto want wants please made".split()
)


# Words only English uses for its grammar. The hints above are English patterns, and in another
# language they misfire: Spanish "a la playa" would read as an article and a subject.
_ENGLISH_MARKERS = frozenset("the and of with about for this that these those is are was were it its my our your their from into how what make "
                             "show tell keep starting opening".split())


def reads_english(goal: str) -> bool:
    """True when a brief is written in English, so the English hints may be applied to it.

    English function words decide it; a very short brief in plain ASCII with none
    ("Short cut please") is taken as English too.
    """
    words = [word.casefold() for word in _words(goal)]
    if not words:
        return False
    if sum(word in _ENGLISH_MARKERS for word in words) >= max(1, len(words) // 10):
        return True
    return len(words) <= 4 and goal.isascii()


class StoryError(VeditError):
    """A local plan could not be produced from this source."""


@dataclass
class Moment:
    start: float
    end: float
    score: float = 0.0
    kind: str = "speech"
    evidence: list[str] = field(default_factory=list)
    words: list[str] = field(default_factory=list)
    span: int = 0  # scene (or temporal span) index the moment belongs to

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def _words(text: str) -> list[str]:
    """The words of any script: runs of letters, marks and digits, keeping an inner ' or -.

    ``\\w`` would cut Hindi or Thai at every vowel sign. Text with no spaces is cut
    into the same word-sized pieces the captions use.
    """
    words: list[str] = []
    for chunk in text.split():
        current = ""
        for index, char in enumerate(chunk):
            wordy = unicodedata.category(char)[0] in "LMN"
            inner = char in "'’-" and current and index + 1 < len(chunk) and unicodedata.category(chunk[index + 1])[0] in "LMN"
            if wordy or inner:
                current += char
            elif current:
                words.append(current)
                current = ""
        if current:
            words.append(current)
    return [edge_stripped(piece) for word in words for piece in (split_words(word) if any(is_tight(char) for char in word) else [word]) if edge_stripped(piece)]


def tokens(text: str) -> list[str]:
    """Content-bearing words, lowercased: three columns ("oak", "水槽"), or two letters of a script without case ("सिंक"), so "of" and "का" do not count."""
    return [w for w in _words(text.lower()) if w not in _STOPWORDS and (text_width(w) >= 3 or sum(unicodedata.category(c) == "Lo" for c in w) >= 2)]


def keywords(goal: str) -> list[str]:
    seen: list[str] = []
    for word in tokens(goal):
        if word not in seen:
            seen.append(word)
    return seen


def content_words(text: str) -> list[str]:
    """Goal/transcript words that describe the subject, not the request.

    Drops stopwords, numbers and instruction words (format, length, editing
    verbs) so "Make a 20-second vertical story about the oak bowl" keeps only
    "oak" and "bowl".
    """
    seen: list[str] = []
    for word in tokens(text):
        if word in _INSTRUCTION_WORDS or any(ch.isdigit() for ch in word):
            continue
        if word not in seen:
            seen.append(word)
    return seen


def _clip_title(text: str, limit: int) -> str:
    text = " ".join(text.split()).strip(" ,;:-–")
    if len(text) > limit:
        cut = text[:limit].rsplit(" ", 1)[0].rstrip(",;:- ")
        text = (cut or text[:limit]).rstrip() + "…"
    return text[:1].upper() + text[1:] if text else ""


def _subject_phrase(goal: str) -> str:
    """The thing the brief is about: "about the oak bowl" → "the oak bowl". English briefs only."""
    if not reads_english(goal):
        return ""
    match = _ABOUT.search(goal)
    if match:
        phrase = _PHRASE_STOP.split(match.group(2), maxsplit=1)[0]
        words = [w.strip(",") for w in phrase.split() if w.lower() not in _INSTRUCTION_WORDS and not any(ch.isdigit() for ch in w)]
        if words and any(w.lower() not in _STOPWORDS for w in words):
            article = f"{match.group(1).capitalize()} " if match.group(1) else ""
            return article + " ".join(words)
    for match in _THE_PHRASE.finditer(goal):
        words = [w for w in match.group(1).split() if w.lower() not in _INSTRUCTION_WORDS and w.lower() not in _STOPWORDS]
        if words and not any(ch.isdigit() for w in words for ch in w):
            return "The " + " ".join(words)
    return ""


def _title_from_goal(goal: str, transcript: list[dict[str, Any]] | None = None, limit: int = 60) -> str:
    """A short, content-based title: the brief's subject, else its content words, else the first spoken sentence.

    Content words are picked with English stopwords, so a brief in another
    language takes its title from what is said, then from the brief as written.
    """
    phrase = _subject_phrase(goal)
    if phrase:
        return _clip_title(phrase, limit)
    english = reads_english(goal) or not goal.strip()
    words = content_words(goal)[:4] if english else []
    if words:
        return _clip_title(join_words(words), limit)
    for sentence in sentences_from_transcript(transcript or []):
        text = _clip_title(sentence["text"].rstrip(SENTENCE_ENDS + " "), limit)
        if text:
            return text
    if not english:
        return _clip_title(goal.rstrip(SENTENCE_ENDS + " "), limit)
    return "Your story"


def _hashtags(goal: str, transcript: list[dict[str, Any]] | None = None, limit: int = 5) -> list[str]:
    """3–5 content tags: the brief's content words first, then the most frequent transcript words."""
    tags: list[str] = []

    def add(word: str) -> None:
        clean = "".join(char for char in word if unicodedata.category(char)[0] in "LMN")
        if text_width(clean) >= 3 and not clean.isdigit() and clean not in tags and len(tags) < limit:
            tags.append(clean)

    for word in content_words(goal):
        add(word)
    if len(tags) < limit and transcript:
        counts: dict[str, int] = {}
        order: list[str] = []
        for cue in transcript:
            for word in content_words(str(cue.get("text", ""))):
                if word not in counts:
                    order.append(word)
                counts[word] = counts.get(word, 0) + 1
        for word in sorted(order, key=lambda w: (-counts[w], order.index(w))):
            add(word)
            if len(tags) >= limit:
                break
    return ["#" + tag for tag in tags]


# ---------------------------------------------------------------------------
# Pure planning on already-measured signals (no ffmpeg)
# ---------------------------------------------------------------------------


def speech_ranges(duration: float, silences: list[Range]) -> list[Range]:
    """Non-silent ranges, clamped to the clip."""
    if duration <= 0:
        return []
    keeps = keep_ranges(duration, silences, pad=0.1, min_keep=0.3)
    return [Range(max(0.0, k.start), min(duration, k.end)) for k in keeps if k.end > k.start]


def heard_ranges(energy: list[Range], sentences: list[dict[str, Any]]) -> list[Range]:
    """Speech is what a transcriber turned into words, inside what the meter measured.

    A meter cannot tell a voice from a music bed or street noise: each reads as
    one long band. Once a transcriber has listened, the rest is only sound.
    """
    heard: list[Range] = []
    for sentence in sentences:
        for band in energy:
            start, end = max(float(sentence["start"]), band.start), min(float(sentence["end"]), band.end)
            if end > start:
                heard.append(Range(start, end))
    return heard


def _was_transcribed(measured: dict[str, Any]) -> bool:
    """Did a transcriber listen? An empty transcript alone cannot say, but words can."""
    return bool(measured.get("transcribed") or measured.get("transcript"))


def split_at_scenes(ranges: list[Range], scenes: list[float]) -> list[Range]:
    pieces: list[Range] = []
    for item in ranges:
        cursor = item.start
        for cut in scenes:
            if item.start + 0.3 < cut < item.end - 0.3:
                pieces.append(Range(cursor, cut))
                cursor = cut
        pieces.append(Range(cursor, item.end))
    return [p for p in pieces if p.end > p.start]


def merge_pieces(pieces: list[Range], *, min_len: float = MIN_PIECE, max_len: float = MAX_PIECE, gap: float = 0.4) -> list[Range]:
    """Merge short neighbours into playable pieces; split very long ones."""
    merged: list[Range] = []
    for piece in sorted(pieces, key=lambda r: r.start):
        if merged and (piece.start - merged[-1].end) <= gap and (piece.end - merged[-1].start) <= max_len and merged[-1].duration < min_len:
            merged[-1] = Range(merged[-1].start, piece.end)
        else:
            merged.append(piece)
    out: list[Range] = []
    for piece in merged:
        if piece.duration > max_len:
            parts = math.ceil(piece.duration / max_len)
            step = piece.duration / parts
            out.extend(Range(piece.start + i * step, piece.start + (i + 1) * step) for i in range(parts))
        elif piece.duration >= MIN_SEGMENT:
            out.append(piece)
    return out


def _overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def clip_cuts(clips: list[tuple[float, float]] | None) -> list[float]:
    """Where the clips of a multi-clip project meet on the source clock."""
    return [float(start) for start, _ in (clips or [])[1:]]


def merge_cuts(scenes: Iterable[float], cuts: Iterable[float], *, tolerance: float = CUT_TOLERANCE) -> list[float]:
    """Detected scene cuts plus known boundaries. A cut detected at a boundary is that boundary."""
    known = sorted({round(float(cut), 3) for cut in cuts})
    kept = [float(scene) for scene in scenes if not any(abs(float(scene) - cut) <= tolerance for cut in known)]
    return sorted(kept + known)


def fit_to_clips(pieces: list[Range], clips: list[tuple[float, float]], *, longest: float = math.inf) -> list[Range]:
    """Keep every candidate inside one clip, and give silent clips and photos their own.

    A piece that crossed a join would flash a few frames of the next clip, and a
    clip with no speech would otherwise never be a candidate at all. ``longest``
    is one clip's fair share of the story, so an early pick leaves room for the rest.
    """
    fitted: list[Range] = []
    for start, end in clips:
        inside = [Range(max(piece.start, start), min(piece.end, end)) for piece in pieces if _overlap(piece.start, piece.end, start, end) >= MIN_SEGMENT]
        fitted.extend(Range(piece.start, min(piece.end, piece.start + longest)) for piece in inside or merge_pieces([Range(start, end)]))
    return fitted


def score_candidates(
    pieces: list[Range],
    *,
    duration: float,
    scenes: list[float],
    speech: list[Range],
    transcript: list[dict[str, Any]],
    goal_keywords: list[str],
    sentences: list[dict[str, Any]] | None = None,
    spans: list[float] | None = None,
    span_label: str = "scene",
) -> list[Moment]:
    """Attach a bounded score and evidence to each candidate piece.

    ``spans`` are the boundaries (scene cuts, or equal time spans when no cuts
    were found) that tag each moment with the part of the source it comes from,
    so selection can prefer parts not yet represented.
    """
    if not pieces:
        return []
    moments: list[Moment] = []
    keyword_set = set(goal_keywords)
    sentence_ends = sorted({round(float(s["end"]), 3) for s in (sentences or []) if isinstance(s, dict) and "end" in s})
    edges = sorted(float(s) for s in (spans or []) if 0 < float(s) < duration)
    speech_pieces = [p for p in pieces if any(_overlap(p.start, p.end, s.start, s.end) > 0 for s in speech)]
    first_speech = min(speech_pieces, key=lambda p: p.start) if speech_pieces else None
    last_speech = max(speech_pieces, key=lambda p: p.end) if speech_pieces else None
    for piece in pieces:
        covered = sum(_overlap(piece.start, piece.end, s.start, s.end) for s in speech)
        coverage = min(1.0, covered / piece.duration) if piece.duration > 0 else 0.0
        words: list[str] = []
        for cue in transcript:
            if _overlap(piece.start, piece.end, float(cue["start"]), float(cue["end"])) > 0:
                words.extend(tokens(str(cue.get("text", ""))))
        matched = sorted({w for w in words if w in keyword_set})
        overlap = len(matched) / max(1, len(keyword_set)) if keyword_set else 0.0
        near_cut = next((c for c in scenes if -0.05 <= piece.start - c <= 1.0), None)
        score = 0.3 * coverage + 0.35 * min(1.0, overlap * 2) + (0.15 if near_cut is not None else 0.0)
        evidence: list[str] = []
        kind = "speech" if coverage >= 0.5 else "scene"
        if coverage > 0:
            evidence.append(f"Speech {piece.start:.1f}–{piece.end:.1f}s ({coverage:.0%} of the moment)")
        if matched:
            evidence.append("mentions " + ", ".join(f'"{w}"' for w in matched[:4]) + " from your brief")
        if near_cut is not None:
            evidence.append(f"follows a scene change at {near_cut:.1f}s")
        if any(abs(piece.end - end) <= _SNAP_EPS for end in sentence_ends):
            evidence.append(f"ends on a sentence end at {piece.end:.1f}s")
        if first_speech is not None and piece is first_speech:
            score += 0.15
            kind = "hook"
            evidence.append("first spoken moment")
        elif last_speech is not None and piece is last_speech and piece is not first_speech:
            score += 0.1
            kind = "closing"
            evidence.append("last spoken moment")
        if not evidence:
            evidence.append(f"Picture-only range {piece.start:.1f}–{piece.end:.1f}s")
        span = 0
        if edges:
            midpoint = (piece.start + piece.end) / 2
            span = sum(1 for edge in edges if edge <= midpoint)
            evidence.append(f"{span_label} {span + 1} of {len(edges) + 1}")
        moments.append(Moment(piece.start, piece.end, round(min(1.0, max(0.0, score)), 3), kind, evidence, matched, span))
    return moments


def coverage_weight(goal: str, *, spans: bool) -> float:
    """How strongly selection should spread across scenes: strong for process briefs, mild otherwise, none without spans."""
    if not spans:
        return 0.0
    return COVERAGE_STRONG if reads_english(goal) and _PROCESS_HINT.search(goal) else COVERAGE_MILD


def _trimmed(moment: Moment, length: float) -> Moment:
    return Moment(moment.start, moment.start + length, moment.score, moment.kind, moment.evidence + [f"trimmed to the {length:.1f}s target"], moment.words, moment.span)


def select_moments(moments: list[Moment], target: float, *, max_segments: int = MAX_SEGMENTS, coverage: float = 0.0) -> list[Moment]:
    """Greedy, non-overlapping, fits the target; returns chronological order.

    ``coverage`` is a bonus added to the score of moments whose ``span`` (scene)
    is not yet represented among the chosen ones. It is re-evaluated after every
    pick, so a strong bonus walks the scenes (hook first, then the best moment
    of each unrepresented scene) before the remaining budget goes to the best
    scores; zero reproduces plain score order.
    """
    if not moments or target <= 0:
        return []
    chosen: list[Moment] = []
    total = 0.0
    while len(chosen) < max_segments:
        remaining = target - total
        if remaining < MIN_SEGMENT:
            break
        represented = {c.span for c in chosen}
        candidates = [m for m in moments if not any(_overlap(m.start, m.end, c.start, c.end) > 0 for c in chosen)]
        unrepresented = {m.span for m in candidates} - represented
        fair = remaining / max(1, len(unrepresented))

        def rank(moment: Moment) -> tuple[float, float]:
            bonus = 0.0
            if coverage > 0 and moment.span not in represented:
                # A moment that eats more than its fair share of what is left
                # would crowd out the scenes still waiting; nudge toward ones
                # that leave room, without overturning real score gaps.
                bonus = max(0.0, coverage - min(0.2, 0.1 * max(0.0, moment.duration - fair) / fair))
            return (-(moment.score + bonus), moment.start)

        ranked = sorted(candidates, key=rank)
        picked: Moment | None = None
        for moment in ranked:
            if moment.duration <= remaining + 0.05:
                picked = moment
                break
            if not chosen and remaining >= 1.0:
                picked = _trimmed(moment, remaining)
                break
        if picked is None:
            break
        chosen.append(picked)
        total += picked.duration
    if not chosen:
        best = max(moments, key=lambda m: (m.score, -m.start))
        chosen = [_trimmed(best, min(best.duration, target))]
    return sorted(chosen, key=lambda m: m.start)


def temporal_spans(duration: float, target: float, sentences: list[dict[str, Any]] | None = None) -> list[float]:
    """Equal span boundaries used in place of scene cuts when none were detected.

    The count is roughly the target length divided by a typical sentence (the
    median transcript sentence, else 4 s), so one sentence per span fills the
    target; at least two spans, at most eight.
    """
    if duration <= 0 or target <= 0:
        return []
    lengths = sorted(float(s["end"]) - float(s["start"]) for s in (sentences or []) if isinstance(s, dict))
    typical = lengths[len(lengths) // 2] if len(lengths) >= 3 else 4.0
    count = int(max(2, min(MAX_SEGMENTS, round(target / max(typical, MIN_SEGMENT)))))
    if duration / count < MIN_PIECE:
        count = max(2, int(duration // MIN_PIECE))
    return [round(duration * i / count, 3) for i in range(1, count)]


def _post_copy(title: str, segments: list[dict[str, Any]], sentences: list[dict[str, Any]], *, limit: int = 220, quotes: int = 2) -> str:
    """Title plus one or two complete sentences spoken inside the selected moments."""
    parts = [title.rstrip(SENTENCE_ENDS + " ") + "."] if title else []
    used = 0
    for segment in segments:
        for sentence in sentences:
            text = " ".join(str(sentence.get("text", "")).split())
            if not _TERMINAL.search(text):
                continue  # only complete sentences, never a cut-off quote
            if float(sentence["start"]) < float(segment["start"]) - _SNAP_EPS or float(sentence["end"]) > float(segment["end"]) + _SNAP_EPS:
                continue
            if text in parts:
                continue
            if len(" ".join(parts + [text])) <= limit:
                parts.append(text)
                used += 1
            if used >= quotes:
                return " ".join(parts)
    return " ".join(parts)


def unused_moments(moments: list[Moment], used: list[tuple[float, float]]) -> list[Moment]:
    """The parts of each moment that another story does not already play."""
    kept: list[Moment] = []
    for moment in moments:
        for low, high in subtract(moment.start, moment.end, used):
            if high - low >= MIN_SEGMENT:
                kept.append(Moment(low, high, moment.score, moment.kind, moment.evidence, moment.words, moment.span))
    return kept


def vary_moments(moments: list[Moment], variation: int, *, duration: float) -> list[Moment]:
    """Lean each short a different way, so a family does not reach for the same kind of moment.

    The leans cycle: later moments, shorter moments, moments near the middle,
    then earlier moments. They nudge the measured score and never replace it.
    """
    lean = variation % 4
    varied = []
    for moment in moments:
        middle = (moment.start + moment.end) / 2 / max(duration, 0.001)
        nudge = {1: middle, 2: 1 - min(1.0, moment.duration / MAX_PIECE), 3: 1 - abs(middle - 0.5) * 2, 0: 1 - middle}[lean]
        varied.append(Moment(moment.start, moment.end, round(min(1.0, moment.score + VARIATION_WEIGHT * nudge), 3), moment.kind, moment.evidence, moment.words, moment.span))
    return varied


def _pad_and_order(chosen: list[Moment], duration: float, *, target: float, hook_first: bool, pad: float = PAD) -> list[Moment]:
    """Add breathing room around cuts without exceeding the target length."""
    ordered = sorted(chosen, key=lambda m: m.start)
    budget = max(0.0, target - sum(m.duration for m in ordered))
    padded: list[Moment] = []
    for index, moment in enumerate(ordered):
        prev_end = padded[-1].end if padded else 0.0
        next_start = ordered[index + 1].start if index + 1 < len(ordered) else duration
        lead = min(pad, budget / 2, moment.start - max(prev_end, 0.0))
        tail = min(pad, budget - max(0.0, lead), max(0.0, min(next_start, duration) - moment.end))
        lead, tail = max(0.0, lead), max(0.0, tail)
        budget -= lead + tail
        padded.append(Moment(round(moment.start - lead, 3), round(moment.end + tail, 3), moment.score, moment.kind, moment.evidence, moment.words))
    if hook_first:
        hooks = [m for m in padded if m.kind == "hook"]
        if hooks:
            padded = hooks + [m for m in padded if m is not hooks[0]]
    return padded


# ---------------------------------------------------------------------------
# Sentences: cut points that do not chop speech mid-word
# ---------------------------------------------------------------------------


def _speech_bounds(speech: Any) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for item in speech or []:
        try:
            if isinstance(item, dict):
                start, end = float(item["start"]), float(item["end"])
            else:
                start, end = float(item.start), float(item.end)
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
        if math.isfinite(start) and math.isfinite(end) and end > start:
            out.append((start, end))
    return sorted(out)


def sentences_from_transcript(transcript: list[dict[str, Any]], *, duration: float = math.inf, speech: Any = None) -> list[dict[str, Any]]:
    """Group consecutive cues into sentences ending at terminal punctuation.

    Transcript lines and silence detection split at breaths, not sentences, so a cue can
    end with "oiled and". Consecutive cues are joined until one ends with
    ``. ! ? …`` (closing quotes allowed); a pause of ``SENTENCE_GAP`` seconds or
    more between cues also closes a sentence, so unpunctuated transcripts still
    break at natural pauses. Each sentence is ``{start, end, text}`` on the
    source clock, bounded like the transcript itself.

    Cue ends can run on to the next cue's start, so a cue-timed "sentence
    end" is really where the next sentence begins. With ``speech`` (measured
    speech ranges, dicts or ``Range`` objects) each sentence is clamped to the
    speech that overlaps it plus ``SENTENCE_PAD``, and never past the next
    sentence's start minus ``SENTENCE_LEAD``; without speech, cue times stand.
    """
    sentences: list[dict[str, Any]] = []
    group: list[tuple[float, float, str]] = []

    def flush() -> None:
        if not group:
            return
        start = min(item[0] for item in group)
        end = max(item[1] for item in group)
        text = join_words(item[2] for item in group)[:MAX_SENTENCE_CHARS]
        if end > start and len(sentences) < MAX_CUES:
            sentences.append({"start": round(start, 3), "end": round(end, 3), "text": text})
        group.clear()

    for cue in transcript or []:
        if not isinstance(cue, dict):
            continue
        try:
            start, end = float(cue["start"]), float(cue["end"])
        except (KeyError, TypeError, ValueError):
            continue
        text = " ".join(str(cue.get("text", "")).split())
        if not text or not (math.isfinite(start) and math.isfinite(end)) or end <= start or start >= duration:
            continue
        start, end = max(0.0, start), min(duration, end)
        if group and start - group[-1][1] >= SENTENCE_GAP:
            flush()
        group.append((start, end, text))
        if _TERMINAL.search(text):
            flush()
    flush()
    return _clamp_to_speech(sentences, _speech_bounds(speech))


def _clamp_to_speech(sentences: list[dict[str, Any]], speech: list[tuple[float, float]]) -> list[dict[str, Any]]:
    """Trim cue-timed sentences to the speech actually heard inside them.

    Each speech range belongs to the sentence holding its midpoint (a range
    that merely spills over a cue boundary belongs to the sentence it mostly
    occupies, so the next sentence's first word never pulls an end outward).
    A sentence then runs from its first word minus ``SENTENCE_PAD`` (never
    before the previous sentence ends, nor more than 0.5 s before its cue) to
    its last word plus the pad, and never past the next sentence's start minus
    ``SENTENCE_LEAD``. Sentences with no measured speech keep cue times.
    """
    if not speech or not sentences:
        return sentences
    heard: list[list[tuple[float, float]]] = [[] for _ in sentences]
    for start, end in speech:
        midpoint = (start + end) / 2
        owner = next((i for i, s in enumerate(sentences) if s["start"] <= midpoint < s["end"]), None)
        if owner is None:
            overlaps = [(min(end, s["end"]) - max(start, s["start"]), i) for i, s in enumerate(sentences)]
            best = max(overlaps)
            owner = best[1] if best[0] > 0 else None
        if owner is not None:
            heard[owner].append((start, end))
    previous_end = 0.0
    clamped: list[bool] = []
    for index, sentence in enumerate(sentences):
        done = False
        if heard[index]:
            cue_start, cue_end = sentence["start"], sentence["end"]
            start = max(previous_end, cue_start - 0.5, min(s for s, _ in heard[index]) - SENTENCE_PAD)
            end = min(cue_end, max(e for _, e in heard[index]) + SENTENCE_PAD)
            if end - start >= MIN_SEGMENT:
                sentence["start"], sentence["end"] = round(max(0.0, start), 3), round(end, 3)
                done = True
        clamped.append(done)
        previous_end = sentence["end"]
    for index, (current, following) in enumerate(zip(sentences, sentences[1:])):
        limit = following["start"] - SENTENCE_LEAD
        if clamped[index] and current["end"] > limit >= current["start"] + MIN_SEGMENT:
            current["end"] = round(limit, 3)
    return sentences


def _containing_sentence(point: float, sentences: list[dict[str, float]]) -> dict[str, float] | None:
    for sentence in sentences:
        if sentence["start"] + _SNAP_EPS < point < sentence["end"] - _SNAP_EPS:
            return sentence
    return None


def _with_note(segment: dict[str, Any], notes: list[str]) -> dict[str, Any]:
    if not notes:
        return segment
    if isinstance(segment.get("evidence"), list):
        segment["evidence"] = list(segment["evidence"]) + notes
    reason = segment.get("reason")
    if isinstance(reason, str) or "evidence" not in segment:
        base = (reason or "").strip().rstrip(".")
        suffix = "; ".join(notes) + "."
        if base:
            room = 600 - len(suffix) - 2
            if len(base) > room:
                base = base[: max(0, room - 1)].rstrip() + "…"
            segment["reason"] = f"{base}; {suffix}"
        else:
            segment["reason"] = suffix[:1].upper() + suffix[1:]
    return segment


def snap_to_sentences(
    segments: list[dict[str, Any]],
    sentences: list[dict[str, Any]],
    *,
    duration: float,
    extend: float = SNAP_EXTEND,
    shrink: float = SNAP_SHRINK,
    finish: float = SNAP_FINISH,
    target: float | None = None,
    overshoot: float = SNAP_OVERSHOOT,
) -> list[dict[str, Any]]:
    """Move segment edges off the middle of a sentence. Pure; returns copies.

    An end inside a sentence moves to that sentence's end when the extension is
    at most ``extend`` seconds, otherwise back to the sentence's start when that
    shrink is at most ``shrink`` seconds and the segment stays at least
    ``MIN_SEGMENT`` long. A start inside a sentence moves back to the sentence
    start within ``extend``, else forward to the sentence end within ``shrink``.
    A segment that lies wholly inside one sentence cannot back off without
    vanishing, so it may finish that sentence with up to ``finish`` seconds.
    Edges within 50 ms of a boundary are tidied onto it without a note.
    Neighbours in source order never overlap, nothing leaves ``[0, duration]``,
    the given (editorial) order is kept, and every adjusted segment gets a note
    in its ``reason`` (and ``evidence`` list when present). With ``target``,
    when the snapped total exceeds the target by more than ``overshoot``, the
    last extended segments are shrunk to the sentence start instead, and when
    none can shrink their extensions are reverted; nothing is ever dropped.
    Segments that overlap on input, or have unusable times, are
    returned unchanged.
    """
    out = [dict(segment) for segment in segments]
    if not out or not sentences:
        return out
    duration = float(duration)
    if not math.isfinite(duration) or duration <= 0:
        return out
    bounds: list[dict[str, float]] = []
    for sentence in sentences:
        try:
            start, end = float(sentence["start"]), float(sentence["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (math.isfinite(start) and math.isfinite(end)):
            continue
        start, end = max(0.0, start), min(duration, end)
        if end - start > _SNAP_EPS:
            bounds.append({"start": start, "end": end})
    bounds.sort(key=lambda s: (s["start"], s["end"]))
    if not bounds:
        return out

    work: list[dict[str, float] | None] = []
    for segment in out:
        try:
            start, end = float(segment["start"]), float(segment["end"])
        except (KeyError, TypeError, ValueError):
            work.append(None)
            continue
        if not (math.isfinite(start) and math.isfinite(end)) or end <= start or start < 0 or end > duration + _SNAP_EPS:
            work.append(None)
            continue
        work.append({"orig_start": start, "orig_end": end, "start": start, "end": min(end, duration)})

    def min_len(item: dict[str, float]) -> float:
        return min(MIN_SEGMENT, item["orig_end"] - item["orig_start"])

    edges = sorted({value for sentence in bounds for value in (sentence["start"], sentence["end"])})

    def tidy(point: float) -> float:
        nearest = min(edges, key=lambda edge: abs(edge - point))
        return nearest if abs(nearest - point) <= _SNAP_EPS else point

    # Independent snaps first.
    for item in work:
        if item is None:
            continue
        item["start"], item["end"] = tidy(item["start"]), tidy(item["end"])
        if item["end"] - item["start"] < min_len(item):
            item["start"], item["end"] = item["orig_start"], item["orig_end"]
        sentence = _containing_sentence(item["start"], bounds)
        if sentence is not None:
            if item["start"] - sentence["start"] <= extend:
                item["start"] = sentence["start"]
            elif sentence["end"] - item["start"] <= shrink and item["end"] - sentence["end"] >= min_len(item):
                item["start"] = sentence["end"]
        sentence = _containing_sentence(item["end"], bounds)
        if sentence is not None:
            # The segment sits inside this one sentence (or starts in the silence
            # just before it): backing off would erase it, so it may finish instead.
            whole = sentence["start"] - item["start"] < min_len(item)
            if sentence["end"] - item["end"] <= (finish if whole else extend):
                item["end"] = sentence["end"]
            elif item["end"] - sentence["start"] <= shrink and sentence["start"] - item["start"] >= min_len(item):
                item["end"] = sentence["start"]

    # Source-order neighbours: a cut inside one sentence can pull both sides
    # into it. Give the sentence to whichever side costs less, else revert.
    order = sorted((i for i, item in enumerate(work) if item is not None), key=lambda i: (work[i]["orig_start"], work[i]["orig_end"]))
    for a, b in zip(order, order[1:]):
        first, second = work[a], work[b]
        assert first is not None and second is not None
        if first["orig_end"] > second["orig_start"] + _SNAP_EPS:
            first["start"], first["end"] = first["orig_start"], first["orig_end"]
            second["start"], second["end"] = second["orig_start"], second["orig_end"]
            continue
        if first["end"] <= second["start"] + 1e-9:
            continue
        options: list[tuple[float, float]] = []
        boundary = first["end"]  # second starts where first ends
        cost = abs(boundary - second["orig_start"])
        if cost <= (shrink if boundary > second["orig_start"] else extend) and second["end"] - boundary >= min_len(second):
            options.append((cost, boundary))
        boundary = second["start"]  # first ends where second starts
        cost = abs(first["orig_end"] - boundary)
        if cost <= (shrink if boundary < first["orig_end"] else finish) and boundary - first["start"] >= min_len(first):
            options.append((cost + 1e-6, boundary))  # ties favour the earlier segment keeping its sentence
        if options:
            _, boundary = min(options)
            first["end"], second["start"] = boundary, boundary
        else:
            first["end"], second["start"] = first["orig_end"], second["orig_start"]

    for item in work:
        if item is not None and item["end"] - item["start"] < min_len(item) - 1e-9:
            item["start"], item["end"] = item["orig_start"], item["orig_end"]

    # Duration sanity: shrink the last extended segments rather than dropping any.
    if target is not None and math.isfinite(float(target)) and float(target) > 0:
        cap = float(target) * (1 + overshoot) + _SNAP_EPS
        total = sum(item["end"] - item["start"] for item in work if item is not None)
        extended = [item for item in reversed(work) if item is not None and item["end"] > item["orig_end"] + 1e-9]
        for item in extended:  # last segments first: shrink to the sentence start where the segment survives
            if total <= cap:
                break
            sentence = _containing_sentence(item["orig_end"], bounds)
            floor = max(MIN_SEGMENT, min(MIN_PIECE, 0.5 * (item["orig_end"] - item["orig_start"])))
            if sentence is not None and sentence["start"] - item["start"] >= floor:
                total -= item["end"] - sentence["start"]
                item["end"] = sentence["start"]
        for item in extended:  # still over: give the extension back rather than exceed the cap
            if total <= cap:
                break
            if item["end"] > item["orig_end"] + 1e-9:
                total -= item["end"] - item["orig_end"]
                item["end"] = item["orig_end"]

    for segment, item in zip(out, work):
        if item is None:
            continue
        notes: list[str] = []
        if item["start"] < item["orig_start"] - _SNAP_EPS:
            notes.append(f"start moved back to sentence start at {item['start']:.1f} s")
        elif item["start"] > item["orig_start"] + _SNAP_EPS:
            notes.append(f"start moved forward to sentence boundary at {item['start']:.1f} s")
        if item["end"] > item["orig_end"] + _SNAP_EPS:
            notes.append(f"end moved to sentence end at {item['end']:.1f} s")
        elif item["end"] < item["orig_end"] - _SNAP_EPS:
            notes.append(f"end moved back to sentence boundary at {item['end']:.1f} s")
        if item["start"] != item["orig_start"] or item["end"] != item["orig_end"]:
            segment["start"], segment["end"] = round(item["start"], 3), round(item["end"], 3)
        _with_note(segment, notes)
    return out


def snap_ranges(pieces: list[Range], sentences: list[dict[str, Any]], *, duration: float) -> list[Range]:
    """Candidate pieces aligned to sentences before scoring, so selection budgets whole sentences."""
    if not sentences or not pieces:
        return pieces
    ordered = sorted(pieces, key=lambda r: (r.start, r.end))
    snapped = snap_to_sentences([{"start": p.start, "end": p.end} for p in ordered], sentences, duration=duration)
    out = [Range(round(float(s["start"]), 3), round(float(s["end"]), 3)) for s in snapped]
    return [p for p in out if p.duration >= MIN_SEGMENT]


def _segment_label(start: float, end: float, sentences: list[dict[str, Any]], *, limit: int = LABEL_CHARS) -> str:
    """A short snippet of the first sentence the segment mostly contains, or ''."""
    for sentence in sentences:
        covered = _overlap(start, end, float(sentence["start"]), float(sentence["end"]))
        length = float(sentence["end"]) - float(sentence["start"])
        if length > 0 and (covered / length >= 0.5 or covered >= 0.5 * max(0.0, end - start)):
            text = " ".join(str(sentence.get("text", "")).split())
            if len(text) > limit:
                cut = text[: limit - 1].rsplit(" ", 1)[0].rstrip(",;:- ")
                text = (cut or text[: limit - 1]).rstrip() + "…"
            return text
    return ""


def require_target_duration(target_duration: object) -> float:
    """Return a target length in the same 0.5–180s contract the CLI and campaigns use.

    ``bool`` is rejected on purpose: it is an ``int`` subclass, and ``True`` would
    otherwise become a one-second target.
    """
    if isinstance(target_duration, bool) or not isinstance(target_duration, (int, float)):
        raise StoryError("The target duration must be a number of seconds between 0.5 and 180.")
    target = float(target_duration)
    if not math.isfinite(target) or target < 0.5 or target > 180:
        raise StoryError("The target duration must be a number of seconds between 0.5 and 180.")
    return target


def build_plan(
    *,
    goal: str,
    duration: float,
    target_duration: float,
    scenes: list[float],
    silences: list[Range],
    transcript: list[dict[str, Any]],
    has_audio: bool,
    warnings: list[str] | None = None,
    clips: list[tuple[float, float]] | None = None,
    exclude: list[tuple[float, float]] | None = None,
    variation: int = 0,
    words: list[dict[str, Any]] | None = None,
    transcribed: bool = False,
) -> dict[str, Any]:
    """Plan from measured signals. Pure and deterministic; safe to unit test.

    ``exclude`` lists source ranges another story already uses, and ``variation``
    leans the scoring, so one long source can yield several distinct shorts.
    ``transcribed`` says a transcriber listened, so an empty transcript means no
    one spoke, and energy bands stop counting as speech.
    """
    if not math.isfinite(duration) or duration <= 0:
        raise StoryError("The source has no measurable duration.")
    target_duration = require_target_duration(target_duration)
    warnings = list(warnings or [])
    scenes = sorted({round(float(s), 3) for s in scenes if 0 < float(s) < duration})[:MAX_SCENES]
    project_clips = clips if clips and len(clips) > 1 else None
    speech = speech_ranges(duration, silences) if has_audio else []
    speech = speech[:MAX_SPEECH]
    transcript = _bound_transcript(transcript, duration)
    sentences = sentences_from_transcript(transcript, duration=duration, speech=speech)
    heard = heard_ranges(speech, sentences) if transcribed else speech
    target = min(float(target_duration), duration)
    goal_keys = keywords(goal)
    hook_first = reads_english(goal) and bool(_HOOK_HINT.search(goal))

    if duration <= target_duration + 0.05 and not exclude:
        chosen = [Moment(0.0, round(duration, 3), 1.0, "opening", ["The whole clip fits the requested length"])]
    else:
        if speech:
            # Pieces aligned to transcript sentences first, so the budget below
            # is spent on whole sentences instead of breath-sized fragments.
            pieces = snap_ranges(merge_pieces(split_at_scenes(speech, scenes)), sentences, duration=duration)
            if not heard:
                warnings.append("No speech was heard, so moments follow the picture and the sound.")
        elif scenes:
            edges = [0.0, *scenes, duration]
            pieces = merge_pieces([Range(a, b) for a, b in zip(edges, edges[1:]) if b - a >= MIN_SEGMENT])
            warnings.append("No speech was detected, so moments follow scene changes only." if has_audio else "This source has no audio, so moments follow scene changes only.")
        else:
            pieces = []
            warnings.append("No scene changes or speech were detected; the plan uses the opening of the clip. Adjust the moments by hand.")
        # Spread the selection across the source: scene cuts when detected,
        # otherwise equal time spans, so a "how it is made" brief is not spent
        # on four consecutive sentences from the first two scenes.
        spans, span_label = (scenes, "scene") if scenes else (temporal_spans(duration, target, sentences), "span")
        coverage = coverage_weight(goal, spans=bool(spans))
        if project_clips:
            # Everything a creator brought in is a choice they made, so the clips
            # themselves are the parts to cover, whatever cuts lie inside them.
            pieces = fit_to_clips(pieces, project_clips, longest=target / min(len(project_clips), MAX_SEGMENTS))
            spans, span_label, coverage = clip_cuts(project_clips), "clip", COVERAGE_STRONG
        moments = score_candidates(pieces, duration=duration, scenes=scenes, speech=heard, transcript=transcript, goal_keywords=goal_keys, sentences=sentences, spans=spans, span_label=span_label)
        if exclude:
            moments = unused_moments(moments, exclude)
            if not moments:
                raise StoryError("Every moment in this clip is already used by another short.")
        if variation:
            moments = vary_moments(moments, variation, duration=duration)
        if not moments:
            moments = [Moment(0.0, min(duration, max(target, MIN_SEGMENT)), 0.2, "opening", ["Opening of the clip"])]
        selected = select_moments(moments, target, coverage=coverage)
        # Sentence bounds already include the pauses around speech; padding
        # would only push an edge back into the neighbouring sentence, and past
        # a clip boundary it would flash a few frames of the next clip.
        chosen = _pad_and_order(selected, duration, target=target, hook_first=hook_first, pad=0.0 if sentences or project_clips else PAD)

    segments = []
    for index, moment in enumerate(chosen, 1):
        label = {"hook": "Opening hook", "closing": "Closing moment", "scene": "Visual moment", "opening": "Opening cut"}.get(moment.kind, "Spoken moment")
        if moment.words:
            label = f"{label}: {', '.join(moment.words[:2])}"
        spoken = _segment_label(moment.start, moment.end, sentences) if moment.kind != "opening" else ""
        segments.append({
            "start": round(moment.start, 3), "end": round(moment.end, 3),
            "label": (spoken or label)[:100], "reason": ("; ".join(moment.evidence) + ".")[:600],
            "score": round(float(moment.score), 3), "kind": moment.kind if moment.kind in KINDS else "speech",
        })
    if sentences and duration > target_duration + 0.05:
        # Padding may have nudged an edge back into a neighbouring sentence.
        segments = snap_to_sentences(segments, sentences, duration=duration, target=target)
    total = round(sum(s["end"] - s["start"] for s in segments), 3)
    transcript_note = f" and {len(transcript)} transcript cues" if transcript else ""
    summary = (
        f"Local analysis found {len(scenes)} scene cuts and {len(heard)} speech ranges{transcript_note}. "
        f"Selected {len(segments)} moment{'s' if len(segments) != 1 else ''} totalling {total:g}s of {duration:.1f}s. "
        "This is deterministic signal analysis; local mode does not choose a story with AI. Review every moment before rendering."
    )
    title = _title_from_goal(goal, transcript)
    return {
        "title": title,
        "summary": summary[:1500],
        "segments": segments,
        "caption": _post_copy(title, segments, sentences),
        "hashtags": _hashtags(goal, transcript),
        "cover_prompt": "",
        "provider": "local",
        "usage": {"estimated_cost_usd": 0},
        "analysis": {
            "method": METHOD,
            "scenes": scenes,
            "speech": [{"start": round(s.start, 3), "end": round(s.end, 3)} for s in heard],
            "transcript": transcript,
            "sentences": sentences,
            "words": word_records(words or (), duration),
            "transcribed": transcribed,
            "duration": round(duration, 3),
            "notes": [NOTE],
        },
        "warnings": warnings,
    }


def _bound_transcript(transcript: list[dict[str, Any]], duration: float) -> list[dict[str, Any]]:
    cues: list[dict[str, Any]] = []
    size = 0
    for cue in transcript:
        try:
            start, end = float(cue["start"]), float(cue["end"])
        except (KeyError, TypeError, ValueError):
            continue
        text = str(cue.get("text", "")).strip()
        if not text or not (math.isfinite(start) and math.isfinite(end)) or end <= start or start >= duration:
            continue
        text = text[:300]
        size += len(text.encode("utf-8"))
        if len(cues) >= MAX_CUES or size > MAX_TRANSCRIPT_BYTES:
            break
        cues.append({"start": round(max(0.0, start), 3), "end": round(min(duration, end), 3), "text": text})
    return cues


# ---------------------------------------------------------------------------
# Measurement (ffmpeg only; a transcript arrives from Meta Voice Transcribe)
# ---------------------------------------------------------------------------


def pick_scene_cuts(candidates: list[tuple[float, float]], *, threshold: float = SCENE_THRESHOLD, fallback: float = SCENE_FALLBACK, duration: float | None = None) -> list[float]:
    """Choose cut times from ``(time, scene_score)`` pairs.

    Strong changes (score ≥ ``threshold``) are cuts as before. When there are
    none, hard cuts between similar-looking shots (same palette, same subject)
    still score ≥ ``fallback`` while camera motion rarely does; those are kept
    sparsely: at least 2 s apart, strongest first, at most one per 5 s of
    source (and at least four), so motion noise cannot flood the list.
    """
    clean = sorted((float(t), float(s)) for t, s in candidates if math.isfinite(float(t)) and math.isfinite(float(s)) and float(t) >= 0)
    out: list[float] = []
    for time, score in clean:
        if score >= threshold and (not out or time - out[-1] > 0.08):
            out.append(time)
    if out:
        return out
    cap = max(4, int(duration / 5)) if duration and math.isfinite(duration) and duration > 0 else 8
    weak = sorted((c for c in clean if c[1] >= fallback), key=lambda c: (-c[1], c[0]))
    kept: list[float] = []
    for time, _ in weak:
        if all(abs(time - other) >= 2.0 for other in kept):
            kept.append(time)
        if len(kept) >= cap:
            break
    return sorted(kept)


def detect_scenes_fast(source: Path, *, threshold: float = SCENE_THRESHOLD, width: int = 320, duration: float | None = None) -> list[float]:
    """Scene cuts measured on a downscaled picture: much faster, same cut times.

    Frames scoring at least ``SCENE_FALLBACK`` are collected in one pass and
    ``pick_scene_cuts`` decides which are cuts.
    """
    result = run_ffmpeg(["-i", str(source), "-an", "-vf", f"scale={width}:-2,select='gte(scene,{min(threshold, SCENE_FALLBACK)})',metadata=print:file=-", "-f", "null", "-"])
    candidates = [(float(t), float(s)) for t, s in _SCENE_SCORE.findall(result.stdout or "")]
    return pick_scene_cuts(candidates, threshold=threshold, duration=duration)


def analyze_source(
    source: Path,
    work_dir: Path,
    *,
    heard: Iterable[Any] | None = None,
    time_budget_s: float = 20.0,
    cuts: Iterable[float] = (),
) -> dict[str, Any]:
    """Measure scenes and silence with FFmpeg. Nothing here calls a model.

    ``heard`` is a transcript Meta Voice Transcribe already made (``captions.Cue``
    lines on the source clock); None means nobody listened. ``cuts`` are known
    boundaries, such as where one clip of a project meets the next.
    """
    info = probe(source)
    if not info.has_video or not math.isfinite(info.duration) or info.duration <= 0:
        raise StoryError("Choose a playable video with a measurable duration.")
    warnings: list[str] = []
    work_dir.mkdir(parents=True, exist_ok=True)
    scenes = merge_cuts(detect_scenes_fast(source, duration=info.duration), cuts)
    track = info.audio_map if info.has_audio else None
    # No fixed line: detect_silence places it under this track's own loudness, so a quiet phone clip is not all pause.
    silences = detect_silence(source, min_silence=0.35, track=track) if info.has_audio else []
    if info.has_audio and not audible(source, track=track):
        warnings.append("This clip's sound track is silent, so moments follow the picture.")
    transcribed = False
    transcript: list[dict[str, Any]] = []
    words: list[dict[str, Any]] = []
    if heard is not None and info.has_audio:
        cues = list(heard)
        transcript = [{"start": c.start, "end": c.end, "text": c.text} for c in cues]
        transcribed = True
        words = word_records(cue_words(cues), info.duration)
    return {"duration": info.duration, "has_audio": info.has_audio, "scenes": scenes, "silences": silences, "transcript": transcript, "words": words, "transcribed": transcribed, "warnings": warnings}


def analyze_signals(
    source: Path,
    *,
    work_dir: Path,
    heard: Iterable[Any] | None = None,
    transcribed_by: str | None = None,
    time_budget_s: float = 20.0,
    cuts: Iterable[float] = (),
) -> dict[str, Any]:
    """Measured, display-ready signals in the plan ``analysis`` shape.

    Shared by local planning and by Meta-mode plans, so the Studio scrubber and
    transcript panel behave the same whichever planner chose the moments.
    Nothing here calls a model: ``heard`` is a transcript the caller already
    has, and ``transcribed_by`` names the model that made it.
    """
    measured = analyze_source(Path(source), Path(work_dir), heard=heard, time_budget_s=time_budget_s, cuts=cuts)
    duration = float(measured["duration"])
    scenes = sorted({round(float(s), 3) for s in measured["scenes"] if 0 < float(s) < duration})[:MAX_SCENES]
    speech = (speech_ranges(duration, measured["silences"]) if measured["has_audio"] else [])[:MAX_SPEECH]
    transcript = _bound_transcript(measured["transcript"], duration)
    sentences = sentences_from_transcript(transcript, duration=duration, speech=speech)
    heard = heard_ranges(speech, sentences) if _was_transcribed(measured) else speech
    return {
        "method": METHOD,
        "scenes": scenes,
        "speech": [{"start": round(s.start, 3), "end": round(s.end, 3)} for s in heard],
        "transcript": transcript,
        "sentences": sentences,
        "words": word_records(measured.get("words") or (), duration),
        "transcribed": bool(measured.get("transcribed")),
        **({"transcribed_by": transcribed_by} if transcribed_by and measured.get("transcribed") else {}),
        "duration": round(duration, 3),
        "notes": [NOTE],
        "has_audio": bool(measured["has_audio"]),
        "warnings": list(measured["warnings"]),
    }


def annotate_segments(segments: list[dict[str, Any]], speech: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add measured ``kind``/``score`` to planner segments that lack them.

    ``kind`` is "speech" when at least half of the segment overlaps measured
    speech, otherwise "scene"; ``score`` is that overlap fraction. Existing
    values are kept.
    """
    out = []
    for segment in segments:
        item = dict(segment)
        try:
            start, end = float(item["start"]), float(item["end"])
        except (KeyError, TypeError, ValueError):
            out.append(item)
            continue
        length = end - start
        covered = 0.0
        if length > 0:
            for band in speech:
                try:
                    covered += _overlap(start, end, float(band["start"]), float(band["end"]))
                except (KeyError, TypeError, ValueError):
                    continue
        fraction = max(0.0, min(1.0, covered / length)) if length > 0 else 0.0
        item.setdefault("kind", "speech" if fraction >= 0.5 else "scene")
        item.setdefault("score", round(fraction, 3))
        out.append(item)
    return out


def plan_local_story(
    source: Path,
    *,
    goal: str,
    target_duration: float,
    work_dir: Path,
    language: str = "en",
    transcribe: bool = False,
    heard: Iterable[Any] | None = None,
    time_budget_s: float = 20.0,
    clips: list[tuple[float, float]] | None = None,
    exclude: list[tuple[float, float]] | None = None,
    variation: int = 0,
) -> dict[str, Any]:
    """Measure the source and return an editable, evidence-cited local plan. Calls no model.

    ``clips`` are the ranges of a multi-clip project on the source clock.
    ``heard`` is a Meta Voice Transcribe transcript the caller already has.
    ``transcribe`` asks for one this planner cannot make, so without ``heard``
    the plan says so; ``language`` is kept for callers and changes nothing here.
    """
    source = Path(source)
    work_dir = Path(work_dir)
    require_target_duration(target_duration)
    measured = analyze_source(source, work_dir, heard=heard, time_budget_s=time_budget_s, cuts=clip_cuts(clips))
    if transcribe and heard is None and measured["has_audio"]:
        measured["warnings"].append(NO_TRANSCRIPT)
    plan = build_plan(
        goal=goal, duration=measured["duration"], target_duration=target_duration,
        scenes=measured["scenes"], silences=measured["silences"], transcript=measured["transcript"], words=measured.get("words"),
        has_audio=measured["has_audio"], warnings=measured["warnings"], clips=clips, exclude=exclude, variation=variation, transcribed=_was_transcribed(measured),
    )
    (work_dir / "story-analysis.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return plan


def format_plan(plan: dict[str, Any]) -> str:
    lines = [plan["title"], "", plan["summary"], ""]
    for index, segment in enumerate(plan["segments"], 1):
        lines.append(f"{index}. {segment['start']:7.2f} → {segment['end']:7.2f}  [{segment.get('kind', '-'):7s} {segment.get('score', 0):.2f}]  {segment['label']}")
        lines.append(f"     {segment['reason']}")
    analysis = plan.get("analysis", {})
    lines.append("")
    lines.append(f"scenes: {len(analysis.get('scenes', []))}  speech ranges: {len(analysis.get('speech', []))}  transcript cues: {len(analysis.get('transcript', []))}")
    segments = plan.get("segments") or []
    total = sum(float(segment["end"]) - float(segment["start"]) for segment in segments)
    duration = analysis.get("duration")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(float(duration)):
        lines.append(f"selected: {total:.1f}s across {len(segments)} moments")
    else:
        lines.append(f"selected: {total:.1f}s of {float(duration):.1f}s across {len(segments)} moments")
    for warning in plan.get("warnings", []):
        lines.append(f"warning: {warning}")
    return "\n".join(lines) + "\n"
