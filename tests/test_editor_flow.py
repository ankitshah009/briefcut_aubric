"""Editor and agent flows a person actually runs. No mocked session."""

from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path
from urllib.parse import quote

import pytest

from vedit.agent.loop import run_session
from vedit.analyze import apply_verify, detect_silence
from vedit.compile import compile_brief
from vedit.ir import Brief, Canvas
from vedit.media import probe
from vedit.pipeline import execute
from vedit.presets import get_preset

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "src" / "vedit" / "studio" / "static" / "index.html"
YOUTUBE = "Cut the pauses and make a YouTube video"
KEEP = "Keep the pauses and make a YouTube video"
SHORT = "Turn this into a vertical short"
_EDITOR_IDS = (
    "goal",
    "scrub-track",
    "scrub-moments",
    "transcript-panel",
    "transcript-list",
    "analysis-badge",
    "caption-provider",
    "run-facts",
    "review",
    "director-notes",
    "shortcuts",
    "clip",
    "drop",
    "decisions",
    "say-pauses",
    "say-shape",
    "say-captions",
    "say-clip",
    "program",
    "program-list",
    "player",
    "proof",
    "make",
    "plan",
    "result",
)


def _load(name: str, filename: str):
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _gap_clip(tmp_path: Path) -> Path:
    """0.8s tone + 1.2s digital silence + 0.8s tone, via the e2e helpers."""
    e2e = _load("vedit_e2e_helpers", "test_e2e.py")
    speech = tmp_path / "parts"
    speech.mkdir(parents=True)
    e2e._make_clip(speech / "t1.mp4", seconds=0.8, freq=440, color="navy")
    e2e.run_ffmpeg(
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
    e2e._make_clip(speech / "t2.mp4", seconds=0.8, freq=660, color="maroon")
    joined = tmp_path / "talk.mp4"
    brief = Brief(
        name="sil",
        sources={
            "a": str(speech / "t1.mp4"),
            "b": str(speech / "sil.mp4"),
            "c": str(speech / "t2.mp4"),
        },
        ops=[
            {"op": "concat", "sources": ["a", "b", "c"]},
            {"op": "export", "path": str(joined), "preset": "web_preview"},
        ],
        canvas=Canvas(width=640, height=360),
        work_dir=str(tmp_path / "build_talk"),
    )
    compiler = compile_brief(brief)
    execute(compiler.pipeline)
    gaps = detect_silence(joined, threshold_db=-34, min_silence=0.45)
    assert gaps, "silence analysis found no removable gap"
    return joined


def _multipart(goal: str, clip: Path, *, render: bool, root: Path) -> tuple[bytes, str]:
    boundary = "veditboundary"
    file_bytes = clip.read_bytes()
    body = b"".join(
        [
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="goal"\r\n\r\n'
                f"{goal}\r\n"
            ).encode(),
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="render"\r\n\r\n'
                f"{'true' if render else 'false'}\r\n"
            ).encode(),
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="analyze"\r\n\r\n'
                f"true\r\n"
            ).encode(),
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="root"\r\n\r\n'
                f"{root}\r\n"
            ).encode(),
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="clip"; filename="{clip.name}"\r\n'
                f"Content-Type: video/mp4\r\n\r\n"
            ).encode()
            + file_bytes
            + b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    return body, f"multipart/form-data; boundary={boundary}"


