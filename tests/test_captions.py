import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

import vedit.campaign
import vedit.campaign_plan
from vedit import meta
from vedit.analyze import Range
from vedit.campaign import CampaignManager
from vedit.captions import (
    MAX_WORDS,
    Cue,
    Word,
    build_caption_file,
    concat_cues,
    cue_key,
    format_ts,
    heard_cues,
    interpolate_words,
    join_words,
    parse_ts,
    polish_cues,
    preview_cues,
    read_srt,
    remap_cues,
    source_cues,
    story_cues,
    strip_annotations,
    transcribe,
    word_records,
    write_srt,
)
from vedit.ir import Canvas
from vedit.media import has_filter, probe
from vedit.render_plan import context as render_context
from vedit.timeline import concat_spans, concat_to_source, plan_timeline


def test_timestamp_roundtrip():
    assert parse_ts(format_ts(3661.5)) == 3661.5
    assert format_ts(1.5).startswith("00:00:01,500")


def test_srt_roundtrip(tmp_path: Path):
    cues = [Cue(0.0, 1.2, "Hello"), Cue(1.2, 2.4, "world")]
    path = tmp_path / "c.srt"
    write_srt(cues, path)
    back = read_srt(path)
    assert len(back) == 2
    assert back[0].text == "Hello"
    assert back[1].end == 2.4


def test_remap_after_silence_cut():
    cues = [Cue(0.2, 0.8, "hi"), Cue(2.1, 2.5, "there")]
    keeps = [Range(0.0, 1.0), Range(2.0, 3.0)]
    remapped = remap_cues(cues, keeps)
    assert remapped[0].start == 0.2
    assert remapped[1].start == pytest.approx(1.1)


def test_write_srt_without_cues_is_still_probeable(tmp_path):
    """A silent clip must not produce an empty .srt that the subtitles filter rejects."""
    from vedit.captions import read_srt, write_srt

    path = tmp_path / "captions.srt"
    write_srt([], path)
    text = path.read_text(encoding="utf-8")
    assert text.startswith("1\n00:00:00,000 --> 00:00:00,001\n")
    assert len(read_srt(path)) == 1
    assert read_srt(path)[0].text.strip("\u200b ") == ""


def test_ass_timestamps_use_centiseconds_and_keep_commas(tmp_path):
    """libass reads H:MM:SS.cc; a millisecond fraction shifts every cue late."""
    from vedit.captions import Cue, format_ts, write_ass

    assert format_ts(0.38, srt=False) == "0:00:00.38"
    assert format_ts(3.5, srt=False) == "0:00:03.50"
    assert format_ts(3661.999, srt=False) == "1:01:02.00"
    assert format_ts(0.38) == "00:00:00,380"
    path = tmp_path / "c.ass"
    write_ass([Cue(0.38, 3.5, "Oiled, and ready.")], path)
    dialogue = [line for line in path.read_text().splitlines() if line.startswith("Dialogue:")]
    assert dialogue == ["Dialogue: 0,0:00:00.38,0:00:03.50,Default,,0,0,0,,Oiled, and ready."]


def test_polish_collapses_doubled_words_within_and_across_cues():
    from vedit.captions import polish_cues

    cues = [
        Cue(16.0, 18.8, "on the lathe the the shape appears in long steady cuts."),
        Cue(24.0, 26.4, "Sanding goes from coarse to fine with the lathe"),
        Cue(26.5, 29.0, "lathe turning slowly."),
    ]
    out = polish_cues(cues)
    assert [c.text for c in out] == [
        "On the lathe the shape appears in long steady cuts.",
        "Sanding goes from coarse to fine with the lathe turning slowly.",
    ]
    assert (out[1].start, out[1].end) == (24.0, 29.0)
    # Case-insensitive, and the copy carrying punctuation survives.
    assert polish_cues([Cue(0, 1, "The the lathe lathe, turning")])[0].text == "The lathe, turning"
    # A cue that only repeats the previous word gives its time to that word.
    out = polish_cues([Cue(0, 1, "the lathe"), Cue(1, 1.4, "lathe")])
    assert out == [Cue(0, 1.4, "The lathe")]


def test_polish_merges_fragments_into_whole_sentences():
    from vedit.captions import polish_cues

    cues = [
        Cue(0.0, 2.69, "This is the finished oak bowl, oiled and"),
        Cue(2.69, 5.0, "ready for the table."),
        Cue(5.0, 7.44, "It started as a rough blank from a fallen"),
        Cue(7.44, 8.0, "tree."),
        Cue(8.0, 11.2, "Here is the raw oak blank, still wet and"),
        Cue(11.2, 12.0, "heavy."),
    ]
    out = polish_cues(cues)
    assert [c.text for c in out] == [
        "This is the finished oak bowl, oiled and ready for the table.",
        "It started as a rough blank from a fallen tree.",
        "Here is the raw oak blank, still wet and heavy.",
    ]
    assert [(c.start, c.end) for c in out] == [(0.0, 5.0), (5.0, 8.0), (8.0, 12.0)]


