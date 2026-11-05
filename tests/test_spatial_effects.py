"""Curated effects: strict bounds and actual encoded motion/audio proof."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from vedit.compile import compile_brief
from vedit.effects import REQUIRED_EFFECT_FILTERS, EffectError, effect_catalogue, validate_effect
from vedit.errors import CompileError
from vedit.ir import Brief, Canvas
from vedit.media import probe, resolve_ffmpeg
from vedit.pipeline import execute, write_codegen, write_report


def _compile(source, directory, ops, *, canvas=None):
    return compile_brief(
        Brief(
            name="spatial",
            sources={"main": str(source)},
            ops=[*ops, {"op": "export", "preset": "web_preview"}],
            canvas=canvas or Canvas(width=960, height=540),
            work_dir=str(directory),
            hw=False,
        )
    )


def _render(compiler):
    write_codegen(compiler.pipeline)
    program = json.loads((compiler.work_dir / "project.json").read_text())
    assert program["steps"]
    assert "vedit.analyze" in (compiler.work_dir / "render.sh").read_text()
    reports = execute(compiler.pipeline, progress=False)
    report = write_report(compiler.pipeline, reports, finals=compiler.finals)
    assert json.loads(report.read_text())["outputs"]
    return probe(compiler.finals[0])


def _pixels(path):
    with Path(path).open("rb") as stream:
        assert stream.readline().strip() == b"P6"
        width, height = map(int, stream.readline().split())
        assert stream.readline().strip() == b"255"
        pixels = stream.read()
        assert len(pixels) == width * height * 3
    return pixels


def _mean_difference(left, right):
    assert len(left) == len(right)
    return sum(abs(a - b) for a, b in zip(left, right)) / len(left)


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    try:
        resolve_ffmpeg()
    except Exception:
        pytest.skip("ffmpeg required")
    spec = importlib.util.spec_from_file_location("spatial_e2e_helpers", Path(__file__).with_name("test_e2e.py"))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._make_clip(tmp_path_factory.mktemp("spatial-source") / "source.mp4", seconds=2)


@pytest.fixture(scope="module")
def baseline(source, tmp_path_factory):
    directory = tmp_path_factory.mktemp("spatial-baseline")
    still = directory / "baseline.ppm"
    compiler = _compile(source, directory, [{"op": "thumbnail", "at": 0.1, "path": str(still)}])
    _render(compiler)
    return _pixels(still)


def test_catalogue_is_safe_and_returns_independent_metadata():
    first = effect_catalogue()
    assert [item["id"] for item in first] == ["tilt", "float", "push"]
    first[0]["id"] = "injected"
    assert effect_catalogue()[0]["id"] == "tilt"
    assert validate_effect(None) is None
    assert validate_effect({"preset": "none"}) is None


@pytest.mark.parametrize(
    "settings",
    [
        {"preset": "scale=9999"},
        {"preset": []},
        {"preset": "float", "filter": "unsafe"},
        {"preset": "float", "intensity": -0.01},
        {"preset": "float", "intensity": 1.01},
        {"preset": "float", "intensity": True},
        {"preset": "float", "intensity": "0.5"},
        {"preset": "float", "intensity": float("nan")},
        {"preset": "float", "start": float("inf")},
        {"preset": "float", "start": -1},
        {"preset": "float", "start": 2},
        {"preset": "float", "duration": 0},
        {"preset": "float", "duration": -1},
        {"preset": "float", "duration": 10**500},
        {"preset": "float", "start": 1.5, "duration": 1},
    ],
)
def test_invalid_effects_fail_before_render(settings):
    with pytest.raises(EffectError):
        validate_effect(settings, clip_duration=2)


def test_only_omitted_duration_adapts_to_short_clips():
    assert validate_effect({"preset": "float"}, clip_duration=0.4) == {
        "preset": "float",
        "intensity": 0.55,
        "start": 0.0,
        "duration": 0.4,
    }
    assert validate_effect({"preset": "tilt", "start": 0.2}, clip_duration=0.4)["duration"] == pytest.approx(0.2)
    with pytest.raises(EffectError, match="ends after"):
        validate_effect({"preset": "float", "duration": 3}, clip_duration=0.4)


def test_compiler_rejects_invalid_duration_and_missing_filters(source, tmp_path, monkeypatch):
    with pytest.raises(CompileError, match="ends after"):
        _compile(source, tmp_path / "invalid", [{"op": "spatial_effect", "preset": "float", "duration": 3}])
    monkeypatch.setattr("vedit.compile.has_filter", lambda name: name != "perspective")
    with pytest.raises(CompileError, match="perspective"):
        _compile(source, tmp_path / "unavailable", [{"op": "spatial_effect", "preset": "float"}])


@pytest.mark.parametrize("preset", ["tilt", "float", "push"])
def test_real_effect_motion_preserves_audio_and_outside_window(source, baseline, tmp_path, preset):
    times = {"before": 0.1, "moving": 0.7, "middle": 1.0, "after": 1.8}
    stills = {name: tmp_path / f"{name}.ppm" for name in times}
    compiler = _compile(
        source,
        tmp_path / "render",
        [
            {"op": "spatial_effect", "preset": preset, "intensity": 0.8, "start": 0.5, "duration": 1},
            *[{"op": "thumbnail", "at": time, "path": str(stills[name])} for name, time in times.items()],
        ],
    )
    info = _render(compiler)
    assert info.has_video and info.has_audio
    assert info.duration == pytest.approx(2.0, abs=0.04)
    assert (info.width, info.height, info.video.color_primaries) == (960, 540, "bt709")
    effect_step = next(step for step in compiler.pipeline.steps if step.name == "spatial_effect")
    assert effect_step.argv[-3:-1] == ["-c:a", "copy"]
    intermediate = probe(effect_step.outputs[0])
    assert intermediate.duration == pytest.approx(2.0, abs=0.04) and intermediate.has_audio
    pixels = {name: _pixels(path) for name, path in stills.items()}
    # Re-encoding may differ by a code value; a displaced, tinted, or cropped
    # frame outside the requested interval would change thousands of pixels.
    assert _mean_difference(pixels["before"], baseline) < 1
    assert _mean_difference(pixels["after"], baseline) < 1
    assert _mean_difference(pixels["middle"], baseline) > 10
    assert _mean_difference(pixels["moving"], pixels["middle"]) > 3
    # Outside the transformed plane is a blurred, darkened copy of the frame
    # (the usual vertical-short backdrop), not an extrapolated source edge or
    # a flat card. The corner is visibly darker than the source corner while
    # the center keeps the source content.
    corner, source_corner = pixels["middle"][:3], baseline[:3]
    assert sum(corner) < sum(source_corner) - 24
    assert all(channel <= original for channel, original in zip(corner, source_corner))
    center = (270 * 960 + 480) * 3
    assert pixels["middle"][center + 2] > 220


def test_zero_intensity_is_an_actual_identity(source, baseline, tmp_path):
    still = tmp_path / "identity.ppm"
    compiler = _compile(
        source,
        tmp_path / "identity",
        [
            {"op": "spatial_effect", "preset": "float", "intensity": 0},
            {"op": "thumbnail", "at": 0.1, "path": str(still)},
        ],
    )
    assert not any(step.name == "spatial_effect" for step in compiler.pipeline.steps)
    info = _render(compiler)
    assert info.has_audio and info.duration == pytest.approx(2, abs=0.04)
    assert _pixels(still) == baseline


def test_portrait_silent_footage_stays_portrait_until_export(tmp_path):
    still = tmp_path / "portrait.ppm"
    still.write_bytes(b"P6\n180 320\n255\n" + bytes((40, 170, 120)) * (180 * 320))
    compiler = _compile(
        still,
        tmp_path / "portrait",
        [
            {"op": "slideshow", "images": ["main"], "duration": 1.2, "fade": 0},
            {"op": "spatial_effect", "preset": "tilt", "duration": 1},
        ],
        canvas=Canvas(width=180, height=320),
    )
    info = _render(compiler)
    step = next(step for step in compiler.pipeline.steps if step.name == "spatial_effect")
    intermediate = probe(step.outputs[0])
    assert (intermediate.width, intermediate.height) == (180, 320)
    assert not intermediate.has_audio and not info.has_audio
    assert intermediate.duration == pytest.approx(1.2, abs=0.05)
    assert (info.width, info.height) == (960, 540)


def test_effect_after_speed_consumes_video_and_audio_filters(source, tmp_path):
    compiler = _compile(
        source,
        tmp_path / "speed",
        [
            {"op": "speed", "factor": 2},
            {"op": "spatial_effect", "preset": "push"},
        ],
    )
    info = _render(compiler)
    assert info.has_audio and info.duration == pytest.approx(1.0, abs=0.08)


def _subject(cx, cy):
    return [{"time": t, "x": cx - 0.1, "y": cy - 0.1, "width": 0.2, "height": 0.2} for t in (0.0, 1.0, 2.0)]


def _float_graph(source, directory, **extra):
    compiler = _compile(
        source,
        directory,
        [
            {"op": "spatial_effect", "preset": "float", "intensity": 0.8, "start": 0.5, "duration": 1, **extra},
        ],
    )
    step = next(step for step in compiler.pipeline.steps if step.name == "spatial_effect")
    return Path(step.outputs[0]).with_suffix(".filter.txt").read_text()


def test_subject_points_are_kept_for_float_only():
    track = [
        {"time": 0, "x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4},
        {"time": 1, "x": 0.5, "y": 0.2, "width": 0.3, "height": 0.4},
    ]
    effect = validate_effect({"preset": "float", "points": track}, clip_duration=2)
    assert effect["points"] == [
        {"time": 0.0, "x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4},
        {"time": 1.0, "x": 0.5, "y": 0.2, "width": 0.3, "height": 0.4},
    ]
    assert "points" not in validate_effect({"preset": "float"}, clip_duration=2)
    with pytest.raises(EffectError, match="Floating frame"):
        validate_effect({"preset": "tilt", "points": track}, clip_duration=2)


@pytest.mark.parametrize(
    "points",
    [
        [],
        "box",
        [{"time": 0, "x": 0.1, "y": 0.1, "width": 0.2}],
        [{"time": 0, "x": 0.9, "y": 0.1, "width": 0.2, "height": 0.2}],
        [
            {"time": 1, "x": 0.1, "y": 0.1, "width": 0.2, "height": 0.2},
            {"time": 1, "x": 0.1, "y": 0.1, "width": 0.2, "height": 0.2},
        ],
        [{"time": -1, "x": 0.1, "y": 0.1, "width": 0.2, "height": 0.2}],
        [{"time": 3, "x": 0.1, "y": 0.1, "width": 0.2, "height": 0.2}],
        [{"time": 0, "x": True, "y": 0.1, "width": 0.2, "height": 0.2}],
        [{"time": 0, "x": 0.1, "y": 0.1, "width": 0.2, "height": 0.2, "filter": "x"}],
        [{"time": i / 1000, "x": 0.1, "y": 0.1, "width": 0.2, "height": 0.2} for i in range(513)],
    ],
)
def test_invalid_subject_tracks_fail_before_render(points):
    with pytest.raises(EffectError):
        validate_effect({"preset": "float", "points": points}, clip_duration=2)


def test_float_graph_follows_subject_center(source, tmp_path):
    plain = _float_graph(source, tmp_path / "plain")
    left = _float_graph(source, tmp_path / "left", points=_subject(0.2, 0.3))
    right = _float_graph(source, tmp_path / "right", points=_subject(0.8, 0.7))
    assert plain != left != right
    # The perspective clock (on), not t, drives the subject track.
    assert "0.20000000)-0.5" in left and "0.80000000)-0.5" in right
    assert "0.30000000)-0.5" in left and "0.70000000)-0.5" in right
    moving = _float_graph(
        source,
        tmp_path / "moving",
        points=[
            {"time": 0, "x": 0.1, "y": 0.1, "width": 0.2, "height": 0.2},
            {"time": 2, "x": 0.7, "y": 0.7, "width": 0.2, "height": 0.2},
        ],
    )
    assert "clip((((on-1)/" in moving
    for graph in (left, right, moving):
        assert not set(_filters(graph)) - set(REQUIRED_EFFECT_FILTERS) - {"setsar"}


def test_float_without_points_builds_current_effect(source, tmp_path):
    graph = _float_graph(source, tmp_path / "plain")
    assert ")-0.5)" not in graph and "(on-1)/" in graph
    assert "perspective=x0='W*(0.13*" in graph
    assert graph == _float_graph(source, tmp_path / "again")


def _filters(graph):
    import re

    return set(re.findall(r"(?:^|[\],;])\s*([a-z_]+)=", graph, re.M))
