"""Ordinary footage, the way people actually have it.

Every clip below is generated with lavfi and pushed through the full local
campaign: plan, render, probe the finished file, read the receipts. Passing
means the file itself decodes at the promised shape, not that a command ran.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pytest

from vedit.campaign import CampaignError, CampaignManager
from vedit.errors import BinaryMissing, BriefError, CaptionError, CompileError, ProbeError, RenderError
from vedit.captions import strip_annotations
from vedit.media import MediaInfo, StreamInfo, export_problem, has_encoder, has_filter, plain_failure, probe, resolve_ffmpeg, resolve_ffprobe, source_notes
from vedit.studio.server import _client_error

pytestmark = pytest.mark.render

SD = "640x360"
H264 = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
AAC = ["-c:a", "aac", "-b:a", "96k"]
CANVAS = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080)}


def ffmpeg(*args: str) -> None:
    subprocess.run([resolve_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def picture(size: str = SD, rate: float = 30, seconds: float = 2.0) -> list[str]:
    return ["-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}:duration={seconds}"]


def tone(rate: int = 48000, seconds: float = 2.0) -> list[str]:
    return ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate={rate}:duration={seconds}"]


def plain(path: Path, *extra: str, size: str = SD, rate: float = 30, seconds: float = 2.0) -> None:
    ffmpeg(*picture(size, rate, seconds), *tone(seconds=seconds), *H264, *AAC, *extra, str(path))


# ---------------------------------------------------------------------------
# Clip builders
# ---------------------------------------------------------------------------


def timecode_track(path: Path) -> None:
    plain(path, "-timecode", "01:00:00:00")


def two_video_streams(path: Path) -> None:
    """A small still proxy first, the real picture second, as some cameras write."""
    ffmpeg("-f", "lavfi", "-i", "color=c=green:s=160x90:r=30:d=2", *picture("1280x720"), *tone(), "-map", "0:v", "-map", "1:v", "-map", "2:a", *H264, *AAC, str(path))


def two_video_streams_and_data(path: Path) -> None:
    source = path.with_name("with-proxy.mov")
    two_video_streams(source)
    ffmpeg("-i", str(source), "-map", "0", "-c", "copy", "-timecode", "01:00:00:00", str(path))


def cover_art_mp4(path: Path) -> None:
    cover = path.with_name("cover.png")
    ffmpeg("-f", "lavfi", "-i", f"color=c=red:s={SD}:d=1", "-frames:v", "1", str(cover))
    source = path.with_name("plain.mp4")
    plain(source)
    ffmpeg("-i", str(source), "-i", str(cover), "-map", "0:v", "-map", "0:a", "-map", "1:v", "-c:v:0", "copy", "-c:a", "copy", "-c:v:1", "mjpeg", "-disposition:v:1", "attached_pic", str(path))


def cover_stream_first_mkv(path: Path) -> None:
    """A large still as the first "video" stream, the footage after it."""
    cover = path.with_name("cover.png")
    ffmpeg("-f", "lavfi", "-i", "color=c=red:s=1920x1080:d=1", "-frames:v", "1", str(cover))
    source = path.with_name("plain.mp4")
    plain(source)
    ffmpeg("-i", str(cover), "-i", str(source), "-map", "0:v", "-map", "1:v", "-map", "1:a", "-c:v:0", "png", "-c:v:1", "copy", "-c:a", "copy", str(path))


def rotated(degrees: int) -> Callable[[Path], None]:
    def build(path: Path) -> None:
        source = path.with_name("sideways.mp4")
        plain(source, size="1280x720")
        ffmpeg("-display_rotation:v:0", str(degrees), "-i", str(source), "-c", "copy", str(path))

    return build


def variable_frame_rate(path: Path) -> None:
    ffmpeg(*picture(rate=60), *tone(), "-vf", "select='lt(mod(n,7),3)'", "-fps_mode", "vfr", *H264, *AAC, str(path))


def mpeg_ts(path: Path) -> None:
    plain(path)


def vp9_opus(path: Path) -> None:
    ffmpeg(*picture(), *tone(), "-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-c:a", "libopus", str(path))


def hevc(path: Path) -> None:
    ffmpeg(*picture(), *tone(), "-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "log-level=error", "-tag:v", "hvc1", *AAC, str(path))


def hevc_10bit_hdr(path: Path) -> None:
    """The shape of an iPhone HDR clip: 10-bit HEVC tagged BT.2020 HLG."""
    ffmpeg(*picture(), *tone(), "-vf", "format=yuv420p10le", "-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "log-level=error", "-tag:v", "hvc1",
           "-color_primaries", "bt2020", "-color_trc", "arib-std-b67", "-colorspace", "bt2020nc", *AAC, str(path))


def av1(path: Path) -> None:
    ffmpeg(*picture(), *tone(), "-c:v", "libsvtav1", "-preset", "12", "-pix_fmt", "yuv420p", *AAC, str(path))


def prores(path: Path) -> None:
    ffmpeg(*picture(), *tone(), "-c:v", "prores_ks", "-profile:v", "0", "-c:a", "pcm_s16le", str(path))


def mjpeg_avi(path: Path) -> None:
    ffmpeg(*picture(), *tone(), "-c:v", "mjpeg", "-pix_fmt", "yuvj420p", "-c:a", "pcm_s16le", str(path))


def odd_dimensions(path: Path) -> None:
    ffmpeg(*picture("853x481"), *tone(), "-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-pix_fmt", "yuv420p", "-c:a", "libopus", str(path))


def odd_dimensions_444(path: Path) -> None:
    ffmpeg(*picture("853x481"), *tone(), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv444p", *AAC, str(path))


def no_audio(path: Path) -> None:
    ffmpeg(*picture(), *H264, str(path))


def digital_silence(path: Path) -> None:
    ffmpeg(*picture(), "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=2", *H264, *AAC, "-shortest", str(path))


def near_silent_noise(path: Path) -> None:
    ffmpeg(*picture(), "-f", "lavfi", "-i", "anoisesrc=d=2:c=white:r=48000:a=0.00001", *H264, *AAC, str(path))


def audio_format(*audio: str, rate: int = 48000) -> Callable[[Path], None]:
    def build(path: Path) -> None:
        ffmpeg(*picture(), *tone(rate), *H264, *AAC, *audio, str(path))

    return build


def clipped_master(path: Path) -> None:
    """A full-scale square wave measures above 0 LUFS, past what loudnorm accepts."""
    ffmpeg(*picture(), "-f", "lavfi", "-i", "aevalsrc='if(lt(mod(t*100,1),0.5),1,-1)|if(lt(mod(t*100,1),0.5),1,-1)':s=48000:d=2", *H264, *AAC, str(path))


def two_audio_tracks(path: Path) -> None:
    ffmpeg(*picture(), "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=2", *tone(), "-map", "0:v", "-map", "2:a", "-map", "1:a", "-shortest", *H264, *AAC, str(path))


def undecodable_audio_track_first(path: Path) -> None:
    """An iPhone's spatial audio track reads like this: present, but with no decoder."""
    source = path.with_name("two-tracks.mp4")
    ffmpeg(*picture(), *tone(), *tone(), "-map", "0:v", "-map", "1:a", "-map", "2:a", *H264, *AAC, str(source))
    path.write_bytes(source.read_bytes().replace(b"mp4a", b"apac", 1))


