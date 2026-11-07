"""Assembly on one clock: every render here is probed against ``plan_timeline``.

The source is a tiny clip with a white flash and a tone burst at the same
known source times, so picture and sound can be measured in the output.
"""

from __future__ import annotations

import array
import math
import subprocess
from pathlib import Path

import pytest

from vedit.compile import compile_brief
from vedit.edit_spec import TRANSITIONS, validate_edit
from vedit.errors import CompileError
from vedit.ir import Brief, Canvas
from vedit.media import probe, resolve_ffmpeg
from vedit.pipeline import execute
from vedit.timeline import plan_timeline, to_output

FPS = 30
FRAME = 1 / FPS
RATE = 48000
SECONDS = 16
FLASHES = [float(second) for second in range(1, SECONDS, 2)]
ONE_OR_TWO_FRAMES = 2 * FRAME + 0.001


def _source(path: Path, *, audio: bool = True) -> Path:
    flash = "gte(mod(t,2),1)*lt(mod(t,2),1.1)"
    command = [
        resolve_ffmpeg(),
        "-hide_banner",
        "-v",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        (
            f"color=c=0x202020:s=160x90:r={FPS}:d={SECONDS},format=yuv420p,"
            f"drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:enable='{flash}'"
        ),
    ]
    if audio:
        command += ["-f", "lavfi", "-i", f"aevalsrc='0.5*sin(2*PI*1000*t)*{flash}':s={RATE}:d={SECONDS}:c=stereo"]
    command += ["-c:v", "libx264", "-preset", "veryfast", "-g", "15", "-pix_fmt", "yuv420p"]
    command += ["-c:a", "aac", "-b:a", "128k"] if audio else ["-an"]
    subprocess.run(command + [str(path)], check=True)
    return path


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    try:
        resolve_ffmpeg()
    except Exception:
        pytest.skip("ffmpeg required")
    return _source(tmp_path_factory.mktemp("ranges") / "source.mp4")


@pytest.fixture(scope="module")
def silent_clip(tmp_path_factory):
    try:
        resolve_ffmpeg()
    except Exception:
        pytest.skip("ffmpeg required")
    return _source(tmp_path_factory.mktemp("ranges-silent") / "silent.mp4", audio=False)


def _plan(segments, *, removed=None, transition=None) -> dict:
    edit = {"removed": removed or []}
    if transition:
        edit["transition"] = {"type": transition[0], "seconds": transition[1]}
    return {"segments": segments, "edit": edit}


def _op(view: dict, plan: dict) -> dict:
    op = {"op": "select_ranges", "ranges": view["segments"]}
    transition = plan["edit"].get("transition")
    if transition:
        op["transition"] = transition
    return op


def _render(source: Path, work: Path, plan: dict):
    view = plan_timeline(plan)
    brief = Brief(
        name="ranges",
        sources={"main": str(source)},
        ops=[{"op": "fit"}, _op(view, plan), {"op": "export", "preset": "web_preview"}],
        canvas=Canvas(width=160, height=90, fps=FPS),
        work_dir=str(work),
        hw=False,
    )
    compiler = compile_brief(brief, work_dir=work)
    assert compiler.duration == view["duration"]
    execute(compiler.pipeline, progress=False)
    return view, compiler.finals[0], probe(compiler.finals[0])


def _matches_plan(view: dict, info) -> None:
    assert info.has_video
    assert info.duration == pytest.approx(view["duration"], abs=ONE_OR_TWO_FRAMES)


def _luma(path: Path) -> list[float]:
    raw = subprocess.run(
        [resolve_ffmpeg(), "-v", "error", "-i", str(path), "-vf", "scale=8:8,format=gray", "-f", "rawvideo", "-"],
        check=True,
        capture_output=True,
    ).stdout
    return [sum(raw[index : index + 64]) / 64 for index in range(0, len(raw), 64)]


def _samples(path: Path) -> array.array:
    raw = subprocess.run(
        [
            resolve_ffmpeg(),
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            str(RATE),
            "-f",
            "s16le",
            "-",
        ],
        check=True,
        capture_output=True,
    ).stdout
    samples = array.array("h")
    samples.frombytes(raw)
    return samples


def _flash_times(path: Path) -> list[float]:
    luma = _luma(path)
    return [index * FRAME for index in range(1, len(luma)) if luma[index] > 120 >= luma[index - 1]]


