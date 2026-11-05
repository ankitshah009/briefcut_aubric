"""Deterministic grading of a story plan, and pure patch application.

``lint_plan`` grades a plan against the transcript, the target length (a
ceiling), the source bounds, a multi-clip project's clip edges, the brief's
named ending and (when asked) caption coverage. A check with nothing to
measure is left out, never passed. Every check is a plain
``{"name", "passed", "at", "note", "basis", "by"}`` record; ``at`` is a time on
the OUTPUT timeline so a viewer can scrub straight to it. Checks that fail also
propose structured patches against the plan, which ``apply_patches`` applies
without ever raising: a patch that would break the plan is reported as skipped.

``basis`` says how a receipt knows what it claims: ``observed`` is read straight
from the delivered file, ``derived`` is code measuring the cut against stored
inputs, ``inferred`` is a model's judgment. Every check here is ``derived``.
``by`` names the instrument. When a model produced the data a check measures
(a transcript, SAM's boxes), ``inputs`` names it; a check that measured nothing
from a model has no ``inputs``.

This module does no I/O and never calls a provider.
"""

from __future__ import annotations

import math
import re
from typing import Any

from vedit.director import EDIT_FIELDS, describe_patch, read_edit, write_edit
from vedit.edit_spec import EditError, default_edit, validate_edit
from vedit.timeline import MAX_SPEED, MIN_SPEED, map_interval, plan_timeline, segment_keeps, segment_output_seconds, segment_speed, to_output

MIN_SEGMENT = 0.5
SENTENCE_TOLERANCE = 0.15
SNAP_EXTEND = 1.6
SNAP_SHRINK = 0.8
CAPTION_COVERAGE = 0.8
CUTOUT_COVERAGE = 0.9
CUTOUT_FLICKER = 0.08
PATCH_OPS = ("trim_start", "trim_end", "remove", "move_before", "set_edit", "set_speed", "split")
MAX_MOMENTS = 32
MAX_STORY_SECONDS = 180.0
_LONGEST_EDIT = 86_400.0
_REASON_LIMIT = 600
_EPS = 1e-6
# How a receipt knows what it claims, weakest last. Borrowed from REA's evidence model.
BASES = ("observed", "derived", "inferred")
SAM = "sam-3.1"
SPARK = "muse-spark"
HERO_HIDDEN = 0.65  # beyond this share of the word behind the subject, the word stops reading
HERO_CONTRAST = 3.0  # the least contrast between the word and what is behind it: WCAG's floor for large text
CLIP_SNAP = 1.0  # a moment that strays this far or less into a neighbouring clip is trimmed back to its own clip
LENGTH_TOLERANCE = 0.05  # the target is a ceiling; a cut may run this share of it (at least 0.5 s) over
WELL_UNDER = 0.7  # under this share of the target, the length check adds a note with the real numbers
_PROTECTED_KINDS = ("hook", "opening", "closing")
_ENDING_HINT = re.compile(r"\b(?:end|ends|ending|finish|finishes|finishing|close|closes|closing|wrap(?:s|ping)?\s+up)\s+(?:on|with)\b", re.IGNORECASE)
_OPENING_HINT = re.compile(r"\b(?:open|opens|opening|start|starts|starting|begin|begins|beginning)\s+(?:on|with)\b", re.IGNORECASE)


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _segments(plan: dict[str, Any]) -> list[dict[str, Any]]:
    raw = plan.get("segments") if isinstance(plan, dict) else None
    out: list[dict[str, Any]] = []
    for item in raw or []:
        if not isinstance(item, dict):
            out.append({"start": math.nan, "end": math.nan})
            continue
        start, end = _finite(item.get("start")), _finite(item.get("end"))
        out.append(dict(item, start=math.nan if start is None else start, end=math.nan if end is None else end))
    return out


def _ranges(value: Any) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for item in value or []:
        if not isinstance(item, dict):
            continue
        start, end = _finite(item.get("start")), _finite(item.get("end"))
        if start is not None and end is not None and end > start:
            out.append((start, end))
    return sorted(out)


def _on_timeline(plan: dict[str, Any]) -> bool:
    """True when the edit moves time: a cut inside a moment, a speed, or a transition."""
    edit = plan.get("edit") if isinstance(plan.get("edit"), dict) else {}
    transition = edit.get("transition") if isinstance(edit.get("transition"), dict) else {}
    return bool(edit.get("removed") or edit.get("trim_pauses") or edit.get("remove_fillers")) \
        or transition.get("type") not in (None, "none") \
        or any(isinstance(item, dict) and segment_speed(item) != 1 for item in plan.get("segments") or [])


def output_time(plan: dict[str, Any], segment_index: int, source_seconds: float) -> float:
    """Output-timeline seconds for a source-clock time inside the 1-based segment.

    A moment the edit cut has no output time of its own, so it reads as the next
    moment that still plays in that segment.
    """
    segments = _segments(plan)
    if not 1 <= segment_index <= len(segments):
        raise ValueError(f"segment {segment_index} is not in the plan")
    if _on_timeline(plan):
        spans = [span for span in plan_timeline(plan)["spans"] if span.segment == segment_index]
        moved = to_output(spans, float(source_seconds))
        if moved is None:
            later = [span.output_start for span in spans if span.source_start >= source_seconds]
            moved = later[0] if later else (spans[-1].output_end if spans else 0.0)
        return round(max(0.0, moved), 3)
    offset = 0.0
    for previous in segments[: segment_index - 1]:
        if math.isfinite(previous["start"]) and math.isfinite(previous["end"]):
            offset += max(0.0, previous["end"] - previous["start"])
    segment = segments[segment_index - 1]
    start = segment["start"] if math.isfinite(segment["start"]) else 0.0
    return round(max(0.0, offset + (float(source_seconds) - start)), 3)


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _patch(op: str, segment: int, seconds: float | None, reason: str) -> dict[str, Any]:
    return {"op": op, "segment": segment, "seconds": None if seconds is None else round(seconds, 3), "reason": reason, "source": "auto"}


