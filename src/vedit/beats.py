"""Find the beat in a music track, and move silent cuts onto it.

FFmpeg decodes to PCM and the rest is plain Python: an onset envelope in two
bands (body and transients), the tempo from its autocorrelation with a prior
around 120 BPM, and a dynamic-programming beat tracker that follows a tempo as it
drifts. Each beat is then refined against the waveform, because the envelope's
10 ms grid is coarser than an edit should land.

``snap_cuts`` is pure: it takes the plan and the beats and returns a new plan.
"""

from __future__ import annotations

import copy
import math
from collections.abc import Iterable
from itertools import pairwise
from operator import mul, sub
from typing import Any

from vedit.sound import decode_mono
from vedit.timeline import plan_timeline, segment_speed

RATE = 22_050
HOP = 220  # about 10 ms
ENVELOPE_RATE = RATE / HOP
MIN_BPM, MAX_BPM = 50.0, 200.0
PRIOR_BPM = 120.0
MAX_SECONDS = 600.0
# Autocorrelation at the beat, relative to lag zero, below which nothing repeats.
MIN_PERIODICITY = 0.12
TIGHTNESS = 100.0
ATTACK_WINDOW = 22  # samples, about 1 ms
ATTACK_SEARCH = 0.04  # seconds either side of an envelope beat
MIN_MOMENT = 0.5
SNAP_TOLERANCE = 0.3
SPEECH_GUARD = 0.08


class Beats(list):
    """Beat times in seconds, in order. ``tempo`` is in beats per minute."""

    def __init__(self, times: Iterable[float] = (), tempo: float = 0.0) -> None:
        super().__init__(times)
        self.tempo = tempo


def _rise(values: list[float]) -> list[float]:
    """Log-compressed, half-wave rectified change, normalized to unit peak."""
    peak = max(values) if values else 0.0
    if peak <= 0:
        return [0.0] * len(values)
    logged = [math.log1p(1000.0 * value / peak) for value in values]
    change = [0.0, *(max(0.0, later - earlier) for earlier, later in pairwise(logged))]
    top = max(change)
    return [value / top for value in change] if top > 0 else change


def onset_envelope(samples: Any) -> list[float]:
    """Onset strength every ``HOP`` samples, from low-band and transient energy."""
    body: list[float] = []
    edge: list[float] = []
    for start in range(0, len(samples) - HOP, HOP):
        chunk = samples[start : start + HOP + 1]
        body.append(float(sum(map(mul, chunk, chunk))))
        slope = list(map(sub, chunk[1:], chunk))
        edge.append(float(sum(map(mul, slope, slope))))
    return [a + b for a, b in zip(_rise(body), _rise(edge), strict=True)]