def test_polish_respects_gap_and_length_limits_and_short_trailers():
    from vedit.captions import polish_cues

    # A sentence that already ended stays separate from a full next sentence.
    out = polish_cues([Cue(0, 1, "Made by hand."), Cue(1, 2, "The finished bowl.")])
    assert [c.text for c in out] == ["Made by hand.", "The finished bowl."]
    # ...but a tiny trailing cue folds in when it fits.
    out = polish_cues([Cue(0, 1, "Made by hand."), Cue(1.1, 1.5, "Really.")])
    assert out == [Cue(0, 1.5, "Made by hand. Really.")]
    # A pause longer than max_gap blocks merging even for an open-ended cue.
    out = polish_cues([Cue(0, 1, "Made by hand and"), Cue(1.6, 2, "oiled.")])
    assert [c.text for c in out] == ["Made by hand and", "oiled."]  # the same sentence goes on, so no capital
    assert [(c.start, c.end) for c in out] == [(0, 1), (1.6, 2)]
    # The joined text must fit max_chars.
    long_a = "the raw oak blank sits wet and heavy on the bench by the door"
    out = polish_cues([Cue(0, 1, long_a), Cue(1, 2, "and more words here")], max_chars=60)
    assert len(out) == 2
    out = polish_cues([Cue(0, 1, long_a), Cue(1, 2, "and more words here")], max_chars=84)
    assert len(out) == 1


def test_polish_normalizes_whitespace_capitalizes_and_drops_empty_cues():
    from vedit.captions import polish_cues

    cues = [
        Cue(0, 0.5, "   "),
        Cue(0.5, 1.0, "\tthis  is\n the   bowl."),
        Cue(1.0, 1.2, ""),
        Cue(2.0, 3.0, "24 hours later, it is ready."),
    ]
    out = polish_cues(cues)
    assert out == [Cue(0.5, 1.0, "This is the bowl."), Cue(2.0, 3.0, "24 hours later, it is ready.")]
    assert polish_cues([]) == []
    assert polish_cues([Cue(0, 1, '"quoted" start')])[0].text == '"Quoted" start'


def test_wrap_breaks_long_cues_into_two_balanced_lines(tmp_path: Path):
    from vedit.captions import wrap_cue_text, write_ass

    short = "Oiled, and ready."
    assert wrap_cue_text(short) == [short]
    assert wrap_cue_text("This is the finished oak bowl, oiled and ready for the table.") == [
        "This is the finished oak bowl,",
        "oiled and ready for the table.",
    ]
    lines = wrap_cue_text("It started as a rough blank from a fallen tree in the yard")
    assert len(lines) == 2 and all(len(line) <= 42 for line in lines)
    assert " ".join(lines) == "It started as a rough blank from a fallen tree in the yard"
    assert wrap_cue_text("already\nbroken") == ["already", "broken"]
    # Burn-in writers use the wrap: SRT gets a newline, ASS gets \N, timing untouched.
    srt = tmp_path / "c.srt"
    cue = Cue(0.38, 3.5, "This is the finished oak bowl, oiled and ready for the table.")
    write_srt([cue], srt)
    assert "oak bowl,\noiled and" in srt.read_text()
    assert read_srt(srt)[0] == Cue(0.38, 3.5, cue.text)
    ass = tmp_path / "c.ass"
    write_ass([cue], ass)
    dialogue = [line for line in ass.read_text().splitlines() if line.startswith("Dialogue:")]
    assert dialogue == [
        r"Dialogue: 0,0:00:00.38,0:00:03.50,Default,,0,0,0,,This is the finished oak bowl,\Noiled and ready for the table."
    ]


def test_meta_shaped_cues_with_doubled_words_come_out_clean():
    """The same conversion campaign._meta_captions applies to Meta Voice Transcribe output."""
    from vedit.captions import polish_cues

    result = {
        "cues": [
            {"start": 16.0, "end": 18.8, "text": "on the lathe the the shape appears in long,"},
            {"start": 18.8, "end": 20.0, "text": "steady cuts."},
            {"start": 24.0, "end": 26.4, "text": "sanding goes from coarse to fine, with the lathe"},
            {"start": 26.4, "end": 29.0, "text": "lathe turning slowly."},
        ],
        "provider": "meta",
    }
    cues = polish_cues([Cue(float(c["start"]), float(c["end"]), str(c["text"])) for c in result["cues"]])
    assert cues == [
        Cue(16.0, 20.0, "On the lathe the shape appears in long, steady cuts."),
        Cue(24.0, 29.0, "Sanding goes from coarse to fine, with the lathe turning slowly."),
    ]


def test_interpolated_words_share_the_cue_by_length_and_say_so() -> None:
    record = interpolate_words({"start": 2.0, "end": 4.0, "text": "aa bbbbbb"})
    assert record["estimated"] is True and all(word["estimated"] for word in record["words"])
    assert [(word["start"], word["end"]) for word in record["words"]] == [(2.0, 2.5), (2.5, 4.0)]
    cue = interpolate_words(Cue(0.0, 1.0, "one two"))
    assert isinstance(cue, Cue) and cue.words[1].end == 1.0 and all(word.estimated for word in cue.words)


def test_interpolation_splits_text_without_spaces_and_rejoins_it() -> None:
    text = "木のボウルは粗いオークの塊から始まります。"
    cue = interpolate_words(Cue(0.0, 2.0, text))
    assert len(cue.words) > 1 and join_words(word.text for word in cue.words) == text
    assert not any(word.text[0] == "。" for word in cue.words), "closing punctuation stays on its word"