def _check(name: str, passed: bool, at: float | None, note: str, *, by: str = "lint", inputs: Any = ()) -> dict[str, Any]:
    """One receipt. Code measured it (``derived``); ``inputs`` names any model whose data it read."""
    check = {"name": name, "passed": passed, "at": None if at is None else round(at, 3), "note": note, "basis": "derived", "by": by}
    named = [str(item) for item in inputs or () if item]
    if named:
        check["inputs"] = list(dict.fromkeys(named))
    return check


def _sentence_check(plan: dict[str, Any], segments: list[dict[str, Any]], sentences: list[tuple[float, float]], duration: float,
                    *, strict: bool = False, heard_by: str | None = None) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Grade cuts against sentences. ``strict`` drops the move tolerances: a cut still
    inside a sentence goes to the nearest boundary, however far that is.

    With no transcript nothing can be measured, so there is no check: a pass would claim what nobody looked at.
    """
    name = "auto: cuts land on sentence boundaries"
    inputs = [heard_by]
    extend_limit, shrink_limit = (math.inf, math.inf) if strict else (SNAP_EXTEND, SNAP_SHRINK)
    if not sentences:
        return None, []
    patches: list[dict[str, Any]] = []
    first_at: float | None = None
    offenders = 0
    resolved = _resolved(plan)
    for index, segment in enumerate(segments, 1):
        start, end = segment["start"], segment["end"]
        if not (math.isfinite(start) and math.isfinite(end)) or end <= start:
            continue
        # Pieces inside a moment join across a pause or a filler on purpose. The
        # story's real cuts are where the moment's first and last pieces meet its neighbours.
        played = segment_keeps(resolved[index - 1]) if resolved else [(start, end)]
        for edge in ("start", "end"):
            boundary = played[0][0] if edge == "start" else played[-1][1]
            inside = next((s for s in sentences if s[0] + SENTENCE_TOLERANCE < boundary < s[1] - SENTENCE_TOLERANCE), None)
            if inside is None:
                continue
            offenders += 1
            if first_at is None:
                first_at = output_time(plan, index, boundary)
            if boundary != (start if edge == "start" else end):
                continue  # a removal made this cut, so moving the moment's own edge cannot fix it
            options: list[tuple[float, float]] = []
            if edge == "start":
                # Moving the start earlier extends the moment; later shrinks it.
                extend, shrink = boundary - inside[0], inside[1] - boundary
                if extend <= extend_limit and inside[0] >= 0 and end - inside[0] >= MIN_SEGMENT:
                    options.append((extend, inside[0]))
                if shrink <= shrink_limit and end - inside[1] >= MIN_SEGMENT:
                    options.append((shrink, inside[1]))
            else:
                extend, shrink = inside[1] - boundary, boundary - inside[0]
                if extend <= extend_limit and inside[1] <= duration + _EPS and inside[1] - start >= MIN_SEGMENT:
                    options.append((extend, min(inside[1], duration)))
                if shrink <= shrink_limit and inside[0] - start >= MIN_SEGMENT:
                    options.append((shrink, inside[0]))
            if not options:
                continue
            _, target = min(options)
            patches.append(_patch(
                "trim_start" if edge == "start" else "trim_end", index, target,
                f"Moment {index} {'starts' if edge == 'start' else 'ends'} mid-sentence at {boundary:.2f} s; snap to the sentence boundary at {target:.2f} s.",
            ))
    if offenders == 0:
        return _check(name, True, None, "every cut sits on a sentence boundary", inputs=inputs), []
    return _check(name, False, first_at, f"{offenders} cut{'s' if offenders != 1 else ''} inside a sentence", inputs=inputs), patches


def _resolved(plan: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Segments with their kept pieces when the edit moves time, by plan position."""
    if not _on_timeline(plan):
        return None
    segments = plan_timeline(plan)["segments"]
    return segments if len(segments) == len(plan.get("segments") or []) else None


def _shortened_end(segment: dict[str, Any], excess: float) -> float:
    """Where a moment must end to play ``excess`` fewer output seconds."""
    remaining = excess * segment_speed(segment)
    for low, high in reversed(segment_keeps(segment)):
        if high - low > remaining:
            return high - remaining
        remaining -= high - low
    return segment["start"]


def _seconds(value: float) -> str:
    """A length as a person writes it: 25, 13.6."""
    return f"{value:.1f}".removesuffix(".0")


def length_check_name(target: float) -> str:
    return f"auto: length fits the target (≤ {_seconds(target)} s)"


def _alive(segment: dict[str, Any]) -> bool:
    return math.isfinite(segment["start"]) and math.isfinite(segment["end"]) and segment["end"] > segment["start"]


def _bookend_range(plan: dict[str, Any], key: str) -> tuple[float, float, str] | None:
    """The source range of the moment the planner placed first or last because the brief named it."""
    bookends = plan.get("bookends") if isinstance(plan.get("bookends"), dict) else {}
    record = bookends.get(key)
    if not isinstance(record, dict):
        return None
    start, end = _finite(record.get("start")), _finite(record.get("end"))
    if start is None or end is None or end <= start:
        return None
    return start, end, str(record.get("label") or "").strip()[:100]


def _protected(plan: dict[str, Any], segments: list[dict[str, Any]]) -> set[int]:
    """1-based moments the length fix may shorten but never drop: those holding the brief's named opening or ending."""
    keep: set[int] = set()
    for key in ("opening", "ending"):
        found = _bookend_range(plan, key)
        if found is None:
            continue
        for index, segment in enumerate(segments, 1):
            if _alive(segment) and _overlap(segment["start"], segment["end"], found[0], found[1]) > _EPS:
                keep.add(index)
    return keep


def _priority(segment: dict[str, Any], index: int) -> tuple[float, int]:
    """Lowest first: the planner's score (0.5 when it gave none), raised for a hook, an opening or a closing. A later moment goes first among equals."""
    score = _finite(segment.get("score"))
    weight = score if score is not None and 0 <= score <= 1 else 0.5
    if segment.get("kind") in _PROTECTED_KINDS:
        weight += 0.25
    return weight, -index