def _burst_times(path: Path) -> list[float]:
    samples = _samples(path)
    window = RATE // 1000
    peaks = [max(map(abs, samples[index : index + window]), default=0) for index in range(0, len(samples), window)]
    found: list[float] = []
    for index in range(1, len(peaks)):
        if peaks[index] > 3000 >= peaks[index - 1] and (not found or index / 1000 - found[-1] > 0.25):
            found.append(index / 1000)
    return found


def _rms(path: Path) -> float:
    samples = _samples(path)
    return (sum(value * value for value in samples) / max(1, len(samples))) ** 0.5


def _expected(view: dict) -> list[float]:
    return sorted(moment for moment in (to_output(view["spans"], time) for time in FLASHES) if moment is not None)


def test_plain_ranges_match_the_clock_and_keep_the_compiler_duration(clip, tmp_path):
    plan = _plan([{"start": 1.2, "end": 3.4}, {"start": 5.1, "end": 6.0}])
    view, _, info = _render(clip, tmp_path, plan)
    assert view["duration"] == 3.1
    _matches_plan(view, info)
    assert info.has_audio


def test_compiler_duration_is_the_timeline_duration(clip, tmp_path):
    plan = _plan(
        [{"start": 0.5, "end": 4.5, "speed": 1.5}, {"start": 7.1, "end": 8.9}],
        removed=[[2.0, 2.7]],
        transition=("fade", 0.4),
    )
    view = plan_timeline(plan)
    compiler = compile_brief(
        Brief(
            name="ranges",
            sources={"main": str(clip)},
            ops=[_op(view, plan)],
            canvas=Canvas(width=160, height=90, fps=FPS),
            work_dir=str(tmp_path),
            hw=False,
        ),
        work_dir=tmp_path,
    )
    assert compiler.duration == view["duration"]
    selected = next(step for step in compiler.pipeline.steps if step.name == "select_ranges")
    assert "xfade=transition=fade" in " ".join(selected.argv) and "acrossfade=" in " ".join(selected.argv)
    assert compiler.pipeline.meta["ops"][0]["ranges"][0]["keep"]
    assert (tmp_path / "01_select_ranges.filter.txt").is_file()


def test_keep_removes_only_the_cut_material(clip, tmp_path):
    plan = _plan([{"start": 0.5, "end": 8.5}], removed=[[1.6, 2.7], [3.5, 4.8], [7.4, 8.1]])
    view, path, info = _render(clip, tmp_path, plan)
    assert view["duration"] == pytest.approx(8.0 - 1.1 - 1.3 - 0.7, abs=0.001)
    _matches_plan(view, info)
    assert _flash_times(path) == pytest.approx(_expected(view), abs=1.5 * FRAME)
    assert _burst_times(path) == pytest.approx(_expected(view), abs=0.02)


@pytest.mark.parametrize("speed", [0.25, 0.5, 0.75, 1.5, 2.0, 4.0])
def test_speed_changes_length_for_both_streams(clip, tmp_path, speed):
    plan = _plan([{"start": 2.2, "end": 4.2, "speed": speed}])
    view, path, info = _render(clip, tmp_path, plan)
    assert view["duration"] == pytest.approx(2.0 / speed, abs=0.001)
    _matches_plan(view, info)
    assert len(_samples(path)) / RATE == pytest.approx(view["duration"], abs=ONE_OR_TWO_FRAMES)
    assert len(_flash_times(path)) == 1


def test_volume_scales_only_its_range(clip, tmp_path):
    loud = _rms(_render(clip, tmp_path / "loud", _plan([{"start": 0.5, "end": 1.5}]))[1])
    half = _rms(_render(clip, tmp_path / "half", _plan([{"start": 0.5, "end": 1.5, "volume": 0.5}]))[1])
    boost = _rms(_render(clip, tmp_path / "boost", _plan([{"start": 0.5, "end": 1.5, "volume": 2.0}]))[1])
    mute = _rms(_render(clip, tmp_path / "mute", _plan([{"start": 0.5, "end": 1.5, "volume": 0}]))[1])
    assert half / loud == pytest.approx(0.5, abs=0.06)
    assert boost / loud == pytest.approx(2.0, abs=0.2)
    assert mute < 2


