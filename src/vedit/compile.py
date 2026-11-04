"""Compile a Brief into an inspectable FFmpeg pipeline."""

from __future__ import annotations

import json
import math
import sys
import unicodedata
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from vedit.captions import burn_filter, title_filter
from vedit.edit_spec import COLOR, CUTOUT_MODES as CUTOUT_ROWS, OVERLAY_POSITIONS, TRANSITIONS
from vedit.effects import EffectError, REQUIRED_EFFECT_FILTERS, validate_effect
from vedit.errors import CompileError
from vedit.ir import Brief, Canvas
from vedit.looks import RECIPES, color_recipes, fallback_filters, is_neutral, lut_filters, spatial_amounts, spatial_filters, write_cube
from vedit.media import (
    MediaInfo,
    escape_filter_path,
    filter_file_args,
    filter_options,
    filter_value,
    has_encoder,
    has_filter,
    is_image,
    probe,
    resolve_ffmpeg,
    source_filters,
    standard_rate,
    with_borders,
)
from vedit.motion import MotionError, plan_pieces, zoom_filter
from vedit.overlays import DEFAULT_FADE, corner, drawtext_filters, image_filter, logo_reserve, text_engine, text_warnings, write_text_ass
from vedit.pipeline import Pipeline, Step
from vedit.settings import default_language
from vedit.presets import MATCH_SOURCE, ExportPreset, encode_args, export_preset_name, get_preset
from vedit.sound import duck_filter, voice_filters
from vedit.timeline import MAX_SPEED, MIN_SPEED, Span, build_timeline, effective_transition, output_duration, segment_keeps, segment_speed
from vedit.edit_spec import HERO_COLORS, HERO_FONTS, HERO_MAX_CHARS, HERO_MAX_SECONDS, HERO_MAX_WORDS, HERO_MIN_SECONDS, HERO_SIDES, hero_word_ok
from vedit.overlays import HERO_FONT_FILES, hero_font, render_hero_ass, visible_rect
from vedit.edit_spec import EditError, validate_motion_card

REQUIRED_HERO_FILTERS = ("alphamerge", "overlay", "scale", "lut", "setpts", "fps", "ass", "split", "setparams")
HERO_ASPECTS = ("9:16", "16:9", "1:1", "4:5", "21:9")
# Wider than a cutout's edge: the subject layer is the frame's own picture, so
# covering a few pixels of background only hides the word a little early.
HERO_EDGE = "lut=y='clip((val-40)*255/120,0,255)'"

CUTOUT_MODES =tuple(row[0] for row in CUTOUT_ROWS if row[0] != "none")
REQUIRED_CUTOUT_FILTERS = ("alphamerge", "scale", "overlay", "lutyuv", "boxblur", "lut", "premultiply", "unpremultiply")
CUTOUT_HOLE = 12
CUTOUT_DIM = 0.32
CUTOUT_DIM_CHROMA = 0.6
CUTOUT_EDGE_CENTER = 167
CUTOUT_EDGE_SPAN = 143
CUTOUT_EDGE_TIGHTEST = 32


# Stored pixels can be non-square (PAL, HDV). Shape decisions need square ones.
SQUARE_PIXELS = "scale=iw*sar:ih,setsar=1"
# The tape's own form: nothing after it sets a size, so its width has to stay even.
SQUARE_TAPE = "scale='trunc(iw*sar/2)*2':ih,setsar=1"


def vf_fit(canvas: Canvas) -> str:
    return (
        f"fps={canvas.fps},{SQUARE_PIXELS},"
        f"scale={canvas.width}:{canvas.height}:force_original_aspect_ratio=decrease,"
        f"pad={canvas.width}:{canvas.height}:(ow-iw)/2:(oh-ih)/2:color={canvas.background},"
        f"setsar=1,format={canvas.pixel_format}"
    )


def vf_fill(canvas: Canvas) -> str:
    return (
        f"fps={canvas.fps},{SQUARE_PIXELS},"
        f"scale={canvas.width}:{canvas.height}:force_original_aspect_ratio=increase,"
        f"crop={canvas.width}:{canvas.height},"
        f"setsar=1,format={canvas.pixel_format}"
    )


def af_norm(canvas: Canvas) -> str:
    return f"aformat=sample_fmts=fltp:sample_rates={canvas.sample_rate}:channel_layouts=stereo"


def atempo_chain(speed: float) -> str:
    if speed <= 0:
        raise CompileError("speed must be > 0")
    parts: list[str] = []
    remaining = float(speed)
    while remaining > 2.0 + 1e-6:
        parts.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5 - 1e-6:
        parts.append("atempo=0.5")
        remaining /= 0.5
    parts.append(f"atempo={remaining:.5f}")
    return ",".join(parts)


def _join(filters: list[str]) -> str:
    return ",".join(f for f in filters if f)


@dataclass
class Compiler:
    brief: Brief
    work_dir: Path
    canvas: Canvas
    current: Path | None = None
    info: MediaInfo | None = None
    vf: list[str] = field(default_factory=list)
    af: list[str] = field(default_factory=list)
    seq: int = 0
    finals: list[Path] = field(default_factory=list)
    sidecar: dict[str, Path] = field(default_factory=dict)
    has_video: bool = True
    has_audio: bool = True
    duration: float | None = None
    pending_loudnorm: dict[str, float] | None = None
    pending_thumbs: list[dict[str, Any]] = field(default_factory=list)
    pending_fade_out: float | None = None
    warnings: list[str] = field(default_factory=list)
    texts: list[dict[str, Any]] = field(default_factory=list)
    images: int = 0
    source_size: tuple[int, int] | None = None
    text_reserve: tuple[int, int] = (0, 0)
    clock_frames: int | None = None  # frames on the output clock select_ranges fixed

    @property
    def pipeline(self) -> Pipeline:
        return self._pipeline

    def __post_init__(self) -> None:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._pipeline = Pipeline(name=self.brief.name, work_dir=self.work_dir)
        self._pipeline.meta = {
            "canvas": self.canvas.as_dict(),
            "sources": self.brief.sources,
            "ops": self.brief.ops,
            "warnings": self.warnings,
        }

    def warn(self, message: str) -> None:
        """A plain note for the person editing: something was degraded, not silently dropped."""
        if message not in self.warnings:
            self.warnings.append(message)

    def _next(self, label: str, suffix: str = ".mp4") -> Path:
        self.seq += 1
        return self.work_dir / f"{self.seq:02d}_{label}{suffix}"

    def _ffmpeg(self) -> list[str]:
        return [resolve_ffmpeg(), "-hide_banner", "-y"]

    def _encode(self, *, final: bool = False, preset: ExportPreset | None = None) -> list[str]:
        if preset is not None:
            return encode_args(preset, hw=self.brief.hw, crf=self.brief.crf)
        if final:
            return encode_args(get_preset("youtube_1080p"), hw=self.brief.hw, crf=self.brief.crf)
        return [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "16",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
        ]

    def _require_current(self) -> Path:
        if self.current is None:
            raise CompileError("no current clip — add a source or a concat/slideshow op first")
        return self.current

    def _note_from_path(self, path: Path) -> None:
        if not path.is_file():
            return
        info = probe(path)
        self.info = info
        self.has_video = info.has_video
        self.has_audio = info.has_audio
        self.duration = info.duration

    def add_vf(self, *filters: str) -> None:
        self.vf.extend(f for f in filters if f)

    def add_af(self, *filters: str) -> None:
        self.af.extend(f for f in filters if f)

    def take_filters(self) -> tuple[str, str]:
        video, audio = _join(self.vf), _join(self.af)
        self.vf.clear()
        self.af.clear()
        return video, audio

    def materialize(self, label: str) -> Path | None:
        if self.current is None:
            return None
        if not self.vf and not self.af:
            return self.current
        path = self.current
        out = self._next(label)
        argv = self._ffmpeg() + ["-i", str(path)]
        if self.vf and self.has_video:
            argv += ["-vf", _join(self.vf)]
        elif not self.has_video:
            argv += ["-vn"]
        if self.af and self.has_audio:
            argv += ["-af", _join(self.af)]
        elif not self.has_audio:
            argv += ["-an"]
        argv += self._encode() + [str(out)]
        self._pipeline.add(Step(label, argv, [str(out)], f"apply pending filters ({label})"))
        self.current = out
        self.vf.clear()
        self.af.clear()
        self.info = None
        return out

    def set_current(self, path: Path) -> None:
        self.current = path
        self.vf.clear()
        self.af.clear()
        self._note_from_path(path)

    def resolve_source(self, spec: Any | None = None) -> Path:
        if spec is None:
            return self._require_current()
        if isinstance(spec, Path):
            return spec
        text = str(spec)
        return self.brief.source_path(text, base=self.work_dir)

    def primary_source(self) -> Path | None:
        for key in ("main", "talk", "input", "video", "src", "footage"):
            if key in self.brief.sources:
                return Path(self.brief.sources[key])
        if len(self.brief.sources) == 1:
            return Path(next(iter(self.brief.sources.values())))
        return None


def compile_brief(brief: Brief, *, work_dir: Path | None = None) -> Compiler:
    root = Path(work_dir or brief.work_dir or Path(brief.output_dir) / brief.name).expanduser()
    if not root.is_absolute():
        root = Path.cwd() / root
    compiler = Compiler(brief=brief, work_dir=root, canvas=brief.canvas)
    primary = compiler.primary_source()
    missing = [f"{name}={path}" for name, path in brief.sources.items() if not Path(path).is_file()]
    if missing:
        raise CompileError("missing source file(s): " + ", ".join(missing))
    if primary is not None:
        compiler.set_current(primary)
        compiler._pipeline.meta["primary"] = str(primary)
        if compiler.info is not None and compiler.info.has_video:
            # Black bars are measured once per file; select_ranges crops them before any reframe.
            compiler.info = with_borders(compiler.info)
            # select_ranges squares anamorphic pixels and truncates the tape
            # width to an even number before a cutout composites the matte.
            square_width = compiler.info.square_width
            compiler.source_size = (square_width - square_width % 2, compiler.info.display_height)
    if not compiler.canvas.fps:
        # A rate of 0 means "match the source": its own rate, snapped to a standard one, before any op counts frames.
        footage = compiler.info.fps if compiler.info is not None and compiler.info.has_video else None
        compiler.canvas = replace(compiler.canvas, fps=standard_rate(footage))
        compiler._pipeline.meta["canvas"] = compiler.canvas.as_dict()
    for index, op in enumerate(brief.ops):
        name = str(op.get("op") or "").strip()
        handler = OPS.get(name)
        if handler is None:
            known = ", ".join(sorted(OPS))
            raise CompileError(f"ops[{index}]: unknown op '{name}'. Known: {known}")
        handler(compiler, op)
    if not compiler.finals:
        if compiler.current is not None:
            dest = Path(brief.output) if brief.output else compiler.work_dir / f"{brief.name}.mp4"
            if not dest.is_absolute():
                dest = compiler.work_dir / dest
            _export(compiler, {"op": "export", "path": str(dest), "preset": (brief.presets or ["youtube_1080p"])[0]})
    _flush_thumbs(compiler)
    compiler._pipeline.meta["finals"] = [str(p) for p in compiler.finals]
    compiler._pipeline.meta["encodes"] = sum(
        1 for s in compiler.pipeline.steps if s.name.startswith(("cut_silence", "export_", "loudnorm", "concat", "trim", "select_ranges", "spatial_effect", "slideshow", "overlay", "music", "replace_audio", "pre_"))
    )
    return compiler


def _master_argv(
    c: Compiler,
    src: Path,
    dest: Path,
    *,
    vf: str,
    af: str,
    loudnorm: dict[str, float] | None,
    preset_name: str,
    has_video: bool,
    has_audio: bool,
    fade_out: float | None = None,
) -> list[str]:
    argv = [
        sys.executable,
        "-m",
        "vedit.analyze",
        "encode",
        "--in",
        str(src),
        "--out",
        str(dest),
        "--preset",
        preset_name,
        "--crf",
        str(c.brief.crf),
    ]
    if vf:
        argv += ["--vf", vf]
    if af:
        argv += ["--af", af]
    if loudnorm:
        argv += [
            "--loudnorm",
            "--i",
            str(loudnorm["i"]),
            "--tp",
            str(loudnorm["tp"]),
            "--lra",
            str(loudnorm["lra"]),
        ]
    if fade_out:
        argv += ["--fade-out", str(fade_out)]
    if c.brief.hw is True:
        argv.append("--hw")
    elif c.brief.hw is False:
        argv.append("--no-hw")
    if has_video:
        argv.append("--video")
    if has_audio:
        argv.append("--audio")
    return argv


