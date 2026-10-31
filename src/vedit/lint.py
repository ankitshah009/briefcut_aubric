"""Deterministic grading of a story plan, and pure patch application.

``lint_plan`` grades a plan against the transcript, the target length, the
source bounds and (when asked) caption coverage. Every check is a plain
``{"name", "passed", "at", "note"}`` record; ``at`` is a time on the OUTPUT
timeline so a viewer can scrub straight to it. Checks that fail also propose
structured patches against the plan, which ``apply_patches`` applies without
ever raising: a patch that would break the plan is reported as skipped.

This module does no I/O and never calls a provider.
"""

from __future__ import annotations

import math
from typing import Any

MIN_SEGMENT = 0.5
SENTENCE_TOLERANCE = 0.15
SNAP_EXTEND = 1.6
SNAP_SHRINK = 0.8
CAPTION_COVERAGE = 0.8
PATCH_OPS = ("trim_start", "trim_end", "remove", "move_before")
_REASON_LIMIT = 600
_EPS = 1e-6


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


def output_time(plan: dict[str, Any], segment_index: int, source_seconds: float) -> float:
    """Output-timeline seconds for a source-clock time inside the 1-based segment."""
    segments = _segments(plan)
    if not 1 <= segment_index <= len(segments):
        raise ValueError(f"segment {segment_index} is not in the plan")
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


def _check(name: str, passed: bool, at: float | None, note: str) -> dict[str, Any]:
    return {"name": name, "passed": passed, "at": None if at is None else round(at, 3), "note": note}


def _sentence_check(plan: dict[str, Any], segments: list[dict[str, Any]], sentences: list[tuple[float, float]], duration: float) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    name = "auto: cuts land on sentence boundaries"
    if not sentences:
        return _check(name, True, None, "no transcript"), []
    patches: list[dict[str, Any]] = []
    first_at: float | None = None
    offenders = 0
    for index, segment in enumerate(segments, 1):
        start, end = segment["start"], segment["end"]
        if not (math.isfinite(start) and math.isfinite(end)) or end <= start:
            continue
        for edge in ("start", "end"):
            boundary = start if edge == "start" else end
            inside = next((s for s in sentences if s[0] + SENTENCE_TOLERANCE < boundary < s[1] - SENTENCE_TOLERANCE), None)
            if inside is None:
                continue
            offenders += 1
            if first_at is None:
                first_at = output_time(plan, index, boundary)
            options: list[tuple[float, float]] = []
            if edge == "start":
                # Moving the start earlier extends the moment; later shrinks it.
                extend, shrink = boundary - inside[0], inside[1] - boundary
                if extend <= SNAP_EXTEND and inside[0] >= 0 and end - inside[0] >= MIN_SEGMENT:
                    options.append((extend, inside[0]))
                if shrink <= SNAP_SHRINK and end - inside[1] >= MIN_SEGMENT:
                    options.append((shrink, inside[1]))
            else:
                extend, shrink = inside[1] - boundary, boundary - inside[0]
                if extend <= SNAP_EXTEND and inside[1] <= duration + _EPS and inside[1] - start >= MIN_SEGMENT:
                    options.append((extend, min(inside[1], duration)))
                if shrink <= SNAP_SHRINK and inside[0] - start >= MIN_SEGMENT:
                    options.append((shrink, inside[0]))
            if not options:
                continue
            _, target = min(options)
            patches.append(_patch(
                "trim_start" if edge == "start" else "trim_end", index, target,
                f"Moment {index} {'starts' if edge == 'start' else 'ends'} mid-sentence at {boundary:.2f} s; snap to the sentence boundary at {target:.2f} s.",
            ))
    if offenders == 0:
        return _check(name, True, None, "every cut sits on a sentence boundary"), []
    return _check(name, False, first_at, f"{offenders} cut{'s' if offenders != 1 else ''} inside a sentence"), patches