def test_slideright_validates_and_compiles_as_xfade(clip, tmp_path):
    """A new catalogue id is the xfade name the compiler passes through."""
    added = (
        "slideright",
        "slidedown",
        "wiperight",
        "wipeup",
        "wipedown",
        "circleclose",
        "smoothleft",
        "pixelize",
    )
    assert set(added) <= {row[0] for row in TRANSITIONS}
    for kind in added:
        assert validate_edit({"transition": {"type": kind, "seconds": 0.4}}, duration=10)["transition"]["type"] == kind
    plan = _plan([{"start": 0.5, "end": 2.5}, {"start": 3.0, "end": 5.0}], transition=("slideright", 0.4))
    view = plan_timeline(plan)
    compiler = compile_brief(
        Brief(
            name="ranges",
            sources={"main": str(clip)},
            ops=[_op(view, plan)],
            canvas=Canvas(width=160, height=90, fps=FPS),
            work_dir=str(tmp_path),
            hw=False,
        ),
        work_dir=tmp_path,
    )
    selected = next(step for step in compiler.pipeline.steps if step.name == "select_ranges")
    graph = " ".join(selected.argv) + (tmp_path / "01_select_ranges.filter.txt").read_text(encoding="utf-8")
    assert "xfade=transition=slideright" in graph


@pytest.mark.parametrize("kind", [row[0] for row in TRANSITIONS if row[0] != "none"])
def test_every_catalogue_transition_renders_to_the_clock(clip, tmp_path, kind):
    plan = _plan(
        [{"start": 0.2, "end": 1.9}, {"start": 2.2, "end": 3.9}, {"start": 8.2, "end": 9.9}], transition=(kind, 0.4)
    )
    view, _, info = _render(clip, tmp_path, plan)
    assert view["transition_seconds"] == 0.4
    assert view["duration"] == pytest.approx(5.1 - 0.8, abs=0.001)
    _matches_plan(view, info)
    assert info.has_audio


def test_a_transition_is_clamped_by_the_timeline_not_ffmpeg(clip, tmp_path):
    plan = _plan(
        [{"start": 0.2, "end": 0.7}, {"start": 2.2, "end": 2.7}, {"start": 4.2, "end": 4.7}], transition=("fade", 2.0)
    )
    view, _, info = _render(clip, tmp_path, plan)
    assert view["transition_seconds"] == pytest.approx(0.2)
    _matches_plan(view, info)


def test_keep_speed_volume_and_transition_together(clip, tmp_path):
    plan = _plan(
        [
            {"start": 0.5, "end": 5.5, "speed": 1.5, "volume": 0.8},
            {"start": 8.2, "end": 11.5, "speed": 0.75},
            {"start": 12.5, "end": 15.5},
        ],
        removed=[[1.5, 2.7], [8.9, 9.6], [13.5, 14.5]],
        transition=("dissolve", 0.3),
    )
    view, _, info = _render(clip, tmp_path, plan)
    _matches_plan(view, info)
    assert info.has_audio


def test_segments_play_in_editorial_order(clip, tmp_path):
    plan = _plan([{"start": 8.5, "end": 9.5}, {"start": 0.5, "end": 1.5}, {"start": 4.5, "end": 5.5}])
    view, path, info = _render(clip, tmp_path, plan)
    _matches_plan(view, info)
    # 9 s plays first, so its flash is at 0.5; order is the plan's, not the source's.
    assert view["spans"][0].source_start == 8.5
    assert _flash_times(path) == pytest.approx([0.5, 1.5, 2.5], abs=1.5 * FRAME)
    assert _burst_times(path) == pytest.approx([0.5, 1.5, 2.5], abs=0.02)


def test_out_of_order_keep_speed_and_transition(clip, tmp_path):
    plan = _plan(
        [{"start": 8.2, "end": 11.5, "speed": 1.25}, {"start": 0.5, "end": 3.5}],
        removed=[[9.3, 10.2], [1.3, 2.5]],
        transition=("wipeleft", 0.25),
    )
    view, path, info = _render(clip, tmp_path, plan)
    _matches_plan(view, info)
    assert _flash_times(path) == pytest.approx(_expected_in_plan_order(view), abs=2 * FRAME)


def _expected_in_plan_order(view: dict) -> list[float]:
    found: list[float] = []
    for span in view["spans"]:
        for time in FLASHES:
            if span.source_start <= time < span.source_end:
                found.append(span.output_start + (time - span.source_start) / span.speed)
    return sorted(found)


