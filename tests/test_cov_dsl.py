"""The `vedit do` language: flag strings, token lists, and constrained English all become one brief."""

from __future__ import annotations

from pathlib import Path

import pytest

from vedit.compile import OPS
from vedit.dsl import parse_do
from vedit.errors import BriefError


def _ops(brief) -> list[str]:
    return [op["op"] for op in brief.ops]


def test_an_empty_do_string_is_refused() -> None:
    with pytest.raises(BriefError, match="empty do-string"):
        parse_do("   ")
    with pytest.raises(BriefError, match="empty do-string"):
        parse_do([])
    with pytest.raises(BriefError, match="empty do-string"):
        parse_do(["--"])  # a lone separator carries no edit


def test_flags_without_an_input_file_are_refused() -> None:
    with pytest.raises(BriefError, match=r"pass an input file \(positional or --in\)"):
        parse_do("--cut-silence --captions")


def test_the_input_may_come_from_in_or_input_flags() -> None:
    assert parse_do("--in talk.mp4 --captions").sources["main"].endswith("talk.mp4")
    assert parse_do(["--input", "~/clips/talk.mp4"]).sources["main"] == str(Path("~/clips/talk.mp4").expanduser())
    assert parse_do("--in talk.mp4").name == "talk"


def test_every_flag_maps_to_its_op_in_a_fixed_order() -> None:
    brief = parse_do(
        "talk.mp4 --cut-silence --highpass 120 --trim 1.5-4 --title Hi --captions --watermark logo.png "
        "--music bed.mp3 --reframe 9:16 --loudnorm --denoise --fade --preset shorts_9x16 --out final.mp4"
    )
    assert _ops(brief) == [
        "cut_silence",
        "highpass",
        "trim",
        "title",
        "captions",
        "watermark",
        "music",
        "reframe",
        "loudnorm",
        "denoise",
        "fade_in",
        "fade_out",
        "export",
    ]
    assert all(name in OPS for name in _ops(brief))
    by_name = {op["op"]: op for op in brief.ops}
    assert by_name["highpass"]["hz"] == 120
    assert by_name["trim"] == {"op": "trim", "start": 1.5, "end": 4.0}
    assert by_name["title"]["text"] == "Hi"
    assert by_name["watermark"]["source"] == "logo" and brief.sources["logo"].endswith("logo.png")
    assert by_name["music"] == {"op": "music", "source": "music", "duck": True}
    assert by_name["reframe"]["aspect"] == "9:16"
    assert by_name["fade_in"]["duration"] == 0.3 and by_name["fade_out"]["duration"] == 0.5
    assert by_name["export"] == {"op": "export", "presets": ["shorts_9x16"], "path": "final.mp4"}
    assert brief.presets == ["shorts_9x16"]


def test_bare_flags_take_their_defaults_and_aliases_are_honoured() -> None:
    brief = parse_do("talk.mp4 --highpass --silence --subs --normalize")
    by_name = {op["op"]: op for op in brief.ops}
    assert by_name["highpass"]["hz"] == 80
    assert "cut_silence" in by_name and "captions" in by_name and "loudnorm" in by_name
    assert by_name["export"] == {"op": "export", "presets": ["youtube_1080p"], "path": None}, (
        "with no preset or output the default is a 1080p YouTube master"
    )


def test_an_open_ended_trim_keeps_no_end() -> None:
    brief = parse_do("talk.mp4 --trim 2")
    assert brief.ops[0] == {"op": "trim", "start": 2.0, "end": None}


def test_unbalanced_quotes_in_a_flag_string_are_reported_as_a_parse_error() -> None:
    with pytest.raises(BriefError, match="could not parse flags"):
        parse_do("talk.mp4 --title 'Unclosed")


def test_token_lists_keep_multi_word_values_and_drop_a_leading_separator() -> None:
    brief = parse_do(["--", "talk.mp4", "--title", "Two Words", "--out", "my cut.mp4"])
    assert next(op for op in brief.ops if op["op"] == "title")["text"] == "Two Words"
    assert brief.ops[-1]["path"] == "my cut.mp4"


def test_a_token_list_without_flags_or_a_file_suffix_is_read_as_english() -> None:
    brief = parse_do(["cut", "silence", "from", "talk.mp4;", "captions"])
    assert _ops(brief) == ["cut_silence", "captions", "export"]
    assert brief.sources["main"].endswith("talk.mp4")


