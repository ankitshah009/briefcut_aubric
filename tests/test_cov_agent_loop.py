"""One editorial session end to end on a real clip: plan, render, judge, revise, and the files it leaves."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from vedit.agent.loop import load_session, run_session, save_session
from vedit.agent.model import Critique, Finding
from vedit.errors import ProbeError, RenderError
from vedit.media import probe, resolve_ffmpeg


def _have_ffmpeg() -> bool:
    try:
        resolve_ffmpeg()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg required")


def _clip(path: Path, *, seconds: float = 1.0) -> Path:
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
            str(seconds),
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


def test_a_missing_media_path_is_a_probe_error_before_any_session_is_written(tmp_path: Path) -> None:
    root = tmp_path / "s"
    with pytest.raises(ProbeError, match="file does not exist"):
        run_session("preview", [str(tmp_path / "gone.mp4")], root=root, render=False, analyze=False)
    assert not (root / "session.json").exists()


def test_a_preview_renders_verifies_and_proves_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vedit.agent.loop.transcriber_ready", lambda: False)
    clip = _clip(tmp_path / "talk.mp4")
    root = tmp_path / "session"
    session = run_session(f"quick preview of {clip}", [], root=root, analyze=False, hw=False)
    assert session.status == "verified", session.critique
    assert session.revision == 0 and session.critique is not None and session.critique.ok
    assert session.primary_output and Path(session.primary_output).is_file()
    info = probe(session.primary_output)
    assert (info.width, info.height) == (960, 540) and info.has_audio
    assert session.proof == {
        "path": str(info.path),
        "duration": info.duration,
        "width": 960,
        "height": 540,
        "fps": info.fps,
        "has_audio": True,
        "has_video": True,
    }
    assert session.captions_skipped is False, "a preview never asked for captions"
    assert session.decision is not None and session.decision.brief.hw is False
    saved = json.loads((root / "session.json").read_text(encoding="utf-8"))
    assert saved["status"] == "verified" and saved["proof"]["width"] == 960
    assert (root / "work" / "report.json").is_file()
    loaded = load_session(root)
    assert loaded.primary_output == session.primary_output and loaded.proof == session.proof
    assert loaded.decisions == session.decisions and loaded.critique == session.critique


def test_captions_are_dropped_and_explained_when_meta_cannot_transcribe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("vedit.agent.loop.transcriber_ready", lambda: False)
    clip = _clip(tmp_path / "talk.mp4")
    root = tmp_path / "session"
    session = run_session("youtube talk with captions", [str(clip)], root=root, render=False, analyze=False)
    assert session.status == "planned" and session.intent.captions is True
    assert session.captions_skipped is True
    assert session.decision is not None
    assert "captions" not in [op["op"] for op in session.decision.brief.ops]
    assert session.decisions is not None and session.decisions["captions"] == "skipped"
    decisions = (root / "decisions.md").read_text(encoding="utf-8")
    assert decisions.startswith("Pauses will be") and "Captions were not burned" in decisions
    assert (root / "inventory.json").is_file() and (root / "intent.json").is_file()


def test_a_render_failure_is_a_failed_session_with_the_error_in_the_critique(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vedit.agent.loop as loop

    monkeypatch.setattr("vedit.agent.loop.transcriber_ready", lambda: False)

    def explode(pipeline, **kwargs):
        raise RenderError("ffmpeg failed (1): step export")

    monkeypatch.setattr(loop, "execute", explode)
    clip = _clip(tmp_path / "talk.mp4")
    root = tmp_path / "session"
    session = run_session("preview", [str(clip)], root=root, analyze=False)
    assert session.status == "failed" and session.primary_output is None and session.proof is None
    assert session.critique is not None
    (finding,) = session.critique.findings
    assert finding.code == "render_failed" and finding.message == "ffmpeg failed (1): step export"
    assert finding.fix == "read project.json and the step stderr"
    assert load_session(root).status == "failed"


def test_a_failing_critique_revises_once_and_records_the_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vedit.agent.loop as loop

    monkeypatch.setattr("vedit.agent.loop.transcriber_ready", lambda: False)
    verdicts = iter(
        [
            Critique(
                ok=False, findings=(Finding(code="loudness", severity="fail", message="too quiet", fix="loudnorm"),)
            ),
            Critique(ok=True, findings=()),
        ]
    )
    judged: list[Path] = []

    def judge(path, intent, **kwargs):
        judged.append(Path(path))
        return next(verdicts)

    monkeypatch.setattr(loop, "critique_output", judge)
    clip = _clip(tmp_path / "talk.mp4")
    root = tmp_path / "session"
    session = run_session("preview", [str(clip)], root=root, analyze=False, max_revisions=2, hw=False)
    assert session.status == "verified" and session.revision == 1
    assert len(judged) == 2 and judged[1].parent == root / "rev1"
    assert session.decision is not None
    assert "loudnorm" in [op["op"] for op in session.decision.brief.ops], "the revision applied the fix"
    assert session.decision.brief.hw is False, "the hardware choice survives a revision"
    assert session.decision.rationale[-1] == "added loudnorm"
    notes = (root / "decisions.md").read_text(encoding="utf-8")
    assert notes.rstrip().endswith("added loudnorm")
    assert (root / "rev1" / "report.json").is_file() and (root / "brief.yaml").is_file()
    assert "loudnorm" in (root / "brief.yaml").read_text(encoding="utf-8"), "the saved brief is the revised one"


def test_a_session_with_no_root_is_allocated_under_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    clip = _clip(tmp_path / "talk.mp4")
    session = run_session("Preview, please!", [str(clip)], render=False, analyze=False)
    assert session.root.parent == tmp_path / "vedit-sessions"
    assert session.root.name.startswith("preview-please-")
    assert (session.root / "session.json").is_file()


def test_save_session_rewrites_the_files_a_session_is_made_of(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    session = run_session("edit the board video", [], root=root, render=False, analyze=False)
    assert session.decision is None
    (root / "session.json").unlink()
    (root / "intent.json").unlink()
    dest = save_session(session)
    assert dest == root / "session.json" and dest.is_file() and (root / "intent.json").is_file()
    assert not (root / "brief.yaml").exists(), "no decision, no brief"
    reloaded = load_session(root)
    assert reloaded.status == "failed" and reloaded.inventory.notes == ("no media paths",)
