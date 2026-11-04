"""Deterministic render grading and pure patch application."""

from __future__ import annotations

import math

import pytest

from vedit.campaign import validate_plan
from vedit.lint import apply_patches, lint_plan, output_time

SENTENCES = [
    {"start": 0.0, "end": 4.0, "text": "This is the finished bowl."},
    {"start": 4.2, "end": 8.0, "text": "It took three days of carving."},
    {"start": 8.5, "end": 12.0, "text": "Food safe oil brings out the grain."},
]


def plan(*ranges: tuple[float, float], **extra):
    segments = [{"start": s, "end": e, "label": f"Moment {i}", "reason": "Chosen."} for i, (s, e) in enumerate(ranges, 1)]
    return {"title": "Bowl", "segments": segments, "duration": round(sum(e - s for s, e in ranges), 3), **extra}


LENGTH = "auto: length fits the target"


def named(result: dict, name: str) -> dict:
    # The length check names its ceiling ("auto: length fits the target (≤ 8 s)"), so it is matched by its stem.
    return next(check for check in result["checks"] if check["name"] == name or (name == LENGTH and check["name"].startswith(LENGTH + " (")))


def test_checks_come_in_contract_order_and_pass_a_clean_plan() -> None:
    result = lint_plan(plan((0.0, 4.0), (8.5, 12.0)), target_duration=8, duration=20, sentences=SENTENCES)
    # Captions were not requested, so nothing measured them and there is no caption check.
    assert [check["name"] for check in result["checks"]] == [
        "auto: cuts land on sentence boundaries",
        "auto: length fits the target (\u2264 8 s)",
        "auto: moments inside the source and in order",
    ]
    assert all(check["passed"] for check in result["checks"])
    assert result["patches"] == []
    for check in result["checks"]:
        assert set(check) == {"name", "passed", "at", "note", "basis", "by"}


def test_no_transcript_leaves_out_the_sentence_check() -> None:
    result = lint_plan(plan((0.0, 2.0)), target_duration=2, duration=10)
    assert "auto: cuts land on sentence boundaries" not in [check["name"] for check in result["checks"]]


def test_mid_sentence_cut_fails_with_output_time_and_snaps_to_the_nearest_boundary() -> None:
    # Second moment starts 1.0 s into sentence two (extend by 1.0 ≤ 1.6) and
    # ends 0.5 s before sentence three ends (extend by 0.5 wins over shrinking 3.0).
    result = lint_plan(plan((0.0, 4.0), (5.2, 11.5)), target_duration=10, duration=20, sentences=SENTENCES)
    check = named(result, "auto: cuts land on sentence boundaries")
    assert check["passed"] is False
    assert check["at"] == 4.0  # the offending start plays right after the 4 s opener
    assert "2 cuts" in check["note"]
    patches = result["patches"]
    assert [(p["op"], p["segment"], p["seconds"], p["source"]) for p in patches] == [
        ("trim_start", 2, 4.2, "auto"), ("trim_end", 2, 12.0, "auto"),
    ]
    assert all("mid-sentence" in p["reason"] for p in patches)
    fixed, applied = apply_patches(plan((0.0, 4.0), (5.2, 11.5)), patches, duration=20)
    assert [item["skipped"] for item in applied] == [None, None]
    assert fixed["segments"][1]["start"] == 4.2 and fixed["segments"][1]["end"] == 12.0
    assert lint_plan(fixed, target_duration=12, duration=20, sentences=SENTENCES)["checks"][0]["passed"]


def test_boundary_within_tolerance_or_too_far_to_snap_is_handled() -> None:
    # 0.1 s inside: within the 0.15 s tolerance, passes.
    assert named(lint_plan(plan((0.1, 4.0)), target_duration=4, duration=20, sentences=SENTENCES), "auto: cuts land on sentence boundaries")["passed"]
    # 2.0 s inside a sentence: extending costs 2.0 (> 1.6) and shrinking costs 2.0 (> 0.8): fails without a patch.
    result = lint_plan(plan((2.0, 4.0)), target_duration=2, duration=20, sentences=SENTENCES)
    assert named(result, "auto: cuts land on sentence boundaries")["passed"] is False
    assert result["patches"] == []


