"""Broadcast-safe export presets. Local codecs only."""

from __future__ import annotations

from dataclasses import dataclass

from vedit.ir import Canvas
from vedit.media import has_encoder


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
        extra=("-profile:v", "high", "-pix_fmt", "yuv420p", "-movflags", "+faststart"),
        description=desc,
    )


PRESETS: dict[str, ExportPreset] = {
    "youtube_1080p": _youtube_x264(1920, 1080, "youtube_1080p", "YouTube 1080p H.264 + AAC, CRF 18, +faststart"),
    "youtube_720p": _youtube_x264(1280, 720, "youtube_720p", "YouTube 720p"),
    "youtube_4k": _youtube_x264(3840, 2160, "youtube_4k", "YouTube 4K"),
    "shorts_9x16": _youtube_x264(1080, 1920, "shorts_9x16", "YouTube Shorts / Reels / TikTok 9:16"),
    "square_1x1": _youtube_x264(1080, 1080, "square_1x1", "Square 1:1 social"),
    "twitter_landscape": _youtube_x264(1280, 720, "twitter_landscape", "X/Twitter landscape"),
    "podcast_video": _youtube_x264(1920, 1080, "podcast_video", "Talking-head / podcast video"),
    "web_preview": ExportPreset(
        name="web_preview",
        canvas=Canvas(width=960, height=540, fps=30.0),
        video_codec="libx264",
        audio_codec="aac",
        crf=28,
        audio_bitrate="128k",
        extra=("-pix_fmt", "yuv420p", "-movflags", "+faststart"),
        description="Small preview encode",
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
        extra=("-profile:v", "high", "-pix_fmt", "yuv420p", "-movflags", "+faststart"),
        description="Higher-quality mezzanine H.264",
    ),
}


def get_preset(name: str) -> ExportPreset:
    key = name.strip().lower().replace("-", "_")
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


def video_encoder(hw: bool) -> str:
    if hw and has_encoder("h264_videotoolbox"):
        return "h264_videotoolbox"
    return "libx264"


def encode_args(
    preset: ExportPreset,
    *,
    hw: bool = False,
    crf: int | None = None,
    has_video: bool = True,
    has_audio: bool = True,
) -> list[str]:
    args: list[str] = []
    if has_video and preset.video_codec:
        codec = video_encoder(hw) if preset.video_codec == "libx264" else preset.video_codec
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
