"""FFmpeg process control, media probe, filter escaping, host capabilities."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import unicodedata
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

from vedit.errors import AnalysisError, BinaryMissing, CaptionError, ProbeError, RenderCancelled, RenderError, VeditError


def trace(step: str) -> None:
    """One line on stderr when VEDIT_TRACE=1. The studio log is stderr."""
    if os.environ.get("VEDIT_TRACE") != "1":
        return
    sys.stderr.write(f"vedit-trace {step}\n")
    sys.stderr.flush()

FULL_FFMPEG = Path("/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg")
FULL_FFPROBE = Path("/opt/homebrew/opt/ffmpeg-full/bin/ffprobe")
INTEL_FULL_FFMPEG = Path("/usr/local/opt/ffmpeg-full/bin/ffmpeg")
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
_SCAN_TIMEOUT_SECONDS = 120.0
_PLAYABLE_TIMEOUT_SECONDS = 60.0
_UNKNOWN_CODECS = frozenset({"", "unknown", "none"})
_HDR_TRANSFERS = frozenset({"smpte2084", "arib-std-b67"})
_INTERLACED_ORDERS = frozenset({"tt", "bb", "tb", "bt"})
# BT.2408 puts HDR reference white at 203 nits, which is SDR white. Measured on the iPhone
# HLG clips: npl=100 left 13 to 23% of pixels at Y >= 235 and lifted mid-tones 20 to 27%;
# npl=203 leaves none clipped, mid-tones within 8% of the signal. tonemap's default desat=2
# bleaches bright colour to white, so it is off. Mobius is linear below its knee, so skin
# keeps its level; hable at the same white point darkens phone footage by about a fifth.
HDR_REFERENCE_WHITE = 203
_HDR_TAIL = "format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=mobius:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p"
# FFmpeg's stream names for a matrix, and what the scale filter calls the same thing.
_SCALE_MATRICES = {"smpte170m": "smpte170m", "bt470bg": "bt470bg", "smpte240m": "smpte240m", "fcc": "fcc", "bt2020nc": "bt2020", "bt2020c": "bt2020"}
_RGB_PIXELS = ("rgb", "bgr", "gbr", "argb", "abgr")
_IMAGE_CODECS = frozenset({"png", "bmp", "tiff", "webp", "jpeg2000", "jpegls"})
# A transport or program stream seeks to a nearby keyframe, which can land past
# the request and leave nothing to decode. Read in early; decoding finds the moment.
LOOSE_SEEK_FORMATS = frozenset({"mpegts", "mpeg", "mpegvideo"})
SEEK_LEAD_IN_SECONDS = 3.0
# Rates players and platforms expect. Footage is conformed to the nearest; nothing exports above 60.
STANDARD_RATES = (24000 / 1001, 24.0, 25.0, 30000 / 1001, 30.0, 50.0, 60000 / 1001, 60.0)
DEFAULT_RATE = 30.0


def standard_rate(fps: float | None) -> float:
    """The standard frame rate nearest to ``fps`` (by ratio, so 23.976 and 24 stay apart), at most 60. Unknown is 30."""
    if fps is None or not math.isfinite(fps) or fps <= 0:
        return DEFAULT_RATE
    wanted = min(float(fps), STANDARD_RATES[-1])
    return min(STANDARD_RATES, key=lambda rate: abs(math.log(wanted / rate)))


def rate_text(fps: float) -> str:
    """A frame rate as FFmpeg's exact fraction, so 29.97 means 30000/1001 and never drifts."""
    from fractions import Fraction

    exact = Fraction(fps).limit_denominator(1001)
    return str(exact.numerator) if exact.denominator == 1 else f"{exact.numerator}/{exact.denominator}"
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
    # Homebrew keeps ffmpeg-full off PATH, and an Intel Mac's Homebrew lives elsewhere.
    if INTEL_FULL_FFMPEG.is_file():
        return str(INTEL_FULL_FFMPEG)
    found = shutil.which("ffmpeg")
    if not found:
        raise BinaryMissing("ffmpeg not found. `vedit setup` prints the install command for this machine.")
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


# Fonts for the scripts a Latin default lacks, on macOS and on Linux (Noto). resolve_font(text)
# checks each file's character map, so a missing or partial font is simply passed over.
_FONT_DIRS = ("/usr/share/fonts/opentype/noto", "/usr/share/fonts/noto-cjk", "/usr/share/fonts/google-noto-cjk", "/usr/share/fonts/truetype/noto",
              "/usr/share/fonts/noto", "/usr/share/fonts/google-noto")
SCRIPT_FONTS = {
    "ja": ("/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc", "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
           *(f"{root}/NotoSansCJK-Bold.ttc" for root in _FONT_DIRS), *(f"{root}/NotoSansCJK-Regular.ttc" for root in _FONT_DIRS)),
    "zh": ("/System/Library/Fonts/Hiragino Sans GB.ttc", "/System/Library/Fonts/STHeiti Medium.ttc"),
    "ko": ("/System/Library/Fonts/AppleSDGothicNeo.ttc",),
    "other": ("/System/Library/Fonts/Kohinoor.ttc", "/System/Library/Fonts/Supplemental/DevanagariMT.ttc", "/System/Library/Fonts/Supplemental/Ayuthaya.ttf",
              "/System/Library/Fonts/Supplemental/Tahoma.ttf", "/System/Library/Fonts/GeezaPro.ttc", "/System/Library/Fonts/SFHebrew.ttf",
              *(f"{root}/NotoSans{script}-{weight}.ttf" for script in ("Devanagari", "Thai", "Arabic", "Hebrew", "Bengali", "Tamil", "Telugu", "Gujarati",
                                                                         "Gurmukhi", "Kannada", "Malayalam", "Khmer", "Lao", "Myanmar", "Georgian", "Armenian", "Ethiopic", "Sinhala")
                for weight in ("Bold", "Regular") for root in _FONT_DIRS),
              "/Library/Fonts/Arial Unicode.ttf"),
}


