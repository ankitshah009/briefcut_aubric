"""One clock for the whole edit.

A story is a list of segments on the source clock. Almost everything a creator
adds changes how source time maps to finished-video time: pauses and filler
words disappear inside a segment, a segment can run faster or slower, and a
transition overlaps two neighbours.

This pure module is the only place that arithmetic lives. The compiler, the
captions, the lint, the preview, and the receipts all read it, so a cut can
never disagree with its own captions. Nothing here touches a file or a process.

Vocabulary:

* **source clock**: seconds in the project's source video.
* **output clock**: seconds in the finished video.
* **segment**: ``{"start", "end", "speed"?, "keep"?}`` on the source clock.
* **keep**: the sub-ranges of a segment that survive, in order.
* **span**: one kept piece placed on the output clock.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

MIN_KEEP = 0.08
MIN_SPEED = 0.25
MAX_SPEED = 4.0
PAUSE_SECONDS = 0.6
PAUSE_PAD = 0.12
TRANSITION_SHARE = 0.4
FILLER_PAD = 0.02
STUTTER_GAP = 0.4

FILLERS = frozenset({"um", "umm", "uhm", "uh", "uhh", "er", "erm", "ah", "ahh", "hmm", "hm", "mm", "mhm"})
_WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)?", re.UNICODE)


@dataclass(frozen=True)
class Span:
    """One kept source range placed on the output clock."""

    segment: int  # 1-based index in plan["segments"]
    source_start: float
    source_end: float
    output_start: float
    output_end: float
    speed: float = 1.0

    @property
    def duration(self) -> float:
        return self.output_end - self.output_start

    def as_dict(self) -> dict[str, Any]:
        return {
            "segment": self.segment,
            "source_start": round(self.source_start, 3),
            "source_end": round(self.source_end, 3),
            "output_start": round(self.output_start, 3),
            "output_end": round(self.output_end, 3),
            "speed": self.speed,
        }


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def segment_speed(segment: dict[str, Any]) -> float:
    """The segment's playback rate, clamped to what FFmpeg can do cleanly."""
    speed = _finite(segment.get("speed"))
    if speed is None or speed <= 0:
        return 1.0
    return min(MAX_SPEED, max(MIN_SPEED, speed))


def segment_keeps(segment: dict[str, Any]) -> list[tuple[float, float]]:
    """The source ranges of a segment that survive, in source order."""
    start, end = _finite(segment.get("start")), _finite(segment.get("end"))
    if start is None or end is None or end <= start:
        return []
    keep = segment.get("keep")
    pieces: list[tuple[float, float]] = []
    if isinstance(keep, (list, tuple)):
        for item in keep:
            if isinstance(item, dict):
                lo, hi = _finite(item.get("start")), _finite(item.get("end"))
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                lo, hi = _finite(item[0]), _finite(item[1])
            else:
                continue
            if lo is None or hi is None:
                continue
            lo, hi = max(lo, start), min(hi, end)
            if hi - lo >= MIN_KEEP:
                pieces.append((lo, hi))
    if not pieces:
        return [(start, end)]
    pieces.sort()
    merged = [pieces[0]]
    for lo, hi in pieces[1:]:
        if lo <= merged[-1][1] + 1e-6:
            merged[-1] = (merged[-1][0], max(hi, merged[-1][1]))
        else:
            merged.append((lo, hi))
    return merged


def segment_output_seconds(segment: dict[str, Any]) -> float:
    """How long a segment plays in the finished video, before transitions."""
    speed = segment_speed(segment)
    return sum(hi - lo for lo, hi in segment_keeps(segment)) / speed


def effective_transition(segments: Iterable[dict[str, Any]], seconds: Any) -> float:
    """The overlap a transition really uses.

    A transition may not eat more than ``TRANSITION_SHARE`` of the shortest
    moment, so the compiler and the clock always agree on a value FFmpeg can run.
    """
    wanted = _finite(seconds) or 0.0
    lengths = [segment_output_seconds(segment) for segment in segments]
    if wanted <= 0 or len(lengths) < 2:
        return 0.0
    return round(min(wanted, TRANSITION_SHARE * min(lengths)), 6)


