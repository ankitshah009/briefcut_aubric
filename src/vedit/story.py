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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vedit.analyze import Range, detect_silence, keep_ranges
from vedit.errors import VeditError
from vedit.media import probe, run_ffmpeg

METHOD = "local-analysis"
NOTE = "Local analysis: scene cuts + speech energy + transcript keywords. Not AI understanding."
KINDS = ("hook", "speech", "scene", "opening", "closing")

MAX_SEGMENTS = 8
MIN_SEGMENT = 0.5
MIN_PIECE = 1.5
MAX_PIECE = 12.0
PAD = 0.15
MAX_SCENES = 400
MAX_SPEECH = 400
MAX_CUES = 600
MAX_TRANSCRIPT_BYTES = 60_000
TRANSCRIBE_MAX_SECONDS = 600.0

_SHOWINFO = re.compile(r"pts_time:([0-9.]+)")
_WORD = re.compile(r"[a-z0-9][a-z0-9'\-]{1,}")
_HOOK_HINT = re.compile(r"\b(open with|opening|start with|hook|begin with|first show)\b", re.I)
_STOPWORDS = frozenset(
    "a an and are as at be been but by can could do does for from had has have how i if in into is it its just "
    "make making me more most my no not of on one only or our out over so some than that the their them then there "
    "these they this those to too up us very was we were what when where which while who will with would you your "
    "video clip story show shows showing keep tell about also like get got into onto want wants please made".split()
)


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

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def tokens(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in _STOPWORDS and len(w) >= 3]


def keywords(goal: str) -> list[str]:
    seen: list[str] = []
    for word in tokens(goal):
        if word not in seen:
            seen.append(word)
    return seen


def _title_from_goal(goal: str, limit: int = 60) -> str:
    first = re.split(r"(?<=[.!?])\s+", goal.strip(), maxsplit=1)[0].strip().rstrip(".!?,;: ")
    if not first:
        first = "Your story"
    if len(first) > limit:
        cut = first[:limit].rsplit(" ", 1)[0].rstrip(",;:- ")
        first = (cut or first[:limit]).rstrip() + "…"
    return first[0].upper() + first[1:]


def _hashtags(goal: str, limit: int = 5) -> list[str]:
    tags: list[str] = []
    for word in keywords(goal):
        clean = re.sub(r"[^a-z0-9]", "", word)
        if len(clean) >= 4 and clean not in tags:
            tags.append(clean)
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


def score_candidates(
    pieces: list[Range],
    *,
    duration: float,
    scenes: list[float],
    speech: list[Range],
    transcript: list[dict[str, Any]],
    goal_keywords: list[str],
) -> list[Moment]:
    """Attach a bounded score and evidence to each candidate piece."""
    if not pieces:
        return []
    moments: list[Moment] = []
    keyword_set = set(goal_keywords)
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
        moments.append(Moment(piece.start, piece.end, round(min(1.0, max(0.0, score)), 3), kind, evidence, matched))
    return moments


def select_moments(moments: list[Moment], target: float, *, max_segments: int = MAX_SEGMENTS) -> list[Moment]:
    """Greedy, non-overlapping, fits the target; returns chronological order."""
    if not moments or target <= 0:
        return []
    chosen: list[Moment] = []
    total = 0.0
    for moment in sorted(moments, key=lambda m: (-m.score, m.start)):
        if len(chosen) >= max_segments:
            break
        if any(_overlap(moment.start, moment.end, c.start, c.end) > 0 for c in chosen):
            continue
        remaining = target - total
        if remaining < MIN_SEGMENT:
            break
        if moment.duration <= remaining + 0.05:
            chosen.append(moment)
            total += moment.duration
        elif remaining >= 1.0 and not chosen:
            trimmed = Moment(moment.start, moment.start + remaining, moment.score, moment.kind, moment.evidence + [f"trimmed to the {remaining:.1f}s target"], moment.words)
            chosen.append(trimmed)
            total += trimmed.duration
    if not chosen:
        best = max(moments, key=lambda m: (m.score, -m.start))
        end = min(best.end, best.start + target)
        chosen = [Moment(best.start, end, best.score, best.kind, best.evidence + [f"trimmed to the {target:.1f}s target"], best.words)]
    return sorted(chosen, key=lambda m: m.start)