def test_verify_reports_the_picture_without_waiting_on_vmaf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file a person just cut is proved by streams and duration, not a VMAF pass."""
    monkeypatch.delenv("VEDIT_VMAF", raising=False)
    e2e = _load("vedit_e2e_helpers_verify", "test_e2e.py")
    clip = tmp_path / "clip.mp4"
    e2e._make_clip(clip, seconds=0.8, freq=440, color="navy")
    report = tmp_path / "clip.verify.json"
    started = time.monotonic()
    payload = apply_verify(clip, report, ref=clip)
    elapsed = time.monotonic() - started
    assert elapsed < 15, f"verify took {elapsed:.1f}s"
    written = json.loads(report.read_text(encoding="utf-8"))
    assert written["has_video"] is True
    assert written["has_audio"] is True
    assert int(written["width"]) > 0
    assert int(written["height"]) > 0
    assert float(written["duration"]) > 0.5
    assert payload["has_video"] is True
    assert "vmaf_log" not in written
    assert not report.with_suffix(".vmaf.json").is_file()


def test_editor_page_has_no_path_field() -> None:
    page = INDEX.read_text(encoding="utf-8")
    assert "absolute path" not in page.lower()
    assert 'id="paths"' not in page
    for ident in _EDITOR_IDS:
        assert f'id="{ident}"' in page
    studio = _load("vedit_studio_package_tests", "test_studio_package.py")
    with studio._studio_server() as port:
        status, raw = studio._exchange("GET", port, "/")
    assert status == 200
    body = raw.decode("utf-8")
    assert "absolute path" not in body.lower()
    assert 'id="clip"' in body
    assert 'id="player"' in body
    assert 'id="decisions"' in body
    assert 'id="make"' in body


def test_sentences_change_the_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vedit.agent.loop.local_transcriber_ready", lambda: False)
    clip = _gap_clip(tmp_path)
    sessions = {
        key: run_session(sentence, [str(clip)], root=tmp_path / key, render=False, analyze=True)
        for key, sentence in (("youtube", YOUTUBE), ("keep", KEEP), ("short", SHORT))
    }
    youtube = sessions["youtube"].decisions or {}
    keep = sessions["keep"].decisions or {}
    short = sessions["short"].decisions or {}
    assert sessions["youtube"].status == "planned"
    assert youtube["pauses"] == "cut"
    assert keep["pauses"] == "kept"
    assert youtube["shape"] == "16:9"
    assert short["shape"] == "9:16"
    assert youtube["pauses_sentence"] != keep["pauses_sentence"]
    assert youtube["shape_sentence"] != short["shape_sentence"]
    assert youtube["captions"] == "skipped"
    assert short["captions"] == "skipped"
    assert str(youtube["captions_sentence"]).startswith("Captions were not burned.")
    assert str(short["captions_sentence"]).startswith("Captions were not burned.")
    assert youtube["captions_sentence"] != short["captions_sentence"]
    assert youtube["clip"] == clip.name
    for decisions in (youtube, keep, short):
        program = decisions["program"]
        assert isinstance(program, list) and program
        assert program[-1]["op"] == "export"
        assert all(op.get("op") != "captions" for op in program)
    assert sessions["youtube"].primary_output is None
    assert "Pauses will be cut." in (tmp_path / "youtube" / "decisions.md").read_text(encoding="utf-8")
    assert "Pauses will be kept." in (tmp_path / "keep" / "decisions.md").read_text(encoding="utf-8")


def test_editor_plan_and_render(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vedit.agent.loop.local_transcriber_ready", lambda: False)
    clip = _gap_clip(tmp_path / "src")
    source = probe(clip)
    studio = _load("vedit_studio_package_tests", "test_studio_package.py")
    plan_root = tmp_path / "plan"
    render_root = tmp_path / "render"
    plan_body, plan_type = _multipart(YOUTUBE, clip, render=False, root=plan_root)
    render_body, render_type = _multipart(YOUTUBE, clip, render=True, root=render_root)
    with studio._studio_server() as port:
        status, raw = studio._exchange(
            "POST",
            port,
            "/api/run",
            plan_body,
            timeout=60,
            content_type=plan_type,
        )
        assert status == 200
        planned = json.loads(raw.decode())["session"]
        status, raw = studio._exchange(
            "POST",
            port,
            "/api/run",
            render_body,
            timeout=300,
            content_type=render_type,
        )
        assert status == 200
        rendered = json.loads(raw.decode())["session"]
        media_root = rendered["root"]
        status, media = studio._exchange(
            "GET",
            port,
            "/api/media?root=" + quote(media_root, safe=""),
            timeout=30,
        )
    decisions = planned["decisions"]
    assert "Pauses will be cut." in decisions["pauses_sentence"]
    assert "16:9" in decisions["shape_sentence"]
    assert decisions["captions_sentence"].startswith("Captions were not burned.")
    assert decisions["program"][-1]["op"] == "export"
    assert rendered["primary_output"]
    assert rendered["captions_skipped"] is True
    proof = rendered["proof"]
    canvas = get_preset("youtube_1080p").canvas
    assert proof["width"] == canvas.width
    assert proof["height"] == canvas.height
    assert proof["has_video"] is True
    assert proof["has_audio"] is True
    assert proof["duration"] < source.duration
    assert status == 200
    assert media[:8] == b"\x00\x00\x00\x18ftyp" or media[4:8] == b"ftyp" or media.startswith(b"\x00\x00\x00")
    assert len(media) > 1000
    played = probe(rendered["primary_output"])
    assert played.width == canvas.width
    assert played.height == canvas.height
    assert played.has_video and played.has_audio


def test_one_upload_supports_replanning_and_verified_render(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    e2e = _load("vedit_upload_e2e", "test_e2e.py")
    clip = tmp_path / "original.mp4"
    e2e._make_clip(clip, seconds=0.8, freq=440, color="navy")
    studio = _load("vedit_upload_studio", "test_studio_package.py")
    sessions = []
    with studio._studio_server() as port:
        status, raw = studio._exchange("POST", port, "/api/upload?name=clip.mp4", clip.read_bytes())
        assert status == 201
        paths = json.loads(raw)["paths"]
        for goal, render in [(KEEP, False), (YOUTUBE, False), (KEEP, True)]:
            status, raw = studio._exchange("POST", port, "/api/run", {
                "goal": goal, "paths": paths, "render": render, "analyze": False,
            }, timeout=90)
            assert status == 200
            session = json.loads(raw)["session"]
            assert session["status"] == ("verified" if render else "planned")
            sessions.append(session)
        assert len({session["root"] for session in sessions}) == 3
        assert len(list((tmp_path / "vedit-sessions").glob("*/inbox/*"))) == 1
        rendered = sessions[-1]
        status, media = studio._exchange("GET", port, "/api/media?root=" + quote(rendered["root"], safe=""))
        assert status == 200
        assert media == Path(rendered["primary_output"]).read_bytes()
    report = json.loads((Path(rendered["work_dir"]) / "report.json").read_text())
    assert all(step["ok"] for step in report["steps"])
    assert report["outputs"][0]["width"] == 1920
    assert report["outputs"][0]["height"] == 1080
    assert report["outputs"][0]["has_audio"] is True
    assert report["outputs"][0]["has_video"] is True
    output = probe(rendered["primary_output"])
    assert output.duration > 0
    assert (output.width, output.height) == (1920, 1080)
    assert output.has_audio and output.has_video
    assert output.video.color_space == "bt709"