def build_timeline(segments: Iterable[dict[str, Any]], *, transition: Any = 0.0) -> list[Span]:
    """Place every kept piece on the output clock, in editorial order."""
    items = list(segments)
    overlap = effective_transition(items, transition)
    spans: list[Span] = []
    clock = 0.0
    for index, segment in enumerate(items, 1):
        keeps = segment_keeps(segment)
        if not keeps:
            continue
        speed = segment_speed(segment)
        if spans:
            clock -= overlap
        for lo, hi in keeps:
            length = (hi - lo) / speed
            spans.append(Span(index, lo, hi, clock, clock + length, speed))
            clock += length
    return spans


def output_duration(spans: Iterable[Span]) -> float:
    return max((span.output_end for span in spans), default=0.0)


def segment_starts(spans: Iterable[Span]) -> dict[int, float]:
    """Output time at which each 1-based segment begins."""
    starts: dict[int, float] = {}
    for span in spans:
        starts.setdefault(span.segment, span.output_start)
    return starts


def to_output(spans: Iterable[Span], source_time: float, *, segment: int | None = None) -> float | None:
    """Output time of a source time, or None when that moment was cut."""
    for span in spans:
        if segment is not None and span.segment != segment:
            continue
        if span.source_start - 1e-6 <= source_time <= span.source_end + 1e-6:
            return span.output_start + (source_time - span.source_start) / span.speed
    return None


def to_source(spans: Iterable[Span], output_time: float, *, segment: int | None = None) -> tuple[int, float] | None:
    """``(segment, source_time)`` at an output time. In an overlap, the incoming moment wins unless ``segment`` names one."""
    found: tuple[int, float] | None = None
    for span in spans:
        if segment is not None and span.segment != segment:
            continue
        if span.output_start - 1e-6 <= output_time <= span.output_end + 1e-6:
            found = (span.segment, span.source_start + (output_time - span.output_start) * span.speed)
    return found


def map_interval(spans: Iterable[Span], start: float, end: float) -> list[tuple[int, float, float]]:
    """Where a source interval plays: ``(segment, output_start, output_end)`` pieces in output order."""
    pieces: list[tuple[int, float, float]] = []
    for span in spans:
        lo, hi = max(start, span.source_start), min(end, span.source_end)
        if hi - lo <= 1e-6:
            continue
        begin = span.output_start + (lo - span.source_start) / span.speed
        finish = span.output_start + (hi - span.source_start) / span.speed
        if pieces and pieces[-1][0] == span.segment and begin - pieces[-1][2] < 1e-3:
            pieces[-1] = (span.segment, pieces[-1][1], finish)
        else:
            pieces.append((span.segment, begin, finish))
    return pieces


def _surviving_text(text: str, start: float, end: float, spans: list[Span], segments: list[int]) -> list[str]:
    """For each segment, the part of a wordless line that plays in it.

    The line's words are spread over its source time by character count, as
    captions estimate them, and a word stays where its middle is kept. So a line
    whose head was cut shows only its tail, and a sliver shorter than half a word
    shows nothing. Moments keep their own words even when the story plays them
    out of source order. Only text is decided here; timing is the spans'.
    Words that stay side by side keep the line's own spacing, so Thai keeps the
    space between its phrases and a line kept whole reads exactly as heard.
    """
    from vedit.captions import join_words, split_words  # captions builds on this clock, so it is read only when needed

    words = split_words(text)
    # split_words cuts the line into pieces of itself, in order, so each one is found where it sits.
    offsets: list[tuple[int, int]] = []
    at = 0
    for word in words:
        found = text.find(word, at)
        offsets.append((found, found + len(word)))
        at = found + len(word)
    total = sum(len(word) for word in words) or 1
    placed: dict[int, list[int]] = {segment: [] for segment in segments}
    spent = 0
    for index, word in enumerate(words):
        middle = start + (end - start) * (spent + len(word) / 2) / total
        spent += len(word)
        owner = next((span.segment for span in spans if span.source_start <= middle < span.source_end), None)
        if owner in placed:
            placed[owner].append(index)

    def written(indices: list[int]) -> str:
        runs: list[tuple[int, int]] = []  # first and last word of each stretch of neighbours
        for index in indices:
            if runs and index == runs[-1][1] + 1:
                runs[-1] = (runs[-1][0], index)
            else:
                runs.append((index, index))
        return join_words(" ".join(text[offsets[first][0] : offsets[last][1]].split()) for first, last in runs)

    return [written(placed[segment]) for segment in segments]


