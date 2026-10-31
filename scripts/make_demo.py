#!/usr/bin/env python3
"""Write local demo assets under examples/demo/.

``talk.mp4`` / ``bed.wav`` / ``logo.png`` are tiny lavfi fixtures for tests.
``workshop.mp4`` is a ~40 s, five-scene narrated clip with real silences and
hard scene changes so silence, scene, and transcript analysis has structure to
find. Narration uses the macOS ``say`` voice; without it the clip is silent and
the script says so.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path

from vedit.media import MediaInfo, probe, resolve_font, run_ffmpeg

REPO = Path(__file__).resolve().parents[1]
DEFAULT_DEST = REPO / "examples" / "demo"
SECONDS = 3.0
TALK_SIZE = "640x360"
LOGO_SIZE = "128x128"
WORKSHOP_SIZE = "1280x720"
WORKSHOP_SCENE_SECONDS = 8.0
# (label, background, narration). ``[[slnc 1000]]`` is a one-second pause for ``say``.
WORKSHOP_SCENES = (
    ("Finished bowl", "0xD8B57A",
     "This is the finished oak bowl, oiled and ready for the table. [[slnc 1000]] It started as a rough blank from a fallen tree."),
    ("Raw oak", "0x1F3A2A",
     "Here is the raw oak blank, still wet and heavy. [[slnc 1000]] The grain tells me where the bowl wants to be."),
    ("Turning", "0x8FB5D8",
     "On the lathe the shape appears in long steady cuts. [[slnc 1000]] I keep the wall thickness even from rim to base."),
    ("Sanding", "0x7A2E12",
     "Sanding goes from coarse to fine, with the lathe turning slowly. [[slnc 1000]] This is where the grain starts to glow."),
    ("Oil finish", "0xE3D3EC",
     "A coat of food safe oil brings out the color. [[slnc 1000]] Twenty four hours later, the bowl is ready to use."),
)


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


def _narration(text: str, dest: Path) -> Path | None:
    """Render one narration line to 48 kHz mono WAV with ``say``; None without it."""
    say = shutil.which("say")
    if say is None:
        return None
    aiff = dest.with_suffix(".aiff")
    try:
        subprocess.run([say, "-o", str(aiff), text], check=True, capture_output=True, timeout=120)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    run_ffmpeg(["-i", str(aiff), "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", str(dest)])
    return dest


def _escape_drawtext(text: str) -> str:
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def write_workshop(path: Path, *, scene_seconds: float = WORKSHOP_SCENE_SECONDS) -> tuple[Path, bool]:
    """Five labeled scenes, each with narration padded to a fixed length.

    Returns the path and whether narration was recorded.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    font = resolve_font()
    narrated = True
    with tempfile.TemporaryDirectory(prefix="vedit-workshop-") as tmp:
        work = Path(tmp)
        parts: list[Path] = []
        for index, (label, color, line) in enumerate(WORKSHOP_SCENES, 1):
            voice = _narration(line, work / f"voice{index}.wav")
            if voice is None:
                narrated = False
            scene = work / f"scene{index}.mp4"
            text = _escape_drawtext(f"{index}. {label}")
            draw = (f"drawtext=fontfile='{font}':text='{text}':fontcolor=white:fontsize=64:"
                    f"x=(w-text_w)/2:y=h-160:box=1:boxcolor=black@0.35:boxborderw=24")
            # A slow drift keeps the picture from being a static card.
            motion = "scale=1408:792,crop=1280:720:x='(in_w-out_w)/2+40*sin(t/4)':y='(in_h-out_h)/2'"
            args = ["-f", "lavfi", "-i", f"color=c={color}:s=1408x792:r=30:d={scene_seconds}"]
            if voice is not None:
                args += ["-i", str(voice), "-af", "adelay=600|600,apad", "-c:a", "aac", "-b:a", "96k"]
            else:
                args += ["-f", "lavfi", "-i", f"anullsrc=r=48000:cl=mono:d={scene_seconds}", "-c:a", "aac", "-b:a", "96k"]
            args += ["-vf", f"{motion},{draw},format=yuv420p", "-t", f"{scene_seconds}", "-shortest",
                     "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
                     "-r", "30", "-ar", "48000", "-ac", "1", str(scene)]
            run_ffmpeg(args)
            parts.append(scene)
        manifest = work / "concat.txt"
        manifest.write_text("".join(f"file '{part}'\n" for part in parts), encoding="utf-8")
        run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(manifest), "-c", "copy", "-movflags", "+faststart", str(path)])
    return path, narrated


def make_demo(dest: Path | None = None, *, seconds: float = SECONDS, workshop: bool = True) -> dict[str, Path]:
    root = Path(dest) if dest is not None else DEFAULT_DEST
    root.mkdir(parents=True, exist_ok=True)
    assets = {
        "talk": write_talk(root / "talk.mp4", seconds=seconds),
        "bed": write_bed(root / "bed.wav", seconds=seconds),
        "logo": write_logo(root / "logo.png"),
    }
    _verify(assets, seconds=seconds)
    if workshop:
        clip, narrated = write_workshop(root / "workshop.mp4")
        assets["workshop"] = clip
        _verify_workshop(clip, narrated=narrated)
        if not narrated:
            print("workshop.mp4: macOS `say` unavailable; the clip has no narration.")
    return assets


def _verify_workshop(path: Path, *, narrated: bool) -> None:
    info = probe(path)
    expected = WORKSHOP_SCENE_SECONDS * len(WORKSHOP_SCENES)
    if not info.has_video or not info.has_audio:
        raise SystemExit(f"workshop.mp4 missing streams: video={info.has_video} audio={info.has_audio}")
    if (info.width, info.height) != (1280, 720):
        raise SystemExit(f"workshop.mp4 expected 1280x720, got {info.width}x{info.height}")
    if not (expected - 0.5) < info.duration < (expected + 0.5):
        raise SystemExit(f"workshop.mp4 duration {info.duration:.3f}s not ~{expected}s")
    if info.size_bytes > 6_000_000:
        raise SystemExit(f"workshop.mp4 is {info.size_bytes} bytes; keep it under 6 MB")


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
    parser.add_argument("--no-workshop", action="store_true", help="skip the narrated workshop.mp4")
    args = parser.parse_args()
    assets = make_demo(args.dest, seconds=args.seconds, workshop=not args.no_workshop)
    for name, path in assets.items():
        print(f"{name}: {_describe(path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
