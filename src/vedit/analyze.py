"""Media analysis: silence, scenes, EBU R128 loudness."""

from __future__ import annotations

import json
import math
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


# FFmpeg prints these with %g, so a time can be negative or carry an exponent.
_START = re.compile(r"silence_start:\s*(-?[0-9.]+(?:e[-+]?[0-9]+)?)")
_END = re.compile(r"silence_end:\s*(-?[0-9.]+(?:e[-+]?[0-9]+)?)")
_PEAK = re.compile(r"max_volume:\s*(-?[0-9.]+|-?inf|nan) dB")
# Quieter than this is room tone at best. Normalizing it would turn hiss into a roar.
_QUIETEST_PROGRAMME_LUFS = -60.0
# Nothing a listener could hear peaks below this.
_INAUDIBLE_PEAK_DB = -60.0
# The bounds FFmpeg declares for each loudnorm option.
_LOUDNORM_BOUNDS = {
    "input_i": (-99.0, 0.0),
    "input_tp": (-99.0, 99.0),
    "input_lra": (0.0, 99.0),
    "input_thresh": (-99.0, 0.0),
    "target_offset": (-99.0, 99.0),
}


def detect_silence(
    path: str | Path,
    *,
    threshold_db: float | None = None,
    min_silence: float = 0.45,
    track: int | None = None,
) -> list[Range]:
    """Return silent ranges using ffmpeg silencedetect (audio-only filter).

    ``track`` is the audio stream to measure, for files that hold several.

    ``threshold_db`` None measures where a pause starts in this recording:
    ``silence_threshold_db`` of its integrated loudness, 16 dB under it and
    within -50 to -26 dBFS, the rule the agent planner uses too. A number is
    used as given. Either way, a recording whose loudest sound barely reaches
    that line would read as one long silence, so its line drops to halfway
    between its own room level and its peak: a whispered talk still has speech
    and pauses. That fallback only ever lowers the line.
    """
    from vedit.agent.decide import SILENCE_DEFAULT_DB, silence_threshold_db

    select = ["-map", f"0:a:{track}"] if track is not None else ["-vn"]
    asked = SILENCE_DEFAULT_DB if threshold_db is None else float(threshold_db)
    measures = _LEVEL_STATS + ("," + _LOUDNESS if threshold_db is None else "")

    def listen(threshold: float, *, stats: bool) -> str:
        chain = f"silencedetect=noise={threshold:.2f}dB:d={min_silence}" + (f",{measures}" if stats else "")
        return run_ffmpeg(["-i", str(path), *select, "-af", chain, "-f", "null", "-"]).stderr

    try:
        stderr = listen(asked, stats=True)
    except RenderError:
        # An FFmpeg too old for these measure options still detects silence.
        stderr, peak, loudness = listen(asked, stats=False), None, None
    else:
        peak, loudness = _level(stderr, "Peak level"), _integrated(stderr)
    threshold = asked if threshold_db is not None else silence_threshold_db(loudness)
    if peak is not None and _INAUDIBLE_PEAK_DB < peak < threshold + _QUIET_HEADROOM_DB:
        floor = _noise_peak_db(path, track)
        if floor is not None and floor < peak:
            threshold = min(threshold, (peak + floor) / 2)
    if abs(threshold - asked) >= 0.5:
        stderr = listen(threshold, stats=False)
    return _silences(stderr)


# Integrated loudness, printed once in ebur128's summary. Per-frame lines stay out of the log.
_LOUDNESS = "ebur128=framelog=verbose"
_INTEGRATED = re.compile(r"\bI:\s+(-?[0-9.]+|-?inf) LUFS")


def _integrated(stderr: str) -> float | None:
    found = _INTEGRATED.findall(stderr)
    if not found:
        return None
    value = float(found[-1])
    return value if math.isfinite(value) else None


# The loudest sample of a whole track, printed once at the end.
_LEVEL_STATS = "astats=measure_perchannel=none:measure_overall=Peak_level"
# A recording whose peak sits less than this above the pause threshold is too quiet for a fixed one.
_QUIET_HEADROOM_DB = 12.0
# Room tone's samples peak about this far above its average level.
_NOISE_CREST_DB = 10.0
_FLOOR_RATE = 4000  # enough to hear a room and a voice; a two-hour track decodes in moments
_FLOOR_WINDOW = 120  # 30 ms at _FLOOR_RATE


def _level(stderr: str, name: str) -> float | None:
    found = re.findall(rf"{name} dB:\s*(-?[0-9.]+|-?inf)", stderr)
    if not found:
        return None
    value = float(found[-1])
    return value if math.isfinite(value) else None


