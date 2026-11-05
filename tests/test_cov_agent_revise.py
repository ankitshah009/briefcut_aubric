"""Revising a brief from a critique: each failure code's fix, and what is left alone."""

from __future__ import annotations

from vedit.agent.model import Critique, Finding, Intent
from vedit.agent.revise import revise_brief
from vedit.ir import Brief, Canvas


def _brief(*ops: dict, presets: list[str] | None = None) -> Brief:
    return Brief(
        name="t", sources={"main": "/clips/a.mp4"}, ops=list(ops), canvas=Canvas(), presets=presets or ["youtube_1080p"]
    )


def _failing(*codes: str) -> Critique:
    return Critique(
        ok=False, findings=tuple(Finding(code=code, severity="fail", message=code, fix=code) for code in codes)
    )


def _ops(brief: Brief) -> list[str]:
    return [op["op"] for op in brief.ops]


def test_a_passing_critique_or_warnings_only_leave_the_brief_alone() -> None:
    brief = _brief({"op": "export"})
    same, notes = revise_brief(brief, Critique(ok=True, findings=()), Intent())
    assert notes == () and same.ops == brief.ops and same is not brief
    warned = Critique(ok=True, findings=(Finding(code="loudness", severity="warn", message="m", fix="f"),))
    assert revise_brief(brief, warned, Intent())[1] == ()


def test_the_original_brief_is_never_edited_in_place() -> None:
    brief = _brief({"op": "export"})
    revised, notes = revise_brief(brief, _failing("loudness"), Intent())
    assert notes == ("added loudnorm",)
    assert _ops(revised) == ["loudnorm", "export"] and _ops(brief) == ["export"]


def test_a_loudness_failure_adds_one_loudnorm_before_export_only_when_missing() -> None:
    brief = _brief({"op": "cut_silence"}, {"op": "export"})
    revised, notes = revise_brief(brief, _failing("loudness", "loudness"), Intent())
    assert _ops(revised) == ["cut_silence", "loudnorm", "export"] and notes == ("added loudnorm",)
    again, notes = revise_brief(revised, _failing("loudness"), Intent())
    assert _ops(again) == _ops(revised) and notes == ()


def test_a_frame_failure_reframes_resizes_the_canvas_and_retargets_the_presets() -> None:
    brief = _brief({"op": "captions"}, {"op": "export", "presets": ["youtube_1080p"]})
    revised, notes = revise_brief(brief, _failing("frame_size"), Intent(platform="shorts", reframe="9:16"))
    assert notes == ("changed aspect to 9:16",)
    assert _ops(revised) == ["reframe", "captions", "export"], "the reframe goes before the captions it would crop"
    assert revised.ops[0] == {"op": "reframe", "aspect": "9:16", "mode": "fill"}
    assert (revised.canvas.width, revised.canvas.height) == (1080, 1920)
    assert revised.presets == ["shorts_9x16"] and revised.ops[-1]["presets"] == ["shorts_9x16"]


def test_a_frame_failure_without_a_stated_aspect_uses_the_platform_default() -> None:
    square, notes = revise_brief(_brief({"op": "export"}), _failing("frame_size"), Intent(platform="square"))
    assert notes == ("changed aspect to 1:1",) and (square.canvas.width, square.canvas.height) == (1080, 1080)
    assert square.presets == ["square_1x1"]
    wide, _ = revise_brief(
        _brief({"op": "reframe", "aspect": "16:9"}, {"op": "export"}),
        _failing("frame_size"),
        Intent(platform="linkedin"),
    )
    assert _ops(wide) == ["reframe", "export"], "an existing reframe is kept"
    assert (wide.canvas.width, wide.canvas.height) == (1920, 1080) and wide.presets == ["youtube_1080p"]
    podcast, _ = revise_brief(_brief({"op": "export"}), _failing("frame_size"), Intent(platform="podcast"))
    assert podcast.ops[-1]["presets"] == ["podcast_video", "podcast_audio"]