def map_cues(cues: Iterable[dict[str, Any]], spans: list[Span]) -> list[dict[str, Any]]:
    """Move source-clock cues onto the output clock.

    A cue with ``words`` keeps only the words that survive the edit, so a removed
    filler also leaves the caption. A cue with no words (Meta's lines) keeps the
    words whose estimated time survives: whole when the edit keeps it, only its
    tail when the head was cut, and shared between moments when the edit splits
    it. Cues that were entirely cut are dropped. Each result names the
    ``segment`` it plays in, which output time alone cannot say inside a transition.
    """
    from vedit.captions import join_words

    moved: list[dict[str, Any]] = []
    for cue in cues:
        start, end = _finite(cue.get("start")), _finite(cue.get("end"))
        text = str(cue.get("text") or "").strip()
        if start is None or end is None or end <= start or not text:
            continue
        words = cue.get("words") if isinstance(cue.get("words"), list) else None
        if words:
            placed: dict[int, list[dict[str, Any]]] = {}
            for word in words:
                if not isinstance(word, dict):
                    continue
                lo, hi = _finite(word.get("start")), _finite(word.get("end"))
                if lo is None or hi is None or hi < lo:
                    continue
                pieces = map_interval(spans, lo, max(hi, lo + 1e-3))
                if not pieces:
                    continue
                # A word across a cut stays with the moment it starts in. The next
                # moment can begin earlier on this clock, under a transition.
                segment, begin = pieces[0][0], pieces[0][1]
                finish = max(hi for owner, _lo, hi in pieces if owner == segment)
                placed.setdefault(segment, []).append(
                    {"start": round(begin, 3), "end": round(finish, 3), "text": str(word.get("text") or "")}
                )
            for segment, segment_words in placed.items():
                if segment_words:
                    moved.append(
                        {
                            "start": segment_words[0]["start"],
                            "end": segment_words[-1]["end"],
                            "text": join_words(item["text"] for item in segment_words),
                            "words": segment_words,
                            "segment": segment,
                        }
                    )
            continue
        pieces = map_interval(spans, start, end)
        if not pieces:
            continue
        groups: dict[int, list[float]] = {}
        for segment, begin, finish in pieces:
            group = groups.setdefault(segment, [begin, finish])
            group[0], group[1] = min(group[0], begin), max(group[1], finish)
        shares = _surviving_text(text, start, end, spans, list(groups))
        for (segment, (lo, hi)), share in zip(groups.items(), shares, strict=True):
            if share:
                moved.append({"start": round(lo, 3), "end": round(hi, 3), "text": share, "segment": segment})
    moved.sort(key=lambda cue: (cue["start"], cue["end"]))
    return moved


def concat_spans(spans: Iterable[Span]) -> list[Span]:
    """The same placements, with the kept audio laid end to end at normal speed as the "source".

    A recognizer that hears only the kept pieces reports times on this concat
    clock. Handing ``map_cues`` these spans covers keep, speed, and transition.
    """
    placed: list[Span] = []
    clock = 0.0
    for span in spans:
        length = span.source_end - span.source_start
        placed.append(Span(span.segment, clock, clock + length, span.output_start, span.output_end, span.speed))
        clock += length
    return placed


def concat_to_source(spans: Iterable[Span], moment: float) -> float:
    """The source time at a moment on the concat clock."""
    clock = 0.0
    last = moment
    for span in spans:
        length = span.source_end - span.source_start
        if moment <= clock + length + 1e-6:
            return span.source_start + max(0.0, moment - clock)
        clock += length
        last = span.source_end
    return last


