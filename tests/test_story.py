"""Deterministic local story planning: pure selection logic plus one real clip."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from vedit.analyze import Range
from vedit.campaign import validate_plan
from vedit.story import (
    KINDS,
    MAX_SEGMENTS,
    Moment,
    StoryError,
    build_plan,
    format_plan,
    keywords,
    merge_pieces,
    pick_scene_cuts,
    plan_local_story,
    score_candidates,
    select_moments,
    sentences_from_transcript,
    snap_to_sentences,
    speech_ranges,
    split_at_scenes,
    temporal_spans,
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
    transcript = [
        {"start": 1, "end": 3.5, "text": "Here is the finished bowl"},
        {"start": 30, "end": 33, "text": "a coat of oil finish"},
    ]
    moments = score_candidates(
        pieces, duration=40, scenes=[9.9], speech=speech, transcript=transcript, goal_keywords=keywords(GOAL)
    )
    assert [m.kind for m in moments] == ["hook", "speech", "closing"]
    assert moments[0].words == ["bowl", "finished"]
    assert any("scene change at 9.9s" in e for e in moments[1].evidence)
    assert all(0 <= m.score <= 1 for m in moments)
    assert moments[0].score > moments[1].score


def test_selection_is_non_overlapping_fits_target_and_is_chronological() -> None:
    moments = [
        Moment(0, 5, 0.9, "hook"),
        Moment(4, 9, 0.95, "speech"),
        Moment(20, 26, 0.6, "speech"),
        Moment(30, 33, 0.7, "closing"),
    ]
    chosen = select_moments(moments, 10)
    assert [(m.start, m.end) for m in chosen] == [(4, 9), (30, 33)]
    total = sum(m.duration for m in chosen)
    assert total <= 10.05
    # Nothing fits: the best moment is trimmed to the target instead of returning nothing.
    only_long = select_moments([Moment(2, 30, 0.8, "speech")], 6)
    assert [(m.start, m.end) for m in only_long] == [(2, 8)]
    assert len(select_moments([Moment(i * 2, i * 2 + 1, 0.5, "speech") for i in range(20)], 100)) == MAX_SEGMENTS


def test_build_plan_rejects_target_outside_contract_and_format_states_selection() -> None:
    """Bad targets raise from the shipped planner; a valid plan prints its real total."""
    duration = 40.0
    speech = [(0.5, 3.7), (8.6, 11.4), (16.5, 19.3)]
    kwargs = {
        "goal": GOAL,
        "duration": duration,
        "scenes": [8.0, 16.0],
        "silences": _silences(duration, speech),
        "transcript": [],
        "has_audio": True,
    }
    for bad in (0, 0.49, 180.01, float("nan"), float("inf"), -3, True, "20"):
        with pytest.raises(StoryError, match="between 0.5 and 180"):
            build_plan(target_duration=bad, **kwargs)
    plan = build_plan(target_duration=12, **kwargs)
    total = sum(segment["end"] - segment["start"] for segment in plan["segments"])
    assert 0 < total <= 12.05
    text = format_plan(plan)
    assert f"selected: {total:.1f}s of {duration:.1f}s across {len(plan['segments'])} moments" in text


def test_build_plan_shape_contract_and_bounds() -> None:
    duration = 40.0
    speech = [(0.5, 3.7), (8.6, 11.4), (16.5, 19.3), (24.5, 28.2), (32.5, 35.1), (36.1, 39.2)]
    transcript = [
        {"start": 0.5, "end": 3.7, "text": "This is the finished bowl."},
        {"start": 32.5, "end": 35.1, "text": "Now the oil finish."},
    ]
    plan = build_plan(
        goal=GOAL,
        duration=duration,
        target_duration=20,
        scenes=[8.0, 16.0, 24.0, 32.0],
        silences=_silences(duration, speech),
        transcript=transcript,
        has_audio=True,
    )
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
    assert plan["title"] == "The finished bowl"
    assert plan["hashtags"][:2] == ["#finished", "#bowl"]
    assert plan["caption"] == "The finished bowl. This is the finished bowl. Now the oil finish."
    # The campaign validator accepts and preserves the contract fields.
    checked = validate_plan(plan, duration, original=plan)
    assert checked["analysis"]["scenes"] == [8.0, 16.0, 24.0, 32.0]
    assert checked["segments"][0]["kind"] == "hook" and "score" in checked["segments"][0]


def test_build_plan_fallbacks_are_explicit() -> None:
    whole = build_plan(
        goal="Show it", duration=1.5, target_duration=30, scenes=[], silences=[], transcript=[], has_audio=True
    )
    assert whole["segments"] == [
        {
            "start": 0.0,
            "end": 1.5,
            "label": "Opening cut",
            "reason": "The whole clip fits the requested length.",
            "score": 1.0,
            "kind": "opening",
        }
    ]
    scenes_only = build_plan(
        goal="Show it", duration=30, target_duration=8, scenes=[10, 20], silences=[], transcript=[], has_audio=False
    )
    assert any("no audio" in w for w in scenes_only["warnings"])
    assert all(s["kind"] == "scene" for s in scenes_only["segments"])
    assert sum(s["end"] - s["start"] for s in scenes_only["segments"]) <= 8.05
    silent = build_plan(
        goal="Show it",
        duration=30,
        target_duration=8,
        scenes=[],
        silences=[Range(0, 30)],
        transcript=[],
        has_audio=True,
    )
    assert silent["segments"][0]["kind"] == "opening" and silent["segments"][0]["start"] == 0
    assert any("opening of the clip" in w for w in silent["warnings"])
    # Oversized analysis is bounded, not rejected.
    from vedit.story import MAX_CUES, MAX_SCENES, MAX_TRANSCRIPT_BYTES

    big = build_plan(
        goal="Show it",
        duration=3000,
        target_duration=10,
        scenes=[float(i) for i in range(1, 2999)],
        silences=[],
        transcript=[{"start": i / 3, "end": i / 3 + 0.2, "text": "x" * 300} for i in range(MAX_CUES + 500)],
        has_audio=True,
    )
    assert len(big["analysis"]["scenes"]) == MAX_SCENES and len(big["analysis"]["transcript"]) <= MAX_CUES
    assert sum(len(c["text"]) for c in big["analysis"]["transcript"]) <= MAX_TRANSCRIPT_BYTES


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


def test_measurement_takes_a_meta_transcript_and_runs_no_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from vedit.captions import Cue
    from vedit.story import NO_TRANSCRIPT, analyze_signals, analyze_source

    monkeypatch.setattr(
        "vedit.story.probe",
        lambda _source: SimpleNamespace(has_video=True, duration=301.0, has_audio=True, audio_map=0),
    )
    monkeypatch.setattr("vedit.story.detect_scenes_fast", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("vedit.story.detect_silence", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("vedit.story.audible", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("vedit.captions.transcribe", lambda *_args, **_kwargs: pytest.fail("measurement never listens"))
    monkeypatch.setattr(
        "vedit.meta.transcribe_audio", lambda *_args, **_kwargs: pytest.fail("measurement never listens")
    )
    nobody = analyze_source(Path("long.mp4"), tmp_path)
    assert nobody["transcribed"] is False and nobody["transcript"] == []
    measured = analyze_source(Path("long.mp4"), tmp_path, heard=[Cue(1.0, 2.0, "Long source speech.")])
    assert measured["transcribed"] is True
    assert measured["transcript"] == [{"start": 1.0, "end": 2.0, "text": "Long source speech."}]
    signals = analyze_signals(
        Path("long.mp4"),
        work_dir=tmp_path,
        heard=[Cue(1.0, 2.0, "Long source speech.")],
        transcribed_by="meta-voice-transcribe",
    )
    assert (
        signals["transcribed_by"] == "meta-voice-transcribe"
        and signals["sentences"][0]["text"] == "Long source speech."
    )
    assert "transcribed_by" not in analyze_signals(
        Path("long.mp4"), work_dir=tmp_path, transcribed_by="meta-voice-transcribe"
    ), "nobody listened, so nobody is named"
    asked = plan_local_story(Path("long.mp4"), goal="Show it", target_duration=8, work_dir=tmp_path, transcribe=True)
    assert NO_TRANSCRIPT in asked["warnings"], "a local plan says it has no transcript instead of running a model"


# ---------------------------------------------------------------------------
# Sentences and sentence-aligned cuts
# ---------------------------------------------------------------------------

WORKSHOP_CUES = [
    {"start": 0.0, "end": 2.69, "text": "This is the finished oak bowl, oiled and"},
    {"start": 2.69, "end": 5.0, "text": "ready for the table."},
    {"start": 5.0, "end": 7.44, "text": "It started as a rough blank from a fallen"},
    {"start": 7.44, "end": 8.0, "text": "tree."},
    {"start": 8.0, "end": 11.2, "text": "Here is the raw oak blank, still wet and"},
    {"start": 11.2, "end": 12.0, "text": "heavy."},
    {"start": 12.0, "end": 15.52, "text": "The grain tells me where the bowl wants"},
    {"start": 15.52, "end": 16.0, "text": "to be."},
    {"start": 16.0, "end": 18.83, "text": "On the lathe the shape appears in long,"},
    {"start": 18.83, "end": 20.0, "text": "steady cuts."},
    {"start": 20.0, "end": 23.1, "text": "I keep the wall thickness even from rim"},
    {"start": 23.1, "end": 24.0, "text": "to base."},
    {"start": 24.0, "end": 26.42, "text": "Sanding goes from coarse to fine, with"},
    {"start": 26.42, "end": 29.0, "text": "the lathe turning slowly."},
    {"start": 29.0, "end": 32.0, "text": "This is where the grain starts to glow."},
    {"start": 32.0, "end": 35.13, "text": "A coat of food safe oil brings out the"},
    {"start": 35.13, "end": 36.0, "text": "color."},
    {"start": 36.0, "end": 40.0, "text": "24 hours later, the bowl is ready to use."},
]
WORKSHOP_GOAL = (
    "Make a 20-second vertical story about the oak bowl: open with the finished bowl, then show how it is made."
)


def test_sentences_group_cues_until_terminal_punctuation() -> None:
    sentences = sentences_from_transcript(WORKSHOP_CUES, duration=40.0)
    assert [(s["start"], s["end"]) for s in sentences][:3] == [(0.0, 5.0), (5.0, 8.0), (8.0, 12.0)]
    assert sentences[0]["text"] == "This is the finished oak bowl, oiled and ready for the table."
    assert len(sentences) == 10 and sentences[-1] == {
        "start": 36.0,
        "end": 40.0,
        "text": "24 hours later, the bowl is ready to use.",
    }
    # Closing quotes and ellipses still terminate; a pause closes an unpunctuated run; nothing crosses the duration.
    quoted = sentences_from_transcript(
        [
            {"start": 0, "end": 1, "text": "She said “go.”"},
            {"start": 1, "end": 2, "text": "Then…"},
            {"start": 2, "end": 3, "text": "no end"},
        ],
        duration=2.5,
    )
    assert [s["text"] for s in quoted] == ["She said “go.”", "Then…", "no end"] and quoted[-1]["end"] == 2.5
    # Measured speech trims the run-on cue ends: sentence 1 ends 0.15 s after its last word, not where sentence 2 starts.
    speech = [Range(0.433, 3.823), Range(4.635, 7.399), Range(8.456, 11.484), {"start": 12.313, "end": 14.884}]
    heard = sentences_from_transcript(WORKSHOP_CUES, duration=40.0, speech=speech)
    assert (heard[0]["start"], heard[0]["end"]) == (0.283, 3.973)
    assert (heard[1]["start"], heard[1]["end"]) == (4.5, 7.549), (
        "the next sentence starts near its own speech (bounded to 0.5 s before its cue), not at the late cue boundary"
    )
    assert (heard[2]["start"], heard[2]["end"]) == (8.306, 11.634) and heard[0]["text"] == sentences[0]["text"]
    assert [(s["start"], s["end"]) for s in heard[4:]] == [(s["start"], s["end"]) for s in sentences[4:]], (
        "sentences without measured speech keep cue times"
    )
    # One continuous speech range (no silence found) says nothing about where sentences end: cue times stand,
    # except that the one sentence owning the range (its midpoint, 20 s) is kept 0.1 s clear of its successor.
    continuous = sentences_from_transcript(WORKSHOP_CUES, duration=40.0, speech=[Range(0.0, 40.0)])
    expected = [(s["start"], s["end"]) for s in sentences]
    expected[5] = (20.0, 23.9)
    assert [(s["start"], s["end"]) for s in continuous] == expected
    paused = sentences_from_transcript(
        [
            {"start": 0, "end": 1, "text": "first part"},
            {"start": 1.2, "end": 2, "text": "same breath"},
            {"start": 3.5, "end": 4, "text": "after a pause"},
        ]
    )
    assert [(s["start"], s["end"], s["text"]) for s in paused] == [
        (0.0, 2.0, "first part same breath"),
        (3.5, 4.0, "after a pause"),
    ]
    assert (
        sentences_from_transcript([]) == []
        and sentences_from_transcript([{"start": "x", "end": 1, "text": "bad"}, {"text": ""}]) == []
    )


def test_snap_finishes_the_sentence_or_backs_off_within_tolerance() -> None:
    sentences = sentences_from_transcript(WORKSHOP_CUES, duration=40.0)
    meta_plan = [
        {"start": 0.0, "end": 2.7, "label": "Finished bowl", "reason": "Rests on speech [0.0–2.7]."},
        {"start": 8.0, "end": 11.2, "label": "Raw oak", "reason": "Rests on speech [8.0–11.2]."},
        {"start": 16.0, "end": 20.0, "label": "Turning", "reason": "Whole sentence."},
        {"start": 32.0, "end": 35.1, "label": "Oil finish", "reason": "Rests on speech [32.0–35.1]."},
    ]
    snapped = snap_to_sentences(meta_plan, sentences, duration=40.0)
    assert [(s["start"], s["end"]) for s in snapped] == [(0.0, 5.0), (8.0, 12.0), (16.0, 20.0), (32.0, 36.0)]
    assert snapped[0]["reason"] == "Rests on speech [0.0–2.7]; end moved to sentence end at 5.0 s."
    assert snapped[2]["reason"] == "Whole sentence." and "start" not in snapped[2]["reason"]
    assert meta_plan[0]["end"] == 2.7, "input is not mutated"
    # Too far from the sentence end to extend: back off to the sentence start when that is a small shrink.
    backed = snap_to_sentences([{"start": 20.0, "end": 24.6, "reason": "x"}], sentences, duration=40.0)
    assert (backed[0]["start"], backed[0]["end"]) == (
        20.0,
        24.0,
    ) and "end moved back to sentence boundary at 24.0 s" in backed[0]["reason"]
    # Neither direction within tolerance: untouched, no note.
    stuck = snap_to_sentences([{"start": 20.0, "end": 26.0, "reason": "x"}], sentences, duration=40.0)
    assert stuck == [{"start": 20.0, "end": 26.0, "reason": "x"}]
    # Starts snap too: back to the sentence start within `extend`, else forward to its end within `shrink`.
    starts = snap_to_sentences(
        [{"start": 1.0, "end": 8.0, "reason": "a"}, {"start": 11.5, "end": 16.0, "reason": "b"}],
        sentences,
        duration=40.0,
    )
    assert [(s["start"], s["end"]) for s in starts] == [(0.0, 8.0), (12.0, 16.0)]
    assert (
        "start moved back to sentence start at 0.0 s" in starts[0]["reason"]
        and "start moved forward to sentence boundary at 12.0 s" in starts[1]["reason"]
    )
    # A tiny segment keeps its length rather than collapsing below MIN_SEGMENT.
    tiny = snap_to_sentences([{"start": 11.6, "end": 12.3}], sentences, duration=40.0)
    assert tiny == [{"start": 11.6, "end": 12.3}]
    # Evidence lists receive the note as well.
    evidenced = snap_to_sentences([{"start": 0.0, "end": 2.7, "evidence": ["speech"]}], sentences, duration=40.0)
    assert evidenced[0]["evidence"] == ["speech", "end moved to sentence end at 5.0 s"] and "reason" not in evidenced[0]


def test_snap_never_overlaps_neighbours_or_leaves_the_source() -> None:
    sentences = sentences_from_transcript(WORKSHOP_CUES, duration=40.0)
    # Two segments cut inside the same sentence: one side gets the whole sentence, they never overlap.
    pair = snap_to_sentences([{"start": 0.0, "end": 2.7}, {"start": 2.7, "end": 8.0}], sentences, duration=40.0)
    assert pair[0]["end"] == pair[1]["start"] and pair[0]["end"] in (0.0, 5.0) or pair[0]["end"] == 2.7
    assert pair[0]["end"] <= pair[1]["start"]
    # Editorial order differs from source order: the given order is kept and source neighbours still cannot overlap.
    hook_last = snap_to_sentences(
        [{"start": 8.0, "end": 11.2}, {"start": 0.0, "end": 2.7}, {"start": 5.2, "end": 7.9}], sentences, duration=40.0
    )
    assert [s["start"] for s in hook_last] == [8.0, 0.0, 5.0]
    by_source = sorted(hook_last, key=lambda s: s["start"])
    assert all(a["end"] <= b["start"] for a, b in zip(by_source, by_source[1:]))
    # The clip ends mid-sentence: the snapped end is clamped to the duration.
    short = snap_to_sentences([{"start": 36.0, "end": 38.5}], sentences, duration=39.0)
    assert short[0]["end"] == 39.0
    # Segments already on boundaries, overlapping inputs, unusable times and empty sentence lists pass through unchanged.
    assert snap_to_sentences([{"start": 16.0, "end": 20.0, "reason": "r"}], sentences, duration=40.0) == [
        {"start": 16.0, "end": 20.0, "reason": "r"}
    ]
    overlapping = [{"start": 0.0, "end": 3.0}, {"start": 2.0, "end": 6.0}]
    assert snap_to_sentences(overlapping, sentences, duration=40.0) == overlapping
    assert snap_to_sentences([{"start": "a", "end": 2.7}], sentences, duration=40.0) == [{"start": "a", "end": 2.7}]
    assert snap_to_sentences([{"start": 0.0, "end": 2.7}], [], duration=40.0) == [{"start": 0.0, "end": 2.7}]
    assert snap_to_sentences([{"start": 0.0, "end": 2.7}], [{"start": "x"}], duration=40.0) == [
        {"start": 0.0, "end": 2.7}
    ]


def test_snap_keeps_the_total_near_the_target_by_shrinking_the_last_segments() -> None:
    sentences = [{"start": i * 10.0, "end": i * 10.0 + 10.0, "text": f"Sentence {i}."} for i in range(4)]
    segments = [
        {"start": 0.0, "end": 8.6, "reason": "a"},
        {"start": 10.0, "end": 18.6, "reason": "b"},
        {"start": 25.0, "end": 38.6, "reason": "c"},
    ]
    free = snap_to_sentences(segments, sentences, duration=40.0)
    assert [(s["start"], s["end"]) for s in free] == [(0.0, 10.0), (10.0, 20.0), (25.0, 40.0)]
    fitted = snap_to_sentences(segments, sentences, duration=40.0, target=26.0)
    assert [(s["start"], s["end"]) for s in fitted] == [(0.0, 10.0), (10.0, 20.0), (25.0, 30.0)], (
        "the last segment backs off to its sentence start; nothing is dropped"
    )
    assert "end moved back to sentence boundary at 30.0 s" in fitted[-1]["reason"] and fitted[0]["end"] == 10.0
    # When no segment can shrink without vanishing, extensions are given back from the end instead.
    whole = [{"start": 0.0, "end": 8.6}, {"start": 10.0, "end": 18.6}, {"start": 30.0, "end": 38.6}]
    reverted = snap_to_sentences(whole, sentences, duration=40.0, target=24.0)
    assert [(s["start"], s["end"]) for s in reverted] == [(0.0, 10.0), (10.0, 18.6), (30.0, 38.6)]
    assert sum(s["end"] - s["start"] for s in reverted) <= 24.0 * 1.15 + 0.05
    # A segment wholly inside one sentence finishes it even beyond `extend` (its only non-destructive fix).
    inside = snap_to_sentences(
        [{"start": 0.0, "end": 2.7}], sentences_from_transcript(WORKSHOP_CUES), duration=40.0, extend=1.6
    )
    assert (inside[0]["start"], inside[0]["end"]) == (0.0, 5.0)
    assert snap_to_sentences(
        [{"start": 0.0, "end": 2.7}], sentences_from_transcript(WORKSHOP_CUES), duration=40.0, finish=2.0
    ) == [{"start": 0.0, "end": 2.7}]


def test_local_plan_uses_whole_sentences_and_spoken_labels() -> None:
    duration = 40.0
    speech = [
        (0.53, 3.72),
        (4.74, 7.3),
        (8.56, 11.38),
        (12.41, 14.78),
        (16.53, 19.32),
        (20.34, 23.06),
        (24.53, 28.21),
        (29.24, 31.39),
        (32.53, 35.1),
        (36.13, 39.19),
    ]
    plan = build_plan(
        goal=WORKSHOP_GOAL,
        duration=duration,
        target_duration=20,
        scenes=[8.0, 16.0, 24.0, 32.0],
        silences=_silences(duration, speech),
        transcript=WORKSHOP_CUES,
        has_audio=True,
    )
    ends = {s["end"] for s in plan["analysis"]["sentences"]}
    assert len(plan["analysis"]["sentences"]) == 10
    for segment in plan["segments"]:
        assert segment["end"] in ends, segment
        assert segment["start"] in {s["start"] for s in plan["analysis"]["sentences"]}, segment
    assert sum(s["end"] - s["start"] for s in plan["segments"]) <= 20.05
    assert (
        plan["segments"][0]["kind"] == "hook"
        and plan["segments"][0]["label"] == "This is the finished oak bowl, oiled…"
    )
    assert all(len(s["label"]) <= 40 for s in plan["segments"])
    assert all("ends on a sentence end" in s["reason"] for s in plan["segments"])
    assert plan["title"] == "The oak bowl"
    assert plan["hashtags"][:3] == ["#oak", "#bowl", "#finished"] and 3 <= len(plan["hashtags"]) <= 5
    assert not any(tag in plan["hashtags"] for tag in ("#20second", "#vertical", "#open", "#story"))
    # Without a transcript the earlier behaviour stands: generic labels, no sentences, padding kept.
    plain = build_plan(
        goal=WORKSHOP_GOAL,
        duration=duration,
        target_duration=20,
        scenes=[8.0, 16.0, 24.0, 32.0],
        silences=_silences(duration, speech),
        transcript=[],
        has_audio=True,
    )
    assert plain["analysis"]["sentences"] == [] and plain["segments"][0]["label"].startswith("Opening hook")
    assert 0 < plain["segments"][0]["start"] < 0.5, "padding, not a sentence start"


@pytest.mark.parametrize(
    "goal, title, tags",
    [
        (
            "Make a 20-second vertical story about the oak bowl: open with the finished bowl, then show how it is made.",
            "The oak bowl",
            ["#oak", "#bowl", "#finished"],
        ),
        (
            "Create a 30s square video of our espresso machine for Instagram",
            "Our espresso machine",
            ["#espresso", "#machine", "#instagram"],
        ),
        ("Show the sunrise hike, then end on the summit.", "The sunrise hike", ["#sunrise", "#hike", "#summit"]),
        ("Keep it short", "Your story", []),
    ],
)
def test_title_and_hashtags_use_content_words_not_instructions(goal: str, title: str, tags: list[str]) -> None:
    from vedit.story import _hashtags, _title_from_goal

    assert _title_from_goal(goal) == title
    assert _hashtags(goal) == tags
    # Empty content falls back to the first spoken sentence and to transcript words.
    assert (
        _title_from_goal("Keep it short", WORKSHOP_CUES)
        == "This is the finished oak bowl, oiled and ready for the table"
    )
    filled = _hashtags("Keep it short", WORKSHOP_CUES)
    assert 3 <= len(filled) <= 5 and filled[0] == "#bowl" and all(not t[1:].isdigit() for t in filled)


# ---------------------------------------------------------------------------
# Scene coverage, scene detection, post copy
# ---------------------------------------------------------------------------

FIVE_SCENE_CUES = [
    {"start": 0.0, "end": 4.0, "text": "This is the finished bowl."},
    {"start": 4.0, "end": 8.0, "text": "It started as a blank."},
    {"start": 8.0, "end": 12.0, "text": "Here is the raw blank."},
    {"start": 12.0, "end": 16.0, "text": "The grain shows the way."},
    {"start": 16.0, "end": 20.0, "text": "On the lathe the shape appears."},
    {"start": 20.0, "end": 24.0, "text": "The wall stays even."},
    {"start": 24.0, "end": 28.0, "text": "Sanding goes from coarse to fine."},
    {"start": 28.0, "end": 32.0, "text": "The grain starts to glow."},
    {"start": 32.0, "end": 36.0, "text": "Oil brings out the colour."},
    {"start": 36.0, "end": 40.0, "text": "A day later the bowl is ready."},
]
FIVE_SCENE_SPEECH = [(i * 4 + 0.4, i * 4 + 3.5) for i in range(10)]


def _scene_of(segment: dict, cuts: list[float]) -> int:
    midpoint = (segment["start"] + segment["end"]) / 2
    return sum(1 for cut in cuts if cut <= midpoint)


def test_process_brief_covers_every_scene_before_filling_by_score() -> None:
    cuts = [8.0, 16.0, 24.0, 32.0]
    kwargs = {
        "duration": 40.0,
        "scenes": cuts,
        "silences": _silences(40.0, FIVE_SCENE_SPEECH),
        "transcript": FIVE_SCENE_CUES,
        "has_audio": True,
    }
    process = build_plan(
        goal="Make a 25-second vertical story about the bowl: open with the finished bowl, then show how it is made.",
        target_duration=25,
        **kwargs,
    )
    chosen = [(s["start"], s["end"]) for s in process["segments"]]
    first = process["analysis"]["sentences"][0]
    assert chosen[0] == (first["start"], first["end"]) == (0.15, 3.75) and process["segments"][0]["kind"] == "hook"
    assert {_scene_of(s, cuts) for s in process["segments"]} == {0, 1, 2, 3, 4}, "one moment from every scene"
    assert chosen == sorted(chosen) and sum(e - s for s, e in chosen) <= 25.05
    assert all(s["end"] in {c["end"] for c in process["analysis"]["sentences"]} for s in process["segments"])
    assert any("scene 3 of 5" in s["reason"] for s in process["segments"])
    # A plain brief keeps score order with only a mild spread: the top-scored sentences win even when they share a scene.
    plain = build_plan(goal="Make a 25-second vertical story about the bowl", target_duration=12, **kwargs)
    plain_scenes = [_scene_of(s, cuts) for s in plain["segments"]]
    assert (
        plain["segments"][0]["kind"] == "hook"
        and plain["segments"][-1]["kind"] == "closing"
        and len(plain["segments"]) == 3
    )
    assert plain_scenes[0] == 0 and plain_scenes[-1] == 4, (
        "score order: the two 'bowl' sentences, then one equal-scored scene opener"
    )
    # Keyword scoring still matters inside the coverage pass: the "sanding" scene picks the sentence that mentions sanding.
    sanding = build_plan(goal="Show how it is made, especially the sanding", target_duration=25, **kwargs)
    assert any(abs(s["start"] - 24.15) < 0.01 and "sanding" in s["label"].lower() for s in sanding["segments"])


def test_select_moments_coverage_bonus_walks_unrepresented_spans() -> None:
    moments = [
        Moment(0, 4, 0.9, "hook", span=0),
        Moment(4, 8, 0.8, "speech", span=0),
        Moment(8, 12, 0.5, "speech", span=1),
        Moment(12, 16, 0.4, "speech", span=2),
    ]
    assert [(m.start, m.end) for m in select_moments(moments, 12)] == [(0, 4), (4, 8), (8, 12)]
    assert [(m.start, m.end) for m in select_moments(moments, 12, coverage=1.0)] == [(0, 4), (8, 12), (12, 16)]
    # Mild coverage does not overturn a clear score gap.
    assert [(m.start, m.end) for m in select_moments(moments, 12, coverage=0.15)] == [(0, 4), (4, 8), (8, 12)]
    from vedit.story import coverage_weight

    assert (
        coverage_weight("show how it is made", spans=True) == 1.0
        and coverage_weight("behind the scenes at the shop", spans=True) == 1.0
    )
    assert (
        coverage_weight("a story about the bowl", spans=True) == 0.15
        and coverage_weight("show the process", spans=False) == 0.0
    )


def test_temporal_spans_replace_missing_scene_cuts() -> None:
    assert (
        temporal_spans(40.0, 25.0, FIVE_SCENE_CUES) == [8.0, 16.0, 24.0, 32.0, 40.0][:-1] + []
        if False
        else temporal_spans(40.0, 25.0, FIVE_SCENE_CUES) == [6.667, 13.333, 20.0, 26.667, 33.333]
    )
    assert temporal_spans(40.0, 8.0) == [20.0]
    assert temporal_spans(3.0, 20.0) == [1.5] and temporal_spans(0, 10) == []
    assert len(temporal_spans(600.0, 180.0)) == MAX_SEGMENTS - 1
    no_cuts = build_plan(
        goal="Show how the bowl is made",
        duration=40.0,
        target_duration=25,
        scenes=[],
        silences=_silences(40.0, FIVE_SCENE_SPEECH),
        transcript=FIVE_SCENE_CUES,
        has_audio=True,
    )
    starts = [s["start"] for s in no_cuts["segments"]]
    assert (
        starts == sorted(starts)
        and starts[0] == 0.15
        and starts[-1] >= 32.0
        and max(b - a for a, b in zip(starts, starts[1:])) <= 12.0
    ), starts
    assert any(re.search(r"span [2-7] of 7", s["reason"]) for s in no_cuts["segments"]), (
        "spans are labelled honestly, never called scenes"
    )


def test_pick_scene_cuts_keeps_strong_cuts_and_sparse_weak_outliers() -> None:
    strong = [(1.0, 0.45), (1.03, 0.4), (9.0, 0.12), (20.0, 0.5)]
    assert pick_scene_cuts(strong) == [1.0, 20.0]
    # Hard cuts between similar shots score 0.1–0.2; they are kept when nothing strong exists.
    weak = [(8.02, 0.169), (16.02, 0.206), (24.02, 0.116), (32.02, 0.151)]
    assert pick_scene_cuts(weak, duration=40.0) == [8.02, 16.02, 24.02, 32.02]
    assert pick_scene_cuts([(t, 0.06) for t, _ in weak], duration=40.0) == []
    # Motion noise: dense weak scores collapse to the strongest, at least 2 s apart and at most one per 5 s.
    noise = [(i * 0.5, 0.1 + (i % 7) * 0.01) for i in range(120)]
    picked = pick_scene_cuts(noise, duration=60.0)
    assert len(picked) <= 12 and all(b - a >= 2.0 for a, b in zip(picked, picked[1:]))
    assert pick_scene_cuts([]) == [] and pick_scene_cuts([(float("nan"), 0.5)]) == []


def test_post_copy_is_title_plus_complete_spoken_sentences() -> None:
    from vedit.story import _post_copy

    sentences = sentences_from_transcript(WORKSHOP_CUES, duration=40.0)
    segments = [{"start": 0.0, "end": 5.0}, {"start": 24.0, "end": 29.0}, {"start": 36.0, "end": 40.0}]
    copy = _post_copy("The oak bowl", segments, sentences)
    assert (
        copy
        == "The oak bowl. This is the finished oak bowl, oiled and ready for the table. Sanding goes from coarse to fine, with the lathe turning slowly."
    )
    assert len(copy) <= 220
    # A moment that cuts a sentence contributes nothing; a pause-closed fragment without punctuation is never quoted.
    assert _post_copy("The oak bowl", [{"start": 0.0, "end": 2.7}], sentences) == "The oak bowl."
    fragment = [{"start": 0.0, "end": 3.0, "text": "oiled and"}]
    assert _post_copy("Title.", [{"start": 0.0, "end": 3.0}], fragment) == "Title."
    assert _post_copy("", segments, sentences).startswith("This is the finished oak bowl")
    long_title = "A" * 215
    assert _post_copy(long_title, segments, sentences) == long_title + "."
    plan = build_plan(
        goal=WORKSHOP_GOAL,
        duration=40.0,
        target_duration=20,
        scenes=[8.0, 16.0, 24.0, 32.0],
        silences=[],
        transcript=WORKSHOP_CUES,
        has_audio=True,
    )
    assert (
        plan["caption"].startswith("The oak bowl. ") and "Make a" not in plan["caption"] and len(plan["caption"]) <= 220
    )
