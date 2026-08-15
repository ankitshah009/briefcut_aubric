#!/usr/bin/env python3
"""Write tiny local lavfi assets under examples/demo/."""

from __future__ import annotations

import argparse
from pathlib import Path

from vedit.media import MediaInfo, probe, run_ffmpeg

REPO = Path(__file__).resolve().parents[1]
DEFAULT_DEST = REPO / "examples" / "demo"
SECONDS = 3.0
TALK_SIZE = "640x360"
LOGO_SIZE = "128x128"


def write_talk(path: Path, *, seconds: float = SECONDS) -> Path:
    """640x360 color field + 440 Hz sine, ~3s."""
    path.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x1e3a5f:s={TALK_SIZE}:r=30:d={seconds}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate=48000:duration={seconds}",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            str(path),
        ]
    )
    return path


def write_bed(path: Path, *, seconds: float = SECONDS) -> Path:
    """Quieter 220 Hz sine bed as PCM WAV."""
    path.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=220:sample_rate=48000:duration={seconds}",
            "-filter:a",
            "volume=-20dB",
            "-c:a",
            "pcm_s16le",
            str(path),
        ]
    )
    return path


def write_logo(path: Path) -> Path:
    """Single-frame solid PNG."""
    path.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            f"color=c=0xe11d48:s={LOGO_SIZE}:r=1:d=1",
            "-frames:v",
            "1",
            str(path),
        ]
    )
    return path


def make_demo(dest: Path | None = None, *, seconds: float = SECONDS) -> dict[str, Path]:
    root = Path(dest) if dest is not None else DEFAULT_DEST
    root.mkdir(parents=True, exist_ok=True)
    assets = {
        "talk": write_talk(root / "talk.mp4", seconds=seconds),
        "bed": write_bed(root / "bed.wav", seconds=seconds),
        "logo": write_logo(root / "logo.png"),
    }
    _verify(assets, seconds=seconds)
    return assets


def _verify(assets: dict[str, Path], *, seconds: float) -> None:
    talk = probe(assets["talk"])
    bed = probe(assets["bed"])
    if not talk.has_video or not talk.has_audio:
        raise SystemExit(f"talk.mp4 missing streams: video={talk.has_video} audio={talk.has_audio}")
    if talk.width != 640 or talk.height != 360:
        raise SystemExit(f"talk.mp4 expected 640x360, got {talk.width}x{talk.height}")
    if not (seconds - 0.25) < talk.duration < (seconds + 0.4):
        raise SystemExit(f"talk.mp4 duration {talk.duration:.3f}s not ~{seconds}s")
    if not bed.has_audio or bed.has_video:
        raise SystemExit(f"bed.wav expected audio-only, video={bed.has_video} audio={bed.has_audio}")
    if not (seconds - 0.25) < bed.duration < (seconds + 0.4):
        raise SystemExit(f"bed.wav duration {bed.duration:.3f}s not ~{seconds}s")
    logo = assets["logo"]
    if not logo.is_file() or logo.stat().st_size < 32:
        raise SystemExit(f"logo.png missing or empty: {logo}")


def _describe(path: Path) -> str:
    try:
        info: MediaInfo = probe(path)
    except Exception:
        return f"{path}  ({path.stat().st_size} bytes)"
    bits = [f"duration={info.duration:.2f}s", f"video={info.has_video}", f"audio={info.has_audio}"]
    if info.has_video and info.width and info.height:
        bits.append(f"{info.width}x{info.height}")
    return f"{path}  " + "  ".join(bits)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate tiny local demo media for vedit examples.")
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST, help="output directory")
    parser.add_argument("--seconds", type=float, default=SECONDS, help="talk/bed duration")
    args = parser.parse_args()
    assets = make_demo(args.dest, seconds=args.seconds)
    for name, path in assets.items():
        print(f"{name}: {_describe(path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