def subtitle_track(path: Path) -> None:
    subtitles = path.with_name("words.srt")
    subtitles.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
    ffmpeg(*picture(), *tone(), "-i", str(subtitles), "-map", "0:v", "-map", "1:a", "-map", "2:s", *H264, *AAC, "-c:s", "srt", str(path))


def interlaced(path: Path) -> None:
    ffmpeg(*picture("1920x1080", rate=50), *tone(), "-vf", "tinterlace=mode=interleave_top,format=yuv420p", "-c:v", "libx264", "-preset", "ultrafast", "-flags", "+ildct+ilme", *AAC, str(path))


def legacy(video: str, audio: str) -> Callable[[Path], None]:
    def build(path: Path) -> None:
        ffmpeg(*picture(), *tone(rate=44100), "-c:v", video, "-c:a", audio, str(path))

    return build


def surround(path: Path) -> None:
    ffmpeg(*picture(), "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2", "-af", "aformat=channel_layouts=5.1", *H264, *AAC, str(path))


def audio_shorter(path: Path) -> None:
    ffmpeg(*picture(), *tone(seconds=0.6), *H264, *AAC, str(path))


def audio_longer(path: Path) -> None:
    ffmpeg(*picture(seconds=1.0), *tone(seconds=2.5), *H264, *AAC, str(path))


def timestamps_shifted(path: Path) -> None:
    source = path.with_name("plain.mp4")
    plain(source)
    ffmpeg("-i", str(source), "-c", "copy", "-output_ts_offset", "2", str(path))


def stream_offset(late: str) -> Callable[[Path], None]:
    def build(path: Path) -> None:
        source = path.with_name("plain.mp4")
        plain(source)
        first, second = ("0:v", "1:a") if late == "video" else ("0:a", "1:v")
        ffmpeg("-itsoffset", "0.7", "-i", str(source), "-i", str(source), "-map", first, "-map", second, "-c", "copy", str(path))

    return build


def webm_without_duration(path: Path) -> None:
    """A browser's MediaRecorder writes to a stream, so the header never learns the length."""
    data = subprocess.run(
        [resolve_ffmpeg(), "-hide_banner", "-loglevel", "error", *picture(), *tone(), "-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-c:a", "libopus", "-f", "webm", "pipe:1"],
        check=True,
        capture_output=True,
    ).stdout
    path.write_bytes(data)


def fragmented_mp4(path: Path) -> None:
    plain(path, "-movflags", "frag_keyframe+empty_moov")


def anamorphic(path: Path) -> None:
    ffmpeg(*picture("720x576", rate=25), *tone(), "-vf", "setsar=16/11", *H264, *AAC, str(path))


def full_range(path: Path) -> None:
    ffmpeg(*picture(), *tone(), "-vf", "scale=out_range=full,format=yuvj420p", "-c:v", "libx264", "-preset", "ultrafast", "-color_range", "pc", *AAC, str(path))


# ---------------------------------------------------------------------------
# The matrix
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Case:
    id: str
    build: Callable[[Path], None]
    suffix: str = ".mp4"
    aspect: str = "9:16"
    audio: bool = True
    seconds: float = 2.0
    encoder: str | None = None


