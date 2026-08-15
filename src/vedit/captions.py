"""Captions: SRT/ASS I/O, local Whisper, timestamp remapping, burn-in."""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from vedit.analyze import Range
from vedit.errors import BinaryMissing, CaptionError
from vedit.media import escape_drawtext, escape_filter_path, has_filter, resolve_ffmpeg, resolve_font, run_command, run_ffmpeg

_WHISPER_MODELS = (
    Path("/opt/homebrew/share/whisper-cpp/for-tests-ggml-tiny.bin"),
    Path(os.path.expanduser("~/.cache/whisper/ggml-base.en.bin")),
    Path(os.path.expanduser("~/.cache/whisper/ggml-small.en.bin")),
    Path(os.path.expanduser("~/.local/share/whisper/ggml-base.en.bin")),
)


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str


def format_ts(seconds: float, *, srt: bool = True) -> str:
    if seconds < 0:
        seconds = 0.0
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, milli = divmod(rem, 1000)
    sep = "," if srt else "."
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{milli:03d}"


def parse_ts(stamp: str) -> float:
    stamp = stamp.strip().replace(",", ".")
    h, m, rest = stamp.split(":")
    return int(h) * 3600 + int(m) * 60 + float(rest)


def write_srt(cues: list[Cue], path: Path) -> None:
    blocks = []
    for i, cue in enumerate(cues, start=1):
        blocks.append(
            f"{i}\n{format_ts(cue.start)} --> {format_ts(cue.end)}\n{cue.text.strip()}\n"
        )
    path.write_text("\n".join(blocks) + ("\n" if blocks else ""), encoding="utf-8")


def write_ass(cues: list[Cue], path: Path, *, canvas_width: int = 1920, canvas_height: int = 1080) -> None:
    font = "Arial"
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {canvas_width}
PlayResY: {canvas_height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},52,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,3,0,2,80,80,64,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    for cue in cues:
        text = cue.text.replace("\n", r"\N").replace(",", "，")
        lines.append(
            f"Dialogue: 0,{format_ts(cue.start, srt=False)},{format_ts(cue.end, srt=False)},Default,,0,0,0,,{text}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


_SRT_BLOCK = re.compile(
    r"(\d+)\s+(\d{2}:\d{2}:\d{2}[,.]\d{3})\s+-->\s+(\d{2}:\d{2}:\d{2}[,.]\d{3})\s+(.*?)(?=\n\d+\s+\d{2}:|\Z)",
    re.S,
)


def read_srt(path: Path) -> list[Cue]:
    text = path.read_text(encoding="utf-8")
    cues: list[Cue] = []
    for match in _SRT_BLOCK.finditer(text.strip() + "\n"):
        body = re.sub(r"\s+", " ", match.group(4)).strip()
        if body:
            cues.append(Cue(parse_ts(match.group(2)), parse_ts(match.group(3)), body))
    return cues


def remap_cues(cues: list[Cue], keeps: list[Range]) -> list[Cue]:
    """Map original-timeline cues onto a silence-cut timeline."""
    remapped: list[Cue] = []
    for cue in cues:
        new_start = _map_time(cue.start, keeps)
        new_end = _map_time(cue.end, keeps)
        if new_start is None or new_end is None or new_end <= new_start:
            continue
        remapped.append(Cue(new_start, new_end, cue.text))
    return remapped


def _map_time(t: float, keeps: list[Range]) -> float | None:
    elapsed = 0.0
    for keep in keeps:
        if keep.start <= t <= keep.end:
            return elapsed + (t - keep.start)
        if t < keep.start:
            return elapsed
        elapsed += keep.duration
    return None


def find_whisper_model() -> Path | None:
    env = os.environ.get("VEDIT_WHISPER_MODEL")
    if env and Path(env).is_file():
        return Path(env)
    for candidate in _WHISPER_MODELS:
        if candidate.is_file():
            return candidate
    return None


def transcribe(
    media: Path,
    work_dir: Path,
    *,
    language: str = "en",
    model: Path | None = None,
) -> list[Cue]:
    """Local speech-to-text. Prefer whisper-cli; fall back to faster-whisper if installed."""
    wav = work_dir / "speech.wav"
    run_ffmpeg(["-i", str(media), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)])
    model_path = model or find_whisper_model()
    cli = shutil.which("whisper-cli")
    if cli and model_path:
        return _transcribe_whisper_cli(cli, model_path, wav, work_dir, language)
    try:
        return _transcribe_faster(wav, language)
    except ImportError:
        raise CaptionError(
            "No local transcriber. Install whisper-cpp (`brew install whisper-cpp`) "
            "and set VEDIT_WHISPER_MODEL to a ggml .bin, or `pip install faster-whisper`."
        ) from None