def _duration_check(segments: list[dict[str, Any]], target: float) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    name = "auto: duration matches the target"
    total = sum(max(0.0, s["end"] - s["start"]) for s in segments if math.isfinite(s["start"]) and math.isfinite(s["end"]))
    low, high = 0.5 * target, target * 1.15 + 0.5
    note = f"{total:.1f} s cut vs {target:.1f} s target"
    if low - _EPS <= total <= high + _EPS:
        return _check(name, True, None, note), []
    patches: list[dict[str, Any]] = []
    if total > high:
        excess = total - target
        alive = sum(1 for s in segments if math.isfinite(s["start"]) and math.isfinite(s["end"]) and s["end"] > s["start"])
        for index in range(len(segments), 0, -1):
            if excess <= _EPS:
                break
            segment = segments[index - 1]
            if not (math.isfinite(segment["start"]) and math.isfinite(segment["end"])) or segment["end"] <= segment["start"]:
                continue
            length = segment["end"] - segment["start"]
            if length - excess >= MIN_SEGMENT:
                patches.append(_patch("trim_end", index, segment["end"] - excess, f"The cut runs {total - target:.1f} s over the {target:.1f} s target; shorten moment {index}."))
                excess = 0.0
            elif alive > 1:
                patches.append(_patch("remove", index, None, f"The cut runs {total - target:.1f} s over the {target:.1f} s target; drop moment {index}."))
                excess -= length
                alive -= 1
        return _check(name, False, round(target, 3), note + " (over by " + f"{total - target:.1f} s)"), patches
    return _check(name, False, None, note + " (under by " + f"{target - total:.1f} s; choose a longer moment)"), []


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
        elif end - start < MIN_SEGMENT - _EPS:
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


def _caption_check(plan: dict[str, Any], segments: list[dict[str, Any]], captions: Any, speech: Any) -> dict[str, Any]:
    name = "auto: captions cover the speech"
    if captions is None or speech is None:
        return _check(name, True, None, "not requested")
    cues = _ranges(captions)
    bands = _ranges(speech)
    spoken: list[tuple[float, float]] = []
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
        return _check(name, True, None, "no speech selected")
    covered = 0.0
    for lo, hi in spoken:
        covered += sum(_overlap(lo, hi, cue[0], cue[1]) for cue in cues)
    share = min(1.0, covered / total)
    note = f"{share * 100:.0f}% of {total:.1f} s of speech has a caption"
    if share >= CAPTION_COVERAGE:
        return _check(name, True, None, note)
    gap_at: float | None = None
    for lo, hi in spoken:
        point = lo
        for cue in cues:
            if cue[0] <= point < cue[1]:
                point = cue[1]
        if point < hi - _EPS:
            gap_at = point
            break
    return _check(name, False, gap_at, note)


_ASPECT_RATIOS = {"9:16": 9 / 16, "16:9": 16 / 9, "1:1": 1.0, "4:5": 4 / 5}


def subject_in_frame_check(points: Any, *, duration: float, aspect: str, source_width: float, source_height: float,
                           max_gap: float = 1.0, min_coverage: float = 0.8, min_inside: float = 0.95) -> dict[str, Any]:
    """Grade a SAM track against the delivered cut: evidence, not a guarantee.

    ``points`` are boxes on the finished clock in source-normalised units. The
    check passes when boxes cover most of the cut without a long gap and the
    subject's centre sits inside the reframed crop that follows it. A missing
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
    step = max(1 / 30, duration / max(1, len(boxes)))
    covered = min(duration, len(boxes) * step)
    coverage = covered / duration
    # The reframe crops the source to the target shape around the box centre,
    # clamped to the picture; the centre must stay inside that window.
    target = _ASPECT_RATIOS.get(aspect, 9 / 16)
    source_ratio = source_width / source_height
    crop_w = min(1.0, target / source_ratio)
    crop_h = min(1.0, source_ratio / target)
    inside = 0
    for _, x, y, width, height in boxes:
        cx, cy = x + width / 2, y + height / 2
        centre_x = min(max(cx, crop_w / 2), 1 - crop_w / 2)
        centre_y = min(max(cy, crop_h / 2), 1 - crop_h / 2)
        if abs(cx - centre_x) <= crop_w / 2 + 1e-6 and abs(cy - centre_y) <= crop_h / 2 + 1e-6:
            inside += 1
    inside_share = inside / len(boxes)
    passed = coverage >= min_coverage and longest_gap <= max_gap and inside_share >= min_inside
    note = (f"{len(boxes)} tracked boxes cover {coverage:.0%} of {duration:.1f} s; longest gap {longest_gap:.1f} s; "
            f"subject centre inside the {aspect} crop in {inside_share:.0%} of boxes")
    at = None if passed else (round(gap_at, 3) if longest_gap > max_gap and gap_at is not None else round(boxes[0][0], 3))
    return _check(name, passed, at, note)


def lint_plan(plan: dict[str, Any], *, target_duration: float, duration: float,
              sentences: list[dict[str, Any]] | None = None, captions: list[dict[str, Any]] | None = None,
              speech: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Grade a plan. ``sentences`` and ``speech`` are on the source clock; ``captions`` on the output clock."""
    duration = float(duration)
    target = float(target_duration)
    segments = _segments(plan)
    checks: list[dict[str, Any]] = []
    patches: list[dict[str, Any]] = []
    check, more = _sentence_check(plan, segments, _ranges(sentences), duration)
    checks.append(check)
    patches.extend(more)
    check, more = _duration_check(segments, target)
    checks.append(check)
    patches.extend(more)
    check, more = _bounds_check(plan, segments, duration)
    checks.append(check)
    patches.extend(more)
    checks.append(_caption_check(plan, segments, captions, speech))
    return {"checks": checks, "patches": patches}


