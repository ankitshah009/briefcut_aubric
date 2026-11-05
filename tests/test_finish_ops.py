"""The finishing ops, proved on rendered files.

Most checks here decode the output and measure it: pixels, samples, or a marker
tracked through every frame. A render that merely exits 0 proves nothing about a
look, a zoom, or a denoiser.
"""

from __future__ import annotations

import array
import json
import math
import random
import shutil
import subprocess
import wave
from pathlib import Path

import pytest

from vedit import beats as beats_module
from vedit import edit_spec, looks, motion, overlays, sound
from vedit.beats import Beats, detect_beats, output_beats, snap_cuts, snap_to_music
from vedit.campaign import _ASPECTS, validate_plan
from vedit.caption_styles import Look, Stage, safe_area
from vedit.compile import compile_brief
from vedit.edit import Edit
from vedit.errors import CompileError
from vedit.ir import Brief, Canvas
from vedit.media import filter_value, probe, resolve_ffmpeg
from vedit.pipeline import execute
from vedit.presets import GIF_FPS, GIF_SIZE, encode_args, export_preset_name, get_preset, verification
from vedit.render_plan import build_ops, context
from vedit.timeline import plan_timeline

RATE = 48_000


def _have_ffmpeg() -> bool:
    try:
        resolve_ffmpeg()
        return True
    except Exception:
        return False


render = pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg required")


def _ffmpeg(*args: str) -> None:
    subprocess.run([resolve_ffmpeg(), "-hide_banner", "-v", "error", "-y", *args], check=True)


def _clip(
    path: Path, *, size: str = "320x568", seconds: float = 1.0, source: str | None = None, audio: bool = True
) -> Path:
    graph = source or f"testsrc2=s={size}:r=30:d={seconds}"
    command = ["-f", "lavfi", "-i", graph]
    if audio:
        command += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate={RATE}:duration={seconds}", "-shortest"]
    _ffmpeg(
        *command,
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "12",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        str(path),
    )
    return path


def _compile(
    tmp_path: Path,
    source: Path,
    ops: list[dict],
    *,
    canvas: Canvas | None = None,
    sources: dict | None = None,
    hw: bool | None = False,
):
    """A brief that exports at the canvas's own size, so pixel checks see the frame as it was drawn."""
    canvas = canvas or Canvas(width=320, height=568, fps=30.0)
    brief = Brief(
        name="finish",
        sources={"main": str(source), **(sources or {})},
        ops=[*ops, {"op": "export", "preset": f"export_{canvas.width}x{canvas.height}_30_mp4"}],
        canvas=canvas,
        work_dir=str(tmp_path / "work"),
        hw=hw,
    )
    return compile_brief(brief, work_dir=tmp_path / "work")


def _run(compiler) -> Path:
    execute(compiler.pipeline, progress=False)
    return compiler.finals[0]


def _frames(path: Path, width: int, height: int, *, chain: str = "format=gray", channels: int = 1) -> list[bytes]:
    raw = subprocess.run(
        [
            resolve_ffmpeg(),
            "-hide_banner",
            "-v",
            "error",
            "-i",
            str(path),
            "-vf",
            f"scale={width}:{height}:flags=area,{chain}",
            "-f",
            "rawvideo",
            "-",
        ],
        capture_output=True,
        check=True,
    ).stdout
    step = width * height * channels
    return [raw[index : index + step] for index in range(0, len(raw), step)]


def _pcm(path: Path, *, chain: str | None = None, rate: int = RATE) -> array.array:
    command = [resolve_ffmpeg(), "-hide_banner", "-v", "error", "-i", str(path)]
    if chain:
        command += ["-af", chain]
    done = subprocess.run([*command, "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"], capture_output=True, check=True)
    samples = array.array("h")
    samples.frombytes(done.stdout)
    return samples


def _db(samples) -> float:
    return 10 * math.log10(sum(value * value for value in samples) / max(1, len(samples)) / 32768**2 + 1e-14)


def _mean(frame: bytes) -> float:
    return sum(frame) / len(frame)


# Looks


def test_every_catalogued_look_has_a_recipe() -> None:
    assert set(looks.RECIPES) == {row[0] for row in edit_spec.LOOKS}


def test_an_empty_recipe_and_zero_intensity_change_nothing() -> None:
    for color in ((0.1, 0.2, 0.3), (0.9, 0.4, 0.2), (0.5, 0.5, 0.5)):
        assert looks.grade(*color, {}) == pytest.approx(color, abs=1e-6)
        assert looks.grade(*color, looks.RECIPES["cinematic"], intensity=0.0) == pytest.approx(color, abs=1e-9)


@pytest.mark.parametrize("name", [key for key in looks.RECIPES if key not in ("none", "auto")])
def test_every_look_keeps_tones_in_order_and_in_range(name: str) -> None:
    recipe = looks.RECIPES[name]
    ramp = [looks.grade(v / 32, v / 32, v / 32, recipe) for v in range(33)]
    luma = [0.2126 * r + 0.7152 * g + 0.0722 * b for r, g, b in ramp]
    assert all(later >= earlier - 1e-3 for earlier, later in zip(luma, luma[1:]))
    assert all(0 <= channel <= 1 for color in ramp for channel in color)


def _hue(r: float, g: float, b: float) -> float:
    luma = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return math.degrees(math.atan2((r - luma) / 1.5748, (b - luma) / 1.8556)) % 360


def test_the_teal_and_orange_grade_moves_foliage_and_spares_skin() -> None:
    recipe = looks.RECIPES["cinematic"]
    skin, leaf = (0.78, 0.56, 0.45), (0.32, 0.5, 0.2)
    assert abs(_hue(*looks.grade(*skin, recipe)) - _hue(*skin)) < 4
    assert abs(_hue(*looks.grade(*leaf, recipe)) - _hue(*leaf)) > 8


def test_mono_looks_have_no_color_left() -> None:
    for name in ("mono", "noir"):
        r, g, b = looks.grade(0.8, 0.3, 0.2, looks.RECIPES[name])
        assert r == pytest.approx(g) == pytest.approx(b)


def test_a_cube_has_the_shape_the_format_requires(tmp_path: Path) -> None:
    path = looks.write_cube(tmp_path / "look.cube", looks.color_recipes({"preset": "warm"}), size=5)
    lines = path.read_text().splitlines()
    assert "LUT_3D_SIZE 5" in lines and len(lines) == 4 + 5**3


def test_the_chain_declares_its_matrix_both_ways_and_works_in_16_bit(tmp_path: Path) -> None:
    chain = ",".join(looks.lut_filters(tmp_path / "look.cube"))
    assert "in_color_matrix=bt709" in chain and "out_color_matrix=bt709" in chain and "gbrp16le" in chain


@render
def test_an_identity_lut_returns_the_same_picture(tmp_path: Path) -> None:
    """Compared byte for byte: FFmpeg's psnr filter converts one of its inputs when the tags differ.

    The 8-bit path FFmpeg negotiates alone measures 44 dB on real footage, and this one 66 dB.
    """
    source = _clip(
        tmp_path / "in.mp4",
        audio=False,
        source="gradients=s=320x568:r=30:d=0.4:c0=0x884422:c1=0x224488:x0=0:y0=0:x1=320:y1=568",
    )
    cube = looks.write_cube(tmp_path / "same.cube", [({}, 1.0)])

    def picture(*chain: str) -> bytes:
        return subprocess.run(
            [
                resolve_ffmpeg(),
                "-hide_banner",
                "-v",
                "error",
                "-i",
                str(source),
                *chain,
                "-f",
                "rawvideo",
                "-pix_fmt",
                "yuv420p",
                "-",
            ],
            capture_output=True,
            check=True,
        ).stdout

    before, after = picture(), picture("-vf", ",".join(looks.lut_filters(cube)))
    assert len(before) == len(after)
    assert sum((a - b) ** 2 for a, b in zip(before, after)) / len(before) < 0.1


@render
@pytest.mark.parametrize("name", [row[0] for row in edit_spec.LOOKS])
def test_every_look_renders_as_verified_bt709(tmp_path: Path, name: str) -> None:
    source = _clip(tmp_path / "in.mp4")
    output = _run(_compile(tmp_path, source, [{"op": "look", "preset": name, "intensity": 0.8}]))
    info = probe(output)
    assert info.has_audio and abs(info.duration - 1.0) < 0.1
    assert (
        info.video.color_space == "bt709" and info.video.color_range == "tv" and info.video.color_primaries == "bt709"
    )


@render
def test_a_look_at_zero_adds_no_filter(tmp_path: Path) -> None:
    compiler = _compile(tmp_path, _clip(tmp_path / "in.mp4"), [{"op": "look", "preset": "warm", "intensity": 0.0}])
    assert not any("lut3d" in " ".join(step.argv) for step in compiler.pipeline.steps)


@render
def test_sliders_work_on_their_own_and_sit_on_top_of_a_preset(tmp_path: Path) -> None:
    source = _clip(tmp_path / "in.mp4", audio=False)
    base = _mean(_frames(source, 32, 56)[10])
    brighter = _run(_compile(tmp_path / "a", source, [{"op": "look", "preset": "none", "brightness": 0.6}]))
    assert _mean(_frames(brighter, 32, 56)[10]) > base + 12
    darker = _run(_compile(tmp_path / "b", source, [{"op": "look", "preset": "warm", "brightness": -0.6}]))
    assert _mean(_frames(darker, 32, 56)[10]) < base - 15


def test_turning_a_look_down_leaves_the_sliders_alone() -> None:
    assert looks.color_recipes({"preset": "none", "brightness": 0.5, "intensity": 0.2}) == [({"exposure": 0.7}, 1.0)]
    steps = looks.color_recipes({"preset": "warm", "brightness": 0.5, "intensity": 0.2})
    assert [strength for _recipe, strength in steps] == [0.2, 1.0]
    assert not looks.is_neutral({"preset": "warm", "brightness": 0.5, "intensity": 0.0})
    assert looks.is_neutral({"preset": "warm", "intensity": 0.0})
    assert not looks.is_neutral({"preset": "auto"})


def test_a_tiny_slider_move_does_not_dim_the_highlights() -> None:
    assert looks.grade(1.0, 1.0, 1.0, {"exposure": 0.002}) == pytest.approx((1.0, 1.0, 1.0), abs=0.002)
    assert looks.grade(1.0, 1.0, 1.0, {"exposure": -0.002})[0] > 0.99


