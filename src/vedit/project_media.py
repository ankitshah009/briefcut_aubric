"""Many clips and photos, one project clock.

A project starts from up to thirty videos and photos, but planning, captions,
tracking, and the compiler all read a single source. This module turns the
items into that source, so nothing downstream has to know how many there were.

One video stays its own untouched original. Anything else becomes
``project-source.mp4``: every item on one canvas at 30 fps with 48 kHz stereo,
silence where an item has no sound, and a blurred copy of the picture where its
shape differs from the canvas.

The decisions here are pure: what a file is, which canvas, how many frames, and
where each clip sits. Encoding goes through the ``project_source`` compile op
and the pipeline, so the job's ``project-source`` folder holds a brief and a
``render.sh`` that reproduce the file, and cancelling stops the running FFmpeg.

Each item becomes an exact whole number of frames and samples, so clip
boundaries on the project clock are exact and sound cannot drift across joins.
"""

from __future__ import annotations

import json
import math
import re
import secrets
import shutil
import threading
import time
from array import array
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from vedit.errors import CompileError, ProbeError, RenderCancelled, RenderError, VeditError
from vedit.ir import Brief, Canvas, brief_dumps
from vedit.media import MediaInfo, check_playable, has_filter, plain_failure, probe, resolve_ffmpeg, resolve_ffprobe, run_command, source_problem
from vedit.pipeline import Pipeline, StepReport, Step, execute, write_codegen, write_report

MAX_ITEMS = 30
MAX_SOURCE_BYTES = 4_000_000_000
MAX_SOURCE_SECONDS = 7_200
PROJECT_FILE = "project-source.mp4"
PROJECT_FOLDER = "project-source"
PHOTO_SECONDS = 3.0
MIN_PHOTO_SECONDS = 0.5
MAX_PHOTO_SECONDS = 60.0
FPS = 30
SAMPLE_RATE = 48_000
SAMPLES_PER_FRAME = SAMPLE_RATE // FPS
MAX_CANVAS = 1920
SMALL_FIRST_ITEM = 640
ASPECT_TOLERANCE = 0.015
STILL_SECONDS = 1.05
JOIN_TOLERANCE = 0.1
AUDIO_BITRATE = "256k"
MAX_ASSETS = 64
MAX_BEATS = 2000
MAX_TILES = 200
TILE_COLUMNS = 10
TILE_HEIGHT = 90
TILE_WORKERS = 4
TILE_TIMEOUT = 20.0
TILE_BUDGET = 90.0
EXACT_SEEK_SECONDS = 120.0
MAX_PEAKS = 4000
PEAKS_PER_SECOND = 10
PEAK_SAMPLE_RATE = 4000
PEAK_TIMEOUT = 900.0

_HDR_TRANSFERS = frozenset({"smpte2084", "arib-std-b67"})
_KNOWN_MATRICES = frozenset({"bt709", "smpte170m", "bt470bg", "smpte240m", "bt2020nc", "bt2020c", "fcc", "gbr"})
_INPUT_MATRICES = frozenset({"bt709", "bt601"})
_REFERENCE_FORMATS = frozenset({"hls", "dash", "concat", "tty"})
_IMAGE_SUFFIX = {"png": ".png", "mjpeg": ".jpg", "webp": ".webp", "bmp": ".bmp", "tiff": ".tif", "gif": ".gif"}
_SAFE_SUFFIX = re.compile(r"^\.[a-z0-9]{1,6}$")
_STREAM_LABEL = re.compile(r"^0:[vag]:\d{1,3}$")
_TAGS = "setparams=range=limited:color_primaries=bt709:color_trc=bt709:colorspace=bt709"
_BLUR = r"boxblur=luma_radius=min(w\,h)/10:luma_power=2:chroma_radius=min(cw\,ch)/10:chroma_power=2"
_TONEMAP = "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=mobius:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p"
# Stitchable x264 headers let the concat demuxer join finished clips without
# touching a frame; a keyframe every second keeps scrubbing and seeking quick.
_ENCODE = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-g", str(FPS), "-x264-params", "stitchable=1", "-pix_fmt", "yuv420p"]