def test_polish_keeps_words_aligned_with_the_text() -> None:
    words = lambda *rows: tuple(Word(*row) for row in rows)  # noqa: E731
    first = Cue(
        0.0, 2.0, "on the the lathe", words((0.0, 0.3, "on"), (0.3, 0.5, "the"), (0.5, 0.7, "the"), (0.7, 2.0, "lathe"))
    )
    second = Cue(2.0, 3.0, "lathe, turning.", words((2.0, 2.4, "lathe,"), (2.4, 3.0, "turning.")))
    out = polish_cues([first, second])
    assert [cue.text for cue in out] == ["On the lathe, turning."]
    assert [word.text for word in out[0].words] == ["On", "the", "lathe,", "turning."]
    assert out[0].words[1].end == 0.7 and out[0].words[2].end == 2.4, (
        "a dropped copy gives its time to the one that stays"
    )


def test_source_cues_either_side_of_a_cut_stay_apart() -> None:
    """Real case: the tail of one moment plus the start of the next made "...from a fallen to be."."""
    plan = {
        "segments": [{"start": 0.0, "end": 2.0}, {"start": 10.0, "end": 12.0}],
        "edit": {},
        "analysis": {
            "transcript": [
                {"start": 1.0, "end": 3.0, "text": "It started from a fallen tree."},
                {"start": 9.0, "end": 10.5, "text": "where it wants to be."},
            ],
            "words": [
                {"start": 1.0, "end": 1.4, "text": "It"},
                {"start": 1.4, "end": 1.9, "text": "started"},
                {"start": 2.5, "end": 3.0, "text": "tree."},
                {"start": 9.0, "end": 9.5, "text": "where"},
                {"start": 10.0, "end": 10.4, "text": "to"},
                {"start": 10.4, "end": 10.5, "text": "be."},
            ],
        },
    }
    assert [cue.text for cue in story_cues(plan)] == ["It started", "To be."]


def test_polish_never_joins_two_story_moments() -> None:
    cues = [Cue(0, 1, "It started as a rough", segment=1), Cue(1.1, 1.3, "to be.", segment=2)]
    assert [cue.text for cue in polish_cues(cues)] == ["It started as a rough", "To be."]
    doubled = [Cue(0, 1, "the", segment=1), Cue(1, 2, "the end", segment=2)]
    assert [cue.text for cue in polish_cues(doubled)] == ["The", "The end"]


def test_word_records_are_bounded_clamped_and_always_have_length() -> None:
    records = word_records(
        [
            Word(1.0, 1.0, "tick"),
            Word(9.0, 12.0, "over"),
            Word(11.0, 12.0, "after"),
            {"start": 2, "end": 3, "text": "bell\x07"},
            {"start": "x", "end": 1, "text": "bad"},
        ],
        10.0,
    )
    assert [record["text"] for record in records] == ["tick", "over", "bell"]
    assert records[0]["end"] > records[0]["start"] and records[1]["end"] == 10.0
    assert len(word_records([Word(0, 1, "w")] * (MAX_WORDS + 5))) == MAX_WORDS


def test_transcript_cleaning_drops_notes_and_phantoms() -> None:
    assert strip_annotations("[Ding] well (laughs) okay ♪") == "well okay"
    analysis = {
        "transcript": [
            {"start": 0, "end": 2, "text": "You"},
            {"start": 3, "end": 5, "text": "(upbeat music)"},
            {"start": 6, "end": 8, "text": "Real words."},
            {"start": 9, "end": 12, "text": "quiet but long enough to be real"},
        ],
        "speech": [{"start": 6, "end": 8}],
    }
    assert [cue.text for cue in source_cues(analysis)] == ["Real words.", "quiet but long enough to be real"]


def test_lines_are_rebuilt_from_words_when_the_transcript_is_missing() -> None:
    words = [
        {"start": 0.0, "end": 0.3, "text": "Done."},
        {"start": 2.0, "end": 2.3, "text": "Next"},
        {"start": 4.0, "end": 4.3, "text": "line"},
    ]
    assert [cue.text for cue in source_cues({"words": words})] == ["Done.", "Next", "line"]


# One caption path.

WORDS = [
    (0.5, 0.9, "First"),
    (1.0, 1.4, "words"),
    (2.2, 2.6, "gone"),
    (3.0, 3.4, "stay"),
    (3.6, 3.9, "here."),
    (6.2, 6.6, "Second"),
    (6.8, 7.2, "moment"),
    (7.4, 7.8, "now."),
]


def clip_plan(**edit) -> dict:
    """Two moments: the first at double speed with a cut inside it, then a crossfade."""
    return {
        "segments": [{"start": 0.0, "end": 4.0, "speed": 2.0}, {"start": 6.0, "end": 8.0}],
        "analysis": {
            "transcript": [
                {"start": 0.5, "end": 3.9, "text": "First words gone stay here."},
                {"start": 6.2, "end": 7.8, "text": "Second moment now."},
            ],
            "words": [{"start": start, "end": end, "text": text} for start, end, text in WORDS],
            "speech": [{"start": 0.0, "end": 8.0}],
        },
        "edit": {"removed": [[2.0, 2.8]], "transition": {"type": "fade", "seconds": 0.4}, **edit},
    }


