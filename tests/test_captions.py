from pathlib import Path

import pytest

from vedit.analyze import Range
from vedit.captions import Cue, format_ts, parse_ts, read_srt, remap_cues, write_srt


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
