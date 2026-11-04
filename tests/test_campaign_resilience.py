"""Durability and cancellation at real filesystem and subprocess boundaries."""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from collections import namedtuple
from pathlib import Path

import pytest

from vedit.campaign import CampaignError, CampaignManager
from vedit.media import resolve_ffmpeg, resolve_ffprobe

DiskUsage = namedtuple("DiskUsage", "total used free")


@pytest.fixture
def uploaded_clip(tmp_path: Path) -> Path:
    source = tmp_path / "vedit-sessions" / "source-resilience" / "inbox" / "clip.mp4"
    source.parent.mkdir(parents=True)
    spec = importlib.util.spec_from_file_location("resilience_media_helpers", Path(__file__).with_name("test_e2e.py"))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not module._have_ffmpeg():
        pytest.skip("FFmpeg is required for the real render boundary")
    return module._make_clip(source, seconds=0.9, color="navy")


def request(source: Path, **overrides) -> dict:
    return {"paths": [str(source)], "goal": "Make a clear short", "target_duration": 0.5, "captions": False, **overrides}


def wait_until(predicate, *, timeout: float = 15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.025)
    pytest.fail("The expected processing boundary was not reached before the deadline")


def wait_status(manager: CampaignManager, ident: str, statuses: set[str], *, timeout: float = 20) -> dict:
    return wait_until(lambda: (job if (job := manager.get(ident))["status"] in statuses else None), timeout=timeout)


def process_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def slow_encoder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, delay_termination: bool = False) -> Path:
    """Intercept only the first real encoder invocation, forwarding all flags.

    This fixture replaces the executable I/O edge, not the compiler, job state,
    pipeline executor, process signals, or output verification.
    """
    real_ffmpeg, real_ffprobe = resolve_ffmpeg(), resolve_ffprobe()
    marker = tmp_path / "encoder-started.pid"
    wrapper = tmp_path / "slow-encoder"
    script = f"""#!{sys.executable}
import os
import pathlib
import signal
import sys
import time
marker = pathlib.Path({str(marker)!r})
if sys.argv[-1].endswith('.mp4') and 'select_ranges' in sys.argv[-1] and not marker.exists():
    if {delay_termination!r}:
        def terminating(signum, frame):
            time.sleep(2)
            raise SystemExit(128 + signum)
        signal.signal(signal.SIGTERM, terminating)
    marker.write_text(str(os.getpid()))
    time.sleep(60)
os.execv({real_ffmpeg!r}, [{real_ffmpeg!r}, *sys.argv[1:]])
"""
    wrapper.write_text(script, encoding="utf-8")
    wrapper.chmod(0o755)
    monkeypatch.setenv("VEDIT_FFMPEG", str(wrapper))
    monkeypatch.setenv("VEDIT_FFPROBE", real_ffprobe)
    return marker