def _font_order(text: str) -> list[str]:
    """Script fonts in the order this text wants them: kana means Japanese, Hangul Korean, other Han Chinese."""
    codes = [ord(char) for char in text]
    kana = any(0x3040 <= code <= 0x30FF or 0x31F0 <= code <= 0x31FF for code in codes)
    hangul = any(0xAC00 <= code <= 0xD7AF or 0x1100 <= code <= 0x11FF or 0x3130 <= code <= 0x318F for code in codes)
    first = ["ja", "zh", "ko"] if kana else ["ko", "ja", "zh"] if hangul else ["zh", "ja", "ko"]
    return [path for group in (*first, "other") for path in SCRIPT_FONTS[group]]


def _uncovered(path: Path, needed: set[str]) -> set[str] | None:
    """The characters of ``needed`` this font file cannot draw, or None when it cannot be read."""
    from vedit.caption_styles import font_face  # the sfnt reader lives with the caption layout that also needs it

    face = font_face(str(path))
    return None if face is None else {char for char in needed if face.advance(char) is None}


def _fc_font(char: str) -> Path | None:
    """The font fontconfig picks for one character, when fontconfig is installed."""
    fc = shutil.which("fc-match")
    if not fc:
        return None
    result = run_command([fc, "-f", "%{file}", f"sans:charset={ord(char):x}"])
    found = Path((result.stdout or "").strip().split(":")[0].strip() or ".")
    return found if found.is_file() else None


def resolve_font(text: str = "") -> Path:
    """The font file to draw ``text`` with, for renderers that take a single file and have no fallback.

    ``VEDIT_FONT`` wins whenever it can draw the text. With no text, or text the
    default font covers, the default stands. Otherwise the font that covers the
    most of the text's letters wins, from the script fonts on this machine and
    then fontconfig, so Japanese, Hindi or Thai never burn as empty boxes.
    """
    env = os.environ.get("VEDIT_FONT")
    base: Path | None = Path(env) if env and Path(env).is_file() else None
    for candidate in (DEFAULT_FONT, HELVETICA, *LINUX_FONTS):
        if base is None and candidate.is_file():
            base = candidate
    if base is None:
        fc = shutil.which("fc-match")
        if fc:
            result = run_command([fc, "-f", "%{file}", "sans"])
            found = (result.stdout or "").strip().split(":")[0].strip()
            if found and Path(found).is_file():
                base = Path(found)
    if base is None:
        raise BinaryMissing("No usable font. Set VEDIT_FONT to a .ttf/.otf file.")
    needed = {char for char in text if unicodedata.category(char)[0] in "LN"}
    if not needed:
        return base
    best, gap = base, _uncovered(base, needed)
    if gap is None:
        best, gap = base, needed
    for candidate in (DEFAULT_FONT, HELVETICA, *LINUX_FONTS, *map(Path, _font_order(text))):
        if not gap:
            break
        if candidate.is_file() and (missing := _uncovered(candidate, needed)) is not None and len(missing) < len(gap):
            best, gap = candidate, missing
    for _attempt in range(3):
        if not gap:
            break
        found = _fc_font(min(gap))
        missing = _uncovered(found, needed) if found else None
        if found is None or missing is None or len(missing) >= len(gap):
            break
        best, gap = found, missing
    return best


def find_whisper_cli() -> str | None:
    """Retired: Briefcut runs no local speech model and never starts whisper-cli. Always None.

    Kept only so setup screens that still look for it import cleanly.
    """
    return None


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
    start_time: float | None = None
    cover: bool = False
    default: bool = False
    codec_tag: str = ""
    field_order: str = ""
    sample_aspect_ratio: float = 1.0

    @property
    def readable(self) -> bool:
        """False when FFmpeg can see the stream but has no name for its codec."""
        return self.codec_name not in _UNKNOWN_CODECS

    @property
    def hdr(self) -> bool:
        return self.color_trc in _HDR_TRANSFERS


