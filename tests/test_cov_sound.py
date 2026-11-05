"""`vedit.sound`: the tape measurement render.sh runs, its command files, and the pure level helpers."""

from __future__ import annotations

import array
import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

from vedit import sound
from vedit.media import resolve_ffmpeg

RATE = 48_000


def _have_ffmpeg() -> bool:
    try:
        resolve_ffmpeg()
        return True
    except Exception:
        return False


render = pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg required")


TONE = f"sine=frequency=440:sample_rate={RATE}"
# Half a second of voice, half a second of faint room noise, over and over.
TALK = f"aevalsrc=if(lt(mod(t\\,1)\\,0.5)\\,0.3*sin(2*PI*220*t)\\,0.003*random(0)):s={RATE}"


def _wav(path: Path, graph: str, seconds: float = 1.0) -> Path:
    """``graph`` is a complete lavfi graph; the file runs ``seconds``."""
    subprocess.run(
        [
            resolve_ffmpeg(),
            "-hide_banner",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            graph,
            "-t",
            str(seconds),
            str(path),
        ],
        check=True,
    )
    return path


def _samples(*levels: float, seconds_each: float) -> array.array:
    """Mono 16-bit samples at the measurement rate: a run of one constant level per entry, so RMS is known."""
    out = array.array("h")
    for level in levels:
        value = int(32767 * level)
        out.extend([value] * int(16_000 * seconds_each))
    return out


def test_level_stats_refuses_a_tape_too_short_to_measure() -> None:
    with pytest.raises(ValueError, match="too little audio to measure"):
        sound.level_stats(_samples(0.03, seconds_each=0.1))  # 100 ms is a few windows, not a measurement
    with pytest.raises(ValueError):
        sound.level_stats(array.array("h"))


def test_level_stats_separates_the_room_from_the_voice() -> None:
    samples = _samples(0.01, 0.3, seconds_each=1.5)  # half room at -40 dBFS, half voice at about -10.5 dBFS
    stats = sound.level_stats(samples)
    assert abs(stats["noise"] - 20 * math.log10(0.01)) < 1.0, stats
    assert abs(stats["speech"] - 20 * math.log10(0.3)) < 1.0, stats


def test_level_stats_never_returns_minus_infinity_for_digital_silence() -> None:
    stats = sound.level_stats(_samples(0.0, seconds_each=1.0))
    assert math.isfinite(stats["noise"]) and math.isfinite(stats["speech"])
    assert stats["noise"] < -100


def test_voice_settings_stay_inside_the_filters_limits_at_the_extremes() -> None:
    loud = sound.voice_settings({"noise": -5.0, "speech": -1.0})
    assert -80.0 <= loud["noise_floor"] <= -20.0
    assert loud["compress"] <= 1.0 and loud["makeup"] >= 1.0
    hushed = sound.voice_settings({"noise": -95.0, "speech": -60.0})
    assert hushed["noise_floor"] == -80.0, "afftdn's floor stops at -80 dB"
    assert hushed["gate"] > 0


def test_voice_commands_write_nothing_when_nothing_is_asked_for() -> None:
    settings = sound.voice_settings({"noise": -40, "speech": -20})
    assert sound.voice_commands(settings, denoise=False, enhance=False) == ""
    text = sound.voice_commands(settings, denoise=True, enhance=True)
    assert text.endswith(";\n") and text.count(";\n") == 5
    gate_in = next(line for line in text.splitlines() if "volume@voice_in" in line)
    gate_out = next(line for line in text.splitlines() if "volume@voice_out" in line)
    scale_in = float(gate_in.split()[-1].rstrip(";"))
    scale_out = float(gate_out.split()[-1].rstrip(";"))
    assert abs(scale_in * scale_out - 1.0) < 1e-3, "the gain moved onto the gate is taken back off afterwards"


