"""Fluent Python API: Edit('talk.mp4').cut_silence().captions().to('out.mp4')."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Self

from vedit.compile import compile_brief
from vedit.ir import Brief, Canvas, brief_dumps
from vedit.pipeline import Pipeline, execute, write_codegen, write_report


class Edit:
    """Tape-based editor. Each method records an op; nothing renders until to()/render()."""

    def __init__(self, source: str | Path | None = None, *, name: str = "edit", canvas: Canvas | None = None):
        self._name = name
        self._sources: dict[str, str] = {}
        self._ops: list[dict[str, Any]] = []
        self._canvas = canvas or Canvas()
        self._presets: list[str] = []
        self._hw = None
        self._crf = 18
        self._work_dir: Path | None = None
        if source is not None:
            path = Path(source).expanduser().resolve()
            self._sources["main"] = str(path)
            self._name = name if name != "edit" else path.stem

    def source(self, name: str, path: str | Path) -> Self:
        self._sources[name] = str(Path(path).expanduser().resolve())
        return self

    def canvas(self, width: int, height: int, fps: float = 30.0) -> Self:
        self._canvas = Canvas(width=width, height=height, fps=fps)
        return self

    def work_dir(self, path: str | Path) -> Self:
        self._work_dir = Path(path)
        return self

    def hw(self, enabled: bool | None = True) -> Self:
        self._hw = enabled
        return self

    def _op(self, name: str, **params: Any) -> Self:
        payload = {"op": name}
        payload.update({k: v for k, v in params.items() if v is not None})
        self._ops.append(payload)
        return self

    def trim(self, start: float = 0.0, end: float | None = None, duration: float | None = None) -> Self:
        return self._op("trim", start=start, end=end, duration=duration)

    def cut_silence(self, threshold_db: float = -32.0, min_silence: float = 0.45, pad: float = 0.12) -> Self:
        return self._op("cut_silence", threshold_db=threshold_db, min_silence=min_silence, pad=pad)

    def concat(self, *sources: str, transition: str | None = None, transition_duration: float = 0.5) -> Self:
        return self._op("concat", sources=list(sources), transition=transition, transition_duration=transition_duration)

    def speed(self, factor: float) -> Self:
        return self._op("speed", factor=factor)

    def reverse(self) -> Self:
        return self._op("reverse")

    def freeze(self, duration: float = 2.0) -> Self:
        return self._op("freeze", duration=duration)

    def fit(self) -> Self:
        return self._op("fit")

    def fill(self) -> Self:
        return self._op("fill")

    def crop(self, width: int, height: int, x: Any = None, y: Any = None) -> Self:
        return self._op("crop", width=width, height=height, x=x, y=y)

    def scale(self, width: int, height: int) -> Self:
        return self._op("scale", width=width, height=height)

    def pad(self, width: int, height: int, color: str | None = None) -> Self:
        return self._op("pad", width=width, height=height, color=color)

    def fps(self, fps: float) -> Self:
        return self._op("fps", fps=fps)

    def blur(self, luma: int = 2) -> Self:
        return self._op("blur", luma=luma)

    def lowpass(self, hz: int = 12000) -> Self:
        return self._op("lowpass", hz=hz)

    def extract_audio(self, path: str | None = None) -> Self:
        return self._op("extract_audio", path=path)

    def rotate(self, degrees: float = 90) -> Self:
        return self._op("rotate", degrees=degrees)

    def hflip(self) -> Self:
        return self._op("hflip")

    def vflip(self) -> Self:
        return self._op("vflip")

    def color(self, *, brightness: float | None = None, contrast: float | None = None, saturation: float | None = None, gamma: float | None = None, lut: str | None = None) -> Self:
        return self._op("color", brightness=brightness, contrast=contrast, saturation=saturation, gamma=gamma, lut=lut)

    def denoise(self, strength: float = 3.0) -> Self:
        return self._op("denoise", strength=strength)

    def sharpen(self) -> Self:
        return self._op("sharpen")

    def fade_in(self, duration: float = 0.4) -> Self:
        return self._op("fade_in", duration=duration)

    def fade_out(self, duration: float = 0.6) -> Self:
        return self._op("fade_out", duration=duration)

    def volume(self, *, db: float | None = None, factor: float | None = None) -> Self:
        return self._op("volume", db=db, factor=factor)

    def highpass(self, hz: int = 80) -> Self:
        return self._op("highpass", hz=hz)

    def loudnorm(self, i: float = -14.0, tp: float = -1.5, lra: float = 11.0) -> Self:
        return self._op("loudnorm", i=i, tp=tp, lra=lra)

    def title(self, text: str, **kwargs: Any) -> Self:
        return self._op("title", text=text, **kwargs)

    def captions(self, *, burn: bool = True, language: str = "en", style: str = "lower-third") -> Self:
        return self._op("captions", burn=burn, language=language, style=style)

    def overlay(self, source: str, **kwargs: Any) -> Self:
        return self._op("overlay", source=source, **kwargs)

    def watermark(self, source: str, **kwargs: Any) -> Self:
        return self._op("watermark", source=source, **kwargs)

    def pip(self, source: str, **kwargs: Any) -> Self:
        return self._op("pip", source=source, **kwargs)

    def music(self, source: str, *, duck: bool = True, volume_db: float = -18.0) -> Self:
        self.source("music", source)
        return self._op("music", source="music", duck=duck, volume_db=volume_db)

    def replace_audio(self, source: str) -> Self:
        return self._op("replace_audio", source=source)

    def reframe(self, aspect: str = "9:16", mode: str = "fill") -> Self:
        return self._op("reframe", aspect=aspect, mode=mode)

    def slideshow(self, images: list[str], duration: float = 3.0, fade: float = 0.4) -> Self:
        return self._op("slideshow", images=images, duration=duration, fade=fade)

    def thumbnail(self, at: float = 1.0) -> Self:
        return self._op("thumbnail", at=at)

    def chromakey(self, color: str = "0x00FF00", **kwargs: Any) -> Self:
        return self._op("chromakey", color=color, **kwargs)

    def scenes(self, threshold: float = 0.35) -> Self:
        return self._op("scenes", threshold=threshold)

    def export(self, *presets: str) -> Self:
        self._presets.extend(presets)
        return self._op("export", presets=list(presets) if presets else None)

    def brief(self) -> Brief:
        return Brief(
            name=self._name,
            sources=dict(self._sources),
            ops=list(self._ops),
            canvas=self._canvas,
            output_dir=str(self._work_dir or Path("out") / self._name),
            presets=list(self._presets),
            hw=self._hw,
            crf=self._crf,
            work_dir=str(self._work_dir) if self._work_dir else None,
        )

    def plan(self, work_dir: str | Path | None = None) -> Pipeline:
        if work_dir:
            self._work_dir = Path(work_dir)
        compiler = compile_brief(self.brief(), work_dir=self._work_dir)
        write_codegen(compiler.pipeline)
        (compiler.work_dir / "brief.yaml").write_text(brief_dumps(self.brief()), encoding="utf-8")
        return compiler.pipeline

    def render(self, *, dry_run: bool = False, work_dir: str | Path | None = None) -> list[Path]:
        if work_dir:
            self._work_dir = Path(work_dir)
        compiler = compile_brief(self.brief(), work_dir=self._work_dir)
        (compiler.work_dir / "brief.yaml").write_text(brief_dumps(self.brief()), encoding="utf-8")
        reports = execute(compiler.pipeline, dry_run=dry_run)
        write_report(compiler.pipeline, reports, finals=compiler.finals)
        return compiler.finals

    def to(self, path: str | Path, *, preset: str = "youtube_1080p", dry_run: bool = False) -> Path:
        dest = Path(path)
        self._ops.append({"op": "export", "path": str(dest), "preset": preset})
        if self._work_dir is None and dest.parent and dest.parent != Path("."):
            self._work_dir = dest.parent / f".vedit-{self._name}"
        outputs = self.render(dry_run=dry_run)
        return outputs[0] if outputs else dest