def test_cues_follow_keep_speed_and_transition_onto_the_output_clock() -> None:
    plan = clip_plan()
    view = plan_timeline(plan)
    assert view["transition_seconds"] == pytest.approx(0.4) and view["duration"] == pytest.approx(3.2)
    first, second = story_cues(plan, view)
    assert first.text == "First words stay here." and second.text == "Second moment now."
    by_word = {word.text: word for word in (*first.words, *second.words)}
    assert "gone" not in by_word, "the removed moment takes its word with it"
    assert by_word["First"].start == pytest.approx(0.25) and by_word["words"].start == pytest.approx(0.5), (
        "double speed halves source time"
    )
    assert by_word["stay"].start == pytest.approx(1.1), "after the cut, 3.0 s plays 0.2 s of source past 2.8 s"
    assert by_word["Second"].start == pytest.approx(1.4), "the second moment starts 0.4 s early, under the crossfade"
    assert first.end == second.start, "the outgoing line gives way, so two captions never stack in the overlap"
    assert (first.segment, second.segment) == (1, 2)


def test_turn_level_cues_cross_the_concat_clock_to_the_output_clock() -> None:
    plan = clip_plan()
    view = plan_timeline(plan)
    # The WAV holds 0-2, then 2.8-4, then 6-8 end to end at normal speed.
    assert sum(span.source_end - span.source_start for span in view["spans"]) == pytest.approx(5.2)
    heard = [
        Cue(0.5, 1.5, "hello there."),
        Cue(2.5, 3.0, "second piece."),
        Cue(3.7, 4.7, "the third moment of this story."),
    ]
    first, second, third = concat_cues(heard, plan, view)
    assert (first.start, first.end) == (pytest.approx(0.25), pytest.approx(0.75))
    assert second.start == pytest.approx(1.25) and second.end == third.start, (
        "a short gap is held so the line does not blink"
    )
    assert (third.start, third.end) == (pytest.approx(1.7), pytest.approx(2.7))
    assert [cue_key(cue) for cue in (first, second, third)] == ["500", "3300", "6500"], (
        "edits key on the source clock, whichever clock the recognizer heard"
    )
    assert all(word.estimated for cue in (first, second, third) for word in cue.words), "turns have no real word timing"


def test_a_turn_heard_across_a_cut_stays_one_line() -> None:
    """The recognizer heard the kept audio in playback order, so this is one sentence as the viewer hears it."""
    plan = clip_plan()
    [cue] = concat_cues([Cue(2.2, 4.2, "words on both sides of it")], plan, plan_timeline(plan))
    assert cue.text == "Words on both sides of it"
    assert [word.text.lower() for word in cue.words] == ["words", "on", "both", "sides", "of", "it"], (
        "a word across the cut stays too"
    )
    assert all(word.end >= word.start for word in cue.words)


def test_concat_spans_keep_the_placement_and_lay_the_audio_end_to_end() -> None:
    spans = plan_timeline(clip_plan())["spans"]
    laid = concat_spans(spans)
    assert [(span.source_start, span.source_end) for span in laid] == [
        (0.0, 2.0),
        (2.0, pytest.approx(3.2)),
        (pytest.approx(3.2), pytest.approx(5.2)),
    ]
    assert [(span.output_start, span.speed, span.segment) for span in laid] == [
        (span.output_start, span.speed, span.segment) for span in spans
    ]
    assert (
        concat_to_source(spans, 2.5) == pytest.approx(3.3)
        and concat_to_source(spans, 3.7) == pytest.approx(6.5)
        and concat_to_source(spans, 99) == 8.0
    )


def test_edits_are_keyed_by_the_source_start_and_estimate_their_word_times() -> None:
    cues = story_cues(clip_plan())
    keys = [cue_key(cue) for cue in cues]
    assert keys == ["500", "6200"]
    edited = story_cues(clip_plan(captions={"edits": {"6200": "A better second line"}}))
    assert edited[0].text == cues[0].text and edited[1].text == "A better second line"
    assert (edited[1].start, edited[1].end) == (cues[1].start, cues[1].end) and [
        word.text for word in edited[1].words
    ] == ["A", "better", "second", "line"]
    assert all(word.estimated for word in edited[1].words) and not any(word.estimated for word in edited[0].words)
    assert [cue.text for cue in story_cues(clip_plan(captions={"edits": {"1400": "Output clock key"}}))] == [
        cue.text for cue in cues
    ], "the output time is not the key"
    assert [cue.text for cue in story_cues(clip_plan(captions={"edits": {"6200": ""}}))] == [cues[0].text], (
        "an emptied line is gone"
    )


def test_edit_keys_survive_a_change_to_the_cut() -> None:
    """A key written against one cut still finds its line after the edit changes."""
    first = cue_key(story_cues(clip_plan())[1])
    tighter = clip_plan(captions={"edits": {first: "Same line"}})
    tighter["segments"][1]["start"] = 7.0
    assert story_cues(tighter)[1].text == "Same line"


def test_a_removed_filler_leaves_the_sound_and_the_caption() -> None:
    plan = {
        "segments": [{"start": 0.0, "end": 3.0}],
        "analysis": {
            "transcript": [{"start": 0.1, "end": 2.6, "text": "So, um, I think."}],
            "words": [
                {"start": 0.1, "end": 0.4, "text": "So,"},
                {"start": 0.6, "end": 1.1, "text": "um,"},
                {"start": 1.1, "end": 1.3, "text": "I"},
                {"start": 1.3, "end": 2.6, "text": "think."},
            ],
        },
    }
    assert story_cues({**plan, "edit": {"remove_fillers": False}})[0].text == "So, um, I think."
    removed = {**plan, "edit": {"remove_fillers": True}}
    view = plan_timeline(removed)
    cues = story_cues(removed, view)
    assert [cue.text for cue in cues] == ["So, I think."]
    assert view["duration"] == pytest.approx(3.0 - 0.54, abs=0.01), "the audio shortens by the same word"
    assert cues[0].words[1].start == pytest.approx(0.6 - 0.02 + 0.02 + 0.0, abs=0.03), "and what follows moves up"