def test_voice_filters_without_a_command_file_use_the_defaults_and_say_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sound, "has_filter", lambda name: True)
    notes: list[str] = []
    chain = sound.voice_filters(denoise=True, enhance=True, commands=None, warn=notes.append)
    assert notes == [], "no command file was promised, so nothing is missing"
    assert chain[0] == "highpass=f=80"
    assert not any(part.startswith("asendcmd") for part in chain)
    denoiser = next(part for part in chain if part.startswith("afftdn@voice=nr=20:nf="))
    assert -80 <= float(denoiser.split("nf=")[1]) <= -20, "the typical floor is one afftdn accepts"
    late = round(sound.DENOISE_LATENCY * 48_000)
    assert f"apad=pad_len={late}" in chain and f"atrim=start_sample={late}" in chain, "afftdn's latency is removed"
    assert chain.index("apad=pad_len=1200") < chain.index("atrim=start_sample=1200")
    assert chain[-1].startswith("acompressor@voice=") and "deesser=i=0.25" in chain


def test_voice_filters_warn_when_this_ffmpeg_cannot_follow_commands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sound, "has_filter", lambda name: name not in {"asendcmd", "afftdn", "deesser"})
    notes: list[str] = []
    chain = sound.voice_filters(denoise=True, enhance=True, commands=tmp_path / "voice.cmd", warn=notes.append)
    assert "This FFmpeg cannot apply measured speech settings, so typical ones were used." in notes
    assert "This FFmpeg has no afftdn filter, so background noise was not reduced." in notes
    assert not any("afftdn" in part or "asendcmd" in part or "deesser" in part for part in chain)
    assert any(part.startswith("agate=") for part in chain), "the gate still runs without the denoiser"


def test_voice_filters_escape_the_command_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sound, "has_filter", lambda name: True)
    commands = tmp_path / "it's here, ok.cmd"
    chain = sound.voice_filters(denoise=False, enhance=False, commands=commands)
    assert chain[0].startswith("asendcmd=f=")
    assert "\\'" in chain[0] and "\\," in chain[0], "apostrophes and commas in the path are escaped for the graph"
    assert chain[1:] == ["highpass=f=80"]


def test_duck_filter_is_a_sidechain_compressor_keyed_at_the_shared_threshold() -> None:
    text = sound.duck_filter()
    assert text.startswith("sidechaincompress=")
    assert f"threshold={sound.DUCK_THRESHOLD}" in text and "release=380" in text


def test_music_commands_level_the_bed_and_key_the_duck_from_the_voice() -> None:
    quiet_bed = sound.music_commands({"speech": -40.0}, None)
    assert quiet_bed.splitlines() == ["0.0 volume@music_level volume 15.84893;"]
    loud_bed = sound.music_commands({"speech": 10.0}, None)
    assert "volume 0.10000;" in loud_bed, "a hot bed is pulled down no further than the floor"
    keyed = sound.music_commands({"speech": -16.0}, {"noise": -60.0, "speech": -30.0})
    lines = keyed.splitlines()
    assert lines[0] == "0.0 volume@music_level volume 1.00000;"
    assert lines[1].startswith("0.0 volume@duck_key volume ")
    assert abs(float(lines[1].split()[-1].rstrip(";")) - 10 ** ((-20.0 + 30.0) / 20)) < 1e-4
    roomy = sound.music_commands({"speech": -16.0}, {"noise": -31.0, "speech": -30.0})
    assert roomy.splitlines()[1] == "0.0 volume@duck_key volume 0.25000;", "room tone alone must not duck the bed"


def test_bundled_music_lists_only_beds_that_exist_on_disk() -> None:
    beds = sound.bundled_music()
    ids = [bed["id"] for bed in beds]
    assert ids == [ident for ident, *_ in sound.BUNDLED_MUSIC if ident in ids]
    for bed in beds:
        assert Path(bed["path"]).is_file() and bed["bpm"] > 0 and bed["name"] and bed["description"]


@render
def test_decode_mono_reads_a_window_at_the_asked_rate(tmp_path: Path) -> None:
    tone = _wav(tmp_path / "tone.wav", f"{TONE},volume=-6dB", seconds=2.0)
    whole = sound.decode_mono(tone, rate=8000)
    assert abs(len(whole) - 16000) <= 80
    part = sound.decode_mono(tone, rate=8000, start=1.0, seconds=0.5)
    assert abs(len(part) - 4000) <= 80
    assert max(abs(v) for v in part) > 1000, "the window holds the tone"
    hush = _wav(tmp_path / "hush.wav", f"anullsrc=cl=mono:r={RATE}", seconds=1.0)
    assert max(abs(v) for v in sound.decode_mono(hush, rate=8000)) == 0


