"""Audio and duration robustness: any rate, layout, track, length or level reaches Meta correctly, or not at all.

Meta is stubbed at ``meta.transcribe_audio``. The stub answers like Meta Voice
Transcribe: turn-level times on the WAV's own clock, with one word joined to its
copy ("ThisThis") and one doubled word ("rough rough").
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import vedit.captions as captions
from vedit import analyze, meta, sound
from vedit.analyze import Range, audible
from vedit.captions import Cue, remap_cues, transcribe, transcribe_kept
from vedit.media import probe, resolve_ffmpeg, run_ffmpeg
from vedit.timeline import Span

pytestmark = pytest.mark.render

LAYOUTS = {"mono": "mono", "stereo": "stereo", "5.1": "5.1", "7.1": "7.1"}
RATES = (8000, 22050, 44100, 48000, 96000)


@pytest.fixture(autouse=True)
def _ffmpeg() -> None:
    try:
        resolve_ffmpeg()
    except Exception:
        pytest.skip("ffmpeg required")


class MetaStub:
    """Records what reached Meta and answers with realistic turns."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, wav: Path, *, duration: float, work_dir: Path, language: str = "en", budget_usd: float = .25) -> dict:
        info = probe(wav)
        self.calls.append({"seconds": info.duration, "given": duration, "rate": info.audio.sample_rate, "channels": info.audio.channels,
                           "audible": audible(wav), "language": language, "work_dir": work_dir})
        end = min(info.duration, 0.9)
        turns = [{"start": 0.1, "end": round(end, 3), "text": "ThisThis is the rough rough blank."}] if end > 0.2 else []
        return {"cues": turns, "provider": "meta", "usage": {"cost_usd": 0.0}}


@pytest.fixture
def heard(monkeypatch: pytest.MonkeyPatch) -> MetaStub:
    stub = MetaStub()
    monkeypatch.setattr(meta, "transcribe_audio", stub)
    return stub


def _make(path: Path, *args: str) -> Path:
    run_ffmpeg([*args, str(path)])
    return path


def _tone(seconds: float = 1.0, rate: int = 48000, *, volume: str = "") -> str:
    return f"sine=frequency=440:sample_rate={rate}:duration={seconds}" + (f",volume={volume}" if volume else "")


# 1. Every channel layout and sample rate is heard as 16 kHz mono, with the voice kept.

@pytest.mark.parametrize("rate", RATES)
@pytest.mark.parametrize("layout", LAYOUTS)
def test_every_layout_and_rate_is_heard_as_16k_mono(tmp_path: Path, heard: MetaStub, layout: str, rate: int) -> None:
    # A film mix carries dialogue in the centre channel only; the downmix must keep it.
    pan = {"mono": "anull", "stereo": "pan=stereo|c0=c0|c1=c0", "5.1": "pan=5.1|FC=c0", "7.1": "pan=7.1|FC=c0"}[layout]
    source = _make(tmp_path / f"{layout}-{rate}.wav", "-f", "lavfi", "-i", _tone(1.0, rate), "-af", pan)
    assert (probe(source).audio.channels, probe(source).audio.sample_rate) == ({"mono": 1, "stereo": 2, "5.1": 6, "7.1": 8}[layout], rate)
    cues = transcribe(source, tmp_path / "work")
    assert len(heard.calls) == 1
    call = heard.calls[0]
    assert (call["rate"], call["channels"], call["audible"]) == (16000, 1, True), "the voice survives the downmix"
    assert call["seconds"] == pytest.approx(1.0, abs=0.02)
    assert [cue.text for cue in cues] == ["This is the rough rough blank."], "a joined double is mended where it enters"


# 2. The track the editor plays is the one heard.

def test_the_default_track_is_heard_even_when_it_is_not_the_first(tmp_path: Path, heard: MetaStub) -> None:
    source = _make(tmp_path / "two.mka", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=1", "-f", "lavfi", "-i", _tone(),
                   "-map", "0:a", "-map", "1:a", "-disposition:a:0", "0", "-disposition:a:1", "default", "-t", "1")
    assert probe(source).audio_map == 1
    transcribe(source, tmp_path / "work")
    assert len(heard.calls) == 1 and heard.calls[0]["audible"]