def _noise_peak_db(path: str | Path, track: int | None) -> float | None:
    """How loud the room's own samples get: the quietest tenth of 30 ms windows, plus their crest."""
    from operator import mul

    from vedit.sound import decode_mono

    try:
        samples = decode_mono(path, rate=_FLOOR_RATE, track=track)
    except ValueError:
        return None
    levels = sorted(sum(map(mul, samples[at:at + _FLOOR_WINDOW], samples[at:at + _FLOOR_WINDOW])) / _FLOOR_WINDOW
                    for at in range(0, len(samples) - _FLOOR_WINDOW + 1, _FLOOR_WINDOW))
    if len(levels) < 10:
        return None
    return 10 * math.log10(max(levels[len(levels) // 10], 1.0) / 32768.0 ** 2) + _NOISE_CREST_DB


def _silences(stderr: str) -> list[Range]:
    silences: list[Range] = []
    open_start: float | None = None
    for line in stderr.splitlines():
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
        raise AnalysisError("Nothing is left to keep: the whole file is quieter than the silence threshold.")
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


def audible(path: str | Path, *, track: int | None = None, start: float = 0.0, seconds: float | None = None) -> bool:
    """False when a track holds nothing a listener could hear, such as digital silence.

    ``start`` and ``seconds`` limit the listen to one stretch of the file.
    """
    window = (["-ss", f"{start:.3f}"] if start > 0 else []) + ["-i", str(path)] + (["-t", f"{seconds:.3f}"] if seconds is not None else [])
    result = run_ffmpeg([*window, *(["-map", f"0:a:{track}"] if track is not None else ["-vn"]), "-af", "volumedetect", "-f", "null", "-"])
    peaks = _PEAK.findall(result.stderr)
    return not peaks or float(peaks[-1]) > _INAUDIBLE_PEAK_DB


def measure_loudness(path: str | Path, *, af: str = "", i: float = -14.0, tp: float = -1.5, lra: float = 11.0) -> dict[str, float] | None:
    """Two-pass loudnorm measurement. Returns the measured_* fields ffmpeg needs.

    None means there is nothing to normalize. BS.1770 gates out anything under
    -70 LUFS and needs 0.4 s of audio, so digital silence and a very short clip
    measure -inf, a value the loudnorm filter refuses. ``af`` is the chain that
    runs ahead of loudnorm in the real encode, so the second pass is told about
    the signal it will actually receive. ``i``, ``tp`` and ``lra`` must be the
    second pass's targets: ``target_offset`` is measured against them.
    """
    result = run_ffmpeg(
        [
            "-i",
            str(path),
            "-vn",
            "-af",
            ",".join(x for x in (af, f"{loudnorm_filter(i=i, tp=tp, lra=lra)}:print_format=json") if x),
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
    if not all(math.isfinite(value) for value in out.values()) or out["input_i"] < _QUIETEST_PROGRAMME_LUFS:
        return None
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
    threshold_db: float | None = None,
    min_silence: float = 0.45,
    pad: float = 0.12,
    width: int = 1920,
    height: int = 1080,
    fps: float = 30.0,
    sample_rate: int = 48000,
    background: str = "black",
    pixel_format: str = "yuv420p",
) -> list[Range]:
    """Detect silence and write a jumped-cut file. Runs at render, not plan.

    ``threshold_db`` None measures the pause level from this recording's loudness.
    """
    from vedit.media import probe

    info = probe(src)
    if not info.has_audio:
        raise AnalysisError(f"cut_silence requires an audio track: {src}")
    silences = detect_silence(src, threshold_db=threshold_db, min_silence=min_silence, track=info.audio_map)
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
        filters.append(f"[0:v:{info.video_map}]select='{expr}',setpts=N/FRAME_RATE/TB,{vf}[v]")
        maps += ["-map", "[v]"]
    else:
        maps += ["-vn"]
    filters.append(f"[0:a:{info.audio_map}]aselect='{expr}',asetpts=N/SR/TB,{af}[a]")
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
    p.add_argument("--threshold-db", type=float, default=None, help="pause level in dBFS; measured from the recording when left out")
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
    # A clipped master can measure above 0 LUFS, which the option refuses.
    clamped = {key: max(low, min(high, measured[key])) for key, (low, high) in _LOUDNORM_BOUNDS.items()}
    return (
        f"{base}:measured_I={clamped['input_i']}:"
        f"measured_TP={clamped['input_tp']}:"
        f"measured_LRA={clamped['input_lra']}:"
        f"measured_thresh={clamped['input_thresh']}:"
        f"offset={clamped['target_offset']}:linear=true"
    )


def apply_loudnorm(
    src: Path,
    dest: Path,
    *,
    i: float = -14.0,
    tp: float = -1.5,
    lra: float = 11.0,
    has_video: bool = True,
) -> dict[str, float] | None:
    """Two-pass EBU R128. Runs at render so plan stays a script writer."""
    measured = measure_loudness(src, i=i, tp=tp, lra=lra)
    argv = ["-i", str(src)]
    if measured:
        argv += ["-af", loudnorm_filter(measured, i=i, tp=tp, lra=lra)]
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


# Ordered dither repeats from frame to frame, which GIF's frame differencing can
# compress. On real footage it halves the file, with no visible loss at this size.
GIF_PALETTE = ("split[gif_in][gif_stats];[gif_stats]palettegen=max_colors=128:stats_mode=diff[gif_palette];"
               "[gif_in][gif_palette]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle")


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
    measured = measure_loudness(src, af=audio, i=i, tp=tp, lra=lra) if loudnorm and has_audio else None
    if measured:
        audio = ",".join(x for x in (audio, loudnorm_filter(measured, i=i, tp=tp, lra=lra)) if x)
        # loudnorm pads its output to whole 100 ms frames; end the sound where the picture ends.
        info = probe(src)
        audio += f",atrim=end={(info.duration if has_video else info.file_duration):.6f}"
    preset_obj = get_preset(preset)
    if preset_obj.video_codec == "gif":
        # After any fade, because the palette has to describe the finished frames.
        video = ",".join(x for x in (video, GIF_PALETTE) if x)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.stem + ".part" + dest.suffix)
    if part.exists():
        part.unlink()

    def _run(use_hw: bool | None) -> None:
        argv: list[str] = ["-i", str(src)]
        if video:
            argv += ["-vf", video]
        if audio and has_audio:
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