def _pad_and_order(chosen: list[Moment], duration: float, *, target: float, hook_first: bool) -> list[Moment]:
    """Add breathing room around cuts without exceeding the target length."""
    ordered = sorted(chosen, key=lambda m: m.start)
    budget = max(0.0, target - sum(m.duration for m in ordered))
    padded: list[Moment] = []
    for index, moment in enumerate(ordered):
        prev_end = padded[-1].end if padded else 0.0
        next_start = ordered[index + 1].start if index + 1 < len(ordered) else duration
        lead = min(PAD, budget / 2, moment.start - max(prev_end, 0.0))
        tail = min(PAD, budget - max(0.0, lead), max(0.0, min(next_start, duration) - moment.end))
        lead, tail = max(0.0, lead), max(0.0, tail)
        budget -= lead + tail
        padded.append(Moment(round(moment.start - lead, 3), round(moment.end + tail, 3), moment.score, moment.kind, moment.evidence, moment.words))
    if hook_first:
        hooks = [m for m in padded if m.kind == "hook"]
        if hooks:
            padded = hooks + [m for m in padded if m is not hooks[0]]
    return padded


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
) -> dict[str, Any]:
    """Plan from measured signals. Pure and deterministic; safe to unit test."""
    if not math.isfinite(duration) or duration <= 0:
        raise StoryError("The source has no measurable duration.")
    warnings = list(warnings or [])
    scenes = sorted({round(float(s), 3) for s in scenes if 0 < float(s) < duration})[:MAX_SCENES]
    speech = speech_ranges(duration, silences) if has_audio else []
    speech = speech[:MAX_SPEECH]
    transcript = _bound_transcript(transcript, duration)
    target = min(float(target_duration), duration)
    goal_keys = keywords(goal)
    hook_first = bool(_HOOK_HINT.search(goal))

    if duration <= target_duration + 0.05:
        chosen = [Moment(0.0, round(duration, 3), 1.0, "opening", ["The whole clip fits the requested length"])]
    else:
        if speech:
            pieces = merge_pieces(split_at_scenes(speech, scenes))
        elif scenes:
            edges = [0.0, *scenes, duration]
            pieces = merge_pieces([Range(a, b) for a, b in zip(edges, edges[1:]) if b - a >= MIN_SEGMENT])
            warnings.append("No speech was detected, so moments follow scene changes only." if has_audio else "This source has no audio, so moments follow scene changes only.")
        else:
            pieces = []
            warnings.append("No scene changes or speech were detected; the plan uses the opening of the clip. Adjust the moments by hand.")
        moments = score_candidates(pieces, duration=duration, scenes=scenes, speech=speech, transcript=transcript, goal_keywords=goal_keys)
        if not moments:
            moments = [Moment(0.0, min(duration, max(target, MIN_SEGMENT)), 0.2, "opening", ["Opening of the clip"])]
        chosen = _pad_and_order(select_moments(moments, target), duration, target=target, hook_first=hook_first)

    segments = []
    for index, moment in enumerate(chosen, 1):
        label = {"hook": "Opening hook", "closing": "Closing moment", "scene": "Visual moment", "opening": "Opening cut"}.get(moment.kind, "Spoken moment")
        if moment.words:
            label = f"{label}: {', '.join(moment.words[:2])}"
        segments.append({
            "start": round(moment.start, 3), "end": round(moment.end, 3),
            "label": label[:100], "reason": ("; ".join(moment.evidence) + ".")[:600],
            "score": round(float(moment.score), 3), "kind": moment.kind if moment.kind in KINDS else "speech",
        })
    total = round(sum(s["end"] - s["start"] for s in segments), 3)
    transcript_note = f" and {len(transcript)} transcript cues" if transcript else ""
    summary = (
        f"Local analysis found {len(scenes)} scene cuts and {len(speech)} speech ranges{transcript_note}. "
        f"Selected {len(segments)} moment{'s' if len(segments) != 1 else ''} totalling {total:g}s of {duration:.1f}s. "
        "This is deterministic signal analysis; local mode does not choose a story with AI. Review every moment before rendering."
    )
    caption = goal.strip()
    if transcript:
        first = str(transcript[0].get("text", "")).strip()
        if first:
            caption = f"{caption}\n\n“{first[:200]}”"
    return {
        "title": _title_from_goal(goal),
        "summary": summary[:1500],
        "segments": segments,
        "caption": caption[:2200],
        "hashtags": _hashtags(goal),
        "cover_prompt": "",
        "provider": "local",
        "usage": {"estimated_cost_usd": 0},
        "analysis": {
            "method": METHOD,
            "scenes": scenes,
            "speech": [{"start": round(s.start, 3), "end": round(s.end, 3)} for s in speech],
            "transcript": transcript,
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
# Measurement (ffmpeg, optional local Whisper)
# ---------------------------------------------------------------------------


def detect_scenes_fast(source: Path, *, threshold: float = 0.3, width: int = 320) -> list[float]:
    """Scene cuts measured on a downscaled picture: much faster, same cut times."""
    result = run_ffmpeg(["-i", str(source), "-an", "-vf", f"scale={width}:-2,select='gt(scene,{threshold})',showinfo", "-f", "null", "-"])
    out: list[float] = []
    for raw in _SHOWINFO.findall(result.stderr):
        value = float(raw)
        if not out or value - out[-1] > 0.08:
            out.append(value)
    return out


def analyze_source(
    source: Path,
    work_dir: Path,
    *,
    language: str = "en",
    transcribe: bool = True,
    time_budget_s: float = 20.0,
) -> dict[str, Any]:
    """Measure scenes, silence and (optionally) a local transcript."""
    info = probe(source)
    if not info.has_video or not math.isfinite(info.duration) or info.duration <= 0:
        raise StoryError("Choose a playable video with a measurable duration.")
    warnings: list[str] = []
    work_dir.mkdir(parents=True, exist_ok=True)
    scenes = detect_scenes_fast(source)
    silences = detect_silence(source, threshold_db=-35.0, min_silence=0.35) if info.has_audio else []
    transcript: list[dict[str, Any]] = []
    if transcribe and info.has_audio:
        from vedit.agent.explain import local_transcriber_ready

        if not local_transcriber_ready():
            warnings.append("No local speech model: moments are chosen from scene cuts and speech energy without transcript keywords.")
        elif info.duration > min(TRANSCRIBE_MAX_SECONDS, time_budget_s * 15):
            warnings.append("The source is long, so the transcript was skipped to keep planning fast; moments use scene cuts and speech energy.")
        else:
            try:
                from vedit.captions import transcribe as run_transcribe

                cues = run_transcribe(source, work_dir, language=language)
                transcript = [{"start": c.start, "end": c.end, "text": c.text} for c in cues]
            except VeditError as exc:
                warnings.append(f"Transcript unavailable ({str(exc).splitlines()[0][:120]}); moments use scene cuts and speech energy.")
    return {"duration": info.duration, "has_audio": info.has_audio, "scenes": scenes, "silences": silences, "transcript": transcript, "warnings": warnings}


def analyze_signals(
    source: Path,
    *,
    work_dir: Path,
    language: str = "en",
    transcribe: bool = True,
    time_budget_s: float = 20.0,
) -> dict[str, Any]:
    """Measured, display-ready signals in the plan ``analysis`` shape.

    Shared by local planning and by Meta-mode plans, so the Studio scrubber and
    transcript panel behave the same whichever planner chose the moments. The
    optional transcript is local speech recognition; nothing here calls Meta.
    """
    measured = analyze_source(Path(source), Path(work_dir), language=language, transcribe=transcribe, time_budget_s=time_budget_s)
    duration = float(measured["duration"])
    scenes = sorted({round(float(s), 3) for s in measured["scenes"] if 0 < float(s) < duration})[:MAX_SCENES]
    speech = (speech_ranges(duration, measured["silences"]) if measured["has_audio"] else [])[:MAX_SPEECH]
    return {
        "method": METHOD,
        "scenes": scenes,
        "speech": [{"start": round(s.start, 3), "end": round(s.end, 3)} for s in speech],
        "transcript": _bound_transcript(measured["transcript"], duration),
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
    transcribe: bool = True,
    time_budget_s: float = 20.0,
) -> dict[str, Any]:
    """Measure the source and return an editable, evidence-cited local plan."""
    source = Path(source)
    work_dir = Path(work_dir)
    measured = analyze_source(source, work_dir, language=language, transcribe=transcribe, time_budget_s=time_budget_s)
    plan = build_plan(
        goal=goal, duration=measured["duration"], target_duration=target_duration,
        scenes=measured["scenes"], silences=measured["silences"], transcript=measured["transcript"],
        has_audio=measured["has_audio"], warnings=measured["warnings"],
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
    for warning in plan.get("warnings", []):
        lines.append(f"warning: {warning}")
    return "\n".join(lines) + "\n"