def test_gain_rolls_highlights_off_instead_of_clipping_them() -> None:
    recipe = {"levels": [0.0, 0.6]}
    top = [looks.grade(v, v, v, recipe)[0] for v in (0.6, 0.7, 0.8, 0.9, 1.0)]
    assert all(later > earlier + 0.002 for earlier, later in zip(top, top[1:]))
    assert top[-1] == pytest.approx(1.0, abs=0.002)


def test_a_midtone_keeps_its_gain_under_the_shoulder() -> None:
    assert looks.grade(0.3, 0.3, 0.3, {"levels": [0.0, 0.6]})[0] == pytest.approx(0.5, abs=0.002)


@render
def test_a_full_range_source_is_converted_not_relabelled(tmp_path: Path) -> None:
    source = tmp_path / "full.mp4"
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "color=c=gray:s=320x568:r=30:d=0.4,format=rgb24,drawbox=x=0:y=0:w=320:h=100:color=white:t=fill,drawbox=x=0:y=468:w=320:h=100:color=black:t=fill",
        "-vf",
        "scale=out_range=full:out_color_matrix=bt709,format=yuvj420p",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "4",
        "-color_range",
        "pc",
        str(source),
    )
    assert probe(source).video.color_range == "pc"
    for index, ops in enumerate(([], [{"op": "look", "preset": "none", "contrast": 0.001}])):
        output = _run(_compile(tmp_path / str(index), source, ops))
        info = probe(output)
        luma = subprocess.run(
            [
                resolve_ffmpeg(),
                "-hide_banner",
                "-v",
                "error",
                "-i",
                str(output),
                "-frames:v",
                "1",
                "-f",
                "rawvideo",
                "-",
            ],
            capture_output=True,
            check=True,
        ).stdout
        assert info.video.color_range == "tv"
        assert abs(luma[50 * 320 + 160] - 235) < 5 and abs(luma[518 * 320 + 160] - 16) < 5


@render
def test_vignette_shades_the_corners_without_ever_going_black(tmp_path: Path) -> None:
    """The filter's aspect option turns the corners solid black, on tall frames most of all."""
    source = _clip(tmp_path / "in.mp4", source="color=c=0x808080:s=320x568:r=30:d=0.4", audio=False)
    output = _run(_compile(tmp_path, source, [{"op": "look", "preset": "none", "vignette": 1.0}]))
    frame = _frames(output, 32, 56)[5]
    corner, centre = frame[0], frame[28 * 32 + 16]
    assert centre > 110 and 25 < corner < centre - 20


@render
def test_grain_moves_from_frame_to_frame_and_stays_subtle(tmp_path: Path) -> None:
    source = _clip(tmp_path / "in.mp4", source="color=c=0x804020:s=320x568:r=30:d=0.4", audio=False)
    output = _run(_compile(tmp_path, source, [{"op": "look", "preset": "film"}]))
    first, second = _frames(output, 160, 284)[3:5]
    moved = sum(abs(a - b) for a, b in zip(first, second)) / len(first)
    assert 0.4 < moved < 8


def test_auto_leaves_good_exposure_alone_and_lifts_a_dark_frame() -> None:
    good = {"low": 0.05, "median": 0.45, "high": 0.9, "chroma": 0.2}
    assert not looks.auto_recipe(good).get("levels") and not looks.auto_recipe(good).get("gamma")
    dark = looks.auto_recipe({"low": 0.01, "median": 0.12, "high": 0.28, "chroma": 0.1})
    assert dark["levels"][1] < 0.6 and dark["gamma"] < 1


def test_a_lawn_is_not_mistaken_for_a_color_cast() -> None:
    lawn = bytes([60, 140, 50]) * 1000
    assert "gains" not in looks.auto_recipe(looks.stats_from_pixels(lawn))


def test_a_real_cast_on_neutral_surfaces_is_corrected() -> None:
    warm_wall = bytes([186, 180, 168]) * 1000
    recipe = looks.auto_recipe(looks.stats_from_pixels(warm_wall))
    assert recipe["gains"][2] > recipe["gains"][0]


@render
def test_auto_fixes_an_underexposed_clip(tmp_path: Path) -> None:
    source = _clip(tmp_path / "good.mp4", audio=False)
    gain = 2 ** (-2 / 2.2)  # two stops under, on gamma-coded values
    dark = tmp_path / "dark.mp4"
    _ffmpeg(
        "-i",
        str(source),
        "-vf",
        f"format=rgb24,lutrgb=r=val*{gain}:g=val*{gain}:b=val*{gain},format=yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "10",
        str(dark),
    )
    fixed = _run(_compile(tmp_path, dark, [{"op": "look", "preset": "auto"}]))
    original, under, repaired = (_mean(_frames(path, 32, 56)[15]) for path in (source, dark, fixed))
    assert abs(repaired - original) < abs(under - original) * 0.35


@render
def test_the_auto_step_runs_during_the_render_because_the_tape_does_not_exist_at_plan_time(tmp_path: Path) -> None:
    compiler = _compile(tmp_path, _clip(tmp_path / "in.mp4"), [{"op": "look", "preset": "auto"}])
    assert compiler.pipeline.steps[0].name == "look_measure"


@render
def test_an_unknown_look_fails_at_plan_time(tmp_path: Path) -> None:
    with pytest.raises(CompileError, match="unknown look"):
        _compile(tmp_path, _clip(tmp_path / "in.mp4"), [{"op": "look", "preset": "sepia-2012"}])


@render
def test_the_edit_api_reaches_every_finishing_op(tmp_path: Path) -> None:
    source = _clip(tmp_path / "in.mp4", seconds=2.0)
    logo = _swatch(tmp_path / "logo.png")
    bed = _bed(tmp_path / "bed.wav")
    moves = [{"start": 0, "end": 2, "from": {"scale": 1}, "to": {"scale": 1.2}}]
    finals = (
        Edit(source)
        .canvas(320, 568)
        .work_dir(tmp_path / "work")
        .hw(False)
        .stabilize()
        .zoom(moves)
        .look("warm", intensity=0.7)
        .voice()
        .text("Hello: 100%", style="title")
        .logo(logo)
        .music(bed, fade_out=0.5)
        .export("shorts_9x16")
        .render()
    )
    info = probe(finals[0])
    assert info.has_audio and abs(info.duration - 2.0) < 0.15


# Zoom


def _marker_path(video: Path, width: int, height: int) -> list[tuple[float, float]]:
    points = []
    for frame in _frames(video, width, height):
        total = x_sum = y_sum = 0
        for y in range(height):
            row = frame[y * width : (y + 1) * width]
            if max(row) < 60:
                continue
            for x, value in enumerate(row):
                if value >= 60:
                    total += value
                    x_sum += x * value
                    y_sum += y * value
        points.append((x_sum / total, y_sum / total))
    return points


def _marker_clip(path: Path, seconds: float = 2.0, *, size: str = "360x640", at: str = "x=90:y=160:w=8:h=8") -> Path:
    return _clip(
        path,
        source=f"color=c=black:s={size}:r=30:d={seconds},format=yuv420p,drawbox={at}:color=white:t=fill",
        audio=False,
    )


def test_moves_land_on_the_frames_of_the_cut() -> None:
    moves = [
        {"start": 0.0, "end": 1.0, "from": {"scale": 1}, "to": {"scale": 1.5}},
        {"start": 1.0, "end": 2.0, "from": {"scale": 2}, "to": {"scale": 2}},
    ]
    pieces = motion.plan_pieces(moves, 30)
    assert motion.state_at(pieces, 29)[0] == pytest.approx(1.5)
    assert motion.state_at(pieces, 30)[0] == pytest.approx(2.0)
    assert motion.state_at(pieces, 60) == motion.IDENTITY


def test_a_centre_is_pulled_inside_so_the_view_never_leaves_the_frame() -> None:
    pieces = motion.plan_pieces(
        [{"start": 0, "end": 1, "from": {"scale": 2, "x": 0.0, "y": 1.0}, "to": {"scale": 2, "x": 0.5, "y": 0.5}}], 30
    )
    for frame in range(30):
        scale, x, y = pieces[0].state(frame)
        half = 0.5 / scale
        assert x - half >= -1e-9 and x + half <= 1 + 1e-9 and y - half >= -1e-9 and y + half <= 1 + 1e-9


def test_interpolated_views_stay_inside_too() -> None:
    pieces = motion.plan_pieces(
        [{"start": 0, "end": 3, "from": {"scale": 3, "x": 0.0, "y": 0.0}, "to": {"scale": 1, "x": 0.5, "y": 0.5}}], 30
    )
    for frame in range(90):
        scale, x, y = pieces[0].state(frame)
        assert x - 0.5 / scale >= -1e-6 and y - 0.5 / scale >= -1e-6


@pytest.mark.parametrize(
    "bad",
    [
        [{"start": 2, "end": 1, "from": {}, "to": {}}],
        [{"start": "1;drawtext", "end": 2, "from": {}, "to": {}}],
        [{"start": 0, "end": 1, "from": {"scale": "2+2"}, "to": {}}],
        [{"start": 0, "end": 1, "from": {}, "to": {}, "ease": "bounce"}],
        [{"start": float("nan"), "end": 1, "from": {}, "to": {}}],
    ],
)
def test_a_bad_move_is_refused(bad: list) -> None:
    with pytest.raises(motion.MotionError):
        motion.plan_pieces(bad, 30)


def test_slow_pushes_in_on_every_moment_across_hard_cuts() -> None:
    view = plan_timeline({"segments": [{"start": 0, "end": 2}, {"start": 3, "end": 4}], "edit": {}})
    moves = motion.zoom_moves(view, {"mode": "slow", "strength": 1.0})
    assert [move["from"]["scale"] for move in moves] == [1.0, 1.0]
    assert moves[0]["to"]["scale"] == pytest.approx(1 + motion.SLOW_ZOOM)


def test_slow_takes_turns_across_a_transition_so_nothing_jumps() -> None:
    view = plan_timeline(
        {
            "segments": [{"start": 0, "end": 2}, {"start": 3, "end": 5}, {"start": 6, "end": 8}],
            "edit": {"transition": {"type": "fade", "seconds": 0.4}},
        }
    )
    moves = motion.zoom_moves(view, {"mode": "slow", "strength": 0.5})
    assert moves[0]["to"] == moves[1]["from"] and moves[1]["to"] == moves[2]["from"]


