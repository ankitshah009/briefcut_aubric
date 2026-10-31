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