CASES = [
    Case("baseline", plain),
    Case("timecode_data_track", timecode_track),
    Case("two_video_streams", two_video_streams),
    Case("two_video_streams_and_data", two_video_streams_and_data, suffix=".mov"),
    Case("cover_art_mp4", cover_art_mp4),
    Case("cover_stream_first_mkv", cover_stream_first_mkv, suffix=".mkv"),
    Case("rotated_90", rotated(90)),
    Case("rotated_180", rotated(180)),
    Case("rotated_270", rotated(270)),
    Case("portrait_to_9x16", lambda p: plain(p, size="1080x1920")),
    Case("portrait_to_16x9", lambda p: plain(p, size="1080x1920"), aspect="16:9"),
    Case("portrait_to_1x1", lambda p: plain(p, size="1080x1920"), aspect="1:1"),
    Case("square_to_9x16", lambda p: plain(p, size="720x720")),
    Case("fps_60", lambda p: plain(p, rate=60)),
    Case("fps_23_976", lambda p: plain(p, rate=24000 / 1001)),
    Case("fps_5_screen_capture", lambda p: plain(p, rate=5)),
    Case("variable_frame_rate", variable_frame_rate),
    Case("uhd_4k", lambda p: plain(p, size="3840x2160", seconds=1.0), seconds=1.0),
    Case("uhd_4k_to_16x9", lambda p: plain(p, size="3840x2160", seconds=1.0), aspect="16:9", seconds=1.0),
    Case("mov", plain, suffix=".mov"),
    Case("mkv", plain, suffix=".mkv"),
    Case("webm_vp9", vp9_opus, suffix=".webm", encoder="libvpx-vp9"),
    Case("mpeg_ts", mpeg_ts, suffix=".ts"),
    Case("avi_mjpeg_pcm", mjpeg_avi, suffix=".avi"),
    Case("hevc", hevc, encoder="libx265"),
    Case("hevc_10bit_hdr", hevc_10bit_hdr, suffix=".mov", encoder="libx265"),
    Case("av1", av1, suffix=".mkv", encoder="libsvtav1"),
    Case("prores", prores, suffix=".mov", encoder="prores_ks"),
    Case("odd_dimensions_vp9", odd_dimensions, suffix=".webm", encoder="libvpx-vp9"),
    Case("odd_dimensions_444", odd_dimensions_444),
    Case("very_short_0_4s", lambda p: plain(p, seconds=0.4), seconds=0.4),
    Case("very_short_0_15s", lambda p: plain(p, seconds=0.15), seconds=0.15),
    Case("no_audio", no_audio, audio=False),
    Case("digital_silence", digital_silence),
    Case("near_silent_noise", near_silent_noise),
    Case("mono", audio_format("-ac", "1")),
    Case("surround_5_1", surround),
    Case("audio_48k", audio_format("-ar", "48000")),
    Case("audio_44k1", audio_format("-ar", "44100"), suffix=".mov"),
    Case("audio_8k", audio_format("-ar", "8000", rate=8000)),
    Case("audio_shorter_than_video", audio_shorter),
    Case("audio_longer_than_video", audio_longer, seconds=1.0),
    Case("audio_longer_than_video_mkv", audio_longer, suffix=".mkv", seconds=1.0),
    Case("nonzero_start_mp4", timestamps_shifted),
    Case("nonzero_start_mkv", timestamps_shifted, suffix=".mkv"),
    Case("video_starts_after_audio", stream_offset("video"), seconds=2.7),
    Case("audio_starts_after_video", stream_offset("audio"), seconds=2.0),
    Case("webm_without_duration", webm_without_duration, suffix=".webm", encoder="libvpx-vp9"),
    Case("fragmented_mp4", fragmented_mp4),
    Case("anamorphic_pal", anamorphic),
    Case("full_range", full_range),
    Case("clipped_master", clipped_master),
    Case("two_audio_tracks", two_audio_tracks),
    Case("undecodable_audio_track_first", undecodable_audio_track_first),
    Case("subtitle_track", subtitle_track, suffix=".mkv"),
    Case("interlaced_1080i", interlaced, suffix=".mkv"),
    Case("m4v", plain, suffix=".m4v"),
    Case("flv", plain, suffix=".flv"),
    Case("wmv", legacy("wmv2", "wmav2"), suffix=".wmv"),
    Case("mpeg2", legacy("mpeg2video", "mp2"), suffix=".mpg"),
    Case("ogg_theora", legacy("libtheora", "libvorbis"), suffix=".ogv", encoder="libtheora"),
    Case("tiny_32x32", lambda p: plain(p, size="32x32")),
    Case("extreme_shape_160x1600", lambda p: plain(p, size="160x1600")),
    Case("awkward_name", plain, suffix=" it's \"my\" [best] clip: été 日本語 100%.mp4"),
]


@pytest.fixture
def manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    instance = CampaignManager(tmp_path, start_worker=False)
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def listening(tmp_path: Path):
    """A manager whose transcriber check is real, for use with ``hears``."""
    instance = CampaignManager(tmp_path, start_worker=False)
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def hears(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[str], Path]:
    """A stand-in Meta Voice Transcribe that hears one fixed turn and logs each call, on a Meta-mode project."""
    from vedit import meta

    log = tmp_path / "transcribe.log"
    heard = {"text": ""}
    monkeypatch.setenv("MODEL_API_KEY", "test-secret-never-write-to-disk")
    monkeypatch.setenv("VEDIT_META_ENABLED", "1")

    def transcribe_audio(wav: Path, **kwargs: Any) -> dict[str, Any]:
        with log.open("a", encoding="utf-8") as handle:
            handle.write("called\n")
        return {"cues": [{"start": 0.0, "end": 1.0, "text": heard["text"]}], "provider": "meta", "usage": {"cost_usd": 0.0}}

    def plan_story(proxy: Path, **kwargs: Any) -> dict[str, Any]:
        return {"title": "A cut", "summary": "s", "caption": "", "cover_prompt": "", "hashtags": [], "provider": "meta", "usage": {"cost_usd": 0.0}, "warnings": [],
                "segments": [{"start": 0.0, "end": min(float(kwargs["duration"]), 2.0), "label": "Opening", "reason": "Chosen."}]}

    monkeypatch.setattr(meta, "transcribe_audio", transcribe_audio)
    monkeypatch.setattr(meta, "plan_story", plan_story)

    def hear(text: str) -> Path:
        heard["text"] = text
        return log

    return hear


