"""Exercise real subprocesses: cancellation must stop work, not just its UI."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from vedit import media
from vedit.errors import BinaryMissing, ProbeError, RenderCancelled, RenderError
from vedit.pipeline import Pipeline, Step, execute


def _running(pid: int) -> bool:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True, timeout=2, check=False
        )
    except PermissionError as exc:
        pytest.skip(f"the sandbox denied process inspection with ps: {exc}")
    # An orphan zombie is already dead and will be reaped by the OS.
    state = result.stdout.strip()
    return bool(state) and not state.startswith("Z")


def _tree_script(tmp_path: Path, *, leader_exits: bool = False) -> Path:
    script = tmp_path / "process_tree.py"
    script.write_text(
        "import os, pathlib, signal, subprocess, sys, time\n"
        "root = pathlib.Path(sys.argv[2])\n"
        "mode = int(sys.argv[1])\n"
        "if mode == 2:\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "(root / f'{mode}.pid').write_text(str(os.getpid()))\n"
        "if mode < 2:\n"
        "    subprocess.Popen([sys.executable, __file__, str(mode + 1), str(root)])\n"
        + (
            "if mode == 0:\n    while not (root / '2.pid').exists(): time.sleep(0.01)\n    sys.exit(0)\n"
            if leader_exits
            else ""
        )
        + "while True: time.sleep(0.05)\n",
        encoding="utf-8",
    )
    return script


def _pids(tmp_path: Path) -> list[int]:
    return [int(path.read_text()) for path in sorted(tmp_path.glob("*.pid"))]


def _assert_tree_stopped(tmp_path: Path) -> None:
    pids = _pids(tmp_path)
    assert len(pids) == 3, "test must observe the render, its child, and its grandchild"
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and any(_running(pid) for pid in pids):
        time.sleep(0.02)
    assert not [pid for pid in pids if _running(pid)], "render descendants survived cleanup"


def _cleanup_tree(tmp_path: Path) -> None:
    for pid in _pids(tmp_path):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@pytest.mark.parametrize("nested", [False, True])
def test_capture_keeps_exit_code_and_replaces_invalid_utf8(monkeypatch, nested):
    monkeypatch.setenv(media._MANAGED_COMMAND_ENV, "1" if nested else "0")
    result = media.run_command(
        [
            sys.executable,
            "-c",
            "import os, sys; os.write(1, b'output\\xff'); os.write(2, b'error\\xfe'); sys.exit(7)",
        ]
    )
    assert result.returncode == 7
    assert result.ok is False
    assert result.stdout == "output\ufffd"
    assert result.stderr == "error\ufffd"
    assert result.duration_ms >= 0


@pytest.mark.parametrize("nested", [False, True])
def test_missing_executable_preserves_typed_error(monkeypatch, nested, tmp_path):
    monkeypatch.setenv(media._MANAGED_COMMAND_ENV, "1" if nested else "0")
    with pytest.raises(BinaryMissing, match="executable not found"):
        media.run_command([str(tmp_path / "missing-program")])


@pytest.mark.skipif(os.name != "posix", reason="POSIX signal return codes")
@pytest.mark.parametrize("nested", [False, True])
def test_signal_exit_status_is_preserved(monkeypatch, nested):
    monkeypatch.setenv(media._MANAGED_COMMAND_ENV, "1" if nested else "0")
    result = media.run_command([sys.executable, "-c", "import os,signal; os.kill(os.getpid(), signal.SIGKILL)"])
    assert result.returncode == -signal.SIGKILL


def test_streamed_stderr_is_inherited_and_not_captured(capfd):
    result = media.run_command(
        [sys.executable, "-c", "import sys; print('live', file=sys.stderr); print('captured')"],
        stream_stderr=True,
    )
    assert result.ok
    assert result.stdout == "captured\n"
    assert result.stderr == ""
    assert "live" in capfd.readouterr().err


def test_pre_cancel_does_not_launch_process(tmp_path):
    event = threading.Event()
    event.set()
    marker = tmp_path / "unexpected"
    with pytest.raises(RenderCancelled):
        media.run_command(
            [sys.executable, "-c", "import pathlib,sys; pathlib.Path(sys.argv[1]).touch()", str(marker)],
            cancel_event=event,
        )
    assert not marker.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group cleanup")
@pytest.mark.parametrize("leader_exits", [False, True])
def test_timeout_stops_nested_children_even_when_parent_exits(tmp_path, leader_exits):
    script = _tree_script(tmp_path, leader_exits=leader_exits)
    started = time.monotonic()
    try:
        with pytest.raises(RenderError, match="timed out after"):
            media.run_command([sys.executable, str(script), "0", str(tmp_path)], timeout=1)
        assert time.monotonic() - started < 3
        _assert_tree_stopped(tmp_path)
    finally:
        _cleanup_tree(tmp_path)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group cleanup")
def test_cancellation_stops_tree_and_preserves_unrelated_process(tmp_path):
    script = _tree_script(tmp_path)
    event = threading.Event()
    cancelled_at: list[float] = []

    def cancel_when_started():
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and not (tmp_path / "2.pid").exists():
            time.sleep(0.01)
        cancelled_at.append(time.monotonic())
        event.set()

    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    trigger = threading.Thread(target=cancel_when_started)
    trigger.start()
    try:
        with pytest.raises(RenderCancelled):
            media.run_command([sys.executable, str(script), "0", str(tmp_path)], cancel_event=event)
        trigger.join(timeout=1)
        assert time.monotonic() - cancelled_at[0] < 2
        _assert_tree_stopped(tmp_path)
        assert unrelated.poll() is None
    finally:
        event.set()
        trigger.join(timeout=5)
        _cleanup_tree(tmp_path)
        unrelated.kill()
        unrelated.wait(timeout=2)


@pytest.mark.skipif(os.name != "posix", reason="POSIX nested helper supervision")
@pytest.mark.parametrize("cancel", [False, True])
def test_nested_vedit_helpers_cannot_escape_cancellation_or_timeout(tmp_path, cancel):
    script = tmp_path / "nested_vedit.py"
    script.write_text(
        "import os, pathlib, signal, sys, time\n"
        f"sys.path.insert(0, {str(Path(media.__file__).parent.parent)!r})\n"
        "from vedit.media import run_command\n"
        "root, depth = pathlib.Path(sys.argv[1]), int(sys.argv[2])\n"
        "if depth == 2: signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "(root / f'{depth}.group').write_text(str(os.getpgrp()))\n"
        "(root / f'{depth}.pid').write_text(str(os.getpid()))\n"
        "if depth < 2:\n"
        "    run_command([sys.executable, __file__, str(root), str(depth + 1)])\n"
        "else:\n"
        "    while True: time.sleep(0.05)\n",
        encoding="utf-8",
    )
    event = threading.Event()

    def cancel_when_started():
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and not (tmp_path / "2.pid").exists():
            time.sleep(0.01)
        event.set()

    trigger = threading.Thread(target=cancel_when_started)
    if cancel:
        trigger.start()
    try:
        with pytest.raises(RenderCancelled if cancel else RenderError):
            media.run_command(
                [sys.executable, str(script), str(tmp_path), "0"],
                # Three interpreters each import vedit before the tree exists to stop.
                timeout=5 if cancel else 4,
                cancel_event=event if cancel else None,
            )
        _assert_tree_stopped(tmp_path)
        groups = [int(path.read_text()) for path in tmp_path.glob("*.group")]
        assert len(set(groups)) == 3, "exercise separate groups created by nested run_command calls"
        assert not [pid for pid in groups if _running(pid)], "supervisor processes survived cleanup"
    finally:
        event.set()
        if cancel:
            trigger.join(timeout=5)
        _cleanup_tree(tmp_path)


@pytest.mark.skipif(os.name != "posix", reason="POSIX hard-kill supervision")
def test_cancellable_root_command_dies_when_its_owner_is_hard_killed(tmp_path):
    command = _tree_script(tmp_path)
    owner = tmp_path / "studio_owner.py"
    owner.write_text(
        "import sys, threading\n"
        f"sys.path.insert(0, {str(Path(media.__file__).parent.parent)!r})\n"
        "from vedit.media import run_command\n"
        "run_command([sys.executable, sys.argv[1], '0', sys.argv[2]], cancel_event=threading.Event())\n",
        encoding="utf-8",
    )
    parent = subprocess.Popen(
        [sys.executable, str(owner), str(command), str(tmp_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and not (tmp_path / "2.pid").exists():
            time.sleep(0.01)
        pids = _pids(tmp_path)
        assert len(pids) == 3, "owner must have launched the entire real render tree"
        supervisor = os.getpgid(pids[0])
        assert supervisor != parent.pid, "render must have its own process group"
        parent.kill()  # No Python cleanup or graceful close can run here.
        parent.wait(timeout=2)
        _assert_tree_stopped(tmp_path)
        assert not _running(supervisor), "supervisor survived owner death"
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.wait(timeout=2)
        _cleanup_tree(tmp_path)


def test_pipeline_cancellation_stops_current_step_and_never_starts_next(tmp_path):
    started = tmp_path / "started"
    unwanted = tmp_path / "next-step"
    event = threading.Event()
    pipe = Pipeline(name="cancel", work_dir=tmp_path / "work")
    pipe.add(
        Step(
            "first",
            [
                sys.executable,
                "-c",
                "import pathlib,sys,time; pathlib.Path(sys.argv[1]).touch(); time.sleep(30)",
                str(started),
            ],
            [],
            "wait",
            optional=True,
        )
    )
    pipe.add(
        Step(
            "second",
            [sys.executable, "-c", "import pathlib,sys; pathlib.Path(sys.argv[1]).touch()", str(unwanted)],
            [str(unwanted)],
            "must not run",
        )
    )

    def cancel_when_started():
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and not started.exists():
            time.sleep(0.01)
        event.set()

    trigger = threading.Thread(target=cancel_when_started)
    trigger.start()
    try:
        with pytest.raises(RenderCancelled):
            execute(pipe, progress=False, cancel_event=event)
        assert started.exists(), "exercise cancellation while a real pipeline step is running"
        assert not unwanted.exists()
    finally:
        event.set()
        trigger.join(timeout=5)


def test_probe_timeout_is_bounded_and_actionable(tmp_path, monkeypatch):
    fake_probe = tmp_path / "ffprobe"
    fake_probe.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n", encoding="utf-8")
    fake_probe.chmod(0o755)
    source = tmp_path / "source.mp4"
    source.touch()
    monkeypatch.setattr(media, "resolve_ffprobe", lambda: str(fake_probe))
    monkeypatch.setattr(media, "_PROBE_TIMEOUT_SECONDS", 0.1)
    started = time.monotonic()
    with pytest.raises(ProbeError, match="timed out.*readable and complete"):
        media.probe(source)
    assert time.monotonic() - started < 2
