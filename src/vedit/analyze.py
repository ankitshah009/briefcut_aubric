"""Media analysis: silence, scenes, EBU R128 loudness."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from vedit.errors import AnalysisError, RenderError
from vedit.media import run_ffmpeg


@dataclass(frozen=True)
class Range:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


_START = re.compile(r"silence_start:\s*([0-9.]+)")
_END = re.compile(r"silence_end:\s*([0-9.]+)")


def detect_silence(
    path: str | Path,
    *,
    threshold_db: float = -32.0,
    min_silence: float = 0.45,
) -> list[Range]:
    """Return silent ranges using ffmpeg silencedetect (audio-only filter)."""
    result = run_ffmpeg(
        [
            "-i",
            str(path),
            "-vn",
            "-af",
            f"silencedetect=noise={threshold_db}dB:d={min_silence}",
            "-f",
            "null",
            "-",
        ]
    )
    silences: list[Range] = []
    open_start: float | None = None
    for line in result.stderr.splitlines():
        start = _START.search(line)
        if start:
            open_start = float(start.group(1))
            continue
        end = _END.search(line)
        if end and open_start is not None:
            silences.append(Range(open_start, float(end.group(1))))
            open_start = None
    return silences


def keep_ranges(
    duration: float,
    silences: list[Range],
    *,
    pad: float = 0.12,
    min_keep: float = 0.05,
) -> list[Range]:
    """Invert silence into keep-ranges. `pad` is speech padding stolen back from silence."""
    if duration <= 0:
        raise AnalysisError("duration must be positive")
    shrunk: list[Range] = []
    for sil in silences:
        start = sil.start + pad
        end = sil.end - pad
        if end > start:
            shrunk.append(Range(start, end))
    keeps: list[Range] = []
    cursor = 0.0
    for sil in shrunk:
        if sil.start - cursor >= min_keep:
            keeps.append(Range(cursor, min(sil.start, duration)))
        cursor = max(cursor, sil.end)
    if duration - cursor >= min_keep:
        keeps.append(Range(cursor, duration))
    return [k for k in keeps if k.duration >= min_keep]


def select_expression(keeps: list[Range]) -> str:
    if not keeps:
        raise AnalysisError("no keep-ranges — the file is entirely silence under the threshold")
    return "+".join(f"between(t,{k.start:.3f},{k.end:.3f})" for k in keeps)


_SCENE = re.compile(r"lavfi\.scd\.time:\s*([0-9.]+)")
_SHOWINFO = re.compile(r"pts_time:([0-9.]+)")


def detect_scenes(path: str | Path, *, threshold: float = 0.35) -> list[float]:
    """Scene-change timestamps in seconds."""
    result = run_ffmpeg(
        [
            "-i",
            str(path),
            "-an",
            "-vf",
            f"select='gt(scene,{threshold})',showinfo",
            "-f",
            "null",
            "-",
        ]
    )
    times = [float(m) for m in _SHOWINFO.findall(result.stderr)]
    # Unique, sorted, drop near-duplicates.
    out: list[float] = []
    for t in times:
        if not out or t - out[-1] > 0.08:
            out.append(t)
    return out


def measure_loudness(path: str | Path) -> dict[str, float]:
    """Two-pass loudnorm measurement. Returns the measured_* fields ffmpeg needs."""
    result = run_ffmpeg(
        [
            "-i",
            str(path),
            "-vn",
            "-af",
            "loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json",
            "-f",
            "null",
            "-",
        ]
    )
    blob = _extract_json(result.stderr)
    if blob is None:
        raise AnalysisError("loudnorm did not print a JSON measurement block")
    keys = (
        "input_i",
        "input_tp",
        "input_lra",
        "input_thresh",
        "target_offset",
    )
    out: dict[str, float] = {}
    for key in keys:
        if key not in blob:
            raise AnalysisError(f"loudnorm JSON missing {key}")
        out[key] = float(blob[key])
    return out


def _extract_json(text: str) -> dict[str, object] | None:
    start = text.rfind("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def apply_cut_silence(
    src: Path,
    dest: Path,
    keeps_path: Path,
    *,
    threshold_db: float = -32.0,
    min_silence: float = 0.45,
    pad: float = 0.12,
    width: int = 1920,
    height: int = 1080,
    fps: float = 30.0,
    sample_rate: int = 48000,
    background: str = "black",
    pixel_format: str = "yuv420p",
) -> list[Range]:
    """Detect silence and write a jumped-cut file. Runs at render, not plan."""
    from vedit.media import probe

    info = probe(src)
    if not info.has_audio:
        raise AnalysisError(f"cut_silence requires an audio track: {src}")
    silences = detect_silence(src, threshold_db=threshold_db, min_silence=min_silence)
    keeps = keep_ranges(info.duration, silences, pad=pad)
    expr = select_expression(keeps)
    keeps_path.write_text(
        json.dumps(
            {
                "silences": [{"start": s.start, "end": s.end} for s in silences],
                "keeps": [{"start": k.start, "end": k.end} for k in keeps],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    vf = (
        f"fps={fps},scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={background},"
        f"setsar=1,format={pixel_format}"
    )
    af = f"aformat=sample_fmts=fltp:sample_rates={sample_rate}:channel_layouts=stereo"
    filters: list[str] = []
    maps: list[str] = []
    if info.has_video:
        filters.append(f"[0:v]select='{expr}',setpts=N/FRAME_RATE/TB,{vf}[v]")
        maps += ["-map", "[v]"]
    else:
        maps += ["-vn"]
    filters.append(f"[0:a]aselect='{expr}',asetpts=N/SR/TB,{af}[a]")
    maps += ["-map", "[a]"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.stem + ".part" + dest.suffix)
    if part.exists():
        part.unlink()
    run_ffmpeg(
        [
            "-i",
            str(src),
            "-filter_complex",
            ";".join(filters),
            *maps,
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
            str(part),
        ]
    )
    part.replace(dest)
    return keeps


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="vedit.analyze")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("cut-silence")
    p.add_argument("--in", dest="inp", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--keeps", required=True)
    p.add_argument("--threshold-db", type=float, default=-32.0)
    p.add_argument("--min-silence", type=float, default=0.45)
    p.add_argument("--pad", type=float, default=0.12)
    p.add_argument("--width", type=int, default=1920)
    p.add_argument("--height", type=int, default=1080)
    p.add_argument("--fps", type=float, default=30.0)
    p.add_argument("--sample-rate", type=int, default=48000)
    p = sub.add_parser("loudnorm")
    p.add_argument("--in", dest="inp", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--i", dest="i_target", type=float, default=-14.0)
    p.add_argument("--tp", type=float, default=-1.5)
    p.add_argument("--lra", type=float, default=11.0)
    p.add_argument("--video", action="store_true")
    p = sub.add_parser("encode")
    p.add_argument("--in", dest="inp", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--vf", default="")
    p.add_argument("--af", default="")
    p.add_argument("--loudnorm", action="store_true")
    p.add_argument("--i", dest="i_target", type=float, default=-14.0)
    p.add_argument("--tp", type=float, default=-1.5)
    p.add_argument("--lra", type=float, default=11.0)
    p.add_argument("--preset", default="youtube_1080p")
    hw = p.add_mutually_exclusive_group()
    hw.add_argument("--hw", dest="hw_flag", action="store_const", const=True)
    hw.add_argument("--no-hw", dest="hw_flag", action="store_const", const=False)
    p.set_defaults(hw_flag=None)
    p.add_argument("--crf", type=int, default=18)
    p.add_argument("--video", action="store_true")
    p.add_argument("--audio", action="store_true")
    p.add_argument("--fade-out", type=float, default=0.0)
    p = sub.add_parser("verify")
    p.add_argument("--in", dest="inp", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--ref")
    p = sub.add_parser("scenes")
    p.add_argument("--in", dest="inp", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--threshold", type=float, default=0.35)
    args = parser.parse_args(argv)
    if args.cmd == "cut-silence":
        apply_cut_silence(
            Path(args.inp),
            Path(args.out),
            Path(args.keeps),
            threshold_db=args.threshold_db,
            min_silence=args.min_silence,
            pad=args.pad,
            width=args.width,
            height=args.height,
            fps=args.fps,
            sample_rate=args.sample_rate,
        )
    elif args.cmd == "loudnorm":
        apply_loudnorm(
            Path(args.inp),
            Path(args.out),
            i=args.i_target,
            tp=args.tp,
            lra=args.lra,
            has_video=bool(args.video),
        )
    elif args.cmd == "encode":
        apply_encode(
            Path(args.inp),
            Path(args.out),
            vf=args.vf,
            af=args.af,
            loudnorm=bool(args.loudnorm),
            i=args.i_target,
            tp=args.tp,
            lra=args.lra,
            preset=args.preset,
            hw=args.hw_flag,
            crf=int(args.crf),
            has_video=bool(args.video),
            has_audio=bool(args.audio),
            fade_out=float(args.fade_out),
        )
    elif args.cmd == "verify":
        apply_verify(Path(args.inp), Path(args.out), ref=Path(args.ref) if args.ref else None)
    elif args.cmd == "scenes":
        apply_scenes(Path(args.inp), Path(args.out), threshold=float(args.threshold))
    return 0


def loudnorm_filter(measured: dict[str, float] | None = None, *, i: float = -14.0, tp: float = -1.5, lra: float = 11.0) -> str:
    base = f"loudnorm=I={i}:TP={tp}:LRA={lra}"
    if not measured:
        return base
    return (
        f"{base}:measured_I={measured['input_i']}:"
        f"measured_TP={measured['input_tp']}:"
        f"measured_LRA={measured['input_lra']}:"
        f"measured_thresh={measured['input_thresh']}:"
        f"offset={measured['target_offset']}:linear=true"
    )


def apply_loudnorm(
    src: Path,
    dest: Path,
    *,
    i: float = -14.0,
    tp: float = -1.5,
    lra: float = 11.0,
    has_video: bool = True,
) -> dict[str, float]:
    """Two-pass EBU R128. Runs at render so plan stays a script writer."""
    measured = measure_loudness(src)
    argv = ["-i", str(src), "-af", loudnorm_filter(measured, i=i, tp=tp, lra=lra)]
    if has_video:
        argv += [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "16",
            "-pix_fmt",
            "yuv420p",
        ]
    else:
        argv += ["-vn"]
    argv += ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(dest)]
    run_ffmpeg(argv)
    return measured


def apply_scenes(src: Path, dest: Path, *, threshold: float = 0.35) -> list[float]:
    times = detect_scenes(src, threshold=threshold)
    dest.write_text(json.dumps({"times": times}, indent=2) + "\n", encoding="utf-8")
    return times


def apply_encode(
    src: Path,
    dest: Path,
    *,
    vf: str = "",
    af: str = "",
    loudnorm: bool = False,
    i: float = -14.0,
    tp: float = -1.5,
    lra: float = 11.0,
    preset: str = "youtube_1080p",
    hw: bool | None = None,
    crf: int = 18,
    has_video: bool = True,
    has_audio: bool = True,
    fade_out: float = 0.0,
) -> None:
    """One master encode: optional pending filters + two-pass loudnorm + preset."""
    from vedit.media import probe
    from vedit.presets import encode_args, get_preset

    video = vf
    audio = af
    if fade_out and fade_out > 0:
        duration = probe(src).duration
        start = max(0.0, duration - fade_out)
        video = ",".join(x for x in (video, f"fade=t=out:st={start:.3f}:d={fade_out}") if x)
        audio = ",".join(x for x in (audio, f"afade=t=out:st={start:.3f}:d={fade_out}") if x)
    if loudnorm:
        measured = measure_loudness(src)
        extra = loudnorm_filter(measured, i=i, tp=tp, lra=lra)
        audio = ",".join(x for x in (audio, extra) if x)
    preset_obj = get_preset(preset)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.stem + ".part" + dest.suffix)
    if part.exists():
        part.unlink()

    def _run(use_hw: bool | None) -> None:
        argv: list[str] = ["-i", str(src)]
        if video:
            argv += ["-vf", video]
        if audio:
            argv += ["-af", audio]
        argv += encode_args(
            preset_obj,
            hw=use_hw,
            crf=crf,
            has_video=has_video,
            has_audio=has_audio,
        )
        argv.append(str(part))
        run_ffmpeg(argv)
        part.replace(dest)

    try:
        _run(hw)
    except RenderError:
        if part.exists():
            part.unlink()
        enc = encode_args(preset_obj, hw=hw, crf=crf, has_video=has_video, has_audio=has_audio)
        if "h264_videotoolbox" in enc:
            _run(False)
        else:
            raise


def _vmaf_enabled() -> bool:
    """Full-frame VMAF is opt-in. The editor proves the file without waiting on it."""
    return os.environ.get("VEDIT_VMAF", "").strip() == "1"


def apply_verify(src: Path, dest: Path, *, ref: Path | None = None) -> dict[str, object]:
    """Prove the file: streams, duration, loudness. VMAF runs only when VEDIT_VMAF=1."""
    from vedit.media import has_filter, probe

    info = probe(src)
    payload: dict[str, object] = {
        "path": str(src),
        "duration": info.duration,
        "width": info.width,
        "height": info.height,
        "fps": info.fps,
        "has_video": info.has_video,
        "has_audio": info.has_audio,
        "size_bytes": info.size_bytes,
    }
    if not info.has_video and not info.has_audio:
        raise AnalysisError(f"verify failed: no streams in {src}")
    if info.duration <= 0.04:
        raise AnalysisError(f"verify failed: duration {info.duration}s in {src}")
    if info.video:
        payload["color"] = {
            "primaries": info.video.color_primaries,
            "trc": info.video.color_trc,
            "space": info.video.color_space,
            "range": info.video.color_range,
            "pix_fmt": info.video.pix_fmt,
        }
    if info.has_audio:
        try:
            payload["loudness"] = measure_loudness(src)
        except AnalysisError:
            payload["loudness"] = None
    if _vmaf_enabled() and ref is not None and ref.is_file() and info.has_video and has_filter("libvmaf"):
        vmaf_log = dest.with_suffix(".vmaf.json")
        try:
            run_ffmpeg(
                [
                    "-i",
                    str(src),
                    "-i",
                    str(ref),
                    "-lavfi",
                    (
                        f"[0:v]scale={info.width}:{info.height}:flags=bicubic,setpts=PTS-STARTPTS[d];"
                        f"[1:v]scale={info.width}:{info.height}:flags=bicubic,setpts=PTS-STARTPTS[r];"
                        f"[d][r]libvmaf=log_fmt=json:log_path={vmaf_log}"
                    ),
                    "-f",
                    "null",
                    "-",
                ]
            )
            if vmaf_log.is_file():
                payload["vmaf_log"] = str(vmaf_log)
        except Exception:
            payload["vmaf"] = None
    dest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


if __name__ == "__main__":
    raise SystemExit(main())
