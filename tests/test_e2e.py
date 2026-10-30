from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from vedit.compile import compile_brief
from vedit.edit import Edit
from vedit.errors import CompileError
from vedit.ir import Brief, Canvas
from vedit.media import has_filter, probe, resolve_ffmpeg, resolve_font, run_ffmpeg
from vedit.pipeline import execute, write_report


def _have_ffmpeg() -> bool:
    try:
        resolve_ffmpeg()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg required")


def _make_clip(path: Path, *, freq: int = 440, seconds: float = 2.0, color: str = "blue") -> Path:
    # 640x360 test pattern + sine. Cheap and deterministic.
    run_ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=640x360:r=30:d={seconds}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={freq}:sample_rate=48000:duration={seconds}",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(path),
        ]
    )
    return path


def _have_drawtext() -> bool:
    try:
        return has_filter("drawtext")
    except Exception:
        return False


def _have_font() -> bool:
    try:
        resolve_font()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _have_drawtext(), reason="drawtext filter missing")
@pytest.mark.skipif(not _have_font(), reason="no usable title font")
def test_trim_title_export(tmp_path: Path):
    src = _make_clip(tmp_path / "in.mp4", seconds=3.0)
    dest = tmp_path / "out.mp4"
    result = (
        Edit(src, name="e2e")
        .work_dir(tmp_path / "work")
        .trim(0.2, 2.2)
        .title("VEDIT")
        .fade_in(0.15)
        .to(dest)
    )
    assert result.is_file()
    info = probe(result)
    assert info.has_video
    assert info.has_audio
    assert 1.6 < info.duration < 2.4
    assert info.width == 1920
    assert info.height == 1080


def test_concat_two_clips(tmp_path: Path):
    a = _make_clip(tmp_path / "a.mp4", freq=330, seconds=1.0, color="red")
    b = _make_clip(tmp_path / "b.mp4", freq=550, seconds=1.0, color="green")
    brief = Brief(
        name="join",
        sources={"a": str(a), "b": str(b)},
        ops=[{"op": "concat", "sources": ["a", "b"]}, {"op": "export", "preset": "web_preview"}],
        canvas=Canvas(width=640, height=360, fps=30),
        work_dir=str(tmp_path / "join"),
    )
    compiler = compile_brief(brief)
    reports = execute(compiler.pipeline)
    write_report(compiler.pipeline, reports, finals=compiler.finals)
    assert compiler.finals
    info = probe(compiler.finals[0])
    assert 1.7 < info.duration < 2.4


def test_silence_cut_keeps_speech(tmp_path: Path):
    # 0.8s tone + 1.2s digital silence + 0.8s tone.
    speech = tmp_path / "parts"
    speech.mkdir()
    _make_clip(speech / "t1.mp4", seconds=0.8, freq=440, color="navy")
    run_ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=640x360:r=30:d=1.2",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-t",
            "1.2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(speech / "sil.mp4"),
        ]
    )
    _make_clip(speech / "t2.mp4", seconds=0.8, freq=660, color="maroon")
    joined = tmp_path / "talk.mp4"
    brief = Brief(
        name="sil",
        sources={"a": str(speech / "t1.mp4"), "b": str(speech / "sil.mp4"), "c": str(speech / "t2.mp4")},
        ops=[{"op": "concat", "sources": ["a", "b", "c"]}, {"op": "export", "path": str(joined), "preset": "web_preview"}],
        canvas=Canvas(width=640, height=360),
        work_dir=str(tmp_path / "build_talk"),
    )
    compiler = compile_brief(brief)
    execute(compiler.pipeline)
    cut_brief = Brief(
        name="cut",
        sources={"main": str(joined)},
        ops=[
            {"op": "cut_silence", "threshold_db": -40, "min_silence": 0.4, "pad": 0.05},
            {"op": "export", "preset": "web_preview"},
        ],
        canvas=Canvas(width=640, height=360),
        work_dir=str(tmp_path / "cut"),
    )
    cut = compile_brief(cut_brief)
    execute(cut.pipeline)
    info = probe(cut.finals[0])
    # Original ~2.8s with 1.2s silence. After cut should be closer to ~1.6–2.1s.
    assert info.duration < 2.4
    assert info.duration > 1.2


def test_missing_source_fails_at_compile(tmp_path: Path):
    brief = Brief(
        name="ghost",
        sources={"main": str(tmp_path / "nope.mp4")},
        ops=[{"op": "trim", "start": 0, "end": 1}],
        work_dir=str(tmp_path / "w"),
    )
    with pytest.raises(CompileError, match="missing source"):
        compile_brief(brief)