@pytest.mark.parametrize(
    "speed, kind", [(1.0, None), (1.5, None), (0.75, "fade"), (1.25, "fadeblack"), (2.0, "wipeleft")]
)
def test_sound_stays_with_picture_after_keep_speed_and_transition(clip, tmp_path, speed, kind):
    """A flash and a tone burst share a source time, so they must share an output time."""
    plan = _plan(
        [{"start": 0.4, "end": 5.6, "speed": speed}, {"start": 6.4, "end": 12.0, "speed": speed}],
        removed=[[1.6, 2.5], [3.6, 4.6], [8.0, 8.6], [10.4, 10.7]],
        transition=(kind, 0.3) if kind else None,
    )
    view, path, info = _render(clip, tmp_path, plan)
    _matches_plan(view, info)
    flashes, bursts, expected = _flash_times(path), _burst_times(path), _expected(view)
    assert len(flashes) == len(bursts) == len(expected) >= 5
    # Half a frame of mapping, half a frame of rounding, and atempo's own window.
    assert flashes == pytest.approx(expected, abs=2 * FRAME + 0.001)
    assert bursts == pytest.approx(expected, abs=0.05)
    assert max(abs(picture - sound) for picture, sound in zip(flashes, bursts)) <= 0.05


def test_many_small_cuts_do_not_drift(clip, tmp_path):
    """Forty cuts: rounding a piece at a time would land several frames off by the end."""
    removed = [
        [0.2 + 0.37 * index + 0.031 * (index % 3), 0.27 + 0.37 * index + 0.031 * (index % 3) + 0.01 * (index % 2)]
        for index in range(40)
    ]
    removed = [item for item in removed if not any(item[0] - 0.05 <= time <= item[1] + 0.15 for time in FLASHES)]
    plan = _plan([{"start": 0.0, "end": 15.5}], removed=removed)
    view, path, info = _render(clip, tmp_path, plan)
    assert len(view["spans"]) >= 20
    _matches_plan(view, info)
    assert _flash_times(path) == pytest.approx(_expected(view), abs=1.5 * FRAME)
    assert _burst_times(path) == pytest.approx(_expected(view), abs=0.02)


def test_a_transport_stream_lands_on_the_right_frames(clip, tmp_path):
    """TS seeking can land a whole GOP past the request; the flash must still be where the plan says."""
    stream = tmp_path / "source.ts"
    subprocess.run(
        [
            resolve_ffmpeg(),
            "-v",
            "error",
            "-y",
            "-i",
            str(clip),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-c:a",
            "aac",
            "-f",
            "mpegts",
            str(stream),
        ],
        check=True,
    )
    plan = _plan(
        [{"start": 0.4, "end": 3.6}, {"start": 6.4, "end": 7.6}], removed=[[1.6, 2.5]], transition=("fade", 0.3)
    )
    view, path, info = _render(stream, tmp_path / "out", plan)
    _matches_plan(view, info)
    assert _flash_times(path) == pytest.approx(_expected(view), abs=2 * FRAME)
    assert _burst_times(path) == pytest.approx(_expected(view), abs=0.05)


def test_cuts_through_a_steady_tone_do_not_click(tmp_path):
    source = tmp_path / "tone.mp4"
    subprocess.run(
        [
            resolve_ffmpeg(),
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=gray:s=160x90:r={FPS}:d=6",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate={RATE}:duration=6",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(source),
        ],
        check=True,
    )
    # These edges fall on different phases of the wave, so a hard cut jumps by far more than its slope.
    plan = _plan(
        [{"start": 0.2, "end": 5.5}, {"start": 0.02, "end": 0.19}],
        removed=[[1.0013, 1.2071], [2.0031, 2.2219], [3.0077, 3.3107], [4.0111, 4.3173]],
    )
    _, path, _ = _render(source, tmp_path / "out", plan)
    samples = _samples(path)
    peak = max(map(abs, samples))
    biggest = max(abs(samples[index] - samples[index - 1]) for index in range(1, len(samples)))
    natural = 2 * math.sin(math.pi * 440 / RATE)
    assert biggest / peak < 5 * natural  # a hard cut measured about 20 times the wave's own slope


def test_many_pieces_fit_ffmpegs_expression_limits(clip, tmp_path):
    """A long flat sum stops parsing at around a hundred terms."""
    plan = _plan(
        [{"start": 0.0, "end": 15.5}], removed=[[0.09 + 0.14 * index, 0.14 + 0.14 * index] for index in range(105)]
    )
    view, _, info = _render(clip, tmp_path, plan)
    assert len(view["spans"]) > 100
    _matches_plan(view, info)


def test_no_audio_source_supports_every_timing_feature(silent_clip, tmp_path):
    plan = _plan(
        [{"start": 0.5, "end": 3.5, "speed": 2.0, "volume": 0.5}, {"start": 6.5, "end": 9.5}],
        removed=[[1.4, 2.0]],
        transition=("fade", 0.25),
    )
    view, path, info = _render(silent_clip, tmp_path, plan)
    _matches_plan(view, info)
    assert not info.has_audio
    assert _flash_times(path) == pytest.approx(_expected(view), abs=2 * FRAME)


