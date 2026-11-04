"""Caption lines start where speech resumes, not where a character count guessed.

Meta Voice Transcribe times turns, not words, so a line inside a long turn is
placed by character count. Live, 2026-11-04, two lines of a 41 s story showed
about a second before the speaker said them. ``align_to_pauses`` moves each
estimated line start onto the pause measured in the audio the recognizer heard.
"""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

import pytest

from vedit.captions import EDGE_LEAD, MIN_LINE, Cue, Word, _pauses_in, align_to_pauses


def test_a_start_inside_speech_moves_to_the_nearby_pause_and_the_previous_line_follows():
    cues = [Cue(0.0, 3.0, "we sand it twice"), Cue(3.0, 6.0, "then oil it once")]
    out = align_to_pauses(cues, [(3.2, 3.7)])
    assert [(c.start, c.end) for c in out] == [(0.0, 3.65), (3.65, 6.0)]
    assert [c.text for c in out] == [c.text for c in cues]


def test_a_start_inside_a_pause_waits_for_the_first_sound():
    out = align_to_pauses([Cue(1.0, 4.0, "hello")], [(0.5, 1.8)])
    assert out[0].start == pytest.approx(1.8 - EDGE_LEAD)


def test_a_pause_out_of_reach_changes_nothing():
    cues = [Cue(0.0, 3.0, "a b c"), Cue(3.0, 6.0, "d e f")]
    assert align_to_pauses(cues, [(4.5, 5.0)]) == cues


def test_no_line_is_squeezed_below_the_minimum_and_none_overlap():
    cues = [Cue(0.0, 3.0, "a"), Cue(3.0, 3.5, "b"), Cue(3.5, 8.0, "c")]
    out = align_to_pauses(cues, [(3.1, 3.4), (3.6, 4.2)])
    assert all(c.end - c.start >= MIN_LINE - 1e-9 or c == o for c, o in zip(out, cues))
    assert all(a.end <= b.start + 1e-9 for a, b in zip(out, out[1:]))
    assert [c.text for c in out] == ["a", "b", "c"]


def test_heard_word_times_are_never_moved():
    words = (Word(1.0, 1.4, "hello"), Word(1.5, 2.0, "there"))
    cues = [Cue(1.0, 2.0, "hello there", words=words)]
    assert align_to_pauses(cues, [(0.2, 1.3)]) == cues


def _tone_with_gap(path: Path, *, gap: tuple[float, float], total: float, rate: int = 16_000) -> Path:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        frames = bytearray()
        for n in range(int(total * rate)):
            t = n / rate
            value = 0 if gap[0] <= t < gap[1] else int(9000 * math.sin(2 * math.pi * 220 * t))
            frames += struct.pack("<h", value)
        out.writeframes(bytes(frames))
    return path


def test_the_pause_is_measured_in_the_audio_the_recognizer_heard(tmp_path: Path):
    wav = _tone_with_gap(tmp_path / "kept.wav", gap=(3.0, 3.6), total=6.0)
    pauses = _pauses_in(wav)
    assert any(low == pytest.approx(3.0, abs=0.1) and high == pytest.approx(3.6, abs=0.1) for low, high in pauses)
    out = align_to_pauses([Cue(0.0, 2.7, "first line"), Cue(2.7, 6.0, "second line")], pauses)
    assert 3.4 <= out[1].start <= 3.6 and out[0].end == out[1].start


def test_an_unreadable_file_skips_the_alignment(tmp_path: Path):
    assert _pauses_in(tmp_path / "missing.wav") == []