def test_duration_window_and_trim_patches_for_an_overlong_cut() -> None:
    within = lint_plan(plan((0.0, 10.5)), target_duration=10, duration=20)
    assert named(within, LENGTH)["passed"], "0.5 s over a 10 s target is inside the allowance"
    assert named(lint_plan(plan((0.0, 11.0)), target_duration=10, duration=20), LENGTH)["passed"] is False
    over = lint_plan(plan((0.0, 4.0), (6.0, 16.0)), target_duration=10, duration=20)
    check = named(over, LENGTH)
    assert check["passed"] is False and "14.0 s cut, 4.0 s over the 10.0 s target" in check["note"] and check["at"] == 10.0
    assert over["patches"] == [{"op": "trim_end", "segment": 2, "seconds": 12.0, "reason": over["patches"][0]["reason"], "source": "auto"}]
    fixed, _ = apply_patches(plan((0.0, 4.0), (6.0, 16.0)), over["patches"], duration=20)
    assert fixed["duration"] == 10.0
    # A moment too short to absorb the excess is kept; a longer one is shortened instead.
    over = lint_plan(plan((0.0, 10.0), (12.0, 13.0)), target_duration=6, duration=20)
    assert [(p["op"], p["segment"], p["seconds"]) for p in over["patches"]] == [("trim_end", 1, 5.0)]
    fixed, applied = apply_patches(plan((0.0, 10.0), (12.0, 13.0)), over["patches"], duration=20)
    assert fixed["duration"] == 6.0 and len(fixed["segments"]) == 2 and [a["skipped"] for a in applied] == [None]
    # When no single moment can absorb it, the lowest-ranked is dropped and the search repeats.
    over = lint_plan(plan((0.0, 3.0), (5.0, 8.0), (10.0, 13.0)), target_duration=4, duration=20)
    assert [(p["op"], p["segment"]) for p in over["patches"]] == [("remove", 3), ("trim_end", 2)]
    # The target is a ceiling: a short cut passes, with a separate note that is not a failure.
    under = lint_plan(plan((0.0, 2.0)), target_duration=10, duration=20)
    assert named(under, LENGTH)["passed"] is True
    note = named(under, "auto: note: the cut is well under the target")
    assert note["passed"] is True and note["advisory"] is True and "2.0 s, 20% of the 10.0 s target" in note["note"]
    assert under["patches"] == []


def test_bounds_check_flags_order_bounds_length_and_overlap() -> None:
    result = lint_plan(plan((0.0, 4.0), (3.0, 7.0), (18.0, 22.0), (9.0, 9.2)), target_duration=12, duration=20)
    check = named(result, "auto: moments inside the source and in order")
    assert check["passed"] is False
    assert "moment 3 leaves" in check["note"] and "moment 4 is shorter" in check["note"] and "moment 2 overlaps" in check["note"]
    assert sorted((p["op"], p["segment"]) for p in result["patches"] if p["op"] == "remove") == [("remove", 2), ("remove", 3), ("remove", 4)]
    assert named(lint_plan(plan((3.0, 7.0), (0.0, 2.0)), target_duration=6, duration=20), "auto: moments inside the source and in order")["passed"]


def test_bounds_check_never_proposes_removing_the_only_moment() -> None:
    result = lint_plan(plan((0.0, 0.2)), target_duration=1, duration=20)
    assert named(result, "auto: moments inside the source and in order")["passed"] is False
    assert result["patches"] == []


def test_caption_coverage_is_measured_on_the_output_clock() -> None:
    speech = [{"start": 1.0, "end": 3.0}, {"start": 10.0, "end": 12.0}]
    story = plan((0.0, 4.0), (9.0, 13.0))
    # Both speech bands fully covered: band one plays at 1–3 s, band two at 5–7 s.
    full = lint_plan(story, target_duration=8, duration=20, captions=[{"start": 0.8, "end": 3.2}, {"start": 4.9, "end": 7.1}], speech=speech)
    check = named(full, "auto: captions cover the speech")
    assert check["passed"] and check["note"].startswith("100% of 4.0 s")
    half = lint_plan(story, target_duration=8, duration=20, captions=[{"start": 0.8, "end": 3.2}], speech=speech)
    check = named(half, "auto: captions cover the speech")
    assert check["passed"] is False and check["at"] == 5.0 and check["note"].startswith("50%")
    silent = lint_plan(story, target_duration=8, duration=20, captions=[], speech=[])
    assert named(silent, "auto: captions cover the speech")["note"].startswith("Captions were requested. The transcript holds no speech in this cut")