def test_audio_only_source_keeps_the_same_clock(clip, tmp_path):
    sound = tmp_path / "sound.wav"
    subprocess.run([resolve_ffmpeg(), "-v", "error", "-y", "-i", str(clip), "-vn", str(sound)], check=True)
    plan = _plan(
        [{"start": 0.5, "end": 3.5}, {"start": 6.5, "end": 9.5, "speed": 1.5}],
        removed=[[1.5, 2.0]],
        transition=("fade", 0.2),
    )
    view = plan_timeline(plan)
    brief = Brief(
        name="ranges",
        sources={"main": str(sound)},
        ops=[_op(view, plan), {"op": "export", "preset": "podcast_audio"}],
        canvas=Canvas(width=160, height=90, fps=FPS),
        work_dir=str(tmp_path),
        hw=False,
    )
    compiler = compile_brief(brief, work_dir=tmp_path)
    execute(compiler.pipeline, progress=False)
    info = probe(compiler.finals[0])
    assert info.has_audio and not info.has_video
    assert info.duration == pytest.approx(view["duration"], abs=0.06)


def test_tiny_ranges_still_render_with_a_transition(clip, tmp_path):
    plan = _plan(
        [
            {"start": 0.2, "end": 0.3, "speed": 4.0},
            {"start": 0.4, "end": 0.5, "speed": 4.0},
            {"start": 0.6, "end": 0.9},
        ],
        transition=("fade", 0.3),
    )
    view, _, info = _render(clip, tmp_path, plan)
    assert info.duration == pytest.approx(view["duration"], abs=3 * FRAME)


def test_thirty_two_ranges_with_transitions_hold_up(clip, tmp_path):
    segments = [{"start": 0.1 + index * 0.45, "end": 0.45 + index * 0.45} for index in range(32)]
    view, _, info = _render(clip, tmp_path, _plan(segments, transition=("fade", 0.1)))
    assert len(view["segments"]) == 32
    _matches_plan(view, info)


@pytest.mark.parametrize(
    "bad",
    [
        {"keep": []},
        {"keep": "0.5,1"},
        {"keep": [[0.5]]},
        {"keep": [[0.5, "1"]]},
        {"keep": [[0.5, float("nan")]]},
        {"keep": [[0.2, 1.0]]},
        {"keep": [[1.0, 0.8]]},
        {"keep": [[0.6, 1.9]]},
        {"speed": 0},
        {"speed": 0.1},
        {"speed": 5},
        {"speed": "2"},
        {"speed": True},
        {"speed": float("inf")},
        {"volume": -0.1},
        {"volume": 2.5},
        {"volume": "loud"},
        {"volume": float("nan")},
    ],
)
def test_range_fields_are_validated(clip, tmp_path, bad):
    with pytest.raises(CompileError):
        compile_brief(
            Brief(
                name="ranges",
                sources={"main": str(clip)},
                ops=[{"op": "select_ranges", "ranges": [{"start": 0.5, "end": 1.5, **bad}]}],
                work_dir=str(tmp_path),
                hw=False,
            ),
            work_dir=tmp_path,
        )


@pytest.mark.parametrize(
    "transition",
    [
        "fade",
        {"type": "evil:offset=0"},
        {"type": "fade;[0:v]"},
        {"type": 3},
        {"type": "fade", "seconds": "1"},
        {"type": "fade", "seconds": -1},
        {"type": "fade", "seconds": float("nan")},
    ],
)
def test_transition_is_a_closed_catalogue(clip, tmp_path, transition):
    with pytest.raises(CompileError):
        compile_brief(
            Brief(
                name="ranges",
                sources={"main": str(clip)},
                ops=[
                    {
                        "op": "select_ranges",
                        "ranges": [{"start": 0.5, "end": 1.5}, {"start": 2.5, "end": 3.5}],
                        "transition": transition,
                    }
                ],
                work_dir=str(tmp_path),
                hw=False,
            ),
            work_dir=tmp_path,
        )


def test_a_none_transition_is_a_hard_cut(clip, tmp_path):
    plan = _plan([{"start": 0.5, "end": 1.5}, {"start": 2.5, "end": 3.5}], transition=("none", 0.5))
    view, _, info = _render(clip, tmp_path, plan)
    assert view["transition_seconds"] == 0 and view["duration"] == 2.0
    _matches_plan(view, info)