def test_the_state_never_jumps_inside_a_transition() -> None:
    view = plan_timeline(
        {
            "segments": [{"start": 0, "end": 2}, {"start": 3, "end": 5}, {"start": 6, "end": 8}],
            "edit": {"transition": {"type": "fade", "seconds": 0.5}},
        }
    )
    moves = motion.zoom_moves(view, {"mode": "punch", "strength": 1.0})
    pieces = motion.plan_pieces(moves, 30, blend=view["transition_seconds"], end=view["duration"])
    scales = [motion.state_at(pieces, frame)[0] for frame in range(round(view["duration"] * 30))]
    assert max(abs(later - earlier) for earlier, later in zip(scales, scales[1:])) < 0.04


def test_a_punch_lands_on_the_cut_and_settles_over_a_few_frames() -> None:
    moves = [{"start": 1.0, "end": 3.0, "from": {"scale": 1.2}, "to": {"scale": 1.2}, "settle": 0.18}]
    pieces = motion.plan_pieces(moves, 30)
    assert motion.state_at(pieces, 29) == motion.IDENTITY
    assert 1.08 < motion.state_at(pieces, 30)[0] < 1.15
    assert motion.state_at(pieces, 40)[0] == pytest.approx(1.2)


def test_an_explicit_motion_wins_over_the_mode() -> None:
    moved = {"from": {"scale": 1.4, "x": 0.3, "y": 0.4}, "to": {"scale": 1, "x": 0.5, "y": 0.5}}
    view = plan_timeline({"segments": [{"start": 0, "end": 2, "motion": moved}], "edit": {}})
    assert motion.zoom_moves(view, {"mode": "slow", "strength": 1})[0]["from"]["scale"] == 1.4


def test_a_big_timeline_stays_shallow_enough_for_ffmpeg() -> None:
    moves = [{"start": index, "end": index + 1, "from": {"scale": 1}, "to": {"scale": 1.2}} for index in range(32)]
    text = motion.zoom_filter(motion.plan_pieces(moves, 30))
    deepest = depth = 0
    for char in text:
        depth += (char == "(") - (char == ")")
        deepest = max(deepest, depth)
    assert deepest < 24


@render
def test_a_slow_push_matches_the_model_with_no_jitter(tmp_path: Path) -> None:
    """Zoompan measures about 1 px on this test, and about 0.4 px after a 4x upscale."""
    source = _marker_clip(tmp_path / "in.mp4")
    moves = [
        {
            "start": 0,
            "end": 2,
            "from": {"scale": 1, "x": 0.5, "y": 0.5},
            "to": {"scale": 1.4, "x": 0.5, "y": 0.5},
            "ease": "linear",
        }
    ]
    output = _run(
        _compile(tmp_path, source, [{"op": "zoom", "moves": moves}], canvas=Canvas(width=360, height=640, fps=30.0))
    )
    path = _marker_path(output, 360, 640)
    scale_end = motion.state_at(motion.plan_pieces(moves, 30), 59)[0]
    assert path[-1][0] == pytest.approx(180 + (94 - 180) * scale_end, abs=1.5)
    bends = [path[index + 1][0] - 2 * path[index][0] + path[index - 1][0] for index in range(1, len(path) - 1)]
    assert (sum(value * value for value in bends) / len(bends)) ** 0.5 < 0.2


@render
def test_a_zoom_changes_on_the_exact_frame_of_the_cut(tmp_path: Path) -> None:
    source = _marker_clip(tmp_path / "in.mp4", at="x=170:y=300:w=20:h=40")
    moves = [{"start": 1.0, "end": 2.0, "from": {"scale": 2}, "to": {"scale": 2}}]
    output = _run(
        _compile(tmp_path, source, [{"op": "zoom", "moves": moves}], canvas=Canvas(width=360, height=640, fps=30.0))
    )
    bright = [sum(value > 60 for value in frame) for frame in _frames(output, 90, 160)]
    assert bright[29] * 2.5 < bright[30]


@render
def test_a_zoom_never_shows_a_black_edge(tmp_path: Path) -> None:
    source = _clip(tmp_path / "in.mp4", source="color=c=white:s=360x640:r=30:d=1", audio=False)
    moves = [
        {"start": 0, "end": 1, "from": {"scale": 1.5, "x": 0.1, "y": 0.1}, "to": {"scale": 1.2, "x": 0.9, "y": 0.9}}
    ]
    output = _run(
        _compile(tmp_path, source, [{"op": "zoom", "moves": moves}], canvas=Canvas(width=360, height=640, fps=30.0))
    )
    assert all(min(frame) > 200 for frame in _frames(output, 36, 64))


def test_the_zoom_op_carries_the_transition_from_the_plan(tmp_path: Path) -> None:
    edit = edit_spec.validate_edit(
        {"zoom": {"mode": "slow"}, "transition": {"type": "fade", "seconds": 0.4}}, duration=10
    )
    plan = {"segments": [{"start": 0, "end": 2}, {"start": 3, "end": 5}], "edit": edit}
    ctx = context(
        plan, request={}, aspect="9:16", canvas=Canvas(width=1080, height=1920), work=tmp_path, has_audio=True
    )
    op = next(item for item in build_ops(ctx) if item["op"] == "zoom")
    assert op["blend"] == pytest.approx(0.4)


# Stabilize


def test_small_transforms_scale_up_to_the_full_frame() -> None:
    text = "Frame 2 (List 2 [(LM -1 3 430 110 54 0.263664 0.000000),(LM 2 0 366 110 54 0.2 0.1)])"
    assert (
        motion.scale_transforms(text, 2)
        == "Frame 2 (List 2 [(LM -2 6 860 220 108 0.263664 0.000000),(LM 4 0 732 220 108 0.2 0.1)])"
    )


def _blotches(path: Path, *, width: int = 640, height: int = 1000) -> Path:
    """A still that never repeats. FFmpeg's random() tiles, which fools any stabilizer."""
    rng = random.Random(11)
    pixels = bytearray(b"\x80" * (width * height))
    for _ in range(700):
        x, y, radius, value = (
            rng.randrange(width),
            rng.randrange(height),
            rng.randrange(6, 30),
            rng.choice((20, 60, 200, 235)),
        )
        for row in range(max(0, y - radius), min(height, y + radius)):
            for column in range(max(0, x - radius), min(width, x + radius)):
                if (row - y) ** 2 + (column - x) ** 2 <= radius**2:
                    pixels[row * width + column] = value
    grey = path.with_suffix(".pgm")
    grey.write_bytes(f"P5 {width} {height} 255\n".encode() + bytes(pixels))
    _ffmpeg("-i", str(grey), "-vf", "gblur=sigma=1.2", str(path))
    return path


def _shaky(path: Path, tmp_path: Path) -> Path:
    scene = _blotches(tmp_path / "scene.png")
    shake_x = "60+9*sin(2*PI*2.3*t)+6*sin(2*PI*5.1*t+1)"
    shake_y = "80+8*sin(2*PI*1.9*t+0.5)+7*sin(2*PI*4.3*t+2)"
    _ffmpeg(
        "-loop",
        "1",
        "-framerate",
        "30",
        "-t",
        "4",
        "-i",
        str(scene),
        "-vf",
        f"crop=480:800:x='{shake_x}':y='{shake_y}',format=yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "12",
        str(path),
    )
    return path


def _step(first: bytes, second: bytes, width: int, height: int) -> tuple[int, int]:
    """The whole-pixel shift between two small frames, by block matching."""
    best, shift = float("inf"), (0, 0)
    for dy in range(-6, 7):
        for dx in range(-6, 7):
            total = 0
            for y in range(8, height - 8, 3):
                row, other = y * width, (y + dy) * width + dx
                for x in range(8, width - 8, 3):
                    total += abs(first[row + x] - second[other + x])
            if total < best:
                best, shift = total, (dx, dy)
    return shift


def _shake(video: Path) -> float:
    frames = _frames(video, 120, 200)[30:90:2]
    steps = [_step(a, b, 120, 200) for a, b in zip(frames, frames[1:])]
    return (sum(dx * dx + dy * dy for dx, dy in steps) / len(steps)) ** 0.5


@render
def test_camera_shake_measurement_separates_a_still_frame_from_handheld_motion(tmp_path: Path) -> None:
    still = _blotches(tmp_path / "still-scene.png", width=480, height=800)
    static = tmp_path / "static.mp4"
    _ffmpeg(
        "-loop",
        "1",
        "-framerate",
        "30",
        "-t",
        "4",
        "-i",
        str(still),
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        str(static),
    )
    shaky = _shaky(tmp_path / "shaky.mp4", tmp_path)
    static_measure = motion.measure_camera_shake(static)
    shaky_measure = motion.measure_camera_shake(shaky)
    assert static_measure is not None and static_measure < motion.STABILIZE_SHAKE_WARNING
    assert shaky_measure is not None and shaky_measure >= motion.STABILIZE_SHAKE_WARNING


@render
def test_stabilizing_removes_most_of_the_shake(tmp_path: Path) -> None:
    source = _shaky(tmp_path / "shaky.mp4", tmp_path)
    compiler = _compile(tmp_path, source, [{"op": "stabilize"}], canvas=Canvas(width=480, height=800, fps=30.0))
    assert [step.name for step in compiler.pipeline.steps][:2] == ["stabilize_detect", "stabilize_scale"]
    assert _shake(_run(compiler)) < _shake(source) * 0.45


@render
def test_without_vidstab_the_simple_stabilizer_runs_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _shaky(tmp_path / "shaky.mp4", tmp_path)
    monkeypatch.setattr("vedit.compile.has_filter", lambda name: not name.startswith("vidstab"))
    compiler = _compile(tmp_path, source, [{"op": "stabilize"}], canvas=Canvas(width=480, height=800, fps=30.0))
    assert any("simpler stabilizer" in note for note in compiler.warnings)
    assert _shake(_run(compiler)) < _shake(source)


# Voice


def _noisy_speech(tmp_path: Path, *, level_db: float = 0.0) -> tuple[Path, list[tuple[float, float]]]:
    """Speech-like bursts over room noise. Known pauses make both levels measurable."""
    bursts = [(0.5, 2.0), (3.0, 4.5), (5.5, 7.5)]
    gate = "+".join(f"between(t,{start},{end})" for start, end in bursts)
    voice = f"({gate})*(0.35+0.25*sin(2*PI*4*t))*(sin(2*PI*180*t)+0.6*sin(2*PI*540*t)+0.3*sin(2*PI*1800*t)+0.15*sin(2*PI*3600*t))*0.5"
    path = tmp_path / "noisy.wav"
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        f"aevalsrc='{voice}':s={RATE}:d=9",
        "-f",
        "lavfi",
        "-i",
        f"anoisesrc=d=9:c=pink:r={RATE}:a=0.05:s=3",
        "-filter_complex",
        f"amix=inputs=2:normalize=0,volume={level_db}dB",
        "-c:a",
        "pcm_s16le",
        "-ac",
        "1",
        str(path),
    )
    return path, bursts


