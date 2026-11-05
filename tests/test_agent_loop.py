"""Plan-only agent sessions. render=False; never execute; never analyze."""

from __future__ import annotations

from pathlib import Path

import pytest

from vedit.agent.loop import _session_dir, load_session, run_session
from vedit.compile import OPS
from vedit.ir import load_brief

# The tracked demo clip in this checkout, so the test runs on any machine and in CI.
# A missing file is a skip; a compile error is a failure.
TALK = Path(__file__).resolve().parents[1] / "examples" / "demo" / "talk.mp4"
GOAL = "cut silence and caption this as a youtube video"


def test_automatic_session_directories_are_unique(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from datetime import datetime

    class FrozenDatetime:
        @staticmethod
        def now():
            return datetime(2026, 10, 29, 12, 0, 0)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("vedit.agent.loop.datetime", FrozenDatetime)
    with ThreadPoolExecutor(max_workers=8) as pool:
        roots = list(pool.map(lambda _: _session_dir(None, GOAL), range(32)))
    assert len(set(roots)) == 32
    assert all(root.is_dir() for root in roots)
    explicit = tmp_path / "chosen"
    assert _session_dir(explicit, GOAL) == _session_dir(explicit, GOAL) == explicit


def _talk() -> Path:
    if not TALK.is_file():
        pytest.skip(f"missing demo file: {TALK}")
    return TALK


def test_plan_only_writes_program(tmp_path: Path) -> None:
    talk = _talk()
    root = tmp_path / "session"
    session = run_session(
        GOAL,
        [str(talk)],
        root=root,
        render=False,
        analyze=False,
        max_revisions=0,
    )
    assert session.status == "planned"
    assert (root / "brief.yaml").is_file()
    assert (root / "intent.json").is_file()
    assert (root / "decisions.md").is_file()
    assert (root / "session.json").is_file()
    assert (root / "work" / "project.json").is_file()
    assert (root / "work" / "render.sh").is_file()
    assert session.primary_output is None
    loaded = load_session(root)
    assert loaded.status == session.status
    assert loaded.goal == session.goal
    assert loaded.status == "planned"
    assert loaded.goal == GOAL


def test_no_paths_fails_clean(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    session = run_session(
        "edit the board video",
        [],
        root=root,
        render=False,
        analyze=False,
    )
    assert session.status == "failed"
    assert session.critique is not None
    assert not session.critique.ok
    assert (root / "session.json").is_file()


def test_brief_ops_are_real(tmp_path: Path) -> None:
    talk = _talk()
    root = tmp_path / "session"
    run_session(
        GOAL,
        [str(talk)],
        root=root,
        render=False,
        analyze=False,
        max_revisions=0,
    )
    brief = load_brief(root / "brief.yaml")
    names = [str(op["op"]) for op in brief.ops]
    assert names
    unknown = [name for name in names if name not in OPS]
    assert unknown == []
    assert names[-1] == "export"
    assert "main" in brief.sources
    assert str(brief.sources["main"]).endswith("talk.mp4")


def _pictures(tmp_path: Path) -> list[str]:
    from vedit.media import run_ffmpeg

    try:
        from vedit.media import resolve_ffmpeg

        resolve_ffmpeg()
    except Exception:
        pytest.skip("ffmpeg required")
    made = []
    for name, color in (("a.png", "red"), ("b.png", "blue")):
        path = tmp_path / name
        run_ffmpeg(["-f", "lavfi", "-i", f"color=c={color}:s=640x360:d=0.1", "-frames:v", "1", str(path)])
        made.append(str(path))
    return made


def _judge(monkeypatch: pytest.MonkeyPatch, ok: bool) -> list[dict]:
    from vedit.agent.model import Critique, Finding

    calls: list[dict] = []

    def fake(compiler, intent, *, enforce_captions=True):
        calls.append({"enforce_captions": enforce_captions, "work": compiler.work_dir})
        findings = () if ok else (Finding(code="missing_output", severity="fail", message="none", fix="none"),)
        return Critique(ok=ok, findings=findings), None, None

    monkeypatch.setattr("vedit.agent.loop._render_and_judge", fake)
    monkeypatch.setattr("vedit.agent.loop.transcriber_ready", lambda: True)
    return calls


def test_captions_are_graded_only_when_the_program_burns_them(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _judge(monkeypatch, ok=True)
    session = run_session("caption these photos as a slideshow", _pictures(tmp_path), root=tmp_path / "s", analyze=False)
    assert session.intent.captions is True and session.captions_skipped is False
    assert not any(op["op"] == "captions" for op in session.decision.brief.ops), "a slideshow has no speech to caption"
    assert calls and calls[0]["enforce_captions"] is False
    assert session.status == "verified"


def test_no_second_render_when_the_revision_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _judge(monkeypatch, ok=False)
    session = run_session("slideshow of these photos", _pictures(tmp_path), root=tmp_path / "s", analyze=False, max_revisions=3)
    assert len(calls) == 1, "an unchanged brief is not rendered again"
    assert session.revision == 0 and session.status == "failed"
    assert not (tmp_path / "s" / "rev1").exists()


def test_slashes_in_the_goal_are_not_media_paths(tmp_path: Path) -> None:
    root = tmp_path / "ratio"
    session = run_session("make a 16/9 cut and/or a vertical one", [], root=root, render=False, analyze=False)
    assert session.status == "failed"
    assert (root / "session.json").is_file()
    assert session.critique is not None and session.critique.findings[0].message == "no media paths"


def test_existing_files_in_the_goal_are_still_found(tmp_path: Path) -> None:
    from vedit.agent.loop import _paths_from_goal

    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"")
    assert _paths_from_goal(f"edit {clip} at 16/9 and/or square") == [str(clip)]
    assert _paths_from_goal("edit talk.mov as a short") == ["talk.mov"]
