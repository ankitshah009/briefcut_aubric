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