@dataclass(frozen=True)
class MediaInfo:
    path: Path
    duration: float
    format_name: str
    size_bytes: int
    bit_rate: int | None
    streams: tuple[StreamInfo, ...]
    # The container's own length. ``duration`` stops where the picture stops.
    file_duration: float = 0.0
    # Black bars found at ingest, as ``(width, height, x, y)`` of the picture inside the upright frame.
    crop: tuple[int, int, int, int] | None = None

    def _of_type(self, kind: str) -> list[StreamInfo]:
        return [s for s in self.streams if s.codec_type == kind]

    @property
    def video(self) -> StreamInfo | None:
        """The footage: not cover art, and not a small proxy that happens to come first."""
        pictures = self._of_type("video")
        if not pictures:
            return None
        return max(pictures, key=lambda s: (not s.cover, s.codec_name not in _IMAGE_CODECS, s.readable, (s.width or 0) * (s.height or 0)))

    @property
    def audio(self) -> StreamInfo | None:
        """The sound the render uses: a track FFmpeg can read, preferring the default one."""
        tracks = self._of_type("audio")
        if not tracks:
            return None
        return max(tracks, key=lambda s: (s.readable, s.default))

    @property
    def video_map(self) -> int:
        """``K`` in ``[n:v:K]``: the footage's position among the video streams."""
        return self._of_type("video").index(self.video) if self.video else 0

    @property
    def audio_map(self) -> int:
        """``K`` in ``[n:a:K]``: the sound's position among the audio streams."""
        return self._of_type("audio").index(self.audio) if self.audio else 0

    @property
    def has_video(self) -> bool:
        video = self.video
        if video is None or video.cover:
            return False
        # A lone JPEG, or art a muxer forgot to flag, is one picture and not a clip.
        if video.codec_name == "mjpeg" and (video.duration or 0) < 0.08:
            return False
        return True

    @property
    def has_audio(self) -> bool:
        audio = self.audio
        return audio is not None and audio.readable

    @property
    def width(self) -> int:
        return int(self.video.width) if self.video and self.video.width else 0

    @property
    def height(self) -> int:
        return int(self.video.height) if self.video and self.video.height else 0

    @property
    def frame_width(self) -> int:
        """Width of the whole frame after FFmpeg applies a phone video's display rotation, bars and all."""
        return self.height if self.video and round(self.video.rotation) % 180 == 90 else self.width

    @property
    def frame_height(self) -> int:
        return self.width if self.video and round(self.video.rotation) % 180 == 90 else self.height

    @property
    def display_width(self) -> int:
        """Width of the picture the edit uses: upright, and inside any black bars ``source_filters`` crops."""
        return self.crop[0] if self.crop else self.frame_width

    @property
    def display_height(self) -> int:
        return self.crop[1] if self.crop else self.frame_height

    @property
    def square_width(self) -> int:
        """Width once pixels are square, which is the shape ``SQUARE_PIXELS`` gives the picture."""
        if not self.video:
            return self.display_width
        aspect = self.video.sample_aspect_ratio
        # Turning the picture on its side turns its pixels too: FFmpeg's transpose inverts the aspect.
        return int(self.display_width / aspect) if round(self.video.rotation) % 180 == 90 else int(self.display_width * aspect)

    @property
    def fps(self) -> float:
        return float(self.video.fps) if self.video and self.video.fps else 30.0

    @property
    def seek_lead_in(self) -> float:
        """Seconds to read before a seek target so the frame there decodes; 0 for exact-seeking containers."""
        return SEEK_LEAD_IN_SECONDS if set(self.format_name.split(",")) & LOOSE_SEEK_FORMATS else 0.0


def _to_float(value: Any) -> float | None:
    if value in (None, "", "N/A"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sample_aspect(value: Any) -> float:
    """``16:11`` as a number. FFmpeg writes ``0:1`` and ``N/A`` when a file does not say, which means square."""
    parts = str(value or "").split(":")
    if len(parts) != 2 or not all(part.isdigit() and int(part) for part in parts):
        return 1.0
    return int(parts[0]) / int(parts[1])


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


def _tag_seconds(tags: dict[str, Any]) -> float | None:
    """Matroska keeps a track's length in a tag, as ``00:00:02.021000000``."""
    for key, value in tags.items():
        if str(key).upper().startswith("DURATION"):
            match = re.fullmatch(r"(\d+):(\d{2}):(\d{2}(?:\.\d+)?)", str(value).strip())
            if match:
                return int(match[1]) * 3600 + int(match[2]) * 60 + float(match[3])
    return None


def _packet_spans(src: Path) -> dict[int, tuple[float, float]]:
    """First and last timestamp of each stream, read from its packets.

    A file written to a stream, such as a browser's MediaRecorder WebM, never
    learns its own length. Reading packets decodes nothing, and it stops where
    the data stops, so it also finds the real end of a truncated file.
    """
    argv = [resolve_ffprobe(), "-v", "error", "-show_entries", "packet=stream_index,pts_time,duration_time", "-of", "csv=p=0", str(src)]
    try:
        lines = run_command(argv, timeout=_SCAN_TIMEOUT_SECONDS).stdout.splitlines()
    except RenderError:
        return {}
    spans: dict[int, tuple[float, float]] = {}
    for line in lines:
        fields = line.split(",")
        pts = _to_float(fields[1]) if len(fields) > 1 else None
        if pts is None or not fields[0].isdigit():
            continue
        end = pts + (_to_float(fields[2]) or 0.0 if len(fields) > 2 else 0.0)
        low, high = spans.get(int(fields[0]), (pts, end))
        spans[int(fields[0])] = (min(low, pts), max(high, end))
    return spans


def probe(path: str | Path) -> MediaInfo:
    src = Path(path).expanduser().resolve()
    if not src.is_file():
        raise ProbeError(f"file does not exist: {src}", plain="The source file is no longer in this project. Upload it again.")
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
            "Check that the file is readable and complete; copy network media to local storage and retry.",
            plain="Reading this file took too long. Copy it to a local drive, then upload it again.",
        ) from exc
    if proc.returncode != 0:
        raise ProbeError(f"ffprobe failed for {src}: {proc.stderr.strip()}", plain=_unreadable_file(src, proc.stderr))
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"ffprobe returned invalid JSON for {src}") from exc
    fmt = data.get("format") or {}
    format_name = str(fmt.get("format_name") or "")
    # -ss counts from the file's first timestamp, so that is where this clock starts.
    start = _to_float(fmt.get("start_time")) or 0.0
    length = _to_float(fmt.get("duration")) or 0.0
    if start > 0 and "matroska" in format_name:
        # Matroska records its last timestamp, not its running time.
        length = max(0.0, length - start)
    ends: dict[int, float] = {}
    streams: list[StreamInfo] = []
    for raw in data.get("streams") or []:
        duration = _to_float(raw.get("duration")) or _to_float(fmt.get("duration"))
        tags = raw.get("tags") or {}
        own = _to_float(raw.get("duration")) or _tag_seconds(tags)
        begins = _to_float(raw.get("start_time"))
        disposition = raw.get("disposition") or {}
        if own:
            ends[int(raw.get("index", 0))] = (start if begins is None else begins) - start + own
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
                tags={str(k): str(v) for k, v in tags.items()},
                rotation=_rotation(raw),
                start_time=begins,
                cover=bool(disposition.get("attached_pic") or disposition.get("timed_thumbnails")),
                default=bool(disposition.get("default")),
                codec_tag=str(raw.get("codec_tag_string") or ""),
                field_order=str(raw.get("field_order") or ""),
                sample_aspect_ratio=_sample_aspect(raw.get("sample_aspect_ratio")),
            )
        )
    if length <= 0:
        for s in streams:
            if s.duration and s.duration > length:
                length = s.duration
    if length <= 0:
        spans = _packet_spans(src)
        if spans:
            first = min(low for low, _ in spans.values())
            length = max(high for _, high in spans.values()) - first
            ends = {index: high - first for index, (_, high) in spans.items()}
    info = MediaInfo(
        path=src,
        duration=float(length),
        format_name=format_name,
        size_bytes=int(fmt["size"]) if str(fmt.get("size") or "").isdigit() else src.stat().st_size,
        bit_rate=int(fmt["bit_rate"]) if str(fmt.get("bit_rate") or "").isdigit() else None,
        streams=tuple(streams),
        file_duration=float(length),
    )
    # A clip ends with its picture: past that point there is nothing to cut to.
    end = ends.get(info.video.index) if info.has_video and info.video else None
    if end and end < length:
        info = replace(info, duration=end)
    # Border detection decodes frames, so it runs once at ingest; every later probe reads its answer.
    known = _cached_borders(src) if info.has_video else None
    return replace(info, crop=known[0]) if known else info


