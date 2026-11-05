"""`vedit.analyze`: the render-time helpers render.sh calls, run on small real files and proven by probing."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from vedit import analyze
from vedit.analyze import Range, keep_ranges, loudnorm_filter, measure_loudness
from vedit.errors import AnalysisError
from vedit.media import probe, resolve_ffmpeg

RATE = 48_000


def _have_ffmpeg() -> bool:
    try:
        resolve_ffmpeg()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg required")


def _ffmpeg(*args: str) -> None:
    subprocess.run([resolve_ffmpeg(), "-hide_banner", "-v", "error", "-y", *args], check=True)


TONE = f"sine=frequency=440:sample_rate={RATE}"
QUIET_TONE = f"{TONE},volume=-26dB"


def _clip(path: Path, *, seconds: float = 1.0, audio: str | None = TONE, size: str = "320x180") -> Path:
    """A test pattern with the sound of ``audio`` (a complete lavfi graph), ``seconds`` long."""
    command = ["-f", "lavfi", "-i", f"testsrc2=s={size}:r=30"]
    if audio:
        command += ["-f", "lavfi", "-i", audio]
    _ffmpeg(
        *command,
        "-t",
        str(seconds),
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        str(path),
    )
    return path


def _two_shots(path: Path) -> Path:
    """Red for one second, then blue: one hard cut at 1.0 s."""
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "color=c=red:s=160x90:r=30:d=1",
        "-f",
        "lavfi",
        "-i",
        "color=c=blue:s=160x90:r=30:d=1",
        "-filter_complex",
        "[0:v][1:v]concat=n=2:v=1:a=0[v]",
        "-map",
        "[v]",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        str(path),
    )
    return path


def _speech_with_gap(path: Path, *, gap: tuple[float, float] = (1.0, 2.0), total: float = 3.0) -> Path:
    """A loud tone with digital silence between ``gap``, plus a picture, as a spoken clip with one pause."""
    start, end = gap
    expr = f"if(between(t\\,{start}\\,{end})\\,0\\,0.5*sin(2*PI*220*t))"
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=s=160x90:r=30",
        "-f",
        "lavfi",
        "-i",
        f"aevalsrc={expr}:s={RATE}",
        "-t",
        str(total),
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        str(path),
    )
    return path


def _wav(path: Path, graph: str, seconds: float = 1.0) -> Path:
    _ffmpeg("-f", "lavfi", "-i", graph, "-t", str(seconds), str(path))
    return path


def test_keep_ranges_refuses_a_non_positive_duration() -> None:
    with pytest.raises(AnalysisError, match="duration must be positive"):
        keep_ranges(0.0, [])
    with pytest.raises(AnalysisError):
        keep_ranges(-1.0, [Range(0.0, 0.5)])


def test_keep_ranges_drops_slivers_shorter_than_min_keep() -> None:
    keeps = keep_ranges(10.0, [Range(0.0, 1.0), Range(1.02, 9.9)], pad=0.0, min_keep=0.05)
    assert keeps == [Range(9.9, 10.0)], "the 20 ms between two pauses is not worth a cut"


def test_loudnorm_filter_clamps_a_clipped_master_into_the_options_bounds() -> None:
    base = loudnorm_filter(i=-16.0, tp=-2.0, lra=7.0)
    assert base == "loudnorm=I=-16.0:TP=-2.0:LRA=7.0"
    clipped = loudnorm_filter(
        {"input_i": 3.2, "input_tp": 1.1, "input_lra": -2.0, "input_thresh": 5.0, "target_offset": 150.0}
    )
    assert "measured_I=0.0:" in clipped, "loudnorm refuses measured_I above 0 LUFS"
    assert "measured_LRA=0.0:" in clipped and "measured_thresh=0.0:" in clipped and "offset=99.0:" in clipped
    assert clipped.endswith(":linear=true")


def test_scenes_subcommand_writes_the_cut_time(tmp_path: Path) -> None:
    clip = _two_shots(tmp_path / "shots.mp4")
    out = tmp_path / "scenes.json"
    assert analyze.main(["scenes", "--in", str(clip), "--out", str(out), "--threshold", "0.3"]) == 0
    times = json.loads(out.read_text(encoding="utf-8"))["times"]
    assert len(times) == 1, f"one hard cut, found {times}"
    assert abs(times[0] - 1.0) < 0.1


def test_detect_scenes_finds_nothing_in_a_steady_shot(tmp_path: Path) -> None:
    clip = _clip(tmp_path / "steady.mp4", audio=None)
    assert analyze.detect_scenes(clip) == []


def test_audible_hears_a_tone_and_not_digital_silence(tmp_path: Path) -> None:
    tone = _wav(tmp_path / "tone.wav", TONE)
    quiet = _wav(tmp_path / "quiet.wav", f"anullsrc=cl=mono:r={RATE}")
    assert analyze.audible(tone) is True
    assert analyze.audible(quiet) is False


def test_audible_can_listen_to_one_stretch_of_a_file(tmp_path: Path) -> None:
    clip = _speech_with_gap(tmp_path / "gap.mp4", gap=(1.0, 2.0), total=3.0)
    assert analyze.audible(clip, start=1.2, seconds=0.6) is False, "the pause is silent"
    assert analyze.audible(clip, start=0.0, seconds=0.8) is True
    assert analyze.audible(clip, start=2.2, seconds=0.6) is True


def test_measure_loudness_returns_none_for_digital_silence(tmp_path: Path) -> None:
    quiet = _wav(tmp_path / "quiet.wav", f"anullsrc=cl=stereo:r={RATE}")
    assert measure_loudness(quiet) is None


def test_measure_loudness_reports_the_tone_level(tmp_path: Path) -> None:
    tone = _wav(tmp_path / "tone.wav", TONE, seconds=2.0)
    softer = _wav(tmp_path / "softer.wav", f"{TONE},volume=-20dB", seconds=2.0)
    measured = measure_loudness(tone)
    quieter = measure_loudness(softer)
    assert measured is not None and quieter is not None
    assert set(measured) == {"input_i", "input_tp", "input_lra", "input_thresh", "target_offset"}
    assert abs((measured["input_i"] - quieter["input_i"]) - 20.0) < 0.5, "20 dB less gain reads 20 LU quieter"
    assert abs((measured["input_tp"] - quieter["input_tp"]) - 20.0) < 0.5
    assert measured["input_tp"] < 0 and quieter["input_i"] > -60.0


def test_loudnorm_subcommand_brings_a_quiet_track_to_the_target(tmp_path: Path) -> None:
    quiet = _wav(tmp_path / "quiet.wav", f"{TONE},volume=-14dB", seconds=3.0)
    before = measure_loudness(quiet)
    assert before is not None and before["input_i"] < -30
    out = tmp_path / "level.m4a"
    assert analyze.main(["loudnorm", "--in", str(quiet), "--out", str(out), "--i", "-16"]) == 0
    after = measure_loudness(out)
    assert after is not None
    assert abs(after["input_i"] - (-16.0)) < 2.0, after
    assert probe(out).has_video is False, "an audio-only loudnorm writes no picture"


def test_loudnorm_subcommand_with_video_keeps_the_picture(tmp_path: Path) -> None:
    clip = _clip(tmp_path / "clip.mp4", audio=QUIET_TONE)
    out = tmp_path / "level.mp4"
    assert analyze.main(["loudnorm", "--in", str(clip), "--out", str(out), "--video"]) == 0
    info = probe(out)
    assert info.has_video and info.has_audio and (info.width, info.height) == (320, 180)


def test_cut_silence_subcommand_removes_the_pause_and_writes_the_keeps(tmp_path: Path) -> None:
    clip = _speech_with_gap(tmp_path / "gap.mp4", gap=(1.0, 2.0), total=3.0)
    out = tmp_path / "cut.mp4"
    keeps = tmp_path / "keeps.json"
    code = analyze.main(
        [
            "cut-silence",
            "--in",
            str(clip),
            "--out",
            str(out),
            "--keeps",
            str(keeps),
            "--threshold-db",
            "-40",
            "--min-silence",
            "0.4",
            "--pad",
            "0.1",
            "--width",
            "160",
            "--height",
            "90",
        ]
    )
    assert code == 0
    written = json.loads(keeps.read_text(encoding="utf-8"))
    assert len(written["silences"]) == 1
    silence = written["silences"][0]
    assert abs(silence["start"] - 1.0) < 0.15 and abs(silence["end"] - 2.0) < 0.15
    assert [round(k["start"], 1) for k in written["keeps"]] == [0.0, 1.9]
    info = probe(out)
    assert info.has_video and info.has_audio
    assert (info.width, info.height) == (160, 90)
    assert 1.9 <= info.duration <= 2.5, f"one second of pause removed from three, got {info.duration}"
    assert not out.with_name("cut.part.mp4").exists(), "the partial file is renamed into place"


def test_cut_silence_refuses_a_clip_without_sound(tmp_path: Path) -> None:
    clip = _clip(tmp_path / "mute.mp4", audio=None)
    with pytest.raises(AnalysisError, match="cut_silence requires an audio track"):
        analyze.apply_cut_silence(clip, tmp_path / "out.mp4", tmp_path / "keeps.json")
    assert not (tmp_path / "keeps.json").exists()


def test_encode_subcommand_applies_the_preset_loudnorm_and_fade(tmp_path: Path) -> None:
    clip = _clip(tmp_path / "clip.mp4", seconds=2.0, audio=QUIET_TONE)
    out = tmp_path / "final.mp4"
    code = analyze.main(
        [
            "encode",
            "--in",
            str(clip),
            "--out",
            str(out),
            "--vf",
            "scale=960:540",
            "--loudnorm",
            "--i",
            "-16",
            "--preset",
            "web_preview",
            "--no-hw",
            "--video",
            "--audio",
            "--fade-out",
            "0.5",
        ]
    )
    assert code == 0
    info = probe(out)
    assert (info.width, info.height) == (960, 540)
    assert info.has_audio and info.has_video
    assert abs(info.duration - 2.0) < 0.15, "loudnorm's padding is trimmed back to the picture's end"
    level = measure_loudness(out)
    assert level is not None and abs(level["input_i"] - (-16.0)) < 2.5, level
    assert not out.with_name("final.part.mp4").exists()


def test_encode_subcommand_can_write_a_gif_from_a_silent_source(tmp_path: Path) -> None:
    clip = _clip(tmp_path / "clip.mp4", audio=None)
    out = tmp_path / "loop.gif"
    code = analyze.main(
        ["encode", "--in", str(clip), "--out", str(out), "--preset", "export_320x180_10_gif", "--video", "--no-hw"]
    )
    assert code == 0
    info = probe(out)
    assert info.has_video and not info.has_audio
    assert (info.width, info.height) == (320, 180)
    assert out.read_bytes()[:6] in (b"GIF89a", b"GIF87a")


def test_encode_refuses_an_unknown_preset_before_touching_the_output(tmp_path: Path) -> None:
    clip = _clip(tmp_path / "clip.mp4")
    out = tmp_path / "final.mp4"
    with pytest.raises(KeyError):
        analyze.apply_encode(clip, out, preset="betamax_2000")
    assert not out.exists()


def test_verify_subcommand_writes_the_proof_for_a_good_file(tmp_path: Path) -> None:
    clip = _clip(tmp_path / "clip.mp4", seconds=1.0)
    out = tmp_path / "verify.json"
    code = analyze.main(
        [
            "verify",
            "--in",
            str(clip),
            "--out",
            str(out),
            "--width",
            "320",
            "--height",
            "180",
            "--audio",
            "--seconds",
            "1.0",
            "--tolerance",
            "0.3",
            "--no-color",
        ]
    )
    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["path"] == str(clip)
    assert (payload["width"], payload["height"]) == (320, 180)
    assert payload["has_audio"] is True and payload["has_video"] is True
    assert payload["expected"] == {
        "width": 320,
        "height": 180,
        "has_audio": True,
        "seconds": 1.0,
        "tolerance": 0.3,
        "color": False,
    }
    assert payload["loudness"] is not None and payload["loudness"]["input_i"] < 0
    assert payload["color"]["pix_fmt"] == "yuv420p"
    assert "vmaf_log" not in payload, "VMAF is opt-in"


def test_verify_subcommand_without_a_size_checks_only_sound_and_length(tmp_path: Path) -> None:
    clip = _clip(tmp_path / "clip.mp4", audio=None)
    out = tmp_path / "verify.json"
    assert analyze.main(["verify", "--in", str(clip), "--out", str(out), "--no-audio", "--seconds", "1"]) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["has_audio"] is False and "loudness" not in payload
    with pytest.raises(AnalysisError, match="lost its sound"):
        analyze.apply_verify(clip, out, has_audio=True)
    with pytest.raises(AnalysisError, match=r"runs 1\.\d+s, not the expected 4\.000s"):
        analyze.apply_verify(clip, out, seconds=4.0, tolerance=0.2)


def test_verify_refuses_a_file_with_sound_it_should_not_have(tmp_path: Path) -> None:
    clip = _clip(tmp_path / "clip.mp4")
    with pytest.raises(AnalysisError, match="carries sound it should not"):
        analyze.apply_verify(clip, tmp_path / "v.json", has_audio=False)


def test_verify_with_a_size_fails_the_wrong_frame_in_plain_words(tmp_path: Path) -> None:
    clip = _clip(tmp_path / "clip.mp4")
    out = tmp_path / "verify.json"
    with pytest.raises(AnalysisError, match=r"came out 320x180 instead of 1920x1080"):
        analyze.main(["verify", "--in", str(clip), "--out", str(out), "--width", "1920", "--height", "1080", "--audio"])
    assert not out.exists(), "no proof is written for a file that failed"


def test_verify_refuses_a_file_with_no_streams_and_a_file_too_short_to_be_media(tmp_path: Path) -> None:
    short = tmp_path / "blink.mp4"
    _ffmpeg(
        "-f", "lavfi", "-i", "color=c=red:s=64x64:r=30:d=0.034", "-frames:v", "1", "-pix_fmt", "yuv420p", str(short)
    )
    with pytest.raises(AnalysisError, match=r"verify failed: duration"):
        analyze.apply_verify(short, tmp_path / "v.json")


def test_vmaf_only_runs_when_asked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clip = _clip(tmp_path / "clip.mp4", audio=None)
    out = tmp_path / "verify.json"
    monkeypatch.delenv("VEDIT_VMAF", raising=False)
    payload = analyze.apply_verify(clip, out, ref=clip)
    assert "vmaf_log" not in payload and "vmaf" not in payload
    assert not out.with_suffix(".vmaf.json").exists()


def test_the_module_runs_as_a_program(tmp_path: Path) -> None:
    import sys

    clip = _two_shots(tmp_path / "shots.mp4")
    out = tmp_path / "scenes.json"
    done = subprocess.run(
        [sys.executable, "-m", "vedit.analyze", "scenes", "--in", str(clip), "--out", str(out)],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(out.read_text(encoding="utf-8"))["times"]
    usage = subprocess.run(
        [sys.executable, "-m", "vedit.analyze"], check=False, capture_output=True, text=True, timeout=60
    )
    assert usage.returncode == 2 and "usage: vedit.analyze" in usage.stderr


def test_measure_loudness_refuses_a_block_whose_number_is_not_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """A loudnorm field that is not a number is an AnalysisError in plain words, not a TypeError from float()."""
    from vedit.media import CommandResult

    def fake_run(args: list[str], **_: object) -> CommandResult:
        block = {
            "input_i": "-23.0",
            "input_tp": "-5.0",
            "input_lra": "7.0",
            "input_thresh": None,
            "target_offset": "0.5",
        }
        return CommandResult(
            argv=args, returncode=0, stdout="", stderr="[Parsed_loudnorm_0]\n" + json.dumps(block), duration_ms=1
        )

    monkeypatch.setattr(analyze, "run_ffmpeg", fake_run)
    with pytest.raises(AnalysisError, match="input_thresh"):
        measure_loudness("anything.wav")


def test_keep_ranges_does_not_pad_a_silence_that_reaches_the_edge_of_the_file() -> None:
    """Silence from the first sample to the last keeps nothing; padding only pulls speech back from silence."""
    assert keep_ranges(2.0, [Range(0.0, 2.0)]) == []
    assert keep_ranges(2.0, [Range(0.0, 1.99)]) == [], "silencedetect can stop a frame short of the end"
    # A silence in the middle still gives the speech either side its pad.
    assert keep_ranges(2.0, [Range(0.8, 1.2)]) == [Range(0.0, 0.92), Range(1.08, 2.0)]
    # A silence that opens the file pads only the speech that follows it.
    assert keep_ranges(2.0, [Range(0.0, 1.0)]) == [Range(0.88, 2.0)]


def test_cut_silence_refuses_a_file_that_is_all_silence(tmp_path: Path) -> None:
    """A silent clip is an error in plain words, not a 0.2 s file made of two padded slivers of silence."""
    quiet = _clip(tmp_path / "quiet.mp4", seconds=2.0, audio=f"anullsrc=cl=stereo:r={RATE}")
    with pytest.raises(AnalysisError, match="Nothing is left to keep"):
        analyze.apply_cut_silence(quiet, tmp_path / "cut.mp4", tmp_path / "keeps.json", threshold_db=-50.0)
    assert not (tmp_path / "cut.mp4").exists()
