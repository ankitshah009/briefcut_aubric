"""Deterministic local story planning: pure selection logic plus one real clip."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from vedit.analyze import Range
from vedit.campaign import validate_plan
from vedit.story import (
    KINDS,
    MAX_SEGMENTS,
    Moment,
    build_plan,
    keywords,
    merge_pieces,
    plan_local_story,
    score_candidates,
    select_moments,
    split_at_scenes,
    speech_ranges,
)

GOAL = "Open with the finished bowl, then show the oak being turned and sanded, and end with the oil finish."


def _silences(duration: float, speech: list[tuple[float, float]]) -> list[Range]:
    """Silence is everything between the given speech ranges."""
    out, cursor = [], 0.0
    for start, end in speech:
        if start > cursor:
            out.append(Range(cursor, start))
        cursor = end
    if duration > cursor:
        out.append(Range(cursor, duration))
    return out


def test_keywords_drop_stopwords_and_keep_order() -> None:
    assert keywords(GOAL)[:4] == ["open", "finished", "bowl", "oak"]
    assert "the" not in keywords(GOAL) and "with" not in keywords(GOAL)


def test_speech_ranges_and_scene_split_and_merge() -> None:
    silences = _silences(40, [(1, 9), (12, 13), (13.2, 14), (20, 36)])
    speech = speech_ranges(40, silences)
    # The 0.2 s gap at 13.0–13.2 is absorbed by speech padding: one range, not two.
    assert [round(r.start, 1) for r in speech] == [0.9, 11.9, 19.9]
    pieces = split_at_scenes(speech, [5.0, 8.95, 24.0, 30.0])
    starts = [round(p.start, 1) for p in pieces]
    assert 5.0 in starts and 24.0 in starts and 30.0 in starts
    assert 8.95 not in [round(p.start, 2) for p in pieces]  # too close to the range edge to split
    merged = merge_pieces(pieces)
    assert all(p.duration <= 12.0 + 1e-9 for p in merged)
    assert all(p.duration >= 0.5 for p in merged)
    # The two tiny speech bursts around 12–14 s merge into one playable piece.
    assert any(abs(p.start - 11.9) < 0.2 and p.end >= 14.0 for p in merged)


def test_scoring_cites_evidence_and_marks_hook_and_closing() -> None:
    pieces = [Range(1, 4), Range(10, 14), Range(30, 34)]
    speech = [Range(1, 4), Range(10, 14), Range(30, 34)]
    transcript = [{"start": 1, "end": 3.5, "text": "Here is the finished bowl"}, {"start": 30, "end": 33, "text": "a coat of oil finish"}]
    moments = score_candidates(pieces, duration=40, scenes=[9.9], speech=speech, transcript=transcript, goal_keywords=keywords(GOAL))
    assert [m.kind for m in moments] == ["hook", "speech", "closing"]
    assert moments[0].words == ["bowl", "finished"]
    assert any("scene change at 9.9s" in e for e in moments[1].evidence)
    assert all(0 <= m.score <= 1 for m in moments)
    assert moments[0].score > moments[1].score


def test_selection_is_non_overlapping_fits_target_and_is_chronological() -> None:
    moments = [Moment(0, 5, 0.9, "hook"), Moment(4, 9, 0.95, "speech"), Moment(20, 26, 0.6, "speech"), Moment(30, 33, 0.7, "closing")]
    chosen = select_moments(moments, 10)
    assert [(m.start, m.end) for m in chosen] == [(4, 9), (30, 33)]
    total = sum(m.duration for m in chosen)
    assert total <= 10.05
    # Nothing fits: the best moment is trimmed to the target instead of returning nothing.
    only_long = select_moments([Moment(2, 30, 0.8, "speech")], 6)
    assert [(m.start, m.end) for m in only_long] == [(2, 8)]
    assert len(select_moments([Moment(i * 2, i * 2 + 1, 0.5, "speech") for i in range(20)], 100)) == MAX_SEGMENTS


def test_build_plan_shape_contract_and_bounds() -> None:
    duration = 40.0
    speech = [(0.5, 3.7), (8.6, 11.4), (16.5, 19.3), (24.5, 28.2), (32.5, 35.1), (36.1, 39.2)]
    transcript = [{"start": 0.5, "end": 3.7, "text": "This is the finished bowl."}, {"start": 32.5, "end": 35.1, "text": "Now the oil finish."}]
    plan = build_plan(goal=GOAL, duration=duration, target_duration=20, scenes=[8.0, 16.0, 24.0, 32.0], silences=_silences(duration, speech), transcript=transcript, has_audio=True)
    assert plan["provider"] == "local" and plan["usage"] == {"estimated_cost_usd": 0}
    assert plan["analysis"]["method"] == "local-analysis"
    assert plan["analysis"]["scenes"] == [8.0, 16.0, 24.0, 32.0]
    assert len(plan["analysis"]["speech"]) == 6 and len(plan["analysis"]["transcript"]) == 2
    assert "Not AI understanding" in plan["analysis"]["notes"][0]
    assert "does not choose a story with AI" in plan["summary"]
    segments = plan["segments"]
    assert 1 <= len(segments) <= MAX_SEGMENTS
    assert sum(s["end"] - s["start"] for s in segments) <= 20.05
    assert all(s["kind"] in KINDS and 0 <= s["score"] <= 1 for s in segments)
    assert all(a["end"] <= b["start"] for a, b in zip(segments, segments[1:]))
    assert segments[0]["kind"] == "hook" and "bowl" in segments[0]["label"]
    assert segments[-1]["kind"] == "closing"
    assert plan["title"].startswith("Open with the finished bowl")
    assert plan["hashtags"][:2] == ["#open", "#finished"]
    assert "“This is the finished bowl.”" in plan["caption"]
    # The campaign validator accepts and preserves the contract fields.
    checked = validate_plan(plan, duration, original=plan)
    assert checked["analysis"]["scenes"] == [8.0, 16.0, 24.0, 32.0]
    assert checked["segments"][0]["kind"] == "hook" and "score" in checked["segments"][0]


def test_build_plan_fallbacks_are_explicit() -> None:
    whole = build_plan(goal="Show it", duration=1.5, target_duration=30, scenes=[], silences=[], transcript=[], has_audio=True)
    assert whole["segments"] == [{"start": 0.0, "end": 1.5, "label": "Opening cut", "reason": "The whole clip fits the requested length.", "score": 1.0, "kind": "opening"}]
    scenes_only = build_plan(goal="Show it", duration=30, target_duration=8, scenes=[10, 20], silences=[], transcript=[], has_audio=False)
    assert any("no audio" in w for w in scenes_only["warnings"])
    assert all(s["kind"] == "scene" for s in scenes_only["segments"])
    assert sum(s["end"] - s["start"] for s in scenes_only["segments"]) <= 8.05
    silent = build_plan(goal="Show it", duration=30, target_duration=8, scenes=[], silences=[Range(0, 30)], transcript=[], has_audio=True)
    assert silent["segments"][0]["kind"] == "opening" and silent["segments"][0]["start"] == 0
    assert any("opening of the clip" in w for w in silent["warnings"])
    # Oversized analysis is bounded, not rejected.
    big = build_plan(goal="Show it", duration=1000, target_duration=10, scenes=[float(i) for i in range(1, 999)], silences=[], transcript=[{"start": i, "end": i + 0.5, "text": "x" * 300} for i in range(900)], has_audio=True)
    assert len(big["analysis"]["scenes"]) <= 400 and len(big["analysis"]["transcript"]) <= 600
    assert sum(len(c["text"]) for c in big["analysis"]["transcript"]) <= 60_000


@pytest.mark.render
def test_plan_local_story_on_demo_clip(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("story_test_media_helpers", Path(__file__).with_name("test_e2e.py"))
    assert spec and spec.loader
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)
    if not helpers._have_ffmpeg():
        pytest.skip("ffmpeg required")
    clip = Path(__file__).resolve().parents[1] / "examples" / "demo" / "talk.mp4"
    if not clip.is_file():
        pytest.skip("examples/demo/talk.mp4 missing; run scripts/make_demo.py")
    plan = plan_local_story(clip, goal="Keep the clearest idea", target_duration=2, work_dir=tmp_path, transcribe=False)
    assert plan["provider"] == "local"
    assert plan["analysis"]["duration"] == pytest.approx(3.0, abs=0.1)
    assert 0.5 <= plan["segments"][0]["end"] - plan["segments"][0]["start"] <= 2.05
    assert sum(s["end"] - s["start"] for s in plan["segments"]) <= 2.05
    assert (tmp_path / "story-analysis.json").is_file()
