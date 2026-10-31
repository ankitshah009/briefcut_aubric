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
    Path(os.path.expanduser("~/.cache/whisper/ggml-base.en.bin")),
    Path(os.path.expanduser("~/.cache/whisper/ggml-small.en.bin")),
    Path(os.path.expanduser("~/.cache/whisper/ggml-tiny.en.bin")),
    Path(os.path.expanduser("~/.local/share/whisper/ggml-base.en.bin")),
    Path("/opt/homebrew/share/whisper-cpp/ggml-tiny.en.bin"),
    Path("/opt/homebrew/share/whisper-cpp/for-tests-ggml-tiny.bin"),
)


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str


def format_ts(seconds: float, *, srt: bool = True) -> str:
    """SRT uses HH:MM:SS,mmm. ASS uses H:MM:SS.cc (centiseconds): libass reads
    a three-digit fraction as centiseconds, so ".380" would mean 3.8 s."""
    total = max(0.0, float(seconds))
    if srt:
        ms = int(round(total * 1000))
        h, rem = divmod(ms, 3_600_000)
        m, rem = divmod(rem, 60_000)
        s, ms = divmod(rem, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    cs = int(round(total * 100))
    h, rem = divmod(cs, 360_000)
    m, rem = divmod(rem, 6_000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def parse_ts(stamp: str) -> float:
    stamp = stamp.strip().replace(",", ".")
    h, m, rest = stamp.split(":")
    return int(h) * 3600 + int(m) * 60 + float(rest)


_TERMINAL_END = re.compile(r"""[.!?…]["'”’)\]]*$""")
_WORD_STRIP = re.compile(r"""^[^\w]+|[^\w]+$""")


def _word_key(word: str) -> str:
    return _WORD_STRIP.sub("", word).casefold()


def _capitalize(text: str) -> str:
    for index, char in enumerate(text):
        if char.isalpha():
            return text[:index] + char.upper() + text[index + 1 :]
        if char.isdigit():
            return text
    return text


def polish_cues(cues: list[Cue], *, max_chars: int = 84, max_gap: float = 0.35) -> list[Cue]:
    """Clean provider cues for burn-in without touching timing except by merging.

    - Immediately repeated words collapse case-insensitively ("the the" -> "the"),
      also across a cue boundary when the next cue opens with the previous cue's
      last word (a Meta Voice Transcribe habit at turn edges).
    - A cue that does not end a sentence merges with the next one when it starts
      within ``max_gap`` seconds and the joined text fits ``max_chars``; a very
      short trailing cue (<= 0.8 s or <= 2 words) merges into its predecessor
      the same way. whisper-cli's ``-ml`` line splitting produces both shapes.
    - Whitespace is normalized, each cue starts with a capital, empties are dropped.
    """
    cleaned: list[Cue] = []
    for cue in cues:
        text = " ".join(cue.text.split())
        if text:
            cleaned.append(Cue(float(cue.start), float(cue.end), text))

    # Collapse doubled words, looking back across the cue boundary.
    collapsed: list[Cue] = []
    for cue in cleaned:
        words: list[str] = []
        for word in cue.text.split():
            if words:
                previous = words[-1]
            elif collapsed:
                previous = collapsed[-1].text.split()[-1]
            else:
                previous = None
            key = _word_key(word)
            if previous is not None and key and key == _word_key(previous):
                # Keep whichever copy carries the punctuation ("lathe lathe," -> "lathe,").
                if _WORD_STRIP.search(word) and not _WORD_STRIP.search(previous):
                    if words:
                        words[-1] = word
                    else:
                        head = collapsed[-1].text.split()
                        head[-1] = word
                        collapsed[-1] = Cue(collapsed[-1].start, collapsed[-1].end, " ".join(head))
                continue
            words.append(word)
        if words:
            collapsed.append(Cue(cue.start, cue.end, " ".join(words)))
        elif collapsed:
            # The whole cue repeated the previous word; its time still belongs to that word.
            last = collapsed[-1]
            collapsed[-1] = Cue(last.start, max(last.end, cue.end), last.text)

    # Merge fragments into whole sentences where the clock allows.
    merged: list[Cue] = []
    for cue in collapsed:
        if merged:
            previous = merged[-1]
            gap = cue.start - previous.end
            joined = f"{previous.text} {cue.text}"
            fits = len(joined) <= max_chars and gap <= max_gap
            open_ended = not _TERMINAL_END.search(previous.text)
            short_trailer = (cue.end - cue.start) <= 0.8 or len(cue.text.split()) <= 2
            if fits and (open_ended or short_trailer):
                merged[-1] = Cue(previous.start, max(previous.end, cue.end), joined)
                continue
        merged.append(cue)

    return [Cue(cue.start, cue.end, _capitalize(cue.text)) for cue in merged]


def wrap_cue_text(text: str, *, max_line: int = 42) -> list[str]:
    """Split long caption text into two balanced lines at a word boundary.

    Text at or under ``max_line`` characters stays on one line. Longer text
    breaks once, choosing the word gap that keeps the halves closest in
    length, with a slight preference for breaking after punctuation. Text
    that already carries line breaks is left as laid out.
    """
    text = text.strip()
    if "\n" in text:
        return [line.strip() for line in text.split("\n") if line.strip()]
    if len(text) <= max_line:
        return [text]
    words = text.split()
    if len(words) < 2:
        return [text]
    best: tuple[float, list[str]] | None = None
    for index in range(1, len(words)):
        left, right = " ".join(words[:index]), " ".join(words[index:])
        score = abs(len(left) - len(right))
        if left[-1] in ",.;:!?":
            score -= 1.5
        if best is None or score < best[0]:
            best = (score, [left, right])
    assert best is not None
    return best[1]


def write_srt(cues: list[Cue], path: Path) -> None:
    blocks = []
    for i, cue in enumerate(cues, start=1):
        blocks.append(
            f"{i}\n{format_ts(cue.start)} --> {format_ts(cue.end)}\n" + "\n".join(wrap_cue_text(cue.text)) + "\n"
        )
    if not blocks:
        # No speech was heard. FFmpeg's subtitles filter cannot probe an empty
        # file, so write one invisible 1 ms cue; the burn then renders nothing.
        blocks.append(f"1\n{format_ts(0)} --> {format_ts(0.001)}\n\u200b\n")
    path.write_text("\n".join(blocks) + "\n", encoding="utf-8")


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
        text = r"\N".join(wrap_cue_text(cue.text))
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


def _is_stub_model(path: Path) -> bool:
    name = path.name.lower()
    if "for-tests" in name or "stub" in name:
        return True
    try:
        return path.stat().st_size < 2_000_000
    except OSError:
        return True


def find_whisper_model(*, allow_stub: bool = False) -> Path | None:
    env = os.environ.get("VEDIT_WHISPER_MODEL")
    if env and Path(env).is_file():
        candidate = Path(env)
        if allow_stub or not _is_stub_model(candidate):
            return candidate
    real: Path | None = None
    stub: Path | None = None
    # Shared models live outside any checkout: $VEDIT_MODELS_DIR or ~/.vedit/models/.
    # A checkout launched via scripts/studio.sh may still keep one under
    # .vedit-models/; resolve both directories at call time, not import.
    shared = Path(os.environ.get("VEDIT_MODELS_DIR") or os.path.expanduser("~/.vedit/models"))
    local = Path.cwd() / ".vedit-models"
    candidates = [shared / "ggml-base.en.bin", shared / "ggml-base.bin"]
    try:
        candidates += sorted(path for path in shared.glob("ggml-*.bin") if path not in candidates)
    except OSError:
        pass
    candidates += [local / "ggml-base.en.bin", local / "ggml-base.bin", *_WHISPER_MODELS]
    for candidate in candidates:
        if not candidate.is_file():
            continue
        if _is_stub_model(candidate):
            stub = stub or candidate
        else:
            real = candidate
            break
    if real is not None:
        return real
    return stub if allow_stub else None


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
        "-ml",
        "42",
        "-sow",
        str(wav),
    ]
    result = run_command(argv, timeout=3600)
    if not result.ok:
        # Older builds may lack word-splitting flags. Retry the basic interface.
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


def title_filter(
    text: str,
    canvas_height: int,
    *,
    y_ratio: float = 0.78,
    size: int = 56,
    box: bool = False,
    start: float | None = None,
    end: float | None = None,
) -> str:
    """Centered drawtext. ``start``/``end`` limit it to a window on the clip clock; ``box`` adds a dark card."""
    if not has_filter("drawtext"):
        raise CaptionError("drawtext filter missing — point VEDIT_FFMPEG at ffmpeg-full")
    font = escape_filter_path(resolve_font())
    _ = canvas_height  # kept for callers; position is expression-based so any frame size works
    spec = (
        f"drawtext=fontfile='{font}':text='{escape_drawtext(text)}':"
        f"fontsize={size}:fontcolor=white:borderw=3:bordercolor=black:"
        f"x=(w-text_w)/2:y=h*{y_ratio:.3f}-text_h/2"
    )
    if box:
        spec += ":box=1:boxcolor=black@0.55:boxborderw=22"
    if start is not None and end is not None:
        spec += f":enable='between(t,{start:.3f},{end:.3f})'"
    elif start is not None:
        spec += f":enable='gte(t,{start:.3f})'"
    elif end is not None:
        spec += f":enable='lt(t,{end:.3f})'"
    return spec


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
    cues = polish_cues(cues)
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