def kill_fixture_process(marker: Path) -> None:
    if marker.is_file():
        pid = int(marker.read_text())
        if process_is_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_cancelling_an_encode_stops_its_process_and_releases_worker(tmp_path: Path, uploaded_clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = slow_encoder(tmp_path, monkeypatch)
    manager = CampaignManager(tmp_path)
    try:
        first = manager.create(request(uploaded_clip, idempotency_key="first"))
        wait_status(manager, first["id"], {"planned"})
        manager.render(first["id"], {})
        wait_until(marker.is_file)
        pid = int(marker.read_text())
        second = manager.create(request(uploaded_clip, idempotency_key="second"))
        never_started = manager.create(request(uploaded_clip, idempotency_key="cancel-before-start"))
        assert manager.cancel(never_started["id"])["status"] == "cancelled"
        started = time.monotonic()
        manager.cancel(first["id"])
        cancelled = wait_status(manager, first["id"], {"cancelled", "failed"}, timeout=8)
        assert cancelled["status"] == "cancelled", cancelled.get("error")
        assert time.monotonic() - started < 8
        # The kill is immediate; reaping it can lag on a loaded machine (the full suite runs on every core).
        wait_until(lambda: not process_is_alive(pid), timeout=10)
        assert cancelled.get("error") is None
        assert manager.get(never_started["id"])["source"] is None
        wait_status(manager, second["id"], {"planned"})
        manager.render(second["id"], {})
        completed = wait_status(manager, second["id"], {"complete", "failed"})
        assert completed["status"] == "complete", completed.get("error")
        assert completed["result"]["proof"]["has_video"] is True
    finally:
        kill_fixture_process(marker)
        manager.close()


def test_failed_cancelled_and_interrupted_revisions_keep_verified_downloads(tmp_path: Path, uploaded_clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = CampaignManager(tmp_path, start_worker=False)
    ident = None
    try:
        initial = manager.create(request(uploaded_clip))
        ident = initial["id"]
        manager.process(ident)
        manager.render(ident, {})
        manager.process(ident)
        first = manager.get(ident)
        assert first["status"] == "complete", first.get("error")
        assert first["verified_revision"] == first["result"]["revision"] == 1
        assert first["result"]["stale"] is False
        original_video = manager.artifact(ident, "video").read_bytes()
        original_bundle = manager.artifact(ident, "bundle").read_bytes()
        original_proof = first["result"]["proof"]

        def preserved(job):
            assert job["revision"] > job["verified_revision"] == 1
            assert job["result"]["revision"] == 1
            assert job["result"]["stale"] is True
            assert job["result"]["proof"] == original_proof
            assert manager.artifact(ident, "video").read_bytes() == original_video
            assert manager.artifact(ident, "bundle").read_bytes() == original_bundle

        queued = manager.render(ident, {})
        preserved(queued)
        with monkeypatch.context() as low_disk:
            low_disk.setattr("shutil.disk_usage", lambda _: DiskUsage(1024, 1024, 0))
            manager.process(ident)
        failed = manager.get(ident)
        assert failed["status"] == "failed"
        preserved(failed)
        manager.retry(ident)
        cancelled = manager.cancel(ident)
        assert cancelled["status"] == "cancelled"
        preserved(cancelled)
        manager.close()

        # A separate process queues the next revision and exits abruptly,
        # exercising real lock release and SQLite recovery rather than editing
        # the database to fabricate an interrupted state.
        crash = "from pathlib import Path; import os,sys; from vedit.campaign import CampaignManager; manager=CampaignManager(Path(sys.argv[1]),start_worker=False); manager.render(sys.argv[2],{}); os._exit(0)"
        subprocess.run([sys.executable, "-c", crash, str(tmp_path), ident], check=True, timeout=15)
        manager = CampaignManager(tmp_path, start_worker=False)
        restored = manager.get(ident)
        assert restored["status"] == "failed"
        assert restored["stage"] == "interrupted"
        preserved(restored)
        report = json.loads(manager.artifact(ident, "report").read_text())
        assert report["proof"] == original_proof
    finally:
        manager.close()


def test_low_disk_blocks_cloud_analysis_before_upload(tmp_path: Path, uploaded_clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_API_KEY", "test-key-never-sent")
    monkeypatch.setenv("VEDIT_META_ENABLED", "1")
    monkeypatch.setattr("shutil.disk_usage", lambda _: DiskUsage(1024, 1024, 0))
    requests = []

    def unexpected_request(*args, **kwargs):
        requests.append((args, kwargs))
        raise AssertionError("Cloud requests must not begin when the proxy cannot fit on disk")

    monkeypatch.setattr("vedit.meta._request", unexpected_request)
    manager = CampaignManager(tmp_path, start_worker=False)
    try:
        job = manager.create(request(uploaded_clip, provider="meta"))
        manager.process(job["id"])
        failed = manager.get(job["id"])
        assert failed["status"] == "failed"
        message = failed["error"].lower()
        assert "space" in message or "disk" in message
        assert "available" in message
        assert "required" in message or "need" in message
        assert requests == []
        assert not list((manager.root / job["id"]).rglob("analysis.mp4"))
    finally:
        manager.close()


def test_disk_safety_margin_is_configurable_without_blocking_tiny_edits(tmp_path: Path, uploaded_clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gib = 1024 ** 3
    monkeypatch.setattr("shutil.disk_usage", lambda _: DiskUsage(2 * gib, gib, gib))
    monkeypatch.setenv("VEDIT_MIN_FREE_DISK_MB", "2048")
    manager = CampaignManager(tmp_path, start_worker=False)
    try:
        job = manager.create(request(uploaded_clip))
        manager.process(job["id"])
        manager.render(job["id"], {})
        manager.process(job["id"])
        assert manager.get(job["id"])["status"] == "failed"
        monkeypatch.setenv("VEDIT_MIN_FREE_DISK_MB", "0")
        manager.retry(job["id"])
        manager.process(job["id"])
        completed = manager.get(job["id"])
        assert completed["status"] == "complete", completed.get("error")
    finally:
        manager.close()


def test_close_keeps_workspace_lease_until_active_encoder_stops(tmp_path: Path, uploaded_clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = slow_encoder(tmp_path, monkeypatch, delay_termination=True)
    manager = CampaignManager(tmp_path)
    replacement = None
    try:
        job = manager.create(request(uploaded_clip))
        wait_status(manager, job["id"], {"planned"})
        manager.render(job["id"], {})
        wait_until(marker.is_file)
        pid = int(marker.read_text())
        assert process_is_alive(pid)
        manager.close(wait=False)

        def acquire_after_shutdown():
            nonlocal replacement
            try:
                replacement = CampaignManager(tmp_path, start_worker=False)
            except CampaignError as error:
                assert error.status == 409
                return False
            assert not process_is_alive(pid), "Workspace lock was released while the old encoder was still running"
            return True

        wait_until(acquire_after_shutdown, timeout=8)
        assert replacement is not None
        recovered = replacement.get(job["id"])
        assert recovered["status"] in {"cancelled", "failed"}
    finally:
        kill_fixture_process(marker)
        manager.close()
        if replacement is not None:
            replacement.close()


def test_verified_artifact_revisions_never_switch_to_newer_bytes(tmp_path: Path, uploaded_clip: Path) -> None:
    manager = CampaignManager(tmp_path, start_worker=False)
    try:
        job = manager.create(request(uploaded_clip))
        ident = job["id"]
        manager.process(ident)
        manager.render(ident, {})
        manager.process(ident)
        first = manager.get(ident)
        assert first["status"] == "complete", first.get("error")
        assert "&v=1" in first["result"]["video_url"]
        first_video = manager.artifact(ident, "video", revision=1).read_bytes()
        first_report = manager.artifact(ident, "report", revision=1).read_bytes()
        plan = dict(first["plan"], segments=[{"start": 0, "end": 0.8, "label": "Longer", "reason": "Selected by the editor."}])
        manager.render(ident, {"plan": plan})
        manager.process(ident)
        second = manager.get(ident)
        assert second["status"] == "complete", second.get("error")
        assert "&v=2" in second["result"]["video_url"]
        assert manager.artifact(ident, "video", revision=1).read_bytes() == first_video
        assert manager.artifact(ident, "report", revision=1).read_bytes() == first_report
        assert manager.artifact(ident, "video", revision=2).read_bytes() != first_video
        assert manager.artifact(ident, "video") == manager.artifact(ident, "video", revision=2)
        manager.render(ident, {})
        manager.cancel(ident)
        # A render directory existing on disk is not proof of a verified export.
        unverified = manager.root / ident / "render-3"
        unverified.mkdir()
        (unverified / "video.mp4").write_bytes(first_video)
        with pytest.raises(CampaignError) as error:
            manager.artifact(ident, "video", revision=3)
        assert error.value.status == 404
    finally:
        manager.close()

