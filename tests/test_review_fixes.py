"""Code-review findings on the render pipeline and compiler, each proven on a real file.

Resume fingerprints, honest reports after a failure, preset CRF, bounded
stand-in pictures in concat, even tape sizes, trim following its source's
streams, strict export verification, and BT.709 conversion on every input path.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from vedit.analyze import apply_verify
from vedit.compile import SQUARE_TAPE, compile_brief
from vedit.errors import AnalysisError, RenderError
from vedit.ir import Brief, Canvas, brief_from_mapping
from vedit.media import probe, resolve_ffmpeg
from vedit.pipeline import DONE_DIR, Pipeline, Step, execute, load_pipeline, reset_resume_state, write_report
from vedit.presets import get_preset

pytestmark = pytest.mark.render


def ffmpeg(*args: str) -> None:
    subprocess.run([resolve_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def clip(
    path: Path,
    *,
    seconds: float = 1.0,
    size: str = "320x180",
    pix_fmt: str = "yuv420p",
    audio: bool = True,
    extra: tuple[str, ...] = (),
) -> Path:
    width, height = size.split("x")
    args = ["-f", "lavfi", "-i", f"testsrc2=size=320x180:rate=30:duration={seconds},scale={width}:{height},setsar=1"]
    if audio:
        args += [
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate=48000:duration={seconds}",
            "-shortest",
            "-c:a",
            "aac",
        ]
    ffmpeg(*args, "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", pix_fmt, *extra, str(path))
    return path


def sound(path: Path, *, seconds: float = 0.8) -> Path:
    ffmpeg("-f", "lavfi", "-i", f"sine=frequency=660:sample_rate=48000:duration={seconds}", "-c:a", "aac", str(path))
    return path


def touch_step(dest: Path, text: str = "one") -> Step:
    code = f"from pathlib import Path; Path({str(dest)!r}).write_text({text!r})"
    return Step(dest.stem, [sys.executable, "-c", code], [str(dest)], f"write {dest.name}")


def copy_step(src: Path, dest: Path) -> Step:
    """A final file the report can probe: a real clip, copied."""
    code = f"import shutil; shutil.copyfile({str(src)!r}, {str(dest)!r})"
    return Step(dest.stem, [sys.executable, "-c", code, str(src)], [str(dest)], f"copy {dest.name}")


# ---------------------------------------------------------------------------
# 1. Resume skips a step only when the same command ran on the same inputs
# ---------------------------------------------------------------------------


def test_resume_skips_only_a_finished_step_with_the_same_command(tmp_path: Path) -> None:
    out = tmp_path / "a.txt"
    pipe = Pipeline(name="p", work_dir=tmp_path, steps=[touch_step(out, "one")])
    first = execute(pipe, progress=False)
    assert first[0].ok and (tmp_path / DONE_DIR / "01_a.json").is_file()
    again = execute(pipe, resume=True, progress=False)
    assert "skipped" in again[0].stderr_tail
    # The same output path, a different command: the old file is not the new render.
    changed = Pipeline(name="p", work_dir=tmp_path, steps=[touch_step(out, "two")])
    reran = execute(changed, resume=True, progress=False)
    assert "skipped" not in (reran[0].stderr_tail or "")
    assert out.read_text() == "two"


def test_resume_reruns_when_an_input_file_changed(tmp_path: Path) -> None:
    src = tmp_path / "in.txt"
    src.write_text("v1")
    out = tmp_path / "out.txt"
    code = f"from pathlib import Path; Path({str(out)!r}).write_text(Path({str(src)!r}).read_text())"
    step = Step("copy", [sys.executable, "-c", code, str(src)], [str(out)], "copy")
    pipe = Pipeline(name="p", work_dir=tmp_path, steps=[step])
    execute(pipe, progress=False)
    assert "skipped" in execute(pipe, resume=True, progress=False)[0].stderr_tail
    src.write_text("v2 longer")
    reports = execute(pipe, resume=True, progress=False)
    assert "skipped" not in (reports[0].stderr_tail or "")
    assert out.read_text() == "v2 longer"


def test_a_redone_step_reruns_everything_after_it(tmp_path: Path) -> None:
    first, second = tmp_path / "first.txt", tmp_path / "second.txt"
    pipe = Pipeline(name="p", work_dir=tmp_path, steps=[touch_step(first, "one"), touch_step(second, "same")])
    execute(pipe, progress=False)
    changed = Pipeline(name="p", work_dir=tmp_path, steps=[touch_step(first, "two"), touch_step(second, "same")])
    reports = execute(changed, resume=True, progress=False)
    assert all("skipped" not in (r.stderr_tail or "") for r in reports), [r.stderr_tail for r in reports]


def test_a_new_plan_in_the_same_folder_forgets_finished_steps_and_the_old_report(tmp_path: Path) -> None:
    source = clip(tmp_path / "in.mp4")
    work = tmp_path / "work"
    brief = Brief(
        name="plan",
        sources={"main": str(source)},
        ops=[{"op": "trim", "start": 0, "end": 0.5}, {"op": "export", "preset": "web_preview"}],
        canvas=Canvas(width=320, height=180),
        work_dir=str(work),
        hw=False,
    )
    compiler = compile_brief(brief)
    reports = execute(compiler.pipeline, progress=False)
    report = write_report(compiler.pipeline, reports, finals=compiler.finals)
    assert json.loads(report.read_text())["ok"] is True
    assert list((work / DONE_DIR).iterdir())
    compile_brief(brief)
    assert not list((work / DONE_DIR).iterdir())
    assert not report.exists()
    reset_resume_state(tmp_path / "never-made")  # a folder with nothing in it is fine


def test_a_resumed_render_with_a_reusable_plan_still_skips(tmp_path: Path) -> None:
    source = clip(tmp_path / "in.mp4")
    work = tmp_path / "work"
    brief = Brief(
        name="plan",
        sources={"main": str(source)},
        ops=[{"op": "trim", "start": 0, "end": 0.5}, {"op": "export", "preset": "web_preview"}],
        canvas=Canvas(width=320, height=180),
        work_dir=str(work),
        hw=False,
    )
    execute(compile_brief(brief).pipeline, progress=False)
    reports = execute(load_pipeline(work), resume=True, progress=False)
    assert all("skipped" in (r.stderr_tail or "") for r in reports)


# ---------------------------------------------------------------------------
# 2. A failed render never leaves the previous passing report behind
# ---------------------------------------------------------------------------


def test_a_failed_step_replaces_the_old_report_with_a_failing_one(tmp_path: Path) -> None:
    source = sound(tmp_path / "src.m4a")
    good = tmp_path / "good.m4a"
    pipe = Pipeline(name="p", work_dir=tmp_path, steps=[copy_step(source, good)], meta={"finals": [str(good)]})
    report = write_report(pipe, execute(pipe, progress=False), finals=[good])
    assert json.loads(report.read_text())["ok"] is True
    broken = Step(
        "explode",
        [sys.executable, "-c", "import sys; sys.stderr.write('boom here\\n'); sys.exit(3)"],
        [str(tmp_path / "never.txt")],
        "fail",
    )
    failing = Pipeline(
        name="p",
        work_dir=tmp_path,
        steps=[copy_step(source, good), broken],
        meta={"finals": [str(tmp_path / "never.txt")]},
    )
    with pytest.raises(RenderError):
        execute(failing, progress=False)
    written = json.loads(report.read_text())
    assert written["ok"] is False
    assert written["failed"]["step"] == "explode"
    assert "boom here" in written["failed"]["stderr_tail"]
    assert [step["ok"] for step in written["steps"]] == [True, False]
    assert written["outputs"] == [{"path": str(tmp_path / "never.txt"), "exists": False}]


def test_the_old_report_is_gone_while_a_render_runs(tmp_path: Path) -> None:
    stale = tmp_path / "report.json"
    stale.write_text(json.dumps({"ok": True, "stale": True}))
    witness = tmp_path / "seen.txt"
    code = f"from pathlib import Path; Path({str(witness)!r}).write_text(str(Path({str(stale)!r}).exists()))"
    pipe = Pipeline(
        name="p", work_dir=tmp_path, steps=[Step("look", [sys.executable, "-c", code], [str(witness)], "look")]
    )
    execute(pipe, progress=False)
    assert witness.read_text() == "False"


def test_report_ok_needs_every_final_file_to_exist_and_read(tmp_path: Path) -> None:
    source = sound(tmp_path / "src.m4a")
    good, text = tmp_path / "good.m4a", tmp_path / "notes.txt"
    pipe = Pipeline(name="p", work_dir=tmp_path, steps=[copy_step(source, good), touch_step(text)])
    reports = execute(pipe, progress=False)
    assert json.loads(write_report(pipe, reports, finals=[good]).read_text())["ok"] is True
    assert json.loads(write_report(pipe, reports, finals=[good, tmp_path / "missing.mp4"]).read_text())["ok"] is False
    unreadable = json.loads(write_report(pipe, reports, finals=[text]).read_text())
    assert (
        unreadable["ok"] is False and unreadable["outputs"][0]["exists"] is True and "error" in unreadable["outputs"][0]
    )


# ---------------------------------------------------------------------------
# 3. A preset's own CRF is used unless the brief sets one
# ---------------------------------------------------------------------------


def _export_argv(tmp_path: Path, brief_kwargs: dict, preset: str) -> list[str]:
    source = clip(tmp_path / "in.mp4")
    brief = Brief(
        name="crf",
        sources={"main": str(source)},
        ops=[{"op": "export", "preset": preset}],
        canvas=Canvas(width=320, height=180),
        work_dir=str(tmp_path / "work"),
        hw=False,
        **brief_kwargs,
    )
    compiler = compile_brief(brief)
    return next(step.argv for step in compiler.pipeline.steps if step.name.startswith("export_"))


def test_brief_crf_defaults_to_the_preset(tmp_path: Path) -> None:
    assert Brief(name="x", sources={}, ops=[]).crf is None
    assert brief_from_mapping({"name": "x", "sources": {}, "ops": []}).crf is None
    assert brief_from_mapping({"name": "x", "sources": {}, "ops": [], "crf": 20}).crf == 20
    assert "--crf" not in _export_argv(tmp_path, {}, "web_preview")
    (tmp_path / "set").mkdir()
    argv = _export_argv(tmp_path / "set", {"crf": 21}, "web_preview")
    assert argv[argv.index("--crf") + 1] == "21"


@pytest.mark.parametrize("name", ["web_preview", "master_aac"])
def test_an_export_encodes_at_its_presets_crf(tmp_path: Path, name: str) -> None:
    from vedit.presets import encode_args

    args = encode_args(get_preset(name), hw=False, crf=None)
    assert args[args.index("-crf") + 1] == str(get_preset(name).crf)
    preset_crf = str(get_preset(name).crf)
    assert preset_crf != "18"
    source = clip(tmp_path / "in.mp4")
    brief = Brief(
        name="crf",
        sources={"main": str(source)},
        ops=[{"op": "export", "preset": name}],
        canvas=Canvas(width=320, height=180),
        work_dir=str(tmp_path / "work"),
        hw=False,
    )
    compiler = compile_brief(brief)
    execute(compiler.pipeline, progress=False)
    verify = json.loads(compiler.finals[0].with_suffix(compiler.finals[0].suffix + ".verify.json").read_text())
    assert verify["has_video"] is True


# ---------------------------------------------------------------------------
# 4. A sound-only input in a concat ends when its sound ends
# ---------------------------------------------------------------------------


def test_concat_with_a_sound_only_input_finishes_at_the_right_length(tmp_path: Path) -> None:
    video = clip(tmp_path / "v.mp4", seconds=1.0)
    audio = sound(tmp_path / "a.m4a", seconds=0.8)
    brief = Brief(
        name="mix",
        sources={"v": str(video), "a": str(audio)},
        ops=[{"op": "concat", "sources": ["v", "a"]}, {"op": "export", "preset": "web_preview"}],
        canvas=Canvas(width=320, height=180),
        work_dir=str(tmp_path / "work"),
        hw=False,
    )
    compiler = compile_brief(brief)
    concat = next(step for step in compiler.pipeline.steps if step.name == "concat")
    graph = concat.argv[concat.argv.index("-filter_complex") + 1]
    assert "color=c=" in graph and ":d=0.8" in graph
    try:
        result = subprocess.run(concat.argv, capture_output=True, timeout=120, check=False)
    except subprocess.TimeoutExpired:
        pytest.fail("concat never reached the end of the sound-only input")
    assert result.returncode == 0, result.stderr.decode(errors="replace")[-800:]
    joined = probe(concat.outputs[0])
    assert joined.has_video and joined.has_audio
    assert abs(joined.duration - 1.8) < 0.15


# ---------------------------------------------------------------------------
# 5. An odd source height is made even before libx264 sees the tape
# ---------------------------------------------------------------------------


def test_the_tape_is_even_on_both_sides() -> None:
    assert "trunc(ih/2)*2" in SQUARE_TAPE and "trunc(iw*sar/2)*2" in SQUARE_TAPE


def test_an_odd_height_source_renders_through_select_ranges(tmp_path: Path) -> None:
    source = clip(tmp_path / "odd.mp4", size="320x179", pix_fmt="yuv444p")
    assert (probe(source).width, probe(source).height) == (320, 179)
    brief = Brief(
        name="odd",
        sources={"main": str(source)},
        ops=[
            {"op": "select_ranges", "ranges": [{"start": 0.0, "end": 0.5}]},
            {"op": "export", "preset": "web_preview"},
        ],
        canvas=Canvas(width=320, height=180),
        work_dir=str(tmp_path / "work"),
        hw=False,
    )
    compiler = compile_brief(brief)
    assert compiler.source_size == (320, 178)
    execute(compiler.pipeline, progress=False)
    tape = probe(next(step for step in compiler.pipeline.steps if step.name == "select_ranges").outputs[0])
    assert (tape.width, tape.height) == compiler.source_size


# ---------------------------------------------------------------------------
# 6. trim follows the streams its source has
# ---------------------------------------------------------------------------


def test_trim_of_a_sound_only_source_has_no_picture_and_says_so(tmp_path: Path) -> None:
    audio = sound(tmp_path / "talk.m4a", seconds=1.0)
    brief = Brief(
        name="trim",
        sources={"main": str(audio)},
        ops=[{"op": "trim", "start": 0.1, "end": 0.6}, {"op": "export", "preset": "podcast_audio"}],
        work_dir=str(tmp_path / "work"),
        hw=False,
    )
    compiler = compile_brief(brief)
    trim = next(step for step in compiler.pipeline.steps if step.name == "trim")
    assert "-vn" in trim.argv and "-vf" not in trim.argv and "-af" in trim.argv
    assert compiler.has_video is False and compiler.has_audio is True
    execute(compiler.pipeline, progress=False)
    out = probe(compiler.finals[0])
    assert not out.has_video and out.has_audio and abs(out.duration - 0.5) < 0.15


def test_trim_of_a_silent_clip_has_no_sound_and_says_so(tmp_path: Path) -> None:
    silent = clip(tmp_path / "silent.mp4", audio=False)
    brief = Brief(
        name="trim",
        sources={"main": str(silent)},
        ops=[{"op": "trim", "start": 0, "end": 0.5}, {"op": "export", "preset": "web_preview"}],
        canvas=Canvas(width=320, height=180),
        work_dir=str(tmp_path / "work"),
        hw=False,
    )
    compiler = compile_brief(brief)
    trim = next(step for step in compiler.pipeline.steps if step.name == "trim")
    assert "-an" in trim.argv and "-af" not in trim.argv and "-vf" in trim.argv
    assert compiler.has_audio is False
    execute(compiler.pipeline, progress=False)
    out = probe(compiler.finals[0])
    assert out.has_video and not out.has_audio


# ---------------------------------------------------------------------------
# 7. Every web_preview variant stays on libx264 (see test_worldclass)
# 8. The verify step holds the export to what was asked for
# ---------------------------------------------------------------------------


def test_verify_step_carries_the_export_promise(tmp_path: Path) -> None:
    source = clip(tmp_path / "in.mp4", seconds=1.0)
    brief = Brief(
        name="v",
        sources={"main": str(source)},
        ops=[{"op": "trim", "start": 0, "end": 0.6}, {"op": "export", "preset": "web_preview"}],
        canvas=Canvas(width=320, height=180),
        work_dir=str(tmp_path / "work"),
        hw=False,
    )
    compiler = compile_brief(brief)
    verify = next(step for step in compiler.pipeline.steps if step.name.startswith("verify_"))
    argv = verify.argv
    assert argv[argv.index("--width") + 1] == "960" and argv[argv.index("--height") + 1] == "540"
    assert "--audio" in argv and "--no-color" not in argv
    assert abs(float(argv[argv.index("--seconds") + 1]) - 0.6) < 1e-6
    execute(compiler.pipeline, progress=False)
    written = json.loads(Path(verify.outputs[0]).read_text())
    assert written["expected"]["width"] == 960 and written["expected"]["has_audio"] is True


def test_verify_step_skips_size_and_color_where_they_do_not_apply(tmp_path: Path) -> None:
    source = clip(tmp_path / "in.mp4", seconds=1.0)
    brief = Brief(
        name="v",
        sources={"main": str(source)},
        ops=[{"op": "export", "presets": ["podcast_audio", "export_320x180_15_gif"]}],
        canvas=Canvas(width=320, height=180),
        work_dir=str(tmp_path / "work"),
        hw=False,
    )
    compiler = compile_brief(brief)
    verifies = [step.argv for step in compiler.pipeline.steps if step.name.startswith("verify_")]
    audio_only, gif = verifies
    assert "--width" not in audio_only and "--audio" in audio_only
    assert "--no-audio" in gif and "--no-color" in gif and gif[gif.index("--width") + 1] == "320"


def test_apply_verify_fails_a_wrong_size_a_lost_sound_and_a_short_file(tmp_path: Path) -> None:
    source = clip(
        tmp_path / "in.mp4",
        seconds=1.0,
        extra=("-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv"),
    )
    report = tmp_path / "verify.json"
    assert (
        apply_verify(source, report, width=320, height=180, has_audio=True, seconds=1.0, tolerance=0.3)["has_video"]
        is True
    )
    with pytest.raises(AnalysisError, match="320x180 instead of 960x540"):
        apply_verify(source, report, width=960, height=540, has_audio=True, seconds=1.0, tolerance=0.3)
    with pytest.raises(AnalysisError, match="gained a sound track"):
        apply_verify(source, report, width=320, height=180, has_audio=False, seconds=1.0, tolerance=0.3)
    with pytest.raises(AnalysisError, match="runs 1.0 seconds, not the 3.0"):
        apply_verify(source, report, width=320, height=180, has_audio=True, seconds=3.0, tolerance=0.3)
    silent = clip(tmp_path / "silent.mp4", audio=False)
    with pytest.raises(AnalysisError, match="lost its sound"):
        apply_verify(silent, report, has_audio=True)
    assert apply_verify(silent, report, has_audio=False, seconds=1.0, tolerance=0.3)["has_audio"] is False


def test_a_render_whose_export_comes_out_wrong_fails_its_verify_step(tmp_path: Path) -> None:
    source = clip(tmp_path / "in.mp4", seconds=1.0)
    brief = Brief(
        name="v",
        sources={"main": str(source)},
        ops=[{"op": "export", "preset": "web_preview"}],
        canvas=Canvas(width=320, height=180),
        work_dir=str(tmp_path / "work"),
        hw=False,
    )
    compiler = compile_brief(brief)
    verify = next(step for step in compiler.pipeline.steps if step.name.startswith("verify_"))
    # Promise a longer file than the export can be.
    verify.argv[verify.argv.index("--seconds") + 1] = "4.0"
    with pytest.raises(RenderError, match="not the 4.0"):
        execute(compiler.pipeline, progress=False)


# ---------------------------------------------------------------------------
# 9. Full-range, interlaced or HDR sources are converted on every input path
# ---------------------------------------------------------------------------


def full_range(path: Path) -> Path:
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x180:rate=30:duration=1",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000:duration=1",
        "-shortest",
        "-vf",
        "scale=out_range=full:out_color_matrix=bt709,format=yuvj420p",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-color_range",
        "pc",
        "-c:a",
        "aac",
        str(path),
    )
    assert probe(path).video is not None and probe(path).video.color_range == "pc"
    return path


def test_concat_converts_each_input_to_limited_bt709(tmp_path: Path) -> None:
    wide = full_range(tmp_path / "full.mp4")
    plain = clip(tmp_path / "plain.mp4")
    brief = Brief(
        name="c",
        sources={"a": str(wide), "b": str(plain)},
        ops=[{"op": "concat", "sources": ["a", "b"]}, {"op": "export", "preset": "web_preview"}],
        canvas=Canvas(width=320, height=180),
        work_dir=str(tmp_path / "work"),
        hw=False,
    )
    concat = next(step for step in compile_brief(brief).pipeline.steps if step.name == "concat")
    graph = concat.argv[concat.argv.index("-filter_complex") + 1]
    first, second = (part for part in graph.split(";") if part.startswith(("[0:v", "[1:v")))
    assert "in_range=full" in first and "out_range=limited" in first
    assert "in_range=full" not in second


def test_trim_converts_its_source_to_limited_bt709(tmp_path: Path) -> None:
    wide = full_range(tmp_path / "full.mp4")
    brief = Brief(
        name="t",
        sources={"main": str(wide)},
        ops=[{"op": "trim", "start": 0, "end": 0.5}, {"op": "export", "preset": "web_preview"}],
        canvas=Canvas(width=320, height=180),
        work_dir=str(tmp_path / "work"),
        hw=False,
    )
    compiler = compile_brief(brief)
    trim = next(step for step in compiler.pipeline.steps if step.name == "trim")
    assert "in_range=full" in trim.argv[trim.argv.index("-vf") + 1]
    execute(compiler.pipeline, progress=False)
    out = probe(compiler.finals[0]).video
    assert out is not None and out.color_range == "tv"


def test_materialized_filters_convert_the_source_once(tmp_path: Path) -> None:
    wide = full_range(tmp_path / "full.mp4")
    # speed settles before select_ranges, so the source is materialized first.
    brief = Brief(
        name="m",
        sources={"main": str(wide)},
        ops=[
            {"op": "speed", "factor": 2.0},
            {"op": "select_ranges", "ranges": [{"start": 0.0, "end": 0.4}]},
            {"op": "export", "preset": "web_preview"},
        ],
        canvas=Canvas(width=320, height=180),
        work_dir=str(tmp_path / "work"),
        hw=False,
    )
    compiler = compile_brief(brief)
    pre = next(step for step in compiler.pipeline.steps if step.name == "pre_select_ranges")
    chain = pre.argv[pre.argv.index("-vf") + 1]
    assert "in_range=full" in chain and chain.index("in_range=full") < chain.index("setpts=")
    ranges = next(step for step in compiler.pipeline.steps if step.name == "select_ranges")
    graph = ranges.argv[ranges.argv.index("-filter_complex") + 1]
    assert "in_range=full" not in graph, "the intermediate is already limited range; converting again would crush it"
    execute(compiler.pipeline, progress=False)
    out = probe(compiler.finals[0]).video
    assert out is not None and out.color_range == "tv"
