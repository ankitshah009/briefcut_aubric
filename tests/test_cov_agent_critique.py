"""Critique of real rendered files and of reports: every finding's code, severity, message and fix."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from vedit.agent.critique import critique_output, critique_report
from vedit.agent.intent import parse_intent
from vedit.agent.model import Intent
from vedit.media import resolve_ffmpeg


def _have_ffmpeg() -> bool:
    try:
        resolve_ffmpeg()
        return True
    except Exception:
        return False


render = pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg required")


def _clip(path: Path, *, size: str = "320x180", seconds: float = 1.0, audio: bool = True) -> Path:
    command = [resolve_ffmpeg(), "-hide_banner", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s={size}:r=30"]
    if audio:
        command += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
    command += ["-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if audio:
        command += ["-c:a", "aac"]
    subprocess.run([*command, str(path)], check=True)
    return path


def _codes(critique) -> dict[str, object]:
    return {finding.code: finding for finding in critique.findings}


def _row(**fields) -> dict:
    row = {
        "path": "/tmp/out.mp4",
        "exists": True,
        "duration": 10.0,
        "width": 1920,
        "height": 1080,
        "has_video": True,
        "has_audio": True,
    }
    row.update(fields)
    return row


def test_a_report_with_no_existing_output_is_a_missing_output() -> None:
    critique = critique_report({"outputs": [_row(exists=False), "junk"]}, Intent())
    assert critique.ok is False
    (finding,) = critique.findings
    assert finding.code == "missing_output" and finding.severity == "fail"
    assert finding.message == "Report lists 1 outputs and 0 of them exist."
    assert "export op" in finding.fix
    assert (
        critique_report("not a report", Intent()).findings[0].message == "Report lists 0 outputs and 0 of them exist."
    )


def test_a_report_prefers_the_output_with_a_picture() -> None:
    report = {"outputs": [_row(path="/x.mp3", has_video=False, width=0, height=0), _row(path="/x.mp4")]}
    critique = critique_report(report, Intent(platform="youtube"))
    assert "no_video" not in _codes(critique) and "frame_size" not in _codes(critique)


def test_an_audio_only_report_row_fails_a_video_contract_but_not_a_podcast_audio_warning() -> None:
    row = _row(has_video=False, width=0, height=0)
    critique = critique_report({"outputs": [row]}, Intent(platform="youtube"))
    finding = _codes(critique)["no_video"]
    assert finding.severity == "fail" and "1920x1080" in finding.message and "0 video streams" in finding.message


def test_missing_duration_and_width_in_a_report_read_as_zero() -> None:
    row = _row()
    del row["duration"], row["width"], row["height"]
    critique = critique_report({"outputs": [row]}, Intent(platform="youtube"))
    codes = _codes(critique)
    assert codes["empty_media"].message == "Duration is missing (0s readable), which is at or under the 0.05s minimum."
    assert "Frame is 0x0" in codes["frame_size"].message
    odd = critique_report(
        {"outputs": [_row(duration="nan", width="1919.6", height="1080.4")]}, Intent(platform="youtube")
    )
    assert "empty_media" in _codes(odd) and "frame_size" not in _codes(odd), "rounded sizes inside 8 px pass"


def test_the_duration_limit_allows_a_third_of_a_second_of_slack() -> None:
    intent = Intent(platform="shorts", max_duration=30)
    inside = critique_report({"outputs": [_row(duration=30.3, width=1080, height=1920)]}, intent)
    assert "duration_over" not in _codes(inside)
    over = critique_report({"outputs": [_row(duration=31.0, width=1080, height=1920)]}, intent)
    finding = _codes(over)["duration_over"]
    assert finding.severity == "fail"
    assert finding.message == "Duration is 31s, over the 30s limit by 1s (slack is 0.35s)."
    assert finding.fix == "Add a trim op ending at 30s, or a cut_silence op, before export."


def test_silent_output_is_a_failure_for_a_podcast_and_a_warning_elsewhere() -> None:
    podcast = critique_report({"outputs": [_row(has_audio=False)]}, Intent(platform="podcast"))
    assert _codes(podcast)["no_audio"].severity == "fail"
    assert "podcast deliverable" in _codes(podcast)["no_audio"].message

    loud = critique_report({"outputs": [_row(has_audio=False)]}, Intent(platform="youtube", loudnorm=True))
    finding = _codes(loud)["no_audio"]
    assert finding.severity == "warn" and "loudnorm was requested for youtube" in finding.message
    assert "before the loudnorm op" in finding.fix

    plain = critique_report({"outputs": [_row(has_audio=False)]}, Intent(platform="youtube"))
    assert _codes(plain)["no_audio"].message == "Audio is missing (0 audio streams) on the youtube deliverable."

    preview = critique_report({"outputs": [_row(has_audio=False, width=960, height=540)]}, Intent(platform="preview"))
    finding = _codes(preview)["no_audio"]
    assert finding.severity == "warn" and "which has no music" in finding.message
    scored = critique_report(
        {"outputs": [_row(has_audio=False, width=960, height=540)]}, Intent(platform="preview", music="bed")
    )
    assert "preview deliverable, which has no music" not in _codes(scored)["no_audio"].message


def test_the_frame_fix_names_the_reframe_aspect_when_the_platform_has_one() -> None:
    shorts = critique_report({"outputs": [_row()]}, Intent(platform="shorts"))
    finding = _codes(shorts)["frame_size"]
    assert finding.message == "Frame is 1920x1080, not the shorts contract of 1080x1920 (tolerance 8 pixels)."
    assert finding.fix == "Add a reframe op with aspect 9:16 before export so the frame is 1080x1920."
    preview = critique_report({"outputs": [_row()]}, Intent(platform="preview"))
    assert _codes(preview)["frame_size"].fix == "Add a fit op before export so the frame is 960x540."


def test_captions_are_a_warning_without_a_work_dir_and_a_failure_with_an_empty_srt(tmp_path: Path) -> None:
    intent = Intent(platform="youtube", captions=True)
    unchecked = critique_report({"outputs": [_row()]}, intent)
    finding = _codes(unchecked)["captions_missing"]
    assert finding.severity == "warn" and "was not checked" in finding.message

    (tmp_path / "captions.srt").write_text("1\n", encoding="utf-8")
    short = critique_report({"work_dir": str(tmp_path), "outputs": [_row()]}, intent)
    finding = _codes(short)["captions_missing"]
    assert finding.severity == "fail" and finding.message == "captions.srt is 2 bytes, under the 20 byte minimum."

    (tmp_path / "captions.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nHello there\n", encoding="utf-8")
    assert "captions_missing" not in _codes(critique_report({"work_dir": str(tmp_path), "outputs": [_row()]}, intent))
    assert "captions_missing" not in _codes(critique_report({"outputs": [_row()]}, Intent(platform="youtube")))


def test_color_primaries_are_read_from_either_report_shape_and_ignored_for_previews() -> None:
    flat = critique_report({"outputs": [_row(color_primaries="bt2020")]}, Intent(platform="youtube"))
    finding = _codes(flat)["color_tags"]
    assert finding.severity == "warn"
    assert finding.message == "Color primaries are bt2020, not bt709, on the 1920x1080 frame."
    nested = critique_report({"outputs": [_row(color={"primaries": "smpte170m"})]}, Intent(platform="youtube"))
    assert "smpte170m" in _codes(nested)["color_tags"].message
    fine = critique_report({"outputs": [_row(color={"primaries": "BT709"})]}, Intent(platform="youtube"))
    assert "color_tags" not in _codes(fine)
    preview = critique_report(
        {"outputs": [_row(width=960, height=540, color_primaries="bt2020")]}, Intent(platform="preview")
    )
    assert "color_tags" not in _codes(preview)
    assert fine.ok is True


def test_a_missing_file_is_reported_with_its_path(tmp_path: Path) -> None:
    gone = tmp_path / "gone.mp4"
    critique = critique_output(gone, Intent())
    (finding,) = critique.findings
    assert finding.code == "missing_output" and finding.message == f"Output {gone} is missing (0 bytes on disk)."


@render
def test_a_file_that_is_not_media_is_reported_as_unprobeable(tmp_path: Path) -> None:
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"this is not a movie")
    critique = critique_output(junk, Intent())
    (finding,) = critique.findings
    assert finding.code == "missing_output"
    assert finding.message.startswith(f"Output {junk} could not be probed (0 streams): ")
    assert finding.message.endswith(".") and not finding.message.endswith("..")


@render
def test_a_real_preview_passes_and_a_shorts_contract_fails_the_same_file(tmp_path: Path) -> None:
    clip = _clip(tmp_path / "clip.mp4", size="960x540")
    preview = critique_output(clip, parse_intent("quick preview"))
    assert preview.ok is True and preview.findings == ()
    shorts = critique_output(clip, parse_intent("a short for reels"), work_dir=tmp_path, enforce_captions=False)
    codes = _codes(shorts)
    assert shorts.ok is False and set(codes) == {"frame_size"}
    assert "960x540" in codes["frame_size"].message and "1080x1920" in codes["frame_size"].message


@render
def test_a_real_silent_podcast_render_fails_on_sound(tmp_path: Path) -> None:
    clip = _clip(tmp_path / "mute.mp4", size="1920x1080", audio=False)
    critique = critique_output(clip, parse_intent("podcast episode, no captions"))
    codes = _codes(critique)
    assert codes["no_audio"].severity == "fail" and critique.ok is False
    assert "frame_size" not in codes


@render
def test_measuring_a_real_file_grades_its_loudness_against_the_platform(tmp_path: Path) -> None:
    clip = _clip(tmp_path / "clip.mp4", size="960x540")
    judged = critique_output(clip, parse_intent("quick preview"), measure=True)
    finding = _codes(judged)["loudness"]
    assert finding.severity == "fail"
    assert "LUFS away from the -16 LUFS target" in finding.message
    assert finding.fix == "Add a loudnorm op targeting -16 LUFS before export."
    unmeasured = critique_output(clip, parse_intent("quick preview"), measure=False)
    assert "loudness" not in _codes(unmeasured)


@render
def test_measuring_a_silent_file_is_not_attempted(tmp_path: Path) -> None:
    clip = _clip(tmp_path / "mute.mp4", size="960x540", audio=False)
    judged = critique_output(clip, Intent(platform="preview"), measure=True)
    assert "loudness" not in _codes(judged), "nothing to measure without a sound track"


@render
def test_a_real_file_whose_measurement_fails_or_is_silent_warns_instead_of_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vedit.analyze as analyze
    from vedit.errors import AnalysisError

    clip = _clip(tmp_path / "clip.mp4", size="960x540")

    def broken(*args, **kwargs):
        raise AnalysisError("loudnorm did not print a JSON measurement block.")

    monkeypatch.setattr(analyze, "measure_loudness", broken)
    finding = _codes(critique_output(clip, Intent(platform="preview"), measure=True))["loudness"]
    assert finding.severity == "warn"
    assert (
        finding.message
        == "Loudness measurement failed with 0 readings (loudnorm did not print a JSON measurement block)."
    )
    assert finding.fix == "Add a loudnorm op targeting -16 LUFS before export."

    monkeypatch.setattr(analyze, "measure_loudness", lambda *args, **kwargs: None)
    finding = _codes(critique_output(clip, Intent(platform="preview"), measure=True))["loudness"]
    assert finding.severity == "warn" and "silent or too short to measure" in finding.message