def test_output_time_sums_previous_segments() -> None:
    story = plan((5.0, 8.0), (0.0, 2.0), (10.0, 11.0))
    assert output_time(story, 1, 5.0) == 0.0
    assert output_time(story, 1, 6.5) == 1.5
    assert output_time(story, 2, 1.0) == 4.0
    assert output_time(story, 3, 10.5) == 5.5
    with pytest.raises(ValueError):
        output_time(story, 4, 0.0)


def test_apply_patches_skips_unsafe_edits_and_never_raises() -> None:
    story = plan((0.0, 4.0), (6.0, 10.0))
    patches = [
        {"op": "trim_end", "segment": 1, "seconds": 0.3, "reason": "too short", "source": "director"},
        {"op": "trim_end", "segment": 1, "seconds": 7.0, "reason": "overlap", "source": "director"},
        {"op": "trim_start", "segment": 2, "seconds": -1, "reason": "bounds", "source": "auto"},
        {"op": "trim_end", "segment": 2, "seconds": 25, "reason": "bounds", "source": "auto"},
        {"op": "trim_end", "segment": 9, "seconds": 1, "reason": "unknown", "source": "auto"},
        {"op": "explode", "segment": 1, "seconds": 1, "reason": "unknown op", "source": "auto"},
        {"op": "trim_start", "segment": 1, "seconds": math.nan, "reason": "nan", "source": "auto"},
        "not a patch",
        {"op": "trim_end", "segment": 1, "seconds": 3.0, "reason": "fine", "source": "director"},
    ]
    fixed, applied = apply_patches(story, patches, duration=20)
    assert [item["skipped"] for item in applied] == [
        "would be shorter than 0.5 s", "would overlap another moment", "outside the source", "outside the source",
        "unknown moment", "unknown patch operation", "missing seconds", None,
    ]
    assert applied[-1] == {"op": "trim_end", "segment": 1, "before": {"start": 0.0, "end": 4.0}, "after": {"start": 0.0, "end": 3.0}, "reason": "fine", "source": "director", "skipped": None, "seconds": 3.0, "summary": "Moment 1 ends at 3.0 s."}
    assert fixed["segments"][0]["end"] == 3.0 and fixed["segments"][1] == story["segments"][1]
    assert fixed["segments"][0]["reason"].endswith("(end moved to 3.00 s)")
    assert fixed["duration"] == 7.0
    assert story["segments"][0]["end"] == 4.0  # the input plan is untouched
    assert validate_plan(fixed, 20, original=story)["duration"] == 7.0


def test_apply_patches_remove_and_move_keep_original_indices() -> None:
    story = plan((0.0, 2.0), (4.0, 6.0), (8.0, 10.0))
    patches = [
        {"op": "move_before", "segment": 3, "seconds": 1, "reason": "open on the reveal", "source": "director"},
        {"op": "remove", "segment": 2, "seconds": None, "reason": "slow", "source": "director"},
        {"op": "remove", "segment": 2, "seconds": None, "reason": "twice", "source": "director"},
        {"op": "move_before", "segment": 1, "seconds": 2, "reason": "target gone", "source": "director"},
        {"op": "move_before", "segment": 1, "seconds": 1, "reason": "self", "source": "director"},
        {"op": "trim_start", "segment": 3, "seconds": 8.5, "reason": "tighten", "source": "auto"},
    ]
    fixed, applied = apply_patches(story, patches, duration=20)
    assert [item["skipped"] for item in applied] == [None, None, "moment already removed", "target moment is unavailable", "target moment is unavailable", None]
    assert [(s["start"], s["end"]) for s in fixed["segments"]] == [(8.5, 10.0), (0.0, 2.0)]
    assert "moved before moment 1" in fixed["segments"][0]["reason"] and "start moved to 8.50 s" in fixed["segments"][0]["reason"]
    assert fixed["duration"] == 3.5
    assert applied[1]["after"] is None
    only, applied = apply_patches(plan((0.0, 2.0)), [{"op": "remove", "segment": 1, "seconds": None, "reason": "x", "source": "auto"}], duration=20)
    assert applied[0]["skipped"] == "would leave no moments" and len(only["segments"]) == 1