class MediaProblem(VeditError):
    """A file cannot be used. The message is plain and safe to show."""


@dataclass(frozen=True)
class MediaItem:
    """One uploaded file, in the terms an editor needs."""

    path: Path
    name: str
    kind: str  # video, photo, or audio
    width: int = 0  # as displayed, after rotation
    height: int = 0
    duration: float = 0.0  # 0 for a photo
    has_audio: bool = False
    label: str = "0:v:0"  # FFmpeg's name for the picture
    sound: str = "0:a:0"  # and for the sound
    hdr: bool = False
    matrix: str | None = None  # a guess, only when the file does not say
    codec: str = ""


def even(value: float) -> int:
    return max(2, int(round(value / 2)) * 2)


def _pictures(info: MediaInfo) -> list[Any]:
    """Real picture streams. Cover art on a music file is not a picture of the project."""
    return [stream for stream in info.streams if stream.codec_type == "video" and not stream.cover]


def _image_format(info: MediaInfo) -> bool:
    return info.format_name == "image2" or info.format_name.endswith("_pipe")


def is_still(path: Path, info: MediaInfo) -> bool:
    """One picture, whatever the container claims. HEIC and AVIF read as one-second videos."""
    if info.has_audio or not _pictures(info):
        return False
    if _image_format(info):
        return True
    return info.duration <= STILL_SECONDS and _packet_count(path) <= 1


