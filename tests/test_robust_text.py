"""Captions, punch words, titles and briefs in any language and script.

Each test pins one fix: Japanese and Thai line breaks, sentence ends outside
Latin, words keyed with their vowel signs, emoji, right-to-left direction in a
mixed line, and a font that can draw the text it is given.
"""

from __future__ import annotations

import os
import re
import subprocess
import unicodedata
from pathlib import Path

import pytest

from vedit import blender_cards, caption_styles, overlays
from vedit.caption_styles import (LOOKS, LRM, RLM, Stage, caption_font, caption_warnings, direction_mark, fallback_faces, font_face,
                                  render_ass, strip_emoji, write_styled_ass)
from vedit.captions import (Cue, Word, burn_filter, cue_key, cues_from_words, heard_cues, interpolate_words, join_words, mend_joined_doubles,
                            polish_cues, split_words, text_width, wrap_cue_text)
from vedit.emphasis import emphasis_lines, suggest_emphasis, validate_emphasis
from vedit.media import filter_value, has_filter, resolve_ffmpeg, resolve_font
from vedit.edit_spec import EditError, validate_edit, validate_motion_card
from vedit.story import _hashtags, _title_from_goal, reads_english, sentences_from_transcript, tokens

JAPANESE = "今日はついにキッチンのシンクを直します。正直に言うと、三時間もかかりました。"
CHINESE = "今天我们终于要修好厨房的水槽了，说实话，这花了整整三个小时。"
THAI = "วันนี้เราจะซ่อมอ่างล้างจานในครัวกันในที่สุด"
HINDI = "आज हम आखिरकार रसोई का सिंक ठीक कर रहे हैं, और सच कहूँ तो इसमें पूरे तीन घंटे लगे।"
ARABIC = "اليوم نصلح أخيرًا حوض المطبخ، وبصراحة استغرق الأمر ثلاث ساعات كاملة."
MIXED = "We tasted 東京 ramen, said مرحبا and नमस्ते, all in one take: 100% real."
SMALL_KANA = set("ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮヵヶー々")


def spoken(line: str) -> str:
    return re.sub(r"(?<!\\)\{[^}]*\}", "", line).replace(r"\N", " ")


def dialogue_text(ass: str) -> list[str]:
    return [line.split(",,", 1)[1].split(",", 4)[-1] for line in ass.splitlines() if line.startswith("Dialogue:")]


def covers(path: Path, text: str) -> bool:
    face = font_face(str(path))
    return face is not None and all(face.advance(char) is not None for char in text if unicodedata.category(char)[0] in "LN")


# --- Word pieces for text written without spaces ---


def test_japanese_pieces_end_where_kana_turns_back_into_a_word() -> None:
    pieces = split_words(JAPANESE)
    assert join_words(pieces) == JAPANESE
    assert "キッチンの" in pieces and "シンクを" in pieces, pieces  # the katakana word is never cut in two
    assert not any(piece[0] in SMALL_KANA for piece in pieces), "a small kana or long-vowel mark never starts a piece"


def test_an_opening_bracket_stays_with_the_word_it_opens() -> None:
    pieces = split_words("iPhone15を買った「本当に」すごい！")
    assert "「本当に」" in pieces and not any(piece.endswith("「") for piece in pieces), pieces


def test_chinese_pieces_are_two_characters_and_keep_their_punctuation() -> None:
    pieces = split_words(CHINESE)
    assert join_words(pieces) == CHINESE
    assert all(text_width(piece.rstrip("，。")) <= 4 for piece in pieces)
    assert not any(piece[0] in "，。" for piece in pieces)


def test_thai_is_cut_between_syllables_never_before_a_following_vowel() -> None:
    pieces = split_words(THAI)
    assert join_words(pieces) == THAI
    for piece in pieces:
        assert unicodedata.category(piece[0])[0] != "M" and piece[0] not in "ะาำๆ", piece
        assert piece[-1] not in "เแโใไ", piece
    assert any(piece.startswith("จะ") for piece in pieces), "จะ stays one syllable (the old cut left ะ alone)"