def _windows(samples, windows: list[tuple[float, float]], rate: int = RATE) -> array.array:
    picked = array.array("h")
    for start, end in windows:
        picked.extend(samples[round(start * rate) : round(end * rate)])
    return picked


def _speech_tape(tmp_path: Path, audio: Path, seconds: float) -> Path:
    path = tmp_path / "tape.mp4"
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        f"color=c=navy:s=320x568:r=30:d={seconds}",
        "-i",
        str(audio),
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-c:a",
        "aac",
        "-b:a",
        "256k",
        "-ar",
        str(RATE),
        "-ac",
        "2",
        "-shortest",
        str(path),
    )
    return path


def test_settings_follow_the_recording_not_absolute_levels() -> None:
    loud, quiet = (
        sound.voice_settings({"noise": -40, "speech": -20}),
        sound.voice_settings({"noise": -54, "speech": -34}),
    )
    assert quiet["noise_floor"] == pytest.approx(loud["noise_floor"] - 14, abs=0.01)
    assert quiet["gate"] == pytest.approx(loud["gate"] / 10 ** (14 / 20), rel=0.02)
    assert quiet["compress"] == pytest.approx(loud["compress"] / 10 ** (14 / 20), rel=0.02)


def test_the_denoiser_floor_stays_under_a_voice_in_a_loud_room() -> None:
    assert sound.voice_settings({"noise": -24, "speech": -20})["noise_floor"] <= -20 - 14 + 0.01


def test_a_room_with_no_voice_in_it_is_not_compressed() -> None:
    settings = sound.voice_settings({"noise": -52, "speech": -48})
    assert settings["compress"] == 1.0 and settings["makeup"] == 1.0


def test_expander_stays_out_when_voice_is_not_well_above_the_room() -> None:
    narrow = sound.voice_settings({"noise": -40, "speech": -28})
    wide = sound.voice_settings({"noise": -40, "speech": -24})
    assert narrow["gate"] == 0.001
    assert wide["gate"] > 0.001


def test_commands_only_name_filters_that_exist() -> None:
    settings = sound.voice_settings({"noise": -40, "speech": -20})
    assert "acompressor" not in sound.voice_commands(settings, denoise=True, enhance=False)
    assert "afftdn" not in sound.voice_commands(settings, denoise=False, enhance=True)


@render
def test_every_measured_setting_really_reaches_its_filter(tmp_path: Path) -> None:
    """``agate`` accepts a threshold command and quietly ignores it, so this has to be a listening test."""
    noise = tmp_path / "noise.wav"
    _ffmpeg("-f", "lavfi", "-i", f"anoisesrc=d=2:c=pink:r={RATE}:a=0.03:s=3", "-c:a", "pcm_s16le", str(noise))
    settings = dict(sound.voice_settings({"noise": -44, "speech": -14}), gate=0.002)
    (tmp_path / "open.cmd").write_text(sound.voice_commands(settings, denoise=True, enhance=False))
    settings["gate"] = 0.2
    (tmp_path / "shut.cmd").write_text(sound.voice_commands(settings, denoise=True, enhance=False))
    opened = _pcm(
        noise, chain=",".join(sound.voice_filters(denoise=True, enhance=False, commands=tmp_path / "open.cmd"))
    )
    closed = _pcm(
        noise, chain=",".join(sound.voice_filters(denoise=True, enhance=False, commands=tmp_path / "shut.cmd"))
    )
    assert _db(opened[RATE:]) > _db(closed[RATE:]) + 8


@render
@pytest.mark.parametrize("level", [0.0, -14.0])
def test_noise_falls_and_speech_holds_at_any_recording_level(tmp_path: Path, level: float) -> None:
    audio, bursts = _noisy_speech(tmp_path, level_db=level)
    tape = _speech_tape(tmp_path, audio, 9.0)
    before = _pcm(tape)
    after = _pcm(_run(_compile(tmp_path, tape, [{"op": "voice", "denoise": True, "enhance": True}])))
    pauses = [(2.3, 2.8), (4.8, 5.3), (7.8, 8.6)]
    speech = [(start + 0.2, end - 0.2) for start, end in bursts]
    assert _db(_windows(after, pauses)) < _db(_windows(before, pauses)) - 8
    assert abs(_db(_windows(after, speech)) - _db(_windows(before, speech))) < 2.5


@render
@pytest.mark.skipif(shutil.which("say") is None, reason="needs macOS say")
def test_real_synthetic_speech_is_cleaned_without_losing_the_voice(tmp_path: Path) -> None:
    speech = tmp_path / "speech.aiff"
    said = subprocess.run(
        [
            "say",
            "-o",
            str(speech),
            "Welcome back. [[slnc 1200]] The first cut is the hardest one. [[slnc 1200]] Thanks for watching.",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if "sandbox_extension_issue_file failed for /usr/bin" in said.stderr:
        pytest.skip("macOS say could not access its system resources in this sandbox")
    assert said.returncode == 0, said.stderr
    clean = tmp_path / "clean.wav"
    _ffmpeg("-i", str(speech), "-ar", str(RATE), "-ac", "1", "-c:a", "pcm_s16le", str(clean))
    seconds = probe(clean).duration
    noisy = tmp_path / "noisy.wav"
    _ffmpeg(
        "-i",
        str(clean),
        "-f",
        "lavfi",
        "-i",
        f"anoisesrc=d={seconds:.3f}:c=pink:r={RATE}:a=0.05:s=5",
        "-filter_complex",
        "[0:a]volume=-6dB[s];[s][1:a]amix=inputs=2:normalize=0:duration=first",
        "-c:a",
        "pcm_s16le",
        "-ac",
        "1",
        str(noisy),
    )
    tape = _speech_tape(tmp_path, noisy, seconds)
    output = _run(_compile(tmp_path, tape, [{"op": "voice", "denoise": True, "enhance": True}]))
    reference, before, after = _pcm(clean), _pcm(tape), _pcm(output)
    block = 960
    energy = [
        sum(value * value for value in reference[index : index + block]) / block
        for index in range(0, len(reference) - block, block)
    ]
    loud = max(energy) * 10**-4.5
    voiced = [value > loud for value in energy]
    quiet = [not any(voiced[max(0, index - 8) : index + 9]) for index in range(len(voiced))]

    def level(samples, mask) -> float:
        return _db(_windows(samples, [(index * 0.02, index * 0.02 + 0.02) for index, keep in enumerate(mask) if keep]))

    assert sum(quiet) > 40
    assert level(after, quiet) < level(before, quiet) - 8
    assert abs(level(after, voiced) - level(before, voiced)) < 1.5


@render
def test_voice_keeps_exact_length_and_sync(tmp_path: Path) -> None:
    """afftdn holds 25 ms of audio without saying so. Left alone, sound trails the lips by that."""
    click = tmp_path / "click.wav"
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        f"aevalsrc='0.9*between(n,48000,48010)*sin(2*PI*2000*t)+0.02*sin(2*PI*700*t)':s={RATE}:d=3",
        "-c:a",
        "pcm_s16le",
        str(click),
    )
    commands = tmp_path / "voice.cmd"
    commands.write_text(
        sound.voice_commands(sound.voice_settings({"noise": -40, "speech": -20}), denoise=True, enhance=True)
    )
    chain = "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo," + ",".join(
        sound.voice_filters(denoise=True, enhance=True, commands=commands)
    )
    samples = _pcm(click, chain=chain)
    peak = max(range(47000, 50000), key=lambda index: abs(samples[index]))
    assert len(samples) == 3 * RATE
    assert abs(peak - 48005) <= 2


@render
def test_voice_on_a_silent_clip_only_warns(tmp_path: Path) -> None:
    compiler = _compile(tmp_path, _clip(tmp_path / "in.mp4", audio=False), [{"op": "voice"}])
    assert any("no sound" in note for note in compiler.warnings) and not any(
        step.name == "voice_measure" for step in compiler.pipeline.steps
    )


# Music


def _bed(path: Path, seconds: float = 2.0, *, frequency: int = 110, level: float = 0.3) -> Path:
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        f"aevalsrc='{level}*sin(2*PI*{frequency}*t)':s={RATE}:d={seconds}",
        "-c:a",
        "pcm_s16le",
        str(path),
    )
    return path


def _burst_tape(tmp_path: Path, *, level: float = 0.25) -> Path:
    """Two tone bursts well above the bed's band, with silence around them."""
    voice = f"{level}*(between(t,2,4)+between(t,6,8))*sin(2*PI*1000*t)"
    wav = tmp_path / "voice.wav"
    _ffmpeg("-f", "lavfi", "-i", f"aevalsrc='{voice}':s={RATE}:d=10", "-c:a", "pcm_s16le", str(wav))
    return _speech_tape(tmp_path, wav, 10.0)


@render
def test_a_short_bed_loops_to_the_length_of_the_tape(tmp_path: Path) -> None:
    tape = _clip(tmp_path / "in.mp4", seconds=5.0)
    output = _run(
        _compile(
            tmp_path,
            tape,
            [{"op": "music", "source": "bed", "duck": False, "fade_out": 0}],
            sources={"bed": str(_bed(tmp_path / "bed.wav", 1.0))},
        )
    )
    assert abs(probe(output).duration - 5.0) < 0.1
    samples = _pcm(output, chain="lowpass=f=200,lowpass=f=200")
    assert _db(samples[4 * RATE : int(4.8 * RATE)]) > -60


