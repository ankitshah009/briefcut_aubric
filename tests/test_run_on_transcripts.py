"""A long, unpunctuated Meta turn still transcribes, and still gives cuts a place to land.

Live, 2026-11-04: Meta Voice Transcribe returned a 169 s monologue as one turn of
over 2,000 characters with no punctuation. Briefcut refused the whole transcript,
and the plan's cuts landed mid-sentence. These tests pin both fixes.
"""

from __future__ import annotations

import pytest

from vedit import captions, meta
from vedit.story import MIN_PIECE, RUN_ON_SECONDS, sentences_from_transcript, snap_to_sentences


def _lines(total: float, step: float, text: str = "and we kept going") -> list[dict]:
    out, at = [], 0.0
    while at < total - 1e-9:
        out.append({"start": round(at, 3), "end": round(min(total, at + step), 3), "text": text})
        at += step
    return out


def _inside_speech(point: float, speech: list[dict]) -> bool:
    return any(item["start"] < point < item["end"] for item in speech)


def test_a_long_turn_is_accepted_and_split_into_lines():
    syllables = ["ba", "ko", "mi", "ru", "te", "lo", "na", "pi", "su", "de"]
    words = " ".join(
        a + b + c for a in syllables for b in syllables for c in syllables
    )  # 1,000 distinct words, ~7,000 characters
    cues = meta._transcript_cues({"turns": [{"startMs": 0, "endMs": 169_000, "transcript": words}]}, 169.0)
    assert len(cues) == 1 and len(cues[0]["text"]) > 2_000
    lines = captions.heard_cues(cues)
    assert len(lines) > 20
    assert lines[0].start == pytest.approx(0.0, abs=0.01) and lines[-1].end == pytest.approx(169.0, abs=0.01)
    assert all(line.text and len(line.text) <= captions.CUE_CHARS * 2 for line in lines)


def test_a_broken_turn_is_still_refused_with_a_readable_message():
    with pytest.raises(meta.MetaError, match=r"at most 40,000 characters"):
        meta._transcript_cues({"turns": [{"startMs": 0, "endMs": 1_000, "transcript": "x" * 40_001}]}, 1.0)


def test_a_run_on_turn_splits_at_the_pauses_measured_in_it():
    transcript = _lines(30.0, 2.0)  # 15 contiguous lines, no punctuation, no gaps between cues
    speech = [{"start": 0.0, "end": 9.0}, {"start": 9.6, "end": 20.0}, {"start": 20.5, "end": 30.0}]
    sentences = sentences_from_transcript(transcript, duration=30.0, speech=speech)
    assert [(s["start"], s["end"]) for s in sentences] == [(0.0, 9.15), (9.45, 20.15), (20.35, 30.0)]
    assert all(s["text"] for s in sentences)
    for edge in [s["start"] for s in sentences[1:]] + [s["end"] for s in sentences[:-1]]:
        assert not _inside_speech(edge, speech)


def test_without_measured_speech_a_run_on_stays_one_sentence():
    sentences = sentences_from_transcript(_lines(30.0, 2.0), duration=30.0)
    assert len(sentences) == 1 and sentences[0]["end"] - sentences[0]["start"] > RUN_ON_SECONDS


def test_a_punctuated_sentence_is_not_split_at_a_breath():
    transcript = [
        {"start": 0.0, "end": 2.0, "text": "We sand it twice,"},
        {"start": 2.0, "end": 4.0, "text": "then oil it once."},
        {"start": 4.0, "end": 6.0, "text": "That is the whole trick."},
    ]
    speech = [{"start": 0.0, "end": 1.8}, {"start": 2.3, "end": 3.9}, {"start": 4.1, "end": 6.0}]
    sentences = sentences_from_transcript(transcript, duration=6.0, speech=speech)
    assert [s["text"] for s in sentences] == ["We sand it twice, then oil it once.", "That is the whole trick."]


def test_pauses_near_a_group_edge_or_each_other_do_not_make_slivers():
    transcript = _lines(20.0, 1.0)
    # pauses at 0.9 (too near the start), 10.0 and 10.8 (too near each other), 19.4 (too near the end)
    speech = [
        {"start": 0.0, "end": 0.7},
        {"start": 1.1, "end": 9.8},
        {"start": 10.2, "end": 10.6},
        {"start": 11.0, "end": 19.2},
        {"start": 19.6, "end": 20.0},
    ]
    sentences = sentences_from_transcript(transcript, duration=20.0, speech=speech)
    assert len(sentences) == 2
    assert all(s["end"] - s["start"] >= MIN_PIECE for s in sentences)


def test_a_cut_in_a_run_on_snaps_onto_a_pause():
    transcript = _lines(30.0, 2.0)
    speech = [{"start": round(3.0 * k + (0.2 if k else 0.0), 3), "end": round(3.0 * k + 2.8, 3)} for k in range(10)]
    speech[-1]["end"] = 30.0
    sentences = sentences_from_transcript(transcript, duration=30.0, speech=speech)
    assert len(sentences) == 10
    snapped = snap_to_sentences([{"start": 4.4, "end": 14.5, "reason": "the build"}], sentences, duration=30.0)
    edges = (snapped[0]["start"], snapped[0]["end"])
    assert edges == (pytest.approx(3.05), pytest.approx(14.95))
    assert not any(_inside_speech(edge, speech) for edge in edges)