def test_scripts_with_spaces_keep_their_words_whole() -> None:
    assert split_words(HINDI) == HINDI.split()
    assert split_words("Küchenspülenabflussrohrverstopfung, das") == ["Küchenspülenabflussrohrverstopfung,", "das"]


# --- Widths, wrapping and sentence ends ---


def test_text_width_counts_cjk_as_two_columns_and_marks_as_none() -> None:
    assert text_width("abc") == 3 and text_width("水槽") == 4 and text_width("सिंक") == 2


def test_srt_lines_wrap_text_with_no_spaces() -> None:
    lines = wrap_cue_text(JAPANESE)
    assert len(lines) == 2 and "".join(lines) == JAPANESE
    assert all(text_width(line) <= 42 for line in lines)


def test_cues_close_at_cjk_hindi_and_arabic_sentence_ends() -> None:
    for text in (JAPANESE, "पहला वाक्य। दूसरा वाक्य।", "هل انتهينا؟ نعم انتهينا."):
        cue = interpolate_words(Cue(0.0, 6.0, text))
        lines = cues_from_words(list(cue.words), max_chars=200)
        assert len(lines) == 2, [line.text for line in lines]


def test_a_cjk_cue_is_capped_by_width_not_character_count() -> None:
    cue = interpolate_words(Cue(0.0, 20.0, "あ" * 60))
    assert all(text_width(line.text) <= 42 for line in cues_from_words(list(cue.words)))


def test_polish_keeps_a_finished_japanese_sentence_apart_from_the_next() -> None:
    cues = [Cue(0.0, 2.0, "直します。"), Cue(2.1, 4.0, "正直に言うと、大変でした。")]
    assert [cue.text for cue in polish_cues(cues)] == ["直します。", "正直に言うと、大変でした。"]


def test_polish_does_not_collapse_hindi_words_that_differ_only_by_vowel_sign() -> None:
    assert polish_cues([Cue(0.0, 2.0, "बच्चे की के लिए")])[0].text == "बच्चे की के लिए"
    assert polish_cues([Cue(0.0, 2.0, "the the bowl")])[0].text == "The bowl"


@pytest.mark.parametrize("heard, mended", [("ÜberÜber alles", "Über alles"), ("ЭтоЭто дом", "Это дом"), ("ÉtéÉté chaud", "Été chaud")])
def test_joined_doubles_are_mended_in_any_cased_script(heard: str, mended: str) -> None:
    assert mend_joined_doubles(heard) == mended


@pytest.mark.parametrize("word", ["谢谢", "慢慢来", "Bonbon", "OKOK", "Ölöl"])
def test_reduplication_that_is_a_real_word_stays(word: str) -> None:
    assert mend_joined_doubles(word) == word


def test_a_meta_turn_in_japanese_becomes_lines_at_its_sentence_ends() -> None:
    lines = heard_cues([{"start": 0.0, "end": 6.0, "text": JAPANESE}])
    assert [line.text for line in lines] == ["今日はついにキッチンのシンクを直します。", "正直に言うと、三時間もかかりました。"]


# --- Punch words ---


def test_emphasis_keys_keep_vowel_signs() -> None:
    lines = emphasis_lines([Cue(0.0, 2.0, "का की के")])
    raw = {"picks": [{"cue_key": lines[0]["key"], "word_index": 0, "word": "की", "why": ""}]}
    picks, warnings = validate_emphasis(raw, lines)
    assert picks[0]["word_index"] == 1 and picks[0]["word"] == "की", "a pick at the wrong place moves to the only matching word"
    assert caption_styles.word_key("हैं,") == "हैं"


@pytest.mark.parametrize("text, index", [(JAPANESE, 1), (ARABIC, 4), (HINDI, 5), (THAI, 1)])
def test_a_punch_word_in_any_script_is_drawn_in_the_accent(text: str, index: int) -> None:
    cue = Cue(0.0, 6.0, text)
    word = split_words(text)[index]
    settings = {"style": "bold", "emphasize": True, "emphasis": [{"cue_key": cue_key(cue), "word_index": index, "word": word}]}
    ass = render_ass([cue], width=1080, height=1920, settings=settings)
    assert any(f"\\fscx115\\fscy115}}{word}{{" in line for line in dialogue_text(ass)), word


