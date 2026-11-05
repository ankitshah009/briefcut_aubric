"""The fluent Edit API: what each call records, and what plan() and to() write without rendering."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from vedit.compile import OPS
from vedit.edit import Edit
from vedit.ir import Canvas
from vedit.media import resolve_ffmpeg


def _have_ffmpeg() -> bool:
    try:
        resolve_ffmpeg()
        return True
    except Exception:
        return False


render = pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg required")


def _clip(path: Path) -> Path:
    subprocess.run(
        [
            resolve_ffmpeg(),
            "-hide_banner",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=320x180:r=30",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            "1",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(path),
        ],
        check=True,
    )
    return path


def test_the_source_names_the_edit_unless_a_name_is_given() -> None:
    assert Edit("~/clips/My Talk.mp4").brief().name == "My Talk"
    assert Edit("~/clips/My Talk.mp4", name="final").brief().name == "final"
    assert Edit().brief().name == "edit" and Edit().brief().sources == {}
    brief = Edit("~/clips/My Talk.mp4").brief()
    assert brief.sources["main"] == str(Path("~/clips/My Talk.mp4").expanduser().resolve())


def test_every_recorder_writes_a_known_op_with_only_the_values_given() -> None:
    edit = (
        Edit("talk.mp4")
        .trim(1.0, end=None, duration=2.5)
        .cut_silence(threshold_db=-30)
        .concat("a", "b", transition="fade")
        .speed(1.5)
        .reverse()
        .freeze(1.0)
        .fit()
        .fill()
        .crop(100, 50)
        .scale(640, 360)
        .pad(640, 480, color="white")
        .fps(24)
        .blur()
        .lowpass()
        .extract_audio()
        .rotate()
        .hflip()
        .vflip()
        .color(saturation=1.2)
        .denoise()
        .sharpen()
        .fade_in()
        .fade_out()
        .volume(db=-3)
        .highpass()
        .loudnorm(i=-16)
        .title("Hi", size=40)
        .captions(language="es", style="boxed")
        .overlay("logo", x=10)
        .watermark("logo")
        .pip("cam")
        .look("film", strength=0.5)
        .zoom([{"at": 0, "scale": 1.2}])
        .stabilize()
        .voice(enhance=False)
        .text("lower", y=0.9)
        .replace_audio("bed")
        .reframe()
        .spatial_effect("float", intensity=0.3)
        .slideshow(["a", "b"], duration=2)
        .thumbnail(0.5)
        .chromakey()
        .scenes()
        .export("web_preview")
    )
    ops = edit.brief().ops
    names = [op["op"] for op in ops]
    unknown = [name for name in names if name not in OPS]
    assert unknown == [], f"Edit records ops the compiler does not know: {unknown}"
    by_name = {op["op"]: op for op in ops}
    assert by_name["trim"] == {"op": "trim", "start": 1.0, "duration": 2.5}, "None values are left out"
    assert by_name["cut_silence"] == {"op": "cut_silence", "threshold_db": -30, "min_silence": 0.45, "pad": 0.12}
    assert by_name["concat"] == {
        "op": "concat",
        "sources": ["a", "b"],
        "transition": "fade",
        "transition_duration": 0.5,
    }
    assert by_name["crop"] == {"op": "crop", "width": 100, "height": 50}
    assert by_name["pad"] == {"op": "pad", "width": 640, "height": 480, "color": "white"}
    assert by_name["color"] == {"op": "color", "saturation": 1.2}
    assert by_name["volume"] == {"op": "volume", "db": -3}
    assert by_name["loudnorm"] == {"op": "loudnorm", "i": -16, "tp": -1.5, "lra": 11.0}
    assert by_name["title"] == {"op": "title", "text": "Hi", "size": 40}
    assert by_name["captions"] == {"op": "captions", "burn": True, "language": "es", "style": "boxed"}
    assert by_name["voice"] == {"op": "voice", "denoise": True, "enhance": False}
    assert by_name["reframe"] == {"op": "reframe", "aspect": "9:16", "mode": "fill"}
    assert by_name["spatial_effect"] == {"op": "spatial_effect", "preset": "float", "intensity": 0.3, "start": 0.0}
    assert by_name["slideshow"] == {"op": "slideshow", "images": ["a", "b"], "duration": 2, "fade": 0.4}
    assert by_name["export"] == {"op": "export", "presets": ["web_preview"]}
    assert names[-1] == "export"


def test_music_and_logo_register_their_sources_under_fixed_names() -> None:
    brief = Edit("talk.mp4").music("~/bed.mp3", volume_db=-20, start=2).logo("brand.png", scale=0.1).brief()
    assert brief.sources["music"] == str(Path("~/bed.mp3").expanduser().resolve())
    assert brief.sources["logo"].endswith("brand.png")
    assert brief.ops[0] == {"op": "music", "source": "music", "duck": True, "volume_db": -20, "start": 2}
    assert brief.ops[1] == {"op": "logo", "source": "logo", "scale": 0.1}
    named = Edit("talk.mp4").source("cam", "cam.mov").brief()
    assert named.sources["cam"].endswith("cam.mov")


def test_canvas_hw_work_dir_and_presets_land_in_the_brief(tmp_path: Path) -> None:
    edit = (
        Edit("talk.mp4")
        .canvas(1080, 1920, fps=60)
        .hw(False)
        .work_dir(tmp_path / "w")
        .export("shorts_9x16", "web_preview")
    )
    brief = edit.brief()
    assert brief.canvas == Canvas(width=1080, height=1920, fps=60)
    assert brief.hw is False and brief.crf is None
    assert brief.work_dir == str(tmp_path / "w") and brief.output_dir == str(tmp_path / "w")
    assert brief.presets == ["shorts_9x16", "web_preview"]
    bare = Edit("talk.mp4").export().brief()
    assert bare.ops[-1] == {"op": "export"} and bare.presets == []
    assert bare.output_dir == str(Path("out") / "talk"), "with no work dir the output goes under out/<name>"


@render
def test_plan_writes_the_program_and_the_brief_without_rendering(tmp_path: Path) -> None:
    clip = _clip(tmp_path / "talk.mp4")
    pipeline = Edit(clip).trim(0, 0.5).export("web_preview").plan(tmp_path / "work")
    assert pipeline.steps and [step.name for step in pipeline.steps]
    work = tmp_path / "work"
    assert json.loads((work / "project.json").read_text(encoding="utf-8"))
    assert (work / "render.sh").is_file()
    saved = (work / "brief.yaml").read_text(encoding="utf-8")
    assert "web_preview" in saved and str(clip.resolve()) in saved
    assert not list(work.glob("*.mp4")), "plan renders nothing"


@render
def test_to_is_its_own_export_and_leaves_the_tape_as_it_was(tmp_path: Path) -> None:
    clip = _clip(tmp_path / "talk.mp4")
    edit = Edit(clip, name="cut").trim(0, 0.5)
    before = [dict(op) for op in edit.brief().ops]
    dest = tmp_path / "delivered" / "final.mp4"
    dest.parent.mkdir()
    out = edit.to(dest, preset="web_preview", dry_run=True)
    assert Path(out).name == "final.mp4"
    assert edit.brief().ops == before, "the export to() added is gone again"
    assert edit.brief().work_dir is None, "the work dir to() derived is not kept"
    derived = dest.parent / ".vedit-cut"
    assert (derived / "brief.yaml").is_file(), "the work dir sits next to the delivery"
    assert "final.mp4" in (derived / "brief.yaml").read_text(encoding="utf-8")
    assert not dest.exists(), "a dry run delivers nothing"


@render
def test_render_dry_run_writes_the_report_but_no_media(tmp_path: Path) -> None:
    clip = _clip(tmp_path / "talk.mp4")
    finals = Edit(clip).work_dir(tmp_path / "work").trim(0, 0.5).export("web_preview").render(dry_run=True)
    assert finals and all(Path(final).suffix == ".mp4" for final in finals)
    assert not any(Path(final).exists() for final in finals)
    report = json.loads((tmp_path / "work" / "report.json").read_text(encoding="utf-8"))
    assert report
