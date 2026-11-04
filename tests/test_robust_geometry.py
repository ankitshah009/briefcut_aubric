"""Any shape, rate, codec and container, through the real campaign with Meta stubbed.

Each clip is generated with lavfi, uploaded, planned by a stand-in Muse Spark
that picks moments inside the clip, rendered, and proven by probing the file:
size, streams, duration, colour, and the receipts beside it. Shape tests also
look at the picture: a centered square must come out square in every export.

The unit tests below the matrix guard the fixes this sweep found.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pytest

from vedit import project_media
from vedit.campaign import CampaignManager
from vedit.errors import CompileError
from vedit.media import (STANDARD_RATES, MediaInfo, StreamInfo, detect_borders, ffmpeg_major, has_encoder, has_filter, probe, rate_text, resolve_ffmpeg,
                         resolve_ffprobe, source_filters, standard_rate, tone_map)
from vedit.presets import export_preset_name

pytestmark = pytest.mark.render

CANVAS = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080)}
H264 = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
H264_444 = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv444p"]
X265 = ["-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "log-level=error", "-tag:v", "hvc1"]
AAC = ["-c:a", "aac", "-b:a", "64k"]


def ffmpeg(*args: str) -> None:
    subprocess.run([resolve_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def tone(seconds: float) -> list[str]:
    return ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}"]


def moving(size: str, rate: str | float = 30, seconds: float = 2.0) -> list[str]:
    # testsrc2 rounds odd sizes down to even, so the size is set by a scale instead.
    width, height = size.split("x")
    return ["-f", "lavfi", "-i", f"testsrc2=size=640x360:rate={rate}:duration={seconds},scale={width}:{height}"]


def square(size: str, *, sar: tuple[int, int] = (1, 1), rate: str | float = 30, seconds: float = 1.5, video: list[str] = H264) -> Callable[[Path], None]:
    """Black frame, white square in the middle: square on screen, whatever the stored pixels."""
    def build(path: Path) -> None:
        width, height = (int(value) for value in size.split("x"))
        side = round(0.3 * min(width * sar[0] / sar[1], height))
        stored = round(side * sar[1] / sar[0])
        box = f"drawbox=x={(width - stored) // 2}:y={(height - side) // 2}:w={stored}:h={side}:color=white:t=fill,setsar={sar[0]}/{sar[1]}"
        ffmpeg("-f", "lavfi", "-i", f"color=c=black:s={size}:r={rate}:d={seconds}", *tone(seconds), "-vf", box, *video, *AAC, str(path))
    return build


def turned(build: Callable[[Path], None], degrees: int = 90) -> Callable[[Path], None]:
    """A phone's portrait clip: stored on its side, with a display rotation flag."""
    def wrap(path: Path) -> None:
        flat = path.with_name("flat" + path.suffix)
        build(flat)
        ffmpeg("-display_rotation:v:0", str(degrees), "-i", str(flat), "-c", "copy", str(path))
    return wrap


# ---------------------------------------------------------------------------
# The campaign, with Meta stubbed
# ---------------------------------------------------------------------------


@pytest.fixture
def meta_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Muse Spark picks two moments inside the clip; Voice Transcribe hears one line. Nothing leaves the machine."""
    from vedit import meta

    monkeypatch.setenv("MODEL_API_KEY", "test-secret-never-write-to-disk")
    monkeypatch.setenv("VEDIT_META_ENABLED", "1")

    def plan_story(proxy: Path, **kwargs: Any) -> dict[str, Any]:
        seconds = float(kwargs["duration"])
        moments = [{"start": 0.0, "end": round(seconds, 3), "label": "All of it", "reason": "Short clip."}]
        if seconds >= 1.5:
            moments = [{"start": 0.0, "end": round(seconds * 0.4, 3), "label": "Opening", "reason": "Stub."},
                       {"start": round(seconds * 0.55, 3), "end": round(seconds * 0.95, 3), "label": "Later", "reason": "Stub."}]
        return {"title": "A cut", "summary": "", "caption": "", "cover_prompt": "", "hashtags": [], "provider": "meta",
                "usage": {"cost_usd": 0.0}, "warnings": [], "segments": moments}

    def transcribe_audio(wav: Path, **kwargs: Any) -> dict[str, Any]:
        return {"cues": [{"start": 0.0, "end": 0.4, "text": "Hello there."}], "provider": "meta", "usage": {"cost_usd": 0.0}}

    monkeypatch.setattr(meta, "plan_story", plan_story)
    monkeypatch.setattr(meta, "transcribe_audio", transcribe_audio)


@pytest.fixture
def manager(tmp_path: Path, meta_stub: None):
    instance = CampaignManager(tmp_path, start_worker=False)
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def inbox(tmp_path: Path) -> Path:
    folder = tmp_path / "vedit-sessions" / "upload" / "inbox"
    folder.mkdir(parents=True)
    return folder


def render(manager: CampaignManager, clips: list[Path], aspect: str, **request: Any) -> dict[str, Any]:
    job = manager.create({"paths": [str(clip) for clip in clips], "goal": "Show the footage clearly", "target_duration": 30,
                          "captions": True, "provider": "meta", "aspect": aspect, **request})
    manager.process(job["id"])
    planned = manager.get(job["id"])
    assert planned["status"] == "planned", planned["error"]
    manager.render(planned["id"], {})
    manager.process(planned["id"])
    done = manager.get(planned["id"])
    assert done["status"] == "complete", done["error"]
    return done


def stream_seconds(video: Path) -> dict[str, float]:
    """Each stream's own length, as a player sees it: the container can outlast its picture."""
    seconds = {}
    for kind in ("v", "a"):
        text = subprocess.run([resolve_ffprobe(), "-v", "error", "-select_streams", f"{kind}:0", "-show_entries", "stream=duration", "-of", "csv=p=0", str(video)],
                              check=True, capture_output=True, text=True).stdout.strip()
        if text and text != "N/A":
            seconds[kind] = float(text)
    return seconds