def test_a_non_english_goal_reaches_the_model_prompt_unchanged(tmp_path: Path) -> None:
    seen: dict[str, str] = {}

    def ask(work_dir: Path, operation: str, **call: object) -> dict[str, object]:
        seen["prompt"] = str(call["prompt"])
        return {"picks": [], "provider": "test"}

    goal = "रसोई का सिंक ठीक करने का छोटा वीडियो"
    result = suggest_emphasis([Cue(0.0, 2.0, HINDI)], goal, "hi", work_dir=tmp_path, ask=ask)
    assert goal in seen["prompt"] and "Hindi" in seen["prompt"] and "[5]सिंक" in seen["prompt"]
    assert result["emphasis"] == []
    suggest_emphasis([Cue(0.0, 2.0, "Hola a todos")], "Un vídeo corto", "es-419", work_dir=tmp_path, ask=ask)  # a region number is a language


# --- Emoji ---


def test_every_part_of_an_emoji_goes_and_joiners_between_letters_stay() -> None:
    assert strip_emoji("fire 🔥🔥 love ❤️ 👍🏽 👨‍👩‍👧 1️⃣ ⭐ 🏴󠁧󠁢󠁳󠁣󠁴󠁿 end") == "fire love 1 end"
    assert strip_emoji("क्‍ष") == "क्‍ष", "a zero-width joiner inside Devanagari is not an emoji"


def test_emoji_leave_no_blank_word_and_a_plain_warning() -> None:
    cue = Cue(0.0, 3.0, "This sauce is fire 🔥🔥 and we love it ❤️",
              tuple(Word(index * 0.3, index * 0.3 + 0.3, text) for index, text in enumerate("This sauce is fire 🔥🔥 and we love it ❤️".split())))
    for style in LOOKS:
        ass = render_ass([cue], width=1080, height=1920, settings={"style": style})
        for line in dialogue_text(ass):
            text = spoken(line)
            assert "🔥" not in text and "❤" not in text and "  " not in text and text.strip()
    warnings = caption_warnings([cue])
    assert len(warnings) == 1 and "🔥" in warnings[0] and "❤️" in warnings[0] and "captions.srt keeps" in warnings[0]


def test_a_punch_word_after_an_emoji_keeps_its_place() -> None:
    text = "🔥 This is huge"
    cue = Cue(0.0, 3.0, text)
    settings = {"style": "boxed", "emphasize": True, "emphasis": [{"cue_key": cue_key(cue), "word_index": 3, "word": "huge"}]}
    assert "}huge{" in render_ass([cue], width=1080, height=1920, settings=settings)


def test_overlay_emoji_warning_names_the_text() -> None:
    warnings = overlays.text_warnings([{"text": "Grand opening 🎉"}, {"text": "No emoji here"}])
    assert warnings == ['The text "Grand opening" had emoji (🎉). The text renderer cannot draw color emoji, so they were left out; the words are unchanged.']


# --- Right to left ---


def test_each_line_of_a_mixed_cue_holds_the_cue_direction() -> None:
    assert direction_mark(MIXED) == LRM and direction_mark(ARABIC) == "" and direction_mark("Hello") == ""
    assert direction_mark("اشتريت iPhone جديد اليوم") == RLM, "an Arabic sentence that opens with a Latin name still reads right to left"
    ass = render_ass([Cue(0.0, 6.0, MIXED)], width=1080, height=1920, settings={"style": "boxed"})
    for line in dialogue_text(ass):
        body = re.sub(r"\{[^}]*\}", "", line)
        assert all(part.startswith(LRM) for part in body.split(r"\N")), body


def test_pure_right_to_left_captions_carry_no_marks() -> None:
    ass = render_ass([Cue(0.0, 6.0, ARABIC)], width=1080, height=1920, settings={"style": "karaoke"})
    assert LRM not in ass and RLM not in ass