# ---------------------------------------------------------------------------
# Letterbox and pillarbox bars
# ---------------------------------------------------------------------------

BORDER_SAMPLES = 5
_BORDER_FRAMES = 4  # per sample; cropdetect keeps the widest picture it saw across them
_BORDER_MIN_SHARE = 0.04  # bars thinner than this, both sides together, are left alone
# Never crop to less than this share of the frame: a small subject on flat black is not a picture in bars.
# Archival film windowboxed in an HD frame, the smallest seen, keeps 9%.
_BORDER_MIN_PICTURE = 0.08
_BORDER_FLAT = 30  # brightest bar pixel, 8-bit limited luma: bars sit at 16, a dark scene does not stay under this
_BORDER_SKEW = 0.25  # bars come in pairs: the two sides may differ by this share of the larger one
_CROP_LINE = re.compile(r"crop=(-?\d+):(-?\d+):(-?\d+):(-?\d+)")


def _borders_cache(src: Path) -> Path:
    stat = src.stat()
    key = hashlib.sha256(f"{src}|{stat.st_size}|{stat.st_mtime_ns}".encode()).hexdigest()[:32]
    home = os.environ.get("VEDIT_HOME", "").strip()
    return (Path(home).expanduser() if home else Path.home() / ".vedit") / "cache" / "borders" / f"{key}.json"