def test_apply_patches_bounds_the_reason_note() -> None:
    story = plan((0.0, 4.0))
    story["segments"][0]["reason"] = "r" * 600
    fixed, _ = apply_patches(story, [{"op": "trim_end", "segment": 1, "seconds": 3.0, "reason": "", "source": "auto"}], duration=20)
    assert len(fixed["segments"][0]["reason"]) <= 600
    validate_plan(fixed, 20)


def test_subject_in_frame_check_grades_sam_tracks():
    from vedit.lint import subject_in_frame_check

    steady = [{"time": i / 10, "x": 0.4, "y": 0.3, "width": 0.2, "height": 0.3} for i in range(100)]
    good = subject_in_frame_check(steady, duration=10, aspect="9:16", source_width=1280, source_height=720)
    assert good["passed"] and good["at"] is None and "100%" in good["note"]
    assert subject_in_frame_check([], duration=10, aspect="9:16", source_width=1280, source_height=720)["note"] == "not requested"
    gappy = [box for box in steady if not 2 < box["time"] < 6]
    gap = subject_in_frame_check(gappy, duration=10, aspect="9:16", source_width=1280, source_height=720)
    assert not gap["passed"] and gap["at"] == 2.0 and "longest gap" in gap["note"]
    # A subject wider than the vertical crop cannot be kept inside it.
    wide = [{"time": i / 10, "x": 0.0, "y": 0.3, "width": 0.98, "height": 0.3} for i in range(100)]
    assert subject_in_frame_check(wide, duration=10, aspect="9:16", source_width=1280, source_height=720)["passed"]  # centre is what must stay inside
    edge = [{"time": i / 10, "x": 0.9 if i % 2 else 0.0, "y": 0.3, "width": 0.1, "height": 0.3} for i in range(100)]
    assert subject_in_frame_check(edge, duration=10, aspect="9:16", source_width=1280, source_height=720)["passed"]


def test_strict_lint_moves_a_model_cut_to_the_nearest_boundary_however_far() -> None:
    # The live note run: a model extended moment 1 to 5.58 s, 1.0 s past the sentence end
    # and 1.9 s short of the next one. Outside the normal tolerances, so only strict snaps it.
    sentences = [{"start": 0.3, "end": 4.585}, {"start": 4.6, "end": 7.5}, {"start": 8.0, "end": 11.6}]
    plan = {"duration": 8.98, "segments": [{"start": 0.0, "end": 5.58, "label": "a", "reason": ""}, {"start": 8.0, "end": 11.6, "label": "b", "reason": ""}]}
    loose = lint_plan(plan, target_duration=25, duration=40, sentences=sentences)
    assert not loose["checks"][0]["passed"] and loose["patches"] == []
    strict = lint_plan(plan, target_duration=25, duration=40, sentences=sentences, strict=True)
    assert [(patch["op"], patch["segment"], patch["seconds"]) for patch in strict["patches"]] == [("trim_end", 1, 4.6)]
    fixed, applied = apply_patches(plan, strict["patches"], duration=40)
    assert applied[0]["skipped"] is None
    assert lint_plan(fixed, target_duration=25, duration=40, sentences=sentences)["checks"][0]["passed"]


def edited(*ranges: tuple[float, float], speech=None, **edit) -> dict:
    story = plan(*ranges)
    story["edit"] = edit
    story["analysis"] = {"speech": speech or []}
    return story