def caption_context(plan: dict, tmp_path: Path, **request):
    work = tmp_path / "project" / "render-1"
    work.mkdir(parents=True, exist_ok=True)
    return render_context(
        plan,
        request={"language": "en", "budget_usd": 0.25, **request},
        aspect="9:16",
        canvas=Canvas(width=540, height=960, fps=30),
        work=work,
        has_audio=True,
    )


def test_preview_is_exactly_what_the_file_burns(tmp_path: Path) -> None:
    plan = clip_plan(captions={"style": "karaoke", "edits": {"6200": "Edited line now"}})
    ctx = caption_context(plan, tmp_path)
    shown = preview_cues(plan, ctx.view)
    subtitle = build_caption_file(plan, ctx.view, ctx)
    assert subtitle == ctx.work / "captions.ass" and subtitle.is_file()
    burned = read_srt(ctx.work / "captions.srt")
    assert [(cue.start, cue.end, cue.text) for cue in burned] == [
        (item["start"], item["end"], item["text"]) for item in shown
    ]
    assert [item["key"] for item in shown] == ["500", "6200"] and shown[0]["source_start"] == 0.5
    assert shown[1]["estimated"] is True and "estimated" not in shown[0]
    assert "Edited" in subtitle.read_text(encoding="utf-8") and r"\c&H" in subtitle.read_text(encoding="utf-8"), (
        "the chosen style reached the file"
    )


def test_no_transcript_means_no_file_and_no_model_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        meta, "transcribe_audio", lambda *a, **k: pytest.fail("a transcript in the plan needs no speech model")
    )
    plan = {"segments": [{"start": 0.0, "end": 2.0}], "analysis": {"transcript": []}, "edit": {}}
    ctx = caption_context(plan, tmp_path)
    assert build_caption_file(plan, ctx.view, ctx) is None and preview_cues(plan, ctx.view) == []


def test_plans_saved_before_word_timing_still_caption() -> None:
    plan = {
        "segments": [{"start": 1.0, "end": 3.0}],
        "analysis": {"transcript": [{"start": 1.2, "end": 2.2, "text": "Old plan."}]},
    }
    cues = preview_cues(plan)
    assert [cue["text"] for cue in cues] == ["Old plan."] and cues[0]["key"] == "1200" and "words" not in cues[0]


class TranslationHttp:
    """Only the provider's HTTP boundary is replaced."""

    def __init__(self, *lines: str) -> None:
        self.lines, self.posts = lines, 0

    def __call__(self, method: str, path: str, **_kwargs) -> dict:
        if path == "/responses/input_tokens":
            return {"input_tokens": 400}
        assert (method, path) == ("POST", "/responses")
        self.posts += 1
        lines = [{"index": index, "text": text} for index, text in enumerate(self.lines, 1)]
        return {
            "status": "completed",
            "usage": {"input_tokens": 400, "output_tokens": 90},
            "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps({"lines": lines})}]}],
        }


def meta_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_API_KEY", "test-secret-never-write-to-disk")
    monkeypatch.setenv("VEDIT_META_ENABLED", "1")


def test_translated_lines_burn_and_both_languages_are_delivered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta_on(monkeypatch)
    http = TranslationHttp("Primeras palabras aquí.", "Segundo momento ahora.")
    monkeypatch.setattr(meta, "_request", http)
    plan = clip_plan(captions={"style": "karaoke", "language": "es"})
    ctx = caption_context(plan, tmp_path)
    subtitle = build_caption_file(plan, ctx.view, ctx)
    english, spanish = read_srt(ctx.work / "captions.srt"), read_srt(ctx.work / "captions.es.srt")
    assert [cue.text for cue in spanish] == ["Primeras palabras aquí.", "Segundo momento ahora."]
    assert [(cue.start, cue.end) for cue in spanish] == [(cue.start, cue.end) for cue in english], (
        "same cues, same times"
    )
    assert english[0].text == "First words stay here."
    burned = subtitle.read_text(encoding="utf-8")
    assert "palabras" in burned and "stay" not in burned
    assert http.posts == 1
    ledger = json.loads((ctx.work.parent / "meta" / "meta-usage.json").read_text())
    assert [call["operation"] for call in ledger["calls"]] == ["translate"]


def test_the_language_already_spoken_costs_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(meta, "_request", lambda *a, **k: pytest.fail("nothing to translate"))
    plan = clip_plan(captions={"language": "en"})
    ctx = caption_context(plan, tmp_path)
    assert build_caption_file(plan, ctx.view, ctx) is not None and not list(ctx.work.glob("captions.*.srt"))