@pytest.fixture
def inbox(tmp_path: Path) -> Path:
    folder = tmp_path / "vedit-sessions" / "source-test" / "inbox"
    folder.mkdir(parents=True)
    return folder


def plan_campaign(manager: CampaignManager, clip: Path, **request: Any) -> dict[str, Any]:
    job = manager.create({"paths": [str(clip)], "goal": "Show the footage clearly", "target_duration": 30, "captions": False, **request})
    manager.process(job["id"])
    return manager.get(job["id"])


def render_campaign(manager: CampaignManager, clip: Path, *, plan: dict[str, Any] | None = None, **request: Any) -> dict[str, Any]:
    planned = plan_campaign(manager, clip, **request)
    assert planned["status"] == "planned", planned["error"]
    manager.render(planned["id"], {"plan": plan} if plan else {})
    manager.process(planned["id"])
    return manager.get(planned["id"])


def frame(video: Path, at: float, *, size: str = "18x32", pixels: str = "rgb24") -> bytes:
    """One decoded frame, shrunk, so a test can look at the picture and not the header."""
    return subprocess.run(
        [resolve_ffmpeg(), "-v", "error", "-ss", f"{at:.3f}", "-i", str(video), "-frames:v", "1", "-vf", f"scale={size}", "-f", "rawvideo", "-pix_fmt", pixels, "-"],
        check=True,
        capture_output=True,
    ).stdout


def difference(first: bytes, second: bytes) -> float:
    assert first and len(first) == len(second)
    return sum(abs(a - b) for a, b in zip(first, second)) / len(first)


