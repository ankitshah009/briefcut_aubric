"""Hero word and verified find on footage that is not the oak bowl: people, shapes, scripts, backgrounds and edits after the fact.

Every paid call is stubbed. SAM's masks are replaced by a matte drawn here, so
these tests measure Briefcut's own decisions (window, side, colour, phrase)
on real decoded pixels, not the SAM wire format.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from vedit import hero_word, matte, meta
from vedit.edit_spec import HERO_COLORS, default_edit, merge_edit, validate_edit
from vedit.hero_word import HeroWordError, candidate_words, validate_suggestion
from vedit.lint import HERO_CONTRAST, hero_word_check
from vedit.media import probe, resolve_ffmpeg
from vedit.timeline import plan_timeline
from vedit.verify_find import named_subject, query_subject, verify_moments

WORDS = [
    {"start": 0.0, "end": 0.32, "text": "This"}, {"start": 0.32, "end": 0.63, "text": "is"}, {"start": 0.63, "end": 0.72, "text": "the"},
    {"start": 0.72, "end": 1.35, "text": "finished"}, {"start": 1.35, "end": 1.59, "text": "oak"}, {"start": 1.59, "end": 1.91, "text": "bowl,"},
    {"start": 2.26, "end": 2.45, "text": "oiled"}, {"start": 2.45, "end": 2.69, "text": "and"}, {"start": 2.69, "end": 3.08, "text": "ready"},
    {"start": 3.08, "end": 3.32, "text": "for"}, {"start": 3.32, "end": 3.56, "text": "the"}, {"start": 3.56, "end": 4.83, "text": "table."},
]
CUTS = [(0.0, 5.0)]


def plan_of(segments: list[dict[str, Any]], words: list[dict[str, Any]] = WORDS, **edit: Any) -> dict[str, Any]:
    analysis: dict[str, Any] = {}
    if words:
        analysis = {"transcript": [{"start": words[0]["start"], "end": words[-1]["end"], "text": " ".join(word["text"] for word in words),
                                    "words": words}], "words": words}
    plan: dict[str, Any] = {"segments": segments, "analysis": analysis}
    plan["edit"] = validate_edit(merge_edit(default_edit(), edit), duration=plan_timeline(plan)["duration"])
    return plan


def said(*pieces: str, start: float = 0.0, step: float = 0.4) -> list[dict[str, Any]]:
    return [{"start": round(start + index * step, 3), "end": round(start + (index + 1) * step, 3), "text": text} for index, text in enumerate(pieces)]


class Refuse:
    """Meta's boundary when nothing may be sent."""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(args)
        pytest.fail(f"Nothing should reach Meta here: {args[:2]}")