def test_translation_refuses_plainly_without_meta(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.delenv("VEDIT_META_ENABLED", raising=False)
    monkeypatch.setattr(meta, "_request", lambda *a, **k: pytest.fail("no provider call without Meta"))
    plan = clip_plan(captions={"language": "es"})
    ctx = caption_context(plan, tmp_path)
    with pytest.raises(meta.MetaError, match="Translated captions use Meta"):
        build_caption_file(plan, ctx.view, ctx)
    assert not (ctx.work / "captions.ass").exists(), "no half-translated file is left to burn"


# Through the campaign: a real render on a tiny clip.


@pytest.fixture
def tiny_clip(tmp_path: Path) -> Path:
    source = tmp_path / "vedit-sessions" / "source-test" / "inbox" / "clip.mp4"
    source.parent.mkdir(parents=True)
    spec = importlib.util.spec_from_file_location("caption_test_media_helpers", Path(__file__).with_name("test_e2e.py"))
    assert spec and spec.loader
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)
    if not helpers._have_ffmpeg() or not has_filter("ass"):
        pytest.skip("ffmpeg with the ass filter required")
    return helpers._make_clip(source, seconds=1.5, color="teal")


@pytest.fixture
def manager(tmp_path: Path):
    instance = CampaignManager(tmp_path, start_worker=False)
    try:
        yield instance
    finally:
        instance.close()


def measured(
    monkeypatch: pytest.MonkeyPatch, transcript: list[dict], words: list[dict], *, listened: bool = True
) -> None:
    def analyze(source: Path, work_dir: Path, **_kwargs) -> dict:
        work_dir.mkdir(parents=True, exist_ok=True)
        return {
            "duration": 1.5,
            "has_audio": True,
            "scenes": [],
            "silences": [],
            "transcript": transcript,
            "words": words,
            "transcribed": listened,
            "warnings": [],
        }

    monkeypatch.setattr("vedit.story.analyze_source", analyze)


def tiny_plan(**edit) -> dict:
    return {
        "title": "A clear view",
        "segments": [{"start": 0.1, "end": 1.4, "label": "Product", "reason": "Chosen."}],
        "caption": "A look.",
        "hashtags": [],
        "edit": edit,
    }


HEARD = [{"start": 0.2, "end": 1.2, "text": "Made by hand."}]
HEARD_WORDS = [
    {"start": 0.2, "end": 0.5, "text": "Made"},
    {"start": 0.5, "end": 0.7, "text": "by"},
    {"start": 0.7, "end": 1.2, "text": "hand."},
]