def test_too_long_with_no_cut_adds_a_silence_cut_after_the_tape_op() -> None:
    brief = _brief({"op": "concat", "sources": ["a", "b"]}, {"op": "captions"}, {"op": "export"})
    revised, notes = revise_brief(brief, _failing("duration_over"), Intent(max_duration=30))
    assert notes == ("added silence cut",)
    assert _ops(revised) == ["concat", "cut_silence", "captions", "export"]
    assert revised.ops[1] == {"op": "cut_silence", "threshold_db": -32, "min_silence": 0.3, "pad": 0.08}
    plain, _ = revise_brief(_brief({"op": "export"}), _failing("duration_over"), Intent(max_duration=30))
    assert _ops(plain) == ["cut_silence", "export"], "with no tape op the cut comes first"


def test_too_long_with_a_loose_cut_tightens_it_and_nudges_the_threshold_up_to_the_cap() -> None:
    loose = {"op": "cut_silence", "threshold_db": -34, "min_silence": 0.45, "pad": 0.12}
    revised, notes = revise_brief(
        _brief(dict(loose), {"op": "export"}), _failing("duration_over"), Intent(max_duration=30)
    )
    assert notes == ("tightened silence cut (min_silence 0.25 s, pad 0.08 s, threshold -31 dB)",)
    assert revised.ops[0] == {"op": "cut_silence", "threshold_db": -31.0, "min_silence": 0.25, "pad": 0.08}
    capped, notes = revise_brief(
        _brief(dict(loose, threshold_db=-27), {"op": "export"}), _failing("duration_over"), Intent(max_duration=30)
    )
    assert capped.ops[0]["threshold_db"] == -26.0 and "threshold -26 dB" in notes[0]
    at_cap, notes = revise_brief(
        _brief(dict(loose, threshold_db=-20), {"op": "export"}), _failing("duration_over"), Intent(max_duration=30)
    )
    assert at_cap.ops[0]["threshold_db"] == -20 and notes == ("tightened silence cut (min_silence 0.25 s, pad 0.08 s)",)
    measured, notes = revise_brief(
        _brief({"op": "cut_silence"}, {"op": "export"}), _failing("duration_over"), Intent(max_duration=30)
    )
    assert measured.ops[0] == {"op": "cut_silence", "min_silence": 0.25, "pad": 0.08}, (
        "a recording-relative threshold stays relative"
    )
    assert notes == ("tightened silence cut (min_silence 0.25 s, pad 0.08 s)",)


def test_too_long_with_a_tight_cut_or_a_silent_source_falls_back_to_a_trim() -> None:
    tight = _brief({"op": "cut_silence", "min_silence": 0.2}, {"op": "export"})
    revised, notes = revise_brief(tight, _failing("duration_over"), Intent(max_duration=30))
    assert notes == ("trimmed to max duration",)
    assert revised.ops[1] == {"op": "trim", "start": 0, "end": 30}
    silent, notes = revise_brief(
        _brief({"op": "export"}), _failing("duration_over"), Intent(max_duration=45), has_audio=False
    )
    assert _ops(silent) == ["trim", "export"] and notes == ("trimmed to max duration",)
    again, notes = revise_brief(silent, _failing("duration_over"), Intent(max_duration=45), has_audio=False)
    assert _ops(again) == ["trim", "export"] and notes == ("trimmed to max duration",), (
        "an existing trim is not doubled"
    )
    unlimited, notes = revise_brief(tight, _failing("duration_over"), Intent(max_duration=None))
    assert notes == () and _ops(unlimited) == _ops(tight), "with no limit there is nothing to trim to"


def test_captions_are_styled_for_the_platform_and_other_codes_report_no_fix() -> None:
    boxed, notes = revise_brief(
        _brief({"op": "export"}), _failing("captions_missing", "color_tags"), Intent(platform="shorts")
    )
    assert boxed.ops[0] == {"op": "captions", "burn": True, "style": "boxed"}
    assert notes == ("added captions", "no automatic fix for color_tags")
    lower, _ = revise_brief(_brief({"op": "export"}), _failing("captions_missing"), Intent(platform="youtube"))
    assert lower.ops[0]["style"] == "lower-third"
