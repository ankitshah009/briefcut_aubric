"""Hero word behind the subject: Spark's proposal checked, the edit bounded, SAM over the window only, and the layers rendered."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from vedit import hero_word, meta
from vedit.compile import compile_brief
from vedit.edit_spec import (
    HERO_COLORS,
    EditError,
    catalogue,
    default_edit,
    merge_edit,
    validate_edit,
    validate_hero_word,
)
from vedit.errors import BriefError, CompileError
from vedit.hero_word import HeroWordError, candidate_words, shots, validate_suggestion, window_range
from vedit.ir import Brief, Canvas
from vedit.lint import hero_word_check
from vedit.media import probe, resolve_ffmpeg
from vedit.overlays import hero_font, hero_layout, render_hero_ass, visible_rect
from vedit.pipeline import execute
from vedit.render_plan import build_ops, context
from vedit.timeline import plan_timeline

WORDS = [
    {"start": 0.0, "end": 0.32, "text": "This"},
    {"start": 0.32, "end": 0.63, "text": "is"},
    {"start": 0.63, "end": 0.72, "text": "the"},
    {"start": 0.72, "end": 1.35, "text": "finished"},
    {"start": 1.35, "end": 1.59, "text": "oak"},
    {"start": 1.59, "end": 1.91, "text": "bowl,"},
    {"start": 2.26, "end": 2.45, "text": "oiled"},
    {"start": 2.45, "end": 2.69, "text": "and"},
    {"start": 2.69, "end": 3.08, "text": "ready"},
    {"start": 3.08, "end": 3.32, "text": "for"},
    {"start": 3.32, "end": 3.56, "text": "the"},
    {"start": 3.56, "end": 4.83, "text": "table."},
]
CUTS = [(0.0, 5.0)]


def plan_of(segments: list[dict[str, Any]], words: list[dict[str, Any]] = WORDS, **edit: Any) -> dict[str, Any]:
    plan: dict[str, Any] = {
        "segments": segments,
        "analysis": {
            "transcript": [
                {
                    "start": words[0]["start"],
                    "end": words[-1]["end"],
                    "text": " ".join(word["text"] for word in words),
                    "words": words,
                }
            ],
            "words": words,
        },
    }
    view = plan_timeline(plan)
    plan["edit"] = validate_edit(merge_edit(default_edit(), edit), duration=view["duration"])
    return plan


def proposal(**values: Any) -> dict[str, Any]:
    return {
        "word": "oak bowl",
        "start": 1.35,
        "end": 3.0,
        "side": "center",
        "reason": "The thing being shown.",
        **values,
    }


# --- Spark's answer, checked against what is said ---


def test_candidate_words_come_from_heard_words_and_spread_lines_that_have_none() -> None:
    words = candidate_words(
        [
            {"start": 0, "end": 1, "text": "x", "words": [{"start": 0.1, "end": 0.4, "text": "Hello"}]},
            {"start": 2.0, "end": 3.0, "text": "big wide world"},
            {"start": 5, "end": 4, "text": "backwards"},
            "junk",
        ]
    )
    assert [word["text"] for word in words] == ["Hello", "big", "wide", "world"]
    assert words[1]["estimated"] and not words[0]["estimated"]
    assert words[3]["start"] == pytest.approx(2.667, abs=1e-3)


def test_a_good_proposal_starts_as_the_word_is_said_and_keeps_spark_s_length() -> None:
    checked = validate_suggestion(proposal(), words=WORDS, cuts=CUTS, duration=5.0)
    assert checked["word"] == "oak bowl" and checked["spoken"] == [1.35, 1.91]
    assert checked["start"] == pytest.approx(1.29) and checked["end"] == pytest.approx(2.94)
    assert checked["side"] == "center" and checked["adjusted"] == []


@pytest.mark.parametrize(
    "word, words",
    [
        ("walnut", "does not say"),
        ("the", "no picture"),
        ("oak bowl oiled", "one or two"),
        ("", "empty"),
        (None, "empty"),
    ],
)
def test_a_word_the_speaker_does_not_say_or_cannot_carry_is_refused(word: Any, words: str) -> None:
    with pytest.raises(HeroWordError, match=words):
        validate_suggestion(proposal(word=word), words=WORDS, cuts=CUTS, duration=5.0)


def test_case_and_punctuation_do_not_hide_a_spoken_word() -> None:
    checked = validate_suggestion(proposal(word="TABLE!", start=3.5, end=5.0), words=WORDS, cuts=CUTS, duration=5.0)
    assert checked["word"] == "table" and checked["spoken"] == [3.56, 4.83]


def test_a_window_away_from_the_word_is_moved_to_where_it_is_said() -> None:
    checked = validate_suggestion(proposal(word="ready", start=0.1, end=1.9), words=WORDS, cuts=CUTS, duration=5.0)
    assert checked["start"] == pytest.approx(2.63) and checked["end"] == pytest.approx(4.43)
    assert any("said at 2.69" in note for note in checked["adjusted"])


def test_too_long_is_cut_to_three_seconds_and_too_short_grows_to_one() -> None:
    long = validate_suggestion(proposal(word="finished", start=0.72, end=9.0), words=WORDS, cuts=CUTS, duration=5.0)
    assert long["end"] - long["start"] == pytest.approx(3.0) and any("at most 3" in note for note in long["adjusted"])
    short = validate_suggestion(proposal(word="finished", start=0.72, end=0.9), words=WORDS, cuts=CUTS, duration=5.0)
    assert short["end"] - short["start"] == pytest.approx(1.0) and any(
        "at least 1" in note for note in short["adjusted"]
    )


def test_the_window_never_leaves_its_shot_and_a_word_too_near_a_cut_is_refused() -> None:
    cuts = [(0.0, 3.3), (3.3, 5.0)]
    ready = validate_suggestion(proposal(word="ready", start=2.69, end=5.0), words=WORDS, cuts=cuts, duration=5.0)
    assert ready["end"] == pytest.approx(3.3) and any("shot ends" in note for note in ready["adjusted"])
    with pytest.raises(HeroWordError, match="too close to a cut"):
        validate_suggestion(
            proposal(word="oiled", start=2.26, end=4.0), words=WORDS, cuts=[(0.0, 2.6), (2.6, 5.0)], duration=5.0
        )
    with pytest.raises(HeroWordError, match="across a cut"):
        validate_suggestion(proposal(word="oiled"), words=WORDS, cuts=[(0.0, 2.0), (2.5, 5.0)], duration=5.0)


def test_a_bad_side_or_window_falls_back_and_says_so() -> None:
    checked = validate_suggestion(
        proposal(side="diagonal", start="soon", end=None), words=WORDS, cuts=CUTS, duration=5.0
    )
    assert checked["side"] == "center" and checked["end"] - checked["start"] == pytest.approx(hero_word.DEFAULT_SECONDS)
    assert len(checked["adjusted"]) == 2


def test_shots_leave_out_the_part_a_transition_shares() -> None:
    plan = plan_of([{"start": 0, "end": 3}, {"start": 10, "end": 13}], transition={"type": "fade", "seconds": 0.5})
    assert shots(plan_timeline(plan)) == [(0.0, 2.5), (3.0, 5.5)]


# --- edit.hero_word, bounded ---

GOOD = {
    "word": "Oak bowl",
    "start": 1.3,
    "end": 2.9,
    "subject": "the wooden bowl",
    "side": "left",
    "font": "condensed",
    "color": "#FFD60A",
}


def test_a_valid_hero_word_is_kept_whole_and_the_default_has_none() -> None:
    assert default_edit()["hero_word"] is None and validate_edit({}, duration=5)["hero_word"] is None
    clean = validate_edit({"hero_word": GOOD}, duration=5.0)["hero_word"]
    assert clean == dict(GOOD, reason="")
    assert catalogue()["hero_word"]["colors"] == list(HERO_COLORS)


@pytest.mark.parametrize(
    "change, words",
    [
        ({"end": 4.5}, "0.5 to 3 seconds"),
        ({"end": 1.5}, "0.5 to 3 seconds"),
        ({"end": 5.5}, "between"),
        ({"start": -1}, "between"),
        ({"word": "a" * 19}, "at most 18"),
        ({"word": "one two three"}, "at most 2 words"),
        ({"word": "{\\b1}x"}, "only letters"),
        ({"word": "hi 🎉"}, "only letters"),
        ({"word": "  "}, "empty"),
        ({"color": "#123456"}, "not one of"),
        ({"color": "red"}, "color like"),
        ({"font": "Comic Sans"}, "not one of"),
        ({"side": "middle"}, "not one of"),
        ({"subject": ""}, "Name the subject"),
        ({"start": True}, "number"),
    ],
)
def test_out_of_bounds_hero_words_are_refused(change: dict[str, Any], words: str) -> None:
    with pytest.raises(EditError, match=words):
        validate_hero_word(dict(GOOD, **change), duration=5.0)


def test_build_hero_word_edit_refuses_a_window_across_a_cut() -> None:
    plan = plan_of([{"start": 0, "end": 2}, {"start": 2.2, "end": 5}])
    with pytest.raises(HeroWordError, match="inside one shot"):
        hero_word.build_hero_word_edit(plan, {"word": "oak", "start": 1.5, "end": 2.5}, subject="the bowl")
    built = hero_word.build_hero_word_edit(
        plan, {"word": "oak", "start": 0.5, "end": 1.9, "side": "top"}, subject="the bowl", color="#FF453A"
    )
    assert built["side"] == "top" and built["color"] == "#FF453A"


# --- Spark, through meta's own request path ---


class Http:
    """Meta's boundary: Spark answers ``answer``; SAM answers ``sam`` as real SAM text."""

    def __init__(self, answer: Any = None, sam: str = "", frames: int = 50) -> None:
        self.answer, self.sam, self.frames, self.calls = answer, sam, frames, []

    def __call__(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((method, path, kwargs))
        if method == "POST" and path == "/responses/input_tokens":
            return {"input_tokens": 1800}
        if method == "POST" and path == "/files":
            return {"id": "file-abc123", "status": "uploaded"}
        if method == "DELETE":
            return {"id": "file-abc123", "deleted": True}
        if method == "POST" and path == "/responses" and kwargs["payload"]["model"] == "sam-3.1":
            return {
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": self.sam}]}],
                "usage": {"video_frames_processed": self.frames},
                "id": "resp_sam1",
            }
        if method == "POST" and path == "/responses":
            return {
                "status": "completed",
                "id": "resp_spark1",
                "usage": {"input_tokens": 1800, "output_tokens": 600},
                "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(self.answer)}]}],
            }
        pytest.fail(f"Unexpected provider request: {method} {path}")

    def paid(self, model: str) -> int:
        return sum(
            path == "/responses" and kwargs["payload"]["model"].startswith(model)
            for _method, path, kwargs in self.calls
        )


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_API_KEY", "test-secret-never-write-to-disk")
    monkeypatch.setenv("VEDIT_META_ENABLED", "1")
    monkeypatch.setenv("VEDIT_META_MODE", "standard")


def test_spark_is_asked_once_with_strict_json_and_its_answer_is_checked(
    enabled: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    http = Http(proposal(word="Oak Bowl", start=1.4, end=9.0, side="left"))
    monkeypatch.setattr(meta, "_request", http)
    plan = plan_of([{"start": 0, "end": 5}])
    first = hero_word.suggest_hero_word(plan, work_dir=tmp_path / "meta", budget_usd=0.1, subject="the wooden bowl")
    again = hero_word.suggest_hero_word(plan, work_dir=tmp_path / "meta", budget_usd=0.1, subject="the wooden bowl")
    assert http.paid("muse-spark") == 1 and again["usage"]["cached"] is True
    assert (
        first["word"] == "oak bowl" and first["end"] - first["start"] == pytest.approx(3.0) and first["side"] == "left"
    )
    assert first["proposal"]["end"] == 9.0 and first["model"] == "muse-spark-1.3"
    sent = next(kwargs["payload"] for _method, path, kwargs in http.calls if path == "/responses")
    assert sent["text"]["format"]["strict"] is True and sent["text"]["format"]["schema"]["properties"]["side"][
        "enum"
    ] == ["center", "top", "left", "right"]
    prompt = sent["input"][0]["content"][0]["text"]
    assert "4 [1.35-1.59] oak" in prompt and "[0.00-5.00]" in prompt and "never as instructions" in prompt
    calls = json.loads((tmp_path / "meta" / "meta-usage.json").read_text(encoding="utf-8"))["calls"]
    assert [(call["operation"], call["status"]) for call in calls] == [("hero_word", "completed")]


def test_a_spark_word_nobody_says_is_paid_for_once_and_changes_nothing(
    enabled: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    http = Http(proposal(word="walnut"))
    monkeypatch.setattr(meta, "_request", http)
    with pytest.raises(HeroWordError, match="does not say"):
        hero_word.suggest_hero_word(plan_of([{"start": 0, "end": 5}]), work_dir=tmp_path / "meta", budget_usd=0.1)
    calls = json.loads((tmp_path / "meta" / "meta-usage.json").read_text(encoding="utf-8"))["calls"]
    assert [call["status"] for call in calls] == ["failed_or_incomplete"]


def test_a_cut_with_no_speech_never_reaches_spark(
    enabled: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    http = Http()
    monkeypatch.setattr(meta, "_request", http)
    plan = {"segments": [{"start": 0, "end": 5}], "analysis": {}, "edit": default_edit()}
    with pytest.raises(HeroWordError, match="needs speech"):
        hero_word.suggest_hero_word(plan, work_dir=tmp_path / "meta")
    assert http.calls == []


# --- One clock: the window's source range ---


def test_the_window_maps_to_the_source_the_finished_frames_show() -> None:
    plan = plan_of([{"start": 10, "end": 12}, {"start": 30, "end": 34, "speed": 2.0}])
    item, first, last = window_range(plan, 2.5, 3.5)
    assert (first, last) == (75, 105)
    # Moment 2 starts at frame 60 = 2.0 s; frame 75 is 0.5 s in at double speed, so 1 s of its source.
    assert item == {"start": 31.0, "end": 33.0, "speed": 2.0}
    with pytest.raises(HeroWordError, match="inside one shot"):
        window_range(plan, 1.5, 2.5)


# --- The side where the word still reads, from SAM's mask ---


def test_spark_s_side_stands_while_the_word_reads_and_is_overruled_when_the_subject_buries_it() -> None:
    assert hero_word.choose_side({"center": 0.4, "top": 0.2, "left": 0.0, "right": 0.1}, "center") == ("center", "")
    assert hero_word.choose_side({"center": 0.0, "top": 0.2, "left": 0.0, "right": 0.1}, "left") == ("left", "")
    side, note = hero_word.choose_side({"center": 0.82, "top": 0.19, "left": 0.54, "right": 0.59}, "center")
    assert side == "top" and "covers 82% of the word" in note and "19% behind" in note
    # Nothing reads well: the least buried side wins.
    assert hero_word.choose_side({"center": 1.0, "top": 0.9, "left": 0.7, "right": 0.95}, "center")[0] == "left"


def test_top_sets_the_word_on_the_subject_s_edge_and_keeps_its_capitals_on_the_picture() -> None:
    _family, face, _path = hero_font("heavy")
    bowl = (0.27, 0.23, 0.76, 0.78)
    for aspect in ("16:9", "9:16"):
        visible = visible_rect(1280, 720, aspect)
        layout = hero_layout("oak", side="top", width=1280, height=720, visible=visible, face=face, subject=bowl)
        edge = bowl[1] * 720
        assert layout["y"] - 0.35 * layout["height"] >= visible[1] + 0.06 * visible[3] - 1
        # The subject's edge crosses the capitals in their lower part, so it stands in front of them.
        assert layout["y"] < edge < layout["y"] + 0.35 * layout["height"]
    assert validate_hero_word(dict(GOOD, subject_box=[0.27, 0.23, 0.76, 0.78]), duration=5.0)["subject_box"] == [
        0.27,
        0.23,
        0.76,
        0.78,
    ]
    for bad in ([0.5, 0.2, 0.4, 0.8], [0, 0, 1], [0, 0, 1, 1.5], "box"):
        with pytest.raises(EditError, match="subject box"):
            validate_hero_word(dict(GOOD, subject_box=bad), duration=5.0)


# --- Layout: big, and inside what survives the reframe ---


@pytest.mark.parametrize("aspect", ["16:9", "9:16", "1:1"])
@pytest.mark.parametrize("side", ["center", "top", "left", "right"])
@pytest.mark.parametrize("word", ["oak", "finished", "craftsmanship"])
def test_the_word_fills_its_side_and_never_leaves_the_visible_picture(aspect: str, side: str, word: str) -> None:
    _family, face, _path = hero_font("heavy")
    visible = visible_rect(1280, 720, aspect)
    layout = hero_layout(word, side=side, width=1280, height=720, visible=visible, face=face)
    left, top, width, height = visible
    assert left <= layout["x"] - layout["width"] / 2 and layout["x"] + layout["width"] / 2 <= left + width
    assert top < layout["y"] < top + height
    share = {"center": 0.9, "top": 0.9, "left": 0.54, "right": 0.54}[side]
    # As wide as the side allows, unless the height cap stops a short word first.
    assert layout["width"] >= 0.95 * share * width or layout["height"] >= 0.41 * height


def test_the_text_file_is_literal_and_timed_to_the_window() -> None:
    family, face, _path = hero_font("heavy")
    text, layout = render_hero_ass(
        "Oak", start=1.483, end=2.983, side="center", color="#FFD60A", family=family, face=face, width=1280, height=720
    )
    event = next(line for line in text.splitlines() if line.startswith("Dialogue:"))
    assert event.startswith("Dialogue: 0,0:00:01.48,0:00:02.98,Hero,") and event.endswith("}OAK")
    assert f"\\pos({layout['x']},{layout['y']})" in event and "\\1c&H0AD6FF&" in event
    sneaky, _ = render_hero_ass(
        "a}{\\b0", start=0, end=1, side="center", color="#FFFFFF", family=family, face=face, width=640, height=360
    )
    assert next(line for line in sneaky.splitlines() if line.startswith("Dialogue:")).endswith("}A\\}\\{\\⁠B0")


# --- The compile op ---


def ffmpeg(*args: str) -> None:
    subprocess.run([resolve_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def planes(path: Path, *, crop: str | None = None) -> list[bytes]:
    """Every frame's raw Y'CbCr bytes, so a color tag cannot hide or fake a change."""
    chain = (f"crop={crop}," if crop else "") + "format=yuv444p"
    run = subprocess.run(
        [resolve_ffmpeg(), "-hide_banner", "-loglevel", "error", "-i", str(path), "-vf", chain, "-f", "rawvideo", "-"],
        check=True,
        capture_output=True,
    )
    width, height = (int(part) for part in crop.split(":")[:2]) if crop else (probe(path).width, probe(path).height)
    size = width * height * 3
    return [run.stdout[at : at + size] for at in range(0, len(run.stdout), size)]


def difference(a: bytes, b: bytes) -> float:
    return sum(abs(x - y) for x, y in zip(a, b)) / max(1, len(a))


@pytest.fixture
def scene(tmp_path: Path) -> tuple[Path, Path]:
    """A teal field, a red block standing in the middle, a tone; and a matte of the block for 1.5 s only."""
    clip = tmp_path / "scene.mp4"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "color=0x208080:s=320x180:r=30:d=3",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=3",
        "-vf",
        "drawbox=x=130:y=30:w=60:h=150:color=0xD03020:t=fill,format=yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "6",
        "-c:a",
        "aac",
        "-shortest",
        str(clip),
    )
    matte = tmp_path / "matte.mkv"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "color=black:s=160x90:r=30:d=1.5",
        "-vf",
        "drawbox=x=65:y=15:w=30:h=75:color=white:t=fill,format=gray",
        "-c:v",
        "ffv1",
        "-pix_fmt",
        "gray",
        str(matte),
    )
    return clip, matte


def hero_brief(tmp_path: Path, clip: Path, **op: Any) -> Brief:
    work = tmp_path / "work"
    hero = {
        "op": "hero_word",
        "word": "Oak",
        "start": 1.0,
        "end": 2.5,
        "side": "center",
        "color": "#FFD60A",
        "font": "heavy",
        **op,
    }
    return Brief(
        name="hero",
        sources={"footage": str(clip)},
        ops=[
            {"op": "select_ranges", "ranges": [{"start": 0, "end": 3}]},
            hero,
            {"op": "export", "preset": "web_preview", "path": str(work / "video.mp4")},
        ],
        canvas=Canvas(width=320, height=180, fps=30),
        hw=False,
        work_dir=str(work),
    )


@pytest.mark.render
def test_the_word_shows_only_in_its_window_and_only_where_the_subject_is_not(
    tmp_path: Path, scene: tuple[Path, Path]
) -> None:
    clip, matte = scene
    compiler = compile_brief(hero_brief(tmp_path, clip, matte=str(matte)), work_dir=tmp_path / "work")
    assert [step.name for step in compiler.pipeline.steps][:2] == ["select_ranges", "hero_word"]
    assert all(step.ok for step in execute(compiler.pipeline, progress=False))
    work = tmp_path / "work"
    tape, layered = next(work.glob("*select_ranges.mp4")), next(work.glob("*hero_word.mp4"))
    info = probe(layered)
    assert (info.width, info.height, info.has_audio, info.has_video) == (320, 180, True, True)
    before, after = planes(tape), planes(layered)
    assert len(before) == len(after) == 90
    # Frames 30..74 are the window. Everywhere else the tape passes through untouched.
    outside = [index for index in range(90) if not 30 <= index < 75]
    assert max(difference(before[index], after[index]) for index in outside) < 0.5
    # How much the word changes a frame depends on the font's weight: 6.6 in Arial Black on a Mac,
    # 5.8 in DejaVu Sans Bold and 4.6 in Noto Sans Bold on Linux. Every frame of the window shows it.
    assert min(difference(before[index], after[index]) for index in range(36, 70)) > 3
    # Inside the block, which the matte covers, the word is hidden: those pixels are the tape's own.
    subject = "40:120:140:40"
    hidden_before, hidden_after = planes(tape, crop=subject), planes(layered, crop=subject)
    assert max(difference(hidden_before[index], hidden_after[index]) for index in range(90)) < 1.0
    # Beside the block the word is drawn in yellow.
    word = "100:40:20:70"
    shown_before, shown_after = planes(tape, crop=word), planes(layered, crop=word)
    # 29.8 in Arial Black, 23.0 in DejaVu Sans Bold, 14.9 in Noto Sans Bold.
    assert difference(shown_before[50], shown_after[50]) > 10
    assert (
        compiler.pipeline.meta["hero_words"][0]["first_frame"] == 30
        and compiler.pipeline.meta["hero_words"][0]["last_frame"] == 75
    )


@pytest.mark.render
@pytest.mark.parametrize(
    "op, words",
    [
        ({"side": "middle"}, "side must be"),
        ({"color": "#123456"}, "color must be"),
        ({"font": "papyrus"}, "font must be"),
        ({"end": 4.5}, "0.5 to 3 seconds"),
        ({"start": 2.0, "end": 4.0}, "after the"),
        ({"word": "{\\an7}x"}, "letters"),
        ({"word": "one two three"}, "at most 2 words"),
        ({"aspect": "3:2"}, "unknown hero word aspect"),
        ({"focus": [2, 0]}, "focus"),
        ({"matte": None}, "requires a matte"),
        ({"matte": "MISSING"}, "unknown source|does not exist"),
        ({"subject_box": [0.6, 0.1, 0.4, 0.9]}, "subject_box"),
        ({"subject_box": [0, 0, 1]}, "subject_box"),
    ],
)
def test_a_bad_hero_word_op_fails_at_compile_time(
    tmp_path: Path, scene: tuple[Path, Path], op: dict[str, Any], words: str
) -> None:
    clip, matte = scene
    if op.get("matte") == "MISSING":
        op = {"matte": str(tmp_path / "missing.mkv")}
    with pytest.raises((CompileError, BriefError), match=words):
        compile_brief(hero_brief(tmp_path, clip, **{"matte": str(matte), **op}), work_dir=tmp_path / "work")


# --- The render order ---


def test_the_hero_word_goes_on_the_source_picture_before_the_reframe(tmp_path: Path) -> None:
    hero = {
        "word": "oak",
        "start": 1.3,
        "end": 2.9,
        "subject": "the bowl",
        "side": "center",
        "subject_box": [0.3, 0.2, 0.7, 0.9],
    }
    plan = plan_of([{"start": 0, "end": 5}], hero_word=hero, cutout={"mode": "dim", "subject": "the bowl"})
    ctx = context(
        plan,
        request={},
        aspect="9:16",
        canvas=Canvas(width=1080, height=1920, fps=30),
        work=tmp_path,
        has_audio=True,
        matte=tmp_path / "cut.mkv",
        hero_matte=tmp_path / "hero.mkv",
        tracking=None,
    )
    names = [op["op"] for op in build_ops(ctx)]
    assert names[:4] == ["select_ranges", "cutout", "hero_word", "reframe"]
    op = next(op for op in build_ops(ctx) if op["op"] == "hero_word")
    assert (
        op["aspect"] == "9:16"
        and op["matte"].endswith("hero.mkv")
        and "focus" not in op
        and op["subject_box"] == [0.3, 0.2, 0.7, 0.9]
    )
    tracked = context(
        plan,
        request={},
        aspect="9:16",
        canvas=Canvas(width=1080, height=1920, fps=30),
        work=tmp_path,
        has_audio=True,
        hero_matte=tmp_path / "hero.mkv",
        tracking=[{"time": 2.0, "x": 0.6, "y": 0.2, "width": 0.2, "height": 0.4}],
    )
    ops = build_ops(tracked)
    assert [op["op"] for op in ops][:3] == ["select_ranges", "hero_word", "track_reframe"]
    assert next(op for op in ops if op["op"] == "hero_word")["focus"] == [0.7, 0.4]


def test_without_its_matte_or_inside_one_shot_the_word_is_left_out_with_a_warning(tmp_path: Path) -> None:
    plan = plan_of([{"start": 0, "end": 5}], hero_word={"word": "oak", "start": 1.3, "end": 2.9, "subject": "the bowl"})
    ctx = context(
        plan, request={}, aspect="16:9", canvas=Canvas(width=1920, height=1080, fps=30), work=tmp_path, has_audio=True
    )
    assert "hero_word" not in [op["op"] for op in build_ops(ctx)] and "subject mask" in ctx.warnings[0]
    split = plan_of(
        [{"start": 0, "end": 2}, {"start": 3, "end": 6}],
        hero_word={"word": "oak", "start": 1.3, "end": 2.9, "subject": "the bowl"},
    )
    ctx = context(
        split,
        request={},
        aspect="16:9",
        canvas=Canvas(width=1920, height=1080, fps=30),
        work=tmp_path,
        has_audio=True,
        hero_matte=tmp_path / "m.mkv",
    )
    assert "hero_word" not in [op["op"] for op in build_ops(ctx)] and "one shot" in ctx.warnings[0]


# --- SAM over the window only, end to end with Meta stubbed ---


@pytest.mark.render
def test_sam_reads_only_the_window_and_its_matte_has_one_frame_per_window_frame(
    enabled: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scene: tuple[Path, Path]
) -> None:
    pytest.importorskip("meta_sam_parser")
    from meta_sam_parser._mask_codec import _encode_raster

    clip, _ = scene

    def line(frame: int) -> str:
        # The block, in the 960x540 proxy: x 390..570, y 90..540.
        payload = _encode_raster(bytes([1]) * (180 * 450), 180, 450)
        return f"<{frame}f>0<|box;x1=390;y1=90;x2=569;y2=539;w=960;h=540;c=0.9|><|mask;x=0;y=0;c=0.9;data=450,180,{payload}|>"

    http = Http(sam="\n".join(line(frame) for frame in range(45)), frames=45)
    monkeypatch.setattr(meta, "_request", http)
    plan = plan_of([{"start": 0, "end": 3}])
    hero = {"word": "oak", "start": 1.0, "end": 2.5, "subject": "the red block", "side": "left", "font": "heavy"}
    made = hero_word.window_matte(clip, plan, hero, work_dir=tmp_path / "w", budget_usd=0.05, aspect="16:9")
    assert http.paid("sam-3.1") == 1
    assert probe(made["proxy"]).duration == pytest.approx(1.5, abs=0.04)
    stats = made["stats"]
    assert stats["frames"] == stats["window_frames"] == 45 and stats["coverage"] == 1.0
    assert (
        made["side"] == "left"
        and 0.0 < stats["hidden"] <= 0.55
        and set(stats["hidden_by_side"]) == {"center", "top", "left", "right"}
    )
    # The block stands in the middle, so a centred word would be mostly hidden; the box SAM drew is kept for the layout.
    assert stats["hidden_by_side"]["center"] > stats["hidden"]
    assert made["subject_box"] == pytest.approx([390 / 960, 90 / 540, 570 / 960, 1.0], abs=1e-3)
    calls = json.loads((tmp_path / "w" / "meta" / "meta-usage.json").read_text(encoding="utf-8"))["calls"]
    # SAM bills a 50-frame minimum: 45 frames cost what 50 do.
    assert [(call["operation"], call["usage"]["cost_usd"]) for call in calls] == [("tracking", 0.01)]
    check = hero_word_check(dict(hero, spoken=[1.06, 1.5]), stats, duration=3.0)
    assert check["passed"] is True and check["inputs"] == ["sam-3.1", "muse-spark"]


# --- The receipt ---


def test_the_receipt_passes_a_readable_word_behind_the_subject_and_explains_a_poor_one() -> None:
    hero = {"word": "oak", "start": 1.0, "end": 2.5, "spoken": [1.06, 1.5]}
    good = hero_word_check(hero, {"frames": 45, "window_frames": 45, "coverage": 0.98, "hidden": 0.3}, duration=5.0)
    assert (
        good["passed"] is True
        and good["basis"] == "derived"
        and good["by"] == "lint"
        and good["inputs"] == ["sam-3.1", "muse-spark"]
    )
    assert good["at"] == 1.0 and "covers 30% of the word" in good["note"]
    assert (
        hero_word_check(hero, {"frames": 45, "window_frames": 45, "coverage": 0.5, "hidden": 0.3}, duration=5.0)[
            "passed"
        ]
        is False
    )
    buried = hero_word_check(hero, {"frames": 45, "window_frames": 45, "coverage": 1.0, "hidden": 0.9}, duration=5.0)
    assert buried["passed"] is False and "too much to read" in buried["note"]
    assert (
        hero_word_check(hero, {"frames": 40, "window_frames": 45, "coverage": 1.0, "hidden": 0.2}, duration=5.0)[
            "passed"
        ]
        is False
    )
    late = hero_word_check(
        dict(hero, spoken=[2.2, 2.4]), {"frames": 45, "window_frames": 45, "coverage": 1.0, "hidden": 0.2}, duration=5.0
    )
    assert late["passed"] is False and "not as the window opens" in late["note"]
    assert hero_word_check(dict(hero, end=4.5), {"frames": 45, "coverage": 1.0}, duration=5.0)["passed"] is False
    assert hero_word_check(hero, None, duration=5.0)["passed"] is False
    clear = hero_word_check(hero, {"frames": 45, "window_frames": 45, "coverage": 1.0, "hidden": 0.0}, duration=5.0)
    assert clear["passed"] is True and "not behind anything" in clear["note"]
    assert hero_word_check(None, None, duration=5.0) == {
        "name": "auto: hero word sits behind the subject",
        "passed": True,
        "at": None,
        "note": "not requested",
        "basis": "derived",
        "by": "lint",
    }