def _cached_borders(src: Path) -> tuple[tuple[int, int, int, int] | None] | None:
    """``(crop,)`` when this exact file was checked, None when it never was."""
    try:
        data = json.loads(_borders_cache(src).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    crop = data.get("crop") if isinstance(data, dict) else None
    if isinstance(crop, list) and len(crop) == 4 and all(isinstance(value, int) and value >= 0 for value in crop) and crop[0] > 0 and crop[1] > 0:
        return (tuple(crop),)  # type: ignore[return-value]
    return (None,) if isinstance(data, dict) and "crop" in data else None


def _sample_times(info: MediaInfo) -> list[float]:
    seconds = max(0.0, info.duration - 0.05)
    return [round(seconds * (index + 0.5) / BORDER_SAMPLES, 3) for index in range(BORDER_SAMPLES)]


def _seek(info: MediaInfo, at: float) -> list[str]:
    begin = max(0.0, at - info.seek_lead_in)
    return ["-ss", f"{begin:.3f}", "-i", str(info.path)] + (["-ss", f"{at - begin:.3f}"] if at > begin else [])


def _active_area(info: MediaInfo, at: float) -> tuple[int, int, int, int] | None:
    """The non-black box cropdetect finds in a few frames from ``at``. None for a frame that is black throughout."""
    argv = [resolve_ffmpeg(), "-hide_banner", "-nostats", *_seek(info, at), "-map", f"0:v:{info.video_map}", "-an", "-frames:v", str(_BORDER_FRAMES),
            "-vf", "cropdetect=round=2:reset=0:skip=0", "-f", "null", "-"]
    try:
        found = _CROP_LINE.findall(run_command(argv, timeout=_PLAYABLE_TIMEOUT_SECONDS).stderr)
    except RenderError:
        return None
    if not found:
        return None
    width, height, x, y = (int(value) for value in found[-1])
    return (width, height, x, y) if width > 0 and height > 0 else None


def _bars_are_flat(info: MediaInfo, times: list[float], box: tuple[int, int, int, int]) -> bool:
    """Bars are uniformly dark. A night scene that cropdetect also reads as black has texture somewhere."""
    full_w, full_h = info.frame_width, info.frame_height
    small_w, small_h = max(2, full_w // 8 * 2), max(2, full_h // 8 * 2)  # a quarter, kept even
    sx, sy = small_w / full_w, small_h / full_h
    width, height, x, y = box
    margin = 2
    bands = [(0, 0, int(x * sx) - margin, small_h), (int((x + width) * sx) + margin, 0, small_w, small_h),
             (0, 0, small_w, int(y * sy) - margin), (0, int((y + height) * sy) + margin, small_w, small_h)]
    for at in times:
        argv = [resolve_ffmpeg(), "-hide_banner", "-v", "error", *_seek(info, at), "-map", f"0:v:{info.video_map}", "-an", "-frames:v", "1",
                "-vf", f"scale={small_w}:{small_h}:flags=neighbor,format=yuv420p", "-f", "rawvideo", "-"]
        try:
            # Raw pixels: run_command decodes its output as text.
            raw = subprocess.run(argv, capture_output=True, timeout=_PLAYABLE_TIMEOUT_SECONDS, check=False).stdout
        except (OSError, subprocess.TimeoutExpired):
            return False
        if len(raw) < small_w * small_h:
            return False
        for left, top, right, bottom in bands:
            left, right, top, bottom = max(0, left), min(small_w, right), max(0, top), min(small_h, bottom)
            if right <= left or bottom <= top:
                continue  # no bar on this side
            for row in range(top, bottom):
                if max(raw[row * small_w + left: row * small_w + right]) > _BORDER_FLAT:
                    return False
    return True


def detect_borders(info: MediaInfo) -> tuple[int, int, int, int] | None:
    """The picture inside stable black bars, as ``(width, height, x, y)``; None when there are none.

    Conservative by design. A side counts as a bar only when it is black in every
    sampled frame across the clip (the union of what cropdetect sees), the two
    sides of an axis roughly match, the bars are flat rather than a dark scene,
    and what is left is a real share of the frame. The answer is cached by file,
    so ``probe`` reports it from then on.
    """
    if not info.has_video or info.video is None or info.duration <= 0:
        return None
    known = _cached_borders(info.path)
    if known is not None:
        return known[0]
    crop = _measure_borders(info)
    try:
        cache = _borders_cache(info.path)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"crop": list(crop) if crop else None}), encoding="utf-8")
    except OSError:
        pass
    return crop


def _measure_borders(info: MediaInfo) -> tuple[int, int, int, int] | None:
    full_w, full_h = info.frame_width, info.frame_height
    if full_w < 16 or full_h < 16:
        return None
    times = _sample_times(info)
    boxes = [box for box in (_active_area(info, at) for at in times) if box is not None]
    if len(boxes) < min(3, len(times)):
        return None
    left = min(x for _, _, x, _ in boxes)
    top = min(y for _, _, _, y in boxes)
    right = full_w - max(x + width for width, _, x, _ in boxes)
    bottom = full_h - max(y + height for _, height, _, y in boxes)
    sides = []
    for low, high, span in ((left, right, full_w), (top, bottom, full_h)):
        low, high = max(0, low), max(0, high)
        paired = abs(low - high) <= max(8, _BORDER_SKEW * max(low, high))
        sides.append((low, high) if low + high >= _BORDER_MIN_SHARE * span and paired else (0, 0))
    (left, right), (top, bottom) = sides
    if not (left or right or top or bottom):
        return None
    # Even sizes and offsets keep 4:2:0 chroma aligned.
    x, y = left // 2 * 2, top // 2 * 2
    width, height = (full_w - x - right) // 2 * 2, (full_h - y - bottom) // 2 * 2
    if width * height < _BORDER_MIN_PICTURE * full_w * full_h:
        return None
    box = (width, height, x, y)
    return box if _bars_are_flat(info, times, box) else None


def with_borders(info: MediaInfo) -> MediaInfo:
    """``info`` with its black bars measured, once per file."""
    if info.crop is not None or not info.has_video:
        return info
    crop = detect_borders(info)
    return replace(info, crop=crop) if crop else info


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
        except (ProcessLookupError, PermissionError):
            # macOS answers EPERM while the group's last members are exiting.
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
            + "\n".join(tail),
            detail=result.stderr,
        )
    return result


def shell_join(argv: list[str]) -> str:
    import shlex

    return " ".join(shlex.quote(part) for part in argv)


def escape_filter_path(path: str | Path) -> str:
    text = str(path)
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("'", r"\'")


def filter_value(value: str | Path) -> str:
    """Escape one option value for a filtergraph, for use without surrounding quotes.

    FFmpeg parses a filtergraph twice, first the graph and then each option, and
    each pass has its own special characters. Escaping for both keeps any path
    intact, including apostrophes, which no quoted form can carry.
    """
    option = re.sub(r"([\\':])", r"\\\1", str(value))
    return re.sub(r"([\\'\[\],;])", r"\\\1", option)


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