def render_campaign_again(manager: CampaignManager, planned: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    manager.render(planned["id"], {"plan": plan})
    manager.process(planned["id"])
    return manager.get(planned["id"])


def decoded_frames(video: Path) -> int:
    """Count frames by decoding them: a header can promise what the file lacks."""
    result = subprocess.run(
        [resolve_ffprobe(), "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(video)],
        check=True,
        capture_output=True,
        text=True,
    )
    return int(result.stdout.strip().split(",")[0])


def assert_delivered(manager: CampaignManager, done: dict[str, Any], *, aspect: str, audio: bool, moves: bool = True) -> Path:
    assert done["status"] == "complete", done["error"]
    video = manager.artifact(done["id"], "video")
    info = probe(video)
    width, height = CANVAS[aspect]
    assert (info.width, info.height) == (width, height)
    assert info.has_video
    assert info.has_audio is audio
    approved = done["plan"]["duration"]
    assert abs(info.duration - approved) <= 0.35, f"{info.duration} s delivered, {approved} s approved"
    assert decoded_frames(video) >= max(1, int(approved * 30 * 0.8))
    if moves and approved >= 0.8:
        # A still cover or proxy passes every header check; only the picture tells.
        assert difference(frame(video, 0.1), frame(video, approved - 0.2)) > 0.5, "the picture never changes"
    proof = done["result"]["proof"]
    assert (proof["color_space"], proof["color_range"]) == ("bt709", "tv")
    report = json.loads(manager.artifact(done["id"], "report").read_text(encoding="utf-8"))
    assert report["proof"] == proof
    assert all(step["ok"] for step in report["steps"]), report["steps"]
    output = report["outputs"][0]
    assert output["exists"] and (output["width"], output["height"]) == (width, height)
    assert output["has_audio"] is audio
    failed = [check for check in done["result"]["checks"] if not check["passed"]]
    assert not failed, failed
    return video


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.id)
def test_campaign_delivers(manager: CampaignManager, inbox: Path, case: Case) -> None:
    if case.encoder and not has_encoder(case.encoder):
        pytest.skip(f"{case.encoder} is not in this FFmpeg build")
    clip = inbox / f"clip{case.suffix}"
    case.build(clip)
    done = render_campaign(manager, clip, aspect=case.aspect)
    assert_delivered(manager, done, aspect=case.aspect, audio=case.audio)
    assert abs(done["source"]["duration"] - case.seconds) <= 0.2, done["source"]



@pytest.mark.parametrize("degrees", [90, 180, 270])
def test_rotated_phone_clip_stays_upright(manager: CampaignManager, inbox: Path, degrees: int) -> None:
    clip = inbox / "phone.mp4"
    rotated(degrees)(clip)
    aspect = "9:16" if degrees != 180 else "16:9"
    video = assert_delivered(manager, render_campaign(manager, clip, aspect=aspect), aspect=aspect, audio=True)
    # FFmpeg's own upright reading of the source is the reference.
    assert difference(frame(video, 1.0), frame(clip, 1.0)) < 12


def test_anamorphic_pixels_come_out_square(manager: CampaignManager, inbox: Path) -> None:
    """PAL 16:9 stores wide pixels: a 110x160 box in the file is a square on screen."""
    clip = inbox / "pal.mp4"
    ffmpeg("-f", "lavfi", "-i", "color=c=black:s=720x576:r=25:d=1.5", *tone(seconds=1.5), "-vf", "drawbox=x=305:y=208:w=110:h=160:color=white:t=fill,setsar=16/11", *H264, *AAC, str(clip))
    video = assert_delivered(manager, render_campaign(manager, clip, aspect="16:9"), aspect="16:9", audio=True, moves=False)
    gray = frame(video, 0.5, size="192x108", pixels="gray")
    rows = [gray[index : index + 192] for index in range(0, len(gray), 192)]
    wide = max(sum(1 for value in row if value > 128) for row in rows)
    tall = sum(1 for row in rows if max(row) > 128)
    assert abs(wide / tall - 1) < 0.12, f"{wide}x{tall}"


def test_late_sound_stays_in_sync(manager: CampaignManager, inbox: Path) -> None:
    clip = inbox / "late.mp4"
    stream_offset("audio")(clip)
    done = render_campaign(manager, clip)
    video = assert_delivered(manager, done, aspect="9:16", audio=True)
    # The file's first 0.7 s has no sound, so the finished video must not either.
    log = subprocess.run([resolve_ffmpeg(), "-hide_banner", "-i", str(video), "-af", "silencedetect=noise=-40dB:d=0.3", "-vn", "-f", "null", "-"], capture_output=True, text=True).stderr
    assert "silence_start: 0" in log, log[-400:]
    assert "silence_duration: 0.6" in log or "silence_duration: 0.7" in log, log[-400:]



def test_interlaced_camcorder_footage_comes_out_progressive(manager: CampaignManager, inbox: Path) -> None:
    clip = inbox / "camcorder.mkv"
    interlaced(clip)
    video = assert_delivered(manager, render_campaign(manager, clip, aspect="16:9"), aspect="16:9", audio=True)
    log = subprocess.run([resolve_ffmpeg(), "-hide_banner", "-nostats", "-i", str(video), "-vf", "idet", "-an", "-f", "null", "-"], capture_output=True, text=True).stderr
    combed, progressive = re.findall(r"Multi frame detection: TFF:\s*(\d+) BFF:\s*\d+ Progressive:\s*(\d+)", log)[-1]
    assert int(progressive) > int(combed), log[-300:]


def test_late_sound_stays_in_sync_inside_a_moment(manager: CampaignManager, inbox: Path) -> None:
    clip = inbox / "late.mp4"
    stream_offset("audio")(clip)
    planned = plan_campaign(manager, clip, target_duration=1.3)
    moment = {"start": 0.5, "end": 1.8, "label": "Across the start of the sound", "reason": "Starts before the sound does."}
    done = render_campaign_again(manager, planned, {**planned["plan"], "segments": [moment]})
    video = assert_delivered(manager, done, aspect="9:16", audio=True)
    log = subprocess.run([resolve_ffmpeg(), "-hide_banner", "-i", str(video), "-af", "silencedetect=noise=-40dB:d=0.1", "-vn", "-f", "null", "-"], capture_output=True, text=True).stderr
    assert "silence_end: 0.1" in log or "silence_end: 0.2" in log, log[-400:]


# ---------------------------------------------------------------------------
# Color: the export says BT.709 limited, so the pixels must be
# ---------------------------------------------------------------------------

PATCH = "0xC68E73"  # skin, the color people notice first
PATCH_RGB = (198, 142, 115)


def patch(*convert: str) -> list[str]:
    return ["-f", "lavfi", "-i", f"color=c={PATCH}:s=1280x720:r=30:d=1.5", *tone(seconds=1.5), *AAC, *convert]


COLOR = {
    "bt709": patch("-vf", "scale=out_color_matrix=bt709,format=yuv420p", *H264, "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"),
    "full_range": patch("-vf", "scale=out_range=full:out_color_matrix=bt709,format=yuv420p,setparams=range=pc:colorspace=bt709", "-c:v", "libx264", "-preset", "ultrafast", "-color_range", "pc", "-colorspace", "bt709"),
    "bt601": patch("-vf", "scale=out_color_matrix=bt601,format=yuv420p,setparams=colorspace=smpte170m", *H264, "-colorspace", "smpte170m", "-color_primaries", "smpte170m", "-color_trc", "smpte170m"),
    "rgb_screen_capture": patch("-c:v", "libx264rgb", "-preset", "ultrafast", "-qp", "0"),
}
HDR = {
    "hlg": "arib-std-b67",
    "pq": "smpte2084",
}


def average_color(video: Path, at: float = 0.7) -> tuple[int, int, int]:
    data = frame(video, at, size="1x1")
    return data[0], data[1], data[2]


def assert_color(manager: CampaignManager, clip: Path) -> None:
    video = assert_delivered(manager, render_campaign(manager, clip, aspect="16:9"), aspect="16:9", audio=True, moves=False)
    color = average_color(video)
    assert all(abs(got - want) <= 12 for got, want in zip(color, PATCH_RGB)), f"{color} delivered for {PATCH_RGB}"


@pytest.mark.parametrize("name", COLOR)
def test_color_survives(manager: CampaignManager, inbox: Path, name: str) -> None:
    clip = inbox / "clip.mkv"
    ffmpeg(*COLOR[name], str(clip))
    assert_color(manager, clip)


@pytest.mark.parametrize("name", HDR)
def test_hdr_is_tone_mapped(manager: CampaignManager, inbox: Path, name: str) -> None:
    if not (has_encoder("libx265") and has_filter("zscale") and has_filter("tonemap")):
        pytest.skip("HDR needs libx265, zscale, and tonemap in this FFmpeg build")
    clip = inbox / "phone.mov"
    transfer = HDR[name]
    ffmpeg("-f", "lavfi", "-i", f"color=c={PATCH}:s=1280x720:r=30:d=1.5", *tone(seconds=1.5), *AAC,
           # BT.2408: SDR white sits at HDR reference white, 203 nits, as phones and graders place it.
           "-vf", f"format=yuv444p,zscale=tin=bt709:pin=bt709:min=bt709:rin=tv:t={transfer}:m=bt2020nc:p=bt2020:r=tv:npl=203,format=yuv420p10le",
           "-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "log-level=error", "-tag:v", "hvc1",
           "-color_primaries", "bt2020", "-color_trc", transfer, "-colorspace", "bt2020nc", str(clip))
    assert_color(manager, clip)


# ---------------------------------------------------------------------------
# Captions say only what was said
# ---------------------------------------------------------------------------


def burned(manager: CampaignManager, done: dict[str, Any]) -> str:
    report = manager.artifact(done["id"], "report")
    srt = report.with_name("captions.srt")
    return srt.read_text(encoding="utf-8") if srt.is_file() else ""


def check(done: dict[str, Any], name: str) -> dict[str, Any]:
    return next(item for item in done["result"]["checks"] if item["name"] == name)


def test_silent_clip_gets_no_invented_caption(listening: CampaignManager, inbox: Path, hears: Callable[[str], Path]) -> None:
    """Recognizers write "You" on digital silence. Two real clips shipped with exactly that, so silence is never sent."""
    log = hears("You")
    clip = inbox / "quiet.mp4"
    digital_silence(clip)
    done = render_campaign(listening, clip, captions=True, provider="meta")
    assert_delivered(listening, done, aspect="9:16", audio=True)
    assert "You" not in burned(listening, done)
    assert done["plan"]["analysis"]["transcript"] == []
    assert not log.exists(), "there was nothing to transcribe"


def test_music_is_not_a_caption(listening: CampaignManager, inbox: Path, hears: Callable[[str], Path]) -> None:
    hears("(upbeat music)")
    clip = inbox / "music.mp4"
    plain(clip)
    done = render_campaign(listening, clip, captions=True, provider="meta")
    assert_delivered(listening, done, aspect="9:16", audio=True)
    assert "music" not in burned(listening, done).lower()
    assert "music" not in json.dumps(done["plan"]["segments"]).lower()
    assert done["plan"]["analysis"]["speech"] == [], "a music bed is not speech"


def test_voice_over_a_music_bed_is_graded_on_the_voice(listening: CampaignManager, inbox: Path, hears: Callable[[str], Path]) -> None:
    """The real AdFlow failure: loud for 74 s, a voice for 6, so 11 percent looked like missing captions."""
    hears("Proven to reduce anxiety.")
    clip = inbox / "advert.mp4"
    plain(clip)
    done = render_campaign(listening, clip, captions=True, provider="meta")
    assert_delivered(listening, done, aspect="9:16", audio=True)
    assert "Proven to reduce anxiety." in burned(listening, done)
    verdict = check(done, "auto: captions cover the speech")
    assert verdict["passed"], verdict
    assert done["plan"]["analysis"]["speech"][0]["end"] <= 1.1, "speech is what was heard, not how loud the room was"


def test_long_clip_without_a_transcript_is_not_graded_on_energy(tmp_path: Path) -> None:
    """Planning skips the transcript on long clips, so its speech bands are only loudness."""
    (tmp_path / "captions.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nHello there.\n", encoding="utf-8")
    plan = {"segments": [{"start": 0.0, "end": 20.0}], "analysis": {"speech": [{"start": 0.0, "end": 20.0}], "transcript": []}}
    graded = CampaignManager._grade(plan, {"captions": True, "target_duration": 20}, 400.0, tmp_path, None)
    # With no transcript there is nothing to compare the captions with, so there is no caption check, not a pass.
    assert "auto: captions cover the speech" not in [item["name"] for item in graded["checks"]]


# ---------------------------------------------------------------------------
# A moment outside what the file holds
# ---------------------------------------------------------------------------


def test_moment_after_the_sound_ends_still_has_a_sound_track(manager: CampaignManager, inbox: Path) -> None:
    clip = inbox / "short-sound.mp4"
    audio_shorter(clip)
    planned = plan_campaign(manager, clip, target_duration=1)
    moment = {"start": 1.2, "end": 1.9, "label": "Late", "reason": "After the sound."}
    done = render_campaign_again(manager, planned, {**planned["plan"], "segments": [moment]})
    assert_delivered(manager, done, aspect="9:16", audio=True)


def test_clip_shorter_than_the_target_passes_its_own_length(manager: CampaignManager, inbox: Path) -> None:
    clip = inbox / "short.mp4"
    plain(clip)
    done = render_campaign(manager, clip, target_duration=30)
    assert_delivered(manager, done, aspect="9:16", audio=True)
    assert check(done, "auto: length fits the target (\u2264 " + f"{done['plan']['source_duration']:.1f}".removesuffix(".0") + " s)")["passed"]


def test_frame_counted_length_is_not_rounded_past_the_clip(manager: CampaignManager, inbox: Path) -> None:
    """A third of 30 fps clips end in .xx6667, which rounds up past the end of the file."""
    clip = inbox / "frames.mp4"
    plain(clip, seconds=1 + 2 / 30)
    assert_delivered(manager, render_campaign(manager, clip), aspect="9:16", audio=True)


# ---------------------------------------------------------------------------
# Files that cannot work say so, plainly
# ---------------------------------------------------------------------------


def text_file(path: Path) -> None:
    path.write_text("This is not a video.\n", encoding="utf-8")


def recording_cut_off(path: Path) -> None:
    source = path.with_name("whole.mp4")
    plain(source, seconds=4.0)
    data = source.read_bytes()
    path.write_bytes(data[: len(data) // 2])


def upload_cut_off(path: Path) -> None:
    source = path.with_name("whole.mp4")
    plain(source, "-movflags", "+faststart", seconds=4.0)
    data = source.read_bytes()
    path.write_bytes(data[: len(data) // 2])


def sound_only(path: Path) -> None:
    ffmpeg(*tone(), *AAC, str(path))


def song_with_cover(path: Path) -> None:
    cover = path.with_name("cover.png")
    ffmpeg("-f", "lavfi", "-i", "color=c=red:s=300x300:d=1", "-frames:v", "1", str(cover))
    ffmpeg(*tone(), "-i", str(cover), "-map", "0:a", "-map", "1:v", "-c:a", "libmp3lame", "-c:v", "mjpeg", "-disposition:v", "attached_pic", str(path))


def unknown_codec(path: Path) -> None:
    source = path.with_name("whole.mp4")
    plain(source)
    path.write_bytes(source.read_bytes().replace(b"avc1", b"zzzz"))


def two_frames(path: Path) -> None:
    ffmpeg(*picture(seconds=2 / 30), *H264, str(path))


def still_with_long_sound(path: Path) -> None:
    ffmpeg(*picture(seconds=1 / 30), *tone(seconds=5), *H264, *AAC, str(path))


BROKEN = [
    ("text_file", text_file, ".mp4", "not a video"),
    ("recording_cut_off", recording_cut_off, ".mp4", "incomplete"),
    ("upload_cut_off", upload_cut_off, ".mp4", "cut short"),
    ("sound_only", sound_only, ".m4a", "sound but no picture"),
    ("song_with_cover", song_with_cover, ".mp3", "sound but no picture"),
    ("unknown_codec", unknown_codec, ".mp4", "cannot decode"),
    ("two_frames", two_frames, ".mp4", "only 0.07 seconds"),
    ("still_with_long_sound", still_with_long_sound, ".mp4", "stops after"),
]


def assert_plain(message: str, root: Path) -> None:
    assert "could not be processed" not in message
    assert str(root) not in message and "/" not in message, message
    assert "0x" not in message and "ffprobe" not in message and "filter_complex" not in message, message
    assert message.endswith((".", "!")) and len(message) < 300, message


@pytest.mark.parametrize("name,build,suffix,phrase", BROKEN, ids=[row[0] for row in BROKEN])
def test_broken_file_says_what_is_wrong(manager: CampaignManager, inbox: Path, tmp_path: Path, name: str, build: Callable[[Path], None], suffix: str, phrase: str) -> None:
    clip = inbox / f"clip{suffix}"
    build(clip)
    failed = plan_campaign(manager, clip)
    assert failed["status"] == "failed", failed["plan"]
    assert phrase in failed["error"], failed["error"]
    assert_plain(failed["error"], tmp_path)


def test_one_frame_file_becomes_a_held_picture(manager: CampaignManager, inbox: Path) -> None:
    clip = inbox / "frame.mp4"
    ffmpeg(*picture(seconds=1 / 30), *H264, str(clip))
    done = render_campaign(manager, clip)
    assert done["status"] == "complete", done["error"]
    assert done["source"]["duration"] >= 1


def test_empty_file_is_refused_with_a_reason(manager: CampaignManager, inbox: Path) -> None:
    clip = inbox / "empty.mp4"
    clip.write_bytes(b"")
    with pytest.raises(CampaignError, match="empty"):
        manager.create({"paths": [str(clip)], "goal": "Show it", "target_duration": 5})


def test_clip_over_the_limit_says_how_long_it_is(manager: CampaignManager, inbox: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vedit.campaign._MAX_SOURCE_SECONDS", 1)
    clip = inbox / "long.mp4"
    plain(clip)
    failed = plan_campaign(manager, clip)
    assert "trim it first" in failed["error"]
    assert_plain(failed["error"], tmp_path)


def test_source_removed_after_planning_says_upload_again(manager: CampaignManager, inbox: Path, tmp_path: Path) -> None:
    clip = inbox / "gone.mp4"
    plain(clip)
    planned = plan_campaign(manager, clip)
    manager.render(planned["id"], {})
    clip.unlink()
    manager.process(planned["id"])
    failed = manager.get(planned["id"])
    assert failed["status"] == "failed"
    assert "pload" in failed["error"], failed["error"]
    assert_plain(failed["error"], tmp_path)


@pytest.mark.parametrize(
    "error,phrase",
    [
        (RenderError("x", step="select_ranges", detail="write failed: No space left on device"), "full"),
        (RenderError("x", step="export", detail="av_interleaved_write_frame(): Permission denied"), "permissions"),
        (RenderError("x", step="export", detail="Cannot allocate memory"), "memory"),
        (RenderError("x", step="select_ranges", detail="[vist#0:0/none @ 0x75] Decoding requested, but no decoder found for: none"), "cannot decode"),
        (RenderError("x", step="select_ranges", detail="[h264 @ 0x79] Invalid NAL unit size (7708 > 3047)."), "damaged"),
        (RenderError("x", step="export", detail="Unknown encoder 'libx264'"), "ffmpeg-full"),
        (RenderError("x", step="select_ranges", detail="[out#0/mp4 @ 0x78] Nothing was written into output file"), "no picture or sound"),
        (RenderError("timed out after 3600s: /opt/ffmpeg -i /Users/me/clip.mp4"), "too long"),
        (RenderError("step failed", step="export_shorts_9x16", detail="Error opening output files: Result too large"), "The export stopped: Result too large."),
        (RenderError("step failed", step="thumbnail", detail="/Users/me/secret/clip.mp4: unknown failure at 0x7f"), "Making the cover frame stopped."),
        (ProbeError("ffprobe failed for /Users/me/clip.mp4: bad"), "could not be read"),
        (CaptionError("This ffmpeg build has no subtitles/ass filter.\n/Users/me/ffmpeg"), "turn captions off"),
        (BinaryMissing("executable not found: /opt/homebrew/bin/ffmpeg"), "vedit doctor"),
        (CompileError("missing source file(s): footage=/Users/me/My Videos/clip.mp4"), "Missing source file(s): footage."),
        (OSError(28, "No space left on device"), "full"),
        (ValueError("boom at /Users/me/secret"), "could not finish"),
    ],
)
def test_failures_are_specific_and_hold_no_path(error: Exception, phrase: str) -> None:
    message = plain_failure(error)
    assert phrase in message, message
    assert "/Users" not in message and "0x" not in message and "could not be processed" not in message, message


@pytest.mark.parametrize(
    "text,kept",
    [
        ("(upbeat music)", ""),
        ("[MUSIC PLAYING]", ""),
        ("Clinically proven to reduce. (upbeat", "Clinically proven to reduce."),
        ("music) And then we start.", "And then we start."),
        ("[BLANK_", ""),
        ("Reduced by half (in two weeks) for most people.", "Reduced by half for most people."),
        ("Everything is fine.", "Everything is fine."),
    ],
)
def test_sound_tags_never_reach_a_caption(text: str, kept: str) -> None:
    assert strip_annotations(text) == kept


def test_studio_routes_never_echo_a_command_or_a_path() -> None:
    leaked = RenderError("ffmpeg failed (1):\n  ffmpeg -i /Users/me/clip.mp4 out.mp4", step="export_shorts_9x16", detail="Conversion failed!")
    message = _client_error(leaked)
    assert "/Users" not in message and "ffmpeg -i" not in message and message.startswith("The export stopped.")
    assert _client_error(BriefError("goal must be text")) == "goal must be text"


def stream(index: int, kind: str, codec: str = "h264", **fields: Any) -> StreamInfo:
    return StreamInfo(index, kind, codec, **fields)


@pytest.mark.parametrize(
    "streams,duration,seconds,phrase",
    [
        ((stream(0, "video", width=320, height=180), stream(1, "video", width=1280, height=720)), 2.0, 2.0, "uses the 1280x720 one"),
        ((stream(0, "video", width=640, height=360), stream(1, "audio", "aac", default=True), stream(2, "audio", "aac")), 2.0, 2.0, "uses track 1"),
        ((stream(0, "video", width=640, height=360), stream(1, "audio", "unknown")), 2.0, 2.0, "will be silent"),
        ((stream(0, "video", width=640, height=360), stream(1, "audio", "aac")), 1.0, 2.5, "sound after the last frame"),
    ],
)
def test_plan_says_what_it_chose(streams: tuple[StreamInfo, ...], duration: float, seconds: float, phrase: str) -> None:
    info = MediaInfo(Path("clip.mp4"), duration, "mov", 1, None, streams, seconds)
    assert any(phrase in note for note in source_notes(info))


def test_clear_files_need_no_note() -> None:
    assert source_notes(MediaInfo(Path("clip.mp4"), 2.0, "mov", 1, None, (stream(0, "video", width=640, height=360), stream(1, "audio", "aac")), 2.03)) == []


def test_goal_naming_another_frame_shape_is_flagged(manager: CampaignManager, inbox: Path) -> None:
    clip = inbox / "shape.mp4"
    plain(clip)
    planned = plan_campaign(manager, clip, goal="Make a square clip of the footage", aspect="9:16")
    assert any("asks for 1:1" in warning for warning in planned["plan"]["warnings"])
    matching = plan_campaign(manager, clip, goal="Make a square clip of the footage", aspect="1:1")
    assert not any("asks for" in warning for warning in matching["plan"]["warnings"])


@pytest.mark.parametrize(
    "fields,phrase",
    [
        ({"streams": (stream(0, "audio", "aac"),)}, "no picture"),
        ({"width": 720}, "720x1920"),
        ({"audio": False}, "lost its sound"),
        ({"duration": 1.0}, "runs 1.0 seconds, not the 3.0"),
        ({"space": "bt470bg"}, "color"),
    ],
)
def test_undeliverable_export_says_exactly_why(fields: dict[str, Any], phrase: str) -> None:
    streams = fields.get("streams") or (
        stream(0, "video", width=fields.get("width", 1080), height=1920, color_space=fields.get("space", "bt709"), color_range="tv"),
        *(() if fields.get("audio") is False else (stream(1, "audio", "aac"),)),
    )
    output = MediaInfo(Path("video.mp4"), fields.get("duration", 3.0), "mov", 1, None, streams)
    message = export_problem(output, width=1080, height=1920, has_audio=True, seconds=3.0, tolerance=0.3)
    assert message and phrase in message, message
    assert "Retry the saved plan" not in message


def test_good_export_has_no_problem() -> None:
    streams = (stream(0, "video", width=1080, height=1920, color_space="bt709", color_range="tv"), stream(1, "audio", "aac"))
    assert export_problem(MediaInfo(Path("video.mp4"), 3.0, "mov", 1, None, streams), width=1080, height=1920, has_audio=True, seconds=3.0, tolerance=0.3) is None
