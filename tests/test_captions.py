from pathlib import Path

import pytest

from vedit.analyze import Range
from vedit.captions import Cue, _is_stub_model, find_whisper_model, format_ts, parse_ts, read_srt, remap_cues, write_srt


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


def test_stub_ggml_is_not_selected_as_real_model():
    stub = Path("/opt/homebrew/share/whisper-cpp/for-tests-ggml-tiny.bin")
    if stub.is_file():
        assert _is_stub_model(stub)
        chosen = find_whisper_model(allow_stub=False)
        if chosen is not None:
            assert not _is_stub_model(chosen)


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
    assert read_srt(path)[0].text.strip("​ ") == ""


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
    assert [c.text for c in out] == ["Made by hand and", "Oiled."]
    assert [(c.start, c.end) for c in out] == [(0, 1), (1.6, 2)]
    # The joined text must fit max_chars.
    long_a = "the raw oak blank sits wet and heavy on the bench by the door"
    out = polish_cues([Cue(0, 1, long_a), Cue(1, 2, "and more words here")], max_chars=60)
    assert len(out) == 2
    out = polish_cues([Cue(0, 1, long_a), Cue(1, 2, "and more words here")], max_chars=84)
    assert len(out) == 1


def test_polish_normalizes_whitespace_capitalizes_and_drops_empty_cues():
    from vedit.captions import polish_cues

    cues = [Cue(0, 0.5, "   "), Cue(0.5, 1.0, "\tthis  is\n the   bowl."), Cue(1.0, 1.2, ""), Cue(2.0, 3.0, "24 hours later, it is ready.")]
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
    assert dialogue == [r"Dialogue: 0,0:00:00.38,0:00:03.50,Default,,0,0,0,,This is the finished oak bowl,\Noiled and ready for the table."]


def test_meta_shaped_cues_with_doubled_words_come_out_clean():
    """The same conversion campaign._meta_captions applies to Meta Voice Transcribe output."""
    from vedit.captions import polish_cues

    result = {"cues": [
        {"start": 16.0, "end": 18.8, "text": "on the lathe the the shape appears in long,"},
        {"start": 18.8, "end": 20.0, "text": "steady cuts."},
        {"start": 24.0, "end": 26.4, "text": "sanding goes from coarse to fine, with the lathe"},
        {"start": 26.4, "end": 29.0, "text": "lathe turning slowly."},
    ], "provider": "meta"}
    cues = polish_cues([Cue(float(c["start"]), float(c["end"]), str(c["text"])) for c in result["cues"]])
    assert cues == [
        Cue(16.0, 20.0, "On the lathe the shape appears in long, steady cuts."),
        Cue(24.0, 29.0, "Sanding goes from coarse to fine, with the lathe turning slowly."),
    ]


def test_find_whisper_model_prefers_env_then_shared_dir_then_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    real = b"\0" * 2_000_001  # anything under 2 MB is treated as a stub
    home = tmp_path / "home"
    shared = home / ".vedit" / "models"
    shared.mkdir(parents=True)
    checkout = tmp_path / "checkout" / ".vedit-models"
    checkout.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("VEDIT_MODELS_DIR", raising=False)
    monkeypatch.delenv("VEDIT_WHISPER_MODEL", raising=False)
    monkeypatch.chdir(tmp_path / "checkout")
    # Only the checkout holds a model: found as before.
    (checkout / "ggml-base.en.bin").write_bytes(real)
    assert find_whisper_model() == checkout / "ggml-base.en.bin"
    # Any ggml-*.bin in ~/.vedit/models/ wins over the checkout; the named files win over the glob.
    (shared / "ggml-small.en.bin").write_bytes(real)
    assert find_whisper_model() == shared / "ggml-small.en.bin"
    (shared / "ggml-base.en.bin").write_bytes(real)
    assert find_whisper_model() == shared / "ggml-base.en.bin"
    # A stub in the shared directory never shadows a real model elsewhere.
    (shared / "ggml-base.en.bin").write_bytes(b"\0" * 10)
    assert find_whisper_model() == shared / "ggml-small.en.bin"
    # VEDIT_MODELS_DIR replaces ~/.vedit/models; the env model path beats everything.
    other = tmp_path / "models-dir"
    other.mkdir()
    (other / "ggml-base.bin").write_bytes(real)
    monkeypatch.setenv("VEDIT_MODELS_DIR", str(other))
    assert find_whisper_model() == other / "ggml-base.bin"
    explicit = tmp_path / "explicit.bin"
    explicit.write_bytes(real)
    monkeypatch.setenv("VEDIT_WHISPER_MODEL", str(explicit))
    assert find_whisper_model() == explicit
    # Stubs are only returned when asked for and nothing real exists.
    monkeypatch.delenv("VEDIT_WHISPER_MODEL")
    for path in (other / "ggml-base.bin", checkout / "ggml-base.en.bin", shared / "ggml-small.en.bin"):
        path.write_bytes(b"\0" * 10)
    if find_whisper_model() is not None:  # a real model under ~/.cache or /opt may exist on this machine
        pytest.skip("a real fallback model exists outside the temp directories")
    assert find_whisper_model(allow_stub=True) == other / "ggml-base.bin"
