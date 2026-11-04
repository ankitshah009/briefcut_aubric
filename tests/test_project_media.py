"""Many clips and photos on one project clock, assets, filmstrip, and waveform, with real media."""

from __future__ import annotations

import http.client
import json
import shutil
import struct
import subprocess
import sys
import threading
import time
import types
from array import array
from collections.abc import Iterator
from pathlib import Path

import pytest

from vedit import project_media
from vedit.analyze import Range
from vedit.campaign import CampaignError, CampaignManager, capabilities
from vedit.compile import compile_brief
from vedit.edit_spec import ASSET_ID, validate_edit
from vedit.errors import CompileError
from vedit.ir import Brief, Canvas
from vedit.media import probe, resolve_ffmpeg, resolve_ffprobe
from vedit.project_media import MediaItem, MediaProblem
from vedit.story import build_plan, clip_cuts, fit_to_clips, merge_cuts
from vedit.studio.server import StudioHandler, _LoopbackServer, _safe_filename

render = pytest.mark.render


def have_ffmpeg() -> bool:
    try:
        return Path(resolve_ffmpeg()).is_file() or shutil.which(resolve_ffmpeg()) is not None
    except Exception:
        return False


needs_ffmpeg = pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg required")


def ffmpeg(*args: str) -> None:
    subprocess.run([resolve_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def make_clip(path: Path, *, size: str = "320x180", seconds: float = 1.0, color: str = "teal", rate: int = 30, tone: int | None = 440, sample_rate: int = 48000, channels: int = 2) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    args = ["-f", "lavfi", "-i", f"color={color}:s={size}:r={rate}:d={seconds}"]
    if tone is not None:
        args += ["-f", "lavfi", "-i", f"sine=f={tone}:r={sample_rate}:d={seconds}"]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if tone is not None:
        args += ["-c:a", "aac", "-ac", str(channels)]
    ffmpeg(*args, str(path))
    return path


def make_photo(path: Path, *, size: str = "320x180", color: str = "red") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg("-f", "lavfi", "-i", f"color={color}:s={size}", "-frames:v", "1", "-update", "1", str(path))
    return path


def make_heic_with_sips(source: Path, dest: Path) -> None:
    command = ["sips", "-s", "format", "heic", str(source), "--out", str(dest)]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode == 13 and "Unable to write image" in result.stderr:
        pytest.skip("macOS sips cannot write HEIC files in this sandbox")
    if result.returncode:
        raise subprocess.CalledProcessError(result.returncode, command, result.stdout, result.stderr)


def make_tone(path: Path, *, seconds: float = 2.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg("-f", "lavfi", "-i", f"sine=f=330:d={seconds}", str(path))
    return path


def with_exif_orientation(photo: Path, dest: Path, orientation: int) -> Path:
    """The smallest honest EXIF block: one IFD entry holding the Orientation tag."""
    tiff = b"II*\0" + struct.pack("<I", 8) + struct.pack("<H", 1) + struct.pack("<HHII", 0x0112, 3, 1, orientation) + struct.pack("<I", 0)
    payload = b"Exif\0\0" + tiff
    raw = photo.read_bytes()
    dest.write_bytes(raw[:2] + b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload + raw[2:])
    return dest


def inbox(tmp_path: Path, name: str, key: str | None = None) -> Path:
    folder = tmp_path / "vedit-sessions" / f"source-{key or name.replace('.', '-')}" / "inbox"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / name


def frames_in(path: Path) -> int:
    out = subprocess.run([resolve_ffprobe(), "-v", "error", "-select_streams", "v:0", "-count_frames", "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True)
    return int(out.stdout.strip().strip(","))


def rgb_at(path: Path, seconds: float, x: int, y: int) -> tuple[int, int, int]:
    info = probe(path)
    raw = subprocess.run([resolve_ffmpeg(), "-hide_banner", "-loglevel", "error", "-ss", f"{seconds:.3f}", "-i", str(path), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    offset = (y * info.width + x) * 3
    return raw[offset], raw[offset + 1], raw[offset + 2]


def peak_between(path: Path, start: float, end: float) -> int:
    raw = subprocess.run([resolve_ffmpeg(), "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(path), "-map", "0:a:0", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"], capture_output=True, check=True).stdout
    samples = array("h")
    samples.frombytes(raw[: len(raw) // 2 * 2])
    return max(max(samples), -min(samples)) if samples else 0


def built(tmp_path: Path, paths: list[Path], **options) -> tuple[Path, dict]:
    items = [project_media.inspect_media(path) for path in paths]
    dest = tmp_path / "project-source.mp4"
    return dest, project_media.build_project(items, work_dir=tmp_path / "project-source", dest=dest, **options)


@pytest.fixture
def manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[CampaignManager]:
    instance = CampaignManager(tmp_path, start_worker=False)
    try:
        yield instance
    finally:
        instance.close()


def request(paths: list[Path], **overrides) -> dict:
    return {"paths": [str(path) for path in paths], "goal": "Show the whole day", "target_duration": 30, "captions": False, **overrides}


def item(kind: str = "video", width: int = 1280, height: int = 720, **fields) -> MediaItem:
    return MediaItem(Path("/x.mp4"), "x.mp4", kind, width, height, fields.pop("duration", 2.0), **fields)


# ---- Decisions ----


def test_canvas_is_the_first_items_shape_capped_and_even() -> None:
    assert project_media.choose_canvas([item(width=3840, height=2160), item(width=720, height=1280)]) == (1920, 1080)
    assert project_media.choose_canvas([item(width=2160, height=3840)]) == (1080, 1920)
    assert project_media.choose_canvas([item(width=1279, height=719)]) == (1280, 720)
    assert project_media.choose_canvas([item(width=1280, height=720), item(width=3840, height=2160)]) == (1280, 720)


def test_a_thumbnail_opening_the_project_does_not_shrink_it() -> None:
    assert project_media.choose_canvas([item("photo", 200, 200), item(width=1920, height=1080)]) == (1920, 1920)
    assert project_media.choose_canvas([item("photo", 320, 180), item(width=320, height=240)]) == (320, 180)


def test_clip_boundaries_land_on_exact_frames() -> None:
    items = [item(duration=2.32), item("photo", duration=0.0), item(duration=0.01)]
    frames = [project_media.frame_count(entry, 3.0) for entry in items]
    assert frames == [70, 90, 1]
    table = project_media.clip_table(items, frames)
    assert [(clip["kind"], clip["start"], clip["end"]) for clip in table] == [("video", 0.0, 2.333), ("photo", 2.333, 5.333), ("video", 5.333, 5.367)]


@pytest.mark.parametrize("duration,tiles,spacing", [(1.5, 1, 2.0), (10, 5, 2.0), (120, 60, 2.0), (300, 60, 5.0), (1000, 200, 5.0), (7200, 200, 36.0)])
def test_filmstrip_spacing_stays_cheap_on_long_sources(duration: float, tiles: int, spacing: float) -> None:
    layout = project_media.filmstrip_layout(duration, 1920, 1080)
    assert layout["count"] == tiles and layout["seconds_per_tile"] == spacing
    assert layout["columns"] * layout["rows"] >= tiles and layout["tile_height"] == 90 and layout["tile_width"] == 160


def test_filmstrip_tiles_follow_the_picture_shape() -> None:
    assert project_media.filmstrip_layout(20, 1080, 1920)["tile_width"] == 50
    assert project_media.filmstrip_layout(20, 8000, 100)["tile_width"] == 240


def test_a_scene_detected_at_a_join_is_the_join() -> None:
    assert merge_cuts([1.0, 4.03, 7.0], [4.0]) == [1.0, 4.0, 7.0]
    assert merge_cuts([], [2.0, 1.0]) == [1.0, 2.0]
    assert clip_cuts([(0, 2), (2, 5), (5, 6)]) == [2.0, 5.0]
    assert clip_cuts(None) == [] and clip_cuts([(0, 2)]) == []


def test_candidates_stay_inside_clips_and_silent_clips_get_their_own() -> None:
    clips = [(0.0, 4.0), (4.0, 7.0), (7.0, 10.0)]
    pieces = fit_to_clips([Range(1.0, 4.2)], clips)
    assert (pieces[0].start, pieces[0].end) == (1.0, 4.0)
    assert [(piece.start, piece.end) for piece in pieces[1:]] == [(4.0, 7.0), (7.0, 10.0)]


def test_local_plan_covers_every_clip_including_silent_photos() -> None:
    plan = build_plan(goal="A short recap", duration=63.0, target_duration=15, scenes=[60.0], silences=[Range(60.0, 63.0)], transcript=[], has_audio=True, clips=[(0.0, 60.0), (60.0, 63.0)])
    assert any(segment["start"] >= 60.0 for segment in plan["segments"])
    assert all(segment["end"] <= 60.0 or segment["start"] >= 60.0 for segment in plan["segments"])
    assert any("clip 2 of 2" in segment["reason"] for segment in plan["segments"])


def test_one_clip_plans_exactly_as_it_did() -> None:
    silences = [Range(4.0, 6.0)]
    plain = build_plan(goal="Recap", duration=20.0, target_duration=8, scenes=[10.0], silences=silences, transcript=[], has_audio=True)
    assert build_plan(goal="Recap", duration=20.0, target_duration=8, scenes=[10.0], silences=silences, transcript=[], has_audio=True, clips=[(0.0, 20.0)]) == plain


def test_project_source_op_refuses_what_would_reach_a_filter_graph(tmp_path: Path) -> None:
    source = tmp_path / "a.mp4"
    source.write_bytes(b"x")

    def compiled(**fields) -> None:
        spec = {"source": "a", "kind": "video", "frames": 3, "width": 8, "height": 8, **fields}
        compile_brief(Brief(name="p", sources={"a": str(source), "b": str(source)}, ops=[{"op": "project_source", "path": str(tmp_path / "o.mp4"), "items": [spec]}], canvas=Canvas(width=8, height=8)), work_dir=tmp_path / "w")

    compiled()
    for bad in ({"kind": "audio"}, {"frames": "3;evil"}, {"frames": True}, {"width": 0}, {"input": "0:v:0]evil[x"}, {"sound": "1:a:0;"}, {"matrix": "bt709:evil"}):
        with pytest.raises(CompileError):
            compiled(**bad)
    with pytest.raises(CompileError):
        compile_brief(Brief(name="p", sources={"a": str(source), "b": str(source)}, ops=[{"op": "project_source", "path": str(tmp_path / "o.mp4"), "items": []}]), work_dir=tmp_path / "w2")


def test_safe_filenames_keep_their_extension() -> None:
    assert _safe_filename("写真.JPG") == "media.JPG"
    assert _safe_filename("../my clip.mov") == "my_clip.mov"
    assert _safe_filename("...") == "clip.mp4"
    assert _safe_filename("x" * 400 + ".mp3").endswith(".mp3")


def test_limits_are_published() -> None:
    limits = capabilities()["limits"]
    assert limits["upload_bytes"] == 4_000_000_000 and limits["source_seconds"] == 7200
    assert limits["clips"] == 30 and limits["photo_seconds"] == 3.0


# ---- Reading files ----


@needs_ffmpeg
def test_files_are_classified_from_their_contents(tmp_path: Path) -> None:
    clip = make_clip(tmp_path / "clip.mp4")
    photo = make_photo(tmp_path / "still.png", size="64x48")
    jpeg = make_photo(tmp_path / "still.jpg", size="64x48")
    tone = make_tone(tmp_path / "tone.wav")
    assert project_media.inspect_media(clip).kind == "video"
    assert (project_media.inspect_media(photo).kind, project_media.inspect_media(photo).width) == ("photo", 64)
    assert project_media.inspect_media(jpeg).kind == "photo"
    assert project_media.inspect_media(tone).kind == "audio"
    renamed = tmp_path / "actually-a-photo.mp3"
    renamed.write_bytes(photo.read_bytes())
    assert project_media.inspect_media(renamed).kind == "photo"
    notes = tmp_path / "notes.mp3"
    notes.write_text("Not sound at all.\n", encoding="utf-8")
    with pytest.raises(MediaProblem, match="notes.mp3 is not a video, photo, or audio file"):
        project_media.inspect_media(notes)


@needs_ffmpeg
def test_music_with_cover_art_is_audio(tmp_path: Path) -> None:
    cover = make_photo(tmp_path / "cover.png", size="64x64")
    music = tmp_path / "song.m4a"
    ffmpeg("-f", "lavfi", "-i", "sine=f=220:d=2", "-i", str(cover), "-map", "0:a", "-map", "1:v", "-c:a", "aac", "-c:v", "png", "-disposition:v:0", "attached_pic", str(music))
    entry = project_media.inspect_media(music)
    assert entry.kind == "audio" and entry.duration == pytest.approx(2.0, abs=0.1)


@needs_ffmpeg
@pytest.mark.parametrize("orientation,expected", [(1, (160, 90)), (3, (160, 90)), (6, (90, 160)), (8, (90, 160))])
def test_exif_orientation_sets_the_real_shape(tmp_path: Path, orientation: int, expected: tuple[int, int]) -> None:
    photo = make_photo(tmp_path / "base.jpg", size="160x90")
    entry = project_media.inspect_media(with_exif_orientation(photo, tmp_path / "turned.jpg", orientation))
    assert (entry.width, entry.height) == expected


@needs_ffmpeg
def test_phone_rotation_sets_the_real_shape(tmp_path: Path) -> None:
    flat = make_clip(tmp_path / "flat.mp4", size="320x180")
    turned = tmp_path / "turned.mp4"
    ffmpeg("-display_rotation", "90", "-i", str(flat), "-c", "copy", str(turned))
    entry = project_media.inspect_media(turned)
    assert (entry.kind, entry.width, entry.height) == ("video", 180, 320)


@needs_ffmpeg
@pytest.mark.skipif(sys.platform != "darwin" or shutil.which("sips") is None, reason="sips writes HEIC on macOS")
def test_tiled_phone_photo_reads_as_one_picture(tmp_path: Path) -> None:
    jpeg = make_photo(tmp_path / "wide.jpg", size="1600x1200", color="orange")
    turned = with_exif_orientation(jpeg, tmp_path / "turned.jpg", 6)
    heic = tmp_path / "phone.heic"
    make_heic_with_sips(turned, heic)
    entry = project_media.inspect_media(heic)
    assert (entry.kind, entry.width, entry.height, entry.label) == ("photo", 1200, 1600, "0:g:0")
    dest, result = built(tmp_path, [heic, make_clip(tmp_path / "next.mp4", size="320x240")])
    assert (result["width"], result["height"]) == (1200, 1600)
    red, green, blue = rgb_at(dest, 1.0, 600, 800)
    assert red > 200 and blue < 80, "the middle of the frame must be the photo, not one tile"


# ---- The project source ----


@needs_ffmpeg
@render
def test_mixed_items_share_one_canvas_clock_and_sound(tmp_path: Path) -> None:
    first = make_clip(tmp_path / "wide.mp4", size="640x360", seconds=1.2, rate=25, sample_rate=44100, channels=1, color="teal")
    tall = make_clip(tmp_path / "tall.mp4", size="360x640", seconds=1.0, rate=60, tone=None, color="yellow")
    photo = make_photo(tmp_path / "still.png", size="400x300", color="magenta")
    last = make_clip(tmp_path / "last.mp4", size="640x360", seconds=0.5, tone=880, color="navy")
    dest, result = built(tmp_path, [first, tall, photo, last], photo_seconds=1.0)

    info = probe(dest)
    assert (info.width, info.height) == (640, 360) and (result["width"], result["height"]) == (640, 360)
    assert info.fps == pytest.approx(25, abs=0.01)  # the first clip's rate: 25 fps footage keeps its motion
    assert (info.audio.sample_rate, info.audio.channels, info.audio.codec_name) == (48000, 2, "aac")
    assert info.video.codec_name == "h264" and info.video.pix_fmt == "yuv420p"
    assert (info.video.color_space, info.video.color_range) == ("bt709", "tv")
    clips = result["clips"]
    assert [clip["kind"] for clip in clips] == ["video", "video", "photo", "video"]
    assert [clip["name"] for clip in clips] == ["wide.mp4", "tall.mp4", "still.png", "last.mp4"]
    assert frames_in(dest) == round(clips[-1]["end"] * 25)
    assert info.duration == pytest.approx(clips[-1]["end"], abs=0.02)
    decoded = subprocess.run([resolve_ffmpeg(), "-hide_banner", "-v", "error", "-i", str(dest), "-f", "null", "-"], capture_output=True, text=True)
    assert decoded.returncode == 0 and decoded.stderr == "", "joined clips must decode cleanly end to end"

    # Sound stays with its clip: a tone, then real silence, then the other tone.
    assert peak_between(dest, 0.2, 0.9) > 1000
    assert peak_between(dest, clips[1]["start"] + 0.1, clips[2]["end"] - 0.1) < 30
    assert peak_between(dest, clips[3]["start"] + 0.1, clips[3]["end"] - 0.1) > 1000

    # A mismatched clip sits in the middle, over a blurred copy of itself, never black bars.
    middle = (clips[1]["start"] + clips[1]["end"]) / 2
    assert sum(rgb_at(dest, middle, 8, 180)) > 150, "the side bar must be a blurred copy, not black"
    red, green, blue = rgb_at(dest, middle, 320, 180)
    assert red > 200 and green > 200 and blue < 80
    assert sum(rgb_at(dest, (clips[2]["start"] + clips[2]["end"]) / 2, 8, 180)) > 100


@needs_ffmpeg
@render
def test_the_program_is_inspectable_and_one_step_per_item(tmp_path: Path) -> None:
    clip = make_clip(tmp_path / "a.mp4")
    photo = make_photo(tmp_path / "b.png")
    dest, _ = built(tmp_path, [clip, photo])
    work = tmp_path / "project-source"
    project = json.loads((work / "project.json").read_text(encoding="utf-8"))
    assert [step["name"] for step in project["steps"]] == ["normalize_01", "normalize_02", "join_clips"]
    assert "op: project_source" in (work / "brief.yaml").read_text(encoding="utf-8")
    assert (work / "render.sh").read_text(encoding="utf-8").startswith("#!/usr/bin/env bash")
    report = json.loads((work / "report.json").read_text(encoding="utf-8"))
    assert report["outputs"][0]["path"] == str(dest) and report["outputs"][0]["has_audio"] is True
    assert not list(work.glob("clip-*.mov")), "finished clips are removed once the join is verified"


@needs_ffmpeg
@render
def test_exif_photo_opens_a_portrait_project_right_way_up(tmp_path: Path) -> None:
    base = tmp_path / "base.jpg"
    ffmpeg("-f", "lavfi", "-i", "color=white:s=320x180,drawbox=x=0:y=0:w=80:h=180:color=red:t=fill", "-frames:v", "1", "-update", "1", "-q:v", "2", str(base))
    turned = with_exif_orientation(base, tmp_path / "turned.jpg", 6)
    dest, result = built(tmp_path, [turned], photo_seconds=0.5)
    assert (result["width"], result["height"]) == (180, 320)
    assert frames_in(dest) == 15
    # Orientation 6 turns the left edge of the sensor to the top of the picture.
    red, green, blue = rgb_at(dest, 0.1, 90, 20)
    assert red > 180 and green < 90 and blue < 90
    assert min(rgb_at(dest, 0.1, 90, 300)) > 200


@needs_ffmpeg
@render
@pytest.mark.parametrize("name", ["red.jpg", "red.png", "red.webp"])
def test_photo_colors_survive_the_trip_to_video(tmp_path: Path, name: str) -> None:
    # JPEG is full range 601 and PNG is RGB. A shortcut here tints every photo.
    photo = make_photo(tmp_path / name, size="160x90", color="#d02030")
    dest, _ = built(tmp_path, [photo, make_clip(tmp_path / "b.mp4", size="160x90")], photo_seconds=0.5)
    red, green, blue = rgb_at(dest, 0.2, 80, 45)
    assert abs(red - 0xd0) <= 12 and abs(green - 0x20) <= 12 and abs(blue - 0x30) <= 12


@needs_ffmpeg
@render
def test_unlabelled_hd_video_matches_labelled_hd_video(tmp_path: Path) -> None:
    source = ("-f", "lavfi", "-i", "color=#d02030:s=1280x720:r=30:d=0.5")
    plain = tmp_path / "plain.mp4"
    ffmpeg(*source, "-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p,setparams=colorspace=unknown", "-c:v", "libx264", "-preset", "ultrafast", str(plain))
    entry = project_media.inspect_media(plain)
    assert entry.matrix == "bt709"
    dest = tmp_path / "out.mp4"
    project_media.build_project([entry], work_dir=tmp_path / "work", dest=dest)
    red, green, blue = rgb_at(dest, 0.2, 640, 360)
    assert abs(red - 0xd0) <= 12 and abs(green - 0x20) <= 12 and abs(blue - 0x30) <= 12


@needs_ffmpeg
@render
def test_hdr_phone_footage_is_tone_mapped_to_the_project(tmp_path: Path) -> None:
    from vedit.media import has_filter

    if not (has_filter("zscale") and has_filter("tonemap")):
        pytest.skip("needs zscale and tonemap")
    hdr = tmp_path / "hlg.mp4"
    ffmpeg("-f", "lavfi", "-i", "gradients=s=320x180:r=30:d=0.5", "-vf", "format=yuv420p10le,setparams=range=tv:color_primaries=bt2020:color_trc=arib-std-b67:colorspace=bt2020nc",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p10le", str(hdr))
    entry = project_media.inspect_media(hdr)
    assert entry.hdr is True
    dest = tmp_path / "out.mp4"
    project_media.build_project([entry], work_dir=tmp_path / "work", dest=dest)
    info = probe(dest)
    assert (info.video.pix_fmt, info.video.color_space, info.video.color_trc, info.video.color_primaries) == ("yuv420p", "bt709", "bt709", "bt709")
    assert frames_in(dest) == 15


@needs_ffmpeg
@render
def test_a_rotated_clip_with_long_sound_keeps_its_picture_length(tmp_path: Path) -> None:
    flat = tmp_path / "flat.mp4"
    ffmpeg("-f", "lavfi", "-i", "color=green:s=320x180:r=30:d=1", "-f", "lavfi", "-i", "sine=f=440:d=1.6", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(flat))
    turned = tmp_path / "turned.mp4"
    ffmpeg("-display_rotation", "90", "-i", str(flat), "-c", "copy", str(turned))
    dest, result = built(tmp_path, [make_clip(tmp_path / "wide.mp4", size="320x180", seconds=0.5), turned])
    assert result["clips"][1]["end"] - result["clips"][1]["start"] == pytest.approx(1.0, abs=0.05)
    assert frames_in(dest) == round(result["clips"][1]["end"] * 30)


@needs_ffmpeg
@render
def test_a_failed_item_is_named_and_nothing_half_done_is_trusted(tmp_path: Path) -> None:
    good = make_clip(tmp_path / "good.mp4")
    entries = [project_media.inspect_media(good), MediaItem(tmp_path / "missing.mp4", "missing.mp4", "video", 320, 180, 1.0)]
    (tmp_path / "missing.mp4").write_bytes(b"not video")
    with pytest.raises(MediaProblem, match="missing.mp4: This file is incomplete"):
        project_media.build_project(entries, work_dir=tmp_path / "work", dest=tmp_path / "out.mp4")
    assert not (tmp_path / "out.mp4").exists()


@needs_ffmpeg
@render
def test_a_rebuild_keeps_finished_clips_and_redoes_a_truncated_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clip = make_clip(tmp_path / "a.mp4")
    photo = make_photo(tmp_path / "b.png")
    entries = [project_media.inspect_media(path) for path in (clip, photo)]
    work, dest = tmp_path / "work", tmp_path / "out.mp4"
    calls: list[str] = []
    running = project_media.execute
    stops = [True]

    def stop_at_join(piece, **options):
        calls.append(piece.steps[0].name)
        if piece.steps[0].name == "join_clips" and stops.pop() if stops else False:
            raise project_media.RenderError("disk went away")
        return running(piece, **options)

    monkeypatch.setattr(project_media, "execute", stop_at_join)
    with pytest.raises(MediaProblem):
        project_media.build_project(entries, work_dir=work, dest=dest)
    assert calls == ["normalize_01", "normalize_02", "join_clips"]
    # A crash leaves a truncated file. It cannot be probed, so it is rebuilt.
    (work / "clip-02.mov").write_bytes((work / "clip-02.mov").read_bytes()[:64])
    calls.clear()
    result = project_media.build_project(entries, work_dir=work, dest=dest)
    assert calls == ["normalize_02", "join_clips"]
    assert frames_in(dest) == round(result["clips"][-1]["end"] * 30)
    calls.clear()
    project_media.build_project(entries, work_dir=work, dest=dest)
    assert calls == [], "a finished project is reused"


# ---- Filmstrip and waveform ----


@needs_ffmpeg
def test_filmstrip_sprite_matches_its_layout(tmp_path: Path) -> None:
    clip = make_clip(tmp_path / "a.mp4", size="320x180", seconds=7)
    sprite = tmp_path / "100% strip" / "filmstrip.jpg"
    sprite.parent.mkdir()
    layout = project_media.make_filmstrip(clip, sprite, duration=7.0, width=320, height=180)
    assert layout["count"] == 4 and layout["columns"] == 4 and layout["rows"] == 1
    info = probe(sprite)
    assert (info.video.width, info.video.height) == (layout["columns"] * layout["tile_width"], layout["rows"] * layout["tile_height"])
    assert [path.name for path in sprite.parent.iterdir()] == ["filmstrip.jpg"]


@needs_ffmpeg
def test_waveform_peaks_are_bounded_and_measure_the_sound(tmp_path: Path) -> None:
    quiet_then_loud = tmp_path / "wave.wav"
    ffmpeg("-f", "lavfi", "-i", "sine=f=500:r=8000:d=2", "-af", "volume=enable='lt(t,1)':volume=0", str(quiet_then_loud))
    result = project_media.make_waveform(quiet_then_loud, tmp_path / "waveform.json", duration=2.0)
    assert len(result["peaks"]) == 20 and result["seconds_per_peak"] == pytest.approx(0.1)
    assert max(result["peaks"][:9]) < 0.01 and min(result["peaks"][11:]) > 0.05
    assert all(0 <= peak <= 1 for peak in result["peaks"])
    assert json.loads((tmp_path / "waveform.json").read_text(encoding="utf-8")) == result
    assert not (tmp_path / "waveform.pcm").exists()
    assert project_media.make_waveform(quiet_then_loud, tmp_path / "none.json", duration=2.0, has_audio=False) == {"peaks": [], "seconds_per_peak": 0}


def test_long_sources_are_capped_at_four_thousand_peaks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_decode(argv, **_options):
        Path(argv[-1]).write_bytes(array("h", [1000] * 7200 * project_media.PEAK_SAMPLE_RATE).tobytes())
        return types.SimpleNamespace(ok=True)

    monkeypatch.setattr(project_media, "run_command", fake_decode)
    monkeypatch.setattr(project_media, "resolve_ffmpeg", lambda: "ffmpeg")
    result = project_media.make_waveform(tmp_path / "long.mp4", tmp_path / "waveform.json", duration=7200.0)
    assert len(result["peaks"]) == project_media.MAX_PEAKS and result["seconds_per_peak"] == pytest.approx(1.8)


# ---- Projects ----


@needs_ffmpeg
def test_one_video_stays_its_own_source(manager: CampaignManager, tmp_path: Path) -> None:
    clip = make_clip(inbox(tmp_path, "solo.mp4"), seconds=2)
    original = clip.read_bytes()
    job = manager.create(request([clip]))
    manager.process(job["id"])
    planned = manager.get(job["id"])
    assert planned["status"] == "planned", planned["error"]
    assert planned["request"]["paths"] == [str(clip)] and "photo_seconds" not in planned["request"]
    assert manager.artifact(job["id"], "source") == clip
    assert not (manager.root / job["id"] / "project-source.mp4").exists()
    assert not (manager.root / job["id"] / "project-source").exists()
    source = planned["source"]
    assert source["name"] == "solo.mp4" and source["clips"] == [{"name": "solo.mp4", "kind": "video", "start": 0.0, "end": round(source["duration"], 3)}]
    assert source["filmstrip"]["url"].endswith("/filmstrip") and source["waveform"]["count"] > 0
    assert clip.read_bytes() == original


@needs_ffmpeg
@render
def test_single_portrait_clip_uses_blurred_fit_for_landscape(manager: CampaignManager, tmp_path: Path) -> None:
    clip = inbox(tmp_path, "portrait.mp4")
    ffmpeg("-f", "lavfi", "-i", "color=blue:s=160x320:r=30:d=1", "-vf",
           "drawbox=x=60:y=0:w=40:h=320:color=red:t=fill", "-an", "-c:v", "libx264", "-preset", "ultrafast", str(clip))
    job = manager.create(request([clip], aspect="16:9", target_duration=1))
    manager.process(job["id"])
    planned = manager.get(job["id"])
    assert planned["status"] == "planned", planned["error"]
    assert (planned["source"]["width"], planned["source"]["height"]) == (1920, 1080)
    project = manager.artifact(job["id"], "source")
    center = rgb_at(project, 0.2, 960, 540)
    beside = rgb_at(project, 0.2, 800, 540)
    assert center[0] > center[2] + 80
    assert beside[2] > beside[0] + 40


@needs_ffmpeg
@render
def test_clips_and_photos_plan_and_render_as_one_project(manager: CampaignManager, tmp_path: Path) -> None:
    first = make_clip(inbox(tmp_path, "one.mp4"), size="320x180", seconds=2, color="teal")
    photo = make_photo(inbox(tmp_path, "two.png"), size="200x200", color="orange")
    last = make_clip(inbox(tmp_path, "three.mp4"), size="180x320", seconds=2, tone=None, color="purple")
    job = manager.create(request([first, photo, last], target_duration=3, aspect="16:9", photo_seconds=1.5))
    assert job["request"]["photo_seconds"] == 1.5
    manager.process(job["id"])
    planned = manager.get(job["id"])
    assert planned["status"] == "planned", planned["error"]
    source = planned["source"]
    assert [clip["name"] for clip in source["clips"]] == ["one.mp4", "two.png", "three.mp4"]
    assert source["clips"][1] == {"name": "two.png", "kind": "photo", "start": 2.0, "end": 3.5}
    assert source["name"] == "one.mp4 and 2 more" and (source["width"], source["height"]) == (320, 180)
    assert source["has_audio"] is True and source["duration"] == pytest.approx(5.5, abs=0.02)
    project = manager.artifact(job["id"], "source")
    assert project.name == "project-source.mp4" and project.parent.name == job["id"]
    scenes = planned["plan"]["analysis"]["scenes"]
    assert 2.0 in scenes and 3.5 in scenes
    starts = [segment["start"] for segment in planned["plan"]["segments"]]
    assert any(start < 2 for start in starts) and any(2 <= start < 3.5 for start in starts) and any(start >= 3.5 for start in starts)

    manager.render(job["id"], {"plan": planned["plan"]})
    manager.process(job["id"])
    done = manager.get(job["id"])
    assert done["status"] == "complete", done["error"]
    video = probe(manager.artifact(job["id"], "video"))
    assert (video.width, video.height) == (1920, 1080) and video.has_audio
    assert video.duration == pytest.approx(planned["plan"]["duration"], abs=0.35)


@needs_ffmpeg
@render
def test_one_photo_is_a_project(manager: CampaignManager, tmp_path: Path) -> None:
    photo = make_photo(inbox(tmp_path, "only.jpg"), size="320x240")
    job = manager.create(request([photo]))
    manager.process(job["id"])
    planned = manager.get(job["id"])
    assert planned["status"] == "planned", planned["error"]
    assert planned["source"]["clips"] == [{"name": "only.jpg", "kind": "photo", "start": 0.0, "end": 3.0}]
    assert planned["source"]["has_audio"] is False
    assert manager.artifact(job["id"], "source").name == "project-source.mp4"


@needs_ffmpeg
@render
def test_thirty_items_is_the_bound(manager: CampaignManager, tmp_path: Path) -> None:
    photos = [make_photo(inbox(tmp_path, f"p{index:02d}.png"), size="64x36", color=("red" if index % 2 else "blue")) for index in range(31)]
    with pytest.raises(CampaignError, match="between 1 and 30"):
        manager.create(request(photos))
    with pytest.raises(CampaignError):
        manager.create(request([]))
    job = manager.create(request(photos[:30], photo_seconds=0.5, target_duration=5))
    manager.process(job["id"])
    planned = manager.get(job["id"])
    assert planned["status"] == "planned", planned["error"]
    assert len(planned["source"]["clips"]) == 30 and planned["source"]["clips"][-1]["end"] == 15.0
    assert frames_in(manager.artifact(job["id"], "source")) == 450


def test_every_path_follows_the_inbox_rules(manager: CampaignManager, tmp_path: Path) -> None:
    good = inbox(tmp_path, "good.mp4")
    good.write_bytes(b"x")
    outside = tmp_path / "private.mp4"
    outside.write_bytes(b"x")
    with pytest.raises(CampaignError, match="uploaded through Studio"):
        manager.create(request([good, outside]))
    assert manager.list() == []


@pytest.mark.parametrize("value", [0.1, 61, "3", True])
def test_photo_length_is_bounded(manager: CampaignManager, tmp_path: Path, value) -> None:
    good = inbox(tmp_path, "good.png")
    good.write_bytes(b"x")
    with pytest.raises(CampaignError, match="Photo length"):
        manager.create(request([good], photo_seconds=value))


@needs_ffmpeg
def test_a_bad_item_fails_the_job_by_name(manager: CampaignManager, tmp_path: Path) -> None:
    good = make_clip(inbox(tmp_path, "good.mp4"))
    bad = inbox(tmp_path, "broken.mp4")
    bad.write_text("this is not footage", encoding="utf-8")
    job = manager.create(request([good, bad]))
    manager.process(job["id"])
    failed = manager.get(job["id"])
    assert failed["status"] == "failed" and "broken.mp4" in failed["error"]


@needs_ffmpeg
def test_audio_cannot_be_a_project_item(manager: CampaignManager, tmp_path: Path) -> None:
    good = make_clip(inbox(tmp_path, "good.mp4"))
    tone = make_tone(inbox(tmp_path, "tone.wav"))
    job = manager.create(request([good, tone]))
    manager.process(job["id"])
    failed = manager.get(job["id"])
    assert failed["status"] == "failed" and "tone.wav has sound but no picture" in failed["error"]


@needs_ffmpeg
def test_a_project_over_two_hours_stops_before_any_encode(manager: CampaignManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vedit.campaign._MAX_SOURCE_SECONDS", 2)
    first = make_clip(inbox(tmp_path, "a.mp4"), seconds=1.5)
    second = make_clip(inbox(tmp_path, "b.mp4"), seconds=1.5)
    job = manager.create(request([first, second]))
    manager.process(job["id"])
    failed = manager.get(job["id"])
    assert failed["status"] == "failed" and "A project holds up to" in failed["error"]
    assert not (manager.root / job["id"] / "project-source").exists()


@needs_ffmpeg
def test_low_disk_stops_a_project_before_any_encode(manager: CampaignManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = make_clip(inbox(tmp_path, "a.mp4"))
    second = make_clip(inbox(tmp_path, "b.mp4"))
    monkeypatch.setattr("vedit.campaign.shutil.disk_usage", lambda _path: types.SimpleNamespace(free=1024))
    job = manager.create(request([first, second]))
    manager.process(job["id"])
    failed = manager.get(job["id"])
    assert failed["status"] == "failed" and "Not enough free disk space for project" in failed["error"]
    assert not (manager.root / job["id"] / "project-source").exists()


@needs_ffmpeg
@render
def test_cancelling_stops_between_clips_and_retry_finishes(manager: CampaignManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = make_clip(inbox(tmp_path, "a.mp4"))
    second = make_clip(inbox(tmp_path, "b.mp4"))
    job = manager.create(request([first, second]))
    stages: list[tuple[str, str]] = []
    update = manager._update

    def watch(ident, **changes):
        if changes.get("stage") == "combining":
            stages.append((changes["stage"], changes["message"]))
            if len(stages) == 2:
                manager.cancel(ident)
        return update(ident, **changes)

    monkeypatch.setattr(manager, "_update", watch)
    manager.process(job["id"])
    assert manager.get(job["id"])["status"] == "cancelled"
    assert stages[0] == ("combining", "Preparing a.mp4 (1 of 2).")
    assert not (manager.root / job["id"] / "project-source.mp4").exists()
    monkeypatch.setattr(manager, "_update", update)
    manager.retry(job["id"])
    manager.process(job["id"])
    assert manager.get(job["id"])["status"] == "planned"


@needs_ffmpeg
@render
def test_a_finished_project_outlives_the_inbox(manager: CampaignManager, tmp_path: Path) -> None:
    first = make_clip(inbox(tmp_path, "a.mp4"))
    second = make_photo(inbox(tmp_path, "b.png"))
    job = manager.create(request([first, second], target_duration=1))
    manager.process(job["id"])
    planned = manager.get(job["id"])
    manager.cancel(job["id"])
    first.unlink()
    second.unlink()
    assert manager.artifact(job["id"], "source").is_file()
    assert manager.get(job["id"])["status"] == "cancelled"
    assert manager.retry(job["id"])["status"] == "queued"
    manager.process(job["id"])
    assert manager.get(job["id"])["source"]["clips"] == planned["source"]["clips"]


@needs_ffmpeg
@render
def test_shorts_share_their_parents_project_file(manager: CampaignManager, tmp_path: Path) -> None:
    first = make_clip(inbox(tmp_path, "a.mp4"), seconds=4)
    second = make_clip(inbox(tmp_path, "b.mp4"), seconds=4, color="navy")
    job = manager.create(request([first, second], target_duration=6))
    manager.process(job["id"])
    children = manager.shorts(job["id"], {"count": 2, "auto_render": False})
    manager.process(children[0]["id"])
    child = manager.get(children[0]["id"])
    assert child["status"] == "planned", child["error"]
    assert not (manager.root / child["id"] / "project-source").exists(), "nothing was encoded twice"
    assert manager.artifact(child["id"], "source").stat().st_ino == manager.artifact(job["id"], "source").stat().st_ino
    assert child["source"]["clips"] == manager.get(job["id"])["source"]["clips"]


def test_retry_needs_every_item_until_the_project_exists(manager: CampaignManager, tmp_path: Path) -> None:
    first = inbox(tmp_path, "a.mp4")
    second = inbox(tmp_path, "b.mp4")
    first.write_bytes(b"x")
    second.write_bytes(b"x")
    job = manager.create(request([first, second]))
    manager.cancel(job["id"])
    second.unlink()
    with pytest.raises(CampaignError):
        manager.retry(job["id"])


@needs_ffmpeg
@render
def test_filmstrip_failure_is_a_warning_not_a_failed_job(manager: CampaignManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_args, **_kwargs):
        raise OSError("disk trouble")

    monkeypatch.setattr(project_media, "make_filmstrip", broken)
    monkeypatch.setattr(project_media, "make_waveform", broken)
    clip = make_clip(inbox(tmp_path, "a.mp4"), seconds=2)
    job = manager.create(request([clip]))
    manager.process(job["id"])
    planned = manager.get(job["id"])
    assert planned["status"] == "planned", planned["error"]
    assert "filmstrip" not in planned["source"] and "waveform" not in planned["source"]
    assert any("picture strip" in warning for warning in planned["plan"]["warnings"])
    assert any("sound wave" in warning for warning in planned["plan"]["warnings"])
    for call in (manager.filmstrip, manager.waveform):
        with pytest.raises(CampaignError) as error:
            call(job["id"])
        assert error.value.status == 404


# ---- Assets ----


@needs_ffmpeg
def test_assets_register_probe_and_outlive_the_inbox(manager: CampaignManager, tmp_path: Path) -> None:
    clip = make_clip(inbox(tmp_path, "a.mp4"))
    job = manager.create(request([clip]))
    image = make_photo(inbox(tmp_path, "logo.png"), size="128x64")
    tone = make_tone(inbox(tmp_path, "song.wav"), seconds=1.5)
    logo = manager.add_asset(job["id"], {"path": str(image)})
    music = manager.add_asset(job["id"], {"path": str(tone)})
    assert ASSET_ID.fullmatch(logo["id"]) and ASSET_ID.fullmatch(music["id"]) and logo["id"] != music["id"]
    assert {key: logo[key] for key in ("kind", "name", "duration", "width", "height")} == {"kind": "image", "name": "logo.png", "duration": None, "width": 128, "height": 64}
    assert music["kind"] == "audio" and music["duration"] == pytest.approx(1.5, abs=0.05) and music["width"] is None
    shutil.rmtree(image.parent.parent)
    shutil.rmtree(tone.parent.parent)
    public = manager.get(job["id"])
    assert set(public["assets"]) == {logo["id"], music["id"]}
    assert all("path" not in record for record in public["assets"].values()), "clients see ids, never paths"
    with manager._connect() as connection:
        stored = manager._load(connection, job["id"])
    logo_file = manager.asset_path(stored, logo["id"])
    assert logo_file.is_file() and logo_file.suffix == ".png" and logo_file.parent == manager.root / job["id"] / "assets"
    assert stored["assets"][logo["id"]]["path"] == str(logo_file), "render_plan reads the stored path"
    assert probe(manager.asset_path(stored, music["id"])).has_audio
    # The edit validator accepts what the route registered, by kind.
    edit = validate_edit({"logo": {"asset": logo["id"]}, "music": {"asset": music["id"]}}, duration=5, assets=public["assets"])
    assert edit["logo"]["asset"] == logo["id"] and edit["music"]["asset"] == music["id"]
    with pytest.raises(CampaignError) as error:
        manager.asset_path(stored, "ffffffff")
    assert error.value.status == 404


@needs_ffmpeg
def test_asset_paths_cannot_leave_the_job(manager: CampaignManager, tmp_path: Path) -> None:
    job = manager.create(request([make_clip(inbox(tmp_path, "a.mp4"))]))
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"x")
    with pytest.raises(CampaignError):
        manager.asset_path({"id": job["id"], "assets": {"abcdabcd": {"path": str(secret)}}}, "abcdabcd")


@needs_ffmpeg
@pytest.mark.parametrize("name,body", [("notes.mp3", b"Plain text, not sound.\n"), ("empty-ish.png", b"\x89PNG\r\n\x1a\nnot really"), ("page.wav", b"<html>nope</html>")])
def test_assets_reject_what_is_not_media(manager: CampaignManager, tmp_path: Path, name: str, body: bytes) -> None:
    job = manager.create(request([make_clip(inbox(tmp_path, "a.mp4"))]))
    fake = inbox(tmp_path, name)
    fake.write_bytes(body)
    with pytest.raises(CampaignError) as error:
        manager.add_asset(job["id"], {"path": str(fake)})
    assert error.value.status == 400 and name in str(error.value)
    assert not (manager.root / job["id"] / "assets").exists()
    assert not manager.get(job["id"]).get("assets")


@needs_ffmpeg
def test_assets_reject_video_paths_outside_and_full_projects(manager: CampaignManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clip = make_clip(inbox(tmp_path, "a.mp4"))
    job = manager.create(request([clip]))
    with pytest.raises(CampaignError, match="is a video"):
        manager.add_asset(job["id"], {"path": str(clip)})
    outside = make_photo(tmp_path / "outside.png")
    with pytest.raises(CampaignError, match="uploaded through Studio"):
        manager.add_asset(job["id"], {"path": str(outside)})
    with pytest.raises(CampaignError):
        manager.add_asset(job["id"], {"path": 7})
    with pytest.raises(CampaignError) as missing:
        manager.add_asset("0" * 32, {"path": str(make_photo(inbox(tmp_path, "x.png")))})
    assert missing.value.status == 404
    monkeypatch.setattr(project_media, "MAX_ASSETS", 1)
    image = make_photo(inbox(tmp_path, "y.png"))
    manager.add_asset(job["id"], {"path": str(image)})
    with pytest.raises(CampaignError) as full:
        manager.add_asset(job["id"], {"path": str(image)})
    assert full.value.status == 409


@needs_ffmpeg
def test_an_image_name_never_decides_what_it_is(manager: CampaignManager, tmp_path: Path) -> None:
    job = manager.create(request([make_clip(inbox(tmp_path, "a.mp4"))]))
    png = make_photo(tmp_path / "real.png")
    disguised = inbox(tmp_path, "logo.html")
    disguised.write_bytes(png.read_bytes())
    asset = manager.add_asset(job["id"], {"path": str(disguised)})
    with manager._connect() as connection:
        stored = manager._load(connection, job["id"])
    assert asset["kind"] == "image" and manager.asset_path(stored, asset["id"]).suffix == ".png"


@needs_ffmpeg
@pytest.mark.skipif(sys.platform != "darwin" or shutil.which("sips") is None, reason="sips writes HEIC on macOS")
def test_a_phone_photo_logo_is_stored_as_a_still_ffmpeg_can_hold(manager: CampaignManager, tmp_path: Path) -> None:
    job = manager.create(request([make_clip(inbox(tmp_path, "a.mp4"))]))
    jpeg = make_photo(tmp_path / "wide.jpg", size="1600x1200", color="orange")
    heic = inbox(tmp_path, "logo.heic")
    make_heic_with_sips(jpeg, heic)
    asset = manager.add_asset(job["id"], {"path": str(heic)})
    with manager._connect() as connection:
        stored = manager._load(connection, job["id"])
    path = manager.asset_path(stored, asset["id"])
    assert (asset["kind"], asset["width"], asset["height"]) == ("image", 1600, 1200) and path.suffix == ".png"
    assert (probe(path).video.width, probe(path).video.height) == (1600, 1200)


@needs_ffmpeg
def test_music_carries_beats_when_the_detector_exists(manager: CampaignManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    job = manager.create(request([make_clip(inbox(tmp_path, "a.mp4"))]))
    tone = make_tone(inbox(tmp_path, "song.wav"))
    module = types.ModuleType("vedit.beats")
    module.detect_beats = lambda path: [0.5, 1.0, float("nan"), "x", 1.5]
    monkeypatch.setitem(sys.modules, "vedit.beats", module)
    assert manager.add_asset(job["id"], {"path": str(tone)})["beats"] == [0.5, 1.0, 1.5]
    module.detect_beats = lambda path: {"beats": [2.0]}
    assert manager.add_asset(job["id"], {"path": str(tone)})["beats"] == [2.0]

    def explode(path):
        raise RuntimeError("no beat today")

    module.detect_beats = explode
    assert "beats" not in manager.add_asset(job["id"], {"path": str(tone)})
    monkeypatch.setitem(sys.modules, "vedit.beats", None)
    assert "beats" not in manager.add_asset(job["id"], {"path": str(tone)})


# ---- HTTP ----


def call(port: int, method: str, path: str, payload: dict | bytes | None = None, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
    outgoing = dict(headers or {})
    body = None
    if isinstance(payload, dict):
        body = json.dumps(payload).encode()
        outgoing.setdefault("Content-Type", "application/json")
    elif payload is not None:
        body = payload
        outgoing.setdefault("Content-Type", "application/octet-stream")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request(method, path, body=body, headers=outgoing)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    monkeypatch.chdir(tmp_path)
    httpd = _LoopbackServer(("127.0.0.1", 0), StudioHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(httpd.server_address[1])
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)
        from vedit.campaign import get_manager

        get_manager().close()


def upload(port: int, path: Path, name: str | None = None) -> tuple[int, dict]:
    status, _, raw = call(port, "POST", f"/api/upload?name={name or path.name}", path.read_bytes())
    return status, json.loads(raw)


def wait_for(port: int, ident: str, states: set[str]) -> dict:
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        status, _, raw = call(port, "GET", f"/api/campaigns/{ident}")
        assert status == 200
        job = json.loads(raw)["job"]
        if job["status"] in states:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job stayed {job['status']}")


@needs_ffmpeg
def test_upload_takes_photos_and_audio_and_refuses_impostors(server: int, tmp_path: Path) -> None:
    media = tmp_path / "made"
    photo = make_photo(media / "shot.jpg", size="64x48")
    tone = make_tone(media / "song.mp3")
    clip = make_clip(media / "clip.mp4")
    assert upload(server, photo)[1]["kind"] == "photo"
    assert upload(server, tone)[1]["kind"] == "audio"
    status, body = upload(server, clip)
    assert status == 201 and body["kind"] == "video" and Path(body["paths"][0]).name == "clip.mp4"

    fake = media / "notes.mp3"
    fake.write_text("Plain text.\n", encoding="utf-8")
    before = sorted(path.name for path in (tmp_path / "vedit-sessions").iterdir())
    status, body = upload(server, fake)
    assert status == 415 and "notes.mp3 is not a video, photo, or audio file" in body["error"]
    assert sorted(path.name for path in (tmp_path / "vedit-sessions").iterdir()) == before

    # A file named as video is still judged when a project is created.
    junk = media / "junk.mp4"
    junk.write_bytes(b"\x00\x01\x02" * 100)
    status, body = upload(server, junk)
    assert status == 201 and body["kind"] is None and Path(body["paths"][0]).read_bytes() == junk.read_bytes()


@needs_ffmpeg
def test_uploads_are_refused_before_the_disk_fills(server: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clip = make_clip(tmp_path / "made" / "clip.mp4")
    monkeypatch.setattr("vedit.studio.server.shutil.disk_usage", lambda _path: types.SimpleNamespace(free=1024))
    status, _, raw = call(server, "POST", "/api/upload?name=clip.mp4", clip.read_bytes())
    assert status == 400 and "free disk space" in json.loads(raw)["error"]
    assert not list(tmp_path.glob("vedit-sessions/source-*"))


def test_form_uploads_stay_small_because_they_sit_in_memory() -> None:
    from vedit.studio import server as module

    assert module._MAX_UPLOAD == 4_000_000_000
    assert module._MAX_FORM_UPLOAD == 200_000_000
    assert "_read_body(_MAX_FORM_UPLOAD" in Path(module.__file__).read_text(encoding="utf-8")


@needs_ffmpeg
@render
def test_a_project_over_http_end_to_end(server: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Studio's routes are Meta-only: enable Meta with a fake key and stub the two calls planning makes.
    from vedit import meta

    monkeypatch.setenv("MODEL_API_KEY", "test-secret-never-write-to-disk")
    monkeypatch.setenv("VEDIT_META_ENABLED", "1")
    monkeypatch.setattr(meta, "transcribe_audio", lambda wav, **kwargs: {"cues": [], "provider": "meta", "usage": {"cost_usd": 0}})
    monkeypatch.setattr(meta, "plan_story", lambda *a, **k: {"title": "The day", "summary": "s", "caption": "c", "cover_prompt": "p", "hashtags": [],
                                                           "segments": [{"start": 0.0, "end": 1.5, "label": "Open", "reason": "r"},
                                                                        {"start": 2.2, "end": 3.0, "label": "Still", "reason": "r"}],
                                                           "usage": {}, "warnings": []})
    media = tmp_path / "made"
    paths = []
    for file in (make_clip(media / "open.mp4", size="320x180", seconds=2, color="teal"), make_photo(media / "still.png", size="300x300", color="orange"), make_clip(media / "close.mp4", size="180x320", seconds=2, tone=None, color="purple")):
        status, body = upload(server, file)
        assert status == 201
        paths.append(body["paths"][0])
    status, _, raw = call(server, "POST", "/api/campaigns", {"paths": paths, "goal": "Show the day", "target_duration": 3, "aspect": "16:9", "consent": True})
    assert status == 202
    job = wait_for(server, json.loads(raw)["job"]["id"], {"planned", "failed"})
    assert job["status"] == "planned", job["error"]
    ident = job["id"]
    assert [clip["kind"] for clip in job["source"]["clips"]] == ["video", "photo", "video"]

    status, headers, raw = call(server, "GET", f"/api/campaigns/{ident}/artifact?name=source", headers={"Range": "bytes=0-1023"})
    assert status == 206 and headers["Content-Type"] == "video/mp4" and len(raw) == 1024
    assert headers["Content-Range"].endswith("/" + str((tmp_path / "vedit-sessions" / "campaigns" / ident / "project-source.mp4").stat().st_size))

    strip = job["source"]["filmstrip"]
    assert strip["url"] == f"/api/campaigns/{ident}/filmstrip"
    assert set(strip) == {"url", "columns", "rows", "tile_width", "tile_height", "count", "seconds_per_tile"}
    status, headers, raw = call(server, "GET", strip["url"])
    assert status == 200 and headers["Content-Type"] == "image/jpeg" and raw[:2] == b"\xff\xd8"
    status, headers, raw = call(server, "HEAD", strip["url"])
    assert status == 200 and raw == b""

    status, _, raw = call(server, "GET", f"/api/campaigns/{ident}/waveform")
    wave = json.loads(raw)
    assert status == 200 and wave["ok"] is True and wave["seconds_per_peak"] > 0
    assert len(wave["peaks"]) <= 4000 and max(wave["peaks"]) > 0.05 and all(0 <= peak <= 1 for peak in wave["peaks"])

    status, body = upload(server, make_tone(media / "bed.wav"))
    status, _, raw = call(server, "POST", f"/api/campaigns/{ident}/assets", {"path": body["paths"][0]})
    assert status == 201
    asset = json.loads(raw)["asset"]
    assert asset["kind"] == "audio" and ASSET_ID.fullmatch(asset["id"]) and "path" not in asset

    fake = media / "fake.mp3"
    fake.write_text("text", encoding="utf-8")
    inbox_copy = tmp_path / "vedit-sessions" / "source-fake" / "inbox" / "fake.mp3"
    inbox_copy.parent.mkdir(parents=True)
    inbox_copy.write_bytes(fake.read_bytes())
    status, _, raw = call(server, "POST", f"/api/campaigns/{ident}/assets", {"path": str(inbox_copy)})
    assert status == 400 and "fake.mp3" in json.loads(raw)["error"]

    for route in ("filmstrip", "waveform"):
        status, _, _ = call(server, "POST", f"/api/campaigns/{ident}/{route}", {})
        assert status == 405
    status, _, _ = call(server, "GET", f"/api/campaigns/{ident}/assets")
    assert status == 400  # reading an asset needs its id

    status, _, _ = call(server, "POST", f"/api/campaigns/{ident}/render", {"plan": job["plan"]})
    assert status == 202
    done = wait_for(server, ident, {"complete", "failed"})
    assert done["status"] == "complete", done["error"]
    assert done["result"]["proof"]["has_audio"] is True and done["result"]["proof"]["width"] == 1920


def test_filmstrip_and_waveform_routes_explain_a_missing_project(server: int) -> None:
    for route in ("filmstrip", "waveform"):
        status, _, raw = call(server, "GET", f"/api/campaigns/{'0' * 32}/{route}")
        assert status == 404 and json.loads(raw)["ok"] is False