def _ffprobe_json(path: Path, *options: str) -> dict[str, Any]:
    try:
        result = run_command([resolve_ffprobe(), "-v", "error", *options, "-of", "json", str(path)], timeout=60.0)
        data = json.loads(result.stdout) if result.ok else {}
    except (RenderError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def _sideways(rotation: Any) -> bool:
    return isinstance(rotation, (int, float)) and round(rotation) % 180 == 90


def _still_size(path: Path, info: MediaInfo, streams: list[Any]) -> tuple[int, int, str]:
    """Display size and FFmpeg label of a still.

    Phones store HEIC photos as a grid of tiles, one stream each, which FFmpeg
    joins through a stream group. EXIF orientation lives on the decoded frame
    rather than the stream, so the first frame is what tells the real shape.
    """
    if len(streams) > 1:
        groups = _ffprobe_json(path, "-show_stream_groups").get("stream_groups") or []
        for group in groups:
            components = group.get("components") if isinstance(group, dict) else None
            grid = components[0] if isinstance(components, list) and components else None
            if isinstance(grid, dict) and grid.get("width") and grid.get("height"):
                turned = any(_sideways(item.get("rotation")) for item in grid.get("side_data_list") or [] if isinstance(item, dict))
                width, height = int(grid["width"]), int(grid["height"])
                return (height, width, "0:g:0") if turned else (width, height, "0:g:0")
    picture, position = info.video, info.video_map
    frames = _ffprobe_json(path, "-select_streams", f"v:{position}", "-read_intervals", "%+#1", "-show_entries", "frame=width,height:frame_side_data=rotation").get("frames") or []
    frame = frames[0] if frames and isinstance(frames[0], dict) else {}
    width, height = int(frame.get("width") or picture.width or 0), int(frame.get("height") or picture.height or 0)
    turned = _sideways(picture.rotation) or any(_sideways(item.get("rotation")) for item in frame.get("side_data_list") or [] if isinstance(item, dict))
    return (height, width, f"0:v:{position}") if turned else (width, height, f"0:v:{position}")


def _packet_count(path: Path) -> int:
    """Pictures in a short file. Reading packets decodes nothing."""
    try:
        result = run_command([resolve_ffprobe(), "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=stream_index", "-of", "csv=p=0", str(path)], timeout=60.0)
    except RenderError:
        return 0
    return len(result.stdout.split())


def _input_matrix(kind: str, color_space: str | None, width: int, height: int) -> str | None:
    """The color matrix to assume when a file does not state one.

    Photos are full-range 601 by convention. Unlabelled video follows what
    players do: 709 for HD, 601 below it. Assuming the wrong one tints the picture.
    """
    if color_space in _KNOWN_MATRICES:
        return None
    if kind == "photo":
        return "bt601"
    return "bt709" if width >= 1280 or height >= 720 else "bt601"


def inspect_media(path: Path | str, *, name: str | None = None) -> MediaItem:
    """Classify a file from its contents. Raises :class:`MediaProblem` for anything else."""
    target = Path(path)
    label = name or target.name
    unreadable = f"{label} is not a video, photo, or audio file Briefcut can read."
    try:
        info = probe(target)
    except ProbeError as exc:
        raise MediaProblem(unreadable) from exc
    if set(info.format_name.split(",")) & _REFERENCE_FORMATS:
        raise MediaProblem(unreadable)
    streams = _pictures(info)
    if not streams:
        if not info.has_audio or not math.isfinite(info.file_duration) or info.file_duration <= 0:
            raise MediaProblem(unreadable)
        return MediaItem(target, label, "audio", duration=info.file_duration, has_audio=True, sound=f"0:a:{info.audio_map}", codec=info.audio.codec_name)
    picture = info.video
    if is_still(target, info):
        width, height, stream_label = _still_size(target, info, streams)
        if width <= 0 or height <= 0:
            raise MediaProblem(unreadable)
        return MediaItem(target, label, "photo", width, height, label=stream_label, matrix=_input_matrix("photo", picture.color_space, width, height), codec=picture.codec_name)
    width, height = info.display_width, info.display_height
    if width <= 0 or height <= 0 or not math.isfinite(info.duration) or info.duration <= 0:
        raise MediaProblem(unreadable)
    hdr = picture.color_trc in _HDR_TRANSFERS and has_filter("zscale") and has_filter("tonemap")
    return MediaItem(target, label, "video", width, height, info.duration, info.has_audio, f"0:v:{info.video_map}", f"0:a:{info.audio_map}", hdr,
                     _input_matrix("video", picture.color_space, width, height), picture.codec_name)


def inspect_clip(path: Path, max_seconds: float) -> MediaItem:
    """One project item: a video or photo, checked to the same bar as a single upload."""
    item = inspect_media(path)
    if item.kind == "audio":
        raise MediaProblem(f"{item.name} has sound but no picture. Add it after creating the project, or choose a video or photo.")
    if item.kind == "video":
        info = probe(path)
        problem = source_problem(info, min_seconds=0.0, max_seconds=max_seconds)
        if problem:
            raise MediaProblem(f"{item.name}: {problem}")
        try:
            check_playable(info)
        except ProbeError as exc:
            raise MediaProblem(f"{item.name}: {exc.plain or 'this clip could not be read.'}") from exc
    return item


def choose_canvas(items: list[MediaItem]) -> tuple[int, int]:
    """The project frame: the first item's shape, at most 1920 on the long side.

    A thumbnail or logo opening a project borrows the size of the largest item
    instead, so one small file cannot shrink everything after it.
    """
    first = items[0]
    longest = max(first.width, first.height)
    wanted = longest
    if longest < SMALL_FIRST_ITEM:
        wanted = max(max(item.width, item.height) for item in items)
    scale = min(MAX_CANVAS, wanted) / longest
    return even(first.width * scale), even(first.height * scale)


def frame_count(item: MediaItem, photo_seconds: float = PHOTO_SECONDS) -> int:
    seconds = photo_seconds if item.kind == "photo" else item.duration
    return max(1, round(seconds * FPS))


def clip_table(items: list[MediaItem], frames: list[int]) -> list[dict[str, Any]]:
    """Where each item sits on the project clock."""
    clips: list[dict[str, Any]] = []
    cursor = 0
    for item, count in zip(items, frames):
        clips.append({"name": item.name, "kind": item.kind, "start": round(cursor / FPS, 3), "end": round((cursor + count) / FPS, 3)})
        cursor += count
    return clips


def _color_head(spec: dict[str, Any]) -> list[str]:
    """Bring any input to 8-bit 4:2:0 limited-range BT.709 before it is composed.

    Every clip of a project must mean the same thing by the same pixel values.
    JPEGs are full range, PNGs are RGB, and phone video is often HDR.
    """
    if spec.get("hdr"):
        return [_TONEMAP, "scale=w=trunc(iw*sar/2)*2:h=ih", "setsar=1"]
    source = f":in_color_matrix={spec['matrix']}" if spec.get("matrix") else ""
    return [f"scale=w=trunc(iw*sar/2)*2:h=ih{source}:out_color_matrix=bt709:out_range=tv", "format=yuv420p", "setsar=1"]


def picture_graph(spec: dict[str, Any], width: int, height: int) -> str:
    """The picture half of one item's filter graph, ending in ``[v]``.

    Video is timed first, so the costly steps only see frames that survive. A
    photo is composed once and held afterwards, so a 48 MP still costs one decode.
    """
    frames = int(spec["frames"])
    photo = spec["kind"] == "photo"
    timing = [f"fps={FPS}:start_time=0", "tpad=stop=-1:stop_mode=clone", f"trim=end_frame={frames}"]
    hold = [f"loop=loop={frames - 1}:size=1:start=0", f"settb=1/{FPS}", "setpts=N"]
    head = ",".join(([] if photo else timing) + _color_head(spec))
    tail = ",".join(["setsar=1", "format=yuv420p", _TAGS] + (hold if photo else []))
    source = f"[{spec['input']}]"
    if abs((spec["width"] / spec["height"]) / (width / height) - 1) <= ASPECT_TOLERANCE:
        return f"{source}{head},scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,crop={width}:{height},{tail}[v]"
    small_width, small_height = even(width / 8), even(height / 8)
    return ";".join([
        f"{source}{head},split=2[back][front]",
        f"[back]scale={small_width}:{small_height}:force_original_aspect_ratio=increase,crop={small_width}:{small_height},{_BLUR},"
        f"scale={width}:{height},eq=brightness=-0.06:saturation=0.9[blurred]",
        f"[front]scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos[fitted]",
        f"[blurred][fitted]overlay=(W-w)/2:(H-h)/2:format=auto,{tail}[v]",
    ])


def sound_graph(spec: dict[str, Any]) -> str:
    """The sound half of one item's filter graph: exactly one frame's worth per frame."""
    samples = int(spec["frames"]) * SAMPLES_PER_FRAME
    if spec.get("has_audio"):
        return f"[{spec['sound']}]aresample={SAMPLE_RATE}:async=1:first_pts=0,aformat=sample_fmts=s16:sample_rates={SAMPLE_RATE}:channel_layouts=stereo,apad,atrim=end_sample={samples}[a]"
    return f"anullsrc=r={SAMPLE_RATE}:cl=stereo,atrim=end_sample={samples},aformat=sample_fmts=s16[a]"


def _clean_spec(raw: Any, index: int) -> dict[str, Any]:
    """Everything a brief can place into a filter graph, checked before it gets there."""
    if not isinstance(raw, dict):
        raise CompileError(f"project_source.items[{index}] must be a mapping")
    spec = dict(raw)
    if spec.get("kind") not in {"video", "photo"}:
        raise CompileError(f"project_source.items[{index}].kind must be video or photo")
    for key in ("frames", "width", "height"):
        value = spec.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise CompileError(f"project_source.items[{index}].{key} must be a positive whole number")
    spec.setdefault("input", "0:v:0")
    spec.setdefault("sound", "0:a:0")
    if not _STREAM_LABEL.fullmatch(str(spec["input"])) or not _STREAM_LABEL.fullmatch(str(spec["sound"])):
        raise CompileError(f"project_source.items[{index}] names an unknown stream")
    if spec.get("matrix") not in (None, *_INPUT_MATRICES):
        raise CompileError(f"project_source.items[{index}].matrix is not a known color matrix")
    return spec


def compile_project_source(c: Any, op: dict[str, Any]) -> None:
    """Compile the ``project_source`` op: one normalize step per item, then a join.

    One step per item keeps a single decoder in memory however many clips there
    are, shows progress clip by clip, and lets a failure name the file. The join
    copies the picture and encodes the sound once.
    """
    raw = op.get("items")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_ITEMS:
        raise CompileError(f"project_source requires between 1 and {MAX_ITEMS} items")
    if not op.get("path"):
        raise CompileError("project_source requires path")
    dest = Path(str(op["path"]))
    width, height = c.canvas.width, c.canvas.height
    clips: list[Path] = []
    for index, item in enumerate(raw, 1):
        spec = _clean_spec(item, index - 1)
        source = c.resolve_source(spec.get("source"))
        out = c.work_dir / f"clip-{index:02d}.mov"
        graph = picture_graph(spec, width, height) + ";" + sound_graph(spec)
        argv = c._ffmpeg() + ["-i", str(source), "-filter_complex", graph, "-map", "[v]", "-map", "[a]", "-r", str(FPS), "-frames:v", str(spec["frames"])]
        argv += _ENCODE + ["-c:a", "pcm_s16le", str(out)]
        c._pipeline.add(Step(f"normalize_{index:02d}", argv, [str(out)], f"fit {source.name} to the {width}x{height} project frame"))
        clips.append(out)
    listing = c.work_dir / "clips.ffconcat"
    listing.write_text("ffconcat version 1.0\n" + "".join(f"file '{clip.name}'\n" for clip in clips), encoding="utf-8")
    argv = c._ffmpeg() + ["-f", "concat", "-i", str(listing), "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", AUDIO_BITRATE, "-movflags", "+faststart", str(dest)]
    c._pipeline.add(Step("join_clips", argv, [str(dest)], f"join {len(clips)} clips into one project source"))
    c.finals.append(dest)
    c.set_current(dest)
    c.duration = sum(int(item["frames"]) for item in raw) / FPS
    c.has_video = c.has_audio = True


def project_brief(items: list[MediaItem], frames: list[int], canvas: tuple[int, int], *, work_dir: Path, dest: Path) -> Brief:
    sources = {f"clip{index:02d}": str(item.path) for index, item in enumerate(items, 1)}
    specs = [
        {"source": key, "name": item.name, "kind": item.kind, "frames": count, "width": item.width, "height": item.height,
         "has_audio": item.has_audio, "input": item.label, "sound": item.sound, "hdr": item.hdr, "matrix": item.matrix}
        for key, item, count in zip(sources, items, frames)
    ]
    return Brief(
        name="project-source", sources=sources,
        ops=[{"op": "project_source", "path": str(dest), "items": specs}],
        canvas=Canvas(width=canvas[0], height=canvas[1], fps=FPS, sample_rate=SAMPLE_RATE), hw=False, crf=16, work_dir=str(work_dir),
    )


def _complete(path: Path, seconds: float) -> bool:
    """A finished output, not a file left behind by a crash. A truncated MP4 cannot be probed."""
    try:
        info = probe(path)
    except VeditError:
        return False
    return info.has_video and info.has_audio and abs(info.duration - seconds) <= JOIN_TOLERANCE


def _named(items: list[MediaItem], index: int) -> str:
    return items[index].name if index < len(items) else "The combined project"


def build_project(
    items: list[MediaItem],
    *,
    work_dir: Path,
    dest: Path,
    photo_seconds: float = PHOTO_SECONDS,
    canvas: tuple[int, int] | None = None,
    on_step: Callable[[int, int, str], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Build the normalized project source and return its clip table and frame.

    Finished clips are kept until the join is verified, so a retry resumes where
    it stopped. Raises :class:`MediaProblem` naming the item that failed.
    """
    canvas = canvas or choose_canvas(items)
    frames = [frame_count(item, photo_seconds) for item in items]
    total = sum(frames) / FPS
    result = {"width": canvas[0], "height": canvas[1], "duration": round(total, 3), "clips": clip_table(items, frames), "has_audio": any(item.has_audio for item in items)}
    if dest.is_file():
        if _complete(dest, total):
            return result
        dest.unlink()
    work_dir.mkdir(parents=True, exist_ok=True)
    brief = project_brief(items, frames, canvas, work_dir=work_dir, dest=dest)
    (work_dir / "brief.yaml").write_text(brief_dumps(brief), encoding="utf-8")
    from vedit.compile import compile_brief

    compiler = compile_brief(brief, work_dir=work_dir)
    write_codegen(compiler.pipeline)
    steps = compiler.pipeline.steps
    labels = [f"Preparing {item.name} ({index} of {len(items)})" for index, item in enumerate(items, 1)] + ["Joining your clips into one project"]
    lengths = [count / FPS for count in frames] + [total]
    reports: list[StepReport] = []
    for index, (step, label, seconds) in enumerate(zip(steps, labels, lengths)):
        if on_step is not None:
            on_step(index, len(steps), label)
        output = Path(step.outputs[0])
        if output.is_file():
            if _complete(output, seconds):
                reports.append(StepReport(step.name, True, 0, step.outputs, "skipped (outputs exist)"))
                continue
            output.unlink(missing_ok=True)
        piece = Pipeline(name=compiler.pipeline.name, work_dir=work_dir / "execution" / f"{index + 1:02d}", steps=[step])
        try:
            reports.extend(execute(piece, progress=False, cancel_event=cancel_event))
        except RenderCancelled:
            raise
        except RenderError as exc:
            raise MediaProblem(f"{_named(items, index)}: {plain_failure(exc)}") from exc
        # FFmpeg can exit cleanly having written nothing, so the file is the proof.
        if not _complete(output, seconds):
            output.unlink(missing_ok=True)
            raise MediaProblem(f"{_named(items, index)} could not be prepared. Check that it plays and that the drive has free space, then retry.")
    write_report(compiler.pipeline, reports, finals=compiler.finals)
    for step in steps[:-1]:
        Path(step.outputs[0]).unlink(missing_ok=True)
    return result


def filmstrip_layout(duration: float, width: int, height: int) -> dict[str, Any]:
    """A tile every 2 to 5 seconds, thinning out past 200 tiles on long sources."""
    spacing = max(min(5.0, max(2.0, duration / 60)), duration / MAX_TILES)
    count = max(1, min(MAX_TILES, math.ceil(duration / spacing)))
    tile_width = even(min(240, max(40, TILE_HEIGHT * width / max(1, height))))
    columns = min(count, TILE_COLUMNS)
    return {"columns": columns, "rows": math.ceil(count / columns), "tile_width": tile_width, "tile_height": TILE_HEIGHT, "count": count, "seconds_per_tile": round(spacing, 3)}


def make_filmstrip(source: Path, dest: Path, *, duration: float, width: int, height: int, stream: int = 0, cancel_event: threading.Event | None = None) -> dict[str, Any]:
    """Write a JPEG sprite of the source and return its layout.

    Each tile is its own seek, so a two-hour file costs about as much as a short
    one. Long files take the nearest keyframe, which is one decode per tile even
    when keyframes are minutes apart.
    """
    layout = filmstrip_layout(duration, width, height)
    tiles = dest.parent / (dest.stem + "-tiles")
    shutil.rmtree(tiles, ignore_errors=True)
    tiles.mkdir(parents=True)
    spacing, last = layout["seconds_per_tile"], max(0.0, duration - 0.05)
    base = [resolve_ffmpeg(), "-hide_banner", "-y", "-v", "error"] + ([] if duration <= EXACT_SEEK_SECONDS else ["-noaccurate_seek"])

    deadline = time.monotonic() + TILE_BUDGET

    def grab(index: int) -> None:
        if time.monotonic() > deadline:
            return
        moment = min((index + 0.5) * spacing, last)
        try:
            run_command(base + ["-ss", f"{moment:.3f}", "-i", str(source), "-map", f"0:v:{stream}", "-an", "-frames:v", "1", "-vf", f"scale={layout['tile_width']}:{layout['tile_height']}",
                                "-q:v", "3", "-update", "1", str(tiles / f"{index:03d}.jpg")], timeout=TILE_TIMEOUT, cancel_event=cancel_event)
        except RenderCancelled:
            raise
        except RenderError:
            return

    try:
        with ThreadPoolExecutor(max_workers=TILE_WORKERS) as pool:
            for _ in pool.map(grab, range(layout["count"])):
                pass
        made = [index for index in range(layout["count"]) if (tiles / f"{index:03d}.jpg").is_file()]
        if not made:
            raise RenderError("no filmstrip tile could be read")
        # A seek past the last readable frame shows its nearest neighbour, so tile N stays at N × spacing.
        for index in range(layout["count"]):
            if index not in made:
                nearest = min(made, key=lambda other: abs(other - index))
                shutil.copyfile(tiles / f"{nearest:03d}.jpg", tiles / f"{index:03d}.jpg")
        pending = dest.with_suffix(".part.jpg")
        # The pattern is relative because a % in the workspace path would read as part of it.
        result = run_command([resolve_ffmpeg(), "-hide_banner", "-y", "-v", "error", "-framerate", "1", "-i", "%03d.jpg", "-vf", f"tile={layout['columns']}x{layout['rows']}",
                              "-frames:v", "1", "-q:v", "5", "-update", "1", str(pending.resolve())], timeout=TILE_TIMEOUT, cwd=tiles, cancel_event=cancel_event)
        if not result.ok or not pending.is_file():
            raise RenderError("the filmstrip could not be assembled")
        pending.replace(dest)
    finally:
        shutil.rmtree(tiles, ignore_errors=True)
    return layout


def make_waveform(source: Path, dest: Path, *, duration: float, has_audio: bool = True, stream: int = 0, cancel_event: threading.Event | None = None) -> dict[str, Any]:
    """Write ``{peaks, seconds_per_peak}`` and return it.

    FFmpeg decodes to a small low-rate file that is then read in pieces, so a
    long source never sits in memory.
    """
    if not has_audio or duration <= 0:
        waveform: dict[str, Any] = {"peaks": [], "seconds_per_peak": 0}
    else:
        points = max(1, min(MAX_PEAKS, math.ceil(duration * PEAKS_PER_SECOND)))
        bucket = max(1, math.ceil(duration * PEAK_SAMPLE_RATE / points))
        pcm = dest.with_suffix(".pcm")
        try:
            result = run_command([resolve_ffmpeg(), "-hide_banner", "-y", "-v", "error", "-i", str(source), "-map", f"0:a:{stream}", "-vn", "-ac", "1", "-ar", str(PEAK_SAMPLE_RATE),
                                  "-f", "s16le", "-c:a", "pcm_s16le", str(pcm)], timeout=PEAK_TIMEOUT, cancel_event=cancel_event)
            if not result.ok:
                raise RenderError("the waveform could not be decoded")
            peaks: list[float] = []
            with pcm.open("rb") as handle:
                while True:
                    raw = handle.read(bucket * 2)
                    if len(raw) < 2:
                        break
                    samples = array("h")
                    samples.frombytes(raw[: len(raw) // 2 * 2])
                    peaks.append(round(min(1.0, max(max(samples), -min(samples)) / 32767), 3))
        finally:
            pcm.unlink(missing_ok=True)
        waveform = {"peaks": peaks[:MAX_PEAKS], "seconds_per_peak": round(bucket / PEAK_SAMPLE_RATE, 4)}
    pending = dest.with_suffix(".part")
    pending.write_text(json.dumps(waveform, separators=(",", ":")) + "\n", encoding="utf-8")
    pending.replace(dest)
    return waveform


def new_asset_id() -> str:
    return secrets.token_hex(8)


def _decodes(item: MediaItem) -> bool:
    """A header can look right on a broken file. Decode a little of it."""
    argv = [resolve_ffmpeg(), "-hide_banner", "-v", "error", "-i", str(item.path)]
    argv += ["-map", item.sound, "-t", "2"] if item.kind == "audio" else ["-frames:v", "1"]
    try:
        return run_command(argv + ["-f", "null", "-"], timeout=60.0).ok
    except RenderError:
        return False


def inspect_asset(path: Path, *, name: str | None = None) -> MediaItem:
    """An audio file or a single image that really decodes, or :class:`MediaProblem`."""
    item = inspect_media(path, name=name)
    if item.kind == "video":
        raise MediaProblem(f"{item.name} is a video. Add an audio file or an image here.")
    if not _decodes(item):
        raise MediaProblem(f"{item.name} is damaged or uses a format Briefcut cannot read.")
    return item


def store_asset(item: MediaItem, folder: Path, ident: str) -> Path:
    """Copy an asset into the job so it outlives the upload inbox.

    The name comes from the contents, never the upload. An image FFmpeg cannot
    treat as a still by name, such as HEIC, is stored as a PNG.
    """
    folder.mkdir(parents=True, exist_ok=True)
    if item.kind == "audio":
        suffix = item.path.suffix.lower()
        dest = folder / (ident + (suffix if _SAFE_SUFFIX.fullmatch(suffix) else ".audio"))
        shutil.copyfile(item.path, dest)
        return dest
    suffix = _IMAGE_SUFFIX.get(item.codec)
    if suffix is not None:
        dest = folder / (ident + suffix)
        shutil.copyfile(item.path, dest)
        return dest
    dest = folder / f"{ident}.png"
    result = run_command([resolve_ffmpeg(), "-hide_banner", "-y", "-v", "error", "-i", str(item.path), "-frames:v", "1", "-update", "1", str(dest)], timeout=120.0)
    if not result.ok or not dest.is_file():
        dest.unlink(missing_ok=True)
        raise MediaProblem(f"{item.name} could not be converted to an image Briefcut can use.")
    return dest


def asset_beats(path: Path) -> list[float] | None:
    """Beat times of a music file when the beat detector is installed. Never a failure."""
    try:
        from vedit.beats import detect_beats
    except ImportError:
        return None
    try:
        found: Any = detect_beats(path)
    except Exception:
        return None
    if isinstance(found, dict):
        found = found.get("beats")
    if not isinstance(found, (list, tuple)):
        return None
    return [round(float(beat), 3) for beat in found[:MAX_BEATS] if isinstance(beat, (int, float)) and not isinstance(beat, bool) and math.isfinite(float(beat))]


def asset_record(item: MediaItem, ident: str, beats: Iterable[float] | None = None) -> dict[str, Any]:
    """What ``job.assets`` holds and clients see. Paths stay private."""
    record: dict[str, Any] = {
        "id": ident, "kind": "audio" if item.kind == "audio" else "image", "name": item.name,
        "duration": round(item.duration, 3) if item.kind == "audio" else None,
        "width": item.width or None, "height": item.height or None,
    }
    if beats is not None:
        record["beats"] = list(beats)
    return record