def _duration_check(plan: dict[str, Any], segments: list[dict[str, Any]], target: float,
                    *, asked: float | None = None, source: float | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The target is a ceiling: pass at or under it plus a small allowance.

    A cut far under the target passes, with a separate note that states the
    numbers. An overlong cut proposes trims from the moments that matter least,
    never dropping one that holds the brief's named opening or ending.
    """
    name = length_check_name(target)
    resolved = _resolved(plan)
    if resolved:
        total = float(plan_timeline(plan)["duration"])
    else:
        total = sum(s["end"] - s["start"] for s in segments if _alive(s))
    allowance = max(0.5, LENGTH_TOLERANCE * target)
    footage = ""
    if asked is not None and source is not None and asked > source + _EPS:
        footage = f" You asked for {asked:.1f} s, but the footage runs {source:.1f} s."
    if total <= target + allowance + _EPS:
        if total <= target + _EPS:
            note = f"{total:.1f} s cut, within the {target:.1f} s target."
        else:
            note = f"{total:.1f} s cut, {total - target:.1f} s over the {target:.1f} s target. The allowance is {allowance:.1f} s."
        checks = [_check(name, True, None, note + footage)]
        if target > 0 and total < WELL_UNDER * target - _EPS:
            checks.append(dict(_check("auto: note: the cut is well under the target", True, None,
                                      f"The cut runs {total:.1f} s, {total / target:.0%} of the {target:.1f} s target. "
                                      "The target is a ceiling, so this is not a failure." + footage), advisory=True))
        return checks, []
    excess = total - target
    patches: list[dict[str, Any]] = []
    lengths = {index: (segment_output_seconds(resolved[index - 1]) if resolved else segment["end"] - segment["start"])
               for index, segment in enumerate(segments, 1) if _alive(segment)}
    alive = len(lengths)
    keep = _protected(plan, segments)
    reason = f"The cut runs {excess:.1f} s over the {target:.1f} s target"

    def trim(index: int) -> None:
        segment = segments[index - 1]
        new_end = _shortened_end(resolved[index - 1], excess) if resolved else segment["end"] - excess
        patches.append(_patch("trim_end", index, new_end, f"{reason}; shorten moment {index}, the lowest-ranked moment that can absorb it."))

    # Least important first. One moment that can absorb the whole excess is shortened;
    # otherwise the least important moment is dropped and the search repeats.
    order = sorted((index for index in lengths if index not in keep), key=lambda index: _priority(segments[index - 1], index))
    while excess > _EPS and order:
        taker = next((index for index in order if lengths[index] - excess >= MIN_SEGMENT), None)
        if taker is not None:
            trim(taker)
            excess = 0.0
            break
        if alive <= 1:
            break
        dropped = order.pop(0)
        patches.append(_patch("remove", dropped, None, f"{reason}; drop moment {dropped}, the lowest-ranked moment left."))
        excess -= lengths[dropped]
        alive -= 1
    if excess > _EPS:
        # The brief's opening and ending may be shortened, never dropped.
        taker = next((index for index in sorted(keep, key=lambda index: _priority(segments[index - 1], index)) if lengths[index] - excess >= MIN_SEGMENT), None)
        if taker is not None:
            trim(taker)
    note = f"{total:.1f} s cut, {total - target:.1f} s over the {target:.1f} s target. The allowance is {allowance:.1f} s."
    return [_check(name, False, round(target, 3), note + footage)], patches


def _bounds_check(plan: dict[str, Any], segments: list[dict[str, Any]], duration: float) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    name = "auto: moments inside the source and in order"
    problems: list[tuple[int, str]] = []
    valid: list[tuple[int, float, float]] = []
    for index, segment in enumerate(segments, 1):
        start, end = segment["start"], segment["end"]
        if not (math.isfinite(start) and math.isfinite(end)):
            problems.append((index, f"moment {index} has no usable times"))
        elif start < 0 or end > duration + _EPS:
            problems.append((index, f"moment {index} leaves the {duration:.1f} s source"))
        elif end <= start:
            problems.append((index, f"moment {index} ends before it starts"))
        elif end - start < min(MIN_SEGMENT, duration) - _EPS:
            problems.append((index, f"moment {index} is shorter than {MIN_SEGMENT:g} s"))
        else:
            valid.append((index, start, end))
    ordered = sorted(valid, key=lambda item: (item[1], item[2]))
    kept: list[tuple[int, float, float]] = []
    for item in ordered:
        if any(_overlap(item[1], item[2], other[1], other[2]) > _EPS for other in kept):
            later = max([item] + [other for other in kept if _overlap(item[1], item[2], other[1], other[2]) > _EPS], key=lambda it: it[0])
            problems.append((later[0], f"moment {later[0]} overlaps another moment in the source"))
            kept = [other for other in kept if other[0] != later[0]]
            if later[0] != item[0]:
                kept.append(item)
        else:
            kept.append(item)
    if not problems:
        return _check(name, True, None, f"{len(segments)} moment{'s' if len(segments) != 1 else ''} inside the source, no overlaps"), []
    patches: list[dict[str, Any]] = []
    remaining = len(segments)
    seen: set[int] = set()
    for index, why in problems:
        if index in seen:
            continue
        seen.add(index)
        if remaining > 1:
            patches.append(_patch("remove", index, None, why[0].upper() + why[1:] + "; remove it."))
            remaining -= 1
    first = problems[0][0]
    at = None
    try:
        at = output_time(plan, first, segments[first - 1]["start"])
    except (ValueError, TypeError):
        at = None
    return _check(name, False, at, "; ".join(why for _, why in problems)), patches


def _caption_check(plan: dict[str, Any], segments: list[dict[str, Any]], captions: Any, speech: Any,
                   *, captioned_by: str | None = None, heard_by: str | None = None) -> dict[str, Any] | None:
    """Caption coverage of the transcribed speech in the cut.

    ``captions`` None means captions were not requested; ``speech`` None means no
    transcript was made. Either way nothing can be measured, so there is no check.
    """
    name = "auto: captions cover the speech"
    if captions is None or speech is None:
        return None
    inputs = [captioned_by, heard_by]
    cues = _ranges(captions)
    bands = _ranges(speech)
    spoken: list[tuple[float, float]] = []
    if _on_timeline(plan):
        spans = plan_timeline(plan)["spans"]
        spoken = sorted((lo, hi) for band in bands for _, lo, hi in map_interval(spans, band[0], band[1]))
    else:
        for index, segment in enumerate(segments, 1):
            start, end = segment["start"], segment["end"]
            if not (math.isfinite(start) and math.isfinite(end)) or end <= start:
                continue
            for band in bands:
                lo, hi = max(start, band[0]), min(end, band[1])
                if hi - lo > _EPS:
                    spoken.append((output_time(plan, index, lo), output_time(plan, index, hi)))
    total = sum(hi - lo for lo, hi in spoken)
    if total <= _EPS:
        return _check(name, True, None, "Captions were requested. The transcript holds no speech in this cut, so there was nothing to caption.", inputs=inputs)
    covered = 0.0
    for lo, hi in spoken:
        covered += sum(_overlap(lo, hi, cue[0], cue[1]) for cue in cues)
    share = min(1.0, covered / total)
    note = f"{share * 100:.0f}% of {total:.1f} s of speech has a caption"
    if share >= CAPTION_COVERAGE:
        return _check(name, True, None, note, inputs=inputs)
    gap_at: float | None = None
    for lo, hi in spoken:
        point = lo
        for cue in cues:
            if cue[0] <= point < cue[1]:
                point = cue[1]
        if point < hi - _EPS:
            gap_at = point
            break
    return _check(name, False, gap_at, note, inputs=inputs)


_ASPECT_RATIOS = {"9:16": 9 / 16, "16:9": 16 / 9, "1:1": 1.0, "4:5": 4 / 5}


def subject_cutout_check(stats: Any, *, min_coverage: float = CUTOUT_COVERAGE, max_flicker: float = CUTOUT_FLICKER,
                         masked_by: str = SAM) -> dict[str, Any]:
    """Grade a cutout from the masks that went into its matte: evidence, not a guarantee.

    Coverage is the share of finished frames with their own mask. Flicker is how
    much the subject's area jumps between frames inside a shot, the 95th
    percentile, so one bad frame shows up without one fast move failing a clip.
    """
    name = "auto: subject cutout covers the subject"
    if not isinstance(stats, dict) or not stats.get("frames"):
        return _check(name, True, None, "not requested")
    coverage, jitter = _finite(stats.get("coverage")) or 0.0, _finite(stats.get("flicker")) or 0.0
    held, empty = int(stats.get("filled_frames") or 0), int(stats.get("empty_frames") or 0)
    note = f"SAM masked {coverage:.1%} of {int(stats['frames'])} frames, with {jitter:.1%} area flicker"
    if held:
        note += f"; {held} held from a neighbouring frame"
    if empty:
        note += f"; {empty} with no subject, so only the background shows"
    return _check(name, coverage >= min_coverage and jitter <= max_flicker, _finite(stats.get("first_missing")), note, inputs=[masked_by])


def hero_word_check(hero: Any, stats: Any, *, duration: float, min_coverage: float = CUTOUT_COVERAGE,
                    max_hidden: float = HERO_HIDDEN) -> dict[str, Any]:
    """Grade a hero word from its window, Spark's spoken time, and the matte SAM made for it: evidence, not a guarantee.

    It passes when the window sits inside the cut for 0.5 to 3 seconds and
    starts as the word is said, the matte has a frame for every frame of the
    window, SAM masked the subject in most of them, and the subject covers no
    more than ``max_hidden`` of the word. A word the subject does not touch
    passes with a note: it reads, but it is not behind anything. When the
    matte report measured the background (``contrast_by_color``), the edit's
    colour must reach ``HERO_CONTRAST`` against it.
    """
    name = "auto: hero word sits behind the subject"
    if not isinstance(hero, dict) or not hero.get("word"):
        return _check(name, True, None, "not requested")
    start, end = _finite(hero.get("start")), _finite(hero.get("end"))
    if start is None or end is None:
        return _check(name, False, None, "the hero word has no window", inputs=[SPARK])
    problems: list[str] = []
    if start < 0 or end > duration + 1e-3 or not 0.5 - 1e-6 <= end - start <= 3.0 + 1e-6:
        problems.append(f"its {start:.2f}-{end:.2f} s window does not fit the {duration:.2f} s cut for 0.5 to 3 s")
    spoken = hero.get("spoken")
    said = _finite(spoken[0]) if isinstance(spoken, (list, tuple)) and spoken else None
    if said is not None and not start - 1e-3 <= said <= min(end, start + 0.5):
        problems.append(f"the word is said at {said:.2f} s, not as the window opens")
    if not isinstance(stats, dict) or not stats.get("frames"):
        problems.append("no subject matte was made for its window")
        return _check(name, False, start, f"\u201c{hero['word']}\u201d: " + "; ".join(problems), inputs=[SAM, SPARK])
    frames, wanted = int(stats["frames"]), stats.get("window_frames")
    coverage, hidden = _finite(stats.get("coverage")) or 0.0, _finite(stats.get("hidden"))
    if isinstance(wanted, int) and frames != wanted:
        problems.append(f"the matte has {frames} frames for a {wanted}-frame window")
    if coverage < min_coverage:
        problems.append(f"SAM masked the subject in only {coverage:.0%} of its {frames} frames")
    if hidden is not None and hidden > max_hidden:
        problems.append(f"the subject covers {hidden:.0%} of the word, too much to read")
    # The contrast of the colour this edit draws, measured behind the word where the subject is not.
    contrasts, color = stats.get("contrast_by_color"), str(hero.get("color") or "").upper()
    ratio = _finite(contrasts.get(color)) if isinstance(contrasts, dict) else None
    if ratio is not None and ratio < HERO_CONTRAST:
        problems.append(f"the word's colour reads at only {ratio:.1f}:1 against what is behind it, under {HERO_CONTRAST:g}:1")
    note = f"\u201c{hero['word']}\u201d for {end - start:.2f} s from {start:.2f} s; SAM masked {coverage:.0%} of {frames} frames"
    if hidden is not None:
        note += f"; the subject covers {hidden:.0%} of the word" + (", so it is not behind anything" if hidden < 0.01 else "")
    if ratio is not None:
        note += f"; {color} reads at {ratio:.1f}:1 against the background"
    if problems:
        found = "; ".join(problems)
        note += ". " + found[:1].upper() + found[1:]
    return _check(name, not problems, start, note, inputs=[SAM, SPARK])


def subject_in_frame_check(points: Any, *, duration: float, aspect: str, source_width: float, source_height: float,
                           max_gap: float = 1.0, min_coverage: float = 0.8, min_inside: float = 0.95, tracked_by: str = SAM) -> dict[str, Any]:
    """Grade a SAM track against the delivered cut: evidence, not a guarantee.

    ``points`` are boxes on the finished clock in source-normalised units. The
    check passes when boxes cover most of the cut without a long gap and the
    subject fits inside the reframed crop that follows it. A missing
    track is reported as not requested, never as a pass about the subject.
    """
    name = "auto: subject stays in frame (SAM 3.1)"
    boxes = []
    for item in points if isinstance(points, list) else []:
        if not isinstance(item, dict):
            continue
        values = [_finite(item.get(key)) for key in ("time", "x", "y", "width", "height")]
        if any(value is None for value in values):
            continue
        boxes.append(tuple(values))
    if not boxes or duration <= 0 or source_width <= 0 or source_height <= 0:
        return _check(name, True, None, "not requested")
    boxes.sort()
    covered, longest_gap, gap_at, previous = 0.0, 0.0, None, 0.0
    for time, *_ in boxes:
        gap = time - previous
        if gap > longest_gap:
            longest_gap, gap_at = gap, previous
        previous = time
    tail = duration - previous
    if tail > longest_gap:
        longest_gap, gap_at = tail, previous
    # Time actually tracked: each box holds until the next one, for at most
    # ``max_gap``, so a track that is dense in one stretch and absent elsewhere
    # covers only that stretch.
    times = [box[0] for box in boxes]
    covered = min(duration, sum(max(0.0, min(max_gap, nxt - cur)) for cur, nxt in zip(times, [*times[1:], duration])))
    coverage = covered / duration
    # The reframe crops the source to the target shape around the box centre,
    # clamped to the picture; the whole box must fit inside that window, so a
    # subject wider than the crop is reported as out of frame.
    target = _ASPECT_RATIOS.get(aspect, 9 / 16)
    source_ratio = source_width / source_height
    crop_w = min(1.0, target / source_ratio)
    crop_h = min(1.0, source_ratio / target)
    inside = 0
    for _, x, y, width, height in boxes:
        cx, cy = x + width / 2, y + height / 2
        centre_x = min(max(cx, crop_w / 2), 1 - crop_w / 2)
        centre_y = min(max(cy, crop_h / 2), 1 - crop_h / 2)
        fits_x = x >= centre_x - crop_w / 2 - 1e-6 and x + width <= centre_x + crop_w / 2 + 1e-6
        fits_y = y >= centre_y - crop_h / 2 - 1e-6 and y + height <= centre_y + crop_h / 2 + 1e-6
        if fits_x and fits_y:
            inside += 1
    inside_share = inside / len(boxes)
    passed = coverage >= min_coverage and longest_gap <= max_gap and inside_share >= min_inside
    note = (f"{len(boxes)} tracked boxes cover {coverage:.0%} of {duration:.1f} s; longest gap {longest_gap:.1f} s; "
            f"subject inside the {aspect} crop in {inside_share:.0%} of boxes")
    at = None if passed else (round(gap_at, 3) if longest_gap > max_gap and gap_at is not None else round(boxes[0][0], 3))
    return _check(name, passed, at, note, inputs=[tracked_by])


def brief_bookends(goal: Any) -> dict[str, bool]:
    """Whether an English brief names how the story opens or ends ("start with ...", "end on ...")."""
    text = goal if isinstance(goal, str) else ""
    return {"opening": bool(_OPENING_HINT.search(text)), "ending": bool(_ENDING_HINT.search(text))}


def _clip_spans(clips: Any) -> list[tuple[float, float, str]]:
    """``(start, end, name)`` for each clip of a project on the source clock, in order."""
    out: list[tuple[float, float, str]] = []
    for item in clips or []:
        if isinstance(item, dict):
            start, end, name = _finite(item.get("start")), _finite(item.get("end")), str(item.get("name") or "")
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            start, end, name = _finite(item[0]), _finite(item[1]), ""
        else:
            continue
        if start is not None and end is not None and end > start:
            out.append((start, end, name))
    return sorted(out)


def _home_clip(start: float, end: float, spans: list[tuple[float, float, str]]) -> tuple[int, list[int]] | None:
    """The clip a moment mostly plays, and every clip it touches, when it touches more than one."""
    touched = [index for index, (low, high, _) in enumerate(spans) if _overlap(start, end, low, high) > _EPS]
    if len(touched) < 2:
        return None
    home = max(touched, key=lambda index: (_overlap(start, end, spans[index][0], spans[index][1]), -index))
    return home, touched


def snap_to_clips(segments: list[dict[str, Any]], clips: Any, *, limit: float = CLIP_SNAP) -> list[dict[str, Any]]:
    """Trim a moment that strays ``limit`` seconds or less into a neighbouring clip back to the clip it mostly plays.

    A project joins its clips into one file, so a planner's range can open with
    a few frames of the previous clip. A moment that crosses further is left as
    it is; the clip check then fails and proposes the trim.
    """
    spans = _clip_spans(clips)
    if len(spans) < 2:
        return list(segments)
    out: list[dict[str, Any]] = []
    for segment in segments:
        start = _finite(segment.get("start")) if isinstance(segment, dict) else None
        end = _finite(segment.get("end")) if isinstance(segment, dict) else None
        found = _home_clip(start, end, spans) if start is not None and end is not None and end > start else None
        if found is None:
            out.append(segment)
            continue
        low, high, _ = spans[found[0]]
        new_start, new_end = max(start, low), min(end, high)
        if new_start - start > limit + _EPS or end - new_end > limit + _EPS or new_end - new_start < MIN_SEGMENT - _EPS:
            out.append(segment)
            continue
        item = dict(segment, start=round(new_start, 3), end=round(new_end, 3))
        moved = []
        if new_start > start + _EPS:
            moved.append(f"start moved {new_start - start:.2f} s to {new_start:.2f} s")
        if new_end < end - _EPS:
            moved.append(f"end moved {end - new_end:.2f} s to {new_end:.2f} s")
        _note(item, "kept inside one clip: " + ", ".join(moved))
        out.append(item)
    return out


def _clip_check(plan: dict[str, Any], segments: list[dict[str, Any]], clips: Any) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Each moment plays from one clip of a multi-clip project. No check for a single clip."""
    spans = _clip_spans(clips)
    if len(spans) < 2:
        return None, []
    name = "auto: each moment stays inside one clip"
    problems: list[str] = []
    patches: list[dict[str, Any]] = []
    first_at: float | None = None
    for index, segment in enumerate(segments, 1):
        if not _alive(segment):
            continue
        start, end = segment["start"], segment["end"]
        found = _home_clip(start, end, spans)
        if found is None:
            continue
        home, touched = found
        low, high, _ = spans[home]
        others = ", ".join(f"{_overlap(start, end, spans[other][0], spans[other][1]):.2f} s of clip {other + 1}" for other in touched if other != home)
        problems.append(f"moment {index} plays clip {home + 1} with {others}")
        if first_at is None:
            try:
                first_at = output_time(plan, index, start)
            except (ValueError, TypeError):
                first_at = None
        new_start, new_end = max(start, low), min(end, high)
        if new_end - new_start < MIN_SEGMENT - _EPS:
            continue
        if new_start > start + _EPS:
            patches.append(_patch("trim_start", index, new_start, f"Moment {index} opens with {new_start - start:.2f} s of clip {home}; start it at {new_start:.2f} s, where clip {home + 1} begins."))
        if new_end < end - _EPS:
            patches.append(_patch("trim_end", index, new_end, f"Moment {index} runs {end - new_end:.2f} s into clip {home + 2}; end it at {new_end:.2f} s, where clip {home + 1} ends."))
    alive = sum(1 for segment in segments if _alive(segment))
    if not problems:
        return _check(name, True, None, f"{alive} moment{'s' if alive != 1 else ''}, each inside one of the {len(spans)} clips."), []
    joined = "; ".join(problems)
    return _check(name, False, first_at, joined[:1].upper() + joined[1:] + "."), patches


def _ending_check(plan: dict[str, Any], segments: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The brief named an ending and the planner placed a moment last for it: that moment still closes the cut."""
    found = _bookend_range(plan, "ending")
    if found is None:
        return None
    start, end, label = found
    named = f"\u201c{label}\u201d" if label else f"the moment at {start:.2f} to {end:.2f} s"
    name = "auto: the cut ends where the brief asked"
    alive = [(index, segment) for index, segment in enumerate(segments, 1) if _alive(segment)]
    if alive and _overlap(alive[-1][1]["start"], alive[-1][1]["end"], start, end) > _EPS:
        return _check(name, True, None, f"The last moment is still {named}, which the planner placed last for the brief's ending.")
    holding = next((index for index, segment in alive if _overlap(segment["start"], segment["end"], start, end) > _EPS), None)
    where = f"It is now moment {holding} of {len(alive)}." if holding else "It is no longer in the cut."
    at = None
    if alive:
        try:
            at = output_time(plan, alive[-1][0], alive[-1][1]["start"])
        except (ValueError, TypeError):
            at = None
    return _check(name, False, at, f"The brief names an ending, and the planner placed {named} last for it. {where}")


def lint_plan(plan: dict[str, Any], *, target_duration: float, duration: float,
              sentences: list[dict[str, Any]] | None = None, captions: list[dict[str, Any]] | None = None,
              speech: list[dict[str, Any]] | None = None, strict: bool = False,
              heard_by: str | None = None, captioned_by: str | None = None, clips: Any = None) -> dict[str, Any]:
    """Grade a plan. ``sentences`` and ``speech`` are on the source clock; ``captions`` on the output clock.

    ``strict`` is for plans a model just patched: any cut left inside a sentence
    gets a patch to the nearest boundary, however far away it is. ``heard_by`` and
    ``captioned_by`` name the models that wrote the transcript and the captions,
    when the caller knows; they become the ``inputs`` of the checks that read them.
    ``clips`` are a multi-clip project's ``{name, start, end}`` ranges on the source clock.

    A check that has nothing to measure (no transcript, captions not requested,
    one clip, no named ending) is left out rather than passed.
    """
    duration = float(duration)
    # The planner aims for min(target, source); a short clip cannot run longer.
    asked = float(target_duration)
    target = min(asked, duration) if duration > 0 else asked
    segments = _segments(plan)
    checks: list[dict[str, Any]] = []
    patches: list[dict[str, Any]] = []
    check, more = _sentence_check(plan, segments, _ranges(sentences), duration, strict=strict, heard_by=heard_by)
    checks.append(check)
    patches.extend(more)
    found, more = _duration_check(plan, segments, target, asked=asked, source=duration if duration > 0 else None)
    checks.extend(found)
    patches.extend(more)
    check, more = _bounds_check(plan, segments, duration)
    checks.append(check)
    patches.extend(more)
    check, more = _clip_check(plan, segments, clips)
    checks.append(check)
    patches.extend(more)
    checks.append(_ending_check(plan, segments))
    checks.append(_caption_check(plan, segments, captions, speech, captioned_by=captioned_by, heard_by=heard_by))
    return {"checks": [item for item in checks if item is not None], "patches": patches}


def _note(segment: dict[str, Any], text: str) -> None:
    reason = str(segment.get("reason") or "").strip()
    note = f" ({text})"
    if len(reason) + len(note) > _REASON_LIMIT:
        reason = reason[: max(0, _REASON_LIMIT - len(note) - 1)].rstrip() + "…"
    segment["reason"] = (reason + note).strip()


def _split_point(seconds: float, start: float, end: float, sentences: list[tuple[float, float]]) -> float | None:
    """Where a split really lands: a time inside a sentence moves to that sentence's nearer edge."""
    inside = next((s for s in sentences if s[0] + SENTENCE_TOLERANCE < seconds < s[1] - SENTENCE_TOLERANCE), None)
    if inside is None:
        return seconds
    edges = [edge for edge in inside if start + MIN_SEGMENT - _EPS <= edge <= end - MIN_SEGMENT + _EPS]
    return min(edges, key=lambda edge: abs(edge - seconds)) if edges else None


def _split_motion(item: dict[str, Any], right: dict[str, Any], share: float) -> None:
    """Keep a two-keyframe move continuous across a split: both halves meet at the midpoint."""
    motion = item.get("motion")
    begin = motion.get("from") if isinstance(motion, dict) else None
    finish = motion.get("to") if isinstance(motion, dict) else None
    if not (isinstance(begin, dict) and isinstance(finish, dict)):
        return
    middle: dict[str, Any] = {}
    for key, value in begin.items():
        low, high = _finite(value), _finite(finish.get(key))
        if low is None or high is None:
            return
        middle[key] = round(low + (high - low) * share, 4)
    item["motion"] = {"from": dict(begin), "to": dict(middle)}
    right["motion"] = {"from": dict(middle), "to": dict(finish)}


def apply_patches(plan: dict[str, Any], patches: list[dict[str, Any]], *, duration: float) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Apply structured patches; never raise. Returns ``(new_plan, applied)``.

    Moment patches name a 1-based position in the plan as given, even after an
    earlier patch removed, moved, or split that moment. Splits run first; a later
    patch reaches the half holding its ``seconds``. ``trim_start`` and ``trim_end``
    take the new source-clock boundary, ``move_before`` takes the other moment's
    number in ``seconds``, ``split`` takes the source time to cut at, and
    ``set_speed`` takes the rate in ``value``. ``set_edit`` changes one allowlisted
    setting from ``path`` to ``value``, and the whole edit must still pass
    ``validate_edit``.

    A patch that would leave a moment shorter than ``MIN_SEGMENT``, overlap
    another, leave the source, exceed ``MAX_MOMENTS`` or ``MAX_STORY_SECONDS``, or
    change a setting to something Briefcut cannot apply is reported with
    ``skipped`` set and otherwise ignored.
    """
    duration = float(duration)
    source = plan if isinstance(plan, dict) else {}
    originals = source.get("segments") if isinstance(source.get("segments"), list) else []
    work: list[dict[str, Any] | None] = []
    for item in originals:
        if isinstance(item, dict) and _finite(item.get("start")) is not None and _finite(item.get("end")) is not None:
            clean = dict(item, start=float(item["start"]), end=float(item["end"]))
            clean.pop("keep", None)  # computed, and stale the moment anything moves
            work.append(clean)
        else:
            work.append(None)
    given = len(work)
    order = [index for index, item in enumerate(work) if item is not None]
    halves: dict[int, list[int]] = {}
    edit: dict[str, Any] | None = dict(source["edit"]) if isinstance(source.get("edit"), dict) else None
    analysis = source.get("analysis") if isinstance(source.get("analysis"), dict) else {}
    sentences = _ranges(analysis.get("sentences"))
    applied: list[dict[str, Any]] = []

    def record(patch: dict[str, Any], before: Any, after: Any, skipped: str | None) -> None:
        entry = {
            "op": patch.get("op"), "segment": patch.get("segment"),
            "before": before, "after": after,
            "reason": str(patch.get("reason") or ""), "source": str(patch.get("source") or "auto"),
            "skipped": skipped,
        }
        for key in ("path", "value", "seconds"):
            # A record must survive JSON, whatever was sent.
            if patch.get(key) is not None and (not isinstance(patch[key], float) or math.isfinite(patch[key])):
                entry[key] = patch[key]
        entry["summary"] = describe_patch(dict(entry, after=None if skipped else after))
        applied.append(entry)

    def snapshot(item: dict[str, Any] | None) -> dict[str, float] | None:
        return None if item is None else {"start": round(item["start"], 3), "end": round(item["end"], 3)}

    def collides(index: int, start: float, end: float) -> bool:
        return any(other is not None and j != index and j in order and _overlap(start, end, other["start"], other["end"]) > _EPS
                   for j, other in enumerate(work))

    def story() -> float:
        return float(plan_timeline(dict(source, segments=[work[index] for index in order], edit=edit or {}))["duration"])

    def members(index: int) -> list[int]:
        return [part for part in halves.get(index, [index]) if part in order]

    def part_of(index: int, seconds: float | None, *, last: bool = False) -> int | None:
        parts = members(index)
        for part in parts if seconds is not None else ():
            if work[part]["start"] - _EPS <= seconds <= work[part]["end"] + _EPS:
                return part
        return (parts[-1] if last else parts[0]) if parts else None

    def set_edit(patch: dict[str, Any]) -> None:
        nonlocal edit
        path = patch.get("path")
        field = EDIT_FIELDS.get(path) if isinstance(path, str) else None
        if field is None:
            record(patch, None, None, "that setting cannot be changed by a patch")
            return
        current = edit if edit is not None else default_edit()
        value = patch.get("value")
        try:
            # Timed items are fitted to the finished story when the plan is validated.
            finished = validate_edit(write_edit(current, path, value), duration=_LONGEST_EDIT)
        except KeyError:
            record(patch, None, None, f"{field.name.lower()} is not in use in this story")
            return
        except EditError as exc:
            record(patch, None, None, str(exc))
            return
        if field.kind == "append" and finished["texts"][-1]["start"] >= story():
            record(patch, None, None, "that time is past the end of the story")
            return
        before = read_edit(current, path) if field.kind != "append" else None
        after = read_edit(finished, path) if field.kind != "append" else finished["texts"][-1]
        if field.kind != "append" and before == after:
            record(patch, before, after, "already set that way")
            return
        edit = finished
        record(dict(patch, value=after), before, after, None)

    # Splits run first, so every other patch can name either half of the moment it meant.
    queued = [patch for patch in patches or [] if isinstance(patch, dict)]
    queued.sort(key=lambda patch: patch.get("op") != "split")
    for patch in queued:
        op = patch.get("op")
        segment = patch.get("segment")
        if op not in PATCH_OPS:
            record(patch, None, None, "unknown patch operation")
            continue
        if op == "set_edit":
            set_edit(patch)
            continue
        if isinstance(segment, bool) or not isinstance(segment, int) or not 1 <= segment <= given:
            record(patch, None, None, "unknown moment")
            continue
        seconds = _finite(patch.get("seconds"))
        # move_before's ``seconds`` names another moment, not a source time, so it never picks a half.
        chosen = part_of(segment - 1, None if op == "move_before" else seconds, last=op == "trim_end") if work[segment - 1] is not None else None
        if chosen is None:
            record(patch, None, None, "moment already removed")
            continue
        index = chosen
        item = work[index]
        before = snapshot(item)
        if op == "remove":
            parts = members(segment - 1)
            if len(order) <= len(parts):
                record(patch, before, before, "would leave no moments")
                continue
            for part in parts:
                order.remove(part)
            record(patch, before, None, None)
            continue
        if op == "move_before":
            if seconds is None or seconds != int(seconds) or not 1 <= int(seconds) <= given:
                record(patch, before, before, "unknown target moment")
                continue
            target = int(seconds) - 1
            targets = members(target)
            if target == segment - 1 or not targets:
                record(patch, before, before, "target moment is unavailable")
                continue
            parts = members(segment - 1)
            for part in parts:
                order.remove(part)
            at = min(order.index(part) for part in targets)
            order[at:at] = parts
            _note(item, f"moved before moment {target + 1}")
            record(patch, before, before, None)
            continue
        if op == "set_speed":
            value = _finite(patch.get("value"))
            if value is None:
                record(patch, before, before, "missing speed")
                continue
            if not MIN_SPEED <= value <= MAX_SPEED:
                record(patch, before, before, f"speed must be between {MIN_SPEED:g}× and {MAX_SPEED:g}×")
                continue
            # Without a source time the whole moment changes speed, every half of a split one.
            targets = [index] if seconds is not None else members(segment - 1)
            old = segment_speed(item)
            if all(abs(segment_speed(work[part]) - value) < 1e-6 for part in targets):
                record(patch, dict(before, speed=old), dict(before, speed=old), "already at that speed")
                continue
            kept = {part: work[part].get("speed") for part in targets}
            for part in targets:
                work[part]["speed"] = round(value, 4)
            if story() > MAX_STORY_SECONDS:
                for part, speed in kept.items():
                    if speed is None:
                        work[part].pop("speed", None)
                    else:
                        work[part]["speed"] = speed
                record(patch, dict(before, speed=old), dict(before, speed=old), f"would run past {MAX_STORY_SECONDS:g} s")
                continue
            for part in targets:
                _note(work[part], f"plays at {value:g}× speed")
            record(patch, dict(before, speed=old), dict(before, speed=round(value, 4)), None)
            continue
        if seconds is None:
            record(patch, before, before, "missing seconds")
            continue
        if op == "split":
            if len(order) >= MAX_MOMENTS:
                record(patch, before, before, f"a story holds at most {MAX_MOMENTS} moments")
                continue
            if not item["start"] + MIN_SEGMENT - _EPS <= seconds <= item["end"] - MIN_SEGMENT + _EPS:
                record(patch, before, before, f"each half must last at least {MIN_SEGMENT:g} s")
                continue
            point = _split_point(seconds, item["start"], item["end"], sentences)
            if point is None:
                record(patch, before, before, "would cut through a sentence")
                continue
            left_end = round(point, 3)
            right = dict(item, start=left_end)
            share = (left_end - item["start"]) / max(item["end"] - item["start"], _EPS)
            _split_motion(item, right, share)
            right["label"] = (str(item.get("label") or f"Moment {segment}")[:88] + " (continued)")
            item["end"] = left_end
            _note(item, f"split at {left_end:.2f} s")
            work.append(right)
            parts = halves.setdefault(segment - 1, [segment - 1])
            parts.insert(parts.index(index) + 1, len(work) - 1)
            order.insert(order.index(index) + 1, len(work) - 1)
            record(dict(patch, seconds=left_end), before, before, None)
            continue
        start, end = item["start"], item["end"]
        if op == "trim_start":
            start = seconds
        else:
            end = seconds
        if start < 0 or end > duration + _EPS:
            record(patch, before, before, "outside the source")
            continue
        if end - start < MIN_SEGMENT - _EPS:
            record(patch, before, before, f"would be shorter than {MIN_SEGMENT:g} s")
            continue
        if collides(index, start, end):
            record(patch, before, before, "would overlap another moment")
            continue
        old_start, old_end = item["start"], item["end"]
        item["start"], item["end"] = round(start, 3), round(min(end, duration), 3)
        if story() > MAX_STORY_SECONDS:
            item["start"], item["end"] = old_start, old_end
            record(patch, before, before, f"would run past {MAX_STORY_SECONDS:g} s")
            continue
        _note(item, f"{'start' if op == 'trim_start' else 'end'} moved to {seconds:.2f} s")
        record(patch, before, snapshot(item), None)

    segments = [work[index] for index in order if work[index] is not None]
    new_plan = dict(plan) if isinstance(plan, dict) else {}
    new_plan["segments"] = segments
    if edit is not None:
        new_plan["edit"] = edit
    new_plan["duration"] = float(plan_timeline(new_plan)["duration"])
    return new_plan, applied