def _verify_export(c: Compiler, dest: Path, *, ref: Path | None) -> None:
    report = dest.with_suffix(dest.suffix + ".verify.json")
    argv = [
        sys.executable,
        "-m",
        "vedit.analyze",
        "verify",
        "--in",
        str(dest),
        "--out",
        str(report),
    ]
    if ref is not None:
        argv += ["--ref", str(ref)]
    c._pipeline.add(Step(f"verify_{dest.stem}", argv, [str(report)], f"verify {dest.name} (streams + loudness)"))


def _flush_thumbs(c: Compiler) -> None:
    if not c.pending_thumbs:
        return
    src = c.finals[0] if c.finals else c.current
    if src is None:
        return
    for thumb in c.pending_thumbs:
        dest = Path(thumb["path"]) if thumb.get("path") else c.work_dir / "thumbnail.jpg"
        argv = c._ffmpeg() + ["-ss", f"{float(thumb['at']):.3f}", "-i", str(src), "-frames:v", "1", str(dest)]
        c._pipeline.add(Step("thumbnail", argv, [str(dest)], f"thumbnail at {thumb['at']}s from {src.name}"))
        c.finals.append(dest)
    c.pending_thumbs.clear()


def _trim(c: Compiler, op: dict[str, Any]) -> None:
    start = float(op.get("start", op.get("in", 0.0)))
    end = op.get("end", op.get("out"))
    duration = op.get("duration")
    c.materialize("pre_trim")
    src = c.resolve_source(op.get("source"))
    out = c._next("trim")
    argv = c._ffmpeg() + ["-ss", f"{start:.3f}", "-i", str(src)]
    if duration is not None:
        argv += ["-t", f"{float(duration):.3f}"]
    elif end is not None:
        argv += ["-t", f"{max(0.01, float(end) - start):.3f}"]
    argv += ["-vf", vf_fit(c.canvas), "-af", af_norm(c.canvas)] + c._encode() + [str(out)]
    c._pipeline.add(Step("trim", argv, [str(out)], f"trim {start}→{end or duration}"))
    c.set_current(out)
    if duration is not None:
        c.duration = float(duration)
    elif end is not None:
        c.duration = max(0.01, float(end) - start)
    elif c.duration is not None:
        c.duration = max(0.01, c.duration - start)
    c.has_video = True
    c.has_audio = True


def _finite_number(value: Any, label: str) -> float:
    """Accept only JSON numbers, never booleans or expression strings."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CompileError(f"{label} must be a finite number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise CompileError(f"{label} must be a finite number") from exc
    if not math.isfinite(number):
        raise CompileError(f"{label} must be a finite number")
    return number


CUT_RAMP_SECONDS = 0.006
LOOSE_SEEK_FORMATS = frozenset({"mpegts", "mpeg", "mpegvideo"})
SEEK_LEAD_IN_SECONDS = 3.0


def _sum_expression(terms: list[str]) -> str:
    """Add many terms. FFmpeg refuses a long flat sum, so nest them as a balanced tree."""
    while len(terms) > 1:
        terms = [f"({terms[i]}+{terms[i + 1]})" if i + 1 < len(terms) else terms[i]
                 for i in range(0, len(terms), 2)]
    return terms[0]


def _range_spec(c: Compiler, item: Any, index: int) -> dict[str, Any]:
    """One validated selection: source interval, optional keeps, speed, and gain."""
    if not isinstance(item, dict):
        raise CompileError(f"select_ranges.ranges[{index}] must contain start and end")
    start = _finite_number(item.get("start"), f"ranges[{index}].start")
    end = _finite_number(item.get("end"), f"ranges[{index}].end")
    if start < 0 or end - start < 0.000001:
        raise CompileError(f"ranges[{index}] requires 0 <= start < end and at least one microsecond")
    if c.duration is not None and end > c.duration + 0.001:
        raise CompileError(f"ranges[{index}].end exceeds the {c.duration:.3f}s clip")
    spec: dict[str, Any] = {"start": start, "end": end}
    keep = item.get("keep")
    if keep is not None:
        if not isinstance(keep, (list, tuple)) or not keep:
            raise CompileError(f"ranges[{index}].keep must list [start, end] pieces")
        pieces = []
        for part, piece in enumerate(keep):
            if not isinstance(piece, (list, tuple)) or len(piece) != 2:
                raise CompileError(f"ranges[{index}].keep[{part}] must be [start, end]")
            low = _finite_number(piece[0], f"ranges[{index}].keep[{part}] start")
            high = _finite_number(piece[1], f"ranges[{index}].keep[{part}] end")
            if low < start - 0.001 or high > end + 0.001 or high <= low:
                raise CompileError(f"ranges[{index}].keep[{part}] must fall inside its range and end after it starts")
            pieces.append([low, high])
        spec["keep"] = pieces
    if item.get("speed") is not None:
        speed = _finite_number(item.get("speed"), f"ranges[{index}].speed")
        if not MIN_SPEED <= speed <= MAX_SPEED:
            raise CompileError(f"ranges[{index}].speed must be between {MIN_SPEED:g} and {MAX_SPEED:g}")
        spec["speed"] = speed
    volume = 1.0
    if item.get("volume") is not None:
        volume = _finite_number(item.get("volume"), f"ranges[{index}].volume")
        if not 0 <= volume <= 2:
            raise CompileError(f"ranges[{index}].volume must be between 0 and 2")
    spec["volume"] = volume
    return spec


def _range_transition(op: dict[str, Any]) -> tuple[str, float]:
    """The transition's closed catalogue id and wanted seconds; ("none", 0) for hard cuts."""
    raw = op.get("transition")
    if raw is None:
        return "none", 0.0
    if not isinstance(raw, dict):
        raise CompileError("select_ranges.transition must be {type, seconds}")
    kind = raw.get("type", "none")
    if not isinstance(kind, str) or kind not in {row[0] for row in TRANSITIONS}:
        raise CompileError("select_ranges.transition.type is not an available transition")
    seconds = _finite_number(raw.get("seconds", 0.35), "select_ranges.transition.seconds")
    if seconds < 0:
        raise CompileError("select_ranges.transition.seconds must not be negative")
    return (kind, seconds) if kind != "none" else ("none", 0.0)


def _frame_windows(spans: list[Span], count: int, fps: float, *, overlapping: bool) -> list[tuple[int, int]]:
    """Each range's ``[first, last)`` frames on the output clock.

    Rounding every edge from the timeline, rather than every length, keeps each
    cut within half a frame of the plan however many ranges there are. A range
    always shows a frame, and an overlap always spans one.
    """
    edges: dict[int, list[float]] = {}
    for span in spans:
        edges.setdefault(span.segment, [span.output_start, span.output_end])[1] = span.output_end
    windows: list[tuple[int, int]] = []
    for index in range(1, count + 1):
        start, end = edges[index]
        first, last = round(start * fps), round(end * fps)
        if windows:
            first = min(first, windows[-1][1] - 1) if overlapping else windows[-1][1]
        windows.append((first, max(last, first + 1)))
    return windows


def _select_ranges(c: Compiler, op: dict[str, Any]) -> None:
    """Assemble source-time intervals in their declared editorial order.

    Separate seekable inputs avoid the unbounded buffering a split/concat
    graph would need when the last part of a long recording is shown first.
    Pending tracking filters see original source timestamps before each
    interval is placed on its own output clock.

    A range can keep only some of its source (``keep``), play at a ``speed``,
    and change its ``volume``; the op can overlap neighbours with a
    ``transition``. All of the timing comes from ``vedit.timeline``, and every
    range is forced to the exact frames and samples that clock gives it, so a
    cut never drifts from its captions, and sound never drifts from picture.
    """
    src = c._require_current()
    raw = op.get("ranges")
    if not isinstance(raw, list) or not 1 <= len(raw) <= 32:
        raise CompileError("select_ranges requires between 1 and 32 ranges")
    ranges = [_range_spec(c, item, index) for index, item in enumerate(raw)]
    if not c.has_video and not c.has_audio:
        raise CompileError("select_ranges requires audio or video")
    kind, wanted = _range_transition(op)
    overlap = effective_transition(ranges, wanted)
    if overlap > 0 and c.has_video and not has_filter("xfade"):
        raise CompileError("Transitions need the FFmpeg xfade filter. Install ffmpeg-full.")
    spans = build_timeline(ranges, transition=overlap)

    # These transforms change which source time a requested tape time means.
    # Settle them before input seeking; geometry-only tracking stays fused.
    if any(bit.startswith(("setpts=", "reverse", "tpad=")) or "movie=" in bit for bit in c.vf) or any(
        bit.startswith(("atempo=", "areverse", "apad=")) for bit in c.af
    ):
        c.materialize("pre_select_ranges")
        src = c._require_current()
    video, audio = c.take_filters()
    fps, rate = c.canvas.fps, c.canvas.sample_rate
    windows = _frame_windows(spans, len(ranges), fps, overlapping=overlap > 0)
    # A source can hold cover art, proxies, or several tracks. Our own tape cannot.
    original = c.info if c.info and c.info.path == src.resolve() else None
    picture_track, sound_track = (original.video_map, original.audio_map) if original else (0, 0)
    prepare = source_filters(original) if original else ""
    argv = c._ffmpeg()
    graph: list[str] = []
    picture: list[str] = []
    sound: list[str] = []
    # A transport stream seeks to a nearby keyframe, which can land past the
    # request, so read in early and let the filters find the moment.
    lead_in = SEEK_LEAD_IN_SECONDS if original and set(original.format_name.split(",")) & LOOSE_SEEK_FORMATS else 0.0
    for index, (spec, (first, last)) in enumerate(zip(ranges, windows)):
        keeps = segment_keeps(spec)
        speed, stop = segment_speed(spec), keeps[-1][1]
        begin = max(0.0, keeps[0][0] - lead_in)
        argv += ["-ss", f"{begin:.6f}", "-t", f"{stop - begin:.6f}", "-i", str(src)]
        selective = len(keeps) > 1 or begin < keeps[0][0]
        # Half-open pieces: a frame sitting exactly on a shared edge plays once.
        pick = _sum_expression([f"between(t,{low:.6f},{high - 0.000001:.6f})" for low, high in keeps])
        frames, samples = last - first, round((last - first) * rate / fps)
        if c.has_video:
            # Each kept frame lands where the timeline says, not one frame after
            # the previous, so rounding cannot build up across many small cuts.
            # The running sum also saturates, so end of stream maps correctly.
            clock = _sum_expression([f"clip(T-{low:.6f},0,{high - low:.6f})" for low, high in keeps])
            chain = [prepare, f"setpts=PTS+{begin:.6f}/TB", video, f"fps={fps}",
                     f"select='{pick}'" if selective else "",
                     f"setpts='round(({clock})/{speed:.6f}/TB)'",
                     f"fps={fps}:start_time=0", "tpad=stop=-1:stop_mode=clone", f"trim=end_frame={frames}",
                     SQUARE_TAPE, "format=yuv420p"]
            graph.append(f"[{index}:v:{picture_track}]" + _join(chain) + f"[rv{index}]")
            picture.append(f"[rv{index}]")
        if c.has_audio:
            # One-millisecond packets let aselect cut close to the sample, and let
            # a short ramp close each edge so a cut through room tone never clicks.
            ramp = _sum_expression([f"clip(min(t-{low:.6f},{high:.6f}-t)/{CUT_RAMP_SECONDS},0,1)" for low, high in keeps])
            # Sound can start after the picture, or drop out. Silence holds its place,
            # because the N/SR clock below counts samples and cannot see a gap.
            chain = [f"asetpts=PTS+{begin:.6f}/TB", audio, af_norm(c.canvas), f"aresample=async=1:first_pts={round(begin * rate)}",
                     f"asetnsamples=n={max(1, rate // 1000)}:p=0", f"volume='{ramp}':eval=frame",
                     f"aselect='{pick}'" if selective else "",
                     "asetpts=N/SR/TB", atempo_chain(speed) if speed != 1 else "",
                     f"volume={spec['volume']:.6f}" if spec["volume"] != 1 else "",
                     f"apad=whole_len={samples}", f"atrim=end_sample={samples}", f"asettb=1/{rate}"]
            graph.append(f"[{index}:a:{sound_track}]" + _join(chain) + f"[ra{index}]")
            sound.append(f"[ra{index}]")
    if overlap > 0:
        last_video, last_audio = picture[0] if picture else "", sound[0] if sound else ""
        for index in range(1, len(ranges)):
            frames = windows[index - 1][1] - windows[index][0]
            if c.has_video:
                graph.append(f"{last_video}{picture[index]}xfade=transition={kind}:duration={frames / fps:.6f}"
                             f":offset={windows[index][0] / fps:.6f}[xv{index}]")
                last_video = f"[xv{index}]"
            if c.has_audio:
                graph.append(f"{last_audio}{sound[index]}acrossfade=ns={round(frames * rate / fps)}:c1=qsin:c2=qsin[xa{index}]")
                last_audio = f"[xa{index}]"
        picture, sound = [last_video] if c.has_video else [], [last_audio] if c.has_audio else []
    else:
        outputs = ("[v]" if c.has_video else "") + ("[a]" if c.has_audio else "")
        streams = [labels for labels in (picture, sound) if labels]
        graph.append("".join("".join(row) for row in zip(*streams)) + f"concat=n={len(ranges)}:v={int(c.has_video)}:a={int(c.has_audio)}" + outputs)
        picture, sound = ["[v]"] if c.has_video else [], ["[a]"] if c.has_audio else []
    out = c._next("select_ranges")
    # Dense tracks repeated across source ranges can exceed the OS argument
    # limit. The filter program remains a readable artifact beside the plan.
    graph_file = out.with_suffix(".filter.txt")
    graph_text = ";\n".join(graph)
    graph_file.write_text(graph_text + "\n", encoding="utf-8")
    # Small graphs remain compatible with older FFmpeg releases. Dense
    # graphs use FFmpeg's current file-backed option syntax.
    argv += (["-filter_complex", graph_text] if len(graph_text) < 24000
             else filter_file_args(graph_file))
    for label in picture + sound:
        argv += ["-map", label]
    argv += c._encode() + [str(out)]
    pieces = sum(len(segment_keeps(spec)) for spec in ranges)
    joined = f" with {kind} transitions" if overlap > 0 else ""
    c.pipeline.add(Step("select_ranges", argv, [str(out)], f"assemble {pieces} selected moments{joined}"))
    c.set_current(out)
    c.info = None
    # The same rounded value plan_timeline reports, so later ops and the plan agree.
    c.duration = round(output_duration(spans), 3)
    c.clock_frames = windows[-1][1]


