"""Punch words: a model proposes, the captions decide, every style draws it, and FFmpeg burns it."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from vedit import meta
from vedit.caption_styles import LOOKS, MIN_SCALE, Punch, Stage, caption_font, emphasised, paginate, render_ass, safe_area, write_styled_ass
from vedit.captions import Cue, Word, burn_filter, cue_key, parse_ts
from vedit.edit_spec import CAPTION_STYLES, EMPHASIS_COLOR, MAX_EMPHASIS, MAX_EMPHASIS_INDEX, EditError, default_edit, merge_edit, validate_edit
from vedit.emphasis import EmphasisError, edit_patch, emphasis_limit, emphasis_lines, emphasis_prompt, suggest_emphasis, validate_emphasis
from vedit.media import has_filter, resolve_ffmpeg
from vedit.meta import MetaError

STYLES = [row[0] for row in CAPTION_STYLES]
TALL, WIDE, SQUARE = (1080, 1920), (1920, 1080), (1080, 1080)
HIGH = "&H0AD6FF&"
ACCENT = "&H8CE83C&"
LINE = re.compile(r"^Dialogue: 0,(\d:\d{2}:\d{2}\.\d{2}),(\d:\d{2}:\d{2}\.\d{2}),Default,,0,0,0,,(.*)$")
SPOKEN = [
    "This is the finished oak bowl, oiled and ready for the table.",
    "Here is the raw oak blank, still wet and heavy.",
    "The grain tells me where the bowl wants to be.",
    "On the lathe the shape appears in long, steady cuts.",
    "Sanding goes from coarse to fine, with the lathe turning slowly.",
    "24 hours later, the bowl is ready to use.",
]
STARTS = [0.2, 3.6, 6.6, 9.2, 12.2, 16.1]
GOAL = "A short about turning a wooden bowl, for people who like handmade things."


def timed(text: str, start: float = 1.0, each: float = 0.3) -> Cue:
    words = tuple(Word(round(start + index * each, 3), round(start + (index + 1) * each, 3), word) for index, word in enumerate(text.split()))
    return Cue(words[0].start, words[-1].end, text, words)


def story() -> list[Cue]:
    return [timed(text, start) for text, start in zip(SPOKEN, STARTS, strict=True)]


def pick(key: str, index: int, word: str, why: str = "It carries the line.") -> dict[str, Any]:
    return {"cue_key": key, "word_index": index, "word": word, "why": why}


GOOD = [pick("200", 4, "oak", "The material is the whole story."), pick("16100", 1, "hours", "Time is the payoff.")]


def events(ass: str) -> list[tuple[float, float, str]]:
    rows = []
    for line in ass.splitlines():
        if line.startswith("Dialogue:"):
            match = LINE.match(line)
            assert match, line
            rows.append((parse_ts(match[1]), parse_ts(match[2]), match[3]))
    return rows


def spoken(text: str) -> str:
    return " ".join(re.sub(r"(?<!\\)\{[^}]*\}", "", text).replace(r"\N", " ").split())


def accented(text: str) -> list[str]:
    """The words drawn in the accent color."""
    return re.findall(r"\{\\c" + ACCENT + r"[^}]*\}([^{]*)\{", text)


def active(text: str) -> list[str]:
    return re.findall(r"\{\\c" + HIGH + r"[^}]*\}([^{]*)\{", text)


def stage(style: str, size: tuple[int, int]) -> Stage:
    """The stage ``render_ass`` builds for a style on a canvas."""
    look = LOOKS[style]
    width, height = size
    pixels = max(8, round(min(width, height) * look.size))
    room = max(pixels, width - 2 * safe_area(width, height)[0] - round(2 * max(round(pixels * look.outline, 1), round(pixels * look.padding))))
    return Stage(look, pixels, room, caption_font()[1])


def on(*entries: dict[str, Any], style: str = "boxed", **extra: Any) -> dict[str, Any]:
    return {"style": style, "emphasize": True, "emphasis": [{key: item[key] for key in ("cue_key", "word_index", "word")} for item in entries], **extra}


# --- Validation: the model proposes, the captions decide ---


def test_lines_carry_the_key_and_the_words_the_renderer_counts() -> None:
    lines = emphasis_lines(story())
    assert [line["key"] for line in lines] == ["200", "3600", "6600", "9200", "12200", "16100"]
    assert lines[0]["words"][4] == "oak" and lines[0]["words"][5] == "bowl,"
    records = [{"key": "5000", "text": "Hello there"}, {"key": "5000", "text": "Shared key"}, {"key": "7000", "text": " ... "}]
    assert [line["key"] for line in emphasis_lines(records)] == ["5000"], "a shared key and a line with no word are left out"
    with pytest.raises(EmphasisError):
        emphasis_lines([])
    with pytest.raises(EmphasisError):
        emphasis_lines([{"key": "../x", "text": "hi"}])


@pytest.mark.parametrize("count,limit", [(0, 0), (1, 1), (2, 1), (3, 1), (5, 2), (6, 2), (10, 4), (600, 240)])
def test_at_most_forty_percent_of_lines_and_at_least_one(count: int, limit: int) -> None:
    assert emphasis_limit(count) == limit


def test_valid_picks_are_kept_in_the_models_order() -> None:
    kept, warnings = validate_emphasis({"picks": GOOD}, emphasis_lines(story()))
    assert kept == [{**GOOD[0]}, {**GOOD[1]}]
    assert warnings == []


def test_unknown_cues_bad_places_and_invented_words_are_left_out() -> None:
    lines = emphasis_lines(story())
    raw = {"picks": [
        GOOD[0],
        pick("3600", 4, "oak"),
        pick("6600", 1, "grain"),  # valid, but past the two lines six allow
        pick("999", 0, "oak"),  # no such line
        pick("9200", 40, "the"),  # out of range, and "the" is in the line twice, so there is no telling which
        pick("6600", 1, "walnut"),  # not in the line at all
    ]}
    kept, warnings = validate_emphasis(raw, lines, share=0.4)
    assert [(item["cue_key"], item["word"]) for item in kept] == [("200", "oak"), ("3600", "oak")]
    assert any("3 picks named a line or word that is not in the captions" in warning for warning in warnings)


@pytest.mark.parametrize("bad", [{"cue_key": 200, "word_index": 4, "word": "oak"}, pick("200", True, "oak"), pick("200", 4.0, "oak"), pick("200", 4, ""),
                                 pick("200", 4, "..."), "oak", None])
def test_malformed_picks_never_count(bad: Any) -> None:
    kept, warnings = validate_emphasis({"picks": [GOOD[0], GOOD[1], bad]}, emphasis_lines(story()))
    assert len(kept) == 2 and warnings


def test_a_word_at_the_wrong_place_moves_to_it_only_when_it_is_unambiguous() -> None:
    lines = emphasis_lines(story())
    kept, warnings = validate_emphasis({"picks": [pick("200", 5, "oak"), pick("16100", 0, "Hours")]}, lines)
    assert [(item["cue_key"], item["word_index"], item["word"]) for item in kept] == [("200", 4, "oak"), ("16100", 1, "hours")]
    assert any("moved to that word" in warning for warning in warnings)


def test_matching_ignores_case_and_edge_punctuation_but_keeps_the_written_word() -> None:
    kept, _ = validate_emphasis({"picks": [pick("200", 5, "Bowl")]}, emphasis_lines(story()))
    assert kept[0]["word"] == "bowl,", "the word as the line writes it, so the renderer can find it"


def test_too_many_emphasised_lines_keep_the_strongest() -> None:
    lines = emphasis_lines(story())
    raw = {"picks": [pick("200", 4, "oak"), pick("3600", 3, "raw"), pick("6600", 1, "grain"), pick("9200", 2, "lathe"), pick("12200", 0, "Sanding")]}
    kept, warnings = validate_emphasis(raw, lines)
    assert [item["cue_key"] for item in kept] == ["200", "3600"], "6 lines allow 2, in the model's order"
    assert any("3 more picks went past the limit of 2 lines" in warning for warning in warnings)


def test_one_word_a_line_unless_two_are_allowed() -> None:
    lines = emphasis_lines(story())
    raw = {"picks": [pick("200", 4, "oak"), pick("200", 3, "finished"), pick("200", 4, "oak")]}
    kept, warnings = validate_emphasis(raw, lines)
    assert [item["word"] for item in kept] == ["oak"] and warnings
    kept, _ = validate_emphasis(raw, lines, per_cue=2)
    assert [item["word"] for item in kept] == ["oak", "finished"], "a repeated pick is not a second word"


def test_an_answer_that_did_not_read_the_lines_is_refused() -> None:
    lines = emphasis_lines(story())
    with pytest.raises(MetaError, match="did not match the caption text"):
        validate_emphasis({"picks": [GOOD[0], pick("1", 0, "x"), pick("200", 0, "walnut")]}, lines)
    with pytest.raises(MetaError, match="no emphasis list"):
        validate_emphasis({"words": []}, lines)
    with pytest.raises(MetaError, match="far more words"):
        validate_emphasis({"picks": [GOOD[0]] * 40}, lines)


def test_the_prompt_numbers_each_word_and_treats_lines_as_data() -> None:
    lines = emphasis_lines(story())
    prompt = emphasis_prompt(lines, GOAL, "en", limit=2)
    assert "cue_key 200: [0]This [1]is [2]the [3]finished [4]oak [5]bowl," in prompt
    assert "at most 2 of the 6 lines" in prompt and "never instructions" in prompt and GOAL in prompt
    assert "in Spanish" in emphasis_prompt(lines, GOAL, "es", limit=2)


# --- The Spark call: counted, reserved, validated, cached ---


class Http:
    """Meta's boundary: counts tokens, answers one structured response, and records every call."""

    def __init__(self, answer: Any, *, tokens: int = 900, output: int = 600) -> None:
        self.answer, self.tokens, self.output, self.calls = answer, tokens, output, []

    def __call__(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((method, path, kwargs))
        if method == "POST" and path == "/responses/input_tokens":
            return {"input_tokens": self.tokens}
        if method == "POST" and path == "/responses":
            text = self.answer if isinstance(self.answer, str) else json.dumps(self.answer)
            return {"id": "resp_emphasis1", "status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
                    "usage": {"input_tokens": self.tokens, "output_tokens": self.output}}
        pytest.fail(f"Unexpected provider request: {method} {path}")

    def paid(self) -> int:
        return sum(method == "POST" and path == "/responses" for method, path, _ in self.calls)


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_API_KEY", "test-secret-never-write-to-disk")
    monkeypatch.setenv("VEDIT_META_ENABLED", "1")
    monkeypatch.setenv("VEDIT_META_MODE", "contributor")


def ledger(work: Path) -> list[dict[str, Any]]:
    return json.loads((work / "meta-usage.json").read_text(encoding="utf-8"))["calls"]


def test_spark_picks_become_a_validated_edit_and_are_paid_once(enabled: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    http = Http({"picks": GOOD})
    monkeypatch.setattr(meta, "_request", http)
    result = suggest_emphasis(story(), GOAL, "en", work_dir=tmp_path, budget_usd=.05)
    assert result["emphasis"] == GOOD and result["limit"] == 2 and result["lines"] == 6
    assert result["model"] == "muse-spark-1.3-contributor" and result["provider"] == "meta"
    assert result["usage"]["cost_usd"] == pytest.approx(900 * .10e-6 + 600 * .20e-6)
    edit = validate_edit(merge_edit(default_edit(), {"captions": result["edit"]}), duration=30)
    assert edit["captions"]["emphasize"] is True
    assert edit["captions"]["emphasis"] == [{"cue_key": "200", "word_index": 4, "word": "oak"}, {"cue_key": "16100", "word_index": 1, "word": "hours"}]
    request = next(kwargs["payload"] for method, path, kwargs in http.calls if path == "/responses")
    assert request["text"]["format"]["type"] == "json_schema" and request["text"]["format"]["strict"] is True
    assert [part["type"] for part in request["input"][0]["content"]] == ["input_text"], "text only: nothing is uploaded"
    assert "[4]oak" in request["input"][0]["content"][0]["text"]
    calls = ledger(tmp_path)
    assert [(call["operation"], call["status"]) for call in calls] == [("emphasis", "completed")]
    assert "test-secret" not in (tmp_path / "meta-usage.json").read_text(encoding="utf-8")
    again = suggest_emphasis(story(), GOAL, "en", work_dir=tmp_path, budget_usd=.05)
    assert http.paid() == 1 and again["usage"]["cached"] is True and again["emphasis"] == GOOD


def test_an_answer_that_fails_validation_is_recorded_as_failed(enabled: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(meta, "_request", Http({"picks": [pick("1", 0, "walnut")]}))
    with pytest.raises(MetaError, match="did not match"):
        suggest_emphasis(story(), GOAL, work_dir=tmp_path, budget_usd=.05)
    assert [call["status"] for call in ledger(tmp_path)] == ["failed_or_incomplete"], "the charge stays on the ledger"
    assert not list(tmp_path.glob("meta-cache/emphasis-*")), "a refused answer is never cached"


def test_malformed_json_changes_nothing(enabled: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(meta, "_request", Http("{not json"))
    with pytest.raises(MetaError, match="malformed JSON"):
        suggest_emphasis(story(), GOAL, work_dir=tmp_path, budget_usd=.05)


def test_the_budget_is_checked_before_anything_is_paid(enabled: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VEDIT_META_MODE", "standard")
    http = Http({"picks": GOOD})
    monkeypatch.setattr(meta, "_request", http)
    with pytest.raises(MetaError, match="campaign budget"):
        suggest_emphasis(story(), GOAL, work_dir=tmp_path, budget_usd=.05)  # standard mode reserves room for 16k output tokens: about $0.07
    assert http.paid() == 0


def test_no_request_without_a_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.delenv("VEDIT_META_ENABLED", raising=False)
    monkeypatch.setattr(meta, "_request", lambda *args, **kwargs: pytest.fail("no request may be made"))
    with pytest.raises(MetaError):
        suggest_emphasis(story(), GOAL, work_dir=tmp_path)
    assert not (tmp_path / "meta-usage.json").exists()


@pytest.mark.parametrize("arguments", [{"language": "English"}, {"share": 0.9}, {"share": 0}, {"per_cue": 3}, {"per_cue": True}])
def test_bad_settings_are_refused_before_any_call(arguments: dict[str, Any], tmp_path: Path) -> None:
    with pytest.raises(EmphasisError):
        suggest_emphasis(story(), GOAL, work_dir=tmp_path, ask=lambda *args, **kwargs: pytest.fail("no call"), **arguments)


def test_another_model_can_answer_through_the_same_function(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The private-mode route: same call, same checks, and Meta is never asked."""
    monkeypatch.setattr(meta, "_request", lambda *args, **kwargs: pytest.fail("Meta must not be called"))
    seen: list[dict[str, Any]] = []

    def local(work_dir: Path, operation: str, *, name: str, schema: dict[str, Any], prompt: str, budget: float, check: Any, purpose: str) -> dict[str, Any]:
        seen.append({"operation": operation, "name": name, "schema": schema, "prompt": prompt, "purpose": purpose})
        # This router forgets to call check: the answer is still validated.
        return {"picks": [*GOOD, pick("200", 0, "walnut")], "provider": "local", "model": "muse-glimmer", "usage": {"cost_usd": 0}}

    result = suggest_emphasis(story(), GOAL, work_dir=tmp_path, ask=local)
    assert seen[0]["operation"] == "emphasis" and seen[0]["schema"]["required"] == ["picks"] and "[4]oak" in seen[0]["prompt"]
    assert result["emphasis"] == GOOD and result["provider"] == "local" and result["model"] == "muse-glimmer"
    assert any("not in the captions" in warning for warning in result["warnings"])
    suggest_emphasis(story(), GOAL, work_dir=tmp_path, ask=local)
    assert len(seen) == 2, "only Meta answers are cached"


def test_an_empty_answer_turns_emphasis_off() -> None:
    assert edit_patch([]) == {"emphasize": False, "emphasis": []}


# --- The edit holds bounded data ---


def test_emphasis_is_off_by_default_and_round_trips() -> None:
    captions = default_edit()["captions"]
    assert captions["emphasize"] is False and captions["emphasis"] == [] and captions["emphasis_color"] == EMPHASIS_COLOR
    entries = [{"cue_key": "200", "word_index": 4, "word": "oak"}, {"cue_key": "200", "word_index": 5, "word": "bowl,"}]
    edit = validate_edit({"captions": {"emphasize": True, "emphasis": entries, "emphasis_color": "#ff5c8a"}}, duration=30)
    assert edit["captions"]["emphasis"] == entries and edit["captions"]["emphasis_color"] == "#FF5C8A"
    noisy = validate_edit({"captions": {"emphasis": [{**entries[0], "why": "dropped", "extra": 1}]}}, duration=30)
    assert noisy["captions"]["emphasis"] == [entries[0]], "only the three fields survive"


@pytest.mark.parametrize("captions", [
    {"emphasize": "yes"},
    {"emphasis": {"cue_key": "200"}},
    {"emphasis": [{"cue_key": "abc", "word_index": 0, "word": "x"}]},
    {"emphasis": [{"cue_key": "123456789", "word_index": 0, "word": "x"}]},
    {"emphasis": [{"cue_key": 200, "word_index": 0, "word": "x"}]},
    {"emphasis": [{"cue_key": "200", "word_index": -1, "word": "x"}]},
    {"emphasis": [{"cue_key": "200", "word_index": True, "word": "x"}]},
    {"emphasis": [{"cue_key": "200", "word_index": 1.0, "word": "x"}]},
    {"emphasis": [{"cue_key": "200", "word_index": MAX_EMPHASIS_INDEX + 1, "word": "x"}]},
    {"emphasis": [{"cue_key": "200", "word_index": 0, "word": ""}]},
    {"emphasis": [{"cue_key": "200", "word_index": 0, "word": "two words"}]},
    {"emphasis": [{"cue_key": "200", "word_index": 0, "word": "x" * 65}]},
    {"emphasis": [{"cue_key": "200", "word_index": 0, "word": "a\nb"}]},
    {"emphasis": [{"cue_key": "200", "word_index": 0}]},
    {"emphasis": [{"cue_key": "200", "word_index": 0, "word": "x"}, {"cue_key": "200", "word_index": 0, "word": "x"}]},
    {"emphasis": [{"cue_key": "200", "word_index": index, "word": "x"} for index in range(3)]},
    {"emphasis": [{"cue_key": str(index), "word_index": 0, "word": "x"} for index in range(MAX_EMPHASIS + 1)]},
    {"emphasis_color": "green"},
])
def test_emphasis_out_of_bounds_is_refused(captions: dict[str, Any]) -> None:
    with pytest.raises(EditError):
        validate_edit({"captions": captions}, duration=30)


def test_the_most_emphasis_an_edit_can_hold_validates() -> None:
    entries = [{"cue_key": str(index // 2), "word_index": index % 2, "word": "w"} for index in range(MAX_EMPHASIS)]
    assert len(validate_edit({"captions": {"emphasize": True, "emphasis": entries}}, duration=30)["captions"]["emphasis"]) == MAX_EMPHASIS


# --- Drawing: every style, and the spoken-word highlight still moves ---


@pytest.mark.parametrize("style", STYLES)
@pytest.mark.parametrize("size", [TALL, WIDE, SQUARE])
def test_every_style_draws_the_punch_word_and_nothing_else_changes(style: str, size: tuple[int, int], arial_width_captions: Path) -> None:
    cues = story()
    plain = events(render_ass(cues, width=size[0], height=size[1], settings={"style": style}))
    punched = events(render_ass(cues, width=size[0], height=size[1], settings=on(*GOOD, style=style)))
    pages: list[str] = []
    for _, _, text in punched:
        if not pages or pages[-1] != spoken(text):
            pages.append(spoken(text))
    assert " ".join(pages) == " ".join(SPOKEN), "every word is still read, once, in order"
    for start, _end, text in punched:
        assert re.sub(r"\\[{}]", "", text).count("{") == re.sub(r"\\[{}]", "", text).count("}")
        words = spoken(text).split()
        expected = [word for word, at in (("oak", 0.2), ("hours", 16.1)) if word in words and at <= start < at + 3.4 and active(text) != [word]]
        assert accented(text) == expected, (style, start, text)
    look = LOOKS[style]
    assert any(f"\\fscx{look.punch}\\fscy{look.punch}" in text for _, _, text in punched)
    assert any("\\b1" in text for _, _, text in punched) is not look.bold, "a light style makes the punch word bold; a bold one needs nothing"
    if look.highlight:
        assert [(start, end) for start, end, _ in punched] == [(start, end) for start, end, _ in plain], "one event per word, at the same times"
        for (_, _, before), (_, _, after) in zip(plain, punched, strict=True):
            assert active(after) == active(before) and len(active(after)) == 1, "the spoken-word highlight moves exactly as before"
            assert spoken(after) == spoken(before), "the page does not reflow when the highlight passes"


def test_the_highlight_wins_the_color_and_the_punch_keeps_its_size() -> None:
    rows = events(render_ass([timed("one two three")], width=TALL[0], height=TALL[1], settings=on(pick("1000", 1, "two"), style="karaoke")))
    assert active(rows[0][2]) == ["one"] and accented(rows[0][2]) == ["two"]
    assert "{\\c" + HIGH + "\\fscx115\\fscy115}two{\\c&HFFFFFF&\\fscx100\\fscy100}" in rows[1][2]
    pop = events(render_ass([timed("one two three")], width=TALL[0], height=TALL[1], settings=on(pick("1000", 1, "two"), style="pop")))
    assert "\\fscx121\\fscy121\\t(0,90,\\fscx108\\fscy108)}two" in pop[1][2], "the pop settles to the punch size, not to 100%"
    assert "\\t(0,90,\\fscx100\\fscy100)}one" in pop[0][2]


@pytest.mark.parametrize("style", STYLES)
@pytest.mark.parametrize("size", [TALL, WIDE, SQUARE])
def test_a_punch_word_never_pushes_a_line_out_of_the_safe_area(style: str, size: tuple[int, int]) -> None:
    fitted = stage(style, size)
    texts = [*SPOKEN, "Supercalifragilisticexpialidocious is a long word to shout", "x" * 120]
    for text in texts:
        cue = timed(text, 0.0)
        tokens = text.split()
        marked = emphasised(cue, [(index, word) for index, word in enumerate(tokens)][:: max(1, len(tokens) // 2)][:2])
        for page in paginate(marked, fitted):
            assert len(page.lines) <= fitted.look.lines
            for line in page.lines:
                assert fitted.width(line) * page.size / fitted.size <= fitted.room + 1, (style, size, text)
            assert page.size >= int(fitted.size * MIN_SCALE) - 1


@pytest.mark.parametrize("style", [style for style in STYLES if not LOOKS[style].words])
def test_two_lines_of_42_characters_stay_two_lines_on_a_wide_frame(style: str) -> None:
    text = "Sanding goes from coarse to fine, with the lathe turning slowly."
    assert len(text) <= 84
    plain = paginate(timed(text, 0.0), stage(style, WIDE))
    punched = paginate(emphasised(timed(text, 0.0), [(3, "coarse")]), stage(style, WIDE))
    assert len(punched) == len(plain) == 1 and len(punched[0].lines) <= 2
    assert any(isinstance(word, Punch) and word.text == "coarse" for word in punched[0].words)


def test_off_means_byte_for_byte_the_plain_captions() -> None:
    for style in STYLES:
        plain = render_ass(story(), width=TALL[0], height=TALL[1], settings={"style": style})
        assert render_ass(story(), width=TALL[0], height=TALL[1], settings={**on(*GOOD, style=style), "emphasize": False}) == plain
        assert render_ass(story(), width=TALL[0], height=TALL[1], settings={"style": style, "emphasize": True, "emphasis": []}) == plain


def test_a_custom_accent_color_is_used() -> None:
    ass = render_ass(story(), width=TALL[0], height=TALL[1], settings=on(*GOOD, emphasis_color="#FF5C8A"))
    assert "{\\c&H8A5CFF&\\fscx115\\fscy115}oak" in ass


def test_an_edited_or_translated_line_keeps_no_stale_emphasis() -> None:
    cue = story()[0]
    edited = Cue(cue.start, cue.end, "This is the finished walnut bowl, oiled and ready.")
    translated = Cue(cue.start, cue.end, "Este es el cuenco de roble terminado, aceitado y listo.")
    for line in (edited, translated):
        ass = render_ass([line], width=TALL[0], height=TALL[1], settings=on(GOOD[0]))
        assert ACCENT not in ass
    moved = Cue(cue.start, cue.end, "Oak: this is the finished oak bowl.")
    assert accented(events(render_ass([moved], width=TALL[0], height=TALL[1], settings=on(GOOD[0])))[0][2]) == [], "index 4 is now 'finished'"


def test_the_key_is_the_source_start_so_cuts_do_not_move_it() -> None:
    cue = Cue(2.0, 4.0, "the oak bowl", source_start=12.5)
    assert cue_key(cue) == "12500"
    assert accented(events(render_ass([cue], width=TALL[0], height=TALL[1], settings=on(pick("12500", 1, "oak"))))[0][2]) == ["oak"]
    assert ACCENT not in render_ass([cue], width=TALL[0], height=TALL[1], settings=on(pick("2000", 1, "oak")))


def test_heard_words_split_differently_from_the_text_are_matched_by_occurrence() -> None:
    words = (Word(0.0, 0.2, "the"), Word(0.2, 0.4, "oak,"), Word(0.4, 0.6, "the"), Word(0.6, 0.8, "bowl"), Word(0.8, 1.0, "again"))
    cue = Cue(0.0, 1.0, "the oak, the bowl", words)
    marked = emphasised(cue, [(2, "the")])
    assert [isinstance(word, Punch) for word in marked.words] == [False, False, True, False, False]
    assert emphasised(cue, [(9, "the")]) is cue and emphasised(cue, [(1, "bowl")]) is cue


def test_a_broken_punch_word_keeps_its_emphasis_in_every_piece() -> None:
    fitted = stage("pop", TALL)
    word = "x" * 120
    pages = paginate(emphasised(timed(word, 0.0), [(0, word)]), fitted)
    pieces = [piece for page in pages for piece in page.words]
    assert len(pieces) > 1 and all(isinstance(piece, Punch) for piece in pieces)
    assert "".join(piece.text for piece in pieces) == word


def test_malformed_settings_rows_are_skipped_by_the_renderer() -> None:
    settings = {"style": "boxed", "emphasize": True, "emphasis": [None, {"cue_key": 200}, {"cue_key": "200", "word_index": True, "word": "oak"}, GOOD[0]]}
    assert accented(events(render_ass(story(), width=TALL[0], height=TALL[1], settings=settings))[0][2]) == ["oak"]


# --- A real burn: libass takes every tag and the accent reaches the frame ---


@pytest.fixture
def burns() -> None:
    try:
        if not has_filter("ass"):
            pytest.skip("this ffmpeg has no ass filter")
    except Exception:
        pytest.skip("ffmpeg required")


def accent_pixels(ass: Path, width: int, height: int, seconds: float) -> int:
    """Burn onto black and count pixels in the accent's green, across every frame."""
    out = subprocess.run([str(resolve_ffmpeg()), "-v", "error", "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:r=10:d={seconds}",
                          "-vf", burn_filter(ass) + ",format=rgb24", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True)
    assert out.returncode == 0, out.stderr.decode(errors="replace")[-600:]
    assert out.stderr == b"", out.stderr.decode(errors="replace")[-600:]
    data = out.stdout
    return sum(1 for at in range(0, len(data) - 2, 3) if data[at + 1] > 170 and data[at] < 130 and 80 < data[at + 2] < 200)


@pytest.mark.render
@pytest.mark.parametrize("style", STYLES)
def test_a_real_burn_draws_the_punch_word_in_the_accent(style: str, tmp_path: Path, burns: None) -> None:
    cues = [timed("The finished oak bowl is ready", 0.0, 0.25), timed("It took 24 hours to dry", 1.6, 0.25)]
    settings = on(pick("0", 2, "oak"), pick("1600", 2, "24"), style=style)
    punched = write_styled_ass(cues, tmp_path / "punched.ass", width=480, height=270, settings=settings)
    assert accent_pixels(punched, 480, 270, 3.2) > 200, "libass drew the accent"
    plain = write_styled_ass(cues, tmp_path / "plain.ass", width=480, height=270, settings={"style": style})
    assert accent_pixels(plain, 480, 270, 3.2) == 0