@pytest.mark.render
def test_a_campaign_burns_the_plans_words_without_running_a_model(
    manager: CampaignManager, tiny_clip: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    measured(monkeypatch, HEARD, HEARD_WORDS)
    monkeypatch.setattr(
        "vedit.campaign.transcribe_speech", lambda *a, **k: pytest.fail("the plan already holds the transcript")
    )
    calls: list[str] = []

    def spy(module: object, name: str):
        real = getattr(module, name)

        def wrapped(*args, **kwargs):
            calls.append(name)
            return real(*args, **kwargs)

        monkeypatch.setattr(module, name, wrapped)

    # The timeline view (campaign_plan) previews the cues; only the render (campaign) writes the caption file.
    spy(vedit.campaign_plan, "preview_cues")
    spy(vedit.campaign, "build_caption_file")
    job = manager.create(
        {"paths": [str(tiny_clip)], "goal": "Show the product", "target_duration": 1, "captions": True}
    )
    manager.process(job["id"])
    assert "preview_cues" in calls and "build_caption_file" not in calls, (
        "the timeline previews; only a render writes the file"
    )
    planned = manager.get(job["id"])
    assert planned["status"] == "planned", planned["error"]
    assert planned["plan"]["analysis"]["words"] == HEARD_WORDS
    assert [cue["text"] for cue in planned["plan"]["timeline"]["captions"]] == ["Made by hand."]
    timeline = manager.timeline(job["id"], {"plan": tiny_plan(captions={"edits": {"200": "Built by hand."}})})
    assert timeline["captions"][0]["text"] == "Built by hand." and timeline["captions"][0]["key"] == "200"
    manager.render(job["id"], {"plan": tiny_plan(captions={"style": "pop"})})
    manager.process(job["id"])
    complete = manager.get(job["id"])
    assert complete["status"] == "complete", complete["error"]
    assert calls.count("build_caption_file") == 1
    report = json.loads(manager.artifact(job["id"], "report").read_text())
    assert "captions_transcribe" not in report["steps"]
    assert probe(manager.artifact(job["id"], "video")).has_video
    with zipfile.ZipFile(manager.artifact(job["id"], "bundle")) as archive:
        ass = archive.read("captions.ass").decode()
        assert r"\t(0,90," in ass and "Default" in ass, "the chosen style, not a default"
        assert archive.read("captions.srt").count(b"\n") >= 3


@pytest.mark.render
def test_a_local_edit_never_listens_and_says_captions_need_meta(
    manager: CampaignManager, tiny_clip: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The model-free path has no speech model to fall back on, so it captions nothing and says why."""
    measured(monkeypatch, [], [], listened=False)
    monkeypatch.setattr("vedit.campaign.transcribe_speech", lambda *a, **k: pytest.fail("a local edit calls no model"))
    monkeypatch.setattr(meta, "transcribe_audio", lambda *a, **k: pytest.fail("a local edit calls no model"))
    job = manager.create(
        {"paths": [str(tiny_clip)], "goal": "Show the product", "target_duration": 1, "captions": True}
    )
    manager.process(job["id"])
    assert any(
        "Captions are unavailable: captions use Meta Voice Transcribe" in warning
        for warning in manager.get(job["id"])["plan"]["warnings"]
    )
    manager.render(job["id"], {"plan": tiny_plan()})
    manager.process(job["id"])
    complete = manager.get(job["id"])
    assert complete["status"] == "complete", complete["error"]
    assert any("Choose Meta mode to caption this video" in warning for warning in complete["result"]["warnings"])
    with zipfile.ZipFile(manager.artifact(job["id"], "bundle")) as archive:
        assert "captions.srt" not in archive.namelist()


@pytest.mark.render
def test_meta_mode_hears_the_source_once_and_captions_reuse_it(
    manager: CampaignManager, tiny_clip: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Meta Voice Transcribe hears the whole source at planning; the render cuts captions from that transcript, free."""
    meta_on(monkeypatch)
    heard: list[dict] = []

    def transcribe_audio(wav: Path, **kwargs) -> dict:
        info = probe(wav)
        heard.append(dict(kwargs, seconds=info.duration, channels=info.audio.channels, rate=info.audio.sample_rate))
        return {
            "cues": [{"start": 0.2, "end": 1.2, "text": "Made by hand. Then oiled."}],
            "provider": "meta",
            "usage": {"cost_usd": 0.0001},
        }

    monkeypatch.setattr(meta, "transcribe_audio", transcribe_audio)
    monkeypatch.setattr(
        meta,
        "plan_story",
        lambda proxy, **kw: {
            **tiny_plan(),
            "summary": "s",
            "cover_prompt": "",
            "provider": "meta",
            "usage": {"cost_usd": 0.001},
            "warnings": [],
        },
    )
    monkeypatch.setattr(
        CampaignManager, "_meta_captions", lambda *a, **k: pytest.fail("the plan already holds Meta's transcript")
    )
    job = manager.create(
        {
            "paths": [str(tiny_clip)],
            "goal": "Show the product",
            "target_duration": 1,
            "captions": True,
            "provider": "meta",
            "caption_provider": "local",
        }
    )
    assert job["request"]["caption_provider"] == "meta", "Meta mode always captions with Meta"
    manager.process(job["id"])
    planned = manager.get(job["id"])
    assert planned["status"] == "planned", planned["error"]
    analysis = planned["plan"]["analysis"]
    assert analysis["transcribed_by"] == "meta-voice-transcribe" and analysis["transcribed"] is True
    assert [sentence["text"] for sentence in analysis["sentences"]] == ["Made by hand.", "Then oiled."], (
        "a turn's sentences are split by punctuation"
    )
    assert "words" not in analysis, (
        "turn-level timing gives no heard words, so nothing cuts audio at a guessed word time"
    )
    assert (
        len(heard) == 1
        and heard[0]["seconds"] == pytest.approx(1.5, abs=0.05)
        and (heard[0]["channels"], heard[0]["rate"]) == (1, 16000)
    )
    manager.render(job["id"], {"plan": tiny_plan()})
    manager.process(job["id"])
    complete = manager.get(job["id"])
    assert complete["status"] == "complete", complete["error"]
    assert len(heard) == 1, "the clip is transcribed once, for planning and captions"
    assert complete["result"]["caption_provider"] == "meta"
    with zipfile.ZipFile(manager.artifact(job["id"], "bundle")) as archive:
        assert b"Made by hand." in archive.read("captions.srt")
    report = json.loads(manager.artifact(job["id"], "report").read_text())
    checks = {check["name"]: check for check in report.get("checks") or complete["result"].get("checks") or []}
    sentence = checks.get("auto: cuts land on sentence boundaries")
    assert sentence is None or sentence.get("inputs") == ["meta-voice-transcribe"]


@pytest.mark.render
def test_a_failed_meta_transcript_never_falls_back_to_another_model(
    manager: CampaignManager, tiny_clip: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta_on(monkeypatch)
    monkeypatch.setattr(
        meta,
        "transcribe_audio",
        lambda *a, **k: (_ for _ in ()).throw(
            meta.MetaError("Meta's rate limit was reached. Wait briefly, then retry.")
        ),
    )
    monkeypatch.setattr(
        meta,
        "plan_story",
        lambda proxy, **kw: {
            **tiny_plan(),
            "summary": "s",
            "cover_prompt": "",
            "provider": "meta",
            "usage": {"cost_usd": 0.001},
            "warnings": [],
        },
    )
    job = manager.create(
        {
            "paths": [str(tiny_clip)],
            "goal": "Show the product",
            "target_duration": 1,
            "captions": True,
            "provider": "meta",
        }
    )
    manager.process(job["id"])
    planned = manager.get(job["id"])
    assert planned["status"] == "planned", planned["error"]
    assert planned["plan"]["analysis"]["transcript"] == [] and "transcribed_by" not in planned["plan"]["analysis"]
    assert any(
        "Meta Voice Transcribe did not finish" in warning and "rate limit" in warning
        for warning in planned["plan"]["warnings"]
    )


@pytest.mark.render
def test_silence_is_not_listened_to_twice(
    manager: CampaignManager, tiny_clip: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recognizer can invent words on silence, so a second listen could only burn a phantom."""
    measured(monkeypatch, [], [], listened=True)
    monkeypatch.setattr(
        "vedit.campaign.transcribe_speech", lambda *a, **k: pytest.fail("the plan already listened and heard nothing")
    )
    job = manager.create(
        {"paths": [str(tiny_clip)], "goal": "Show the product", "target_duration": 1, "captions": True}
    )
    manager.process(job["id"])
    manager.render(job["id"], {"plan": tiny_plan()})
    manager.process(job["id"])
    complete = manager.get(job["id"])
    assert complete["status"] == "complete", complete["error"]
    assert any("No speech was heard" in warning for warning in complete["result"]["warnings"])


@pytest.mark.render
def test_a_campaign_delivers_the_translated_srt(
    manager: CampaignManager, tiny_clip: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta_on(monkeypatch)
    measured(monkeypatch, HEARD, HEARD_WORDS)
    monkeypatch.setattr(meta, "_request", TranslationHttp("Hecho a mano."))
    job = manager.create(
        {"paths": [str(tiny_clip)], "goal": "Show the product", "target_duration": 1, "captions": True}
    )
    manager.process(job["id"])
    manager.render(job["id"], {"plan": tiny_plan(captions={"language": "es"})})
    manager.process(job["id"])
    complete = manager.get(job["id"])
    assert complete["status"] == "complete", complete["error"]
    with zipfile.ZipFile(manager.artifact(job["id"], "bundle")) as archive:
        assert b"Hecho a mano." in archive.read("captions.es.srt") and b"Made by hand." in archive.read("captions.srt")
        assert "Hecho" in archive.read("captions.ass").decode()
    assert manager.get(job["id"])["budget"]["operations"]["translate"]["calls"] == 1


@pytest.mark.render
def test_a_campaign_refuses_translation_plainly_without_meta(
    manager: CampaignManager, tiny_clip: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    measured(monkeypatch, HEARD, HEARD_WORDS)
    job = manager.create(
        {"paths": [str(tiny_clip)], "goal": "Show the product", "target_duration": 1, "captions": True}
    )
    manager.process(job["id"])
    manager.render(job["id"], {"plan": tiny_plan(captions={"language": "es"})})
    manager.process(job["id"])
    failed = manager.get(job["id"])
    assert (
        failed["status"] == "failed"
        and "Translated captions use Meta" in failed["error"]
        and "caption language" in failed["error"]
    )


def test_meta_turns_become_sentence_lines_with_estimated_words() -> None:
    turns = [
        {
            "start": 0.0,
            "end": 8.0,
            "text": "This is the finished oak bowl, oiled and ready for the table. It started as a rough blank.",
        },
        {"start": 9.0, "end": 9.5, "text": "[BLANK_AUDIO]"},
    ]
    lines = heard_cues(turns)
    assert all(len(line.text) <= 42 and not line.words for line in lines)
    assert " ".join(line.text for line in lines) == turns[0]["text"]
    ends = [line for line in lines if line.text.endswith("table.")]
    assert len(ends) == 1 and 0 < ends[0].end < 8.0, "a sentence end inside a turn is placed by length"
    assert lines[0].start == 0.0 and lines[-1].end == 8.0, "the turn's own edges are heard"


def test_transcribe_sends_16k_mono_once_and_never_sends_silence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vedit.media import run_ffmpeg

    calls: list[dict] = []

    def transcribe_audio(wav: Path, **kwargs) -> dict:
        info = probe(wav)
        calls.append(dict(kwargs, seconds=info.duration, rate=info.audio.sample_rate, channels=info.audio.channels))
        return {"cues": [{"start": 0.1, "end": 0.9, "text": "Hello there."}]}

    monkeypatch.setattr(meta, "transcribe_audio", transcribe_audio)
    tone, quiet = tmp_path / "tone.wav", tmp_path / "quiet.wav"
    run_ffmpeg(["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1", "-ac", "2", str(tone)])
    run_ffmpeg(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "1", str(quiet)])
    cues = transcribe(tone, tmp_path / "work", language="es", budget_usd=0.05, meta_dir=tmp_path / "ledger")
    assert [cue.text for cue in cues] == ["Hello there."]
    assert len(calls) == 1 and (calls[0]["rate"], calls[0]["channels"]) == (16000, 1) and calls[0]["language"] == "es"
    assert calls[0]["work_dir"] == tmp_path / "ledger" and calls[0]["budget_usd"] == 0.05
    assert transcribe(quiet, tmp_path / "quiet-work") == [] and len(calls) == 1, "silence is not sent"


def test_a_long_recording_is_heard_in_pieces_cut_at_pauses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import vedit.captions as captions

    pieces: list[float] = []

    def transcribe_audio(wav: Path, **kwargs) -> dict:
        pieces.append(probe(wav).duration)
        return {"cues": [{"start": 0.5, "end": 1.0, "text": f"Piece {len(pieces)}."}]}

    monkeypatch.setattr(meta, "transcribe_audio", transcribe_audio)
    monkeypatch.setattr(captions, "MAX_PIECE_SECONDS", 4.0)
    monkeypatch.setattr(captions, "_PIECE_SEARCH_SECONDS", 2.0)
    from vedit.media import run_ffmpeg

    long = tmp_path / "long.wav"
    # Tone, a pause from 3.0 to 3.6 s, tone again: the first piece ends inside that pause, not at 4 s.
    run_ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=16000:duration=10",
            "-af",
            "volume=enable='between(t,3,3.6)':volume=0",
            "-ac",
            "1",
            str(long),
        ]
    )
    cues = transcribe(long, tmp_path / "work")
    assert len(pieces) >= 3 and all(seconds <= 4.0 + 0.05 for seconds in pieces)
    assert 3.0 <= pieces[0] <= 3.6, "the first piece ends in the pause"
    assert [cue.text for cue in cues] == [f"Piece {index}." for index in range(1, len(pieces) + 1)]
    assert cues[1].start == pytest.approx(pieces[0] + 0.5, abs=0.01), "each piece is moved back onto the source clock"