def _cut_silence(c: Compiler, op: dict[str, Any]) -> None:
    c.materialize("pre_silence")
    src = c.resolve_source(op.get("source"))
    if src.is_file():
        info = probe(src)
        if not info.has_audio:
            raise CompileError("cut_silence requires an audio track")
        c.has_audio = True
        c.has_video = info.has_video
        c.duration = info.duration
    elif not c.has_audio:
        raise CompileError("cut_silence requires an audio track")
    # Left out, the pause level is measured from this recording's own loudness at render.
    threshold = None if op.get("threshold_db") is None else _finite_number(op["threshold_db"], "cut_silence threshold_db")
    min_silence = float(op.get("min_silence", 0.45))
    pad = float(op.get("pad", 0.12))
    out = c._next("cut_silence")
    keeps = c.work_dir / "keep_ranges.json"
    argv = [
        sys.executable,
        "-m",
        "vedit.analyze",
        "cut-silence",
        "--in",
        str(src),
        "--out",
        str(out),
        "--keeps",
        str(keeps),
        *(["--threshold-db", str(threshold)] if threshold is not None else []),
        "--min-silence",
        str(min_silence),
        "--pad",
        str(pad),
        "--width",
        str(c.canvas.width),
        "--height",
        str(c.canvas.height),
        "--fps",
        str(c.canvas.fps),
        "--sample-rate",
        str(c.canvas.sample_rate),
    ]
    c._pipeline.add(
        Step(
            "cut_silence",
            argv,
            [str(out), str(keeps)],
            f"drop silence < {threshold if threshold is not None else 'the measured'} dB (pad {pad}s) at render",
        )
    )
    c.set_current(out)
    c.sidecar["keeps"] = keeps


def _concat(c: Compiler, op: dict[str, Any]) -> None:
    raw_sources = op.get("sources") or op.get("clips")
    if not raw_sources:
        raise CompileError("concat requires sources: [a, b, ...]")
    paths = [c.brief.source_path(str(s)) if str(s) in c.brief.sources else Path(str(s)).expanduser().resolve() for s in raw_sources]
    for path in paths:
        if not path.is_file():
            raise CompileError(f"concat source missing: {path}")
    transition = op.get("transition")
    tdur = float(op.get("transition_duration", 0.5))
    if c.current is not None:
        c.materialize("pre_concat")
    out = c._next("concat")
    argv = c._ffmpeg()
    for path in paths:
        argv += ["-i", str(path)]
    n = len(paths)
    parts: list[str] = []
    for i, path in enumerate(paths):
        info = probe(path)
        if info.has_video:
            parts.append(f"[{i}:v]{vf_fit(c.canvas)},setpts=PTS-STARTPTS[v{i}]")
        else:
            parts.append(
                f"color=c={c.canvas.background}:s={c.canvas.width}x{c.canvas.height}:r={c.canvas.fps},"
                f"format={c.canvas.pixel_format}[v{i}]"
            )
        if info.has_audio:
            parts.append(f"[{i}:a]{af_norm(c.canvas)},asetpts=PTS-STARTPTS[a{i}]")
        else:
            parts.append(
                f"anullsrc=r={c.canvas.sample_rate}:cl=stereo,atrim=0:{max(info.duration, 0.05):.3f}[a{i}]"
            )
    if transition and n >= 2 and has_filter("xfade"):
        durations = [probe(p).duration for p in paths]
        acc = durations[0]
        last_v, last_a = "v0", "a0"
        for i in range(1, n):
            offset = max(0.0, acc - tdur)
            nv, na = f"vx{i}", f"ax{i}"
            parts.append(
                f"[{last_v}][v{i}]xfade=transition={transition}:duration={tdur}:offset={offset:.3f}[{nv}]"
            )
            parts.append(f"[{last_a}][a{i}]acrossfade=d={tdur}[{na}]")
            last_v, last_a = nv, na
            acc = acc + durations[i] - tdur
        argv += ["-filter_complex", ";".join(parts), "-map", f"[{last_v}]", "-map", f"[{last_a}]"]
        desc = f"concat {n} clips with {transition} xfade"
    else:
        chain = "".join(f"[v{i}][a{i}]" for i in range(n))
        parts.append(f"{chain}concat=n={n}:v=1:a=1[v][a]")
        argv += ["-filter_complex", ";".join(parts), "-map", "[v]", "-map", "[a]"]
        desc = f"concat {n} clips"
    argv += c._encode() + [str(out)]
    c._pipeline.add(Step("concat", argv, [str(out)], desc))
    c.set_current(out)
    total = sum(probe(p).duration for p in paths)
    if transition and n >= 2:
        total -= tdur * (n - 1)
    c.duration = total
    c.has_video = True
    c.has_audio = True


def _speed(c: Compiler, op: dict[str, Any]) -> None:
    factor = float(op.get("factor", op.get("speed", 1.0)))
    c.add_vf(f"setpts=PTS/{factor}")
    if c.has_audio:
        c.add_af(atempo_chain(factor))
    if c.duration is not None:
        c.duration = c.duration / factor


def _reverse(c: Compiler, op: dict[str, Any]) -> None:
    c.add_vf("reverse")
    if c.has_audio:
        c.add_af("areverse")


def _freeze(c: Compiler, op: dict[str, Any]) -> None:
    seconds = float(op.get("duration", 2.0))
    c.add_vf(f"tpad=stop_mode=clone:stop_duration={seconds}")
    if c.has_audio:
        c.add_af(f"apad=pad_dur={seconds}")
    if c.duration is not None:
        c.duration += seconds


def _fit(c: Compiler, op: dict[str, Any]) -> None:
    c.add_vf(vf_fit(c.canvas))


def _fill(c: Compiler, op: dict[str, Any]) -> None:
    c.add_vf(vf_fill(c.canvas))


def _scale(c: Compiler, op: dict[str, Any]) -> None:
    w = int(op.get("width", c.canvas.width))
    h = int(op.get("height", c.canvas.height))
    c.add_vf(f"scale={w}:{h}")


def _crop(c: Compiler, op: dict[str, Any]) -> None:
    w = int(op.get("width", c.canvas.width))
    h = int(op.get("height", c.canvas.height))
    x = op.get("x", "(in_w-out_w)/2")
    y = op.get("y", "(in_h-out_h)/2")
    c.add_vf(f"crop={w}:{h}:{x}:{y}")


