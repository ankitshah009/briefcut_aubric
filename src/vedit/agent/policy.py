"""Platform delivery contracts: canvas, loudness, captions, and duration."""

from __future__ import annotations

from dataclasses import dataclass

from vedit.errors import BriefError
from vedit.ir import Canvas


@dataclass(frozen=True)
class PlatformContract:
    name: str
    presets: tuple[str, ...]
    width: int
    height: int
    fps: float
    target_lufs: float
    captions_default: bool
    max_duration: float | None
    reframe: str | None
    audio_only: bool = False


_CONTRACTS: dict[str, PlatformContract] = {
    "youtube": PlatformContract(
        name="youtube",
        presets=("youtube_1080p",),
        width=1920,
        height=1080,
        fps=30.0,
        target_lufs=-14.0,
        captions_default=True,
        max_duration=None,
        reframe="16:9",
    ),
    "shorts": PlatformContract(
        name="shorts",
        presets=("shorts_9x16",),
        width=1080,
        height=1920,
        fps=30.0,
        target_lufs=-14.0,
        captions_default=True,
        max_duration=60.0,
        reframe="9:16",
    ),
    "podcast": PlatformContract(
        name="podcast",
        presets=("podcast_video", "podcast_audio"),
        width=1920,
        height=1080,
        fps=30.0,
        target_lufs=-16.0,
        captions_default=True,
        max_duration=None,
        reframe="16:9",
    ),
    "linkedin": PlatformContract(
        name="linkedin",
        presets=("youtube_1080p",),
        width=1920,
        height=1080,
        fps=30.0,
        target_lufs=-14.0,
        captions_default=True,
        max_duration=90.0,
        reframe="16:9",
    ),
    "preview": PlatformContract(
        name="preview",
        presets=("web_preview",),
        width=960,
        height=540,
        fps=30.0,
        target_lufs=-16.0,
        captions_default=False,
        max_duration=None,
        reframe=None,
    ),
    "square": PlatformContract(
        name="square",
        presets=("square_1x1",),
        width=1080,
        height=1080,
        fps=30.0,
        target_lufs=-14.0,
        captions_default=True,
        max_duration=None,
        reframe="1:1",
    ),
}

_ALIASES: dict[str, str] = {
    "yt": "youtube",
    "reels": "shorts",
    "tiktok": "shorts",
    "vertical": "shorts",
    "draft": "preview",
    "1:1": "square",
}


def contract_for(platform: str) -> PlatformContract:
    key = platform.strip().lower()
    key = _ALIASES.get(key, key)
    contract = _CONTRACTS.get(key)
    if contract is None:
        known = ", ".join(sorted(_CONTRACTS))
        raise BriefError(f"unknown platform '{platform}'. Known: {known}")
    return contract


def canvas_for(contract: PlatformContract) -> Canvas:
    return Canvas(width=contract.width, height=contract.height, fps=contract.fps, sample_rate=48000)