def _transcribe_whisper_cli(cli: str, model: Path, wav: Path, work_dir: Path, language: str) -> list[Cue]:
    prefix = work_dir / "whisper"
    argv = [
        cli,
        "-m",
        str(model),
        "-l",
        language,
        "-osrt",
        "-of",
        str(prefix),
        "-nt",
        "false",
        str(wav),
    ]
    result = run_command(argv, timeout=3600)
    if not result.ok:
        # Some builds reject -nt. Retry minimal flags.
        argv = [cli, "-m", str(model), "-l", language, "-osrt", "-of", str(prefix), str(wav)]
        result = run_command(argv, timeout=3600)
    srt = Path(str(prefix) + ".srt")
    if not srt.is_file():
        alt = work_dir / "whisper.srt"
        if alt.is_file():
            srt = alt
    if not srt.is_file():
        raise CaptionError(
            "whisper-cli did not write an SRT.\n"
            + result.stderr[-1500:]
        )
    return read_srt(srt)


def _transcribe_faster(wav: Path, language: str) -> list[Cue]:
    from faster_whisper import WhisperModel  # type: ignore

    model_name = os.environ.get("VEDIT_FASTER_WHISPER_MODEL", "base.en")
    model = WhisperModel(model_name, device="auto", compute_type="int8")
    segments, _info = model.transcribe(str(wav), language=language, vad_filter=True)
    return [Cue(float(seg.start), float(seg.end), seg.text.strip()) for seg in segments if seg.text.strip()]


def burn_filter(subtitle_path: Path, *, style: str = "lower-third") -> str:
    """Return an ffmpeg -vf fragment that burns captions."""
    escaped = escape_filter_path(subtitle_path)
    if subtitle_path.suffix.lower() == ".ass" and has_filter("ass"):
        return f"ass='{escaped}'"
    if has_filter("subtitles"):
        force = (
            "FontName=Arial,FontSize=28,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
            "BorderStyle=1,Outline=2,Shadow=0,MarginV=64,Alignment=2"
        )
        if style == "boxed":
            force = (
                "FontName=Arial,FontSize=26,PrimaryColour=&H00FFFFFF,BackColour=&H80000000,"
                "BorderStyle=3,Outline=1,MarginV=56,Alignment=2"
            )
        return f"subtitles='{escaped}':force_style='{force}'"
    raise CaptionError(
        "This ffmpeg build has no subtitles/ass filter. "
        "Use ffmpeg-full (`brew install ffmpeg-full`) or set VEDIT_FFMPEG."
    )


def title_filter(text: str, canvas_height: int, *, y_ratio: float = 0.78, size: int = 56) -> str:
    if not has_filter("drawtext"):
        raise CaptionError("drawtext filter missing — point VEDIT_FFMPEG at ffmpeg-full")
    font = escape_filter_path(resolve_font())
    _ = canvas_height  # kept for callers; position is expression-based so any frame size works
    return (
        f"drawtext=fontfile='{font}':text='{escape_drawtext(text)}':"
        f"fontsize={size}:fontcolor=white:borderw=3:bordercolor=black:"
        f"x=(w-text_w)/2:y=h*{y_ratio:.3f}-text_h/2"
    )


def ffmpeg_has_text() -> bool:
    return has_filter("drawtext") or has_filter("subtitles") or has_filter("ass")


def transcribe_to_files(
    media: Path,
    work_dir: Path,
    *,
    srt: Path,
    ass: Path,
    language: str = "en",
    keeps_path: Path | None = None,
    width: int = 1920,
    height: int = 1080,
) -> list[Cue]:
    """Run STT and write SRT/ASS. Used as a pipeline step (`python -m vedit.captions`)."""
    cues = transcribe(media, work_dir, language=language)
    if keeps_path is not None and keeps_path.is_file():
        import json

        data = json.loads(keeps_path.read_text(encoding="utf-8"))
        keeps = [Range(float(k["start"]), float(k["end"])) for k in data.get("keeps") or []]
        if keeps:
            cues = remap_cues(cues, keeps)
    write_srt(cues, srt)
    write_ass(cues, ass, canvas_width=width, canvas_height=height)
    return cues


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="vedit.captions", description="Local caption transcription step")
    parser.add_argument("--media", required=True)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--srt", required=True)
    parser.add_argument("--ass", required=True)
    parser.add_argument("--keeps")
    parser.add_argument("--language", default="en")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    args = parser.parse_args(argv)
    transcribe_to_files(
        Path(args.media),
        Path(args.work_dir),
        srt=Path(args.srt),
        ass=Path(args.ass),
        language=args.language,
        keeps_path=Path(args.keeps) if args.keeps else None,
        width=args.width,
        height=args.height,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