class Spark:
    """Muse Spark answering ``answer`` to any structured call."""

    def __init__(self, answer: Any) -> None:
        self.answer, self.calls = answer, []

    def __call__(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((method, path))
        if path == "/responses/input_tokens":
            return {"input_tokens": 900}
        if method == "POST" and path == "/responses":
            return {"status": "completed", "id": "resp_spark", "usage": {"input_tokens": 900, "output_tokens": 300},
                    "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(self.answer)}]}]}
        pytest.fail(f"Unexpected provider request: {method} {path}")


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_API_KEY", "test-secret-never-write-to-disk")
    monkeypatch.setenv("VEDIT_META_ENABLED", "1")
    monkeypatch.setenv("VEDIT_META_MODE", "standard")


# --- Hero word: what is said ------------------------------------------------------------------

def test_a_cut_of_only_fillers_is_refused_before_anything_is_sent(enabled: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gate = Refuse()
    monkeypatch.setattr(meta, "_request", gate)
    plan = plan_of([{"start": 0, "end": 3}], said("um", "uh,", "like", "yeah", "so", "okay"))
    with pytest.raises(HeroWordError, match="Only filler words.*Nothing was sent"):
        hero_word.suggest_hero_word(plan, work_dir=tmp_path / "meta")
    assert gate.calls == [] and not (tmp_path / "meta").exists()


def test_no_speech_at_all_is_refused_and_never_invents_a_word(enabled: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gate = Refuse()
    monkeypatch.setattr(meta, "_request", gate)
    with pytest.raises(HeroWordError, match="needs speech.*Nothing was sent"):
        hero_word.suggest_hero_word(plan_of([{"start": 0, "end": 3}], []), work_dir=tmp_path / "meta")
    assert gate.calls == []


def test_spark_may_decline_and_an_empty_word_changes_nothing(enabled: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spark = Spark({"word": "", "start": 0, "end": 0, "side": "center", "reason": "Nothing stands out."})
    monkeypatch.setattr(meta, "_request", spark)
    with pytest.raises(HeroWordError, match="no spoken word worth showing"):
        hero_word.suggest_hero_word(plan_of([{"start": 0, "end": 5}]), work_dir=tmp_path / "meta", budget_usd=0.2)
    assert spark.calls.count(("POST", "/responses")) == 1


def test_the_prompt_states_the_length_limit_and_allows_no_word(enabled: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []

    def spark(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        if path == "/responses/input_tokens":
            return {"input_tokens": 900}
        sent.append(kwargs["payload"]["input"][0]["content"][0]["text"])
        answer = {"word": "oak", "start": 1.35, "end": 3.0, "side": "center", "reason": "The material."}
        return {"status": "completed", "id": "r", "usage": {"input_tokens": 900, "output_tokens": 50},
                "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(answer)}]}]}

    monkeypatch.setattr(meta, "_request", spark)
    hero_word.suggest_hero_word(plan_of([{"start": 0, "end": 5}]), work_dir=tmp_path / "meta", budget_usd=0.2, aspect="1:1", subject="the person")
    assert "at most 18 characters" in sent[0] and "return an empty word" in sent[0] and "never invent" in sent[0]
    assert "delivered 1:1" in sent[0] and "Subject: the person" in sent[0]


@pytest.mark.parametrize("pieces, proposed, shown", [
    (["我们", "去", "东", "京", "吧"], "东京", "东京"),  # a recognizer that split one word into characters
    (["我们", "去", "东京", "吧"], "东京", "东京"),
    (["今日", "は", "富士山", "です"], "富士山", "富士山"),
    (["오늘", "서울", "날씨"], "서울", "서울"),  # Korean is written with spaces
])
def test_words_in_scripts_without_spaces_are_found_and_shown_whole(pieces: list[str], proposed: str, shown: str) -> None:
    words = candidate_words([{"start": 0, "end": 2, "text": "".join(pieces), "words": said(*pieces)}])
    checked = validate_suggestion({"word": proposed, "start": 0.4, "end": 2.0, "side": "center", "reason": ""}, words=words, cuts=CUTS, duration=5.0)
    assert checked["word"] == shown and checked["start"] <= checked["spoken"][0]


def test_a_word_a_hero_word_cannot_draw_is_refused_with_a_reason_not_a_crash() -> None:
    # A symbol inside the word has no place in a hero word's letters: refused with a reason.
    words = candidate_words([{"start": 0, "end": 2, "text": "R&D matters", "words": said("R&D", "matters")}])
    with pytest.raises(HeroWordError, match="cannot show"):
        validate_suggestion({"word": "R&D", "start": 0.0, "end": 1.5}, words=words, cuts=CUTS, duration=5.0)


def test_a_hindi_hero_word_is_drawn_whole() -> None:
    # Devanagari vowel signs are combining marks; libass shapes them with a font that covers the script.
    words = candidate_words([{"start": 0, "end": 2, "text": "नमस्ते दोस्तों", "words": said("नमस्ते", "दोस्तों")}])
    checked = validate_suggestion({"word": "नमस्ते", "start": 0.0, "end": 1.5}, words=words, cuts=CUTS, duration=5.0)
    assert checked["word"] == "नमस्ते"


def test_a_word_too_long_for_the_screen_is_refused() -> None:
    words = said("internationalization", "matters")
    with pytest.raises(HeroWordError, match="too long"):
        validate_suggestion({"word": "internationalization", "start": 0.0, "end": 1.5}, words=words, cuts=CUTS, duration=5.0)


def test_a_word_said_at_the_very_start_or_near_the_end_of_the_cut_stays_inside_it() -> None:
    opening = [{"start": 0.0, "end": 0.4, "text": "Oak!"}] + WORDS[1:]
    first = validate_suggestion({"word": "oak", "start": 0.0, "end": 2.0, "side": "top"}, words=opening, cuts=CUTS, duration=5.0)
    assert first["word"] == "Oak!" and first["start"] == 0.0 and first["end"] == pytest.approx(2.0)
    words = WORDS[:-1] + [{"start": 4.3, "end": 4.8, "text": "table."}]
    late = validate_suggestion({"word": "table", "start": 4.3, "end": 6.0}, words=words, cuts=CUTS, duration=5.0)
    assert late["start"] == pytest.approx(4.24) and late["end"] == 5.0 and any("shot ends" in note for note in late["adjusted"])
    too_late = WORDS[:-1] + [{"start": 4.7, "end": 4.95, "text": "table."}]
    with pytest.raises(HeroWordError, match="too close to a cut"):
        validate_suggestion({"word": "table", "start": 4.7, "end": 5.0}, words=too_late, cuts=CUTS, duration=5.0)


# --- Hero word: a cut re-trimmed after the word was set -----------------------------------------

def test_the_window_is_fresh_while_the_word_is_said_as_it_opens() -> None:
    plan = plan_of([{"start": 0, "end": 5}])
    status = hero_word.window_status(plan, {"word": "oak", "start": 1.29, "end": 3.29})
    assert status == {"status": "fresh", "spoken": [1.35, 1.59]}


def test_a_re_trimmed_cut_is_detected_and_the_window_can_follow_the_word() -> None:
    # The first second was trimmed off: "oak" is now said at 0.35 s, but the window still opens at 1.29 s.
    plan = plan_of([{"start": 1.0, "end": 5.0}])
    status = hero_word.window_status(plan, {"word": "oak", "start": 1.29, "end": 3.29, "side": "top"})
    assert status["status"] == "moved" and "now said at 0.35 s" in status["note"]
    assert status["start"] == pytest.approx(0.29) and status["end"] == pytest.approx(2.29) and status["spoken"][0] == pytest.approx(0.35)


def test_a_word_trimmed_out_of_the_cut_is_reported_and_no_words_means_unknown() -> None:
    gone = hero_word.window_status(plan_of([{"start": 2.0, "end": 5.0}]), {"word": "oak", "start": 1.29, "end": 2.5})
    assert gone["status"] == "unspoken" and "not said in this cut" in gone["note"]
    silent = hero_word.window_status(plan_of([{"start": 0, "end": 5}], []), {"word": "oak", "start": 1.29, "end": 2.5})
    assert silent["status"] == "unknown"


def test_a_stale_window_is_refused_before_anything_is_rendered_or_sent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(meta, "subject_masks", lambda *a, **k: pytest.fail("SAM must not be asked for a stale window"))
    monkeypatch.setattr(hero_word, "window_proxy", lambda *a, **k: pytest.fail("no proxy for a stale window"))
    plan = plan_of([{"start": 1.0, "end": 5.0}])
    hero = {"word": "oak", "start": 1.29, "end": 3.29, "subject": "the bowl", "side": "center", "font": "heavy", "color": "#FFFFFF"}
    with pytest.raises(HeroWordError, match="cut changed after the hero word was set.*Pick the hero word again"):
        hero_word.window_matte(tmp_path / "missing.mp4", plan, hero, work_dir=tmp_path / "w")


# --- Hero word: colour against what is behind it --------------------------------------------------

def flat(code: int, count: int = 1000) -> list[int]:
    histogram = [0] * 256
    histogram[code] = count
    return histogram


def test_white_is_kept_on_a_dark_background_and_replaced_on_a_light_one() -> None:
    dark = hero_word.contrast_by_color(flat(30))
    assert dark["#FFFFFF"] > 10 and hero_word.pick_color(dark, "#FFFFFF") == ("#FFFFFF", "")
    light = hero_word.contrast_by_color(flat(220))
    assert light["#FFFFFF"] < HERO_CONTRAST
    color, note = hero_word.pick_color(light, "#ffffff")
    assert color == "#101014" and "light" in note and "black" in note and "white" in note


def test_a_chosen_colour_that_reads_is_respected_and_one_that_does_not_is_replaced() -> None:
    dark = hero_word.contrast_by_color(flat(25))
    assert hero_word.pick_color(dark, "#FFD60A") == ("#FFD60A", "")
    mid = hero_word.contrast_by_color(flat(126))
    color, note = hero_word.pick_color(mid, "#FF453A")  # red on mid gray barely differs
    assert color in HERO_COLORS and color != "#FF453A" and mid[color] >= HERO_CONTRAST and "red" in note


def test_the_contrast_is_what_most_of_the_background_reaches_not_its_average() -> None:
    # Three quarters bright sky, one quarter dark trees: white must fail, because most of the word sits on the sky.
    histogram = [0] * 256
    histogram[225], histogram[30] = 800, 200
    contrasts = hero_word.contrast_by_color(histogram)
    assert contrasts["#FFFFFF"] < HERO_CONTRAST < contrasts["#101014"]
    assert hero_word.contrast_by_color([0] * 256) == {} and hero_word.pick_color({}, "#FFFFFF") == ("#FFFFFF", "")


def test_a_busy_background_is_called_busy_and_the_colour_that_reads_best_is_used() -> None:
    # Live, a white colonnade behind one person and a dark suit beside them: no flat colour reached 3:1.
    histogram = [0] * 256
    histogram[225], histogram[30] = 500, 500
    contrasts = hero_word.contrast_by_color(histogram)
    assert max(contrasts["#FFFFFF"], contrasts["#101014"]) < HERO_CONTRAST
    color, note = hero_word.pick_color(contrasts, "#FFFFFF")
    # A mid-tone reads against both halves, so it is used, and the note does not call the background dark.
    assert color == "#0A84FF" and contrasts[color] >= HERO_CONTRAST and "busy, light and dark, so it is drawn in blue" in note
    live = {"#FFFFFF": 2.17, "#FFD60A": 1.54, "#FF453A": 2.17, "#FF9F0A": 1.32, "#30D158": 1.3, "#0A84FF": 2.29, "#101014": 1.34}
    color, note = hero_word.pick_color(live, "#FFFFFF")
    assert color == "#0A84FF" and "busy, light and dark, so no colour reaches 3:1; blue reads best, at 2.3:1 (white 2.2:1)" in note


def test_a_side_where_no_colour_reads_gives_way_to_one_where_a_colour_does() -> None:
    hidden = {"center": 0.44, "top": 0.0, "left": 0.09, "right": 0.62}
    contrasts = {"center": {"#FFFFFF": 2.2, "#0A84FF": 2.3}, "top": {"#101014": 9.0}, "left": {"#101014": 6.1, "#FFFFFF": 1.5}, "right": {"#FFFFFF": 8.0}}
    side, note = hero_word.side_that_reads(hidden, contrasts, "center")
    # "left" reads and keeps some of the word behind the subject, so it beats "top", which hides nothing; "right" buries the word.
    assert side == "left" and "too busy" in note and "6.1:1" in note and "9% behind the subject" in note
    # Live, 16:9: only a side clear of the subject read. A word that reads beats one behind the subject that does not.
    side, note = hero_word.side_that_reads(hidden, dict(contrasts, left={"#FFFFFF": 2.0}), "center")
    assert side == "top" and "clear of the subject" in note and "9.0:1" in note
    assert hero_word.side_that_reads(hidden, dict(contrasts, left={"#FFFFFF": 2.0}, top={"#FFFFFF": 2.5}), "center") == ("center", "")
    assert hero_word.side_that_reads(hidden, dict(contrasts, center={"#FFFFFF": 4.0}), "center") == ("center", "")
    assert hero_word.side_that_reads(hidden, {}, "center") == ("center", "")


def test_the_background_is_read_where_the_word_is_and_the_subject_is_not() -> None:
    area = matte.Area(2, 0, 8, 4)  # the source sits 2 px in from the proxy's left edge
    proxy_width = 12
    luma = bytearray([200] * (proxy_width * 4))
    for y in range(4):
        luma[y * proxy_width + area.left + 4] = 20  # a dark column behind the subject only
    mask = bytearray(8 * 4)
    for y in range(4):
        mask[y * 8 + 4] = 255
    layout = {"x": 4, "y": 2, "width": 8, "height": 8}  # the whole 8x4 frame
    counts = hero_word.background_histogram([bytes(luma)], [bytes(mask)], layout, frame_width=8, frame_height=4, area=area, proxy_width=proxy_width)
    assert counts[20] == 0 and counts[200] == sum(counts) > 0


def test_the_receipt_fails_a_colour_that_does_not_read_and_says_how_well_it_reads() -> None:
    hero = {"word": "oak", "start": 1.0, "end": 2.5, "spoken": [1.06, 1.5], "color": "#FFFFFF"}
    stats = {"frames": 45, "window_frames": 45, "coverage": 1.0, "hidden": 0.3, "contrast_by_color": {"#FFFFFF": 1.4, "#101014": 12.0}}
    poor = hero_word_check(hero, stats, duration=5.0)
    assert poor["passed"] is False and "1.4:1" in poor["note"] and "under 3:1" in poor["note"]
    good = hero_word_check(dict(hero, color="#101014"), stats, duration=5.0)
    assert good["passed"] is True and "#101014 reads at 12.0:1" in good["note"]
    assert hero_word_check(hero, {k: v for k, v in stats.items() if k != "contrast_by_color"}, duration=5.0)["passed"] is True


# --- Hero word: SAM's subject in many shapes, with the matte drawn here ------------------------------

def ffmpeg(*args: str) -> None:
    subprocess.run([resolve_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def clip_of(tmp_path: Path, size: tuple[int, int], background: str, box: tuple[float, float, float, float]) -> Path:
    """A 5 s clip: a flat background, one dark subject at ``box`` (shares of the frame), and a tone."""
    width, height = size
    x0, y0, x1, y1 = box
    path = tmp_path / f"clip-{width}x{height}-{background}-{'-'.join(f'{v:.2f}' for v in box)}.mp4"
    ffmpeg("-f", "lavfi", "-i", f"color={background}:s={width}x{height}:r=30:d=5", "-f", "lavfi", "-i", "sine=frequency=330:duration=5",
           "-vf", f"drawbox=x={round(x0 * width)}:y={round(y0 * height)}:w={max(2, round((x1 - x0) * width))}:h={max(2, round((y1 - y0) * height))}"
           ":color=0x402010:t=fill,format=yuv420p", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "10", "-c:a", "aac", "-shortest", str(path))
    return path


def fake_sam(monkeypatch: pytest.MonkeyPatch, box: tuple[float, float, float, float], *, present: float = 1.0) -> dict[str, Any]:
    """SAM's masks replaced: the subject at ``box`` in every frame, for the first ``present`` share of the window."""
    seen: dict[str, Any] = {}

    def masks(proxy: Path, *, subject: str, duration: float, work_dir: Path, budget_usd: float, **_: Any) -> dict[str, Any]:
        info = probe(proxy)
        frames = round(duration * 30)
        seen.update(subject=subject, proxy=proxy, frames=frames)
        area = matte.picture_area(seen["source_width"], seen["source_height"], info.width, info.height)
        pixel = [area.left + box[0] * area.width, area.top + box[1] * area.height, area.left + box[2] * area.width, area.top + box[3] * area.height]
        return {"provider": "meta", "raw": str(work_dir / "stub.sam"), "fps": 30.0, "duration": duration, "object_id": 0,
                "frame_width": info.width, "frame_height": info.height, "usage": {"cost_usd": 0.01, "cached": False}, "warnings": [],
                "frames": [{"frame": index, "box": pixel, "span": [0, 0]} for index in range(max(1, round(frames * present)))]}

    def build(found: dict[str, Any], *, path: Path, area: Any, frames: int, cuts: Any = (), cancel_event: Any = None, **_: Any) -> dict[str, Any]:
        path.parent.mkdir(parents=True, exist_ok=True)
        x, y = round(box[0] * area.width), round(box[1] * area.height)
        w, h = max(1, round((box[2] - box[0]) * area.width)), max(1, round((box[3] - box[1]) * area.height))
        held = max(1, round(frames * present))
        ffmpeg("-f", "lavfi", "-i", f"color=black:s={area.width}x{area.height}:r=30:d={frames / 30:.4f}",
               "-vf", f"drawbox=x={x}:y={y}:w={w}:h={h}:color=white:t=fill:enable='lt(n,{held})',format=gray", "-frames:v", str(frames),
               "-c:v", "ffv1", "-pix_fmt", "gray", str(path))
        empty = frames - held
        return {"frames": frames, "masked_frames": held, "filled_frames": 0, "empty_frames": empty, "coverage": round(held / frames, 4),
                "flicker": 0.0, "mean_area": 0.1, "first_missing": round(held / 30, 3) if empty else None}

    monkeypatch.setattr(meta, "subject_masks", masks)
    monkeypatch.setattr(matte, "build_matte", build)
    return seen


def run_matte(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, size: tuple[int, int] = (320, 180), background: str = "0xE6E6E6",
              box: tuple[float, float, float, float] = (0.42, 0.25, 0.58, 1.0), present: float = 1.0, aspect: str = "16:9",
              hero: dict[str, Any] | None = None, plan: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    clip = clip_of(tmp_path, size, background, box)
    seen = fake_sam(monkeypatch, box, present=present)
    seen.update(source_width=size[0], source_height=size[1])
    hero = hero or {"word": "oak", "start": 1.29, "end": 2.79, "subject": "the person", "side": "center", "font": "heavy", "color": "#FFFFFF"}
    made = hero_word.window_matte(clip, plan or plan_of([{"start": 0, "end": 5}]), hero, work_dir=tmp_path / "w", aspect=aspect, **kwargs)
    made["seen"] = seen
    return made


@pytest.mark.render
@pytest.mark.parametrize("size, aspect", [((320, 180), "16:9"), ((180, 320), "9:16"), ((240, 240), "1:1"), ((320, 180), "9:16"), ((180, 320), "1:1")])
def test_every_shape_gets_a_matte_frame_per_window_frame_and_a_readable_side(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                              size: tuple[int, int], aspect: str) -> None:
    made = run_matte(tmp_path, monkeypatch, size=size, aspect=aspect, background="0x182030")
    stats = made["stats"]
    assert stats["frames"] == stats["window_frames"] == 45 and probe(made["proxy"]).duration == pytest.approx(1.5, abs=0.04)
    assert made["side"] in ("center", "top", "left", "right") and stats["hidden"] <= 0.55
    assert len(stats["sampled_frames"]) == hero_word.MATTE_SAMPLES and stats["sampled_frames"][-1] == 44
    upright = size[1] > size[0]
    assert (probe(made["proxy"]).width, probe(made["proxy"]).height) == ((540, 960) if upright else (960, 540))
    assert made["color"] == "#FFFFFF" and made["color_note"] == "" and stats["contrast"] >= HERO_CONTRAST
    assert made["edit"]["side"] == made["side"] and made["edit"]["color"] == made["color"] and made["edit"]["subject_box"] == made["subject_box"]


@pytest.mark.render
def test_on_a_light_background_white_gives_way_to_a_colour_that_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    made = run_matte(tmp_path, monkeypatch, background="0xEDEDED")
    stats = made["stats"]
    assert stats["contrast_by_color"]["#FFFFFF"] < HERO_CONTRAST and made["color"] == "#101014"
    assert "light" in made["color_note"] and stats["background_luma"] > 200
    hero = dict(made["edit"], spoken=[1.35, 1.59])
    assert hero_word_check(hero, stats, duration=5.0)["passed"] is True
    assert hero_word_check(dict(hero, color="#FFFFFF"), stats, duration=5.0)["passed"] is False


@pytest.mark.render
def test_a_subject_that_fills_the_frame_is_refused_with_a_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(HeroWordError, match="fills so much of the picture.*would not read"):
        run_matte(tmp_path, monkeypatch, box=(0.02, 0.02, 0.98, 1.0))


@pytest.mark.render
def test_a_tiny_subject_is_said_to_be_too_small_to_go_behind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    made = run_matte(tmp_path, monkeypatch, box=(0.05, 0.05, 0.07, 0.08), background="0x101010")
    assert max(made["stats"]["hidden_by_side"].values()) < hero_word.READABLE[0]
    assert any("small in the picture" in note for note in made["warnings"])


@pytest.mark.render
def test_a_subject_that_leaves_mid_window_is_reported_with_when(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    made = run_matte(tmp_path, monkeypatch, present=0.5, background="0x101010")
    note = next(note for note in made["warnings"] if note.startswith("SAM lost"))
    assert "the person" in note and "23 of 45 frames" in note and "from 2.02 s" in note
    assert made["stats"]["coverage"] == pytest.approx(22 / 45, abs=1e-3)
    assert hero_word_check(dict(made["edit"], spoken=[1.35, 1.59]), made["stats"], duration=5.0)["passed"] is False


@pytest.mark.render
def test_with_reanchor_a_re_trimmed_window_follows_the_word_and_the_matte_matches_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hero = {"word": "oak", "start": 1.29, "end": 2.79, "subject": "the person", "side": "center", "font": "heavy", "color": "#FFFFFF"}
    made = run_matte(tmp_path, monkeypatch, hero=hero, plan=plan_of([{"start": 1.0, "end": 5.0}]), reanchor=True, background="0x101010")
    assert made["start"] == pytest.approx(0.29) and made["end"] == pytest.approx(1.79) and made["window"]["status"] == "moved"
    assert made["edit"]["start"] == made["start"] and made["stats"]["frames"] == 45
    assert any("moved with it" in note for note in made["warnings"])


# --- Verified find: what SAM is asked to look for ------------------------------------------------------

@pytest.mark.parametrize(("query", "phrase"), [
    ("the woman walking", "woman"), ("a man in a red jacket", "man"), ("two people walking", "people"), ("the person in red", "person"),
    ("someone waving", "person"), ("someone waving at the camera", "person"), ("people dancing", "people"),
    ("something red", "red object"), ("red", "red object"), ("red things", "red object"), ("the white house", "white house"),
    ("the dogs", "dogs"), ("the big red car", "big red car"), ("the dark wooden table", "dark wooden table"),
    ("when I pick up the bowl", "bowl"), ("me holding the bowl", "bowl"), ("the bird in the sky", "bird"),
    ("Show me the woman walking", "woman"), ("café tables", "café tables"), ("close-up of hands", "hands"), ("a wide shot of the city", "city"),
    ("when the crowd starts cheering", "crowd"), ("a dog playing fetch", "dog"),
])
def test_people_actions_colours_plurals_and_adjectives_name_something_sam_can_outline(query: str, phrase: str) -> None:
    subject = query_subject(query)
    assert (subject["kind"], subject["phrase"]) == ("visual", phrase)


@pytest.mark.parametrize(("query", "kind"), [
    ("where I mention pricing", "spoken"), ("when she says hello", "spoken"), ("the part about pricing", "abstract"),
    ("dancing", "abstract"), ("waving", "abstract"), ("la mujer caminando", "abstract"), ("el perro rojo", "abstract"),
    ("女人在走路", "abstract"), ("Où est le chien", "abstract"), ("犬", "abstract"), ("the music", "spoken"), ("", "abstract"),
    ("where the music starts", "spoken"), ("the sound of rain", "spoken"), ("the end of the video", "abstract"),
])
def test_speech_actions_alone_and_other_languages_are_not_sent_to_sam_by_the_rule(query: str, kind: str) -> None:
    subject = query_subject(query)
    assert subject["kind"] == kind and subject["phrase"] is None and "SAM" in subject["reason"]


def test_spark_s_subject_is_checked_like_the_rule_s_own() -> None:
    assert named_subject("woman", by="muse-spark") == {"kind": "visual", "phrase": "woman", "reason": "Muse Spark named what SAM should look for: “woman”."}
    assert named_subject("background music", by="muse-spark")["kind"] == "spoken"
    assert named_subject("the best part")["kind"] == "abstract" and named_subject("something")["kind"] == "abstract"
    assert named_subject("") is None and named_subject("   ") is None and named_subject(None) is None and named_subject(3) is None


# --- Verified find: end to end on stubbed SAM -----------------------------------------------------------

pytest.importorskip("meta_sam_parser")

FIXTURE = json.loads(Path(__file__).with_name("fixtures").joinpath("sam_bowl.json").read_text(encoding="utf-8"))["records"]
MASK = FIXTURE["lossless_keyframe"]["line"].removeprefix("<0f>")  # real SAM 3.1 output: one box and one lossless mask at 960x540


class Sam:
    """SAM's boundary: the masked frames each window gets, by the window's start in ms, and what it was asked for."""

    def __init__(self, answer: Any = "") -> None:
        self.answer, self.calls, self.files, self.asked, self.lock = answer, [], {}, [], threading.Lock()

    def __call__(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        with self.lock:
            self.calls.append((method, path))
            if method == "POST" and path == "/files":
                ident = f"file-{len(self.files) + 1}"
                self.files[ident] = Path(kwargs["body"].source)
                return {"id": ident, "status": "uploaded"}
        if method == "DELETE":
            return {"deleted": True}
        if method == "POST" and path == "/responses":
            content = kwargs["payload"]["input"][0]["content"]
            window = self.files[content[1]["file_id"]]
            start = int(re.search(r"-(\d+)-(\d+)\.mp4$", window.name).group(1))
            with self.lock:
                self.asked.append(content[0]["text"])
            frames = self.answer(start) if callable(self.answer) else self.answer
            text = "\n".join(f"<{frame}f>{MASK}" for frame in frames)
            return {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
                    "usage": {"video_frames_processed": 45}}
        pytest.fail(f"Unexpected provider request: {method} {path}")

    def paid(self) -> int:
        return sum(call == ("POST", "/responses") for call in self.calls)


@pytest.fixture(scope="module")
def footage(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("footage") / "footage.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=12", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path))
    return path


def found(*spans: tuple[float, float]) -> list[dict[str, Any]]:
    return [{"start": start, "end": end, "reason": f"Seen at {start}s.", "label": f"Seen at {start}s.", "source": "meta", "score": 0.9 - 0.05 * rank}
            for rank, (start, end) in enumerate(spans)]


def test_spark_s_subject_is_what_sam_looks_for_and_the_result_says_so(enabled: None, footage: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sam = Sam(range(45))
    monkeypatch.setattr(meta, "_request", sam)
    result = verify_moments(found((0.0, 3.0)), "la mujer caminando", footage, tmp_path / "meta", 0.05, phrase="woman", phrase_from="muse-spark")
    assert sam.asked == ["woman"] and result["phrase"] == "woman" and result["phrase_from"] == "muse-spark"
    assert result["moments"][0]["verified"] is True and "“woman”" in result["receipt"]["name"]


def test_without_spark_s_subject_the_rule_names_it(enabled: None, footage: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sam = Sam(range(45))
    monkeypatch.setattr(meta, "_request", sam)
    result = verify_moments(found((0.0, 3.0)), "someone waving", footage, tmp_path / "meta", 0.05, phrase="", phrase_from="muse-spark")
    assert sam.asked == ["person"] and result["phrase_from"] == "rule"


def test_a_named_sound_or_a_foreign_search_without_a_subject_costs_nothing(enabled: None, footage: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sam = Sam(range(45))
    monkeypatch.setattr(meta, "_request", sam)
    sound = verify_moments(found((0.0, 3.0)), "where the music starts", footage, tmp_path / "meta", 0.05, phrase="music", phrase_from="muse-spark")
    foreign = verify_moments(found((0.0, 3.0)), "el perro rojo", footage, tmp_path / "meta", 0.05)
    assert sam.calls == [] and sound["kind"] == "spoken" and foreign["kind"] == "abstract"
    assert all(moment["verified"] is None and moment["verification"] for moment in sound["moments"] + foreign["moments"])
    assert sound["receipt"]["passed"] is True and sound["receipt"]["note"].startswith("Not checked:")


def test_an_empty_search_result_checks_nothing_and_costs_nothing(enabled: None, footage: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sam = Sam(range(45))
    monkeypatch.setattr(meta, "_request", sam)
    result = verify_moments([], "the woman walking", footage, tmp_path / "meta", 0.05, phrase="woman", phrase_from="muse-spark")
    assert sam.calls == [] and result["moments"] == [] and result["spent_usd"] == 0.0 and result["receipt"]["passed"] is True
    assert "nothing to check" in result["reason"]


def test_a_sam_miss_is_flagged_and_kept_never_dropped_by_default(enabled: None, footage: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sam = Sam(lambda start: range(45) if start == 0 else [])
    monkeypatch.setattr(meta, "_request", sam)
    result = verify_moments(found((6.0, 9.0), (0.0, 3.0)), "the woman walking", footage, tmp_path / "meta", 0.05, phrase="woman", phrase_from="muse-spark")
    assert [moment["start"] for moment in result["moments"]] == [0.0, 6.0] and result["dropped"] == []
    missed = result["moments"][1]
    assert missed["flagged"] is True and missed["verified"] is False and missed["score"] == pytest.approx(0.45) and missed["evidence"]["spark_score"] == 0.9
    assert result["receipt"]["passed"] is False and "kept and flagged" in result["receipt"]["note"]


def test_the_budget_cap_holds_before_any_frame_is_sent(enabled: None, footage: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sam = Sam(range(45))
    monkeypatch.setattr(meta, "_request", sam)
    moments = found((0.0, 2.0), (2.5, 4.5), (5.0, 7.0), (7.5, 9.5), (10.0, 12.0))
    result = verify_moments(moments, "the woman walking", footage, tmp_path / "meta", 0.03, phrase="woman", phrase_from="muse-spark")
    assert sam.calls == [] and result["status"] == "refused" and "needs up to $0.0500" in result["reason"] and "Nothing was sent" in result["reason"]
    assert [moment["verified"] for moment in result["moments"]] == [None] * 5


@pytest.mark.render
def test_a_word_the_clip_is_silent_through_is_refused_before_sam_is_asked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # whisper.cpp hears "You" in 15 s of digital silence; a transcript like that must never become a hero word.
    clip = tmp_path / "silent.mp4"
    ffmpeg("-f", "lavfi", "-i", "color=0x203040:s=320x180:r=30:d=5", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", "5",
           "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(clip))
    monkeypatch.setattr(meta, "subject_masks", lambda *a, **k: pytest.fail("SAM must not be asked about a silent window"))
    hero = {"word": "oak", "start": 1.29, "end": 2.79, "subject": "the person", "side": "center", "font": "heavy", "color": "#FFFFFF"}
    with pytest.raises(HeroWordError, match="silent while it should be said"):
        hero_word.window_matte(clip, plan_of([{"start": 0, "end": 5}]), hero, work_dir=tmp_path / "w")
    assert hero_word.window_loudness(clip, plan_of([{"start": 0, "end": 5}]), 1.29, 2.79) < hero_word.SILENT_DB


@pytest.mark.render
def test_a_re_trim_that_puts_a_cut_inside_the_window_is_refused_before_sam_is_asked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clip = clip_of(tmp_path, (320, 180), "0x101010", (0.42, 0.25, 0.58, 1.0))
    monkeypatch.setattr(meta, "subject_masks", lambda *a, **k: pytest.fail("SAM must not be asked across a cut"))
    # "oak" is still said at 1.35 s, but a cut now falls at 2.0 s, inside the 1.29 to 2.79 s window.
    plan = plan_of([{"start": 0.0, "end": 2.0}, {"start": 3.0, "end": 5.0}])
    hero = {"word": "oak", "start": 1.29, "end": 2.79, "subject": "the person", "side": "center", "font": "heavy", "color": "#FFFFFF"}
    assert hero_word.window_status(plan, hero)["status"] == "fresh"
    with pytest.raises(HeroWordError, match="inside one shot"):
        hero_word.window_matte(clip, plan, hero, work_dir=tmp_path / "w")


# --- Nothing copied from meta.py: language, model ids and prices ------------------------------------------

@pytest.mark.parametrize("language, written", [("auto", "the language the words are spoken in"), ("", "the language the words are spoken in"),
                                               ("pt-BR", "Portuguese (pt-BR)"), ("ja", "Japanese")])
def test_the_hero_word_prompt_names_a_language_a_model_can_write_in(language: str, written: str) -> None:
    words = candidate_words([{"start": 0, "end": 2, "text": "oak bowl", "words": said("oak", "bowl")}])
    prompt = hero_word._prompt(words, CUTS, duration=5.0, aspect="9:16", language=language, subject="the bowl")
    assert f"Write the reason in {meta.write_language(language, 'the language the words are spoken in')}." in prompt
    assert written.split(" (")[0] in prompt and "Write the reason in auto" not in prompt


def test_model_ids_and_the_sam_price_come_from_meta() -> None:
    from vedit import verify_find

    assert hero_word.SAM == verify_find.SAM_MODEL == meta._SAM_MODEL
    assert hero_word.SPARK == verify_find.FOUND_BY == meta._DIRECTOR_LABELS["spark"]
    assert verify_find.FRAME_PRICE == meta._FRAME_PRICE
    # A window sampled to MAX_FRAMES reserves what meta reserves for it: the 50-frame minimum.
    for frames in (1, verify_find.MAX_FRAMES, 120):
        assert verify_find.billed_usd(frames) == round(max(50, frames + 2) * meta._FRAME_PRICE, 8)