def test_a_right_to_left_lower_third_starts_at_the_right() -> None:
    item = {"text": "مطبخ أحمد", "style": "lower_third", "position": "bottom", "start": 0, "duration": 2}
    event = overlays.text_event(item, width=1920, height=1080, end_limit=None, face=None)
    assert event is not None and "\\an3" in event
    assert "\\an1" in overlays.text_event({**item, "text": "Ahmed's kitchen"}, width=1920, height=1080, end_limit=None, face=None)


# --- Fonts ---


@pytest.mark.parametrize("text", [JAPANESE, CHINESE, HINDI, THAI, "오늘은 드디어 주방 싱크대를 고칩니다.", ARABIC, "היום אנחנו מתקנים"])
def test_the_font_for_a_text_can_draw_every_letter_of_it(text: str) -> None:
    path = resolve_font(text)
    if not covers(path, text):
        pytest.skip(f"no installed font covers this script ({path.name})")
    assert covers(path, text)


def test_no_text_or_latin_text_keeps_the_default_font(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VEDIT_FONT", raising=False)
    assert resolve_font() == resolve_font("Hello, world") == resolve_font("Olá, ação, über")


def test_a_chosen_font_wins_when_it_covers_the_text(monkeypatch: pytest.MonkeyPatch) -> None:
    default = resolve_font()
    monkeypatch.setenv("VEDIT_FONT", str(default))
    assert resolve_font("Hello") == default
    other = resolve_font(JAPANESE)
    if covers(default, JAPANESE) or not covers(other, JAPANESE):
        pytest.skip("needs a Latin default and a Japanese font on this machine")
    assert other != default, "a chosen Latin font gives way where it would draw boxes"


def test_letters_the_caption_font_lacks_are_measured_from_the_font_that_draws_them() -> None:
    _family, face, _path = caption_font()
    extra = fallback_faces(HINDI, face)
    if face is None or not extra:
        pytest.skip("needs a Latin caption font and a Devanagari font")
    plain, fitted = Stage(LOOKS["bold"], 100, 1000, face), Stage(LOOKS["bold"], 100, 1000, face, extra)
    assert fitted.measure(HINDI) != plain.measure(HINDI)
    assert all(any(other.advance(char) is not None for other in (face, *extra)) for char in HINDI if unicodedata.category(char)[0] == "L")


def test_a_hero_word_in_japanese_does_not_use_a_latin_only_display_font() -> None:
    family, face, _path = overlays.hero_font("heavy", "台所")
    if face is not None and family == "Arial Black":
        pytest.fail("Arial Black has no Japanese, so the hero word would be measured wrong")


def test_titles_without_libass_get_a_font_that_holds_their_letters(tmp_path: Path) -> None:
    items = [{"text": JAPANESE[:12], "style": "title", "start": 0, "duration": 2}, {"text": "Plain title", "style": "label", "start": 0, "duration": 2}]
    filters = overlays.drawtext_filters(items, work_dir=tmp_path, width=1080, height=1920)
    japanese, plain = resolve_font(JAPANESE[:12]), resolve_font("Plain title")
    assert filter_value(japanese) in filters[0] and filter_value(plain) in filters[1]


# --- Briefs in other languages ---


def test_brief_words_keep_accents_and_vowel_signs() -> None:
    assert tokens("Haz un vídeo sobre la cerámica") == ["haz", "vídeo", "sobre", "cerámica"]
    assert "सिंक" in tokens("रसोई का सिंक ठीक करना") and "का" not in tokens("रसोई का सिंक")
    assert _hashtags("Un vídeo sobre la cerámica", []) == ["#vídeo", "#sobre", "#cerámica"]


def test_a_japanese_title_and_sentences_join_without_spaces() -> None:
    assert " " not in _title_from_goal("キッチンのシンク修理")
    sentences = sentences_from_transcript([{"start": 0, "end": 1, "text": "正直に言うと"}, {"start": 1, "end": 2, "text": "大変でした。"},
                                           {"start": 2, "end": 3, "text": "क्या यह ठीक है?"}, {"start": 3, "end": 4, "text": "हाँ।"}])
    assert [sentence["text"] for sentence in sentences] == ["正直に言うと大変でした。", "क्या यह ठीक है?", "हाँ।"]


# --- Real burns ---


@pytest.fixture
def ffmpeg() -> str:
    try:
        if not has_filter("ass"):
            pytest.skip("this ffmpeg has no ass filter")
        return resolve_ffmpeg()
    except Exception:
        pytest.skip("ffmpeg required")


@pytest.mark.render
def test_captions_burn_from_a_folder_named_with_spaces_quotes_unicode_and_emoji(tmp_path: Path, ffmpeg: str) -> None:
    folder = tmp_path / "my clip's [final], 日本語 été 🎬"
    folder.mkdir()
    ass = write_styled_ass([Cue(0.0, 1.0, MIXED)], folder / "captions.ass", width=320, height=240, settings={"style": "bold"})
    out = folder / "frame 1 🎬.png"
    result = subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=320x240:d=1", "-ss", "0.5",
                             "-vf", burn_filter(ass), "-frames:v", "1", str(out)], capture_output=True, text=True, env={**os.environ})
    assert result.returncode == 0, result.stderr
    assert out.stat().st_size > 0