@lru_cache(maxsize=16)
def filter_options(name: str) -> frozenset[str]:
    """The option names this FFmpeg's filter takes. Builds differ, as distribution packages lag."""
    result = run_command([resolve_ffmpeg(), "-hide_banner", "-h", f"filter={name}"])
    return frozenset(re.findall(r"^\s+(\w+)\s+<", result.stdout, re.MULTILINE))


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


@lru_cache(maxsize=1)
def ffmpeg_major() -> int:
    """FFmpeg's major version. A build with no number is a recent git build."""
    match = re.search(r"version n?(\d+)\.", version_line())
    return int(match.group(1)) if match else 99


def filter_file_args(path: str | Path) -> list[str]:
    """Read a filter graph from a file. FFmpeg 7 changed the option; Ubuntu 24.04 has 6.1."""
    return ["-/filter_complex" if ffmpeg_major() >= 7 else "-filter_complex_script", str(path)]


def version_line() -> str:
    result = run_command([resolve_ffmpeg(), "-version"])
    return (result.stdout.splitlines() or ["unknown"])[0]


def is_image(path: Path) -> bool:
    return path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}


def is_audio(path: Path) -> bool:
    return path.suffix.lower() in {".wav", ".mp3", ".aac", ".m4a", ".flac", ".ogg", ".opus", ".aiff"}


# ---------------------------------------------------------------------------
# What went wrong, for the person who chose the file
# ---------------------------------------------------------------------------

CONVERT = "Convert it to H.264 MP4, then upload that."
RETRY_SAVED = "Your clip and plan are saved."
_INTERRUPTED = "The recording or copy was probably interrupted. Copy or export it again, then upload the new file."

# First match wins. Environment problems come first: a full disk says nothing about the clip.
_FAILURES = tuple(
    (re.compile(pattern, re.I), message)
    for pattern, message in (
        (r"No space left on device|Disk quota exceeded", f"The drive holding this project is full. Free some space, then retry. {RETRY_SAVED}"),
        (r"Permission denied|Operation not permitted", f"Briefcut was not allowed to read or write a file in this project. Check the folder's permissions, then retry. {RETRY_SAVED}"),
        (r"Cannot allocate memory|out of memory", f"This machine ran out of memory on this video. Close other apps and retry, or use a shorter or smaller clip. {RETRY_SAVED}"),
        (r"timed out after", f"This step took too long and was stopped. Retry when the machine is less busy, or use a shorter clip. {RETRY_SAVED}"),
        (r"moov atom not found|partial file", f"This file is incomplete. {_INTERRUPTED}"),
        (r"no decoder found|Decoder \S* ?not found|Unknown decoder|Failed to open decoder|Error while opening decoder|Could not find codec parameters|Unsupported codec",
         f"This clip uses a format FFmpeg on this machine cannot decode. {CONVERT}"),
        (r"Decoding error|Error while decoding|Invalid NAL unit|corrupt|concealing|non-existing PPS|Invalid data found when processing input",
         f"Part of this clip is damaged and could not be decoded. Check that it plays to the end, or export it again, then upload the new file. {RETRY_SAVED}"),
        (r"Unknown encoder|Encoder \S* ?not found|No such filter|Filter not found", f"Your FFmpeg build is missing something this edit needs. Install ffmpeg-full, then retry. {RETRY_SAVED}"),
        (r"matches no streams|does not contain any stream|Nothing was written|Output file is empty|Could not open encoder before EOF",
         f"One of your moments has no picture or sound in this file. Move it to a part of the clip that plays, then retry. {RETRY_SAVED}"),
    )
)
_STEPS = (
    ("select_ranges", "Assembling your moments"), ("captions", "Making captions"), ("export", "The export"),
    ("verify", "Checking the export"), ("thumbnail", "Making the cover frame"), ("cut_silence", "Trimming pauses"),
    ("spatial_effect", "The spatial effect"), ("music", "Adding music"), ("concat", "Joining your clips"),
)
_ERROR_LINE = re.compile(r"error|fail|invalid|cannot|could not|unable|not found|too large|unsupported|out of range", re.I)
_SAFE_REASON = re.compile(r"[A-Za-z][A-Za-z ,'-]{5,90}")
_PATH = re.compile(r"(?:[A-Za-z]:)?[\\/][^\s'\"]*")


def _unreadable_file(src: Path, stderr: str) -> str:
    """Why ffprobe gave up. Extensions choose the demuxer, so read the bytes as well."""
    try:
        with src.open("rb") as handle:
            head = handle.read(16)
    except OSError:
        return "This file could not be opened. Check that it is readable, then upload it again."
    if not head:
        return "This file is empty. Export or copy it again, then upload the new file."
    if "moov atom not found" in stderr and head[4:8] in {b"ftyp", b"wide", b"free", b"mdat", b"skip"}:
        return f"This file is incomplete: its index is missing. {_INTERRUPTED}"
    return f"This file is not a video FFmpeg can read, whatever its name says. Check that it plays. {CONVERT}"


