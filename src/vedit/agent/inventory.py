"""Name and probe local media into an edit inventory."""

from __future__ import annotations

from pathlib import Path

from vedit.agent.decide import silence_threshold_db
from vedit.agent.model import Inventory, SourceShot
from vedit.analyze import Range, detect_scenes, detect_silence, measure_loudness
from vedit.errors import ProbeError
from vedit.media import MediaInfo, is_audio, is_image, probe, trace


def inventory_paths(paths: list[str], *, analyze: bool = False) -> Inventory:
    """Probe paths into named shots. Silence, scenes, and loudness run only when analyze is true."""
    if not paths:
        raise ProbeError("no media paths")
    rows = [_read(path) for path in paths]
    names = _names([kind for _, _, kind in rows])
    notes: list[str] = []
    shots: list[SourceShot] = []
    for name, (src, info, kind) in zip(names, rows):
        silence_ratio: float | None = None
        scene_count: int | None = None
        loudness_i: float | None = None
        if analyze:
            silence_ratio, scene_count, loudness_i = _analyze(name, src, info, kind, notes)
        shots.append(
            SourceShot(
                name=name,
                path=str(info.path),
                kind=kind,
                duration=info.duration,
                width=info.width,
                height=info.height,
                fps=info.fps,
                has_video=info.has_video,
                has_audio=info.has_audio,
                size_bytes=info.size_bytes,
                silence_ratio=silence_ratio,
                scene_count=scene_count,
                loudness_i=loudness_i,
            )
        )
    primary = next((shot.name for shot in shots if shot.kind == "video"), shots[0].name)
    return Inventory(shots=tuple(shots), primary=primary, notes=tuple(notes))


def _read(raw: str) -> tuple[Path, MediaInfo, str]:
    src = Path(raw).expanduser().resolve()
    if not src.is_file():
        raise ProbeError(f"file does not exist: {src}")
    trace("probe-enter")
    info = probe(src)
    trace("probe-exit")
    return src, info, _kind(src, info)


def _kind(path: Path, info: MediaInfo) -> str:
    if is_image(path):
        return "image"
    if is_audio(path) and not info.has_video:
        return "audio"
    if info.has_video:
        return "video"
    return "unknown"


def _names(kinds: list[str]) -> list[str]:
    has_video = any(kind == "video" for kind in kinds)
    names: list[str] = []
    named_main = False
    broll = 0
    music_n = 0
    image_n = 0
    unknown_n = 0
    for kind in kinds:
        if not named_main and (kind == "video" or not has_video):
            names.append("main")
            named_main = True
            continue
        if kind == "video":
            broll += 1
            names.append(f"broll{broll}")
        elif kind == "audio":
            music_n += 1
            names.append("music" if music_n == 1 else f"music{music_n}")
        elif kind == "image":
            image_n += 1
            names.append(f"image{image_n}")
        else:
            unknown_n += 1
            names.append(f"unknown{unknown_n}")
    return names


def _analyze(
    name: str,
    src: Path,
    info: MediaInfo,
    kind: str,
    notes: list[str],
) -> tuple[float | None, int | None, float | None]:
    silence_ratio: float | None = None
    scene_count: int | None = None
    loudness_i: float | None = None
    if info.has_audio:
        try:
            trace("loudness-enter")
            loudness_i = float(measure_loudness(src)["input_i"])
            trace("loudness-exit")
        except Exception as exc:
            trace("loudness-fail")
            notes.append(_failure(name, "loudness analysis", exc))
    if info.has_audio:
        try:
            trace("silence-enter")
            # The same line the silence cut uses, relative to this recording's loudness.
            silence_ratio = _silence_ratio(detect_silence(src, threshold_db=silence_threshold_db(loudness_i)), info.duration)
            trace("silence-exit")
        except Exception as exc:
            trace("silence-fail")
            notes.append(_failure(name, "silence analysis", exc))
    if info.has_video and kind != "image":
        try:
            trace("scenes-enter")
            scene_count = len(detect_scenes(src))
            trace("scenes-exit")
        except Exception as exc:
            trace("scenes-fail")
            notes.append(_failure(name, "scene detection", exc))
    return silence_ratio, scene_count, loudness_i


def _silence_ratio(spans: list[Range], duration: float) -> float:
    if duration <= 0:
        return 0.0
    total = sum(span.duration for span in spans)
    return min(1.0, max(0.0, total / duration))


def _failure(name: str, what: str, exc: Exception) -> str:
    detail = " ".join(str(exc).split()) or type(exc).__name__
    if len(detail) > 80:
        detail = detail[:77] + "..."
    return f"{name}: {what} failed: {detail}"
