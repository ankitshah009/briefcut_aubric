"""Source-time selection and tracked framing, including real encoded pixels."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from vedit.compile import compile_brief
from vedit.errors import CompileError
from vedit.ir import Brief, Canvas
from vedit.media import probe, resolve_ffmpeg
from vedit.pipeline import execute, write_codegen, write_report


def _helpers():
    spec = importlib.util.spec_from_file_location("campaign_e2e_helpers", Path(__file__).with_name("test_e2e.py"))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    try:
        resolve_ffmpeg()
    except Exception:
        pytest.skip("ffmpeg required")
    return _helpers()._make_clip(tmp_path_factory.mktemp("campaign-source") / "source.mp4", seconds=2.0)


def _compile(source, tmp_path, ops, preset="web_preview"):
    return compile_brief(Brief(
        name="campaign", sources={"main": str(source)},
        ops=[*ops, {"op": "export", "preset": preset}],
        canvas=Canvas(width=640, height=360), work_dir=str(tmp_path), hw=False,
    ))


def _render(compiler):
    write_codegen(compiler.pipeline)
    assert (compiler.work_dir / "project.json").is_file()
    assert "vedit.analyze" in (compiler.work_dir / "render.sh").read_text()
    reports = execute(compiler.pipeline, progress=False)
    report = write_report(compiler.pipeline, reports, finals=compiler.finals)
    written = json.loads(report.read_text())
    assert written["outputs"]
    return probe(compiler.finals[0])


@pytest.mark.parametrize("ranges", [
    [], [{"start": -1, "end": 1}], [{"start": 1, "end": 1}],
    [{"start": 0, "end": 3}], [{"start": 0, "end": float("nan")}],
    [{"start": 0, "end": 10 ** 500}],
    [{"start": True, "end": 1}], [{"start": "0", "end": 1}],
])
def test_select_ranges_rejects_invalid_intervals(clip, tmp_path, ranges):
    with pytest.raises(CompileError):
        _compile(clip, tmp_path, [{"op": "select_ranges", "ranges": ranges}])


def test_selected_duration_and_audio_are_preserved(clip, tmp_path):
    compiler = _compile(clip, tmp_path, [{"op": "select_ranges", "ranges": [
        {"start": 1.2, "end": 1.8}, {"start": 0.1, "end": 0.5},
    ]}])
    assert compiler.duration == pytest.approx(1.0)
    info = _render(compiler)
    assert info.duration == pytest.approx(1.0, abs=0.1)
    assert info.has_audio and info.has_video
    assert (info.width, info.height) == (960, 540)
    assert info.video.color_primaries == "bt709"


def test_ranges_follow_a_preceding_speed_change(clip, tmp_path):
    compiler = _compile(clip, tmp_path, [
        {"op": "speed", "factor": 2},
        {"op": "select_ranges", "ranges": [{"start": 0.5, "end": 0.9}]},
    ])
    assert compiler.pipeline.steps[0].name == "pre_select_ranges"
    info = _render(compiler)
    assert info.duration == pytest.approx(0.4, abs=0.1)


def _box(time=0, x=0.1, y=0.1, width=0.2, height=0.5):
    return dict(time=time, x=x, y=y, width=width, height=height)


@pytest.mark.parametrize("points", [
    [], [_box(time=-1)], [_box(time=3)], [_box(x=-0.1)],
    [_box(width=0)], [_box(x=0.9, width=0.2)], [_box(y=float("inf"))],
    [_box(time=1), _box(time=0.5)], [_box(), _box()], [_box(x="0.1")],
])
def test_tracking_rejects_invalid_matches(clip, tmp_path, points):
    with pytest.raises(CompileError):
        _compile(clip, tmp_path, [{"op": "track_reframe", "points": points}])


def _bars(tmp_path: Path, *, portrait=False) -> Path:
    """Three spatial colors distinguish tracked positions without CV mocks."""
    width, height = (360, 640) if portrait else (640, 360)
    colors = [bytes((255, 0, 0)), bytes((0, 255, 0)), bytes((0, 0, 255))]
    if portrait:
        pixels = b"".join(colors[min(2, y * 3 // height)] * width for y in range(height))
    else:
        row = b"".join(colors[min(2, x * 3 // width)] for x in range(width))
        pixels = row * height
    still = tmp_path / "bars.ppm"
    still.write_bytes(f"P6\n{width} {height}\n255\n".encode() + pixels)
    dest = tmp_path / "bars.mp4"
    compiler = compile_brief(Brief(
        name="bars", sources={"poster": str(still)},
        ops=[{"op": "slideshow", "images": ["poster"], "duration": 1.2, "fade": 0},
             {"op": "export", "path": str(dest), "preset": "shorts_9x16" if portrait else "web_preview"}],
        canvas=Canvas(width=width, height=height), work_dir=str(tmp_path / "bars-work"), hw=False,
    ))
    _render(compiler)
    return dest


def _center_rgb(path: Path):
    with path.open("rb") as source:
        assert source.readline().strip() == b"P6"
        width, height = map(int, source.readline().split())
        assert source.readline().strip() == b"255"
        source.seek(((height // 2) * width + width // 2) * 3, 1)
        return tuple(source.read(3))


@pytest.mark.parametrize("portrait", [False, True])
def test_tracking_moves_the_actual_crop_and_handles_silent_media(tmp_path, portrait):
    source = _bars(tmp_path, portrait=portrait)
    first, last = tmp_path / "first.ppm", tmp_path / "last.ppm"
    points = ([_box(0, x=0.3, y=0, width=0.3, height=0.1),
               _box(1.0, x=0.3, y=0.9, width=0.3, height=0.1)] if portrait else
              [_box(0, x=0, width=0.1), _box(1.0, x=0.9, width=0.1)])
    compiler = _compile(source, tmp_path / "track", [
        {"op": "track_reframe", "aspect": "16:9" if portrait else "9:16", "points": points},
        {"op": "thumbnail", "at": 0.05, "path": str(first)},
        {"op": "thumbnail", "at": 1.05, "path": str(last)},
    ], preset="web_preview" if portrait else "shorts_9x16")
    info = _render(compiler)
    assert info.has_video and not info.has_audio
    assert (info.width, info.height) == ((960, 540) if portrait else (1080, 1920))
    red, _, blue = _center_rgb(first)
    assert red > 180 and blue < 50
    red, _, blue = _center_rgb(last)
    assert blue > 180 and red < 50


def test_source_tracking_survives_reordered_selection(tmp_path):
    source = _bars(tmp_path)
    first, last = tmp_path / "first.ppm", tmp_path / "last.ppm"
    compiler = _compile(source, tmp_path / "reorder", [
        {"op": "track_reframe", "aspect": "9:16", "points": [
            _box(0, x=0, width=0.1), _box(1.0, x=0.9, width=0.1),
        ]},
        {"op": "select_ranges", "ranges": [{"start": 0.9, "end": 1.1}, {"start": 0, "end": 0.2}]},
        {"op": "thumbnail", "at": 0.05, "path": str(first)},
        {"op": "thumbnail", "at": 0.3, "path": str(last)},
    ], preset="shorts_9x16")
    info = _render(compiler)
    assert info.duration == pytest.approx(0.4, abs=0.1)
    red, _, blue = _center_rgb(first)
    assert blue > 180 and red < 50
    red, _, blue = _center_rgb(last)
    assert red > 180 and blue < 50