@render
def test_a_compressed_bed_loops_without_a_hole_at_the_seam(tmp_path: Path) -> None:
    """Looping the input restarts the AAC decoder, which leaves 10 ms of digital silence at every repeat."""
    bed = tmp_path / "bed.m4a"
    _ffmpeg(
        "-f", "lavfi", "-i", f"aevalsrc='0.3*sin(2*PI*220*t)':s={RATE}:d=1", "-c:a", "aac", "-b:a", "192k", str(bed)
    )
    tape = _clip(tmp_path / "in.mp4", seconds=3.5, audio=False)
    output = _run(
        _compile(
            tmp_path, tape, [{"op": "music", "source": "bed", "duck": False, "fade_out": 0}], sources={"bed": str(bed)}
        )
    )
    samples = _pcm(output)
    block = RATE // 100
    levels = [_db(samples[index : index + block]) for index in range(RATE // 4, 3 * RATE, block)]
    assert min(levels) > max(levels) - 12


@render
def test_music_fades_out_at_the_end(tmp_path: Path) -> None:
    tape = _clip(tmp_path / "in.mp4", seconds=4.0)
    output = _run(
        _compile(
            tmp_path,
            tape,
            [{"op": "music", "source": "bed", "duck": False, "fade_out": 1.5, "mute_source": True}],
            sources={"bed": str(_bed(tmp_path / "bed.wav", 4.0))},
        )
    )
    samples = _pcm(output)
    assert _db(samples[int(3.85 * RATE) : int(3.95 * RATE)]) < _db(samples[RATE : 2 * RATE]) - 15


@render
def test_music_can_start_partway_into_the_track(tmp_path: Path) -> None:
    track = tmp_path / "track.wav"
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        f"aevalsrc='0.3*sin(2*PI*if(lt(t,2),220,880)*t)':s={RATE}:d=4",
        "-c:a",
        "pcm_s16le",
        str(track),
    )
    tape = _clip(tmp_path / "in.mp4", seconds=1.0, audio=False)
    output = _run(
        _compile(
            tmp_path,
            tape,
            [{"op": "music", "source": "bed", "start": 2.5, "duck": False, "fade_out": 0}],
            sources={"bed": str(track)},
        )
    )
    samples = _pcm(output, chain="highpass=f=500,highpass=f=500")
    assert _db(samples[int(0.2 * RATE) : int(0.8 * RATE)]) > -40


@render
def test_a_clip_with_no_sound_gets_the_bed_alone_and_keeps_its_length(tmp_path: Path) -> None:
    tape = _clip(tmp_path / "in.mp4", seconds=3.0, audio=False)
    info = probe(
        _run(
            _compile(
                tmp_path, tape, [{"op": "music", "source": "bed"}], sources={"bed": str(_bed(tmp_path / "bed.wav"))}
            )
        )
    )
    assert info.has_audio and info.has_video and abs(info.duration - 3.0) < 0.1


@render
def test_mute_source_drops_the_original_sound(tmp_path: Path) -> None:
    tape = _clip(tmp_path / "in.mp4", seconds=2.0)
    bed = _bed(tmp_path / "bed.wav", 2.0, frequency=110)
    output = _run(
        _compile(
            tmp_path,
            tape,
            [{"op": "music", "source": "bed", "mute_source": True, "duck": False, "fade_out": 0}],
            sources={"bed": str(bed)},
        )
    )
    assert (
        _db(_pcm(output, chain="bandpass=f=440:w=60,bandpass=f=440:w=60"))
        < _db(_pcm(output, chain="bandpass=f=110:w=40")) - 20
    )


def _duck_depth(tmp_path: Path, tape: Path, bed: Path) -> float:
    output = _run(
        _compile(
            tmp_path,
            tape,
            [{"op": "music", "source": "bed", "volume_db": -12, "duck": True, "fade_out": 0}],
            sources={"bed": str(bed)},
        )
    )
    low = _pcm(output, chain="lowpass=f=200,lowpass=f=200,lowpass=f=200")
    return _db(low[int(0.5 * RATE) : int(1.8 * RATE)]) - _db(low[int(2.6 * RATE) : int(3.8 * RATE)])


@render
def test_music_ducks_under_speech_and_comes_back(tmp_path: Path) -> None:
    assert 6 < _duck_depth(tmp_path, _burst_tape(tmp_path), _bed(tmp_path / "bed.wav", 3.0)) < 20


@render
def test_quiet_speech_still_ducks_the_music(tmp_path: Path) -> None:
    """A fixed threshold would never fire at this level."""
    assert _duck_depth(tmp_path, _burst_tape(tmp_path, level=0.015), _bed(tmp_path / "bed.wav", 3.0)) > 6


@render
def test_a_quiet_track_is_levelled_so_volume_db_means_the_same_thing(tmp_path: Path) -> None:
    tape = _clip(tmp_path / "in.mp4", seconds=2.0, audio=False)
    levels = []
    for index, level in enumerate((0.4, 0.02)):
        bed = _bed(tmp_path / f"bed{index}.wav", level=level)
        output = _run(
            _compile(
                tmp_path / str(index),
                tape,
                [{"op": "music", "source": "bed", "duck": False, "fade_out": 0}],
                sources={"bed": str(bed)},
            )
        )
        levels.append(_db(_pcm(output)))
    assert abs(levels[0] - levels[1]) < 1.5


@render
def test_the_mix_never_passes_full_scale(tmp_path: Path) -> None:
    tape = _clip(tmp_path / "in.mp4", seconds=2.0)
    bed = _bed(tmp_path / "bed.wav", 2.0, level=0.95)
    output = _run(
        _compile(
            tmp_path,
            tape,
            [
                {"op": "volume", "db": 12},
                {"op": "music", "source": "bed", "volume_db": 0, "duck": False, "fade_out": 0},
            ],
            sources={"bed": str(bed)},
        )
    )
    assert max(abs(value) for value in _pcm(output)) < 32767


@render
def test_music_after_pause_trimming_ends_with_the_speech(tmp_path: Path) -> None:
    """The trimmed length only exists during the render, so the bed has to stop on its own."""
    audio, _bursts = _noisy_speech(tmp_path)
    tape = _speech_tape(tmp_path, audio, 9.0)
    compiler = _compile(
        tmp_path,
        tape,
        [{"op": "cut_silence", "threshold_db": -20, "min_silence": 0.4}, {"op": "music", "source": "bed"}],
        sources={"bed": str(_bed(tmp_path / "bed.wav"))},
    )
    assert any("could not fade" in note for note in compiler.warnings)
    info = probe(_run(compiler))
    assert info.has_audio and 3 < info.duration < 8.5


@render
def test_a_file_with_no_sound_cannot_be_the_music(tmp_path: Path) -> None:
    tape = _clip(tmp_path / "in.mp4")
    picture = _clip(tmp_path / "picture.mp4", audio=False)
    with pytest.raises(CompileError, match="no sound"):
        _compile(tmp_path, tape, [{"op": "music", "source": "bed"}], sources={"bed": str(picture)})


# Beats


@render
@pytest.mark.parametrize("bed", sound.bundled_music(), ids=lambda bed: bed["id"])
def test_a_bundled_bed_is_a_usable_loop(bed: dict) -> None:
    info = probe(bed["path"])
    assert info.has_audio and not info.has_video and 15 < info.duration < 30
    assert Path(bed["path"]).stat().st_size < 600_000
    assert info.duration == pytest.approx(8 * 4 * 60 / bed["bpm"], abs=0.03)  # eight whole bars
    found = detect_beats(bed["path"])
    if found:
        assert found.tempo == pytest.approx(bed["bpm"], rel=0.02)


def test_all_three_beds_ship_with_the_package() -> None:
    assert len(sound.bundled_music()) == 3
    assert "assets/music/*.m4a" in (Path(__file__).parent.parent / "pyproject.toml").read_text()


def _click_track(path: Path, bpm: float, seconds: float, *, first: float = 0.25, rate: int = 44_100) -> list[float]:
    count = int(seconds * rate)
    samples = array.array("h", [0]) * count
    truth, moment = [], first
    while moment < seconds - 0.05:
        start = round(moment * rate)
        truth.append(start / rate)
        for index in range(int(0.012 * rate)):
            if start + index < count:
                samples[start + index] = int(
                    28000 * math.exp(-index / (0.003 * rate)) * math.sin(2 * math.pi * 2200 * index / rate)
                )
        moment += 60 / bpm
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(samples.tobytes())
    return truth


@render
@pytest.mark.parametrize(("bpm", "first"), [(120, 0.25), (90, 0.13), (140, 0.4), (73.5, 0.31)])
def test_a_click_track_is_found_within_a_few_milliseconds(tmp_path: Path, bpm: float, first: float) -> None:
    path = tmp_path / "click.wav"
    truth = _click_track(path, bpm, 12, first=first)
    found = detect_beats(path)
    assert isinstance(found, list) and found.tempo == pytest.approx(bpm, rel=0.01)
    assert len(found) == len(truth)
    assert max(min(abs(beat - time) for beat in found) for time in truth) < 0.005


@render
def test_compressed_tracks_land_on_the_same_beats(tmp_path: Path) -> None:
    path = tmp_path / "click.wav"
    truth = _click_track(path, 120, 8)
    compressed = tmp_path / "click.m4a"
    _ffmpeg("-i", str(path), "-c:a", "aac", "-b:a", "192k", str(compressed))
    found = detect_beats(compressed)
    assert max(min(abs(beat - time) for beat in found) for time in truth) < 0.005


@render
def test_noise_and_silence_have_no_beat(tmp_path: Path) -> None:
    noise = tmp_path / "noise.wav"
    _ffmpeg("-f", "lavfi", "-i", "anoisesrc=d=8:c=pink:r=44100:a=0.2:s=4", str(noise))
    silence = tmp_path / "silence.wav"
    _ffmpeg("-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "6", str(silence))
    for path in (noise, silence):
        found = detect_beats(path)
        assert found == [] and found.tempo == 0


def test_a_tempo_near_the_prior_beats_its_octaves() -> None:
    samples = array.array("h", [0]) * (beats_module.RATE * 20)
    for beat in range(38):
        start = int((0.2 + beat * 0.5) * beats_module.RATE)
        for index in range(200):
            samples[start + index] = 20000 if index % 2 else -20000
    assert beats_module.beats_from_samples(samples).tempo == pytest.approx(120, rel=0.01)


def test_beats_follow_a_looped_bed_onto_the_output_clock() -> None:
    assert output_beats([1.0, 2.0, 3.0], start=1.0, length=4.0, duration=8.0) == [0.0, 1.0, 2.0, 4.0, 5.0, 6.0, 8.0]


def _plan(**extra: object) -> dict:
    return {
        "segments": [{"start": 0.0, "end": 2.1}, {"start": 3.0, "end": 5.0}, {"start": 6.0, "end": 8.0}],
        "edit": {},
        "analysis": {"speech": []},
        **extra,
    }


def test_a_silent_cut_moves_to_the_nearest_beat_and_the_story_follows() -> None:
    plan, moved = snap_cuts(_plan(), [2.0, 4.0, 6.0])
    assert plan["segments"][0]["end"] == 2.0
    assert plan["segments"][1]["end"] == 5.0  # the next cut lands on 4.0 once the first has moved
    assert moved == [{"segment": 1, "from": 2.1, "to": 2.0, "beat": 2.0}]


def test_the_original_plan_is_left_alone() -> None:
    plan = _plan()
    snap_cuts(plan, [2.0])
    assert plan["segments"][0]["end"] == 2.1


def test_a_cut_with_speech_at_it_stays_where_the_story_put_it() -> None:
    plan, moved = snap_cuts(_plan(analysis={"speech": [{"start": 1.5, "end": 2.2}]}), [2.0])
    assert plan["segments"][0]["end"] == 2.1 and not moved


def test_speech_at_the_start_of_the_next_moment_also_holds_the_cut() -> None:
    plan, moved = snap_cuts(_plan(analysis={"speech": [{"start": 3.0, "end": 4.0}]}), [2.0])
    assert plan["segments"][0]["end"] == 2.1 and not moved


def test_a_far_beat_is_ignored() -> None:
    plan, moved = snap_cuts(_plan(), [2.9], tolerance=0.3)
    assert plan["segments"][0]["end"] == 2.1 and not moved


def test_growing_a_moment_never_shows_footage_twice() -> None:
    plan = {"segments": [{"start": 0.0, "end": 2.0}, {"start": 2.05, "end": 4.0}], "edit": {}}
    assert snap_cuts(plan, [2.2])[1] == []


def test_a_nudge_stays_inside_the_source() -> None:
    plan = {"segments": [{"start": 1.0, "end": 3.0}, {"start": 0.0, "end": 1.0}], "edit": {}}
    snapped, moved = snap_cuts(plan, [2.2], source_duration=3.0)
    assert not moved and snapped["segments"][0]["end"] == 3.0


def test_speed_scales_a_nudge_back_to_source_time() -> None:
    plan = {"segments": [{"start": 0.0, "end": 4.2, "speed": 2.0}, {"start": 5.0, "end": 6.0}], "edit": {}}
    snapped, _moved = snap_cuts(plan, [2.0])
    assert snapped["segments"][0]["end"] == pytest.approx(4.0)


def test_a_transition_cut_lands_on_the_beat_at_its_midpoint() -> None:
    plan = {
        "segments": [{"start": 0.0, "end": 2.1}, {"start": 3.0, "end": 5.0}],
        "edit": {"transition": {"type": "fade", "seconds": 0.4}},
    }
    snapped, _moved = snap_cuts(plan, [1.8])
    view = plan_timeline(snapped)
    assert (view["spans"][0].output_end + view["spans"][1].output_start) / 2 == pytest.approx(1.8, abs=0.01)


def test_validating_twice_is_stable() -> None:
    edit = {"music": {"asset": "abcd1234", "start": 0.0, "beat_sync": True}}
    assets = {"abcd1234": {"beats": [2.0, 4.0, 6.0], "duration": 30.0}}
    once = snap_to_music(_plan()["segments"], edit, None, assets, source_duration=10.0)
    assert snap_to_music(once, edit, None, assets, source_duration=10.0) == once and once[0]["end"] == 2.0


def test_a_validated_plan_carries_its_cuts_on_the_beat_into_the_timeline() -> None:
    assets = {"abcd1234": {"id": "abcd1234", "kind": "audio", "beats": [2.0, 4.0, 6.0], "duration": 30.0}}
    plan = validate_plan(
        {
            "title": "Beat",
            "segments": [{"start": 0.0, "end": 2.1, "label": "a"}, {"start": 3.0, "end": 5.0, "label": "b"}],
            "edit": {"music": {"asset": "abcd1234", "beat_sync": True}},
        },
        20.0,
        assets=assets,
    )
    assert plan["segments"][0]["end"] == 2.0
    assert plan["duration"] == pytest.approx(4.0)


def test_no_sync_without_the_setting_or_the_beats() -> None:
    segments = _plan()["segments"]
    assert (
        snap_to_music(
            segments,
            {"music": {"asset": "abcd1234", "beat_sync": False}},
            None,
            {"abcd1234": {"beats": [2.0]}},
            source_duration=9.0,
        )
        == segments
    )
    assert (
        snap_to_music(
            segments, {"music": {"asset": "abcd1234", "beat_sync": True}}, None, {"abcd1234": {}}, source_duration=9.0
        )
        == segments
    )


def test_beats_are_a_plain_list_with_a_tempo() -> None:
    assert json.dumps(Beats([1.0, 2.0], tempo=60.0)) == "[1.0, 2.0]"


# Text


HOSTILE = [
    "She said \"it's fine\" and 'left'",
    "Time: 10:30, Price: $5; [draft]",
    "100% done %{pts} %d %%",
    r"C:\new\table\n and \\ back",
    r"{\b1}not bold{\i1} {literal}",
    "Day 1 🔥 ship it 🚀",
    "你好世界 こんにちは 안녕하세요",
    "مرحبا بالعالم",
    "नमस्ते दुनिया",
    "a=b:c=d,e;[f]'g'\\h%i",
]


@render
@pytest.mark.parametrize("text", HOSTILE)
def test_any_text_draws_without_breaking_the_render(tmp_path: Path, text: str) -> None:
    source = _clip(tmp_path / "in.mp4", source="color=c=black:s=320x568:r=30:d=0.6", audio=False)
    output = _run(
        _compile(
            tmp_path, source, [{"op": "text", "text": text, "style": "title", "start": 0, "duration": 0.6, "fade": 0}]
        )
    )
    assert max(_frames(output, 160, 284)[5]) > 100


@render
@pytest.mark.parametrize("text", HOSTILE)
def test_the_title_op_takes_the_same_strings(tmp_path: Path, text: str) -> None:
    """A plain "100% done" used to fail the whole render with exit 234."""
    source = _clip(tmp_path / "in.mp4", source="color=c=black:s=320x568:r=30:d=0.4", audio=False)
    assert probe(_run(_compile(tmp_path, source, [{"op": "title", "text": text, "size": 20}]))).has_video


@render
@pytest.mark.parametrize("text", HOSTILE)
def test_the_drawtext_fallback_takes_the_same_strings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    source = _clip(tmp_path / "in.mp4", source="color=c=black:s=320x568:r=30:d=0.4", audio=False)
    monkeypatch.setattr("vedit.compile.text_engine", lambda: "drawtext")
    compiler = _compile(tmp_path, source, [{"op": "text", "text": text, "style": "label", "fade": 0.1}])
    assert any("simpler renderer" in note for note in compiler.warnings)
    assert probe(_run(compiler)).has_video


def test_control_sequences_in_text_stay_literal() -> None:
    event = overlays.text_event(
        {"text": r"{\pos(0,0)}hi\Nthere", "style": "title"}, width=1080, height=1920, end_limit=None, face=None
    )
    assert event.count("\\pos(") == 1 and "\\{" in event and "\\⁠N" in event


def test_emoji_warn_instead_of_burning_as_empty_boxes() -> None:
    assert overlays.text_warnings([{"text": "ship it 🚀"}]) and not overlays.text_warnings([{"text": "ship it"}])
    assert "🚀" not in overlays.text_event({"text": "ship it 🚀"}, width=1080, height=1920, end_limit=None, face=None)


def test_text_that_is_only_emoji_draws_nothing() -> None:
    assert overlays.text_event({"text": "🚀🔥"}, width=1080, height=1920, end_limit=None, face=None) is None


def test_long_text_wraps_inside_the_safe_width() -> None:
    text = "A title long enough that it will never fit on a single line of a phone screen at this size"
    lines, size = overlays.layout_text(text, overlays.TEXT_STYLES["title"], width=1080, height=1920)
    stage = Stage(Look(size=0.09), size, 1080 - 2 * safe_area(1080, 1920)[0])
    assert len(lines) > 1 and all(stage.measure(line) <= stage.room + 1 for line in lines)


def test_unspaced_scripts_wrap_too() -> None:
    """libass breaks only at spaces, so a CJK line would otherwise run off both edges."""
    lines, _size = overlays.layout_text(
        "今天我们一起去海边看日出然后回家吃早饭这是一段很长的中文句子用来测试换行",
        overlays.TEXT_STYLES["title"],
        width=1080,
        height=1920,
    )
    assert len(lines) > 1 and "".join(lines).startswith("今天我们")


def test_one_endless_word_is_cut_rather_than_clipped() -> None:
    lines, _size = overlays.layout_text(
        "https://example.com/" + "a" * 120, overlays.TEXT_STYLES["label"], width=720, height=1280
    )
    assert len(lines) > 1 and "".join(lines).endswith("a")


def test_lines_are_balanced_not_filled() -> None:
    lines, _size = overlays.layout_text(
        "one two three four five six seven eight nine ten", overlays.TEXT_STYLES["title"], width=1080, height=1920
    )
    assert len(lines[-1].split()) > 1


def test_callout_text_stays_readable_on_its_box() -> None:
    assert overlays._contrast("#FFD60A") == "#101014" and overlays._contrast("#1E3A5F") == "#FFFFFF"


def _bright_bounds(frame: bytes, width: int, height: int) -> tuple[int, int, int, int]:
    xs, ys = [], []
    for y in range(height):
        for x in range(width):
            if frame[y * width + x] > 100:
                xs.append(x)
                ys.append(y)
    return min(xs), max(xs), min(ys), max(ys)


@render
@pytest.mark.parametrize(
    ("size", "width", "height"), [("540x960", 540, 960), ("960x540", 960, 540), ("540x540", 540, 540)]
)
def test_text_stays_inside_the_safe_area_of_every_canvas(tmp_path: Path, size: str, width: int, height: int) -> None:
    source = _clip(tmp_path / "in.mp4", source=f"color=c=black:s={size}:r=30:d=0.4", audio=False)
    long = "Everything we made this year, in one short and very honest video for you"
    ops = [
        {"op": "text", "text": long, "style": "title", "position": "top", "fade": 0},
        {"op": "text", "text": long, "style": "label", "position": "bottom", "fade": 0},
    ]
    output = _run(_compile(tmp_path, source, ops, canvas=Canvas(width=width, height=height, fps=30.0)))
    frame = _frames(output, width // 2, height // 2)[5]
    left, right, top, bottom = _bright_bounds(frame, width // 2, height // 2)
    side, top_margin, bottom_margin = (value // 2 for value in safe_area(width, height))
    assert left >= side - 4 and right <= width // 2 - side + 4
    assert top >= top_margin - 4 and bottom <= height // 2 - bottom_margin + 4


@render
def test_text_keeps_its_time_and_fades(tmp_path: Path) -> None:
    source = _clip(tmp_path / "in.mp4", source="color=c=black:s=320x568:r=30:d=2", audio=False)
    output = _run(
        _compile(
            tmp_path,
            source,
            [{"op": "text", "text": "HELLO", "style": "title", "start": 0.5, "duration": 1.0, "fade": 0.3}],
        )
    )
    peak = [max(frame) for frame in _frames(output, 80, 142)]
    assert peak[10] < 40 and peak[50] < 40  # before and after
    assert peak[30] > 150  # held
    assert 40 < peak[19] < peak[30]  # on its way up


@render
def test_several_text_items_share_one_filter(tmp_path: Path) -> None:
    source = _clip(tmp_path / "in.mp4", source="color=c=black:s=320x568:r=30:d=0.4", audio=False)
    compiler = _compile(
        tmp_path, source, [{"op": "text", "text": f"item {index}", "start": 0, "duration": 0.4} for index in range(6)]
    )
    assert " ".join(compiler.pipeline.steps[0].argv).count("ass=") == 1
    assert (tmp_path / "work" / "texts.ass").read_text(encoding="utf-8").count("Dialogue:") == 6


@render
def test_text_colors_arrive_as_asked(tmp_path: Path) -> None:
    source = _clip(tmp_path / "in.mp4", source="color=c=black:s=320x568:r=30:d=0.4", audio=False)
    output = _run(
        _compile(
            tmp_path,
            source,
            [
                {
                    "op": "text",
                    "text": "████",
                    "style": "title",
                    "color": "#FF3B30",
                    "start": 0,
                    "duration": 0.4,
                    "fade": 0,
                }
            ],
        )
    )
    frame = _frames(output, 160, 284, chain="scale=in_color_matrix=bt709,format=rgb24", channels=3)[5]
    best = max(range(0, len(frame), 3), key=lambda index: frame[index] - frame[index + 1])
    assert abs(frame[best] - 255) < 16 and abs(frame[best + 1] - 59) < 20 and abs(frame[best + 2] - 48) < 20


def test_filter_values_survive_both_parsing_passes() -> None:
    assert filter_value("a:b") == "a\\\\:b"
    assert filter_value("it's") == "it\\\\\\'s"
    assert filter_value("[x],y;z") == "\\[x\\]\\,y\\;z"


@render
def test_a_hostile_path_reaches_the_filter_intact(tmp_path: Path) -> None:
    folder = tmp_path / "it's a [hard]; path, with: colons"
    folder.mkdir()
    cube = looks.write_cube(folder / "lo'ok 100%.cube", [({}, 1.0)], size=5)
    subprocess.run(
        [
            resolve_ffmpeg(),
            "-hide_banner",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=16x16:d=0.1",
            "-vf",
            f"lut3d=file={filter_value(cube)}",
            "-f",
            "null",
            "-",
        ],
        check=True,
    )


# Logo and overlays


def _swatch(path: Path, color: str = "0xE01040", size: str = "64x64") -> Path:
    _ffmpeg("-f", "lavfi", "-i", f"color=c={color}:s={size}:d=0.04", "-frames:v", "1", str(path))
    return path


def _region_mean(frame: bytes, width: int, box: tuple[int, int, int, int]) -> float:
    left, top, right, bottom = box
    values = [frame[y * width + x] for y in range(top, bottom) for x in range(left, right)]
    return sum(values) / len(values)


@render
@pytest.mark.parametrize(
    ("position", "box"),
    [
        ("top-right", (120, 20, 160, 60)),
        ("top-left", (0, 20, 40, 60)),
        ("bottom-left", (0, 170, 40, 225)),
        ("bottom-right", (120, 170, 160, 225)),
        ("center", (65, 125, 95, 160)),
    ],
)
def test_a_logo_lands_where_it_was_asked_inside_the_safe_area(tmp_path: Path, position: str, box: tuple) -> None:
    source = _clip(tmp_path / "in.mp4", source="color=c=black:s=320x568:r=30:d=0.4", audio=False)
    output = _run(
        _compile(
            tmp_path,
            source,
            [{"op": "logo", "source": "logo", "position": position, "scale": 0.2, "opacity": 1.0}],
            sources={"logo": str(_swatch(tmp_path / "logo.png", "white"))},
        )
    )
    frame = _frames(output, 160, 284)[5]
    assert _region_mean(frame, 160, box) > 30
    left, right, top, bottom = _bright_bounds(frame, 160, 284)
    side, upper, lower = (value // 2 for value in safe_area(320, 568))
    assert left >= side - 2 and right <= 160 - side + 2 and top >= upper - 2 and bottom <= 284 - lower + 2


def test_a_logo_only_claims_its_own_band() -> None:
    top = overlays.logo_reserve("top-right", image_width=100, image_height=50, width=1000, height=1000, scale=0.2)
    assert top[0] > safe_area(1000, 1000)[1] + 100 and top[1] == 0
    bottom = overlays.logo_reserve("bottom-left", image_width=100, image_height=50, width=1000, height=1000, scale=0.2)
    assert bottom[0] == 0 and bottom[1] > safe_area(1000, 1000)[2] + 100
    assert overlays.logo_reserve("center", image_width=100, image_height=50, width=1000, height=1000, scale=0.2) == (
        0,
        0,
    )


@render
def test_a_title_moves_clear_of_a_logo_in_the_same_band(tmp_path: Path) -> None:
    """Text is planned before the logo, so the logo has to move text that already exists."""
    source = _clip(tmp_path / "in.mp4", source="color=c=black:s=640x640:r=30:d=0.4", audio=False)
    logo = _swatch(tmp_path / "logo.png", "0x0000ff", "120x60")
    ops = [
        {
            "op": "text",
            "text": "A title that spreads right across the top of this frame",
            "style": "title",
            "position": "top",
            "fade": 0,
            "color": "#FFFF00",
        },
        {"op": "logo", "source": "logo", "position": "top-right", "scale": 0.3, "opacity": 1.0},
    ]
    output = _run(
        _compile(tmp_path, source, ops, canvas=Canvas(width=640, height=640, fps=30.0), sources={"logo": str(logo)})
    )
    frame = _frames(output, 160, 160, chain="scale=in_color_matrix=bt709,format=rgb24", channels=3)[5]

    def rows(test) -> list[int]:
        return [
            y for y in range(160) if any(test(frame[(y * 160 + x) * 3 : (y * 160 + x) * 3 + 3]) for x in range(160))
        ]

    blue, yellow = rows(lambda p: p[2] > 150 and p[0] < 90), rows(lambda p: p[0] > 150 and p[1] > 150 and p[2] < 110)
    assert blue and yellow and min(yellow) > max(blue)


@render
def test_opacity_blends_with_the_picture(tmp_path: Path) -> None:
    source = _clip(tmp_path / "in.mp4", source="color=c=black:s=320x568:r=30:d=0.4", audio=False)
    logo = _swatch(tmp_path / "logo.png", "white")
    levels = []
    for index, opacity in enumerate((1.0, 0.5)):
        output = _run(
            _compile(
                tmp_path / str(index),
                source,
                [{"op": "logo", "source": "logo", "position": "center", "scale": 0.4, "opacity": opacity}],
                sources={"logo": str(logo)},
            )
        )
        levels.append(max(_frames(output, 160, 284)[5]))
    assert levels[1] == pytest.approx(levels[0] * 0.5, abs=30)


@render
def test_overlay_colors_survive_the_trip_to_bt709(tmp_path: Path) -> None:
    source = _clip(tmp_path / "in.mp4", source="color=c=black:s=320x568:r=30:d=0.4", audio=False)
    output = _run(
        _compile(
            tmp_path,
            source,
            [{"op": "logo", "source": "logo", "position": "center", "scale": 0.5, "opacity": 1.0}],
            sources={"logo": str(_swatch(tmp_path / "logo.png"))},
        )
    )
    frame = _frames(output, 160, 284, chain="scale=in_color_matrix=bt709,format=rgb24", channels=3)[5]
    middle = (142 * 160 + 80) * 3
    assert abs(frame[middle] - 0xE0) < 8 and abs(frame[middle + 1] - 0x10) < 12 and abs(frame[middle + 2] - 0x40) < 8


@render
def test_an_overlay_keeps_to_its_window_and_the_video_keeps_its_length(tmp_path: Path) -> None:
    """A fading overlay is an endless stream. It once held the picture open until the disk filled."""
    source = _clip(tmp_path / "in.mp4", source="color=c=black:s=320x568:r=30:d=2", audio=False)
    output = _run(
        _compile(
            tmp_path,
            source,
            [{"op": "overlay", "source": "logo", "start": 0.5, "duration": 1.0, "position": "center", "scale": 0.4}],
            sources={"logo": str(_swatch(tmp_path / "logo.png", "white"))},
        )
    )
    assert abs(probe(output).duration - 2.0) < 0.1
    peak = [max(frame) for frame in _frames(output, 80, 142)]
    assert peak[10] < 40 and peak[30] > 150 and peak[50] < 40


@render
def test_old_overlays_still_work(tmp_path: Path) -> None:
    source = _clip(tmp_path / "in.mp4", audio=False)
    output = _run(
        _compile(
            tmp_path,
            source,
            [{"op": "watermark", "source": "logo"}],
            sources={"logo": str(_swatch(tmp_path / "logo.png"))},
        )
    )
    assert probe(output).has_video


@render
def test_many_images_share_one_encode(tmp_path: Path) -> None:
    source = _clip(tmp_path / "in.mp4", audio=False)
    ops = [
        {"op": "overlay", "source": "logo", "start": index * 0.1, "duration": 0.1, "position": "center", "scale": 0.2}
        for index in range(5)
    ]
    compiler = _compile(tmp_path, source, ops, sources={"logo": str(_swatch(tmp_path / "logo.png"))})
    assert not any(step.name.startswith("pre_") for step in compiler.pipeline.steps)
    assert abs(probe(_run(compiler)).duration - 1.0) < 0.1


@render
def test_an_image_placed_before_assembly_is_settled_first(tmp_path: Path) -> None:
    """Assembly repeats pending filters per range, and an image's labels may only appear once."""
    source = _clip(tmp_path / "in.mp4", seconds=2.0)
    ranges = [{"start": 0.0, "end": 0.6}, {"start": 1.0, "end": 1.6}]
    compiler = _compile(
        tmp_path,
        source,
        [{"op": "logo", "source": "logo"}, {"op": "select_ranges", "ranges": ranges}],
        sources={"logo": str(_swatch(tmp_path / "logo.png"))},
    )
    assert abs(probe(_run(compiler)).duration - 1.2) < 0.1


@render
def test_text_logo_and_look_all_fuse_into_one_encode(tmp_path: Path) -> None:
    source = _clip(tmp_path / "in.mp4", seconds=2.0)
    compiler = _compile(
        tmp_path,
        source,
        [
            {"op": "look", "preset": "cinematic"},
            {"op": "text", "text": "Hi", "style": "title"},
            {"op": "logo", "source": "logo"},
        ],
        sources={"logo": str(_swatch(tmp_path / "logo.png"))},
    )
    assert len([step for step in compiler.pipeline.steps if not step.name.startswith("verify")]) == 1
    assert abs(probe(_run(compiler)).duration - 2.0) < 0.1


# Export


def test_portrait_4x5_is_a_first_class_aspect() -> None:
    assert _ASPECTS["4:5"] == "portrait_4x5"
    canvas = get_preset("portrait_4x5").canvas
    assert (canvas.width, canvas.height) == (1080, 1350)


@pytest.mark.parametrize(
    ("base", "quality", "size"),
    [
        ("shorts_9x16", "720p", (720, 1280)),
        ("shorts_9x16", "4k", (2160, 3840)),
        ("youtube_1080p", "720p", (1280, 720)),
        ("youtube_1080p", "4k", (3840, 2160)),
        ("square_1x1", "4k", (2160, 2160)),
        ("portrait_4x5", "720p", (720, 900)),
    ],
)
def test_quality_means_the_short_side_at_every_aspect(base: str, quality: str, size: tuple) -> None:
    canvas = get_preset(export_preset_name(base, quality=quality)).canvas
    assert (canvas.width, canvas.height) == size


def test_nothing_chosen_leaves_a_preset_alone() -> None:
    assert export_preset_name("shorts_9x16") == "shorts_9x16"


def test_gif_is_held_to_a_size_someone_can_post() -> None:
    preset = get_preset(export_preset_name("shorts_9x16", quality="4k", fps=60, format="gif"))
    assert min(preset.canvas.width, preset.canvas.height) == GIF_SIZE and preset.canvas.fps == GIF_FPS


def test_verification_knows_what_each_format_can_prove() -> None:
    assert verification(get_preset(export_preset_name("shorts_9x16", format="gif"))) == {"audio": False, "color": False}
    assert verification(get_preset(export_preset_name("shorts_9x16", format="webm"))) == {"audio": True, "color": False}
    assert verification(get_preset(export_preset_name("shorts_9x16", format="mov"))) == {"audio": True, "color": True}


def test_mov_keeps_the_videotoolbox_choice_and_webm_never_asks_for_faststart(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vedit.presets.has_encoder", lambda name: True)
    monkeypatch.setattr("vedit.presets.sys.platform", "darwin")
    mov = encode_args(get_preset(export_preset_name("shorts_9x16", format="mov")), hw=None)
    assert "h264_videotoolbox" in mov and "-q:v" in mov and "bt709" in mov
    assert "libx264" in encode_args(get_preset(export_preset_name("shorts_9x16", format="mov")), hw=False)
    webm = encode_args(get_preset(export_preset_name("shorts_9x16", format="webm")), hw=None)
    assert "libvpx-vp9" in webm and "libopus" in webm and "-movflags" not in webm and "h264_videotoolbox" not in webm


def _export(tmp_path: Path, source: Path, **choice: object) -> tuple[Path, list[str]]:
    brief = Brief(
        name="finish",
        sources={"main": str(source)},
        ops=[{"op": "export", "preset": "shorts_9x16", "path": str(tmp_path / "video.mp4"), **choice}],
        canvas=Canvas(width=320, height=568, fps=30.0),
        work_dir=str(tmp_path / "work"),
        hw=False,
    )
    compiler = compile_brief(brief, work_dir=tmp_path / "work")
    return _run(compiler), compiler.warnings


@render
@pytest.mark.parametrize(
    ("choice", "size", "fps", "container", "audio", "color"),
    [
        ({"quality": "720p"}, (720, 1280), 30, ".mp4", True, True),
        ({"quality": "720p", "fps": 24}, (720, 1280), 24, ".mp4", True, True),
        ({"quality": "720p", "fps": 60}, (720, 1280), 60, ".mp4", True, True),
        ({"quality": "720p", "format": "mov"}, (720, 1280), 30, ".mov", True, True),
        ({"quality": "720p", "format": "webm"}, (720, 1280), 30, ".webm", True, False),
        ({"quality": "720p", "format": "gif"}, (540, 960), 15, ".gif", False, False),
    ],
)
def test_every_format_comes_out_as_asked(
    tmp_path: Path, choice: dict, size: tuple, fps: int, container: str, audio: bool, color: bool
) -> None:
    output, _notes = _export(tmp_path, _clip(tmp_path / "in.mp4", size="1280x720", seconds=0.6), **choice)
    info = probe(output)
    # GIF delays are whole hundredths of a second, so its rate is only close.
    assert (
        output.suffix == container
        and (info.width, info.height) == size
        and info.fps == pytest.approx(fps, abs=2 if container == ".gif" else 0.6)
    )
    assert info.has_audio == audio and abs(info.duration - 0.6) < 0.2
    if color:
        assert info.video.color_space == "bt709" and info.video.color_range == "tv"
    assert json.loads(output.with_suffix(output.suffix + ".verify.json").read_text())["has_video"]


@render
def test_gif_survives_a_fade_and_loudnorm_it_has_no_sound_for(tmp_path: Path) -> None:
    source = _clip(tmp_path / "in.mp4", size="640x360", seconds=1.0)
    brief = Brief(
        name="finish",
        sources={"main": str(source)},
        ops=[
            {"op": "loudnorm"},
            {"op": "fade_out", "duration": 0.3},
            {"op": "export", "preset": "shorts_9x16", "path": str(tmp_path / "v.mp4"), "format": "gif"},
        ],
        canvas=Canvas(width=320, height=568, fps=30.0),
        work_dir=str(tmp_path / "work"),
        hw=False,
    )
    output = _run(compile_brief(brief, work_dir=tmp_path / "work"))
    assert output.suffix == ".gif" and not probe(output).has_audio
    frames = _frames(output, 40, 71)
    assert _mean(frames[-1]) < _mean(frames[2]) * 0.65  # the fade reached the palette


@render
def test_upscaling_past_the_source_says_so(tmp_path: Path) -> None:
    source = _clip(tmp_path / "in.mp4", size="640x360", seconds=0.4)
    _output, notes = _export(tmp_path, source, quality="1080p", fps=24)
    assert any("upscaled" in note for note in notes)
    _output, notes = _export(tmp_path / "b", _clip(tmp_path / "big.mp4", size="1920x1080", seconds=0.4), quality="720p")
    assert not any("upscaled" in note for note in notes)


@render
def test_the_tape_is_drawn_at_the_export_size(tmp_path: Path) -> None:
    """Text burned at 1080p and scaled up to 4K would go soft."""
    source = _clip(tmp_path / "in.mp4", size="1280x720", seconds=0.4)
    brief = Brief(
        name="finish",
        sources={"main": str(source)},
        ops=[{"op": "reframe", "aspect": "4:5"}, {"op": "export", "preset": "portrait_4x5", "quality": "720p"}],
        canvas=Canvas(width=720, height=900, fps=30.0),
        work_dir=str(tmp_path / "work"),
        hw=False,
    )
    compiler = compile_brief(brief, work_dir=tmp_path / "work")
    assert (compiler.canvas.width, compiler.canvas.height) == (720, 900)


def test_a_gif_plan_writes_a_gif_path(tmp_path: Path) -> None:
    edit = edit_spec.validate_edit({"export": {"format": "gif"}}, duration=4)
    plan = {"segments": [{"start": 0, "end": 4}], "edit": edit}
    ctx = context(
        plan, request={}, aspect="9:16", canvas=Canvas(width=540, height=960, fps=15.0), work=tmp_path, has_audio=True
    )
    assert build_ops(ctx)[-1]["path"].endswith("video.gif")


# The whole chain


@render
def test_every_finishing_op_together_on_one_tape(tmp_path: Path) -> None:
    audio, _bursts = _noisy_speech(tmp_path)
    tape = _speech_tape(tmp_path, audio, 9.0)
    ranges = [{"start": 0.5, "end": 2.0}, {"start": 3.0, "end": 4.5, "speed": 2.0}, {"start": 5.5, "end": 7.5}]
    transition = {"type": "fade", "seconds": 0.2}
    ops = [
        {"op": "select_ranges", "ranges": ranges, "transition": transition},
        {"op": "stabilize"},
        {
            "op": "zoom",
            "moves": [
                {"start": 0, "end": 1.2, "from": {"scale": 1}, "to": {"scale": 1.1}},
                {"start": 1.5, "end": 3.5, "from": {"scale": 1.2}, "to": {"scale": 1.2}},
            ],
            "blend": 0.2,
        },
        {"op": "look", "preset": "film", "intensity": 0.7, "sharpen": 0.3},
        {"op": "voice", "denoise": True, "enhance": True},
        {"op": "text", "text": '100% real: "ok"', "style": "callout", "start": 0.2, "duration": 2},
        {"op": "logo", "source": "logo"},
        {"op": "overlay", "source": "logo", "start": 1, "duration": 1, "position": "bottom-left", "scale": 0.2},
        {"op": "fade_in", "duration": 0.2},
        {"op": "fade_out", "duration": 0.3},
        {"op": "music", "source": "bed", "duck": True},
        {"op": "loudnorm", "i": -16},
    ]
    output = _run(
        _compile(
            tmp_path,
            tape,
            ops,
            canvas=Canvas(width=360, height=640, fps=30.0),
            sources={"logo": str(_swatch(tmp_path / "logo.png")), "bed": str(_bed(tmp_path / "bed.wav", 2.0))},
        )
    )
    info = probe(output)
    expected = plan_timeline({"segments": ranges, "edit": {"transition": transition}})["duration"]
    assert abs(info.duration - expected) < 0.1
    assert info.has_audio and info.video.color_space == "bt709" and info.video.color_range == "tv"