@pytest.mark.parametrize("text", ["היום סוף סוף מתקנים", "धीरे धीरे चलो", "빨리 빨리 와"])
def test_words_doubled_on_purpose_in_uncased_scripts_stay(text: str) -> None:
    assert polish_cues([Cue(0.0, 2.0, text)])[0].text == text


def test_thai_keeps_the_spaces_between_its_phrases() -> None:
    text = "วันนี้เราจะซ่อมอ่างล้างจาน บอกตรงๆ ว่าใช้เวลาสามชั่วโมง"
    lines = heard_cues([{"start": 0.0, "end": 6.0, "text": text}])
    assert "".join(line.text for line in lines).replace(" ", "") == text.replace(" ", "")
    assert "จาน บอกตรงๆ ว่า" in lines[0].text, "the phrase spaces survive the split into lines"
    assert polish_cues([Cue(0.0, 1.0, "บอกตรงๆ"), Cue(1.1, 2.0, "ว่าใช้เวลา")])[0].text == "บอกตรงๆ ว่าใช้เวลา"


# --- Motion cards, pause levels and the brief's language ---

CARD = {"kind": "intro_title", "text": "台所の修理", "subtitle": "", "style": "soft_clay", "palette": "rose", "duration": 1.5}


def test_a_japanese_card_takes_a_font_that_can_draw_it() -> None:
    try:
        spec = blender_cards.render_spec(validate_motion_card(CARD), width=192, height=108, fps=12, samples=1)
    except blender_cards.MotionCardError as error:
        if "No font on this machine" not in str(error):
            raise
        pytest.skip("no installed font covers Japanese (on Linux, fonts-noto-cjk)")
    font = spec["fonts"]["title"]
    if font is None or not covers(Path(font), CARD["text"]):
        pytest.skip("no installed font covers Japanese")
    assert font not in blender_cards.STYLE_FONTS["soft_clay"], "the Latin style font would draw boxes"
    latin = blender_cards.render_spec(validate_motion_card({**CARD, "text": "Kitchen Fix"}), width=192, height=108, fps=12)
    assert latin["fonts"]["title"] == blender_cards._first_file(blender_cards.STYLE_FONTS["soft_clay"])


def test_a_card_no_installed_font_can_draw_is_refused_plainly(monkeypatch: pytest.MonkeyPatch) -> None:
    import vedit.media

    latin = blender_cards._first_file(blender_cards.STYLE_FONTS["soft_clay"])
    if latin is None:
        pytest.skip("needs a Latin style font")
    monkeypatch.setattr(vedit.media, "resolve_font", lambda text="": Path(latin))
    with pytest.raises(blender_cards.MotionCardError, match="No font on this machine can draw the card text"):
        blender_cards.render_spec(validate_motion_card(CARD), width=192, height=108, fps=12)


