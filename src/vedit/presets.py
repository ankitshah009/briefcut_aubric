"""Broadcast-safe export presets. Local codecs only."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass

from vedit.ir import Canvas
from vedit.media import STANDARD_RATES, has_encoder

BROADCAST_COLOR = (
    "-pix_fmt",
    "yuv420p",
    "-color_primaries",
    "bt709",
    "-color_trc",
    "bt709",
    "-colorspace",
    "bt709",
    "-color_range",
    "tv",
    "-movflags",
    "+faststart",
)


@dataclass(frozen=True)
class ExportPreset:
    name: str
    canvas: Canvas
    video_codec: str
    audio_codec: str
    crf: int
    audio_bitrate: str
    extra: tuple[str, ...]
    description: str
    container: str = "mp4"


def _youtube_x264(width: int, height: int, name: str, desc: str, fps: float = 30.0) -> ExportPreset:
    return ExportPreset(
        name=name,
        canvas=Canvas(width=width, height=height, fps=fps),
        video_codec="libx264",
        audio_codec="aac",
        crf=18,
        audio_bitrate="192k",
        extra=("-profile:v", "high", *BROADCAST_COLOR),
        description=desc,
    )


PRESETS: dict[str, ExportPreset] = {
    "youtube_1080p": _youtube_x264(1920, 1080, "youtube_1080p", "YouTube 1080p H.264 + AAC, CRF 18, +faststart"),
    "youtube_720p": _youtube_x264(1280, 720, "youtube_720p", "YouTube 720p"),
    "youtube_4k": _youtube_x264(3840, 2160, "youtube_4k", "YouTube 4K"),
    "shorts_9x16": _youtube_x264(1080, 1920, "shorts_9x16", "YouTube Shorts / Reels / TikTok 9:16"),
    "square_1x1": _youtube_x264(1080, 1080, "square_1x1", "Square 1:1 social"),
    "portrait_4x5": _youtube_x264(1080, 1350, "portrait_4x5", "Instagram feed 4:5"),
    "twitter_landscape": _youtube_x264(1280, 720, "twitter_landscape", "X/Twitter landscape"),
    "podcast_video": _youtube_x264(1920, 1080, "podcast_video", "Talking-head / podcast video"),
    "web_preview": ExportPreset(
        name="web_preview",
        canvas=Canvas(width=960, height=540, fps=30.0),
        video_codec="libx264",
        audio_codec="aac",
        crf=28,
        audio_bitrate="128k",
        extra=BROADCAST_COLOR,
        description="Small preview encode",
    ),
    "web_preview_9x16": ExportPreset(
        name="web_preview_9x16",
        canvas=Canvas(width=540, height=960, fps=30.0),
        video_codec="libx264",
        audio_codec="aac",
        crf=28,
        audio_bitrate="128k",
        extra=BROADCAST_COLOR,
        description="Small vertical preview encode",
    ),
    "web_preview_1x1": ExportPreset(
        name="web_preview_1x1",
        canvas=Canvas(width=540, height=540, fps=30.0),
        video_codec="libx264",
        audio_codec="aac",
        crf=28,
        audio_bitrate="128k",
        extra=BROADCAST_COLOR,
        description="Small square preview encode",
    ),
    "web_preview_4x5": ExportPreset(
        name="web_preview_4x5",
        # 4:5 at about 540 wide; 540x675 would give H.264 an odd height, so the frame is 544x680.
        canvas=Canvas(width=544, height=680, fps=30.0),
        video_codec="libx264",
        audio_codec="aac",
        crf=28,
        audio_bitrate="128k",
        extra=BROADCAST_COLOR,
        description="Small 4:5 portrait preview encode",
    ),
    "podcast_audio": ExportPreset(
        name="podcast_audio",
        canvas=Canvas(width=0, height=0, fps=0.0, sample_rate=48000),
        video_codec="",
        audio_codec="libmp3lame",
        crf=0,
        audio_bitrate="192k",
        extra=(),
        description="Audio-only MP3, 48 kHz",
        container="mp3",
    ),
    "master_aac": ExportPreset(
        name="master_aac",
        canvas=Canvas(width=1920, height=1080, fps=30.0),
        video_codec="libx264",
        audio_codec="aac",
        crf=16,
        audio_bitrate="256k",
        extra=("-profile:v", "high", *BROADCAST_COLOR),
        description="Higher-quality mezzanine H.264",
    ),
}


# Short side of the frame, so a quality means the same at any aspect.
EXPORT_SIZES = {"720p": 720, "1080p": 1080, "4k": 2160}
# Match the Studio export promise: GIFs support a short side up to 540 pixels.
GIF_SIZE = 540
GIF_FPS = 15
# The rate is a whole number, a broadcast rate such as 29.97, or ``source``: the footage's own, set when the render knows it.
_EXPORT_NAME = re.compile(r"export_(\d{3,4})x(\d{3,4})_(\d{1,3}(?:\.\d{1,3})?|source)_(mp4|mov|webm|gif)")
MATCH_SOURCE = "source"
_VP9 = ("-crf", "31", "-b:v", "0", "-row-mt", "1", "-cpu-used", "4", "-pix_fmt", "yuv420p",
        "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv")


def _rate_name(fps: float) -> str:
    """30, 29.97, 23.976, or ``source`` for 0."""
    if not fps:
        return MATCH_SOURCE
    return str(round(fps)) if abs(fps - round(fps)) < 1e-6 else f"{fps:.3f}".rstrip("0")


def _rate_value(text: str) -> float:
    """The exact rate a name means: 29.97 is 30000/1001, and ``source`` is 0 until the footage is known."""
    if text == MATCH_SOURCE:
        return 0.0
    value = float(text)
    for exact in STANDARD_RATES:
        if abs(value - exact) < 0.0015:
            return exact
    return value


def _export_preset(width: int, height: int, fps: float, container: str) -> ExportPreset:
    rate = _rate_name(fps)
    name = f"export_{width}x{height}_{rate}_{container}"
    canvas = Canvas(width=width, height=height, fps=float(fps))
    description = f"{width}x{height} at {'the source frame rate' if rate == MATCH_SOURCE else rate + ' fps'}, {container.upper()}"
    if container == "webm":
        return ExportPreset(name, canvas, "libvpx-vp9", "libopus", 31, "128k", _VP9, description, container="webm")
    if container == "gif":
        return ExportPreset(name, canvas, "gif", "", 0, "", (), description, container="gif")
    return ExportPreset(name, canvas, "libx264", "aac", 18, "192k", ("-profile:v", "high", *BROADCAST_COLOR), description, container=container)


def export_preset_name(base: str, *, quality: str | None = None, fps: float | str | None = None, format: str | None = None) -> str:
    """The preset for an ``edit.export`` choice on a base preset's aspect.

    The name carries the settings, so ``vedit.analyze encode --preset`` resolves
    it like any other. Nothing to change returns ``base``. ``fps="source"`` names
    a preset at rate 0, which the compiler sets to the footage's standard rate.
    """
    preset = get_preset(base)
    if quality is None and fps is None and format is None:
        return preset.name
    width, height = preset.canvas.width, preset.canvas.height
    if not width or not height:
        raise KeyError(f"preset '{base}' has no picture to export")
    container = format or preset.container
    short = EXPORT_SIZES[quality] if quality else min(width, height)
    rate = 0.0 if fps == MATCH_SOURCE else float(fps or preset.canvas.fps)
    if container == "gif":
        short, rate = min(short, GIF_SIZE), min(rate or GIF_FPS, GIF_FPS)
    # H.264 and VP9 both need even dimensions.
    scale = short / min(width, height)
    return f"export_{round(width * scale / 2) * 2}x{round(height * scale / 2) * 2}_{_rate_name(rate)}_{container}"


def verification(preset: ExportPreset) -> dict[str, bool]:
    """What a finished file of this preset can be checked for."""
    return {"audio": bool(preset.audio_codec), "color": preset.container in ("mp4", "mov")}


def get_preset(name: str) -> ExportPreset:
    key = name.strip().lower().replace("-", "_")
    built = _EXPORT_NAME.fullmatch(key)
    if built:
        return _export_preset(int(built[1]), int(built[2]), _rate_value(built[3]), built[4])
    aliases = {
        "youtube": "youtube_1080p",
        "yt": "youtube_1080p",
        "shorts": "shorts_9x16",
        "reels": "shorts_9x16",
        "tiktok": "shorts_9x16",
        "vertical": "shorts_9x16",
        "preview": "web_preview",
        "audio": "podcast_audio",
        "mp3": "podcast_audio",
    }
    key = aliases.get(key, key)
    if key not in PRESETS:
        known = ", ".join(sorted(PRESETS))
        raise KeyError(f"unknown preset '{name}'. Known: {known}")
    return PRESETS[key]


def resolve_hw(explicit: bool | None) -> bool:
    """None = VideoToolbox on Apple Silicon for delivery presets, never for web_preview."""
    if explicit is False:
        return False
    if explicit is True:
        return has_encoder("h264_videotoolbox")
    return sys.platform == "darwin" and has_encoder("h264_videotoolbox")


def is_preview_preset(preset_name: str) -> bool:
    """Every web_preview variant (16:9, 9:16, 1:1, 4:5) is a small proxy that wants CRF, not VideoToolbox's fixed q:v."""
    return preset_name.startswith("web_preview")


def video_encoder(hw: bool | None, *, preset_name: str = "") -> str:
    use = resolve_hw(hw) and not is_preview_preset(preset_name)
    if use and has_encoder("h264_videotoolbox"):
        return "h264_videotoolbox"
    return "libx264"


def encode_args(
    preset: ExportPreset,
    *,
    hw: bool | None = None,
    crf: int | None = None,
    has_video: bool = True,
    has_audio: bool = True,
) -> list[str]:
    args: list[str] = []
    if has_video and preset.video_codec:
        codec = video_encoder(hw, preset_name=preset.name) if preset.video_codec == "libx264" else preset.video_codec
        args += ["-c:v", codec]
        if codec == "h264_videotoolbox":
            # q:v 45–65 is the usable quality band on Apple Silicon.
            args += ["-q:v", "50", "-pix_fmt", "yuv420p"]
        elif codec == "libx264":
            args += ["-preset", "medium", "-crf", str(crf if crf is not None else preset.crf)]
        args += list(preset.extra)
    else:
        args += ["-vn"]
    if has_audio and preset.audio_codec:
        args += ["-c:a", preset.audio_codec, "-b:a", preset.audio_bitrate, "-ar", "48000", "-ac", "2"]
    else:
        args += ["-an"]
    return args