def _note(segment: dict[str, Any], text: str) -> None:
    reason = str(segment.get("reason") or "").strip()
    note = f" ({text})"
    if len(reason) + len(note) > _REASON_LIMIT:
        reason = reason[: max(0, _REASON_LIMIT - len(note) - 1)].rstrip() + "…"
    segment["reason"] = (reason + note).strip()


def apply_patches(plan: dict[str, Any], patches: list[dict[str, Any]], *, duration: float) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Apply structured patches; never raise. Returns ``(new_plan, applied)``.

    Segment indices are 1-based positions in the plan as given, even after an
    earlier patch removed or moved a moment. Patches that would leave a moment
    shorter than ``MIN_SEGMENT``, overlapping another, or outside the source are
    reported with ``skipped`` set and otherwise ignored.
    """
    duration = float(duration)
    originals = plan.get("segments") if isinstance(plan, dict) and isinstance(plan.get("segments"), list) else []
    work: list[dict[str, Any] | None] = []
    for item in originals:
        if isinstance(item, dict) and _finite(item.get("start")) is not None and _finite(item.get("end")) is not None:
            work.append(dict(item, start=float(item["start"]), end=float(item["end"])))
        else:
            work.append(None)
    order = [index for index, item in enumerate(work) if item is not None]
    applied: list[dict[str, Any]] = []

    def record(patch: dict[str, Any], before: dict[str, Any] | None, after: dict[str, Any] | None, skipped: str | None) -> None:
        applied.append({
            "op": patch.get("op"), "segment": patch.get("segment"),
            "before": before, "after": after,
            "reason": str(patch.get("reason") or ""), "source": str(patch.get("source") or "auto"),
            "skipped": skipped,
        })

    def snapshot(item: dict[str, Any] | None) -> dict[str, float] | None:
        return None if item is None else {"start": round(item["start"], 3), "end": round(item["end"], 3)}

    def collides(index: int, start: float, end: float) -> bool:
        return any(other is not None and j != index and j in order and _overlap(start, end, other["start"], other["end"]) > _EPS
                   for j, other in enumerate(work))

    for patch in patches or []:
        if not isinstance(patch, dict):
            continue
        op = patch.get("op")
        segment = patch.get("segment")
        if op not in PATCH_OPS:
            record(patch, None, None, "unknown patch operation")
            continue
        if isinstance(segment, bool) or not isinstance(segment, int) or not 1 <= segment <= len(work):
            record(patch, None, None, "unknown moment")
            continue
        index = segment - 1
        item = work[index]
        if item is None or index not in order:
            record(patch, None, None, "moment already removed")
            continue
        before = snapshot(item)
        seconds = _finite(patch.get("seconds"))
        if op == "remove":
            if len(order) <= 1:
                record(patch, before, before, "would leave no moments")
                continue
            order.remove(index)
            record(patch, before, None, None)
            continue
        if op == "move_before":
            if seconds is None or seconds != int(seconds) or not 1 <= int(seconds) <= len(work):
                record(patch, before, before, "unknown target moment")
                continue
            target = int(seconds) - 1
            if target == index or target not in order:
                record(patch, before, before, "target moment is unavailable")
                continue
            order.remove(index)
            order.insert(order.index(target), index)
            _note(item, f"moved before moment {target + 1}")
            record(patch, before, before, None)
            continue
        if seconds is None:
            record(patch, before, before, "missing seconds")
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
        item["start"], item["end"] = round(start, 3), round(min(end, duration), 3)
        _note(item, f"{'start' if op == 'trim_start' else 'end'} moved to {seconds:.2f} s")
        record(patch, before, snapshot(item), None)

    segments = [work[index] for index in order if work[index] is not None]
    total = round(sum(item["end"] - item["start"] for item in segments), 3)
    new_plan = dict(plan) if isinstance(plan, dict) else {}
    new_plan["segments"] = segments
    new_plan["duration"] = total
    return new_plan, applied