def test_output_time_reads_the_clock_when_time_moves() -> None:
    fast = plan((0.0, 4.0), (10.0, 12.0))
    fast["segments"][0]["speed"] = 2.0
    assert output_time(fast, 1, 2.0) == 1.0
    assert output_time(fast, 2, 11.0) == 3.0
    cut = edited((0.0, 10.0), (20.0, 22.0), removed=[[2.0, 5.0]])
    assert output_time(cut, 1, 6.0) == 3.0
    # A removed time reads as the next moment that still plays.
    assert output_time(cut, 1, 3.0) == 2.0
    assert output_time(cut, 2, 20.0) == 7.0
    blended = edited((0.0, 4.0), (10.0, 14.0), transition={"type": "fade", "seconds": 0.5})
    assert output_time(blended, 2, 10.0) == 3.5


def test_duration_check_counts_the_finished_length() -> None:
    fast = plan((0.0, 20.0))
    fast["segments"][0]["speed"] = 2.0
    assert "10.0 s cut" in named(lint_plan(fast, target_duration=10, duration=30), LENGTH)["note"]
    trimmed = edited((0.0, 20.0), removed=[[5.0, 15.0]])
    assert named(lint_plan(trimmed, target_duration=10, duration=30), LENGTH)["passed"]
    overlapped = edited((0.0, 6.0), (10.0, 16.0), (20.0, 26.0), transition={"type": "fade", "seconds": 1.0})
    assert "16.0 s cut" in named(lint_plan(overlapped, target_duration=16, duration=30), LENGTH)["note"]


def test_a_long_cut_is_shortened_through_speed_and_keep() -> None:
    fast = plan((0.0, 20.0))
    fast["segments"][0]["speed"] = 2.0
    # 10 s of output at 2x is 20 s of source; losing 5 s of output moves the end by 10 s of source.
    result = lint_plan(fast, target_duration=5, duration=30)
    assert [(p["op"], p["seconds"]) for p in result["patches"]] == [("trim_end", 10.0)]
    cut = edited((0.0, 20.0), removed=[[10.0, 18.0]])
    # 12 s plays; losing 7 s walks back over the removed gap and leaves the first 5 s.
    result = lint_plan(cut, target_duration=5, duration=30)
    assert [(p["op"], p["seconds"]) for p in result["patches"]] == [("trim_end", 5.0)]


def test_caption_coverage_follows_the_finished_clock() -> None:
    speech = [{"start": 0.0, "end": 2.0}, {"start": 8.0, "end": 10.0}]
    cut = edited((0.0, 10.0), removed=[[2.0, 8.0]])
    covered = lint_plan(cut, target_duration=4, duration=20, captions=[{"start": 0.0, "end": 4.0}], speech=speech)
    assert named(covered, "auto: captions cover the speech")["passed"] is True
    missing = lint_plan(cut, target_duration=4, duration=20, captions=[{"start": 0.0, "end": 2.0}], speech=speech)
    check = named(missing, "auto: captions cover the speech")
    assert check["passed"] is False and check["at"] == 2.0


def test_sentence_check_judges_the_cuts_that_really_play() -> None:
    # A pause trim leaves the moment's own start silent; the real cut sits on sentence two.
    speech = [{"start": 4.2, "end": 8.0}]
    trimmed = edited((3.0, 8.0), trim_pauses=True, speech=speech)
    result = lint_plan(trimmed, target_duration=4, duration=20, sentences=SENTENCES)
    assert named(result, "auto: cuts land on sentence boundaries")["passed"] is True
    # A filler removed inside a sentence is a deliberate join, not a story cut.
    inner = edited((0.0, 8.0), removed=[[1.0, 1.4], [5.0, 5.3]])
    assert named(lint_plan(inner, target_duration=7, duration=20, sentences=SENTENCES), "auto: cuts land on sentence boundaries")["passed"] is True
    # A removal that reaches the moment's edge makes a real cut inside a sentence, and
    # moving the moment's own edge could not fix it, so it is reported without a patch.
    outer = edited((0.0, 8.0), removed=[[0.0, 2.0]])
    result = lint_plan(outer, target_duration=6, duration=20, sentences=SENTENCES)
    check = named(result, "auto: cuts land on sentence boundaries")
    assert check["passed"] is False and check["at"] == 0.0 and result["patches"] == []