def delivered(manager: CampaignManager, done: dict[str, Any], aspect: str, *, audio: bool | None = True, fps: float = 30.0) -> Path:
    """The file and its receipts say the same thing, and both say what was asked. ``audio=None`` leaves sound unchecked."""
    video = manager.artifact(done["id"], "video")
    info = probe(video)
    assert (info.width, info.height) == CANVAS[aspect]
    assert info.has_video and (audio is None or info.has_audio is audio)
    assert info.has_audio is done["result"]["proof"]["has_audio"]
    assert abs(info.duration - done["plan"]["duration"]) <= 0.35
    assert info.fps == pytest.approx(fps, abs=0.05)
    lengths = stream_seconds(video)
    if "a" in lengths:
        assert abs(lengths["a"] - lengths["v"]) <= 1 / fps + 0.001, f"sound and picture end apart: {lengths}"
    assert (info.video.color_space, info.video.color_range, info.video.color_primaries, info.video.color_trc) == ("bt709", "tv", "bt709", "bt709")
    report = json.loads(manager.artifact(done["id"], "report").read_text(encoding="utf-8"))
    assert report["proof"] == done["result"]["proof"] and all(step["ok"] for step in report["steps"])
    for receipt in ("brief.yaml", "project.json", "render.sh"):
        assert (video.parent / receipt).is_file(), receipt
    assert not [check for check in done["result"]["checks"] if not check["passed"]]
    return video