def source_problem(info: MediaInfo, *, min_seconds: float, max_seconds: float) -> str | None:
    """What stops this file being edited, in plain words; None when nothing does."""
    video = info.video
    if video is not None and not video.cover and not video.readable:
        named = f" ({video.codec_tag})" if video.codec_tag.isalnum() else ""
        return f"This clip's picture uses a format FFmpeg cannot decode{named}. {CONVERT}"
    if not info.has_video:
        if info.has_audio:
            return "This file has sound but no picture. Choose a video clip."
        return "This file has no picture or sound that can be read. Check that it plays, then upload it again."
    seconds = info.duration
    if not math.isfinite(seconds) or seconds <= 0:
        return f"This clip's length could not be measured. {CONVERT}"
    if seconds <= min_seconds:
        if info.file_duration > seconds + 0.5:
            return f"The picture in this file stops after {seconds:.2f} seconds, while its sound runs for {info.file_duration:.1f}. Export it again with video the whole way through."
        return f"This clip is only {seconds:.2f} seconds long. Choose a clip longer than {min_seconds:g} seconds."
    if seconds > max_seconds:
        return f"This clip runs {seconds / 60:.0f} minutes. Studio edits clips up to {max_seconds / 60:.0f} minutes, so trim it first, then upload it again."
    return None


def source_filters(info: MediaInfo) -> str:
    """Turn the footage into what every export claims it is: progressive, 8-bit limited BT.709.

    Exports tag frames BT.709 with ``setparams``, which changes the label and not
    the pixels, so anything else has to be converted here first. Only a tag that
    says otherwise changes a pixel: untagged video passes through untouched.
    """
    video = info.video
    if video is None:
        return ""
    bars = f"crop={info.crop[0]}:{info.crop[1]}:{info.crop[2]}:{info.crop[3]}" if info.crop else ""
    return ",".join(part for part in (_deinterlace(video), bars, _to_rec709(video)) if part)


def _deinterlace(video: StreamInfo) -> str:
    """Camcorder and broadcast footage. Only frames flagged interlaced are touched."""
    if video.field_order not in _INTERLACED_ORDERS:
        return ""
    name = next((name for name in ("bwdif", "yadif") if has_filter(name)), "")
    return f"{name}=mode=send_frame:deint=interlaced" if name else ""


def tone_map(video: StreamInfo | None = None) -> str:
    """The one HDR-to-SDR chain, for single clips and projects alike: PQ or HLG to 8-bit limited BT.709.

    A stream's own tags are passed in, and a missing primaries or matrix tag is read
    as BT.2020 (PQ and HLG are defined on it), so zscale always has a path. Empty
    when this FFmpeg has no zscale or tonemap.
    """
    if not (has_filter("zscale") and has_filter("tonemap")):
        return ""
    given: list[str] = []
    if video is not None:
        if video.color_trc in _HDR_TRANSFERS:
            given.append(f"tin={video.color_trc}")
        if video.color_primaries in (None, "", "unknown"):
            given += ["pin=bt2020", "p=bt2020"]  # the output keeps the input's primaries, so name them both
        if video.color_space in (None, "", "unknown"):
            given.append("min=bt2020nc")
    return "zscale=" + ":".join([*given, "t=linear", f"npl={HDR_REFERENCE_WHITE}"]) + "," + _HDR_TAIL


def _to_rec709(video: StreamInfo) -> str:
    if video.hdr:
        return tone_map(video)
    options: list[str] = []
    matrix = _SCALE_MATRICES.get(video.color_space or "")
    if matrix:
        options.append(f"in_color_matrix={matrix}")
    if video.color_range == "pc" or (video.pix_fmt or "").startswith("yuvj"):
        options.append("in_range=full")
    if not options and not (video.pix_fmt or "").startswith(_RGB_PIXELS):
        return ""
    return "scale=" + ":".join([*options, "out_color_matrix=bt709", "out_range=limited"]) + ",format=yuv420p"


def source_notes(info: MediaInfo) -> list[str]:
    """Choices made about a file that the person who chose it should see on the plan."""
    notes: list[str] = []
    video, audio = info.video, info.audio
    footage = [s for s in info.streams if s.codec_type == "video" and not s.cover and s.codec_name not in _IMAGE_CODECS]
    if len(footage) > 1 and video is not None:
        notes.append(f"This file holds {len(footage)} video streams. Briefcut uses the {video.width}x{video.height} one.")
    tracks = [s for s in info.streams if s.codec_type == "audio" and s.readable]
    if len(tracks) > 1:
        notes.append(f"This file holds {len(tracks)} sound tracks. Briefcut uses track {tracks.index(info.audio) + 1}. If that one is wrong, export the clip with the sound you want first.")
    if audio is not None and not audio.readable:
        notes.append("This clip's sound uses a format FFmpeg cannot read, so the finished video will be silent.")
    if video is not None and video.hdr and not (has_filter("zscale") and has_filter("tonemap")):
        notes.append("This clip is HDR, and this FFmpeg cannot convert it, so its colors will look flat. Install ffmpeg-full to fix that.")
    if info.file_duration > info.duration + 0.5:
        notes.append(f"The picture stops at {info.duration:.1f} seconds while the sound runs to {info.file_duration:.1f}. The sound after the last frame is left out.")
    return notes