def subtract(start: float, end: float, removals: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    """``[start, end]`` minus the removals, dropping slivers too short to play."""
    cuts = sorted((max(lo, start), min(hi, end)) for lo, hi in removals if min(hi, end) > max(lo, start))
    keeps: list[tuple[float, float]] = []
    cursor = start
    for lo, hi in cuts:
        if lo - cursor >= MIN_KEEP:
            keeps.append((cursor, lo))
        cursor = max(cursor, hi)
    if end - cursor >= MIN_KEEP:
        keeps.append((cursor, end))
    return keeps


def pause_removals(
    speech: Iterable[dict[str, Any]],
    start: float,
    end: float,
    *,
    min_pause: float = PAUSE_SECONDS,
    pad: float = PAUSE_PAD,
) -> list[tuple[float, float]]:
    """Pauses inside a moment, from measured speech bands. A moment with no speech keeps its picture."""
    bands = []
    for band in speech:
        lo, hi = _finite(band.get("start")), _finite(band.get("end"))
        if lo is not None and hi is not None and min(hi, end) > max(lo, start):
            bands.append((max(lo, start), min(hi, end)))
    if not bands:
        return []
    removals: list[tuple[float, float]] = []
    cursor = start
    for lo, hi in sorted(bands):
        if lo - cursor >= min_pause:
            begin = cursor + pad if cursor > start else cursor
            if lo - pad - begin >= MIN_KEEP:
                removals.append((begin, lo - pad))
        cursor = max(cursor, hi)
    if end - cursor >= min_pause and end - cursor - pad >= MIN_KEEP:
        removals.append((cursor + pad, end))
    return removals


def _plain(word: str) -> str:
    match = _WORD.search(word.lower())
    return match.group(0) if match else ""


def _spoken(text: str) -> list[str]:
    return [match.group(0) for match in _WORD.finditer(text.lower())]


def _estimated(word: dict[str, Any]) -> bool:
    """True when this time was spread across a turn by length, not heard."""
    return bool(word.get("estimated"))


def _placed_word(word: dict[str, Any], start: float, end: float) -> tuple[float, float, str] | None:
    if _estimated(word):
        return None
    lo, hi = _finite(word.get("start")), _finite(word.get("end"))
    if lo is None or hi is None or hi <= lo or lo < start or hi > end:
        return None
    return lo, hi, _plain(str(word.get("text") or ""))


def _repeats_next(inside: list[tuple[float, float, str]], index: int) -> bool:
    hi, plain = inside[index][1], inside[index][2]
    if not plain or index + 1 >= len(inside):
        return False
    nxt_lo, nxt_plain = inside[index + 1][0], inside[index + 1][2]
    return plain == nxt_plain and nxt_lo - hi <= STUTTER_GAP


def _word_filler_removals(
    words: Iterable[dict[str, Any]], start: float, end: float, pad: float
) -> list[tuple[float, float]]:
    inside = [placed for word in words if (placed := _placed_word(word, start, end)) is not None]
    removals: list[tuple[float, float]] = []
    for index, (lo, hi, plain) in enumerate(inside):
        if plain in FILLERS:
            removals.append((max(start, lo - pad), min(end, hi + pad)))
        elif _repeats_next(inside, index):
            # "the the shape": the second copy is the clean take.
            removals.append((max(start, lo - pad), inside[index + 1][0]))
    return removals


def _transcript_lines(cues: Iterable[Any]) -> list[tuple[float, float, str]]:
    lines: list[tuple[float, float, str]] = []
    for cue in cues:
        if not isinstance(cue, dict):
            continue
        lo, hi = _finite(cue.get("start")), _finite(cue.get("end"))
        text = str(cue.get("text") or "").strip()
        if lo is None or hi is None or hi <= lo or not text:
            continue
        lines.append((lo, hi, text))
    lines.sort()
    return lines


def _only_filler(text: str) -> bool:
    spoken = _spoken(text)
    return bool(spoken) and all(word in FILLERS for word in spoken)


def _same_line(left: str, right: str) -> bool:
    plain = " ".join(_spoken(left))
    return bool(plain) and plain == " ".join(_spoken(right))


def _overlaps(lo: float, hi: float, start: float, end: float) -> bool:
    return min(hi, end) > max(lo, start)


def _heard_interval(word: dict[str, Any]) -> tuple[float, float] | None:
    if _estimated(word):
        return None
    lo, hi = _finite(word.get("start")), _finite(word.get("end"))
    if lo is None or hi is None or hi <= lo:
        return None
    return lo, hi


def _heard_intervals(words: Iterable[dict[str, Any]]) -> list[tuple[float, float]]:
    return [interval for word in words if (interval := _heard_interval(word)) is not None]


def _heard_overlaps(heard: list[tuple[float, float]], lo: float, hi: float) -> bool:
    return any(_overlaps(word_lo, word_hi, lo, hi) for word_lo, word_hi in heard)


def _padded_cut(lo: float, hi: float, start: float, end: float, pad: float) -> tuple[float, float] | None:
    if not _overlaps(lo, hi, start, end):
        return None
    cut_lo, cut_hi = max(start, lo - pad), min(end, hi + pad)
    if cut_hi <= cut_lo:
        return None
    return cut_lo, cut_hi


def _line_filler_removals(
    lines: list[tuple[float, float, str]],
    heard: list[tuple[float, float]],
    start: float,
    end: float,
    pad: float,
) -> list[tuple[float, float]]:
    removals: list[tuple[float, float]] = []
    for lo, hi, text in lines:
        if not _only_filler(text) or _heard_overlaps(heard, lo, hi):
            continue
        cut = _padded_cut(lo, hi, start, end, pad)
        if cut is not None:
            removals.append(cut)
    return removals


def _line_repeat_removals(
    lines: list[tuple[float, float, str]],
    heard: list[tuple[float, float]],
    start: float,
    end: float,
    pad: float,
) -> list[tuple[float, float]]:
    """The second of two identical lines said back to back. The first stays.

    Word stutters drop the first word instead. A heard word on the second line
    leaves that line to the word path.
    """
    removals: list[tuple[float, float]] = []
    for index in range(len(lines) - 1):
        first_hi, first_text = lines[index][1], lines[index][2]
        second_lo, second_hi, second_text = lines[index + 1]
        if second_lo - first_hi > STUTTER_GAP or not _same_line(first_text, second_text):
            continue
        if _heard_overlaps(heard, second_lo, second_hi):
            continue
        cut = _padded_cut(second_lo, second_hi, start, end, pad)
        if cut is not None:
            removals.append(cut)
    return removals


def filler_removals(
    words: Iterable[dict[str, Any]],
    start: float,
    end: float,
    *,
    pad: float = FILLER_PAD,
    cues: Iterable[Any] = (),
) -> list[tuple[float, float]]:
    """Spoken fillers and immediate repeats. Only a time the transcript heard can be removed.

    A word marked estimated was spread by length, so it never moves a cut.
    A heard line with no word time is removed when every word of it is a filler.
    The second copy is removed when the same line is said again back to back.
    A filler inside a sentence stays, because that line was not timed per word.
    """
    lines = _transcript_lines(cues)
    heard = _heard_intervals(words)
    return (
        _word_filler_removals(words, start, end, pad)
        + _line_filler_removals(lines, heard, start, end, pad)
        + _line_repeat_removals(lines, heard, start, end, pad)
    )


def _mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    """``data[key]`` when it is a mapping, else an empty one. A plan is loose JSON, not a schema."""
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def _records(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    return [item for item in (data.get(key) or []) if isinstance(item, dict)]


def resolve_segments(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Fill in each segment's ``keep`` from the edit: manual cuts, pauses, fillers.

    A removal that would erase a whole moment is ignored; deleting a moment is a
    decision for the story, not a side effect of a cleanup setting.
    """
    edit = _mapping(plan, "edit")
    analysis = _mapping(plan, "analysis")
    speech = _records(analysis, "speech")
    words = _records(analysis, "words")
    cues = _records(analysis, "transcript")
    manual = []
    for item in edit.get("removed") or []:
        if isinstance(item, dict):
            lo, hi = _finite(item.get("start")), _finite(item.get("end"))
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            lo, hi = _finite(item[0]), _finite(item[1])
        else:
            continue
        if lo is not None and hi is not None and hi > lo:
            manual.append((lo, hi))
    resolved: list[dict[str, Any]] = []
    for segment in plan.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        item = dict(segment)
        item.pop("keep", None)
        start, end = _finite(item.get("start")), _finite(item.get("end"))
        if start is None or end is None or end <= start:
            resolved.append(item)
            continue
        removals = list(manual)
        if edit.get("trim_pauses"):
            removals.extend(pause_removals(speech, start, end))
        if edit.get("remove_fillers"):
            removals.extend(filler_removals(words, start, end, cues=cues))
        if removals:
            keeps = subtract(start, end, removals)
            if keeps and keeps != [(start, end)]:
                item["keep"] = [[round(lo, 3), round(hi, 3)] for lo, hi in keeps]
        resolved.append(item)
    return resolved


def plan_timeline(plan: dict[str, Any]) -> dict[str, Any]:
    """The display and compile view of a plan: resolved segments and their spans."""
    segments = resolve_segments(plan)
    transition = _mapping(_mapping(plan, "edit"), "transition")
    seconds = transition.get("seconds") if transition.get("type") not in (None, "none") else 0.0
    overlap = effective_transition(segments, seconds)
    spans = build_timeline(segments, transition=overlap)
    return {
        "segments": segments,
        "spans": spans,
        "transition_seconds": overlap,
        "duration": round(output_duration(spans), 3),
    }