def test_english_clauses_cover_every_verb() -> None:
    brief = parse_do(
        "trim 2 to 9.5 of talk.mp4; captions; duck bed.mp3; normalize; remove rumble; denoise; fade; "
        'title "Big Day"; export youtube'
    )
    assert _ops(brief) == [
        "trim",
        "captions",
        "music",
        "loudnorm",
        "highpass",
        "denoise",
        "fade_in",
        "fade_out",
        "title",
        "export",
    ]
    by_name = {op["op"]: op for op in brief.ops}
    assert by_name["trim"] == {"op": "trim", "start": 2.0, "end": 9.5}
    assert by_name["title"]["text"] == "Big Day"
    assert by_name["highpass"]["hz"] == 80
    assert brief.sources["music"].endswith("bed.mp3")
    assert brief.presets == ["youtube_1080p"]
    assert by_name["export"]["presets"] == ["youtube_1080p"], "the export clause names the preset of the final op"
    assert brief.name == "talk"


def test_english_export_words_pick_the_preset() -> None:
    assert parse_do("captions for talk.mp4; export shorts").presets == ["shorts_9x16"]
    assert parse_do("captions for talk.mp4; render a preview").presets == ["web_preview"]
    assert parse_do("captions for talk.mp4; export audio only").presets == ["podcast_audio"]
    assert parse_do("captions for talk.mp4; export as mp3").presets == ["podcast_audio"]
    assert parse_do("captions for talk.mp4; render").presets == ["youtube_1080p"]


def test_english_without_an_export_clause_still_ends_in_an_export() -> None:
    brief = parse_do("make talk.mp4 vertical for tiktok")
    assert _ops(brief) == ["reframe", "export"]
    assert brief.ops[0]["aspect"] == "9:16"
    assert brief.ops[-1]["presets"] == ["shorts_9x16"], "a vertical cut exports vertical by default"


def test_a_title_clause_without_quotes_uses_the_whole_clause() -> None:
    brief = parse_do("captions for talk.mp4; lower third for the speaker")
    assert brief.ops[1] == {"op": "title", "text": "lower third for the speaker"}


def test_a_music_clause_needs_an_audio_file() -> None:
    with pytest.raises(BriefError, match="music/duck clause needs an audio file"):
        parse_do("captions for talk.mp4; duck the music")
    brief = parse_do("duck bed.wav under talk.mp4", extra_sources={"music": "ignored.mp3"})
    assert brief.sources["music"].endswith("bed.wav"), "a file named in the clause beats a pre-registered bed"
    assert brief.sources["main"].endswith("bed.wav"), "the first file in the clause is the tape when none came before"


def test_a_pre_registered_music_source_satisfies_a_bare_duck_clause() -> None:
    brief = parse_do("cut silence from talk.mp4; duck the music", extra_sources={"music": "/beds/calm.mp3"})
    assert brief.sources["music"] == "/beds/calm.mp3"
    assert "music" in _ops(brief)


def test_concat_and_slideshow_clauses_collect_files() -> None:
    joined = parse_do("stitch a.mp4 b.mp4 c.mov")
    assert joined.ops[0] == {"op": "concat", "sources": ["a.mp4", "b.mp4", "c.mov"]}
    assert joined.sources["main"].endswith("a.mp4")
    deck = parse_do("slideshow from images one.png two.jpg")
    assert deck.ops[0] == {"op": "slideshow", "images": ["one.png", "two.jpg"]}


def test_an_extra_file_named_without_a_verb_is_ignored_not_invented() -> None:
    brief = parse_do("cut silence from talk.mp4; broll.mp4; captions")
    assert _ops(brief) == ["cut_silence", "captions", "export"]
    assert "broll" not in str(brief.ops)


def test_a_clause_that_means_nothing_is_refused_by_name() -> None:
    with pytest.raises(BriefError, match=r"could not parse clause: 'make it pop'"):
        parse_do("cut silence from talk.mp4; make it pop")


def test_english_with_no_file_anywhere_is_refused() -> None:
    with pytest.raises(BriefError, match="no input file found in do-string"):
        parse_do("cut silence; captions")


def test_english_with_only_extra_sources_uses_the_first_as_the_tape() -> None:
    brief = parse_do("cut silence; captions", extra_sources={"talk": "/clips/a.mp4"})
    assert brief.sources["main"] == "/clips/a.mp4"
    assert brief.name == "edit"


def test_newlines_separate_clauses_like_semicolons() -> None:
    brief = parse_do("cut silence from talk.mp4\ncaptions\nfade")
    assert _ops(brief) == ["cut_silence", "captions", "fade_in", "fade_out", "export"]