def export_problem(output: MediaInfo, *, width: int, height: int, has_audio: bool, seconds: float, tolerance: float, color: bool = True) -> str | None:
    """Why a finished file must not be delivered, in plain words; None when it may be."""
    again = f"It was not delivered. {RETRY_SAVED} Retry once. If it happens again, convert the clip to H.264 MP4 and upload that."
    if not output.has_video:
        return f"The finished file has no picture. {again}"
    if (output.width, output.height) != (width, height):
        return f"The finished file came out {output.width}x{output.height} instead of {width}x{height}. {again}"
    if output.has_audio != has_audio:
        return f"The finished file {'lost its sound' if has_audio else 'gained a sound track the clip does not have'}. {again}"
    if abs(output.duration - seconds) > tolerance:
        return (f"The finished file runs {output.duration:.1f} seconds, not the {seconds:.1f} you approved. It was not delivered. "
                f"The clip may be damaged there. Choose other moments, or export the clip again and upload that. {RETRY_SAVED}")
    video = output.video
    if color and (video is None or video.color_space != "bt709" or video.color_range != "tv"):
        return f"The finished file's color information came out wrong. {again}"
    return None


def _frame_decodes(info: MediaInfo, at: float) -> bool:
    argv = [
        resolve_ffmpeg(), "-hide_banner", "-v", "error", "-ss", f"{at:.3f}", "-i", str(info.path),
        "-map", f"0:v:{info.video_map}", "-frames:v", "1", "-f", "framecrc", "-",
    ]
    try:
        result = run_command(argv, timeout=_PLAYABLE_TIMEOUT_SECONDS)
    except RenderError:
        return True  # Slow is not broken; a real problem still surfaces at render.
    return any(line and not line.startswith("#") for line in result.stdout.splitlines())


def check_playable(info: MediaInfo) -> None:
    """Decode a frame at each end, so a damaged upload fails now and says why.

    A truncated MP4 with its index at the front still probes at full length,
    and would otherwise only fail once the render reaches the missing footage.
    This is the ingest check, so it also measures black bars once; ``probe``
    reports them from then on.
    """
    _check_ends(info)
    detect_borders(info)


def _check_ends(info: MediaInfo) -> None:
    if not _frame_decodes(info, 0.0):
        raise ProbeError(
            f"no frame decodes at the start of {info.path}",
            plain=f"The picture in this clip could not be decoded. The file may be damaged. Check that it plays. {CONVERT}",
        )
    tail = info.duration - 1.0
    video = info.video
    if tail <= 0 or video is None or _frame_decodes(info, tail):
        return
    # A failed seek proves nothing on its own. The packets say where the data really stops.
    spans = _packet_spans(info.path)
    if video.index not in spans:
        return
    stops = spans[video.index][1] - min(low for low, _ in spans.values())
    if stops < tail:
        raise ProbeError(
            f"picture stops at {stops:.3f}s of {info.duration:.3f}s in {info.path}",
            plain=f"This file is cut short: it should run {info.duration:.1f} seconds, but its picture stops at {stops:.1f}. {_INTERRUPTED}",
        )


def _reason(detail: str) -> str:
    """FFmpeg's own words for a failure, only when they carry no path or address."""
    for line in reversed(detail.splitlines()):
        if not _ERROR_LINE.search(line):
            continue
        text = re.sub(r"^(?:\[[^\]]*\]\s*)+", "", line.strip()).rsplit(": ", 1)[-1].rstrip(".!")
        if _SAFE_REASON.fullmatch(text) and text.casefold() != "conversion failed":
            return text
    return ""


def _step_label(step: str) -> str:
    return next((label for key, label in _STEPS if step.startswith(key)), "This step")


def plain_failure(exc: BaseException) -> str:
    """One specific, plain message for a failed job. It never holds a command or a path."""
    plain = getattr(exc, "plain", None)
    if plain:
        return plain
    detail = getattr(exc, "detail", "") or str(exc)
    if isinstance(exc, (VeditError, OSError)):
        for pattern, message in _FAILURES:
            if pattern.search(detail):
                return message
    if isinstance(exc, BinaryMissing):
        return f"A program Briefcut needs is missing. Run vedit doctor to see what to install, then retry. {RETRY_SAVED}"
    if isinstance(exc, CaptionError):
        return f"The captions could not be written into the video. Run vedit doctor, or turn captions off to render now. {RETRY_SAVED}"
    if isinstance(exc, AnalysisError):
        if "audio track" in detail:
            return f"This clip has no sound, so pauses cannot be trimmed. Remove that instruction from the goal, then retry. {RETRY_SAVED}"
        if "silence" in detail:
            return f"This clip is silent from start to end, so trimming pauses would leave nothing. Remove that instruction from the goal, then retry. {RETRY_SAVED}"
        return f"Measuring this clip's sound did not finish. Retry, or remove the sound instructions from the goal. {RETRY_SAVED}"
    if isinstance(exc, ProbeError):
        return f"This file could not be read as a video. Check that it plays. {CONVERT}"
    if isinstance(exc, RenderError):
        reason = _reason(detail)
        stopped = f"{_step_label(getattr(exc, 'step', ''))} stopped" + (f": {reason}." if reason else ".")
        return f"{stopped} {RETRY_SAVED} Retry once. If it stops again, convert the clip to H.264 MP4 and upload that, or run vedit doctor."
    if isinstance(exc, VeditError):
        first = str(exc).strip().split("\n", 1)[0]
        text = _PATH.split(first, 1)[0].rstrip(" :=,(")
        if text:
            return f"{text[0].upper()}{text[1:].rstrip('.')}. {RETRY_SAVED}"
    return f"The edit could not finish. {RETRY_SAVED} Restart Studio and retry."
