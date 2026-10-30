"""FFmpeg process control, media probe, filter escaping, host capabilities."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from vedit.errors import BinaryMissing, ProbeError, RenderCancelled, RenderError


def trace(step: str) -> None:
    """One line on stderr when VEDIT_TRACE=1. The studio log is stderr."""
    if os.environ.get("VEDIT_TRACE") != "1":
        return
    sys.stderr.write(f"vedit-trace {step}\n")
    sys.stderr.flush()

FULL_FFMPEG = Path("/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg")
FULL_FFPROBE = Path("/opt/homebrew/opt/ffmpeg-full/bin/ffprobe")
DEFAULT_FONT = Path("/System/Library/Fonts/Supplemental/Arial.ttf")
HELVETICA = Path("/System/Library/Fonts/Helvetica.ttc")
LINUX_FONTS = (
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
    Path("/usr/share/fonts/truetype/freefont/FreeSans.ttf"),
    Path("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf"),
    Path("/usr/share/fonts/TTF/DejaVuSans.ttf"),
)
_PROBE_TIMEOUT_SECONDS = 30.0
_CANCEL_POLL_SECONDS = 0.1
_TERMINATE_GRACE_SECONDS = 0.5
# Nested helpers must not orphan their independent groups when cancelled.
# Cancellable root commands also watch their owner for abrupt Studio crashes.
# Only descendants receive this marker.
_MANAGED_COMMAND_ENV = "_VEDIT_MANAGED_COMMAND"


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
    for candidate in (DEFAULT_FONT, HELVETICA, *LINUX_FONTS):
        if candidate.is_file():
            return candidate
    fc = shutil.which("fc-match")
    if fc:
        result = run_command([fc, "-f", "%{file}", "sans"])
        text = (result.stdout or "").strip().split(":")[0].strip()
        if text:
            found = Path(text)
            if found.is_file():
                return found
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
    color_primaries: str | None = None
    color_trc: str | None = None
    color_space: str | None = None
    color_range: str | None = None
    tags: dict[str, str] = field(default_factory=dict)
    rotation: float = 0.0


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
    def display_width(self) -> int:
        """Width after FFmpeg applies a phone video's display rotation."""
        return self.height if self.video and round(self.video.rotation) % 180 == 90 else self.width

    @property
    def display_height(self) -> int:
        return self.width if self.video and round(self.video.rotation) % 180 == 90 else self.height

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


def _rotation(raw: dict[str, Any]) -> float:
    # Display Matrix side data supersedes the legacy QuickTime rotate tag.
    for entry in raw.get("side_data_list") or []:
        if isinstance(entry, dict) and "rotation" in entry:
            value = _to_float(entry["rotation"])
            if value is not None and math.isfinite(value):
                return value % 360
    value = _to_float((raw.get("tags") or {}).get("rotate"))
    return value % 360 if value is not None and math.isfinite(value) else 0.0


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
    try:
        proc = run_command(argv, timeout=_PROBE_TIMEOUT_SECONDS)
    except RenderError as exc:
        raise ProbeError(
            f"ffprobe could not inspect {src}: {exc}. "
            "Check that the file is readable and complete; copy network media to local storage and retry."
        ) from exc
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
                color_primaries=raw.get("color_primaries"),
                color_trc=raw.get("color_transfer"),
                color_space=raw.get("color_space"),
                color_range=raw.get("color_range"),
                tags={str(k): str(v) for k, v in (raw.get("tags") or {}).items()},
                rotation=_rotation(raw),
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