def test_plan_cut_silence_then_captions(tmp_path: Path):
    src = _make_clip(tmp_path / "in.mp4", seconds=1.5)
    brief = Brief(
        name="cap",
        sources={"main": str(src)},
        ops=[
            {"op": "cut_silence", "threshold_db": -40, "min_silence": 0.4, "pad": 0.05},
            {"op": "captions", "burn": True},
            {"op": "export", "preset": "web_preview"},
        ],
        canvas=Canvas(width=640, height=360),
        work_dir=str(tmp_path / "w"),
    )
    compiler = compile_brief(brief)
    from vedit.pipeline import write_codegen

    write_codegen(compiler.pipeline)
    script = (compiler.work_dir / "render.sh").read_text()
    assert (compiler.work_dir / "render.sh").is_file()
    assert any(step.name == "cut_silence" for step in compiler.pipeline.steps)
    assert any(step.name == "captions_transcribe" for step in compiler.pipeline.steps)
    assert not (compiler.work_dir / "speech.wav").exists()
    assert not (compiler.work_dir / "keep_ranges.json").exists()
    assert not (compiler.work_dir / "captions.srt").exists()
    assert not (compiler.work_dir / "captions.ass").exists()
    assert "vedit.captions" in script
    assert "vedit.analyze" in script
    assert not (compiler.work_dir / "01_cut_silence.mp4").exists()
    cap = next(s for s in compiler.pipeline.steps if s.name == "captions_transcribe")
    assert any(str(compiler.work_dir / "01_cut_silence.mp4") == a for a in cap.argv)
    from vedit.pipeline import outputs_complete

    assert not outputs_complete(cap)


def test_trim_then_cut_silence_plans(tmp_path: Path):
    src = _make_clip(tmp_path / "in.mp4", seconds=2.0)
    brief = Brief(
        name="seq",
        sources={"main": str(src)},
        ops=[
            {"op": "trim", "start": 0.1, "end": 1.8},
            {"op": "cut_silence", "threshold_db": -40, "min_silence": 0.4, "pad": 0.05},
            {"op": "export", "preset": "web_preview"},
        ],
        canvas=Canvas(width=640, height=360),
        work_dir=str(tmp_path / "w"),
    )
    compiler = compile_brief(brief)
    names = [s.name for s in compiler.pipeline.steps]
    assert "trim" in names
    assert "cut_silence" in names


def test_youtube_style_plan_is_two_encodes(tmp_path: Path):
    src = _make_clip(tmp_path / "in.mp4", seconds=2.0)
    brief = Brief(
        name="yt",
        sources={"main": str(src)},
        ops=[
            {"op": "cut_silence", "threshold_db": -40, "min_silence": 0.4, "pad": 0.05},
            {"op": "captions", "burn": True},
            {"op": "loudnorm", "i": -14},
            {"op": "fade_in"},
            {"op": "fade_out"},
            {"op": "thumbnail", "at": 0.4},
            {"op": "export", "preset": "web_preview"},
        ],
        canvas=Canvas(width=640, height=360),
        work_dir=str(tmp_path / "w"),
    )
    compiler = compile_brief(brief)
    names = [s.name for s in compiler.pipeline.steps]
    assert "cut_silence" in names
    assert "captions_transcribe" in names
    assert "export_web_preview" in names
    assert "thumbnail" in names
    assert not any(n.startswith("pre_") for n in names)
    encodes = [n for n in names if n in {"cut_silence", "export_web_preview"}]
    assert encodes == ["cut_silence", "export_web_preview"]


def test_execute_resumes_existing_plan(tmp_path: Path):
    from vedit.pipeline import execute, load_pipeline, write_codegen

    src = _make_clip(tmp_path / "in.mp4", seconds=1.0)
    dest_dir = tmp_path / "work"
    Edit(src).work_dir(dest_dir).trim(0, 0.5).export("web_preview").plan()
    pipe = load_pipeline(dest_dir)
    assert pipe.steps
    first = execute(pipe)
    assert all(r.ok for r in first)
    second = execute(pipe, resume=True)
    assert all(r.ok for r in second)
    assert any("skipped" in (r.stderr_tail or "") for r in second)


def test_cli_renders_existing_work_dir(tmp_path: Path):
    from vedit.cli import main

    src = _make_clip(tmp_path / "in.mp4", seconds=1.0)
    work = tmp_path / "work"
    Edit(src).work_dir(work).trim(0, 0.4).export("web_preview").plan()
    assert (work / "project.json").is_file()
    assert (work / "timeline.otio.json").is_file()
    assert main(["render", str(work)]) == 0
    finals = list(work.glob("*.mp4"))
    assert finals
    assert probe(finals[0]).duration > 0.2


def test_codegen_is_executable(tmp_path: Path):
    src = _make_clip(tmp_path / "in.mp4", seconds=1.0)
    dest_dir = tmp_path / "work"
    Edit(src).work_dir(dest_dir).trim(0, 0.5).plan()
    assert (dest_dir / "render.sh").is_file()
    assert (dest_dir / "project.json").is_file()
    assert (dest_dir / "brief.yaml").is_file()
    assert (dest_dir / "timeline.otio.json").is_file()
    assert shutil.which("bash")
    # render.sh should mention ffmpeg
    text = (dest_dir / "render.sh").read_text()
    assert "ffmpeg" in text
    assert "set -euo pipefail" in text
