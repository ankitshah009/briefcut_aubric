"""FFmpeg process control, media probe, filter escaping, host capabilities."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from vedit.errors import BinaryMissing, ProbeError, RenderError

FULL_FFMPEG = Path("/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg")
FULL_FFPROBE = Path("/opt/homebrew/opt/ffmpeg-full/bin/ffprobe")
DEFAULT_FONT = Path("/System/Library/Fonts/Supplemental/Arial.ttf")
HELVETICA = Path("/System/Library/Fonts/Helvetica.ttc")


def resolve_ffmpeg() -> str:
    env = os.environ.get("VEDIT_FFMPEG")
    if env:
        return env
    if FULL_FFMPEG.is_file():
        return str(FULL_FFMPEG)
    found = shutil.which("ffmpeg")
    if not found:
        raise BinaryMissing("ffmpeg not found. Install with: brew install ffmpeg")
    return found


def resolve_ffprobe() -> str:
    env = os.environ.get("VEDIT_FFPROBE")
    if env:
        return env
    ffmpeg = Path(resolve_ffmpeg())
    sibling = ffmpeg.with_name("ffprobe")
    if sibling.is_file():
        return str(sibling)
    if FULL_FFPROBE.is_file():
        return str(FULL_FFPROBE)
    found = shutil.which("ffprobe")
    if not found:
        raise BinaryMissing("ffprobe not found. It ships with ffmpeg.")
    return found


def resolve_font() -> Path:
    env = os.environ.get("VEDIT_FONT")
    if env and Path(env).is_file():
        return Path(env)
    if DEFAULT_FONT.is_file():
        return DEFAULT_FONT
    if HELVETICA.is_file():
        return HELVETICA
    raise BinaryMissing("No usable font. Set VEDIT_FONT to a .ttf/.otf file.")


def which_optional(*names: str) -> str | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


@dataclass(frozen=True)
class StreamInfo:
    index: int
    codec_type: str
    codec_name: str
    duration: float | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    sample_rate: int | None = None
    channels: int | None = None
    bit_rate: int | None = None
    pix_fmt: str | None = None
    tags: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class MediaInfo:
    path: Path
    duration: float
    format_name: str
    size_bytes: int
    bit_rate: int | None
    streams: tuple[StreamInfo, ...]

    @property
    def video(self) -> StreamInfo | None:
        return next((s for s in self.streams if s.codec_type == "video"), None)

    @property
    def audio(self) -> StreamInfo | None:
        return next((s for s in self.streams if s.codec_type == "audio"), None)

    @property
    def has_video(self) -> bool:
        video = self.video
        if video is None:
            return False
        # Attached album art is a single JPEG "video" stream — treat as none.
        if video.codec_name == "mjpeg" and (video.duration or 0) < 0.08:
            return False
        return True

    @property
    def has_audio(self) -> bool:
        return self.audio is not None

    @property
    def width(self) -> int:
        return int(self.video.width) if self.video and self.video.width else 0

    @property
    def height(self) -> int:
        return int(self.video.height) if self.video and self.video.height else 0

    @property
    def fps(self) -> float:
        return float(self.video.fps) if self.video and self.video.fps else 30.0


def _to_float(value: Any) -> float | None:
    if value in (None, "", "N/A"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_fps(rate: str | None) -> float | None:
    if not rate or rate in {"0/0", "N/A"}:
        return None
    if "/" in rate:
        num, den = rate.split("/", 1)
        d = float(den)
        return float(num) / d if d else None
    return _to_float(rate)


def probe(path: str | Path) -> MediaInfo:
    src = Path(path).expanduser().resolve()
    if not src.is_file():
        raise ProbeError(f"file does not exist: {src}")
    argv = [
        resolve_ffprobe(),
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(src),
    ]
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise ProbeError(f"ffprobe failed for {src}: {proc.stderr.strip()}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"ffprobe returned invalid JSON for {src}") from exc
    fmt = data.get("format") or {}
    streams: list[StreamInfo] = []
    for raw in data.get("streams") or []:
        duration = _to_float(raw.get("duration")) or _to_float(fmt.get("duration"))
        streams.append(
            StreamInfo(
                index=int(raw.get("index", 0)),
                codec_type=str(raw.get("codec_type") or ""),
                codec_name=str(raw.get("codec_name") or ""),
                duration=duration,
                width=int(raw["width"]) if raw.get("width") else None,
                height=int(raw["height"]) if raw.get("height") else None,
                fps=_parse_fps(raw.get("avg_frame_rate")) or _parse_fps(raw.get("r_frame_rate")),
                sample_rate=int(raw["sample_rate"]) if raw.get("sample_rate") else None,
                channels=int(raw["channels"]) if raw.get("channels") else None,
                bit_rate=int(raw["bit_rate"]) if str(raw.get("bit_rate") or "").isdigit() else None,
                pix_fmt=raw.get("pix_fmt"),
                tags={str(k): str(v) for k, v in (raw.get("tags") or {}).items()},
            )
        )
    duration = _to_float(fmt.get("duration")) or 0.0
    if duration <= 0:
        for s in streams:
            if s.duration and s.duration > duration:
                duration = s.duration
    return MediaInfo(
        path=src,
        duration=float(duration),
        format_name=str(fmt.get("format_name") or ""),
        size_bytes=int(fmt["size"]) if str(fmt.get("size") or "").isdigit() else src.stat().st_size,
        bit_rate=int(fmt["bit_rate"]) if str(fmt.get("bit_rate") or "").isdigit() else None,
        streams=tuple(streams),
    )


@dataclass
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run_command(
    argv: list[str],
    *,
    timeout: float | None = None,
    cwd: str | Path | None = None,
) -> CommandResult:
    import time

    started = time.perf_counter()
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
            check=False,
        )
    except FileNotFoundError as exc:
        raise BinaryMissing(f"executable not found: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RenderError(f"timed out after {timeout}s: {' '.join(argv[:6])}") from exc
    elapsed = int((time.perf_counter() - started) * 1000)
    return CommandResult(
        argv=list(argv),
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        duration_ms=elapsed,
    )


def run_ffmpeg(args: list[str], *, timeout: float | None = None) -> CommandResult:
    argv = [resolve_ffmpeg(), "-hide_banner", "-y", *args]
    result = run_command(argv, timeout=timeout)
    if not result.ok:
        tail = result.stderr.strip().splitlines()[-12:]
        raise RenderError(
            "ffmpeg failed ("
            + str(result.returncode)
            + "):\n  "
            + " ".join(argv)
            + "\n"
            + "\n".join(tail)
        )
    return result


def shell_join(argv: list[str]) -> str:
    import shlex

    return " ".join(shlex.quote(part) for part in argv)


def escape_filter_path(path: str | Path) -> str:
    text = str(path)
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("'", r"\'")


def escape_drawtext(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\u2019")
        .replace("%", "\\%")
    )


@lru_cache(maxsize=1)
def _filter_names() -> frozenset[str]:
    result = run_command([resolve_ffmpeg(), "-hide_banner", "-filters"])
    names: set[str] = set()
    for line in result.stdout.splitlines():
        match = re.match(r"\s*\S+\s+(\w+)", line)
        if match:
            names.add(match.group(1))
    return frozenset(names)


def has_filter(name: str) -> bool:
    return name in _filter_names()


@lru_cache(maxsize=1)
def _encoder_names() -> frozenset[str]:
    result = run_command([resolve_ffmpeg(), "-hide_banner", "-encoders"])
    names: set[str] = set()
    for line in result.stdout.splitlines():
        match = re.match(r"\s*\S+\s+(\S+)", line)
        if match:
            names.add(match.group(1))
    return frozenset(names)


def has_encoder(name: str) -> bool:
    return name in _encoder_names()


def version_line() -> str:
    result = run_command([resolve_ffmpeg(), "-version"])
    return (result.stdout.splitlines() or ["unknown"])[0]


def is_image(path: Path) -> bool:
    return path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}


def is_audio(path: Path) -> bool:
    return path.suffix.lower() in {".wav", ".mp3", ".aac", ".m4a", ".flac", ".ogg", ".opus", ".aiff"}