def _stop_process_tree(proc: subprocess.Popen[str]) -> None:
    """Stop only this command's session, even if its leader has already exited.

    A child may retain captured pipes or ignore SIGTERM after its parent exits.
    Always send the final SIGKILL to the owned process group, then reap the
    direct child. The caller closes pipes even if a daemon escaped the group.
    """
    def send(sig: int) -> None:
        try:
            if os.name == "posix":
                os.killpg(proc.pid, sig)
            elif proc.poll() is None:
                if sig == signal.SIGTERM:
                    proc.terminate()
                else:
                    proc.kill()
        except ProcessLookupError:
            pass

    send(signal.SIGTERM)
    try:
        proc.communicate(timeout=_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    finally:
        # Do this even when communicate returned: descendants may have closed
        # their inherited pipes while still doing expensive work.
        send(signal.SIGKILL if os.name == "posix" else signal.SIGTERM)
        if os.name != "posix" and proc.poll() is None:
            proc.kill()
    try:
        proc.communicate(timeout=_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        # An escaped daemon holding our pipes must not prevent cancellation.
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                pipe.close()
        proc.wait(timeout=_TERMINATE_GRACE_SECONDS)


def run_command(
    argv: list[str],
    *,
    timeout: float | None = None,
    cwd: str | Path | None = None,
    stream_stderr: bool = False,
    cancel_event: threading.Event | None = None,
) -> CommandResult:
    """Run a command, preserving its exit status and optional live stderr.

    POSIX commands own a new session. Cancellation and timeout stop that
    session's process group before raising, including nested render helpers.
    Nested vedit calls and cancellable root commands use a small supervisor
    watching a pipe owned by their caller: if that caller dies, their entire
    new group is killed too. Ordinary probes retain their direct launch path.
    """
    if cancel_event is not None and cancel_event.is_set():
        raise RenderCancelled("render cancelled before starting a command")
    started = time.perf_counter()
    deadline = started + timeout if timeout is not None else None
    env = os.environ.copy()
    supervised = os.name == "posix" and (
        env.get(_MANAGED_COMMAND_ENV) == "1" or cancel_event is not None
    )
    env[_MANAGED_COMMAND_ENV] = "1"
    guard_read = guard_write = error_read = error_write = None
    launch_argv = argv
    pass_fds: tuple[int, ...] = ()
    if supervised:
        guard_read, guard_write = os.pipe()
        error_read, error_write = os.pipe()
        pass_fds = (guard_read, error_write)
        supervisor = Path(__file__).with_name("_process_guard.py")
        launch_argv = [sys.executable, str(supervisor), str(guard_read), str(error_write), *argv]
    try:
        # Inherit stderr when streaming so a TTY shows ffmpeg stats without
        # filling a captured pipe (pytest / CI deadlock).
        proc = subprocess.Popen(
            launch_argv,
            stdout=subprocess.PIPE,
            stderr=None if stream_stderr else subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(cwd) if cwd else None,
            start_new_session=os.name == "posix",
            env=env,
            pass_fds=pass_fds,
        )
    except BaseException as exc:
        for fd in (guard_write, error_read):
            if fd is not None:
                os.close(fd)
        if isinstance(exc, FileNotFoundError):
            raise BinaryMissing(f"executable not found: {argv[0]}") from exc
        raise
    finally:
        for fd in (guard_read, error_write):
            if fd is not None:
                os.close(fd)
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise RenderCancelled("render cancelled; running command stopped")
            remaining = deadline - time.perf_counter() if deadline is not None else None
            if remaining is not None and remaining <= 0:
                raise RenderError(f"timed out after {timeout}s: {' '.join(argv[:6])}")
            interval = _CANCEL_POLL_SECONDS if cancel_event is not None else remaining
            if remaining is not None and interval is not None:
                interval = min(interval, remaining)
            try:
                stdout, stderr = proc.communicate(timeout=interval)
                break
            except subprocess.TimeoutExpired:
                continue
        if cancel_event is not None and cancel_event.is_set():
            raise RenderCancelled("render cancelled; running command stopped")
        if error_read is not None:
            startup_error = os.read(error_read, 65536)
            if startup_error:
                errno, message, filename = json.loads(startup_error)
                exc = OSError(errno, message, filename)
                if isinstance(exc, FileNotFoundError):
                    raise BinaryMissing(f"executable not found: {argv[0]}") from exc
                raise exc
    except BaseException:
        _stop_process_tree(proc)
        raise
    finally:
        for fd in (guard_write, error_read):
            if fd is not None:
                os.close(fd)
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                pipe.close()
    elapsed = int((time.perf_counter() - started) * 1000)
    return CommandResult(
        argv=list(argv),
        returncode=proc.returncode,
        stdout=stdout or "",
        stderr="" if stream_stderr else (stderr or ""),
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