def test_card_titles_in_japanese_break_into_lines() -> None:
    lines = blender_cards.title_lines("キッチンのシンクを直す日", width=1080, height=1920)
    assert len(lines) >= 2 and "".join(lines) == "キッチンのシンクを直す日"


def test_the_card_prompt_names_the_language_and_assumes_no_subject() -> None:
    call = {"goal": "台所", "spoken": "", "duration": 10.0, "aspect": "9:16", "kind": None, "backdrop": False}
    prompt = blender_cards._prompt(language="ja", **call)
    assert "woodwork" not in prompt and "in Japanese" in prompt
    assert "in the language the transcript is spoken in" in blender_cards._prompt(language="auto", **call)


@pytest.mark.skipif(not blender_cards.blender_available(), reason="Blender is not installed")
def test_blender_draws_japanese_card_text_with_the_chosen_font(tmp_path: Path) -> None:
    import json

    spec = blender_cards.render_spec(validate_motion_card(CARD), width=192, height=108, fps=12, samples=1)
    spec["duration"] = 0.5  # the template's shortest card: a few frames are enough to see the letters
    (tmp_path / "frames").mkdir()
    (tmp_path / "spec.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    result = subprocess.run([blender_cards.blender_path(), "-b", "--factory-startup", "--python-exit-code", "1", "--python", str(blender_cards.TEMPLATE),
                             "--", str(tmp_path / "spec.json"), str(tmp_path / "frames")], capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stderr[-800:]
    drawn = json.loads((tmp_path / "frames" / "card.json").read_text(encoding="utf-8"))
    assert drawn.get("title_body", CARD["text"]).replace("\n", "") == CARD["text"]
    last = sorted((tmp_path / "frames").glob("frame_*.png"))[-1]
    alpha = subprocess.run([resolve_ffmpeg(), "-v", "error", "-i", str(last), "-vf", "alphaextract,format=gray", "-f", "rawvideo", "-"],
                           check=True, capture_output=True).stdout
    assert len(alpha) == 192 * 108 and max(alpha) > 200


