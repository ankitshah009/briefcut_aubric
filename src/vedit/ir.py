"""Project IR — OpenTimelineIO-shaped, FFmpeg-compilable, JSON-stable."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from vedit.errors import BriefError
from vedit.yamlio import dump_yaml, load_document

TrackKind = Literal["video", "audio"]


@dataclass(frozen=True)
class Canvas:
    width: int = 1920
    height: int = 1080
    fps: float = 30.0
    sample_rate: int = 48000
    pixel_format: str = "yuv420p"
    background: str = "black"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Transform:
    x: int = 0
    y: int = 0
    scale: float = 1.0
    rotate: float = 0.0
    opacity: float = 1.0
    crop: tuple[int, int, int, int] | None = None  # w,h,x,y


@dataclass
class Clip:
    id: str
    source: str
    timeline_start: float = 0.0
    duration: float | None = None
    source_in: float = 0.0
    source_out: float | None = None
    speed: float = 1.0
    volume: float = 1.0
    transform: Transform = field(default_factory=Transform)
    fade_in: float = 0.0
    fade_out: float = 0.0
    effects: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Track:
    name: str
    kind: TrackKind
    clips: list[Clip] = field(default_factory=list)
    muted: bool = False


@dataclass
class Marker:
    time: float
    name: str
    note: str = ""


@dataclass
class Brief:
    """The program the user (or an agent) writes. This is the source of truth."""

    name: str
    sources: dict[str, str]
    ops: list[dict[str, Any]]
    canvas: Canvas = field(default_factory=Canvas)
    output_dir: str = "out"
    output: str | None = None
    presets: list[str] = field(default_factory=list)
    hw: bool | None = None
    # None means each export preset's own CRF; a number overrides every export.
    crf: int | None = None
    work_dir: str | None = None

    def source_path(self, key: str, *, base: Path | None = None) -> Path:
        if key not in self.sources:
            # Allow passing a raw path as the key.
            candidate = Path(key).expanduser()
            if candidate.exists():
                return candidate.resolve()
            raise BriefError(f"unknown source '{key}'. Known: {sorted(self.sources)}")
        raw = Path(self.sources[key]).expanduser()
        if not raw.is_absolute() and base is not None:
            raw = (base / raw).resolve()
        return raw


def parse_hw(value: Any) -> bool | None:
    """true / false / auto. None means the host picks (VideoToolbox on Darwin)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"", "auto", "none", "default"}:
        return None
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise BriefError(f"hw must be true, false, or auto — got {value!r}")


def canvas_from(data: dict[str, Any] | None) -> Canvas:
    if not data:
        return Canvas()
    allowed = {f.name for f in Canvas.__dataclass_fields__.values()}
    return Canvas(**{k: v for k, v in data.items() if k in allowed})


def brief_from_mapping(data: dict[str, Any], *, origin: Path | None = None) -> Brief:
    if not isinstance(data, dict):
        raise BriefError("brief must be a mapping")
    sources = data.get("sources") or {}
    if not isinstance(sources, dict):
        raise BriefError("sources must be a mapping of name → path")
    ops = data.get("ops") or []
    if not isinstance(ops, list):
        raise BriefError("ops must be a list")
    cleaned: list[dict[str, Any]] = []
    for i, op in enumerate(ops):
        if not isinstance(op, dict) or "op" not in op:
            raise BriefError(f"ops[{i}] must be a mapping with an 'op' field")
        cleaned.append(dict(op))
    presets = data.get("presets") or []
    if isinstance(presets, str):
        presets = [presets]
    name = str(data.get("name") or (origin.stem if origin else "edit"))
    return Brief(
        name=name,
        sources={str(k): str(v) for k, v in sources.items()},
        ops=cleaned,
        canvas=canvas_from(data.get("canvas") if isinstance(data.get("canvas"), dict) else None),
        output_dir=str(data.get("output_dir") or "out"),
        output=str(data["output"]) if data.get("output") else None,
        presets=[str(p) for p in presets],
        hw=parse_hw(data["hw"]) if "hw" in data else None,
        crf=int(data["crf"]) if data.get("crf") is not None else None,
        work_dir=str(data["work_dir"]) if data.get("work_dir") else None,
    )


def load_brief(path: str | Path) -> Brief:
    src = Path(path).expanduser().resolve()
    data = load_document(src)
    if not isinstance(data, dict):
        raise BriefError(f"{src}: expected a mapping at the top level")
    brief = brief_from_mapping(data, origin=src)
    # Resolve relative source paths against the brief file.
    resolved = {}
    for key, value in brief.sources.items():
        p = Path(value).expanduser()
        if not p.is_absolute():
            p = (src.parent / p).resolve()
        resolved[key] = str(p)
    return replace(brief, sources=resolved)


def brief_to_dict(brief: Brief) -> dict[str, Any]:
    return {
        "name": brief.name,
        "canvas": brief.canvas.as_dict(),
        "sources": brief.sources,
        "ops": brief.ops,
        "output_dir": brief.output_dir,
        "output": brief.output,
        "presets": brief.presets,
        "hw": "auto" if brief.hw is None else brief.hw,
        "crf": brief.crf,
    }


def brief_dumps(brief: Brief) -> str:
    return dump_yaml(brief_to_dict(brief))