def _pad(c: Compiler, op: dict[str, Any]) -> None:
    w = int(op.get("width", c.canvas.width))
    h = int(op.get("height", c.canvas.height))
    color = op.get("color", c.canvas.background)
    c.add_vf(f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color={color}")


def _rotate(c: Compiler, op: dict[str, Any]) -> None:
    degrees = float(op.get("degrees", 90))
    if degrees % 360 == 90:
        c.add_vf("transpose=1")
    elif degrees % 360 == 270:
        c.add_vf("transpose=2")
    elif degrees % 360 == 180:
        c.add_vf("transpose=1,transpose=1")
    else:
        c.add_vf(f"rotate={degrees}*PI/180")


def _hflip(c: Compiler, op: dict[str, Any]) -> None:
    c.add_vf("hflip")


def _vflip(c: Compiler, op: dict[str, Any]) -> None:
    c.add_vf("vflip")


def _fps(c: Compiler, op: dict[str, Any]) -> None:
    c.add_vf(f"fps={float(op.get('fps', c.canvas.fps))}")


def _color(c: Compiler, op: dict[str, Any]) -> None:
    eq = []
    for key in ("brightness", "contrast", "saturation", "gamma"):
        if key in op:
            eq.append(f"{key}={op[key]}")
    if eq:
        c.add_vf("eq=" + ":".join(eq))
    if op.get("lut"):
        c.add_vf(f"lut3d='{escape_filter_path(op['lut'])}'")


def _denoise(c: Compiler, op: dict[str, Any]) -> None:
    strength = float(op.get("strength", 3))
    c.add_vf(f"hqdn3d={strength}:{strength}:{strength*1.5}:{strength*1.5}")


def _sharpen(c: Compiler, op: dict[str, Any]) -> None:
    c.add_vf(f"unsharp={float(op.get('luma', 5))}:{float(op.get('chroma', 5))}")


def _blur(c: Compiler, op: dict[str, Any]) -> None:
    c.add_vf(f"boxblur={int(op.get('luma', 2))}:{int(op.get('chroma', 1))}")


def _fade_in(c: Compiler, op: dict[str, Any]) -> None:
    d = float(op.get("duration", 0.4))
    c.add_vf(f"fade=t=in:st=0:d={d}")
    if c.has_audio:
        c.add_af(f"afade=t=in:st=0:d={d}")


def _fade_out(c: Compiler, op: dict[str, Any]) -> None:
    c.pending_fade_out = float(op.get("duration", 0.6))


def _volume(c: Compiler, op: dict[str, Any]) -> None:
    if "db" in op:
        c.add_af(f"volume={float(op['db'])}dB")
    else:
        c.add_af(f"volume={float(op.get('factor', 1.0))}")


def _highpass(c: Compiler, op: dict[str, Any]) -> None:
    c.add_af(f"highpass=f={int(op.get('hz', 80))}")


def _lowpass(c: Compiler, op: dict[str, Any]) -> None:
    c.add_af(f"lowpass=f={int(op.get('hz', 12000))}")


def _loudnorm(c: Compiler, op: dict[str, Any]) -> None:
    c.pending_loudnorm = {
        "i": float(op.get("i", -14)),
        "tp": float(op.get("tp", -1.5)),
        "lra": float(op.get("lra", 11)),
    }


def _title(c: Compiler, op: dict[str, Any]) -> None:
    text = str(op.get("text") or "")
    if not text:
        raise CompileError("title requires text")
    start = end = None
    if op.get("start") is not None:
        start = _finite_number(op.get("start"), "title.start")
        if start < 0:
            raise CompileError("title.start must be >= 0")
    if op.get("duration") is not None:
        length = _finite_number(op.get("duration"), "title.duration")
        if length <= 0:
            raise CompileError("title.duration must be > 0")
        end = (start or 0.0) + length
    c.add_vf(title_filter(
        text, c.canvas.height, y_ratio=float(op.get("y_ratio", 0.78)), size=int(op.get("size", 56)),
        box=bool(op.get("box", False)), start=start, end=end,
    ))


def _existing_media(c: Compiler, spec: Any | None = None) -> Path:
    if spec is not None:
        path = c.resolve_source(spec)
        if path.is_file():
            return path
        raise CompileError(f"captions/scenes source does not exist yet: {path}")
    if c.current is not None and c.current.is_file():
        return c.current
    primary = c.primary_source()
    if primary is not None and primary.is_file():
        return primary
    raise CompileError("no existing media file to analyze — render earlier steps or pass source=")


def _captions(c: Compiler, op: dict[str, Any]) -> None:
    if op.get("file"):
        # A finished subtitle file (for example from a provider transcript) is
        # burned as-is; no transcription step is scheduled.
        provided = Path(str(op["file"]))
        if not provided.is_absolute():
            provided = (c.work_dir / provided).resolve()
        if provided.suffix.lower() not in {".srt", ".ass"}:
            raise CompileError("captions file= must be an .srt or .ass subtitle file")
        if not provided.is_file() or provided.stat().st_size == 0:
            raise CompileError(f"captions file not found or empty: {provided}")
        c.sidecar["srt" if provided.suffix.lower() == ".srt" else "ass"] = provided
        if c.current is None:
            raise CompileError("captions needs a current clip")
        if op.get("burn", True):
            c.add_vf(burn_filter(provided, style=str(op.get("style", "lower-third"))))
        return
    if op.get("source"):
        src = c.resolve_source(op.get("source"))
    elif c.current is not None:
        src = c.current
    else:
        raise CompileError("captions needs a current clip or source=")
    language = str(op.get("language") or default_language())
    srt = c.work_dir / "captions.srt"
    ass = c.work_dir / "captions.ass"
    argv = [
        sys.executable,
        "-m",
        "vedit.captions",
        "--media",
        str(src),
        "--work-dir",
        str(c.work_dir),
        "--srt",
        str(srt),
        "--ass",
        str(ass),
        "--language",
        language,
        "--width",
        str(c.canvas.width),
        "--height",
        str(c.canvas.height),
    ]
    keeps_path = c.sidecar.get("keeps")
    if keeps_path is not None and Path(keeps_path).is_file() and c.current is not None and src.resolve() != c.current.resolve():
        argv += ["--keeps", str(keeps_path)]
    c._pipeline.add(
        Step(
            "captions_transcribe",
            argv,
            [str(srt), str(ass)],
            f"transcribe {src.name} → SRT/ASS (Meta Voice Transcribe)",
        )
    )
    c.sidecar["srt"] = srt
    c.sidecar["ass"] = ass
    if op.get("burn", True):
        target = srt if op.get("format", "srt") == "srt" else ass
        c.add_vf(burn_filter(target, style=str(op.get("style", "lower-third"))))


def _overlay(c: Compiler, op: dict[str, Any]) -> None:
    overlay = op.get("source") or op.get("image") or op.get("file")
    if not overlay:
        raise CompileError("overlay requires source")
    if is_image(c.resolve_source(overlay)) and any(key in op for key in ("position", "start", "duration")):
        _timed_image(c, op, c.resolve_source(overlay))
        return
    c.materialize("pre_overlay")
    base = c._require_current()
    over = c.resolve_source(overlay)
    hold = c.duration if c.duration else 10.0
    out = c._next("overlay")
    x = op.get("x", 40)
    y = op.get("y", 40)
    opacity = float(op.get("opacity", 1.0))
    scale = float(op.get("scale", 1.0))
    argv = c._ffmpeg() + ["-i", str(base)]
    if is_image(over):
        argv += ["-loop", "1", "-t", f"{hold:.3f}", "-i", str(over)]
    else:
        argv += ["-i", str(over)]
    logo = f"[1:v]scale=iw*{scale}:ih*{scale},format=rgba,colorchannelmixer=aa={opacity}[ov]"
    filt = f"{logo};[0:v][ov]overlay={x}:{y}:format=auto[v]"
    argv += ["-filter_complex", filt, "-map", "[v]"]
    if c.has_audio:
        argv += ["-map", "0:a"]
    argv += c._encode() + [str(out)]
    c._pipeline.add(Step("overlay", argv, [str(out)], f"overlay {over.name} at {x},{y}"))
    c.set_current(out)


def _watermark(c: Compiler, op: dict[str, Any]) -> None:
    op = {**op, "x": op.get("x", "w-overlay_w-40"), "y": op.get("y", "40"), "opacity": op.get("opacity", 0.85), "scale": op.get("scale", 0.12)}
    _overlay(c, op)


def _pip(c: Compiler, op: dict[str, Any]) -> None:
    if "source" not in op:
        raise CompileError("pip requires source")
    op = {
        **op,
        "x": op.get("x", "w-overlay_w-48"),
        "y": op.get("y", "h-overlay_h-48"),
        "scale": op.get("scale", 0.28),
        "opacity": op.get("opacity", 1.0),
    }
    _overlay(c, op)


def _music(c: Compiler, op: dict[str, Any]) -> None:
    """Lay a bed under the tape: looped, offset, faded, and ducked under speech.

    The tape's own sound is optional. A clip with none, or with ``mute_source``,
    gets the bed alone. Levels are measured at render time, so a quiet voice
    still ducks the music and a quiet bed still sits where ``volume_db`` says.
    """
    src = op.get("source") or op.get("file")
    if not src:
        raise CompileError("music requires source")
    base = c._require_current()
    music = c.resolve_source(src)
    if not music.is_file():
        raise CompileError(f"music file not found: {music}")
    db = _finite_number(op.get("volume_db", op.get("db", -18)), "music volume")
    duck = bool(op.get("duck", True))
    fade = _finite_number(op.get("fade_out", 1.5), "music fade")
    start = _finite_number(op.get("start", 0.0), "music start")
    if fade < 0 or start < 0:
        raise CompileError("music fade and start must not be negative")
    track = probe(music)
    if not track.has_audio:
        raise CompileError(f"{music.name} has no sound to use as music")
    length = track.duration
    if length > 0:
        start %= length
    with_source = c.has_audio and not op.get("mute_source")
    # Pause trimming sets the tape's length during the render, so here the bed
    # simply runs until the speech ends.
    duration = c.duration if c.duration and "keeps" not in c.sidecar else None
    if duration is None and fade > 0:
        c.warn("The music could not fade out, because trimming pauses sets the final length during the render.")
    rate = c.canvas.sample_rate
    samples = round(duration * rate) if duration else 0
    # Looping the input restarts the decoder, and AAC and MP3 open with a silent
    # frame: a 10 ms hole at every repeat. Looping decoded samples has no seam.
    looped = length > 0 and (duration is None or start + duration > length)
    c.seq += 1
    commands = c.work_dir / f"{c.seq:02d}_music.cmd"
    measure = [sys.executable, "-m", "vedit.sound", "--bed", str(music), "--out", str(commands)]
    live = has_filter("asendcmd")
    if live:
        c.pipeline.add(Step("music_measure", (measure + ["--in", str(base)]) if with_source and duck else measure, [str(commands)], "measure the music and the voice"))
    vf, af = c.take_filters()
    bed = [f"asendcmd=f={filter_value(commands)}" if live else "", af_norm(c.canvas)]
    first = round(start * rate)
    if looped:
        bed.append(f"aloop=loop=-1:size={round(length * rate)}:start=0")
        bed.append(f"atrim=start_sample={first}" + (f":end_sample={first + samples}" if samples else ""))
    elif samples:
        bed.append(f"atrim=end_sample={samples}")
    bed += ["asetpts=N/SR/TB", "volume@music_level=volume=1", f"volume={db}dB"]
    if fade > 0 and duration:
        # Equal-power, so the bed thins out instead of dropping away at the end.
        bed.append(f"afade=t=out:st={max(0.0, duration - min(fade, duration)):.6f}:d={min(fade, duration):.6f}:c=qsin")
    # The track a player would play: a file can hold commentary or a silent track first.
    parts = [f"[1:a:{track.audio_map}]{_join(bed)}[mus]"]
    if not with_source:
        parts.append("[mus]anull[mixed]")
        desc = f"{music.name} at {db:g} dB, on its own"
    else:
        parts.append(f"[0:a]{af + ',' if af else ''}{af_norm(c.canvas)}[speech]")
        if duck and has_filter("sidechaincompress"):
            parts += ["[speech]asplit=2[voice][key]", "[key]volume@duck_key=volume=1[level]", f"[mus][level]{duck_filter()}[ducked]",
                      "[voice][ducked]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[mixed]"]
            desc = f"duck {music.name} under speech ({db:g} dB)"
        else:
            if duck:
                c.warn("This FFmpeg cannot duck music under speech, so the music plays at a steady level.")
            parts.append("[speech][mus]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[mixed]")
            desc = f"mix {music.name} at {db:g} dB"
    # Voice plus music can pass full scale, and AAC would turn that into clicks.
    parts.append("[mixed]alimiter=limit=0.97:level=0:latency=1" + (f",atrim=end_sample={samples}" if samples else "") + "[a]")
    out = c._next("music")
    argv = c._ffmpeg() + ["-i", str(base)] + ([] if looped else ["-ss", f"{start:.6f}"]) + ["-i", str(music)]
    if vf:
        parts.insert(0, f"[0:v]{vf}[v]")
        argv += ["-filter_complex", ";".join(parts), "-map", "[v]", "-map", "[a]"] + c._encode()
    else:
        argv += ["-filter_complex", ";".join(parts), "-map", "0:v?", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k"]
    if not samples:
        argv.append("-shortest")
    argv.append(str(out))
    c._pipeline.add(Step("music", argv, [str(out)], desc))
    c.set_current(out)
    c.has_audio = True
    c.info = None


def _replace_audio(c: Compiler, op: dict[str, Any]) -> None:
    src = op.get("source") or op.get("file")
    if not src:
        raise CompileError("replace_audio requires source")
    base = c._require_current()
    audio = c.resolve_source(src)
    out = c._next("replace_audio")
    vf, af = c.take_filters()
    argv = c._ffmpeg() + ["-i", str(base), "-i", str(audio)]
    if vf:
        graph = f"[0:v]{vf}[v]"
        argv += ["-filter_complex", graph, "-map", "[v]", "-map", "1:a", "-shortest"] + c._encode()
    else:
        argv += ["-map", "0:v?", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest"]
    argv.append(str(out))
    c._pipeline.add(Step("replace_audio", argv, [str(out)], f"replace audio with {audio.name}"))
    c.set_current(out)
    c.has_audio = True
    _ = af


def _extract_audio(c: Compiler, op: dict[str, Any]) -> None:
    c.materialize("pre_extract_audio")
    src = c._require_current()
    dest = Path(op["path"]) if op.get("path") else c.work_dir / f"{c.brief.name}.wav"
    argv = c._ffmpeg() + ["-i", str(src), "-vn", "-ac", "2", "-ar", "48000", str(dest)]
    c._pipeline.add(Step("extract_audio", argv, [str(dest)], f"extract audio → {dest.name}"))
    c.finals.append(dest)


def _canvas_for(c: Compiler, shape: tuple[int, int]) -> tuple[int, int]:
    """The canvas size when it already has this shape, so a 720p or 4K export is drawn at its own size."""
    width, height = c.canvas.width, c.canvas.height
    if width and height and abs(width / height - shape[0] / shape[1]) < 0.005:
        return width, height
    return shape


def _reframe(c: Compiler, op: dict[str, Any]) -> None:
    aspect = str(op.get("aspect", op.get("ratio", "9:16")))
    mode = str(op.get("mode", "fill"))
    mapping = {
        "9:16": (1080, 1920),
        "16:9": (1920, 1080),
        "1:1": (1080, 1080),
        "4:5": (1080, 1350),
        "21:9": (2560, 1080),
    }
    if aspect not in mapping:
        raise CompileError(f"unknown aspect '{aspect}'")
    w, h = _canvas_for(c, mapping[aspect])
    c.canvas = Canvas(width=w, height=h, fps=c.canvas.fps, sample_rate=c.canvas.sample_rate)
    if mode == "fit":
        c.add_vf(vf_fit(c.canvas))
    else:
        c.add_vf(vf_fill(c.canvas))


def _tracking_expression(points: list[tuple[float, float]], time: str = "t") -> str:
    """Continuous linear interpolation with a constant value outside the track.

    A sum of bounded ramps avoids deeply nested if() expressions and never
    extrapolates a subject beyond its first or last observation.
    """
    terms = [f"{points[0][1]:.8f}"]
    for (start, value), (end, next_value) in zip(points, points[1:]):
        delta = next_value - value
        if abs(delta) < 1e-9:
            continue
        terms.append(f"({delta:.8f})*clip(({time}-{start:.6f})/{end-start:.6f},0,1)")
    # Parenthesizing a balanced tree also bounds FFmpeg expression recursion.
    while len(terms) > 1:
        terms = [f"({terms[i]}+{terms[i + 1]})" if i + 1 < len(terms) else terms[i]
                 for i in range(0, len(terms), 2)]
    return terms[0]


def _track_reframe(c: Compiler, op: dict[str, Any]) -> None:
    """Follow a chosen subject using normalized source-space bounding boxes."""
    c._require_current()
    if not c.has_video:
        raise CompileError("track_reframe requires a video stream")
    aspect = str(op.get("aspect", "9:16"))
    mapping = {"9:16": (1080, 1920), "16:9": (1920, 1080),
               "1:1": (1080, 1080), "4:5": (1080, 1350), "21:9": (2560, 1080)}
    if aspect not in mapping:
        raise CompileError(f"unknown tracking aspect '{aspect}'")
    raw = op.get("points")
    if not isinstance(raw, list) or not 1 <= len(raw) <= 512:
        raise CompileError("track_reframe requires 1–512 matched subject points")
    x_points: list[tuple[float, float]] = []
    y_points: list[tuple[float, float]] = []
    previous = -1.0
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise CompileError(f"tracking points[{index}] must be a bounding box")
        values = {key: _finite_number(item.get(key), f"points[{index}].{key}")
                  for key in ("time", "x", "y", "width", "height")}
        time, x, y, width, height = (values[key] for key in ("time", "x", "y", "width", "height"))
        if time < 0 or time - previous < 0.000001:
            raise CompileError("tracking times must be non-negative and strictly increasing")
        if c.duration is not None and time > c.duration + 0.001:
            raise CompileError(f"tracking point {index} exceeds the {c.duration:.3f}s clip")
        if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1.000001 or y + height > 1.000001:
            raise CompileError(f"tracking point {index} must be a positive box inside normalized 0–1 bounds")
        x_points.append((time, min(1.0, x + width / 2)))
        y_points.append((time, min(1.0, y + height / 2)))
        previous = time
    width, height = _canvas_for(c, mapping[aspect])
    ratio = width / height
    x_expr, y_expr = _tracking_expression(x_points), _tracking_expression(y_points)
    # Compute dimensions against the actual filter input, not stale probe data:
    # earlier ops may have changed geometry without materializing a file yet.
    crop = (f"crop=w='max(2,trunc(min(iw,ih*{ratio:.9f})/2)*2)'"
            f":h='max(2,trunc(min(ih,iw/{ratio:.9f})/2)*2)'"
            f":x='clip(iw*({x_expr})-ow/2,0,iw-ow)'"
            f":y='clip(ih*({y_expr})-oh/2,0,ih-oh)'")
    c.canvas = Canvas(width=width, height=height, fps=c.canvas.fps, sample_rate=c.canvas.sample_rate)
    c.add_vf(crop, f"scale={width}:{height}", "setsar=1", "format=yuv420p")


def _spatial_effect(c: Compiler, op: dict[str, Any]) -> None:
    """Animate a flat footage plane; preserve the original outside the window."""
    src = c._require_current()
    if not c.has_video:
        raise CompileError("Spatial effects require video footage.")
    try:
        effect = validate_effect(op, clip_duration=c.duration)
    except EffectError as exc:
        raise CompileError(str(exc)) from exc
    if effect is None or effect["intensity"] == 0:
        return
    missing = [name for name in REQUIRED_EFFECT_FILTERS if not has_filter(name)]
    if missing:
        raise CompileError("Spatial effects need FFmpeg filters: " + ", ".join(missing) + ". Install ffmpeg-full.")
    fps = _finite_number(c.canvas.fps, "effect frame rate")
    if fps <= 0:
        raise CompileError("Spatial effects require a positive frame rate.")
    start, duration, intensity = effect["start"], effect["duration"], effect["intensity"]
    # perspective exposes W/H and one-based in/on frame counters, not t.
    # Normalize to the project's CFR before translating the counter to time.
    phase = f"clip(((on-1)/{fps:.8f}-{start:.8f})/{duration:.8f},0,1)"
    envelope = f"({intensity:.8f}*pow(sin(PI*({phase})),2))"
    wave = f"sin(2*PI*({phase}))"
    if effect["preset"] == "tilt":
        margin = f"0.12*{envelope}"
        dx = f"0.018*{envelope}*{wave}"
        dy = "0"
        skew = f"0.075*{envelope}*cos(2*PI*({phase}))"
        roll = "0"
    elif effect["preset"] == "float":
        margin = f"0.13*{envelope}"
        dx = f"0.018*{envelope}*{wave}"
        dy = f"-0.028*{envelope}*sin(PI*({phase}))"
        skew = f"0.008*{envelope}*{wave}"
        roll = f"0.018*{envelope}*{wave}"
        if effect.get("points"):
            # Drift the lifted plane toward the tracked subject center. The
            # 0.2 gain keeps the offset within the envelope's 0.13 margin.
            clock = f"((on-1)/{fps:.8f})"
            track = [(p["time"], p["x"] + p["width"] / 2, p["y"] + p["height"] / 2) for p in effect["points"]]
            cx = _tracking_expression([(time, x) for time, x, _ in track], clock)
            cy = _tracking_expression([(time, y) for time, _, y in track], clock)
            dx = f"{dx}+0.2*{envelope}*(({cx})-0.5)"
            dy = f"{dy}+0.2*{envelope}*(({cy})-0.5)"
    else:
        margin = f"0.23*{envelope}*(1-0.65*({phase}))"
        dx = f"-0.025*{envelope}*(1-({phase}))"
        dy = "0"
        skew = f"0.06*{envelope}*(1-({phase}))"
        roll = "0"
    corners = {
        "x0": f"W*({margin}+({dx})+({roll}))",
        "y0": f"H*({margin}+({dy})+({skew}))",
        "x1": f"W*(1-({margin})+({dx})+({roll}))",
        "y1": f"H*({margin}+({dy})-({skew}))",
        "x2": f"W*({margin}+({dx})-({roll}))",
        "y2": f"H*(1-({margin})+({dy})-({skew}))",
        "x3": f"W*(1-({margin})+({dx})-({roll}))",
        "y3": f"H*(1-({margin})+({dy})+({skew}))",
    }
    window = f"between(t,{start:.8f},{start + duration:.8f})"
    transform = "perspective=" + ":".join(f"{key}='{value}'" for key, value in corners.items())
    transform += f":sense=destination:eval=frame:interpolation=linear:enable='{window}'"
    video, audio = c.take_filters()
    normalized = _join([video, f"fps={fps:.8f}", "setsar=1", "format=yuv420p"])
    graph = [
        f"[0:v:0]{normalized},split=3[original][backdrop][surface]",
        # The lifted plane floats over a blurred, darkened copy of the same frame,
        # the usual vertical-short treatment, instead of a flat slate card.
        "[backdrop]boxblur=luma_radius=min(w\\,h)/40:luma_power=2:chroma_radius=min(cw\\,ch)/40:chroma_power=2,"
        "eq=brightness=-0.12:saturation=0.85[background]",
        # The transparent outer pixels are essential: perspective clamps
        # sampling at input edges, so opaque edges would otherwise smear.
        "[surface]format=yuva420p,pad=iw+4:ih+4:2:2:color=0xe5eaf2,"
        "pad=iw+8:ih+8:4:4:color=black@0," + transform + ",crop=iw-12:ih-12:6:6,split=2[face][shadow_source]",
        "[shadow_source]lut=y=16:u=128:v=128:a='val*0.4',"
        "boxblur=lr=0:lp=0:cr=0:cp=0:ar='min(12,min(w,h)/50)':ap=2[shadow]",
        "[background][shadow]overlay=x=6:y=10:format=auto:shortest=1[stage]",
        "[stage][face]overlay=x=0:y=0:format=auto:shortest=1,format=yuv420p[treatment]",
        f"[original][treatment]overlay=x=0:y=0:format=auto:shortest=1:enable='{window}',format=yuv420p[v]",
    ]
    out = c._next("spatial_effect")
    graph_file = out.with_suffix(".filter.txt")
    graph_text = ";\n".join(graph)
    graph_file.write_text(graph_text + "\n", encoding="utf-8")
    argv = c._ffmpeg() + ["-i", str(src)]
    argv += (["-filter_complex", graph_text] if len(graph_text) < 24000
             else filter_file_args(graph_file))
    argv += ["-map", "[v]"]
    if c.has_audio:
        argv += ["-map", "0:a:0"]
        if audio:
            argv += ["-af", audio]
    else:
        argv += ["-an"]
    argv += c._encode()
    # Retain encoded audio without another lossy pass when its codec fits MP4.
    if c.has_audio and not audio and (c.info is None or c.info.audio and c.info.audio.codec_name in {"aac", "mp3", "ac3", "eac3", "alac"}):
        argv += ["-c:a", "copy"]
    argv += [str(out)]
    c.pipeline.add(Step("spatial_effect", argv, [str(out)], f"{effect['preset']} motion from {start:g}s for {duration:g}s"))
    c.set_current(out)
    c.info = None


def _limited_yuv(color: str) -> tuple[int, int, int]:
    """A ``#RRGGBB`` color as limited-range BT.709, the matrix every export is tagged with."""
    red, green, blue = (int(color[at:at + 2], 16) / 255 for at in (1, 3, 5))
    luma = 0.2126 * red + 0.7152 * green + 0.0722 * blue
    return round(16 + 219 * luma), round(128 + 224 * (blue - luma) / 1.8556), round(128 + 224 * (red - luma) / 1.5748)


def _cutout_blur(blur: float) -> list[str]:
    """Blur the tape with the subject taken out, so its color cannot glow around it.

    Premultiplying by "not the subject" and blurring color and alpha alike is a
    normalized blur: each pixel averages only background. Inside the hole that
    average means nothing, and the subject covers it.
    """
    size = f"{0.004 + 0.04 * blur:.6f}"
    luma, chroma = f"'max(1,trunc(min(w,h)*{size}))'", f"'max(1,trunc(min(cw,ch)*{size}))'"
    return [
        "[sized]split[matte][outline]",
        f"[outline]lut=y='if(gt(val,{CUTOUT_HOLE}),0,255)'[hole]",
        "[tape]format=yuva444p[plate]",
        "[plate][hole]alphamerge,premultiply=inplace=1,"
        f"boxblur=lr={luma}:lp=2:cr={chroma}:cp=2:ar={luma}:ap=2,unpremultiply=inplace=1,format=yuv444p[behind]",
    ]


def _cutout_edge(tape: tuple[int, int] | None, matte: Path) -> str:
    """The curve from SAM's soft alpha to the finished edge.

    It starts above zero, which pulls the edge inside the subject so its outer
    pixels stop carrying the old background. A tape larger than the matte
    stretches SAM's soft band with it, so the curve steepens to match: a smooth
    ramp cut steeply stays a clean contour, and the edge stays a few pixels wide
    at any size. The base curve suits a tape 4/3 of the matte, the usual 720p clip.
    """
    info = probe(matte)
    scale = max(tape) / max(info.width, info.height) if tape and info.width and info.height else 1.0
    span = max(CUTOUT_EDGE_TIGHTEST, round(CUTOUT_EDGE_SPAN / max(1.0, scale * 0.75)))
    return f"lut=y='clip((val-{CUTOUT_EDGE_CENTER - span // 2})*255/{span},0,255)'"


def _cutout_backdrop(mode: str, color: str) -> str:
    """The filter that turns the tape into what stands behind the subject."""
    if mode == "dim":
        keep = f"128+(val-128)*{CUTOUT_DIM_CHROMA}"
        return f"lutyuv=y='16+(val-16)*{CUTOUT_DIM}':u='{keep}':v='{keep}'"
    if mode == "mono":
        return "lutyuv=u=128:v=128"
    luma, blue, red = _limited_yuv(color)
    return f"lutyuv=y={luma}:u={blue}:v={red}"


def _cutout(c: Compiler, op: dict[str, Any]) -> None:
    """Change everything except the subject, from a grayscale matte on this tape's clock.

    The tape stays the length authority: a matte that runs short holds its last
    frame and one that runs long is cut off. Every stream is normalized to the
    canvas rate first, so matte frame N lands on tape frame N exactly.
    Compositing stays in Y'CbCr, so the subject's color never takes a trip
    through RGB and cannot pick up a matrix mismatch.
    """
    src = c._require_current()
    if not c.has_video:
        raise CompileError("A cutout needs a video stream.")
    mode = op.get("mode")
    if mode not in CUTOUT_MODES:
        raise CompileError("cutout mode must be one of: " + ", ".join(CUTOUT_MODES))
    missing = [name for name in REQUIRED_CUTOUT_FILTERS if not has_filter(name)]
    if missing:
        raise CompileError("Cutouts need FFmpeg filters: " + ", ".join(missing) + ". Install ffmpeg-full.")
    if not op.get("matte"):
        raise CompileError("cutout requires a matte")
    matte = c.resolve_source(op["matte"])
    if not matte.is_file():
        raise CompileError(f"The cutout matte does not exist: {matte}")
    blur = _finite_number(op.get("blur", 0.6), "cutout blur")
    if not 0 <= blur <= 1:
        raise CompileError("cutout blur must be between 0 and 1")
    color = op.get("color", "#101418")
    if not isinstance(color, str) or not COLOR.fullmatch(color):
        raise CompileError("cutout color must look like #101418")
    image: Path | None = None
    if mode == "image":
        if not op.get("image"):
            raise CompileError("The image cutout requires an image")
        image = c.resolve_source(op["image"])
        if not image.is_file():
            raise CompileError(f"The cutout background does not exist: {image}")
    fps = _finite_number(c.canvas.fps, "cutout frame rate")
    if not c.source_size or min(c.source_size) < 1:
        raise CompileError("Cutouts need the source frame size to scale their matte and background.")
    frame_width, frame_height = c.source_size
    video, audio = c.take_filters()
    rate = f"fps={fps:.8f}"
    graph = [
        f"[0:v:0]{_join([video, rate, 'setsar=1', 'format=yuv444p'])},split[picture][tape]",
        f"[1:v:0]{rate},format=gray[raw]",
        f"[raw]scale={frame_width}:{frame_height}:flags=bicubic[sized]",
        "[picture]format=yuva444p[solid]",
    ]
    if mode == "blur":
        graph += _cutout_blur(blur)
    else:
        graph.append("[sized]null[matte]")
    graph += [f"[matte]{_cutout_edge(c.source_size, matte)}[alpha]", "[solid][alpha]alphamerge[cut]"]
    if image is not None:
        # One still, covering the frame: the first overlay holds it for the whole tape.
        graph += [
            "[2:v:0]setsar=1,scale=out_color_matrix=bt709:out_range=tv,format=yuv444p[still]",
            f"[still]scale=w='ceil(max({frame_width},{frame_height}*iw/ih))+2':h='ceil(max({frame_height},{frame_width}*ih/iw))+2':flags=lanczos[cover]",
            "[tape]null[frame]",
            "[frame][cover]overlay=x=(W-w)/2:y=(H-h)/2:format=yuv444[behind]",
        ]
    elif mode != "blur":
        graph.append(f"[tape]{_cutout_backdrop(mode, color)}[behind]")
    graph.append("[behind][cut]overlay=format=yuv444:shortest=1,format=yuv420p[v]")
    out = c._next("cutout")
    graph_file = out.with_suffix(".filter.txt")
    graph_text = ";\n".join(graph)
    graph_file.write_text(graph_text + "\n", encoding="utf-8")
    argv = c._ffmpeg() + ["-i", str(src), "-i", str(matte)]
    if image is not None:
        argv += ["-i", str(image)]
    argv += ["-filter_complex", graph_text, "-map", "[v]"]
    if c.has_audio:
        argv += ["-map", "0:a:0"]
        if audio:
            argv += ["-af", audio]
    else:
        argv += ["-an"]
    argv += c._encode()
    if c.has_audio and not audio and (c.info is None or c.info.audio and c.info.audio.codec_name in {"aac", "mp3", "ac3", "eac3", "alac"}):
        argv += ["-c:a", "copy"]
    argv += [str(out)]
    c.pipeline.add(Step("cutout", argv, [str(out)], f"{mode} cutout from {matte.name}"))
    c.set_current(out)
    c.info = None


def _hero_window(c: Compiler, op: dict[str, Any], fps: float) -> tuple[int, int]:
    """The hero word's ``[first, last)`` frames on the tape's clock, checked against the clip."""
    start = _finite_number(op.get("start"), "hero word start")
    end = _finite_number(op.get("end"), "hero word end")
    if start < 0 or not HERO_MIN_SECONDS - 1e-6 <= end - start <= HERO_MAX_SECONDS + 1e-6:
        raise CompileError(f"A hero word shows for {HERO_MIN_SECONDS:g} to {HERO_MAX_SECONDS:g} seconds, from a time inside the clip.")
    if c.duration is not None and end > c.duration + 0.5 / fps:
        raise CompileError(f"The hero word ends at {end:.3f}s, after the {c.duration:.3f}s clip.")
    return round(start * fps), round(end * fps)


def _hero_word(c: Compiler, op: dict[str, Any]) -> None:
    """One huge word behind the subject for a moment: the background, then the word, then the subject.

    The subject layer is the tape's own picture with the matte as its alpha,
    so laying it back on top changes nothing except where the word was drawn:
    the subject hides the part of the word it stands in front of. The matte
    covers only the window and is placed at the window's first frame. Outside
    the window there is no text event and the overlay is switched off, so
    those frames pass through as they were. Nothing here changes timing.
    """
    src = c._require_current()
    if not c.has_video:
        raise CompileError("A hero word needs a video stream.")
    missing = [name for name in REQUIRED_HERO_FILTERS if not has_filter(name)]
    if missing:
        raise CompileError("Hero words need FFmpeg filters: " + ", ".join(missing) + ". Install ffmpeg-full.")
    word = op.get("word")
    if not isinstance(word, str) or not word.strip():
        raise CompileError("hero_word requires a word")
    word = " ".join(word.split())
    if len(word) > HERO_MAX_CHARS or len(word.split()) > HERO_MAX_WORDS or not hero_word_ok(word):
        raise CompileError(f"A hero word is at most {HERO_MAX_WORDS} words and {HERO_MAX_CHARS} letters, digits, spaces, apostrophes or hyphens.")
    side, font, color = op.get("side", "center"), op.get("font", "heavy"), str(op.get("color", HERO_COLORS[0])).upper()
    if side not in HERO_SIDES:
        raise CompileError("hero word side must be one of: " + ", ".join(HERO_SIDES))
    if font not in {row[0] for row in HERO_FONTS}:
        raise CompileError("hero word font must be one of: " + ", ".join(row[0] for row in HERO_FONTS))
    if color not in HERO_COLORS:
        raise CompileError("hero word color must be one of: " + ", ".join(HERO_COLORS))
    aspect = op.get("aspect")
    if aspect is not None and aspect not in HERO_ASPECTS:
        raise CompileError(f"unknown hero word aspect '{aspect}'")
    focus = op.get("focus")
    if focus is not None:
        if not isinstance(focus, (list, tuple)) or len(focus) != 2:
            raise CompileError("hero word focus is an [x, y] point in 0–1")
        focus = tuple(_finite_number(value, "hero word focus") for value in focus)
        if not all(0 <= value <= 1 for value in focus):
            raise CompileError("hero word focus is an [x, y] point in 0–1")
    box = op.get("subject_box")
    if box is not None:
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            raise CompileError("hero word subject_box is [x0, y0, x1, y1] in 0–1")
        box = tuple(_finite_number(value, "hero word subject_box") for value in box)
        if not (0 <= box[0] < box[2] <= 1 and 0 <= box[1] < box[3] <= 1):
            raise CompileError("hero word subject_box is [x0, y0, x1, y1] in 0–1")
    if not op.get("matte"):
        raise CompileError("hero_word requires a matte")
    matte = c.resolve_source(op["matte"])
    if not matte.is_file():
        raise CompileError(f"The hero word matte does not exist: {matte}")
    if not c.source_size or min(c.source_size) < 1:
        raise CompileError("Hero words need the source frame size to scale their matte.")
    fps = _finite_number(c.canvas.fps, "hero word frame rate")
    first, last = _hero_window(c, op, fps)
    frame_width, frame_height = c.source_size
    family, face, font_file = hero_font(font, word)
    if font_file is None or str(font_file) not in HERO_FONT_FILES[font]:
        installed = any(Path(candidate).is_file() for candidate in HERO_FONT_FILES[font])
        c.warn(f"The {font} hero word font cannot draw these letters, so the word is drawn in {family}." if installed
               else f"The {font} hero word font is not on this machine, so the word is drawn in {family}.")
    # Half a frame either side of the window, so neither layer can catch a neighbouring frame.
    shown, hidden = (first - 0.5) / fps, (last - 0.5) / fps
    out = c._next("hero_word")
    subtitle = out.with_suffix(".ass")
    text, layout = render_hero_ass(word, start=shown, end=hidden, side=side, color=color, family=family, face=face,
                                   width=frame_width, height=frame_height, visible=visible_rect(frame_width, frame_height, aspect, focus), subject=box)
    subtitle.write_text(text, encoding="utf-8")
    fonts = out.with_name(out.stem + "-fonts")
    fonts.mkdir(parents=True, exist_ok=True)
    if font_file is not None:
        (fonts / font_file.name).write_bytes(font_file.read_bytes())
    c.pipeline.meta.setdefault("hero_words", []).append({"word": word, "first_frame": first, "last_frame": last, "side": side, **layout})
    video, audio = c.take_filters()
    rate = f"fps={fps:.8f}"
    graph = [
        # Both layers carry the same color tags, so FFmpeg never converts one of them on the way into the overlay.
        f"[0:v:0]{_join([video, rate, 'setsar=1', 'format=yuv420p', _bt709()])},split[plate][body]",
        f"[1:v:0]setpts=PTS-STARTPTS+{first / fps:.6f}/TB,{rate},format=gray,scale={frame_width}:{frame_height}:flags=bicubic,{HERO_EDGE}[alpha]",
        "[body]format=yuva420p[solid]",
        "[solid][alpha]alphamerge[subject]",
        f"[plate]ass={filter_value(subtitle)}:fontsdir={filter_value(fonts)}[worded]",
        f"[worded][subject]overlay=format=yuv420:enable='between(t,{shown:.6f},{hidden:.6f})'[v]",
    ]
    graph_text = ";\n".join(graph)
    out.with_suffix(".filter.txt").write_text(graph_text + "\n", encoding="utf-8")
    argv = c._ffmpeg() + ["-i", str(src), "-i", str(matte), "-filter_complex", graph_text, "-map", "[v]"]
    if c.has_audio:
        argv += ["-map", "0:a:0"]
        if audio:
            argv += ["-af", audio]
    else:
        argv += ["-an"]
    argv += c._encode()
    if c.has_audio and not audio and (c.info is None or c.info.audio and c.info.audio.codec_name in {"aac", "mp3", "ac3", "eac3", "alac"}):
        argv += ["-c:a", "copy"]
    argv += [str(out)]
    c.pipeline.add(Step("hero_word", argv, [str(out)], f"hero word “{word}” behind the subject, {first / fps:.2f}s to {last / fps:.2f}s"))
    c.set_current(out)
    c.info = None


def _slideshow(c: Compiler, op: dict[str, Any]) -> None:
    images = op.get("images") or op.get("sources")
    if not images:
        raise CompileError("slideshow requires images: [...]")
    hold = float(op.get("duration", 3.0))
    fade = float(op.get("fade", 0.4))
    paths = []
    for item in images:
        path = c.brief.source_path(str(item)) if str(item) in c.brief.sources else Path(str(item)).expanduser().resolve()
        if not path.is_file():
            raise CompileError(f"slideshow image missing: {path}")
        paths.append(path)
    out = c._next("slideshow")
    argv = c._ffmpeg()
    for path in paths:
        argv += ["-loop", "1", "-t", f"{hold + fade:.3f}", "-i", str(path)]
    n = len(paths)
    shape = vf_fill(c.canvas) if op.get("mode") == "fill" else vf_fit(c.canvas)
    parts = [f"[{i}:v]{shape},setpts=PTS-STARTPTS[v{i}]" for i in range(n)]
    if n == 1:
        parts.append("[v0]format=yuv420p[v]")
        last = "v"
        acc = hold
    else:
        acc = hold
        last = "v0"
        for i in range(1, n):
            offset = max(0.0, acc - fade)
            nxt = f"vx{i}"
            parts.append(f"[{last}][v{i}]xfade=transition=fade:duration={fade}:offset={offset:.3f}[{nxt}]")
            last = nxt
            acc = acc + hold - fade
        parts.append(f"[{last}]format=yuv420p[v]")
        last = "v"
    argv += ["-filter_complex", ";".join(parts), "-map", f"[{last}]", "-an"] + c._encode() + [str(out)]
    c._pipeline.add(Step("slideshow", argv, [str(out)], f"slideshow {n} images × {hold}s"))
    c.set_current(out)
    # The generated tape does not exist while planning. Do not retain the
    # still image's zero duration or a previous tape's audio metadata.
    c.duration = acc + fade
    c.has_video = True
    c.has_audio = False
    c.info = None


def _thumbnail(c: Compiler, op: dict[str, Any]) -> None:
    c.pending_thumbs.append({"at": float(op.get("at", 1.0)), "path": op.get("path")})


def _chromakey(c: Compiler, op: dict[str, Any]) -> None:
    color = str(op.get("color", "0x00FF00"))
    similarity = float(op.get("similarity", 0.2))
    blend = float(op.get("blend", 0.1))
    c.add_vf(f"chromakey={color}:{similarity}:{blend}")


def _scenes(c: Compiler, op: dict[str, Any]) -> None:
    src = c.resolve_source(op.get("source")) if op.get("source") else c._require_current()
    dest = c.work_dir / "scenes.json"
    argv = [
        sys.executable,
        "-m",
        "vedit.analyze",
        "scenes",
        "--in",
        str(src),
        "--out",
        str(dest),
        "--threshold",
        str(float(op.get("threshold", 0.35))),
    ]
    c._pipeline.add(Step("scenes", argv, [str(dest)], "scene detect at render"))
    c.sidecar["scenes"] = dest


def _project_source(c: Compiler, op: dict[str, Any]) -> None:
    from vedit.project_media import compile_project_source

    compile_project_source(c, op)


def _look(c: Compiler, op: dict[str, Any]) -> None:
    """Grade the tape with one LUT, then sharpen, vignette, and grain. See ``vedit.looks``."""
    if not c.has_video:
        raise CompileError("A look needs a video stream.")
    settings = {key: op[key] for key in ("preset", "intensity", "brightness", "contrast", "saturation", "temperature", "vignette", "sharpen") if key in op}
    for key, value in settings.items():
        if key != "preset":
            _finite_number(value, f"look {key}")
    preset = str(settings.get("preset", "none"))
    if preset not in RECIPES:
        raise CompileError(f"unknown look '{preset}'. Known: {', '.join(sorted(RECIPES))}")
    if is_neutral(settings):
        return
    steps = color_recipes(settings)
    if preset == "auto" or steps:
        if has_filter("lut3d"):
            c.seq += 1
            cube = c.work_dir / f"{c.seq:02d}_look.cube"
            if preset == "auto":
                # The tape does not exist until the render reaches it, and the
                # ffmpeg step only names the file the measurement leaves behind.
                argv = [sys.executable, "-m", "vedit.looks", "--in", str(c._require_current()), "--out", str(cube), "--look", json.dumps(settings, sort_keys=True)]
                c.pipeline.add(Step("look_measure", argv, [str(cube)], "measure exposure and color for the auto look"))
            else:
                write_cube(cube, steps)
            c.add_vf(*lut_filters(cube))
        else:
            c.warn("This FFmpeg has no lut3d filter, so the look is a rough match.")
            c.add_vf(*fallback_filters(steps))
    c.add_vf(*spatial_filters(spatial_amounts(settings), warn=c.warn))


def _zoom(c: Compiler, op: dict[str, Any]) -> None:
    """Two-keyframe moves on the output clock, changing on the exact frame of each cut."""
    if not c.has_video:
        raise CompileError("Zoom needs a video stream.")
    moves = op.get("moves")
    if not isinstance(moves, list) or len(moves) > 256:
        raise CompileError("zoom requires a list of up to 256 moves")
    fps = _finite_number(c.canvas.fps, "zoom frame rate")
    try:
        blend = _finite_number(op.get("blend", 0.0), "zoom blend")
        move = zoom_filter(plan_pieces(moves, fps, blend=blend, end=c.duration))
    except MotionError as exc:
        raise CompileError(str(exc)) from exc
    if move is None:
        return
    if not has_filter("perspective"):
        c.warn("This FFmpeg has no perspective filter, so the zoom was skipped.")
        return
    # Frames are counted from here, so the tape has to be constant rate.
    c.add_vf(f"fps={fps}", move)


def _stabilize(c: Compiler, op: dict[str, Any]) -> None:
    """Two passes of vid.stab. Detection runs small, and its motions are scaled back up."""
    if not c.has_video:
        raise CompileError("Stabilizing needs a video stream.")
    seconds = _finite_number(op.get("smoothing", 1.0), "stabilize smoothing")
    if not 0 < seconds <= 10:
        raise CompileError("stabilize smoothing must be between 0 and 10 seconds")
    if not (has_filter("vidstabdetect") and has_filter("vidstabtransform")):
        if has_filter("deshake"):
            c.warn("This FFmpeg has no vid.stab, so a simpler stabilizer was used. Install ffmpeg-full for the steadier one.")
            c.add_vf("deshake=rx=32:ry=32:edge=mirror")
        else:
            c.warn("This FFmpeg has no stabilizer, so the footage was left as shot.")
        return
    c.materialize("pre_stabilize")
    src = c._require_current()
    width, height = _frame_size(c)
    factor = max(1, round(min(width, height) / 540))
    c.seq += 1
    raw = c.work_dir / f"{c.seq:02d}_stabilize.small.trf"
    transforms = raw.with_name(f"{c.seq:02d}_stabilize.trf")
    shrink = f"scale=iw/{factor}:-2:flags=bilinear," if factor > 1 else ""
    # Text transforms, which older builds always write and have no option for.
    fileformat = ":fileformat=ascii" if "fileformat" in filter_options("vidstabdetect") else ""
    detect = f"{shrink}vidstabdetect=result={filter_value(raw)}{fileformat}:shakiness=6:accuracy=9:stepsize=6:mincontrast=0.2"
    c.pipeline.add(Step("stabilize_detect", c._ffmpeg() + ["-i", str(src), "-map", "0:v:0", "-vf", detect, "-f", "null", "-"], [str(raw)], "find the camera shake"))
    c.pipeline.add(Step("stabilize_scale", [sys.executable, "-m", "vedit.motion", "--in", str(raw), "--out", str(transforms), "--factor", str(factor)],
                        [str(transforms)], "scale the shake to the full frame"))
    frames = max(2, round(seconds * _finite_number(c.canvas.fps, "stabilize frame rate")))
    # A tiled wall or a fence can fool detection. The limits keep a bad reading
    # from zooming into the shot, while real handheld shake stays well inside them.
    limit = max(8, round(min(width, height) * 0.08))
    c.add_vf(f"vidstabtransform=input={filter_value(transforms)}:smoothing={frames}:maxshift={limit}:maxangle=0.08:optzoom=1:interpol=bilinear")


def _voice(c: Compiler, op: dict[str, Any]) -> None:
    """Clean up speech. Its settings are measured from the tape during the render."""
    denoise, enhance = bool(op.get("denoise", True)), bool(op.get("enhance", True))
    if not denoise and not enhance:
        return
    if not c.has_audio:
        c.warn("This video has no sound, so speech cleanup was skipped.")
        return
    c.seq += 1
    commands = c.work_dir / f"{c.seq:02d}_voice.cmd"
    argv = [sys.executable, "-m", "vedit.sound", "--in", str(c._require_current()), "--out", str(commands)]
    argv += (["--denoise"] if denoise else []) + (["--enhance"] if enhance else [])
    c.pipeline.add(Step("voice_measure", argv, [str(commands)], "measure the room and the voice"))
    c.add_af(af_norm(c.canvas), *voice_filters(denoise=denoise, enhance=enhance, commands=commands, rate=c.canvas.sample_rate, warn=c.warn))


def _frame_size(c: Compiler) -> tuple[int, int]:
    """The picture's size at this point in the chain. A pending reframe has already set the canvas."""
    if c.info is not None and c.info.has_video and not any("scale=" in bit or "crop=" in bit for bit in c.vf):
        return c.info.display_width, c.info.display_height
    return c.canvas.width, c.canvas.height


def _bt709() -> str:
    """Burned text and images take their color from the frame, and the export declares BT.709."""
    return "setparams=range=limited:color_primaries=bt709:color_trc=bt709:colorspace=bt709"


def _text(c: Compiler, op: dict[str, Any]) -> None:
    """One styled text item. All items share one ASS file and one filter."""
    if not c.has_video:
        raise CompileError("Text needs a video stream.")
    body = str(op.get("text") or "").strip()
    if not body:
        raise CompileError("text requires text")
    item: dict[str, Any] = {"text": body, "style": str(op.get("style", "label")), "position": str(op.get("position", "center")),
                            "color": str(op.get("color", "#FFFFFF"))}
    if not COLOR.fullmatch(item["color"]):
        # Refuse rather than quietly drawing white: a brief that names a colour should get that colour or an error.
        raise CompileError(f"text color must be #RRGGBB, not {item['color'][:40]!r}")
    for key, fallback in (("start", 0.0), ("duration", c.duration or 3.0), ("scale", 1.0), ("fade", DEFAULT_FADE)):
        item[key] = _finite_number(op.get(key, fallback), f"text {key}")
    if item["start"] < 0 or item["duration"] <= 0 or item["scale"] <= 0 or item["fade"] < 0:
        raise CompileError("text start and fade must not be negative, and its duration and scale must be positive")
    engine = text_engine()
    if not engine:
        c.warn("This FFmpeg cannot draw text, so on-screen text was left out. Install ffmpeg-full.")
        return
    width, height = _frame_size(c)
    c.texts.append(item)
    if engine == "drawtext":
        # Only reached without libass, so there is no ASS path to send right-to-left text to.
        right_to_left = any(unicodedata.bidirectional(char) in ("R", "AL") for char in body)
        if right_to_left and "text_shaping" not in filter_options("drawtext"):
            c.warn("This FFmpeg has no libass, so text uses a simpler renderer with no right-to-left shaping: Arabic or Hebrew may read backwards. Install ffmpeg-full.")
        else:
            c.warn("This FFmpeg has no libass, so text uses a simpler renderer with no right-to-left shaping.")
        c.add_vf(*drawtext_filters([item], work_dir=c.work_dir, width=width, height=height, end_limit=c.duration))
        return
    for message in text_warnings([item]):
        c.warn(message)
    path = _write_texts(c, width, height)
    if len(c.texts) == 1:
        c.add_vf(_bt709(), burn_filter(path))


def _write_texts(c: Compiler, width: int, height: int) -> Path:
    return write_text_ass(c.texts, c.work_dir / "texts.ass", width=width, height=height, end_limit=c.duration, reserve=c.text_reserve)


def _timed_image(c: Compiler, op: dict[str, Any], path: Path) -> None:
    """An image from ``edit.overlays``: placed in the safe area, on the output clock, with a short fade."""
    position = str(op.get("position", "center"))
    width, height = _frame_size(c)
    size = _finite_number(op.get("scale", 0.3), "overlay scale")
    start = _finite_number(op.get("start", 0.0), "overlay start")
    duration = _finite_number(op.get("duration", (c.duration or 3.0) - start), "overlay duration")
    fade = _finite_number(op.get("fade", DEFAULT_FADE), "overlay fade")
    if not 0 < size <= 1 or start < 0 or duration <= 0 or fade < 0:
        raise CompileError("overlay scale must be between 0 and 1, and its start and duration must fit the video")
    if position not in OVERLAY_POSITIONS:
        raise CompileError(f"unknown overlay position '{position}'")
    x, y = corner(position, width=width, height=height)
    c.images += 1
    c.add_vf(_bt709(), image_filter(path, tag=f"image{c.images}", x=x, y=y, width=max(2, round(width * size / 2) * 2),
                                    opacity=_finite_number(op.get("opacity", 1.0), "overlay opacity"), start=start, end=start + duration,
                                    fade=min(fade, duration / 3), fps=c.canvas.fps))


def _logo(c: Compiler, op: dict[str, Any]) -> None:
    """A logo held for the whole video, inside the safe area."""
    source = op.get("source") or op.get("image") or op.get("file")
    if not source:
        raise CompileError("logo requires source")
    path = c.resolve_source(source)
    if not path.is_file() or not is_image(path):
        raise CompileError(f"logo must be an image file: {path}")
    position = str(op.get("position", "top-right"))
    if position not in OVERLAY_POSITIONS:
        raise CompileError(f"unknown logo position '{position}'")
    size = _finite_number(op.get("scale", 0.14), "logo scale")
    opacity = _finite_number(op.get("opacity", 0.9), "logo opacity")
    if not 0 < size <= 1 or not 0 < opacity <= 1:
        raise CompileError("logo scale and opacity must be between 0 and 1")
    width, height = _frame_size(c)
    x, y = corner(position, width=width, height=height)
    c.images += 1
    c.add_vf(_bt709(), image_filter(path, tag=f"image{c.images}", x=x, y=y, width=max(2, round(width * size / 2) * 2), opacity=opacity))
    # Text is planned first, so it moves out of the logo's band here.
    image = probe(path)
    c.text_reserve = logo_reserve(position, image_width=image.width, image_height=image.height, width=width, height=height, scale=size)
    if c.texts and text_engine() == "ass":
        _write_texts(c, width, height)


def _export(c: Compiler, op: dict[str, Any]) -> None:
    src = c._require_current()
    has_video, has_audio = c.has_video, c.has_audio
    src_w, src_h = c.canvas.width, c.canvas.height
    if src.is_file():
        info = probe(src)
        has_video, has_audio = info.has_video, info.has_audio
        src_w, src_h = info.width or src_w, info.height or src_h
    pending_vf, pending_af = c.take_filters()
    loudnorm = c.pending_loudnorm
    fade_out = c.pending_fade_out
    c.pending_loudnorm = None
    c.pending_fade_out = None
    names = op.get("presets") or op.get("preset") or c.brief.presets or ["youtube_1080p"]
    if isinstance(names, str):
        names = [names]
    explicit = Path(op["path"]) if op.get("path") else None
    choice = {key: op[key] for key in ("quality", "fps", "format") if op.get(key) is not None}
    if choice.get("fps") == MATCH_SOURCE:
        choice["fps"] = c.canvas.fps
    # Picture and sound end on the same frame. select_ranges fixed this clock; when no
    # later op changed the length, the export holds both streams to it exactly.
    ending = None
    # The plan's length rounds each cut to a frame, so the two agree within half a frame.
    if c.clock_frames and c.canvas.fps and c.duration is not None and abs(c.duration - c.clock_frames / c.canvas.fps) <= 0.5 / c.canvas.fps + 0.001:
        ending = c.clock_frames / c.canvas.fps
    first_dest: Path | None = None
    for i, name in enumerate(names):
        preset = get_preset(export_preset_name(str(name), **choice))
        if preset.canvas.width and not preset.canvas.fps:
            # A preset named for the source rate, given directly: the canvas already holds that rate.
            preset = get_preset(export_preset_name(str(name), **{**choice, "fps": c.canvas.fps}))
        for encoder in (preset.video_codec, preset.audio_codec):
            if encoder in ("libvpx-vp9", "libopus") and not has_encoder(encoder):
                raise CompileError(f"WebM export needs the {encoder} encoder. Install ffmpeg-full.")
        if c.source_size and preset.canvas.width and min(preset.canvas.width, preset.canvas.height) > min(c.source_size) * 1.02:
            c.warn(f"The source is {c.source_size[0]}x{c.source_size[1]}, so this {preset.canvas.width}x{preset.canvas.height} export is upscaled and gains no detail.")
        if explicit is not None and i == 0:
            dest = explicit
            if not dest.is_absolute():
                dest = c.work_dir / dest
            if choice.get("format"):
                dest = dest.with_suffix(f".{preset.container}")
        else:
            dest = c.work_dir / f"{c.brief.name}_{preset.name}.{preset.container}"
        vf_bits: list[str] = []
        af_bits: list[str] = []
        if i == 0:
            if pending_vf:
                vf_bits.append(pending_vf)
            if pending_af:
                af_bits.append(pending_af)
        if preset.canvas.width and has_video:
            # Intermediate files do not exist during planning; their actual
            # dimensions cannot be inferred from the project canvas alone.
            # Enforce the requested output shape even when the known shape
            # appears equal. Equal-size scale/pad is a no-op.
            mode = str(op.get("reframe", "fill" if preset.canvas.height > preset.canvas.width else "fit"))
            vf_bits.append(vf_fill(preset.canvas) if mode == "fill" else vf_fit(preset.canvas))
            if ending is not None:
                vf_bits += ["tpad=stop=-1:stop_mode=clone", f"trim=end={ending:.6f}"]
        if ending is not None and has_video and has_audio and preset.audio_codec:
            af_bits += [f"apad=whole_dur={ending:.6f}", f"atrim=end={ending:.6f}"]
        if has_video and preset.video_codec and preset.container != "gif":
            # A full-range source has to be converted, not just relabelled, or its
            # blacks and whites are crushed on playback. FFmpeg 9 carries frame color
            # metadata through the encoder; codec flags alone can leave primaries
            # and transfer unspecified.
            vf_bits += ["scale=out_range=limited", "setparams=range=limited:color_primaries=bt709:color_trc=bt709:colorspace=bt709"]
        src_i = src if i == 0 else first_dest
        assert src_i is not None
        argv = _master_argv(
            c,
            src_i,
            dest,
            vf=_join(vf_bits),
            af=_join(af_bits),
            loudnorm=loudnorm if i == 0 else None,
            preset_name=preset.name,
            has_video=has_video and bool(preset.video_codec),
            has_audio=has_audio and bool(preset.audio_codec),
            fade_out=fade_out if i == 0 else None,
        )
        c._pipeline.add(Step(f"export_{preset.name}", argv, [str(dest)], preset.description))
        c.finals.append(dest)
        _verify_export(c, dest, ref=src if i == 0 else first_dest)
        if i == 0:
            first_dest = dest
            c.set_current(dest)
            src_w, src_h = preset.canvas.width or src_w, preset.canvas.height or src_h
    _flush_thumbs(c)



REQUIRED_CARD_FILTERS = ("movie", "setpts", "scale", "format", "overlay")


def card_filter(path: str | Path, *, tag: str, start: float, end: float, width: int, height: int) -> str:
    """Fuse a motion card (a video with alpha) into the chain, on the output clock.

    The card's first frame is placed at ``start``; before it and after the card
    ends the picture passes through untouched, so the clock never moves.
    """
    chain = [f"movie={filter_value(path)}", f"setpts=PTS-STARTPTS+{start:.6f}/TB",
             f"scale={width}:{height}:flags=lanczos:in_color_matrix=bt709:in_range=limited:out_color_matrix=bt709:out_range=limited",
             "format=yuva420p"]
    return (f"null[{tag}m];{','.join(chain)}[{tag}o];"
            f"[{tag}m][{tag}o]overlay=x=0:y=0:format=yuv420:eof_action=pass:enable='between(t,{start:.6f},{end:.6f})'")


def _motion_card(c: Compiler, op: dict[str, Any]) -> None:
    """A 3D motion card over the opening or closing seconds, rendered by Briefcut's Blender template.

    ``card`` is ``edit.motion_card``. When the card for this frame size is not
    cached yet, a Blender step joins the program before the export that reads
    it; ``file`` instead names a card rendered earlier. Without Blender the card
    is left out with a warning. Nothing here changes timing.
    """
    from vedit import blender_cards

    if not c.has_video:
        raise CompileError("A motion card needs a video stream.")
    missing = [name for name in REQUIRED_CARD_FILTERS if not has_filter(name)]
    if missing:
        raise CompileError("Motion cards need FFmpeg filters: " + ", ".join(missing) + ". Install ffmpeg-full.")
    width, height = _frame_size(c)
    fps = _finite_number(c.canvas.fps, "motion card frame rate")
    if op.get("file"):
        path = c.resolve_source(op["file"])
        if not path.is_file():
            raise CompileError(f"The motion card file does not exist: {path}")
        seconds = _finite_number(op.get("duration") or probe(path).duration, "motion card length")
        label, default_start = path.name, 0.0
    else:
        try:
            card = validate_motion_card(op.get("card"))
        except EditError as exc:
            raise CompileError(f"motion_card: {exc}") from None
        if card is None:
            raise CompileError("motion_card requires a card or a file")
        if not blender_cards.blender_available():
            c.warn("Blender is not installed on this machine, so the 3D motion card was left out.")
            return
        backdrop = c.resolve_source(op["backdrop"]) if op.get("backdrop") else None
        try:
            spec = blender_cards.render_spec(card, width=width, height=height, fps=fps, backdrop=backdrop)
        except blender_cards.MotionCardError as exc:
            raise CompileError(str(exc)) from None
        path = blender_cards.card_path(spec)
        if not path.is_file():
            card_json = c._next("motion_card", ".json")
            card_json.write_text(json.dumps(card, indent=2, ensure_ascii=False), encoding="utf-8")
            argv = [sys.executable, "-m", "vedit.blender_cards", "render", "--card", str(card_json), "--width", str(width),
                    "--height", str(height), "--fps", f"{fps:g}", "--out", str(path.parent.parent)]
            if backdrop is not None:
                argv += ["--backdrop", str(backdrop)]
            c.pipeline.add(Step("motion_card", argv, [str(path)], f"render the 3D motion card “{card['text']}” in Blender (Eevee)"))
        seconds, label = card["duration"], card["text"]
        default_start = blender_cards.card_window(card, c.duration)[0] if c.duration is not None else 0.0
    start = _finite_number(op.get("start", default_start), "motion card start")
    if start < 0 or seconds <= 0:
        raise CompileError("A motion card starts at or after 0 s and has a length.")
    if c.duration is not None and start + seconds > c.duration + 0.5 / fps:
        raise CompileError(f"The motion card runs to {start + seconds:.3f}s, past the {c.duration:.3f}s clip.")
    c.images += 1
    c.add_vf(_bt709(), card_filter(path, tag=f"card{c.images}", start=start, end=start + seconds, width=width, height=height))
    c.pipeline.meta.setdefault("motion_cards", []).append({"path": str(path), "text": label, "start": round(start, 4),
                                                           "end": round(start + seconds, 4)})


OPS = {
    "trim": _trim,
    "select_ranges": _select_ranges,
    "cut_silence": _cut_silence,
    "concat": _concat,
    "speed": _speed,
    "reverse": _reverse,
    "freeze": _freeze,
    "fit": _fit,
    "fill": _fill,
    "scale": _scale,
    "crop": _crop,
    "pad": _pad,
    "rotate": _rotate,
    "hflip": _hflip,
    "vflip": _vflip,
    "fps": _fps,
    "color": _color,
    "denoise": _denoise,
    "sharpen": _sharpen,
    "blur": _blur,
    "fade_in": _fade_in,
    "fade_out": _fade_out,
    "volume": _volume,
    "highpass": _highpass,
    "lowpass": _lowpass,
    "loudnorm": _loudnorm,
    "title": _title,
    "captions": _captions,
    "overlay": _overlay,
    "logo": _logo,
    "text": _text,
    "look": _look,
    "zoom": _zoom,
    "stabilize": _stabilize,
    "voice": _voice,
    "watermark": _watermark,
    "pip": _pip,
    "music": _music,
    "replace_audio": _replace_audio,
    "extract_audio": _extract_audio,
    "reframe": _reframe,
    "track_reframe": _track_reframe,
    "spatial_effect": _spatial_effect,
    "cutout": _cutout,
    "hero_word": _hero_word,
    "motion_card": _motion_card,
    "slideshow": _slideshow,
    "thumbnail": _thumbnail,
    "chromakey": _chromakey,
    "scenes": _scenes,
    "project_source": _project_source,
    "export": _export,
}