def test_the_local_planner_lets_silence_detection_place_its_own_line(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from types import SimpleNamespace

    from vedit.story import plan_local_story

    asked: list[dict[str, object]] = []
    monkeypatch.setattr("vedit.story.probe", lambda _source: SimpleNamespace(has_video=True, duration=30.0, has_audio=True, audio_map=1))
    monkeypatch.setattr("vedit.story.detect_scenes_fast", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("vedit.story.detect_silence", lambda *_args, **kwargs: asked.append(kwargs) or [])
    monkeypatch.setattr("vedit.story.audible", lambda *_args, **_kwargs: True)
    plan_local_story(Path("quiet phone clip.mp4"), goal="Haz un vídeo corto", target_duration=8, work_dir=tmp_path)
    assert asked and all("threshold_db" not in call and call.get("track") == 1 for call in asked), asked


def test_english_hints_are_not_applied_to_other_languages() -> None:
    assert reads_english("Make a 20-second story about the oak bowl")
    assert not reads_english("Haz un vídeo a la playa con mi perro") and not reads_english("Mach ein Video an der Küste")
    assert _title_from_goal("Haz un vídeo a la playa con mi perro") == "Haz un vídeo a la playa con mi perro"
    assert _title_from_goal("Haz un vídeo", [{"start": 0, "end": 1, "text": "Hoy vamos a la playa."}]) == "Hoy vamos a la playa"


def test_the_cutout_example_names_no_one_subject() -> None:
    with pytest.raises(EditError, match="the person on the left"):
        validate_edit({"cutout": {"mode": "blur", "subject": ""}}, duration=10.0)


# --- Phrases said twice ---


@pytest.mark.parametrize("cues, expected", [
    (["this is the bowl the bowl we made"], ["This is the bowl we made"]),
    (["this is the bowl", "the bowl we made"], ["This is the bowl we made"]),  # across a turn edge, as Meta splits them
    (["THE BOWL the bowl."], ["THE BOWL."]),  # case-insensitive; the closing punctuation is kept
    (["dijo que que vendría"], ["Dijo que vendría"]),  # a Spanish stutter collapses like "the the"
    (["It is the bowl. The bowl is oak."], ["It is the bowl. The bowl is oak."]),  # a repeat across a sentence end is meant
    (["the bowl, the bowl, the bowl"], ["The bowl, the bowl, the bowl"]),  # so is a list said for effect
    (["谢谢 谢谢 你好你好"], ["谢谢 谢谢 你好你好"]),  # Chinese doubles words on purpose, with or without spaces
    (["היום סוף סוף מתקנים"], ["היום סוף סוף מתקנים"]),
])
def test_a_phrase_said_twice_in_a_row_is_burned_once(cues: list[str], expected: list[str]) -> None:
    assert [cue.text for cue in polish_cues([Cue(index, index + 1, text) for index, text in enumerate(cues)])] == expected


def test_a_collapsed_phrase_keeps_its_words_and_time() -> None:
    words = tuple(Word(index * 0.3, index * 0.3 + 0.3, text) for index, text in enumerate("look at the bowl the bowl now".split()))
    cue = polish_cues([Cue(0.0, 2.1, "look at the bowl the bowl now", words)])[0]
    assert cue.text == "Look at the bowl now" and [word.text for word in cue.words] == ["Look", "at", "the", "bowl", "now"]
    assert cue.words[3].end == pytest.approx(1.8), "the kept 'bowl' covers the copy that went"


def test_phrases_in_different_story_moments_never_collapse() -> None:
    cues = [Cue(0.0, 1.0, "the bowl", segment=0), Cue(1.0, 2.0, "the bowl", segment=1)]
    assert [cue.text for cue in polish_cues(cues)] == ["The bowl", "The bowl"]


# --- Lines on the output clock, and where sentences start ---


def test_a_line_continuing_a_sentence_keeps_its_lowercase_start() -> None:
    cues = [Cue(0.0, 1.0, "I put the blank"), Cue(3.0, 4.0, "on the lathe."), Cue(5.0, 6.0, "then it spins.")]
    assert [cue.text for cue in polish_cues(cues)] == ["I put the blank", "on the lathe.", "Then it spins."]


def test_a_wordless_line_whose_head_was_cut_shows_only_its_tail() -> None:
    from vedit.timeline import Span, map_cues

    line = {"start": 0.0, "end": 4.0, "text": "one two three four five six seven eight nine ten"}
    kept = [Span(1, 3.0, 4.0, 0.0, 1.0)]  # the last quarter of the line survives
    moved = map_cues([line], kept)
    assert len(moved) == 1 and (moved[0]["start"], moved[0]["end"]) == (0.0, 1.0), "timing is the clock's, unchanged"
    assert moved[0]["text"] == "eight nine ten"
    assert map_cues([line], [Span(1, 3.95, 4.0, 0.0, 0.05)]) == [], "a sliver too short for a word shows nothing"
    assert map_cues([line], [Span(1, 0.0, 4.0, 0.0, 4.0)])[0]["text"] == line["text"]


def test_a_wordless_line_split_between_reordered_moments_keeps_each_part_with_its_moment() -> None:
    from vedit.timeline import Span, map_cues

    line = {"start": 0.0, "end": 2.0, "text": "first half here second half there"}
    moved = map_cues([line], [Span(1, 1.0, 2.0, 0.0, 1.0), Span(2, 0.0, 1.0, 5.0, 6.0)])  # the story plays the tail first
    assert [(cue["segment"], cue["text"]) for cue in moved] == [(1, "second half there"), (2, "first half here")]


def test_japanese_words_rejoin_on_the_output_clock_without_spaces() -> None:
    from vedit.timeline import Span, map_cues

    cue = interpolate_words(Cue(0.0, 2.0, "今日はついにキッチンの"))
    record = {"start": 0.0, "end": 2.0, "text": cue.text, "words": [{"start": w.start, "end": w.end, "text": w.text} for w in cue.words]}
    assert map_cues([record], [Span(1, 0.0, 2.0, 0.0, 2.0)])[0]["text"] == "今日はついにキッチンの"