@render
def test_decode_mono_names_the_file_it_could_not_read(tmp_path: Path) -> None:
    missing = tmp_path / "missing.wav"
    with pytest.raises(ValueError, match=r"could not decode audio from .*missing\.wav"):
        sound.decode_mono(missing)
    junk = tmp_path / "junk.wav"
    junk.write_bytes(b"not a wave file at all")
    with pytest.raises(ValueError, match="could not decode audio"):
        sound.decode_mono(junk)


@render
def test_measure_voice_finds_the_room_under_a_voice(tmp_path: Path) -> None:
    tape = _wav(tmp_path / "talk.wav", TALK, seconds=3.0)
    settings = sound.measure_voice(tape)
    assert set(settings) == {"noise_floor", "gate", "compress", "makeup"}
    assert -80 <= settings["noise_floor"] <= -20
    assert settings["gate"] > 0.001, "a clear voice over a quiet room earns a working expander"


@render
def test_the_measure_program_writes_commands_and_settings(tmp_path: Path) -> None:
    tape = _wav(tmp_path / "talk.wav", TALK, seconds=3.0)
    out = tmp_path / "voice.cmd"
    assert sound.main(["--in", str(tape), "--out", str(out), "--denoise", "--enhance"]) == 0
    text = out.read_text(encoding="utf-8")
    assert "afftdn@voice noise_floor" in text and "acompressor@voice makeup" in text
    settings = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
    assert settings == sound.measure_voice(tape)


@render
def test_a_tape_too_short_to_measure_gets_the_typical_settings(tmp_path: Path) -> None:
    blip = _wav(tmp_path / "blip.wav", TONE, seconds=0.05)
    hush = _wav(tmp_path / "hush.wav", f"anullsrc=cl=mono:r={RATE}", seconds=0.1)
    settings = []
    for tape in (blip, hush):
        out = tmp_path / f"{tape.stem}.cmd"
        assert sound.main(["--in", str(tape), "--out", str(out), "--denoise"]) == 0
        written = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
        assert set(written) == {"noise_floor", "gate", "compress", "makeup"}
        assert out.read_text(encoding="utf-8") == sound.voice_commands(written, denoise=True, enhance=False)
        settings.append(written)
    assert settings[0] == settings[1], "every tape too short to measure gets the same typical settings"
    with pytest.raises(ValueError):
        sound.measure_voice(blip)


@render
def test_the_bed_mode_levels_music_against_the_voice(tmp_path: Path) -> None:
    bed = _wav(tmp_path / "bed.wav", f"sine=frequency=110:sample_rate={RATE},volume=-26dB", seconds=2.0)
    voice = _wav(tmp_path / "talk.wav", TALK, seconds=3.0)
    out = tmp_path / "music.cmd"
    assert sound.main(["--bed", str(bed), "--in", str(voice), "--out", str(out)]) == 0
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("0.0 volume@music_level volume ") and lines[1].startswith("0.0 volume@duck_key volume ")
    gain = float(lines[0].split()[-1].rstrip(";"))
    assert gain > 1.0, "a -26 dBFS bed is lifted toward -16"
    assert not out.with_suffix(".json").exists(), "bed mode writes no speech settings"

    alone = tmp_path / "alone.cmd"
    assert sound.main(["--bed", str(bed), "--out", str(alone)]) == 0
    assert len(alone.read_text(encoding="utf-8").splitlines()) == 1, "no voice, no duck key"


@render
def test_an_unreadable_bed_falls_back_to_unity_gain(tmp_path: Path) -> None:
    out = tmp_path / "music.cmd"
    assert sound.main(["--bed", str(tmp_path / "missing.mp3"), "--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == "0.0 volume@music_level volume 1.00000;\n"


@render
def test_the_module_runs_as_a_program(tmp_path: Path) -> None:
    tape = _wav(tmp_path / "talk.wav", f"sine=frequency=220:sample_rate={RATE},volume=-14dB", seconds=2.0)
    out = tmp_path / "voice.cmd"
    done = subprocess.run(
        [sys.executable, "-m", "vedit.sound", "--in", str(tape), "--out", str(out), "--denoise"],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    assert out.is_file() and out.with_suffix(".json").is_file()
    usage = subprocess.run(
        [sys.executable, "-m", "vedit.sound"], check=False, capture_output=True, text=True, timeout=60
    )
    assert usage.returncode == 2 and "--out" in usage.stderr
