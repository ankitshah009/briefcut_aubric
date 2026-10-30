"""Compile a Brief into an inspectable FFmpeg pipeline."""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vedit.captions import burn_filter, title_filter
from vedit.effects import EffectError, REQUIRED_EFFECT_FILTERS, validate_effect
from vedit.errors import CompileError
from vedit.ir import Brief, Canvas
from vedit.media import (
    MediaInfo,
    escape_filter_path,
    has_filter,
    is_image,
    probe,
    resolve_ffmpeg,
)
from vedit.pipeline import Pipeline, Step
from vedit.presets import ExportPreset, encode_args, get_preset


def vf_fit(canvas: Canvas) -> str:
    return (
        f"fps={canvas.fps},"
        f"scale={canvas.width}:{canvas.height}:force_original_aspect_ratio=decrease,"
        f"pad={canvas.width}:{canvas.height}:(ow-iw)/2:(oh-ih)/2:color={canvas.background},"
        f"setsar=1,format={canvas.pixel_format}"
    )


def vf_fill(canvas: Canvas) -> str:
    return (
        f"fps={canvas.fps},"
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
        }

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
        for key in ("main", "talk", "input", "video", "src"):
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


def _select_ranges(c: Compiler, op: dict[str, Any]) -> None:
    """Assemble source-time intervals in their declared editorial order.

    Separate seekable inputs avoid the unbounded buffering a split/concat
    graph would need when the last part of a long recording is shown first.
    Pending tracking filters see original source timestamps before each
    interval is reset to zero for concat.
    """
    src = c._require_current()
    raw = op.get("ranges")
    if not isinstance(raw, list) or not 1 <= len(raw) <= 32:
        raise CompileError("select_ranges requires between 1 and 32 ranges")
    ranges: list[tuple[float, float]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise CompileError(f"select_ranges.ranges[{index}] must contain start and end")
        start = _finite_number(item.get("start"), f"ranges[{index}].start")
        end = _finite_number(item.get("end"), f"ranges[{index}].end")
        if start < 0 or end - start < 0.000001:
            raise CompileError(f"ranges[{index}] requires 0 <= start < end and at least one microsecond")
        if c.duration is not None and end > c.duration + 0.001:
            raise CompileError(f"ranges[{index}].end exceeds the {c.duration:.3f}s clip")
        ranges.append((start, end))
    if not c.has_video and not c.has_audio:
        raise CompileError("select_ranges requires audio or video")

    # These transforms change which source time a requested tape time means.
    # Settle them before input seeking; geometry-only tracking stays fused.
    if any(bit.startswith(("setpts=", "reverse", "tpad=")) for bit in c.vf) or any(
        bit.startswith(("atempo=", "areverse", "apad=")) for bit in c.af
    ):
        c.materialize("pre_select_ranges")
        src = c._require_current()
    video, audio = c.take_filters()
    argv = c._ffmpeg()
    for start, end in ranges:
        argv += ["-ss", f"{start:.6f}", "-t", f"{end - start:.6f}", "-i", str(src)]
    graph: list[str] = []
    labels: list[str] = []
    for index, (start, end) in enumerate(ranges):
        if c.has_video:
            chain = [f"setpts=PTS-STARTPTS+{start:.6f}/TB", video,
                     "setpts=PTS-STARTPTS", f"fps={c.canvas.fps}", "setsar=1", "format=yuv420p"]
            graph.append(f"[{index}:v:0]" + _join(chain) + f"[rv{index}]")
            labels.append(f"[rv{index}]")
        if c.has_audio:
            chain = [f"asetpts=PTS-STARTPTS+{start:.6f}/TB", audio,
                     "asetpts=PTS-STARTPTS", af_norm(c.canvas)]
            graph.append(f"[{index}:a:0]" + _join(chain) + f"[ra{index}]")
            labels.append(f"[ra{index}]")
    outputs = ("[v]" if c.has_video else "") + ("[a]" if c.has_audio else "")
    graph.append("".join(labels) + f"concat=n={len(ranges)}:v={int(c.has_video)}:a={int(c.has_audio)}" + outputs)
    out = c._next("select_ranges")
    # Dense tracks repeated across source ranges can exceed the OS argument
    # limit. The filter program remains a readable artifact beside the plan.
    graph_file = out.with_suffix(".filter.txt")
    graph_text = ";\n".join(graph)
    graph_file.write_text(graph_text + "\n", encoding="utf-8")
    # Small graphs remain compatible with older FFmpeg releases. Dense
    # graphs use FFmpeg's current file-backed option syntax.
    argv += (["-filter_complex", graph_text] if len(graph_text) < 24000
             else ["-/filter_complex", str(graph_file)])
    if c.has_video:
        argv += ["-map", "[v]"]
    if c.has_audio:
        argv += ["-map", "[a]"]
    argv += c._encode() + [str(out)]
    c.pipeline.add(Step("select_ranges", argv, [str(out)], f"assemble {len(ranges)} selected moments"))
    c.set_current(out)
    c.info = None
    c.duration = sum(end - start for start, end in ranges)


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
    threshold = float(op.get("threshold_db", -32))
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
        "--threshold-db",
        str(threshold),
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
            f"drop silence < {threshold} dB (pad {pad}s) at render",
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
    c.add_vf(title_filter(text, c.canvas.height, y_ratio=float(op.get("y_ratio", 0.78)), size=int(op.get("size", 56))))


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
    if op.get("source"):
        src = c.resolve_source(op.get("source"))
    elif c.current is not None:
        src = c.current
    else:
        raise CompileError("captions needs a current clip or source=")
    language = str(op.get("language", "en"))
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
            f"transcribe {src.name} → SRT/ASS (local whisper)",
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
    src = op.get("source") or op.get("file")
    if not src:
        raise CompileError("music requires source")
    base = c._require_current()
    music = c.resolve_source(src)
    if not c.has_audio:
        raise CompileError("music duck needs speech audio on the current clip")
    out = c._next("music")
    db = float(op.get("volume_db", op.get("db", -18)))
    duck = bool(op.get("duck", True))
    vf, af = c.take_filters()
    parts: list[str] = []
    if af:
        parts.append(f"[0:a]{af}[a0]")
        speech = "[a0]"
    else:
        speech = "[0:a]"
    parts.append(f"[1:a]{af_norm(c.canvas)},volume={db}dB[mus]")
    if duck and has_filter("sidechaincompress"):
        parts.append(f"{speech}asplit=2[voice][sc]")
        parts.append("[mus][sc]sidechaincompress=threshold=0.05:ratio=8:attack=40:release=280:makeup=1[ducked]")
        parts.append("[voice][ducked]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]")
        desc = f"duck {Path(str(music)).name} under speech ({db} dB)"
    else:
        parts.append(f"{speech}[mus]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]")
        desc = f"mix {Path(str(music)).name} at {db} dB"
    argv = c._ffmpeg() + ["-i", str(base), "-stream_loop", "-1", "-i", str(music)]
    if vf:
        parts.insert(0, f"[0:v]{vf}[v]")
        argv += ["-filter_complex", ";".join(parts), "-map", "[v]", "-map", "[a]", "-shortest"] + c._encode()
    else:
        argv += ["-filter_complex", ";".join(parts), "-map", "0:v?", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest"]
    argv.append(str(out))
    c._pipeline.add(Step("music", argv, [str(out)], desc))
    c.set_current(out)


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
    w, h = mapping[aspect]
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
    width, height = mapping[aspect]
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
        "[backdrop]drawbox=x=0:y=0:w=iw:h=ih:color=0x172236:t=fill[background]",
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
    argv = c._ffmpeg() + ["-i", str(src), "-filter_complex", graph_text, "-map", "[v]"]
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
    parts = [f"[{i}:v]{vf_fit(c.canvas)},setpts=PTS-STARTPTS[v{i}]" for i in range(n)]
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
    first_dest: Path | None = None
    for i, name in enumerate(names):
        preset = get_preset(str(name))
        if explicit is not None and i == 0:
            dest = explicit
            if not dest.is_absolute():
                dest = c.work_dir / dest
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
        if has_video and preset.video_codec:
            # FFmpeg 9 carries frame color metadata through the encoder;
            # codec flags alone can leave primaries/transfer unspecified.
            vf_bits.append("setparams=range=limited:color_primaries=bt709:color_trc=bt709:colorspace=bt709")
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
            has_audio=has_audio,
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
    "watermark": _watermark,
    "pip": _pip,
    "music": _music,
    "replace_audio": _replace_audio,
    "extract_audio": _extract_audio,
    "reframe": _reframe,
    "track_reframe": _track_reframe,
    "spatial_effect": _spatial_effect,
    "slideshow": _slideshow,
    "thumbnail": _thumbnail,
    "chromakey": _chromakey,
    "scenes": _scenes,
    "export": _export,
}
