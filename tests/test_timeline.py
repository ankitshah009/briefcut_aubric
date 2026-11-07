"""The one clock every part of the edit reads."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from vedit.timeline import (
    FILLER_PAD,
    build_timeline,
    effective_transition,
    filler_removals,
    map_cues,
    map_interval,
    output_duration,
    pause_removals,
    plan_timeline,
    resolve_segments,
    segment_keeps,
    segment_starts,
    subtract,
    to_output,
    to_source,
)


def test_plain_segments_play_end_to_end_in_editorial_order() -> None:
    spans = build_timeline([{"start": 10, "end": 12}, {"start": 2, "end": 5}])
    assert [(s.segment, s.output_start, s.output_end) for s in spans] == [(1, 0, 2), (2, 2, 5)]
    assert output_duration(spans) == 5
    assert to_output(spans, 3) == 3
    assert to_source(spans, 1) == (1, 11)
    assert to_output(spans, 7) is None


def test_speed_and_keep_change_the_output_clock() -> None:
    spans = build_timeline([{"start": 0, "end": 10, "speed": 2.0, "keep": [[0, 2], [4, 6]]}])
    assert output_duration(spans) == pytest.approx(2.0)
    assert to_output(spans, 5) == pytest.approx(1.5)
    assert to_output(spans, 3) is None
    assert to_source(spans, 1.5) == (1, pytest.approx(5.0))


def test_a_transition_overlaps_neighbours_and_never_eats_a_short_moment() -> None:
    segments = [{"start": 0, "end": 4}, {"start": 10, "end": 14}]
    spans = build_timeline(segments, transition=0.5)
    assert segment_starts(spans) == {1: 0, 2: 3.5}
    assert output_duration(spans) == pytest.approx(7.5)
    assert effective_transition([{"start": 0, "end": 1}, {"start": 2, "end": 6}], 2.0) == pytest.approx(0.4)
    assert effective_transition([{"start": 0, "end": 1}], 1.0) == 0


def test_a_moment_with_no_valid_keep_plays_whole_and_keep_pieces_merge() -> None:
    assert segment_keeps({"start": 1, "end": 3, "keep": [[9, 10]]}) == [(1, 3)]
    assert segment_keeps({"start": 0, "end": 5, "keep": [[0, 1], [0.5, 2], {"start": 3, "end": 4}]}) == [(0, 2), (3, 4)]
    assert segment_keeps({"start": 3, "end": 1}) == []


def test_map_interval_reports_each_piece_of_a_cut_up_range() -> None:
    spans = build_timeline([{"start": 0, "end": 10, "keep": [[0, 2], [4, 6]]}])
    assert map_interval(spans, 1, 5) == [(1, 1, 3)]
    other = build_timeline([{"start": 0, "end": 2}, {"start": 4, "end": 6}])
    assert map_interval(other, 1, 5) == [(1, 1, 2), (2, 2, 3)]
    assert map_interval(other, 2.5, 3.5) == []


def test_cues_with_words_lose_the_words_that_were_cut() -> None:
    spans = build_timeline([{"start": 0, "end": 3, "keep": [[0, 1], [1.5, 3]]}])
    cue = {
        "start": 0,
        "end": 3,
        "text": "well um okay",
        "words": [
            {"start": 0.2, "end": 0.8, "text": "well"},
            {"start": 1.1, "end": 1.4, "text": "um"},
            {"start": 1.6, "end": 2.5, "text": "okay"},
        ],
    }
    moved = map_cues([cue], spans)
    assert [item["text"] for item in moved] == ["well okay"]
    assert moved[0]["start"] == pytest.approx(0.2) and moved[0]["end"] == pytest.approx(2.0)


def test_cues_without_words_split_across_moments_keep_the_words_that_play() -> None:
    spans = build_timeline([{"start": 0, "end": 2}, {"start": 4, "end": 6}])
    moved = map_cues([{"start": 1, "end": 5, "text": "one two three four"}], spans)
    # The middle half of the line was cut, so the words estimated there leave with it.
    assert [item["text"] for item in moved] == ["one", "four"]
    assert map_cues([{"start": 2.5, "end": 3, "text": "gone"}], spans) == []


def test_subtract_drops_slivers() -> None:
    assert subtract(0, 10, [(2, 3), (3.02, 5)]) == [(0, 2), (5, 10)]
    assert subtract(0, 1, [(0, 1)]) == []


def test_pause_removal_keeps_a_breath_and_never_blanks_a_silent_moment() -> None:
    speech = [{"start": 1, "end": 3}, {"start": 5, "end": 6}]
    assert pause_removals(speech, 0, 8) == [(0, 0.88), (3.12, 4.88), (6.12, 8)]
    assert pause_removals(speech, 3.2, 4.8) == []
    assert pause_removals([{"start": 1, "end": 2}, {"start": 2.3, "end": 3}], 1, 3) == []


def test_filler_removal_only_touches_what_was_heard() -> None:
    words = [
        {"start": 0.0, "end": 0.3, "text": "So"},
        {"start": 0.4, "end": 0.7, "text": "um,"},
        {"start": 0.8, "end": 1.0, "text": "the"},
        {"start": 1.05, "end": 1.2, "text": "the"},
        {"start": 1.3, "end": 1.6, "text": "shape"},
    ]
    assert filler_removals(words, 0, 2) == [(0.38, 0.72), (0.78, 1.05)]
    assert filler_removals([{"start": 0, "end": 1, "text": "umbrella"}], 0, 2) == []


def _turn_plan(cues: list[dict[str, object]], words: list[dict[str, object]] | None = None) -> dict[str, object]:
    """A 5 s moment whose transcript is turn-timed, the shape Meta Voice Transcribe stores."""
    analysis: dict[str, object] = {"transcript": cues}
    if words is not None:
        analysis["words"] = words
    return {"segments": [{"start": 0.0, "end": 5.0}], "analysis": analysis, "edit": {"remove_fillers": True}}


def test_a_filler_only_line_is_cut_and_a_filler_inside_a_sentence_stays() -> None:
    """Meta times the line. A line that is only "um" goes; "um, I think" has nothing to cut inside."""
    removed = plan_timeline(_turn_plan([{"start": 1.0, "end": 1.4, "text": "um"}]))
    assert removed["segments"][0]["keep"] == [
        [pytest.approx(0.0), pytest.approx(1.0 - FILLER_PAD)],
        [pytest.approx(1.4 + FILLER_PAD), pytest.approx(5.0)],
    ]
    assert removed["duration"] == pytest.approx(5.0 - (0.4 + 2 * FILLER_PAD))
    kept = plan_timeline(_turn_plan([{"start": 1.0, "end": 1.4, "text": "um, I think"}]))
    assert kept["duration"] == pytest.approx(5.0) and "keep" not in kept["segments"][0]
    whole = plan_timeline(_turn_plan([{"start": 0.0, "end": 5.0, "text": "um"}]))
    assert whole["duration"] == pytest.approx(5.0) and "keep" not in whole["segments"][0]


def test_a_line_repeated_back_to_back_drops_the_second_take() -> None:
    close = plan_timeline(
        _turn_plan(
            [
                {"start": 1.0, "end": 1.5, "text": "the bowl"},
                {"start": 1.6, "end": 2.1, "text": "the bowl"},
            ]
        )
    )
    assert close["segments"][0]["keep"] == [
        [pytest.approx(0.0), pytest.approx(1.6 - FILLER_PAD)],
        [pytest.approx(2.1 + FILLER_PAD), pytest.approx(5.0)],
    ]
    assert close["duration"] == pytest.approx(5.0 - (0.5 + 2 * FILLER_PAD))
    wide = plan_timeline(
        _turn_plan(
            [
                {"start": 1.0, "end": 1.5, "text": "the bowl"},
                {"start": 3.5, "end": 4.0, "text": "the bowl"},
            ]
        )
    )
    assert wide["duration"] == pytest.approx(5.0) and "keep" not in wide["segments"][0]
    heard = plan_timeline(
        _turn_plan(
            [
                {"start": 1.0, "end": 1.5, "text": "the bowl"},
                {"start": 1.6, "end": 2.1, "text": "the bowl"},
            ],
            words=[
                {"start": 1.6, "end": 1.8, "text": "the"},
                {"start": 1.8, "end": 2.1, "text": "bowl"},
            ],
        )
    )
    assert heard["duration"] == pytest.approx(5.0) and "keep" not in heard["segments"][0]


def test_an_estimated_filler_inside_a_sentence_does_not_move_the_cut() -> None:
    words = [
        {"start": 0.5, "end": 1.0, "text": "So", "estimated": True},
        {"start": 1.2, "end": 1.6, "text": "um", "estimated": True},
        {"start": 1.8, "end": 3.0, "text": "there", "estimated": True},
    ]
    assert filler_removals(words, 0, 5) == []
    view = plan_timeline(_turn_plan([{"start": 0.5, "end": 3.0, "text": "So um there"}], words))
    assert view["duration"] == pytest.approx(5.0) and "keep" not in view["segments"][0]
    line = plan_timeline(
        _turn_plan(
            [{"start": 1.0, "end": 1.4, "text": "um"}],
            [{"start": 1.05, "end": 1.35, "text": "um", "estimated": True}],
        )
    )
    assert line["segments"][0]["keep"] == [
        [pytest.approx(0.0), pytest.approx(1.0 - FILLER_PAD)],
        [pytest.approx(1.4 + FILLER_PAD), pytest.approx(5.0)],
    ]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_page_clock_matches_filler_cuts() -> None:
    """The timeline mirror cuts the same filler lines the server clock cuts."""
    source = (Path(__file__).resolve().parents[1] / "src/vedit/studio/static/timeline.js").read_text(encoding="utf-8")
    start = source.index("  const finiteNumber = (value) =>")
    marker = "  window.BriefcutClock = Clock;"
    snippet = source[start : source.index(marker) + len(marker)]
    plans = [
        _turn_plan([{"start": 1.0, "end": 1.4, "text": "um"}]),
        _turn_plan([{"start": 1.0, "end": 1.4, "text": "um, I think"}]),
        _turn_plan(
            [
                {"start": 1.0, "end": 1.5, "text": "the bowl"},
                {"start": 1.6, "end": 2.1, "text": "the bowl"},
            ]
        ),
        {
            "segments": [{"start": 0.0, "end": 2.0}],
            "analysis": {
                "words": [
                    {"start": 0.0, "end": 0.3, "text": "So"},
                    {"start": 0.4, "end": 0.7, "text": "um,"},
                    {"start": 0.8, "end": 1.0, "text": "the"},
                    {"start": 1.05, "end": 1.2, "text": "the"},
                    {"start": 1.3, "end": 1.6, "text": "shape"},
                ]
            },
            "edit": {"remove_fillers": True},
        },
    ]
    script = (
        "const window = global;\n"
        + snippet
        + "\nconst plans = "
        + json.dumps(plans)
        + ";\nprocess.stdout.write(JSON.stringify(plans.map((plan) => window.BriefcutClock.plan(plan))));\n"
    )
    completed = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False, timeout=30)
    assert completed.returncode == 0, completed.stderr
    page = json.loads(completed.stdout)
    for plan, mirror in zip(plans, page, strict=True):
        view = plan_timeline(plan)
        assert mirror["duration"] == pytest.approx(view["duration"], abs=0.001)
        server_keep = view["segments"][0].get("keep")
        page_keep = mirror["segments"][0].get("keep")
        if server_keep is None:
            assert page_keep is None
        else:
            assert isinstance(page_keep, list) and len(page_keep) == len(server_keep)
            for page_piece, server_piece in zip(page_keep, server_keep, strict=True):
                assert page_piece == pytest.approx(server_piece, abs=0.001)


def test_a_heard_word_on_a_filler_line_keeps_the_word_cut() -> None:
    view = plan_timeline(
        _turn_plan(
            [{"start": 1.0, "end": 2.0, "text": "um"}],
            [{"start": 1.2, "end": 1.5, "text": "um"}],
        )
    )
    assert view["segments"][0]["keep"] == [
        [pytest.approx(0.0), pytest.approx(1.2 - FILLER_PAD)],
        [pytest.approx(1.5 + FILLER_PAD), pytest.approx(5.0)],
    ]


def test_resolve_applies_settings_and_refuses_to_erase_a_moment() -> None:
    plan = {
        "segments": [{"start": 0, "end": 8, "label": "a"}, {"start": 10, "end": 12, "label": "b"}],
        "analysis": {"speech": [{"start": 1, "end": 3}, {"start": 5, "end": 6}]},
        "edit": {"trim_pauses": True, "removed": [[10, 12]]},
    }
    resolved = resolve_segments(plan)
    assert resolved[0]["keep"] == [[0.88, 3.12], [4.88, 6.12]]
    assert "keep" not in resolved[1]
    assert plan["segments"][0].get("keep") is None


def test_plan_timeline_is_the_single_view_of_duration() -> None:
    plan = {
        "segments": [{"start": 0, "end": 4, "speed": 2.0}, {"start": 10, "end": 14}],
        "edit": {"transition": {"type": "fade", "seconds": 0.5}},
    }
    view = plan_timeline(plan)
    assert view["transition_seconds"] == pytest.approx(0.5)
    assert view["duration"] == pytest.approx(5.5)
    assert plan_timeline({"segments": plan["segments"], "edit": {"transition": {"type": "none", "seconds": 0.5}}})[
        "duration"
    ] == pytest.approx(6.0)


def test_a_word_across_a_cut_stays_with_the_moment_it_starts_in() -> None:
    """Under a transition the next moment begins earlier on the output clock, so the word's end must come from its own moment."""
    spans = build_timeline([{"start": 0, "end": 2}, {"start": 4, "end": 6}], transition=0.5)
    cue = {
        "start": 1.5,
        "end": 4.5,
        "text": "over the cut",
        "words": [
            {"start": 1.5, "end": 1.8, "text": "over"},
            {"start": 1.8, "end": 4.2, "text": "the"},
            {"start": 4.2, "end": 4.5, "text": "cut"},
        ],
    }
    first, second = map_cues([cue], spans)
    assert first["text"] == "over the" and first["segment"] == 1 and first["end"] == pytest.approx(2.0)
    assert all(word["end"] >= word["start"] for word in first["words"])
    assert second["text"] == "cut" and second["segment"] == 2


def test_to_source_can_name_the_moment_inside_a_transition() -> None:
    spans = build_timeline([{"start": 0, "end": 2}, {"start": 4, "end": 6}], transition=0.5)
    assert to_source(spans, 1.75) == (2, pytest.approx(4.25)), "the incoming moment wins by default"
    assert to_source(spans, 1.75, segment=1) == (1, pytest.approx(1.75))