def box_ratio(video: Path, aspect: str, at: float = 0.4) -> float:
    """Width over height of the bright run through the frame's centre."""
    width, height = (side // 4 for side in CANVAS[aspect])
    raw = subprocess.run([resolve_ffmpeg(), "-v", "error", "-ss", f"{at:.3f}", "-i", str(video), "-frames:v", "1", "-vf", f"scale={width}:{height}",
                          "-f", "rawvideo", "-pix_fmt", "gray", "-"], check=True, capture_output=True).stdout
    rows = [raw[index * width:(index + 1) * width] for index in range(height)]

    def run(values: list[int], centre: int) -> int:
        assert values[centre] > 128, "the square left the centre of the frame"
        low = high = centre
        while low > 0 and values[low - 1] > 128:
            low -= 1
        while high < len(values) - 1 and values[high + 1] > 128:
            high += 1
        return high - low + 1

    return run(list(rows[height // 2]), width // 2) / run([row[width // 2] for row in rows], height // 2)


SHAPES = {  # each to every export; the PAL-rate HDV clip keeps 25 fps
    "21x9": square("840x360"),
    "2_39x1": square("860x360"),
    "4x3": square("480x360"),
    "odd_639x359": square("639x359", video=H264_444),
    "hdv_anamorphic": square("720x540", sar=(4, 3), rate=25),
    "phone_rotated_90": turned(square("640x360")),
}


@pytest.mark.parametrize("aspect", list(CANVAS))
@pytest.mark.parametrize("shape", list(SHAPES))
def test_a_square_stays_square_from_any_shape_to_any_export(manager: CampaignManager, inbox: Path, shape: str, aspect: str) -> None:
    clip = inbox / "clip.mp4"
    SHAPES[shape](clip)
    video = delivered(manager, render(manager, [clip], aspect), aspect, fps=25.0 if shape == "hdv_anamorphic" else 30.0)
    assert box_ratio(video, aspect) == pytest.approx(1.0, abs=0.06)


@dataclass(frozen=True)
class Clip:
    id: str
    build: Callable[[Path], None]
    suffix: str = ".mp4"
    aspect: str = "9:16"
    audio: bool | None = True
    encoder: str | None = None
    fps: float = 30.0  # the source's own rate, snapped to a standard one


def made(*args: str) -> Callable[[Path], None]:
    def build(path: Path) -> None:
        ffmpeg(*args, str(path))
    return build


def with_cover(path: Path) -> None:
    cover = path.with_name("cover.png")
    ffmpeg("-f", "lavfi", "-i", "color=c=red:s=640x360:d=1", "-frames:v", "1", str(cover))
    flat = path.with_name("flat.mp4")
    made(*moving("640x360"), *tone(2), *H264, *AAC)(flat)
    ffmpeg("-i", str(flat), "-i", str(cover), "-map", "0:v", "-map", "0:a", "-map", "1:v", "-c:v:0", "copy", "-c:a", "copy", "-c:v:1", "png",
           "-disposition:v:1", "attached_pic", str(path))


def with_font_attachment(path: Path) -> None:
    flat = path.with_name("flat.mkv")
    made(*moving("640x360"), *tone(2), *H264, *AAC)(flat)
    font = path.with_name("font.ttf")
    font.write_bytes(b"\0" * 64)
    ffmpeg("-i", str(flat), "-map", "0", "-c", "copy", "-attach", str(font), "-metadata:s:t", "mimetype=font/ttf", str(path))


PQ_HDR10 = ("log-level=error:hdr10=1:repeat-headers=1:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:"
            "master-display=G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1):max-cll=1000,400")

CLIPS = [
    Clip("fps_23_976", made(*moving("640x360", "24000/1001"), *tone(2), *H264, *AAC), fps=24000 / 1001),
    Clip("fps_25", made(*moving("640x360", 25), *tone(2), *H264, *AAC), fps=25.0),
    Clip("fps_29_97", made(*moving("640x360", "30000/1001"), *tone(2), *H264, *AAC), fps=30000 / 1001),
    Clip("fps_50", made(*moving("640x360", 50), *tone(2), *H264, *AAC), fps=50.0),
    Clip("fps_59_94_to_16x9", made(*moving("640x360", "60000/1001"), *tone(2), *H264, *AAC), aspect="16:9", fps=60000 / 1001),
    Clip("fps_120_capped_at_60", made(*moving("640x360", 120), *tone(2), *H264, *AAC), fps=60.0),
    # Known gap: FFmpeg's loudnorm pads its output to whole 100 ms frames, and vedit.analyze adds it after the
    # export's trim (analyze.py, apply_encode). This 3.21 s cut gets 3.30 s of sound until a trim follows loudnorm.
    Clip("fps_1_timelapse", made(*moving("640x360", 1, seconds=4), *tone(4), *H264, *AAC), fps=24000 / 1001),
    Clip("hdr10_pq_with_mastering_metadata", made(*moving("640x360"), *tone(2), "-pix_fmt", "yuv420p10le", "-c:v", "libx265", "-preset", "ultrafast", "-tag:v", "hvc1",
                                                  "-x265-params", PQ_HDR10, *AAC), encoder="libx265"),
    Clip("ten_bit_sdr_h264", made(*moving("640x360"), *tone(2), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p10le", *AAC)),
    Clip("prores_422hq_10bit", made(*moving("640x360"), *tone(2), "-c:v", "prores_ks", "-profile:v", "3", "-pix_fmt", "yuv422p10le", "-c:a", "pcm_s24le"),
         suffix=".mov", encoder="prores_ks"),
    Clip("av1_mp4", made(*moving("640x360"), *tone(2), "-c:v", "libsvtav1", "-preset", "12", "-pix_fmt", "yuv420p", *AAC), encoder="libsvtav1"),
    Clip("hevc_ts", made(*moving("640x360"), *tone(2), *X265[:-2], *AAC), suffix=".ts", encoder="libx265"),
    Clip("vp9_odd_1279x719", made(*moving("1279x719"), *tone(2), "-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-pix_fmt", "yuv420p", "-c:a", "libopus"),
         suffix=".webm", aspect="1:1", encoder="libvpx-vp9"),
    Clip("mjpeg_odd_1279x719_avi", made(*moving("1279x719"), *tone(2), "-c:v", "mjpeg", "-pix_fmt", "yuvj422p", "-c:a", "pcm_s16le"), suffix=".avi"),
    Clip("short_0_25s_120fps", made(*moving("640x360", 120, seconds=0.25), *tone(0.25), *H264, *AAC), fps=60.0),
    Clip("no_audio_landscape", made(*moving("640x360"), *H264), audio=False),
    # Reframed through the project source, which holds a silent track for clips without sound.
    Clip("no_audio_portrait_to_16x9", made(*moving("360x640"), *H264), aspect="16:9", audio=None),
    Clip("cover_art", with_cover),
    Clip("mkv_font_attachment", with_font_attachment, suffix=".mkv"),
    Clip("audio_stream_first", made(*tone(2), *moving("640x360"), "-map", "0:a", "-map", "1:v", *H264, *AAC)),
]


@pytest.mark.parametrize("case", CLIPS, ids=lambda case: getattr(case, "id", None))
def test_any_rate_codec_and_container_delivers(manager: CampaignManager, inbox: Path, case: Clip) -> None:
    if case.encoder and not has_encoder(case.encoder):
        pytest.skip(f"{case.encoder} is not in this FFmpeg build")
    clip = inbox / f"clip{case.suffix}"
    case.build(clip)
    delivered(manager, render(manager, [clip], case.aspect), case.aspect, audio=case.audio, fps=case.fps)


# ---------------------------------------------------------------------------
# Fixes
# ---------------------------------------------------------------------------


def test_filmstrip_reads_every_tile_of_a_transport_stream(tmp_path: Path) -> None:
    """A transport stream seeks to a keyframe that can land past the moment, which used to leave no tile at all."""
    clip = tmp_path / "broadcast.ts"
    ffmpeg("-f", "lavfi", "-i", "color=c=red:s=320x180:r=30:d=1.5", "-f", "lavfi", "-i", "color=c=blue:s=320x180:r=30:d=1.5",
           "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0", *H264, "-g", "600", str(clip))
    info = probe(clip)
    assert info.seek_lead_in > 0
    strip = tmp_path / "filmstrip.jpg"
    layout = project_media.make_filmstrip(clip, strip, duration=info.duration, width=info.width, height=info.height, stream=info.video_map)
    assert layout["count"] == 2
    raw = subprocess.run([resolve_ffmpeg(), "-v", "error", "-i", str(strip), "-vf", "scale=8:1:flags=area", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         check=True, capture_output=True).stdout
    first, second = raw[3:6], raw[18:21]  # the middle of each tile
    assert first[0] > 150 and first[2] < 100, f"the first tile should be the red half: {first.hex()}"
    assert second[2] > 150 and second[0] < 100, f"the second tile should be the blue half: {second.hex()}"


def test_mp4_needs_no_seek_lead_in(tmp_path: Path) -> None:
    clip = tmp_path / "plain.mp4"
    made(*moving("320x180", seconds=0.5), *H264)(clip)
    assert probe(clip).seek_lead_in == 0.0


def _item(fps: float, kind: str = "video") -> project_media.MediaItem:
    return project_media.MediaItem(Path("x"), "x", kind, 640, 360, 1.0 if kind == "video" else 0.0, fps=fps if kind == "video" else 0.0)


@pytest.mark.parametrize("fps,expected", [
    (24000 / 1001, 24000 / 1001), (23.98, 24000 / 1001), (24.0, 24.0), (25.0, 25.0), (30000 / 1001, 30000 / 1001), (29.995, 30.0), (30.0, 30.0),
    (50.0, 50.0), (60000 / 1001, 60000 / 1001), (60.0, 60.0), (120.0, 60.0), (240.0, 60.0), (1.0, 24000 / 1001), (26.4, 25.0), (None, 30.0), (0.0, 30.0),
])
def test_standard_rate_snaps_to_broadcast_rates(fps: float | None, expected: float) -> None:
    assert standard_rate(fps) == pytest.approx(expected, abs=1e-9)


def test_rate_text_is_the_exact_fraction() -> None:
    assert [rate_text(rate) for rate in STANDARD_RATES] == ["24000/1001", "24", "25", "30000/1001", "30", "50", "60000/1001", "60"]


def test_project_clock_follows_the_first_video() -> None:
    assert project_media.choose_rate([_item(25.0), _item(60.0)]) == 25.0
    assert project_media.choose_rate([_item(0, "photo"), _item(60000 / 1001)]) == pytest.approx(60000 / 1001)
    assert project_media.choose_rate([_item(120.0)]) == 60.0
    assert project_media.choose_rate([_item(0, "photo"), _item(0, "photo")]) == 30.0


def test_project_sound_matches_picture_at_broadcast_rates() -> None:
    spec = {"frames": 90, "has_audio": False}
    assert "end_sample=144144" in project_media.sound_graph(spec, 30000 / 1001)  # 90 frames of 29.97 is 3.003 s
    assert "end_sample=72000" in project_media.sound_graph(spec, 60)
    assert "end_sample=172800" in project_media.sound_graph(spec, 25)


def test_project_source_refuses_a_rate_off_the_standard_ones(tmp_path: Path) -> None:
    from vedit.compile import compile_brief
    from vedit.ir import Brief, Canvas

    clip = tmp_path / "a.mp4"
    made(*moving("320x180", seconds=0.5), *H264)(clip)
    spec = {"source": "a", "kind": "video", "frames": 12, "width": 320, "height": 180, "has_audio": False}
    brief = Brief(name="p", sources={"a": str(clip)}, ops=[{"op": "project_source", "path": str(tmp_path / "out.mp4"), "items": [spec]}],
                  canvas=Canvas(width=320, height=180, fps=45))
    with pytest.raises(CompileError, match="standard frame rate"):
        compile_brief(brief, work_dir=tmp_path / "work")


def _frame_hashes(video: Path) -> list[str]:
    lines = subprocess.run([resolve_ffmpeg(), "-v", "error", "-i", str(video), "-map", "0:v:0", "-f", "framemd5", "-"], check=True, capture_output=True, text=True).stdout
    return [line.rsplit(",", 1)[-1].strip() for line in lines.splitlines() if line and not line.startswith("#")]


def _repeats(hashes: list[str]) -> int:
    """A frame shown twice in a row, which is what a slower clock leaves in a faster export."""
    return sum(1 for a, b in zip(hashes, hashes[1:]) if a == b)


def test_a_59_94_project_keeps_every_filmed_frame(tmp_path: Path) -> None:
    """Two 59.94 fps phone clips used to become a 30 fps project, so a 60 fps export showed each frame twice."""
    first, second = tmp_path / "one.mp4", tmp_path / "two.mov"
    made(*moving("320x180", "60000/1001", seconds=1.0), *tone(1), *H264, *AAC)(first)
    made(*moving("180x320", "60000/1001", seconds=0.5), *H264)(second)
    items = [project_media.inspect_clip(path, 600) for path in (first, second)]
    dest = tmp_path / "project.mp4"
    result = project_media.build_project(items, work_dir=tmp_path / "work", dest=dest)
    info = probe(dest)
    assert result["fps"] == pytest.approx(60000 / 1001) and info.fps == pytest.approx(59.94, abs=0.01)
    hashes = _frame_hashes(dest)
    assert len(hashes) == round(result["duration"] * 60000 / 1001)
    assert _repeats(hashes) <= 3, f"{_repeats(hashes)} repeated frames"
    lengths = stream_seconds(dest)
    assert abs(lengths["a"] - lengths["v"]) <= 1 / 59.94 + 0.001, lengths


def test_a_25fps_first_clip_makes_a_25fps_project(tmp_path: Path) -> None:
    clip = tmp_path / "pal.mp4"
    made(*moving("320x180", 25, seconds=0.6), *tone(0.6), *H264, *AAC)(clip)
    dest = tmp_path / "project.mp4"
    result = project_media.build_project([project_media.inspect_clip(clip, 600)], work_dir=tmp_path / "work", dest=dest)
    assert result["fps"] == 25.0 and probe(dest).fps == pytest.approx(25, abs=0.01)
    assert _repeats(_frame_hashes(dest)) == 0


def test_multi_clip_export_matches_the_source_rate_by_default(manager: CampaignManager, inbox: Path) -> None:
    """No export rate chosen: a 59.94 fps project exports at 59.94, every filmed frame once."""
    first, second = inbox / "a.mp4", inbox / "b.mp4"
    made(*moving("640x360", "60000/1001", seconds=1.5), *tone(1.5), *H264, *AAC)(first)
    made(*moving("640x360", 60, seconds=1.5), *tone(1.5), *H264, *AAC)(second)
    done = render(manager, [first, second], "16:9")
    video = delivered(manager, done, "16:9", fps=60000 / 1001)
    hashes = _frame_hashes(video)
    assert _repeats(hashes) <= len(hashes) // 20, f"{_repeats(hashes)} of {len(hashes)} frames repeat"


@pytest.mark.parametrize("chosen,expected", [(24, 24.0), (30, 30.0), (60, 60.0)])
def test_an_explicit_export_rate_still_wins(manager: CampaignManager, inbox: Path, chosen: int, expected: float) -> None:
    clip = inbox / "pal.mp4"
    made(*moving("640x360", 25, seconds=1.5), *tone(1.5), *H264, *AAC)(clip)
    delivered(manager, render(manager, [clip], "9:16", edit={"export": {"fps": chosen}}), "9:16", fps=expected)


def test_a_gif_stays_at_15fps_or_less_when_matching_the_source() -> None:
    from vedit.presets import get_preset
    from vedit.render_plan import delivery_preset
    from vedit.edit_spec import default_edit

    edit = default_edit()
    edit["export"] = dict(edit["export"], format="gif")
    assert delivery_preset("shorts_9x16", edit).canvas.fps == 15.0
    assert get_preset(export_preset_name("shorts_9x16", fps=60, format="gif")).canvas.fps == 15.0


def test_match_source_is_the_default_and_listed_for_studio() -> None:
    from vedit.edit_spec import catalogue, default_edit, validate_edit

    assert default_edit()["export"]["fps"] == "source"
    assert catalogue()["export"]["fps"][0] == "source"
    assert validate_edit({"export": {"fps": 24}}, duration=10)["export"]["fps"] == 24
    with pytest.raises(Exception):
        validate_edit({"export": {"fps": 48}}, duration=10)


def test_preset_names_carry_broadcast_and_source_rates() -> None:
    from vedit.presets import get_preset

    assert export_preset_name("shorts_9x16", fps="source") == "export_1080x1920_source_mp4"
    assert get_preset("export_1080x1920_source_mp4").canvas.fps == 0.0
    assert export_preset_name("youtube_1080p", fps=30000 / 1001) == "export_1920x1080_29.97_mp4"
    assert get_preset("export_1920x1080_29.97_mp4").canvas.fps == 30000 / 1001
    assert get_preset("export_3840x2160_23.976_mp4").canvas.fps == 24000 / 1001
    assert export_preset_name("shorts_9x16") == "shorts_9x16"


def test_compile_resolves_a_source_rate_canvas_from_the_footage(tmp_path: Path) -> None:
    from vedit.compile import compile_brief
    from vedit.ir import Brief, Canvas

    clip = tmp_path / "ntsc.mp4"
    made(*moving("320x180", "30000/1001", seconds=0.5), *H264)(clip)
    brief = Brief(name="b", sources={"footage": str(clip)}, ops=[{"op": "export", "preset": "shorts_9x16", "fps": "source", "path": str(tmp_path / "out.mp4")}],
                  canvas=Canvas(width=1080, height=1920, fps=0))
    compiler = compile_brief(brief, work_dir=tmp_path / "work")
    assert compiler.canvas.fps == pytest.approx(30000 / 1001)
    export = next(step for step in compiler.pipeline.steps if step.name.startswith("export_"))
    assert "export_1080x1920_29.97_mp4" in export.argv


def _stream(**fields: Any) -> StreamInfo:
    return StreamInfo(index=0, codec_type="video", codec_name="h264", width=720, height=576, **fields)


def test_square_width_turns_with_a_rotated_anamorphic_picture() -> None:
    """FFmpeg's transpose inverts the pixel aspect, so 720x576 at 16:11 on its side is 396x720 once square."""
    flat = MediaInfo(Path("x"), 1.0, "mov", 1, None, (_stream(sample_aspect_ratio=16 / 11),))
    side = MediaInfo(Path("x"), 1.0, "mov", 1, None, (_stream(sample_aspect_ratio=16 / 11, rotation=90.0),))
    assert (flat.square_width, flat.display_height) == (1047, 576)
    assert (side.square_width, side.display_height) == (396, 720)


def test_rotated_anamorphic_square_width_matches_ffmpeg(tmp_path: Path) -> None:
    clip = tmp_path / "sideways-pal.mp4"
    turned(square("720x576", sar=(16, 11), rate=25, seconds=0.4))(clip)
    info = probe(clip)
    # SQUARE_PIXELS, after FFmpeg has turned the picture upright.
    log = subprocess.run([resolve_ffmpeg(), "-hide_banner", "-nostats", "-i", str(clip), "-frames:v", "1", "-vf", "scale=iw*sar:ih,setsar=1,showinfo",
                          "-f", "null", "-"], capture_output=True, text=True).stderr
    assert f"s:{info.square_width}x{info.display_height}" in log, log[-600:]


def _cover_first(path: Path, footage: list[str]) -> None:
    """A large still as the first video stream, the footage after it, as some exports write."""
    cover = path.with_name("cover.png")
    ffmpeg("-f", "lavfi", "-i", "color=c=red:s=1920x1080:d=1", "-frames:v", "1", str(cover))
    body = path.with_name("body.mp4")
    ffmpeg(*footage, *H264, str(body))
    # Flagged default, the still is what FFmpeg itself picks when no stream is named.
    ffmpeg("-i", str(cover), "-i", str(body), "-map", "0:v", "-map", "1:v", "-c:v:0", "png", "-c:v:1", "copy",
           "-disposition:v:0", "default", "-disposition:v:1", "0", str(path))


def test_auto_look_measures_the_footage_not_a_cover_still(tmp_path: Path) -> None:
    from vedit.looks import measure

    clip = tmp_path / "cover-first.mkv"
    _cover_first(clip, ["-f", "lavfi", "-i", "color=c=gray:s=320x180:r=30:d=1"])
    assert probe(clip).video_map == 1
    stats = measure(clip)
    assert stats["chroma"] < 0.05, f"measured the red cover: {stats}"


def test_camera_shake_reads_the_footage_not_a_cover_still(tmp_path: Path) -> None:
    from vedit import motion

    clip = tmp_path / "cover-first.mkv"
    shaky = ["-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=4",
             "-vf", "crop=520:290:x='60+50*sin(n*1.7)':y='35+30*cos(n*2.3)',scale=480:270"]
    _cover_first(clip, shaky)
    measured = motion.measure_camera_shake(clip)
    assert measured is not None and measured >= motion.STABILIZE_SHAKE_WARNING, measured


def test_probe_reports_odd_sizes_as_stored(tmp_path: Path) -> None:
    """``testsrc2=size=1279x719`` silently makes 1278x718; the matrix above scales instead, and checks it."""
    clip = tmp_path / "odd.mp4"
    made(*moving("1279x719", seconds=0.2), *H264_444)(clip)
    assert (probe(clip).width, probe(clip).height) == (1279, 719)


# ---------------------------------------------------------------------------
# HDR: one tone map, chosen by measurement on iPhone HLG footage
# ---------------------------------------------------------------------------

needs_hdr = pytest.mark.skipif(not (has_filter("zscale") and has_filter("tonemap") and has_encoder("libx265")), reason="needs zscale, tonemap and libx265")
OLD_TONEMAP = "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=mobius,zscale=t=bt709:m=bt709:r=tv,format=yuv420p"


def hdr_clip(path: Path, transfer: str, *, nits: int, source: str = "testsrc2=size=320x180:rate=30:duration=0.3") -> Path:
    """An SDR picture placed in HDR with its white at ``nits``: 203 is BT.2408 reference white, 1000 a bright highlight."""
    place = f"zscale=tin=bt709:pin=bt709:min=bt709:rin=tv:t={transfer}:p=bt2020:m=bt2020nc:r=tv:npl={nits},format=yuv420p10le"
    ffmpeg("-f", "lavfi", "-i", source, "-vf", place, *X265, "-color_primaries", "bt2020", "-color_trc", transfer, "-colorspace", "bt2020nc", str(path))
    return path


def luma(path: Path, chain: str = "", *, source: list[str] | None = None) -> bytes:
    """The Y plane of one frame after ``chain``, at 160x90, in limited range: black 16, white 235."""
    vf = ",".join(part for part in (chain, "scale=160:90", "format=yuv420p") if part)
    return subprocess.run([resolve_ffmpeg(), "-v", "error", *(source or ["-i", str(path)]), "-frames:v", "1", "-vf", vf, "-f", "rawvideo", "-"],
                          check=True, capture_output=True).stdout[:160 * 90]


def clipped(y: bytes) -> float:
    return sum(1 for value in y if value >= 235) / len(y)


@needs_hdr
@pytest.mark.parametrize("transfer", ["arib-std-b67", "smpte2084"])
def test_hdr_highlights_keep_their_detail(tmp_path: Path, transfer: str) -> None:
    """A picture at 1000 nits: the old npl=100 mobius chain left 31% of it at Y >= 235."""
    clip = hdr_clip(tmp_path / "bright.mov", transfer, nits=1000)
    chain = source_filters(probe(clip))
    assert clipped(luma(clip, chain)) <= 0.01
    # FFmpeg 9 with the old chain left about 29% of this pattern at Y >= 235. FFmpeg 6.1's zscale and
    # tonemap (Ubuntu 24.04) left none, so there the old chain has no clipping to compare against.
    if ffmpeg_major() > 6:
        assert clipped(luma(clip, OLD_TONEMAP)) > 0.2, "the pattern no longer shows the old clipping"


@needs_hdr
@pytest.mark.parametrize("transfer", ["arib-std-b67", "smpte2084"])
def test_hdr_reference_white_comes_back_as_sdr_white(tmp_path: Path, transfer: str) -> None:
    """Bars placed at BT.2408 reference white return close to the SDR bars; the old chain was 18.8 levels off on average."""
    bars = "smptehdbars=size=320x180:rate=30:duration=0.3"
    clip = hdr_clip(tmp_path / "bars.mov", transfer, nits=203, source=bars)
    sdr = luma(clip, source=["-f", "lavfi", "-i", bars])
    mapped = luma(clip, source_filters(probe(clip)))
    assert sum(abs(a - b) for a, b in zip(mapped, sdr)) / len(sdr) <= 8
    assert abs(sum(mapped) / len(mapped) - sum(sdr) / len(sdr)) <= 8


def test_one_tone_map_serves_single_clips_and_projects() -> None:
    hdr = StreamInfo(index=0, codec_type="video", codec_name="hevc", width=1920, height=1080, color_trc="arib-std-b67", color_primaries="bt2020", color_space="bt2020nc")
    info = MediaInfo(Path("x"), 1.0, "mov", 1, None, (hdr,))
    chain = tone_map()
    if not chain:
        pytest.skip("this FFmpeg has no zscale and tonemap")
    assert chain in project_media._color_head({"hdr": True})
    assert source_filters(info).endswith(chain.split(",", 1)[1])
    assert "npl=203" in chain and "desat=0" in chain and "npl=100" not in chain


@pytest.mark.skipif(not (has_filter("zscale") and has_filter("tonemap")), reason="needs zscale and tonemap")
def test_tone_map_fills_the_tags_a_file_leaves_out() -> None:
    """Frames tagged HLG with no primaries or matrix: zscale finds no path unless the chain says BT.2020."""
    partial = StreamInfo(index=0, codec_type="video", codec_name="hevc", color_trc="arib-std-b67")
    chain = tone_map(partial)
    assert "tin=arib-std-b67" in chain and "pin=bt2020" in chain and "p=bt2020" in chain and "min=bt2020nc" in chain
    frames = ["-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=0.1,format=yuv420p10le,setparams=color_trc=arib-std-b67:color_primaries=unknown:colorspace=unknown"]
    old = subprocess.run([resolve_ffmpeg(), "-v", "error", *frames, "-frames:v", "1", "-vf", OLD_TONEMAP, "-f", "null", "-"], capture_output=True, text=True)
    assert old.returncode != 0
    assert len(luma(Path("x"), chain, source=frames)) == 160 * 90


# ---------------------------------------------------------------------------
# Black bars: found once at ingest, cropped before any reframe
# ---------------------------------------------------------------------------


def boxed(frame: str, picture: str, *, seconds: float = 2.0, rate: int = 30) -> Callable[[Path], None]:
    """Moving footage inside black bars, centred, as a TV export or an archive transfer leaves it."""
    def build(path: Path) -> None:
        fw, fh = frame.split("x")
        pw, ph = picture.split("x")
        ffmpeg("-f", "lavfi", "-i", f"testsrc2=size=640x360:rate={rate}:duration={seconds},scale={pw}:{ph},pad={fw}:{fh}:(ow-iw)/2:(oh-ih)/2:black",
               *tone(seconds), *H264, *AAC, str(path))
    return build


@pytest.mark.parametrize("frame,picture", [("960x720", "960x540"), ("1280x720", "960x720"), ("1280x720", "480x300"), ("720x1280", "720x406")],
                         ids=["letterbox", "pillarbox", "windowbox", "portrait_letterbox"])
def test_bars_are_found_and_reported_by_probe(tmp_path: Path, frame: str, picture: str) -> None:
    clip = tmp_path / "boxed.mp4"
    boxed(frame, picture)(clip)
    found = detect_borders(probe(clip))
    pw, ph = (int(value) for value in picture.split("x"))
    fw, fh = (int(value) for value in frame.split("x"))
    assert found is not None
    width, height, x, y = found
    assert abs(width - pw) <= 4 and abs(height - ph) <= 4 and abs(x - (fw - pw) / 2) <= 4 and abs(y - (fh - ph) / 2) <= 4, found
    info = probe(clip)
    assert info.crop == found and (info.display_width, info.display_height) == (width, height)
    assert (info.frame_width, info.frame_height) == (fw, fh)
    assert source_filters(info).startswith(f"crop={width}:{height}:{x}:{y}")


def test_bars_are_found_upright_on_a_rotated_phone_clip(tmp_path: Path) -> None:
    clip = tmp_path / "sideways.mp4"
    turned(boxed("1280x720", "640x360"))(clip)
    width, height, x, y = detect_borders(probe(clip))
    assert (abs(width - 360), abs(height - 640), abs(x - 180), abs(y - 320)) <= (4, 4, 4, 4) and max(abs(width - 360), abs(height - 640)) <= 4


@pytest.mark.parametrize("name", ["full_frame", "small_subject_on_black", "dark_textured_edges", "dark_top_only"])
def test_pictures_without_bars_are_left_alone(tmp_path: Path, name: str) -> None:
    clip = tmp_path / f"{name}.mp4"
    graphs = {
        "full_frame": "testsrc2=size=640x360:rate=30:duration=2",
        "small_subject_on_black": "color=black:size=640x360:rate=30:duration=2,drawbox=x=290:y=150:w=60:h=60:color=white:t=fill",
        # Dark edges with grain: a night scene, not bars.
        "dark_textured_edges": "testsrc2=size=640x360:rate=30:duration=2,drawbox=y=0:h=60:color=0x101010:t=fill,drawbox=y=300:h=60:color=0x101010:t=fill,noise=alls=30:allf=t",
        # One dark side is not a pair of bars.
        "dark_top_only": "testsrc2=size=640x360:rate=30:duration=2,drawbox=y=0:h=90:color=black:t=fill",
    }
    ffmpeg("-f", "lavfi", "-i", graphs[name], *H264, str(clip))
    assert detect_borders(probe(clip)) is None
    assert probe(clip).crop is None


def black_share(video: Path, at: float, aspect: str) -> float:
    width, height = (side // 8 for side in CANVAS[aspect])
    y = subprocess.run([resolve_ffmpeg(), "-v", "error", "-ss", f"{at:.3f}", "-i", str(video), "-frames:v", "1", "-vf", f"scale={width}:{height},format=yuv420p",
                        "-f", "rawvideo", "-"], check=True, capture_output=True).stdout[:width * height]
    return sum(1 for value in y if value <= 20) / len(y)


@pytest.mark.parametrize("aspect", ["9:16", "16:9"])
def test_a_windowboxed_clip_fills_the_export(manager: CampaignManager, inbox: Path, aspect: str) -> None:
    """Archive film inside an HD frame used to come out 79% black in a 9:16 export."""
    clip = inbox / "archive.mp4"
    boxed("1280x720", "480x300")(clip)
    video = delivered(manager, render(manager, [clip], aspect), aspect)
    assert black_share(video, 0.5, aspect) < 0.1


def test_a_boxed_clip_in_a_project_is_cropped_before_it_is_fitted(tmp_path: Path) -> None:
    boxed_clip, plain = tmp_path / "boxed.mp4", tmp_path / "plain.mp4"
    boxed("1280x720", "640x360")(boxed_clip)
    made(*moving("640x360", seconds=1.0), *tone(1), *H264, *AAC)(plain)
    items = [project_media.inspect_clip(path, 600) for path in (boxed_clip, plain)]
    assert (items[0].width, items[0].height) == (640, 360) and items[0].crop is not None
    dest = tmp_path / "project.mp4"
    result = project_media.build_project(items, work_dir=tmp_path / "work", dest=dest)
    assert (result["width"], result["height"]) == (640, 360)
    assert black_share(dest, 0.5, "16:9") < 0.1


# ---------------------------------------------------------------------------
# Sound and picture end together; compile flags from the audio sweep
# ---------------------------------------------------------------------------


def _compile(tmp_path: Path, ops: list[dict[str, Any]], sources: dict[str, str], fps: float = 30) -> Any:
    from vedit.compile import compile_brief
    from vedit.ir import Brief, Canvas

    return compile_brief(Brief(name="t", sources=sources, ops=ops, canvas=Canvas(width=1080, height=1920, fps=fps)), work_dir=tmp_path / "work")


def _step(compiler: Any, name: str) -> list[str]:
    return next(step.argv for step in compiler.pipeline.steps if step.name == name or (name.endswith("_") and step.name.startswith(name)))


def test_export_holds_sound_to_the_picture_clock(tmp_path: Path) -> None:
    clip = tmp_path / "long-sound.mp4"
    ffmpeg(*moving("320x180", seconds=1.0), *tone(2.5), *H264, *AAC, str(clip))
    compiler = _compile(tmp_path, [{"op": "select_ranges", "ranges": [{"start": 0.0, "end": 0.5}, {"start": 0.6, "end": 0.95}]},
                                   {"op": "export", "preset": "shorts_9x16", "path": str(tmp_path / "out.mp4")}], {"footage": str(clip)})
    argv = _step(compiler, "export_")
    ending = f"{round(0.85 * 30) / 30:.6f}"  # the frames the second cut really ends on, not the plan's rounded 0.85 s
    assert f"atrim=end={ending}" in argv[argv.index("--af") + 1] and f"apad=whole_dur={ending}" in argv[argv.index("--af") + 1]
    assert f"trim=end={ending}" in argv[argv.index("--vf") + 1]


def test_loudnorm_pads_to_100ms_so_its_output_needs_the_trim_after_it(tmp_path: Path) -> None:
    """The measured cause of sound outlasting picture: loudnorm rounds its output up to a whole 100 ms frame."""
    wav, out = tmp_path / "cut.wav", tmp_path / "cut.m4a"
    ffmpeg("-f", "lavfi", "-i", "sine=f=440:r=48000:d=3.211542", "-c:a", "pcm_s16le", str(wav))
    ffmpeg("-i", str(wav), "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-c:a", "aac", str(out))
    assert probe(out).duration == pytest.approx(3.3, abs=0.005)
    ffmpeg("-i", str(wav), "-af", "loudnorm=I=-16:TP=-1.5:LRA=11,atrim=end=3.211542", "-c:a", "aac", str(out))
    assert probe(out).duration == pytest.approx(3.2115, abs=0.005)


def test_export_leaves_length_alone_once_a_later_op_changes_it(tmp_path: Path) -> None:
    clip = tmp_path / "talk.mp4"
    ffmpeg(*moving("320x180", seconds=1.0), *tone(1.0), *H264, *AAC, str(clip))
    compiler = _compile(tmp_path, [{"op": "select_ranges", "ranges": [{"start": 0.0, "end": 1.0}]}, {"op": "speed", "factor": 2.0},
                                   {"op": "export", "preset": "shorts_9x16", "path": str(tmp_path / "out.mp4")}], {"footage": str(clip)})
    argv = _step(compiler, "export_")
    assert "atrim=end=" not in " ".join(argv)


def test_pause_trimming_measures_its_own_threshold_unless_told(tmp_path: Path) -> None:
    clip = tmp_path / "talk.mp4"
    ffmpeg(*moving("320x180", seconds=1.0), *tone(1.0), *H264, *AAC, str(clip))
    measured = _step(_compile(tmp_path / "a", [{"op": "cut_silence"}], {"footage": str(clip)}), "cut_silence")
    assert "--threshold-db" not in measured
    told = _step(_compile(tmp_path / "b", [{"op": "cut_silence", "threshold_db": -40}], {"footage": str(clip)}), "cut_silence")
    assert told[told.index("--threshold-db") + 1] == "-40.0"


def test_music_uses_the_track_a_player_would_play(tmp_path: Path) -> None:
    clip, bed = tmp_path / "talk.mp4", tmp_path / "bed.mkv"
    ffmpeg(*moving("320x180", seconds=1.0), *tone(1.0), *H264, *AAC, str(clip))
    ffmpeg("-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=2", "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=48000:duration=2",
           "-map", "0:a", "-map", "1:a", "-c:a", "aac", "-disposition:a:0", "0", "-disposition:a:1", "default", str(bed))
    assert probe(bed).audio_map == 1
    argv = _step(_compile(tmp_path, [{"op": "music", "source": "bed"}], {"footage": str(clip), "bed": str(bed)}), "music")
    graph = argv[argv.index("-filter_complex") + 1]
    assert "[1:a:1]" in graph and "[1:a]" not in graph


def test_captions_default_to_the_saved_language(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import vedit.compile as compile_module

    clip = tmp_path / "talk.mp4"
    ffmpeg(*moving("320x180", seconds=1.0), *tone(1.0), *H264, *AAC, str(clip))
    monkeypatch.setattr(compile_module, "default_language", lambda: "es")
    argv = _step(_compile(tmp_path, [{"op": "captions"}], {"footage": str(clip)}), "captions_transcribe")
    assert argv[argv.index("--language") + 1] == "es"


def test_the_hero_word_font_is_chosen_for_its_letters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import vedit.compile as compile_module
    from vedit.ir import Brief, Canvas

    clip, matte = tmp_path / "clip.mp4", tmp_path / "matte.mp4"
    made(*moving("320x180", seconds=3.0), *tone(3.0), *H264, *AAC)(clip)
    made(*moving("320x180", seconds=1.5), *H264)(matte)
    asked: list[tuple[str, str]] = []
    real = compile_module.hero_font

    def spy(font: str, text: str = "") -> Any:
        asked.append((font, text))
        return real(font, text)

    monkeypatch.setattr(compile_module, "hero_font", spy)
    hero = {"op": "hero_word", "word": "東京", "start": 1.0, "end": 2.5, "side": "center", "color": "#FFD60A", "font": "heavy", "matte": str(matte)}
    brief = Brief(name="hero", sources={"footage": str(clip)}, ops=[{"op": "select_ranges", "ranges": [{"start": 0, "end": 3}]}, hero,
                                                                     {"op": "export", "preset": "web_preview", "path": str(tmp_path / "video.mp4")}],
                  canvas=Canvas(width=320, height=180, fps=30))
    compile_module.compile_brief(brief, work_dir=tmp_path / "work")
    assert ("heavy", "東京") in asked


def test_drawtext_says_it_cannot_shape_right_to_left(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import vedit.compile as compile_module

    clip = tmp_path / "clip.mp4"
    made(*moving("320x180", seconds=1.0), *H264)(clip)
    monkeypatch.setattr(compile_module, "text_engine", lambda: "drawtext")
    monkeypatch.setattr(compile_module, "filter_options", lambda name: frozenset())
    compiler = _compile(tmp_path, [{"op": "text", "text": "مرحبا بالعالم", "duration": 1.0}], {"footage": str(clip)})
    assert any("right-to-left" in warning and "backwards" in warning for warning in compiler.warnings)
    assert not any("font fallback" in warning for warning in compiler.warnings)