def estimate_period(envelope: list[float]) -> float:
    """The beat period in envelope frames, or 0 when nothing repeats. Pure."""
    mean = sum(envelope) / len(envelope) if envelope else 0.0
    centred = [value - mean for value in envelope]
    energy = sum(map(mul, centred, centred))
    if energy <= 0:
        return 0.0
    shortest = max(2, int(ENVELOPE_RATE * 60 / MAX_BPM))
    longest = min(len(centred) // 2, int(ENVELOPE_RATE * 60 / MIN_BPM) + 1)
    best, best_score, strength = 0, -1.0, 0.0
    scores: dict[int, float] = {}
    for lag in range(shortest, longest + 1):
        score = sum(map(mul, centred, centred[lag:])) / energy
        scores[lag] = score
        # Comparing against neighbours would miss a tempo that sits between lags.
        prior = math.exp(-0.5 * (math.log2(60 * ENVELOPE_RATE / lag / PRIOR_BPM) / 0.9) ** 2)
        weighted = score * prior
        if weighted > best_score:
            best, best_score, strength = lag, weighted, score
    if not best or strength < MIN_PERIODICITY:
        return 0.0
    if best - 1 in scores and best + 1 in scores:
        # A parabola through the peak gives the tempo finer than one lag.
        left, right = scores[best - 1], scores[best + 1]
        bend = left - 2 * scores[best] + right
        if bend < 0:
            return best + 0.5 * (left - right) / bend
    return float(best)


def track(envelope: list[float], period: float) -> list[int]:
    """Ellis's dynamic programming tracker: strong onsets, spaced close to the period."""
    near, far = max(1, round(period / 2)), max(2, round(period * 2))
    penalty = {gap: -TIGHTNESS * math.log(gap / period) ** 2 for gap in range(near, far + 1)}
    score = list(envelope)
    previous = [-1] * len(envelope)
    for frame in range(near, len(envelope)):
        best, source = float("-inf"), -1
        for gap, cost in penalty.items():
            if gap > frame:
                break
            value = score[frame - gap] + cost
            if value > best:
                best, source = value, frame - gap
        if best > 0:
            score[frame] += best
            previous[frame] = source
    # Start from the best ending in the last period, so the trace is not cut short.
    tail = range(max(0, len(score) - round(period)), len(score))
    frame = max(tail, key=lambda index: score[index])
    beats = []
    while frame >= 0:
        beats.append(frame)
        frame = previous[frame]
    beats.reverse()
    # The trace can run on past the music to fill out the grid. An end with no onset is not a beat.
    typical = sorted(envelope[frame] for frame in beats)[len(beats) // 2]
    while len(beats) > 1 and envelope[beats[-1]] < 0.1 * typical:
        beats.pop()
    return beats


def _attack(samples: Any, time: float) -> float:
    """The start of the transient nearest ``time``: where short-term energy first climbs."""
    centre = round(time * RATE)
    reach = round(ATTACK_SEARCH * RATE)
    first = max(0, centre - reach)
    last = min(len(samples) - ATTACK_WINDOW, centre + reach)
    if last <= first:
        return time
    energies = []
    for start in range(first, last, ATTACK_WINDOW):
        chunk = samples[start : start + ATTACK_WINDOW]
        energies.append(sum(map(mul, chunk, chunk)))
    floor, top = min(energies), max(energies)
    if top <= 0 or top < 4 * floor:
        return time
    level = floor + 0.25 * (top - floor)
    for index, energy in enumerate(energies):
        if energy >= level:
            return (first + index * ATTACK_WINDOW) / RATE
    return time


def beats_from_samples(samples: Any) -> Beats:
    """Beats in 16-bit mono samples at ``RATE``. Pure."""
    if len(samples) < 2 * RATE:
        return Beats()
    envelope = onset_envelope(samples)
    period = estimate_period(envelope)
    if period <= 0:
        return Beats()
    times = [_attack(samples, (frame + 0.5) * HOP / RATE) for frame in track(envelope, period)]
    times = sorted({round(time, 4) for time in times})
    if len(times) < 2:
        return Beats()
    # Across the whole track, not a typical gap: refinement can pull one beat onto a
    # nearby hit, which skews any single gap but not the span.
    return Beats(times, tempo=round(60 * (len(times) - 1) / (times[-1] - times[0]), 2))


def detect_beats(path: Any, *, max_seconds: float = MAX_SECONDS) -> Beats:
    """Beat times in ``path``, with the tempo on ``.tempo``. Empty when it has no pulse."""
    return beats_from_samples(decode_mono(path, rate=RATE, seconds=max_seconds))


def output_beats(beats: Iterable[float], *, start: float = 0.0, length: float = 0.0, duration: float) -> list[float]:
    """Beats on the output clock for a bed that starts at ``start`` and loops at ``length``. Pure."""
    times = sorted(beats)
    placed: list[float] = []
    offset = -start
    while times and offset <= duration:
        placed.extend(time + offset for time in times if 0 <= time + offset <= duration)
        if length <= 0:
            break
        offset += length
    return sorted(placed)


def _speech_near(speech: Iterable[dict[str, Any]], low: float, high: float) -> bool:
    return any(
        float(band.get("start", 0)) < high + SPEECH_GUARD and float(band.get("end", 0)) > low - SPEECH_GUARD
        for band in speech
    )


def snap_to_music(
    segments: list[dict[str, Any]],
    edit: dict[str, Any],
    analysis: dict[str, Any] | None,
    assets: dict[str, Any] | None,
    *,
    source_duration: float,
) -> list[dict[str, Any]]:
    """``segments`` with their silent cuts on the beat, when ``edit.music.beat_sync`` asks. Pure.

    Runs where a plan is validated, so preview, captions, and render share the
    same cuts. Cuts already on a beat stay put, so validating twice changes nothing.
    """
    music = edit.get("music") or {}
    asset = music.get("asset")
    record = (assets or {}).get(asset) if music.get("beat_sync") and isinstance(asset, str) else None
    if not isinstance(record, dict) or not record.get("beats"):
        return segments
    plan = {"segments": segments, "edit": edit, "analysis": analysis}
    length = float(record.get("duration") or 0.0)
    grid = output_beats(
        record["beats"],
        start=float(music.get("start", 0.0)),
        length=length,
        duration=plan_timeline(plan)["duration"] + SNAP_TOLERANCE,
    )
    return snap_cuts(plan, grid, source_duration=source_duration)[0]["segments"]


def snap_cuts(
    plan: dict[str, Any],
    beats: Iterable[float],
    *,
    source_duration: float | None = None,
    tolerance: float = SNAP_TOLERANCE,
) -> tuple[dict[str, Any], list[dict[str, float]]]:
    """Move each cut with no speech at it onto the nearest beat. Pure.

    ``beats`` are on the output clock. Only the end of the outgoing moment moves,
    by at most ``tolerance`` seconds of finished video, so a nudge can never clip
    a word: a cut with speech at either side stays where the story put it. Nudging
    one cut slides everything after it, so the clock is rebuilt for each one.
    Returns the new plan and the moves made.
    """
    grid = sorted(beats)
    out = copy.deepcopy(plan)
    moved: list[dict[str, float]] = []
    segments = out.get("segments") or []
    if not grid or len(segments) < 2:
        return out, moved
    analysis = out.get("analysis")
    bands = analysis.get("speech") if isinstance(analysis, dict) else None
    speech = [band for band in bands or [] if isinstance(band, dict)]
    for index in range(len(segments) - 1):
        view = plan_timeline(out)
        ends = [span.output_end for span in view["spans"] if span.segment == index + 1]
        starts = [span.output_start for span in view["spans"] if span.segment == index + 2]
        if not ends or not starts:
            continue
        # A transition reads as a cut at its midpoint.
        cut = (ends[-1] + starts[0]) / 2
        beat = min(grid, key=lambda time: abs(time - cut))
        shift = beat - cut
        if abs(shift) > tolerance or abs(shift) < 0.005:
            continue
        current, following = segments[index], segments[index + 1]
        speed = segment_speed(current)
        new_end = round(float(current["end"]) + shift * speed, 3)
        low, high = min(float(current["end"]), new_end), max(float(current["end"]), new_end)
        if _speech_near(speech, low, high) or _speech_near(
            speech, float(following["start"]), float(following["start"])
        ):
            continue
        if new_end - float(current["start"]) < MIN_MOMENT * speed:
            continue
        if source_duration is not None and new_end > source_duration:
            continue
        # Growing into footage another moment already plays would show it twice.
        if shift > 0 and any(
            other is not current and float(other["start"]) < new_end and float(other["end"]) > float(current["end"])
            for other in segments
        ):
            continue
        moved.append({"segment": index + 1, "from": float(current["end"]), "to": new_end, "beat": round(beat, 3)})
        current["end"] = new_end
    return out, moved