def test_decode_mono_measures_the_track_the_editor_plays(tmp_path: Path) -> None:
    # FFmpeg itself would pick the stereo track (most channels). The render plays the default one.
    source = _make(tmp_path / "two.mka", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=1", "-f", "lavfi", "-i", _tone(),
                   "-map", "0:a", "-map", "1:a", "-disposition:a:0", "0", "-disposition:a:1", "default", "-t", "1")
    samples = sound.decode_mono(source)
    assert max(abs(value) for value in samples) > 1000, "the tone on the default track, not the silent first track"
    assert max(abs(value) for value in sound.decode_mono(source, track=0)) == 0


# 3. Nothing to hear: nothing is sent.

def test_no_audio_track_sends_nothing(tmp_path: Path, heard: MetaStub) -> None:
    source = _make(tmp_path / "picture.mp4", "-f", "lavfi", "-i", "color=c=teal:s=64x64:r=10:d=1", "-c:v", "libx264", "-pix_fmt", "yuv420p")
    assert transcribe(source, tmp_path / "work") == [] and heard.calls == []


def test_a_silent_track_sends_nothing(tmp_path: Path, heard: MetaStub) -> None:
    source = _make(tmp_path / "quiet.wav", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=5.1", "-t", "1")
    assert transcribe(source, tmp_path / "work") == [] and heard.calls == []


def test_a_silent_piece_of_a_long_recording_is_never_sent(tmp_path: Path, heard: MetaStub, monkeypatch: pytest.MonkeyPatch) -> None:
    """Speech for 3 s, then 9 s of room silence: only the piece with sound goes to Meta."""
    monkeypatch.setattr(captions, "MAX_PIECE_SECONDS", 4.0)
    monkeypatch.setattr(captions, "_PIECE_SEARCH_SECONDS", 2.0)
    source = _make(tmp_path / "talk.wav", "-f", "lavfi", "-i", _tone(12.0, 16000), "-af", "volume=enable='gt(t,3)':volume=0", "-ac", "1")
    cues = transcribe(source, tmp_path / "work")
    assert heard.calls and all(call["audible"] for call in heard.calls), "a silent piece reached Meta"
    assert len(heard.calls) == 1 and [cue.text for cue in cues] == ["This is the rough rough blank."]


def test_a_sliver_left_at_the_end_is_not_sent(tmp_path: Path, heard: MetaStub, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _make(tmp_path / "talk.wav", "-f", "lavfi", "-i", _tone(4.05, 16000), "-ac", "1")
    monkeypatch.setattr(captions, "_pieces", lambda wav, seconds: [(0.0, 4.0), (4.0, seconds)])
    transcribe(source, tmp_path / "work")
    assert len(heard.calls) == 1, "Meta refuses a request with nothing in it, and nothing can be said in 50 ms"


def test_a_clip_shorter_than_a_word_sends_nothing(tmp_path: Path, heard: MetaStub) -> None:
    source = _make(tmp_path / "blip.wav", "-f", "lavfi", "-i", _tone(0.05), "-ac", "1")
    assert transcribe(source, tmp_path / "work") == [] and heard.calls == []


@pytest.mark.parametrize("seconds", [0.3, 0.6, 0.95])
def test_a_clip_under_a_second_is_heard_once(tmp_path: Path, heard: MetaStub, seconds: float) -> None:
    source = _make(tmp_path / "short.wav", "-f", "lavfi", "-i", _tone(seconds, 48000), "-ac", "2")
    transcribe(source, tmp_path / "work")
    assert len(heard.calls) == 1 and heard.calls[0]["seconds"] == pytest.approx(seconds, abs=0.02)


# 4. Long recordings split at their own pauses, whatever their level, and land back on the source clock.

def _pieces_heard(tmp_path: Path, heard: MetaStub, monkeypatch: pytest.MonkeyPatch, chain: str, seconds: float = 10.0) -> list[float]:
    monkeypatch.setattr(captions, "MAX_PIECE_SECONDS", 4.0)
    monkeypatch.setattr(captions, "_PIECE_SEARCH_SECONDS", 2.0)
    source = _make(tmp_path / "long.wav", "-f", "lavfi", "-i", _tone(seconds, 16000), "-af", chain, "-ac", "1")
    transcribe(source, tmp_path / "work")
    return [call["seconds"] for call in heard.calls]


def test_quiet_speech_still_splits_at_its_pauses(tmp_path: Path, heard: MetaStub, monkeypatch: pytest.MonkeyPatch) -> None:
    """A whisper at -50 dB sits under any fixed pause level; the pause is found against the recording's own floor."""
    pieces = _pieces_heard(tmp_path, heard, monkeypatch, "volume=-32dB,volume=enable='between(t,2.6,3.2)':volume=0")
    assert 2.6 <= pieces[0] <= 3.2, f"the first piece ends in the pause, not at the hard limit: {pieces}"


def test_speech_over_music_cuts_at_the_quietest_moment(tmp_path: Path, heard: MetaStub, monkeypatch: pytest.MonkeyPatch) -> None:
    """Music never falls silent, so the cut goes where the sound dips most, not mid-word at the limit."""
    # Peaks at -6 dB, dipping to -21 dB: never under a fixed -35 dB pause level.
    pieces = _pieces_heard(tmp_path, heard, monkeypatch, "volume=12dB,volume=enable='between(t,2.6,3.2)':volume=-15dB")
    assert 2.6 <= pieces[0] <= 3.2, f"the first piece ends in the dip: {pieces}"


def test_each_piece_lands_back_on_the_source_clock_exactly(tmp_path: Path, heard: MetaStub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(captions, "MAX_PIECE_SECONDS", 4.0)
    monkeypatch.setattr(captions, "_PIECE_SEARCH_SECONDS", 2.0)
    source = _make(tmp_path / "long.wav", "-f", "lavfi", "-i", _tone(11.0, 16000),
                   "-af", "volume=enable='between(t,3.0,3.5)+between(t,6.6,7.0)':volume=0", "-ac", "1")
    starts: list[float] = []
    real = captions._pieces

    def spy(wav: Path, seconds: float) -> list[tuple[float, float]]:
        found = real(wav, seconds)
        starts.extend(start for start, _ in found)
        return found

    monkeypatch.setattr(captions, "_pieces", spy)
    cues = transcribe(source, tmp_path / "work")
    assert len(starts) == len(heard.calls) >= 3
    assert all(call["seconds"] <= 4.0 + 0.01 for call in heard.calls), "every request fits Meta's limit"
    assert [cue.start for cue in cues] == [pytest.approx(start + 0.1, abs=0.002) for start in starts]
    assert all(later.start >= earlier.end for earlier, later in zip(cues, cues[1:])), "pieces merge in order, without overlap"


def test_no_speech_files_are_left_behind(tmp_path: Path, heard: MetaStub, monkeypatch: pytest.MonkeyPatch) -> None:
    """An hour of 16 kHz speech is 115 MB, and a long talk would leave it twice over."""
    monkeypatch.setattr(captions, "MAX_PIECE_SECONDS", 4.0)
    monkeypatch.setattr(captions, "_PIECE_SEARCH_SECONDS", 2.0)
    source = _make(tmp_path / "long.wav", "-f", "lavfi", "-i", _tone(9.0, 16000), "-ac", "1")
    work = tmp_path / "work"
    transcribe(source, work, meta_dir=tmp_path / "ledger")
    assert len(heard.calls) >= 2 and not list(work.glob("*.wav"))


def test_meta_picks_the_language_unless_told(tmp_path: Path, heard: MetaStub) -> None:
    source = _make(tmp_path / "talk.wav", "-f", "lavfi", "-i", _tone(), "-ac", "1")
    transcribe(source, tmp_path / "a")
    transcribe(source, tmp_path / "b", language="pt-BR")
    assert [call["language"] for call in heard.calls] == ["auto", "pt-BR"]


# 5. The render's own listen hears only kept audio, and never silence.

def _spans(*pieces: tuple[float, float]) -> list[Span]:
    spans, at = [], 0.0
    for index, (start, end) in enumerate(pieces, 1):
        spans.append(Span(segment=index, source_start=start, source_end=end, output_start=at, output_end=at + end - start, speed=1.0))
        at += end - start
    return spans


def test_the_kept_audio_is_heard_on_the_concat_clock(tmp_path: Path, heard: MetaStub) -> None:
    source = _make(tmp_path / "talk.mp4", "-f", "lavfi", "-i", "color=c=teal:s=64x64:r=10:d=4", "-f", "lavfi", "-i", _tone(4.0),
                   "-af", "volume=enable='between(t,1,2)':volume=0", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest")
    cues = transcribe_kept(source, _spans((0.0, 1.0), (2.0, 3.0)), tmp_path / "work", meta_dir=tmp_path / "ledger")
    assert len(heard.calls) == 1 and heard.calls[0]["seconds"] == pytest.approx(2.0, abs=0.03)
    assert (heard.calls[0]["rate"], heard.calls[0]["channels"]) == (16000, 1)
    assert [cue.text for cue in cues] == ["This is the rough rough blank."], "joined doubles are mended on this path too"


def test_kept_silence_is_never_sent(tmp_path: Path, heard: MetaStub) -> None:
    source = _make(tmp_path / "talk.mp4", "-f", "lavfi", "-i", "color=c=teal:s=64x64:r=10:d=4", "-f", "lavfi", "-i", _tone(4.0),
                   "-af", "volume=enable='between(t,1,3)':volume=0", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest")
    assert transcribe_kept(source, _spans((1.2, 2.8)), tmp_path / "work") == [] and heard.calls == []


# 6. Timing after cuts.

def test_a_line_that_trails_into_the_closing_cut_still_shows() -> None:
    """Meta's turns end a little after the voice. Cutting the closing pause must not drop the last line."""
    keeps = [Range(0.0, 5.0), Range(6.0, 10.0)]
    cues = remap_cues([Cue(1.0, 2.0, "First."), Cue(9.2, 10.6, "Last words."), Cue(10.8, 11.5, "uh")], keeps)
    assert [(cue.text, cue.start, cue.end) for cue in cues] == [("First.", 1.0, 2.0), ("Last words.", pytest.approx(8.2), pytest.approx(9.0))]


# 7. Loudness and pause trimming follow the asked target and the played track.

def test_loudness_is_measured_against_the_target_it_will_meet(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[str] = []

    class Done:
        stderr = '{"input_i": "-30.0", "input_tp": "-10.0", "input_lra": "5.0", "input_thresh": "-40.0", "target_offset": "0.4"}'

    monkeypatch.setattr(analyze, "run_ffmpeg", lambda argv, **_: seen.append(" ".join(argv)) or Done())
    analyze.measure_loudness(tmp_path / "a.wav", i=-23.0, tp=-2.0, lra=7.0)
    assert "loudnorm=I=-23.0:TP=-2.0:LRA=7.0:print_format=json" in seen[0], "target_offset is only right for the target it was measured at"


def test_pause_trimming_hears_the_track_the_editor_plays(tmp_path: Path) -> None:
    # Track 0: stereo silence (FFmpeg's own pick, by channel count). Track 1, the default: speech with a pause.
    source = _make(tmp_path / "two.mkv", "-f", "lavfi", "-i", "color=c=teal:s=64x64:r=10:d=3", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=3",
                   "-f", "lavfi", "-i", _tone(3.0), "-filter_complex", "[2:a]volume=enable='between(t,1,2)':volume=0[speech]",
                   "-map", "0:v", "-map", "1:a", "-map", "[speech]", "-disposition:a:0", "0", "-disposition:a:1", "default",
                   "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le", "-t", "3")
    keeps = analyze.apply_cut_silence(source, tmp_path / "cut.mp4", tmp_path / "keeps.json", width=64, height=64, fps=10)
    assert len(keeps) == 2 and keeps[0].end == pytest.approx(1.12, abs=0.05) and keeps[1].start == pytest.approx(1.88, abs=0.05)
    assert probe(tmp_path / "cut.mp4").duration == pytest.approx(2.24, abs=0.15)


# 8. Speech bands on a quiet recording.

def test_quiet_speech_still_has_speech_and_pauses(tmp_path: Path) -> None:
    """A voice peaking near -48 dB sits wholly under a fixed -35 dB pause level. Its own floor and peak set the level instead."""
    source = _make(tmp_path / "quiet.wav", "-f", "lavfi", "-i", _tone(3.0), "-f", "lavfi", "-i", "anoisesrc=c=pink:r=48000:a=0.0003:d=3",
                   "-filter_complex", "[0:a]volume=-30dB,volume=enable='between(t,1,1.8)':volume=0[v];[v][1:a]amix=inputs=2:normalize=0", "-ac", "1")
    silences = analyze.detect_silence(source, threshold_db=-35.0, min_silence=0.35)
    assert [(round(s.start, 1), round(s.end, 1)) for s in silences] == [(1.0, 1.8)]


def test_a_normal_recording_keeps_the_threshold_it_was_given(tmp_path: Path) -> None:
    # Room noise at about -40 dB between loud words: silence at -35 dB, and the threshold must not drop under it.
    source = _make(tmp_path / "room.wav", "-f", "lavfi", "-i", _tone(3.0), "-f", "lavfi", "-i", "anoisesrc=c=pink:r=48000:a=0.01:d=3",
                   "-filter_complex", "[0:a]volume=-3dB,volume=enable='between(t,1,1.8)':volume=0[v];[v][1:a]amix=inputs=2:normalize=0", "-ac", "1")
    silences = analyze.detect_silence(source, threshold_db=-35.0, min_silence=0.35)
    assert [(round(s.start, 1), round(s.end, 1)) for s in silences] == [(1.0, 1.8)]
    assert analyze.detect_silence(source, threshold_db=-60.0, min_silence=0.35) == [], "the room is louder than -60 dB, and stays so"


def test_a_noisy_room_still_has_pauses_at_its_measured_level(tmp_path: Path) -> None:
    """Room noise near -30 dB never falls under a fixed -35 dB line. Measured from this recording's loudness, it does."""
    source = _make(tmp_path / "room.wav", "-f", "lavfi", "-i", _tone(3.0), "-f", "lavfi", "-i", "anoisesrc=c=pink:r=48000:a=0.04:d=3",
                   "-filter_complex", "[0:a]volume=12dB,volume=enable='between(t,1,1.8)':volume=0[v];[v][1:a]amix=inputs=2:normalize=0", "-ac", "1")
    assert analyze.detect_silence(source, threshold_db=-35.0, min_silence=0.35) == []
    assert [(round(s.start, 1), round(s.end, 1)) for s in analyze.detect_silence(source, min_silence=0.35)] == [(1.0, 1.8)]


@pytest.mark.parametrize("level", ["volume=0dB", "volume=-30dB"])
def test_the_measured_level_finds_the_same_pause_in_typical_and_quiet_speech(tmp_path: Path, level: str) -> None:
    source = _make(tmp_path / "talk.wav", "-f", "lavfi", "-i", _tone(3.0), "-f", "lavfi", "-i", "anoisesrc=c=pink:r=48000:a=0.0003:d=3",
                   "-filter_complex", f"[0:a]{level},volume=enable='between(t,1,1.8)':volume=0[v];[v][1:a]amix=inputs=2:normalize=0", "-ac", "1")
    assert [(round(s.start, 1), round(s.end, 1)) for s in analyze.detect_silence(source, min_silence=0.35)] == [(1.0, 1.8)]


def test_the_voice_gate_waits_for_a_clear_margin_and_spares_quiet_syllables() -> None:
    import math

    assert sound.voice_settings({"noise": -60.0, "speech": -45.0})["gate"] == 0.001, "under 16 dB of margin the expander stays out"
    opened = sound.voice_settings({"noise": -60.0, "speech": -44.0})["gate"]
    assert -44.0 - 20 * math.log10(opened) == pytest.approx(10.0, abs=0.5), "it opens about 10 dB under the voice"


def test_a_transcript_the_budget_cannot_finish_is_refused_before_anything_is_sent(tmp_path: Path, heard: MetaStub, monkeypatch: pytest.MonkeyPatch) -> None:
    """A two-hour talk at a $0.25 cap would otherwise pay for eight pieces and fail on the ninth."""
    monkeypatch.setattr(captions, "MAX_PIECE_SECONDS", 4.0)
    monkeypatch.setattr(captions, "_PIECE_SEARCH_SECONDS", 2.0)
    source = _make(tmp_path / "talk.wav", "-f", "lavfi", "-i", _tone(12.0, 16000), "-ac", "1")
    most = 12 / 3600 * meta._TRANSCRIBE_PRICE_PER_HOUR
    with pytest.raises(meta.MetaError, match="Raise the budget"):
        transcribe(source, tmp_path / "work", budget_usd=most / 2)
    assert heard.calls == [], "nothing was sent or paid for"
    transcribe(source, tmp_path / "work", budget_usd=most * 2)
    assert len(heard.calls) >= 3


def test_audible_can_listen_to_one_stretch(tmp_path: Path) -> None:
    source = _make(tmp_path / "talk.wav", "-f", "lavfi", "-i", _tone(4.0), "-af", "volume=enable='gt(t,2)':volume=0", "-ac", "1")
    assert audible(source, seconds=2.0) and not audible(source, start=2.2, seconds=1.5)
