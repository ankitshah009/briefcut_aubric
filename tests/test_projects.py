"""The project library, and proof that removing things stays inside the workspace."""

from __future__ import annotations

import http.client
import json
import os
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from vedit import projects
from vedit.campaign import CampaignError, CampaignManager, get_manager
from vedit.studio.server import StudioHandler, _LoopbackServer


def sessions(root: Path) -> Path:
    return root / "vedit-sessions"


def upload(root: Path, name: str = "clip.mp4", folder: str | None = None, data: bytes = b"source" * 100) -> Path:
    path = sessions(root) / (folder or f"source-{uuid.uuid4().hex[:8]}") / "inbox" / name
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    return path.resolve()


def add_job(manager: CampaignManager, clip: Path | str, *, status: str = "planned", stage: str = "review", **fields: object) -> str:
    ident = uuid.uuid4().hex
    now = "2026-10-31T00:00:00.000+00:00"
    job = {
        "id": ident, "status": status, "stage": stage, "progress": 1, "message": "", "created_at": now, "updated_at": now,
        "request": {"paths": [str(clip)], "goal": "A brief", "budget_usd": 0.5, "provider": "local", "target_duration": 5},
        "plan": {"title": "The bowl", "duration": 4.0, "segments": [{"start": 0, "end": 4, "label": "All", "reason": "All"}]},
        "result": None, "source": {"name": "clip.mp4", "duration": 8.0, "has_video": True},
        "error": None, "source_url": f"/api/campaigns/{ident}/artifact?name=source", "cancel_requested": False, "revision": 0,
        "verified_revision": None, "budget": {"cap_usd": 0.5, "reserved_usd": 0.2, "remaining_usd": 0.3},
        "_action": "plan", "_artifacts": {}, "_fingerprint": ident,
    }
    job.update(fields)
    with manager._connect() as connection:
        connection.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?)", (ident, ident, job["status"], now, now, json.dumps(job)))
    (manager.root / ident).mkdir()
    return ident


def add_render(manager: CampaignManager, ident: str, revision: int, *, size: int = 1000) -> dict[str, str]:
    folder = manager.root / ident / f"render-{revision}"
    folder.mkdir()
    artifacts = {}
    for name, file in (("video", "video.mp4"), ("cover", "cover.jpg"), ("report", "report.json"), ("brief", "brief.yaml"), ("bundle", "campaign.zip")):
        (folder / file).write_bytes(b"x" * size)
        artifacts[name] = f"render-{revision}/{file}"
    return artifacts


def verified_job(manager: CampaignManager, source: Path, revisions: int = 3) -> str:
    ident = add_job(manager, source, status="complete", stage="complete", revision=revisions, verified_revision=revisions)
    history = {str(revision): add_render(manager, ident, revision) for revision in range(1, revisions + 1)}
    versions = [{"revision": revision, "status": "verified", "source": "approved", "proof": {"duration": 4.0}} for revision in range(1, revisions + 1)]
    with manager._lock, manager._connect() as connection:
        job = manager._load(connection, ident)
        job.update(_artifacts=history[str(revisions)], _verified_artifacts=history, versions=versions, result={"proof": {"duration": 4.0, "has_video": True}, "revision": revisions})
        manager._save(connection, job)
    return ident


def tampered(manager: CampaignManager, ident: str, **fields: object) -> None:
    with manager._lock, manager._connect() as connection:
        job = manager._load(connection, ident)
        job.update(fields)
        manager._save(connection, job)


def job_row(manager: CampaignManager, ident: str) -> dict | None:
    with manager._connect() as connection:
        row = connection.execute("SELECT data FROM jobs WHERE id=?", (ident,)).fetchone()
    return json.loads(row[0]) if row else None


@pytest.fixture
def manager(tmp_path: Path) -> Iterator[CampaignManager]:
    (tmp_path / "workspace").mkdir()
    instance = CampaignManager(tmp_path / "workspace", start_worker=False)
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def workspace(manager: CampaignManager) -> Path:
    return manager.workspace


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """A file that must survive everything, next to the workspace."""
    folder = tmp_path / "outside"
    folder.mkdir()
    (folder / "keep.txt").write_text("precious", encoding="utf-8")
    return folder


# ---- Library ----


def test_library_lists_what_a_person_needs(manager: CampaignManager, workspace: Path) -> None:
    source = upload(workspace)
    ident = verified_job(manager, source)
    (manager.root / ident / "note.txt").write_bytes(b"y" * 500)
    data = projects.library(manager)
    row = data["projects"][0]
    assert row["id"] == ident and row["title"] == "The bowl"
    assert row["status"] == "complete" and row["status_label"] == "Verified"
    assert row["duration"] == 4.0 and row["created_at"] and row["updated_at"]
    assert row["versions"] == 3 and row["verified_revision"] == 3
    assert row["size_bytes"] == 15000 + 500 + source.stat().st_size
    assert row["thumbnail_url"] == f"/api/projects/{ident}/thumbnail"
    assert data["storage"]["total_bytes"] >= row["size_bytes"]
    assert data["storage"]["reclaimable_bytes"] == 10000


def test_source_size_counts_once_in_the_total_when_shared(manager: CampaignManager, workspace: Path) -> None:
    source = upload(workspace)
    add_job(manager, source)
    add_job(manager, source)
    first = projects.library(manager)
    assert all(row["size_bytes"] == source.stat().st_size for row in first["projects"])
    assert first["storage"]["total_bytes"] < 2 * source.stat().st_size + (manager.database.stat().st_size + 1_000_000)


def test_interrupted_jobs_are_labelled_plainly(tmp_path: Path) -> None:
    root = tmp_path / "w"
    root.mkdir()
    first = CampaignManager(root, start_worker=False)
    source = upload(first.workspace)
    ident = add_job(first, source, status="rendering", stage="encoding")
    first.close()
    second = CampaignManager(root, start_worker=False)
    try:
        data = projects.library(second)
        row = data["projects"][0]
        assert row["id"] == ident and row["status"] == "interrupted" and row["status_label"] == "Interrupted"
        assert data["interrupted"] == 1
        assert "interrupted" in row["error"]
        # Retry works from the saved plan, not from a claim that the job went on.
        assert second.retry(ident)["status"] == "queued"
    finally:
        second.close()


def test_rename_changes_the_project_not_the_video_title(manager: CampaignManager, workspace: Path) -> None:
    ident = add_job(manager, upload(workspace))
    row = projects.rename(manager, ident, {"title": "  Client cut  "})
    assert row["title"] == "Client cut"
    job = manager.get(ident)
    assert job["name"] == "Client cut" and job["plan"]["title"] == "The bowl"


@pytest.mark.parametrize("title", ["", "   ", "x" * 121, 5, None, "bad\x00name"])
def test_rename_rejects_bad_names(manager: CampaignManager, workspace: Path, title: object) -> None:
    ident = add_job(manager, upload(workspace))
    with pytest.raises(CampaignError):
        projects.rename(manager, ident, {"title": title})


def test_duplicate_is_a_fresh_project_on_the_same_source(manager: CampaignManager, workspace: Path) -> None:
    source = upload(workspace)
    ident = verified_job(manager, source)
    (manager.root / ident / "assets").mkdir()
    (manager.root / ident / "assets" / "logo.png").write_bytes(b"logo")
    (manager.root / ident / "meta").mkdir()
    (manager.root / ident / "meta" / "meta-usage.json").write_text('{"calls": []}', encoding="utf-8")
    (manager.root / ident / "cover-3.json").write_text("{}", encoding="utf-8")
    tampered(manager, ident, source={"name": "clip.mp4", "duration": 8.0, "has_video": True, "filmstrip": {"url": f"/api/campaigns/{ident}/filmstrip"}})
    copy = projects.duplicate(manager, ident)
    assert copy["id"] != ident and copy["status"] == "planned" and copy["revision"] == 0
    assert copy["name"] == "The bowl copy" and copy["duplicated_from"] == ident
    assert copy["plan"] == manager.get(ident)["plan"]
    assert copy["request"]["paths"] == [str(source)]
    assert copy["result"] is None and copy["versions"] == [] and copy["verified_revision"] is None
    assert copy["budget"]["reserved_usd"] == 0 and copy["budget"]["remaining_usd"] == 0.5
    assert copy["source"]["filmstrip"]["url"] == f"/api/campaigns/{copy['id']}/filmstrip"
    assert copy["source_url"].startswith(f"/api/campaigns/{copy['id']}/")
    folder = manager.root / copy["id"]
    assert (folder / "assets" / "logo.png").read_bytes() == b"logo"
    assert not list(folder.glob("render-*")) and not (folder / "meta").exists() and not (folder / "cover-3.json").exists()
    assert manager.get(copy["id"])["id"] == copy["id"]
    with pytest.raises(CampaignError, match="after the render"):
        manager.artifact(copy["id"], "video")


def test_duplicate_can_be_rendered_and_deleted_without_touching_the_original(manager: CampaignManager, workspace: Path) -> None:
    source = upload(workspace)
    ident = add_job(manager, source)
    copy = projects.duplicate(manager, ident)
    projects.delete(manager, copy["id"])
    assert source.is_file(), "the original still uses the inbox copy"
    assert manager.get(ident)["id"] == ident


def test_large_media_is_linked_not_copied(manager: CampaignManager, workspace: Path) -> None:
    ident = add_job(manager, upload(workspace))
    large = manager.root / ident / "project-source.mp4"
    with large.open("wb") as handle:
        handle.truncate(17 * 1024 * 1024)
    copy = projects.duplicate(manager, ident)
    twin = manager.root / copy["id"] / "project-source.mp4"
    assert twin.stat().st_ino == large.stat().st_ino
    projects.delete(manager, ident)
    assert twin.stat().st_size == 17 * 1024 * 1024


@pytest.mark.parametrize("status", ["queued", "analyzing", "rendering"])
def test_duplicate_waits_for_active_jobs(manager: CampaignManager, workspace: Path, status: str) -> None:
    ident = add_job(manager, upload(workspace), status=status)
    with pytest.raises(CampaignError, match="finish"):
        projects.duplicate(manager, ident)


def test_duplicate_needs_a_plan(manager: CampaignManager, workspace: Path) -> None:
    ident = add_job(manager, upload(workspace), status="failed", plan=None)
    with pytest.raises(CampaignError, match="plan"):
        projects.duplicate(manager, ident)


# ---- Delete ----


def test_delete_removes_the_job_and_its_unshared_source(manager: CampaignManager, workspace: Path) -> None:
    source = upload(workspace)
    ident = verified_job(manager, source)
    size = projects.library(manager)["projects"][0]["size_bytes"]
    result = projects.delete(manager, ident)
    assert result["deleted"] == ident and result["freed_bytes"] == size
    assert result["source_removed"] == 1 and result["source_kept"] == 0
    assert not (manager.root / ident).exists()
    assert not source.exists() and not source.parent.exists() and not source.parent.parent.exists()
    assert job_row(manager, ident) is None
    with pytest.raises(CampaignError):
        manager.get(ident)


@pytest.mark.parametrize("status", ["queued", "analyzing", "rendering"])
def test_delete_is_refused_while_active(manager: CampaignManager, workspace: Path, status: str) -> None:
    source = upload(workspace)
    ident = add_job(manager, source, status=status)
    with pytest.raises(CampaignError) as caught:
        projects.delete(manager, ident)
    assert caught.value.status == 409
    assert (manager.root / ident).is_dir() and source.is_file() and job_row(manager, ident)


def test_a_source_another_project_uses_is_kept(manager: CampaignManager, workspace: Path) -> None:
    source = upload(workspace)
    first, second = add_job(manager, source), add_job(manager, source)
    result = projects.delete(manager, first)
    assert result["source_removed"] == 0 and result["source_kept"] == 1 and source.is_file()
    result = projects.delete(manager, second)
    assert result["source_removed"] == 1 and not source.exists()


def test_any_mention_in_another_job_keeps_the_source(manager: CampaignManager, workspace: Path) -> None:
    source = upload(workspace)
    other = upload(workspace, name="other.mp4")
    ident = add_job(manager, source)
    add_job(manager, other, assets={"a1": {"path": str(source)}})
    assert projects.delete(manager, ident)["source_kept"] == 1
    assert source.is_file()


def test_a_folder_shared_with_an_older_session_is_left_alone(manager: CampaignManager, workspace: Path) -> None:
    source = upload(workspace)
    session = source.parent.parent / "session.json"
    session.write_text("{}", encoding="utf-8")
    ident = add_job(manager, source)
    assert projects.delete(manager, ident)["source_kept"] == 1
    assert source.is_file() and session.is_file()


def test_other_files_in_the_inbox_survive(manager: CampaignManager, workspace: Path) -> None:
    source = upload(workspace)
    neighbour = source.parent / "second.mp4"
    neighbour.write_bytes(b"x")
    projects.delete(manager, add_job(manager, source))
    assert not source.exists() and neighbour.is_file()


def test_a_crash_between_steps_is_finished_later(manager: CampaignManager, workspace: Path) -> None:
    ident = uuid.uuid4().hex
    trash = manager.root / f".deleting-{ident}"
    trash.mkdir()
    (trash / "video.mp4").write_bytes(b"x")
    not_ours = manager.root / ".deleting-notes"
    not_ours.mkdir()
    projects.library(manager)
    assert not trash.exists() and not_ours.is_dir()


def test_worker_survives_a_job_deleted_while_queued(tmp_path: Path) -> None:
    root = tmp_path / "w"
    root.mkdir()
    manager = CampaignManager(root, start_worker=False)
    try:
        source = upload(manager.workspace)
        ident = add_job(manager, source, status="cancelled", stage="cancelled")
        projects.delete(manager, ident)
        manager._queue.put_nowait(ident)
        manager._worker = threading.Thread(target=manager._work, daemon=True)
        manager._worker.start()
        deadline = time.monotonic() + 3
        while manager._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        assert manager._queue.unfinished_tasks == 0
        time.sleep(0.05)
        assert manager._worker.is_alive(), "one stale id must not stop every later render"
    finally:
        manager.close()


# ---- Deletion cannot leave the workspace ----


@pytest.mark.parametrize("ident", ["", "..", "../..", "..%2f..", "abc", "A" * 32, "0" * 31, "0" * 33, "g" * 32, "/etc/passwd", "0" * 32 + "/../.."])
def test_a_bad_id_is_refused_before_any_path_is_built(manager: CampaignManager, outside: Path, ident: str) -> None:
    with pytest.raises(CampaignError) as caught:
        projects.delete(manager, ident)
    assert caught.value.status == 404
    assert (outside / "keep.txt").is_file()


def test_a_tampered_source_path_outside_the_workspace_survives(manager: CampaignManager, workspace: Path, outside: Path) -> None:
    ident = add_job(manager, outside / "keep.txt")
    result = projects.delete(manager, ident)
    assert result["source_removed"] == 0 and result["source_kept"] == 1
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "precious"


@pytest.mark.parametrize("build", [
    lambda root, out: root / "vedit-sessions" / "a" / "inbox" / ".." / ".." / ".." / ".." / "outside" / "keep.txt",
    lambda root, out: root / "vedit-sessions" / "campaigns" / "inbox" / "campaigns.sqlite3",
    lambda root, out: root / "vedit-sessions" / "campaigns" / "campaigns.sqlite3",
    lambda root, out: root / "vedit-sessions" / "x" / "keep.txt",
    lambda root, out: root / "vedit-sessions" / "x" / "inbox" / "deep" / "keep.txt",
    lambda root, out: root / "vedit-sessions" / "x" / "inbox" / "..",
])
def test_a_tampered_path_with_traversal_survives(manager: CampaignManager, workspace: Path, outside: Path, build) -> None:
    raw = str(build(workspace, outside))
    for folder in (workspace / "vedit-sessions" / "a" / "inbox", workspace / "vedit-sessions" / "x" / "inbox" / "deep"):
        folder.mkdir(parents=True, exist_ok=True)
    (workspace / "vedit-sessions" / "x" / "keep.txt").write_text("k", encoding="utf-8")
    (workspace / "vedit-sessions" / "x" / "inbox" / "deep" / "keep.txt").write_text("k", encoding="utf-8")
    ident = add_job(manager, raw)
    assert projects.delete(manager, ident)["source_removed"] == 0
    assert (outside / "keep.txt").is_file()
    assert manager.database.is_file()
    assert (workspace / "vedit-sessions" / "x" / "keep.txt").is_file()
    assert (workspace / "vedit-sessions" / "x" / "inbox" / "deep" / "keep.txt").is_file()


@pytest.mark.parametrize("paths", ["not a list", None, [None], [5], [{}], [""], ["relative/clip.mp4"]])
def test_a_malformed_request_deletes_only_the_job(manager: CampaignManager, workspace: Path, paths: object) -> None:
    ident = add_job(manager, "x")
    tampered(manager, ident, request={"paths": paths})
    assert projects.delete(manager, ident)["source_removed"] == 0
    assert not (manager.root / ident).exists()


def test_a_linked_job_folder_is_unlinked_not_followed(manager: CampaignManager, workspace: Path, outside: Path) -> None:
    source = upload(workspace)
    ident = add_job(manager, source)
    folder = manager.root / ident
    folder.rmdir()
    folder.symlink_to(outside, target_is_directory=True)
    projects.delete(manager, ident)
    assert not folder.is_symlink() and not folder.exists()
    assert (outside / "keep.txt").is_file()


def test_a_link_inside_a_job_folder_is_not_followed(manager: CampaignManager, workspace: Path, outside: Path) -> None:
    ident = add_job(manager, upload(workspace))
    folder = manager.root / ident
    (folder / "linked").symlink_to(outside, target_is_directory=True)
    (folder / "linked-file").symlink_to(outside / "keep.txt")
    render = folder / "render-1"
    render.mkdir()
    (render / "deep-link").symlink_to(outside, target_is_directory=True)
    projects.delete(manager, ident)
    assert not folder.exists() and (outside / "keep.txt").is_file()


def test_a_linked_inbox_folder_is_refused(manager: CampaignManager, workspace: Path, outside: Path) -> None:
    session = sessions(workspace) / "linked-session"
    session.mkdir()
    (session / "inbox").symlink_to(outside, target_is_directory=True)
    ident = add_job(manager, session / "inbox" / "keep.txt")
    assert projects.delete(manager, ident)["source_removed"] == 0
    assert (outside / "keep.txt").is_file()


def test_a_linked_source_file_removes_the_link_only(manager: CampaignManager, workspace: Path, outside: Path) -> None:
    link = sessions(workspace) / "s1" / "inbox" / "clip.mp4"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside / "keep.txt")
    ident = add_job(manager, link)
    result = projects.delete(manager, ident)
    assert result["source_removed"] == 1 and result["freed_bytes"] == 0
    assert not link.is_symlink() and (outside / "keep.txt").is_file()


def test_a_linked_session_folder_is_refused(manager: CampaignManager, workspace: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere" / "inbox"
    elsewhere.mkdir(parents=True)
    (elsewhere / "clip.mp4").write_bytes(b"x")
    (sessions(workspace) / "s2").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    ident = add_job(manager, sessions(workspace) / "s2" / "inbox" / "clip.mp4")
    assert projects.delete(manager, ident)["source_removed"] == 0
    assert (elsewhere / "clip.mp4").is_file()


def test_an_id_for_a_folder_with_no_job_touches_nothing(manager: CampaignManager) -> None:
    ident = uuid.uuid4().hex
    (manager.root / ident).mkdir()
    (manager.root / ident / "file").write_bytes(b"x")
    with pytest.raises(CampaignError):
        projects.delete(manager, ident)
    assert (manager.root / ident / "file").is_file()


def test_deleted_files_stay_inside_the_expected_folders(manager: CampaignManager, workspace: Path, tmp_path: Path, outside: Path) -> None:
    """Snapshot everything outside the two allowed folders; delete; compare."""
    source = upload(workspace)
    ident = verified_job(manager, source)
    keep = upload(workspace, name="keep.mp4")
    add_job(manager, keep)

    def listing() -> set[str]:
        found = set()
        for root, _dirs, files in os.walk(tmp_path):
            if str(manager.root / ident) in root or str(source.parent.parent) in root:
                continue
            found.update(os.path.join(root, name) for name in files if "sqlite3" not in name)
        return found

    before = listing()
    projects.delete(manager, ident)
    assert listing() == before


# ---- Clean up old renders ----


def test_cleanup_keeps_the_latest_verified_version(manager: CampaignManager, workspace: Path) -> None:
    ident = verified_job(manager, upload(workspace))
    preview = projects.cleanup(manager, dry_run=True)
    assert preview["freed_bytes"] == 10000 and preview["dry_run"] is True
    assert (manager.root / ident / "render-1").is_dir(), "a preview removes nothing"
    result = projects.cleanup(manager)
    assert result["freed_bytes"] == 10000
    assert [item["revision"] for item in result["removed"]] == [1, 2]
    assert not (manager.root / ident / "render-1").exists() and not (manager.root / ident / "render-2").exists()
    assert (manager.root / ident / "render-3" / "video.mp4").is_file()
    assert manager.artifact(ident, "video", revision=3).is_file()
    assert manager.artifact(ident, "video").is_file()
    with pytest.raises(CampaignError):
        manager.artifact(ident, "video", revision=1)
    job = manager.get(ident)
    assert [entry["revision"] for entry in job["versions"]] == [3]
    assert job["verified_revision"] == 3


def test_cleanup_twice_is_a_no_op(manager: CampaignManager, workspace: Path) -> None:
    verified_job(manager, upload(workspace))
    projects.cleanup(manager)
    assert projects.cleanup(manager) == {"freed_bytes": 0, "removed": [], "orphans": [], "dry_run": False}


def test_cleanup_moves_each_render_aside_under_the_lock_and_removes_it_after(manager: CampaignManager, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A multi-gigabyte rmtree must not hold the lock every poll and worker update takes."""
    ident = verified_job(manager, upload(workspace))
    seen: list[tuple[str, bool]] = []
    real = projects.shutil.rmtree

    def observed(path, *args, **kwargs):
        free = manager._lock.acquire(blocking=False)
        if free:
            manager._lock.release()
        seen.append((Path(path).name, free))
        real(path, *args, **kwargs)

    monkeypatch.setattr(projects.shutil, "rmtree", observed)
    result = projects.cleanup(manager)
    assert result["freed_bytes"] == 10000
    assert sorted(seen) == [(f".deleting-{ident}-1", True), (f".deleting-{ident}-2", True)], "rmtree runs on the moved folder, with the lock free"
    assert not list(manager.root.glob(".deleting-*"))
    assert (manager.root / ident / "render-3" / "video.mp4").is_file()


def test_sweep_finishes_a_render_cleanup_a_crash_interrupted(manager: CampaignManager, workspace: Path) -> None:
    ident = verified_job(manager, upload(workspace))
    leftover = manager.root / f".deleting-{ident}-1"
    leftover.mkdir()
    (leftover / "video.mp4").write_bytes(b"x" * 100)
    unrelated = manager.root / ".deleting-not-an-id"
    unrelated.mkdir()
    projects.library(manager)
    assert not leftover.exists()
    assert unrelated.is_dir(), "only folders a delete or cleanup named are swept"


# ---- Orphan uploads ----


def _age(path: Path, hours: float) -> None:
    then = time.time() - hours * 3600
    for item in [path, path / "inbox", *(path / "inbox").iterdir()]:
        os.utime(item, (then, then), follow_symlinks=False)


def test_an_upload_no_project_names_is_reclaimed_after_a_day(manager: CampaignManager, workspace: Path) -> None:
    used = upload(workspace, folder="source-used")
    add_job(manager, used)
    orphan = upload(workspace, "big.mov", folder="source-orphan", data=b"o" * 5000)
    fresh = upload(workspace, folder="source-fresh")
    _age(orphan.parent.parent, 25)
    _age(used.parent.parent, 25)
    _age(fresh.parent.parent, 23)
    assert projects.library(manager)["storage"]["reclaimable_bytes"] == 5000
    preview = projects.cleanup(manager, dry_run=True)
    assert preview["orphans"] == [{"folder": "source-orphan", "bytes": 5000}] and preview["freed_bytes"] == 5000
    assert orphan.is_file(), "a preview removes nothing"
    result = projects.cleanup(manager)
    assert result["orphans"] == [{"folder": "source-orphan", "bytes": 5000}] and result["freed_bytes"] == 5000
    assert not orphan.parent.parent.exists()
    assert used.is_file() and fresh.is_file()
    assert projects.cleanup(manager)["orphans"] == []


def test_a_script_projects_pictures_are_not_orphans(manager: CampaignManager, workspace: Path) -> None:
    picture = upload(workspace, "scene.png", folder="source-picture", data=b"\x89PNG" * 10)
    ident = add_job(manager, upload(workspace, folder="source-clip"))
    tampered(manager, ident, request={"paths": [], "goal": "A script", "budget_usd": 0.5, "provider": "meta", "script": {"text": "One.", "scenes": [], "voice": None, "images": [str(picture)]}})
    _age(picture.parent.parent, 48)
    assert projects.cleanup(manager)["orphans"] == []
    assert picture.is_file()


@pytest.mark.parametrize("shape", ["session_files", "subfolder", "linked_folder", "hidden"])
def test_only_a_bare_inbox_folder_counts_as_an_orphan(manager: CampaignManager, workspace: Path, outside: Path, shape: str) -> None:
    if shape == "linked_folder":
        (sessions(workspace) / "source-linked").symlink_to(outside, target_is_directory=True)
        (outside / "inbox").mkdir()
        (outside / "inbox" / "clip.mp4").write_bytes(b"x")
        folder = sessions(workspace) / "source-linked"
    else:
        name = ".source-hidden" if shape == "hidden" else "edit-20261001"
        clip = upload(workspace, folder=name)
        folder = clip.parent.parent
        if shape == "session_files":
            (folder / "session.json").write_text("{}", encoding="utf-8")
        elif shape == "subfolder":
            (folder / "inbox" / "more").mkdir()
        _age(folder, 48)
    result = projects.cleanup(manager)
    assert result["orphans"] == []
    assert (outside / "keep.txt").is_file()
    assert folder.exists() or folder.is_symlink()


def test_a_failed_attempt_newer_than_the_latest_verified_is_kept(manager: CampaignManager, workspace: Path) -> None:
    ident = verified_job(manager, upload(workspace), revisions=2)
    add_render(manager, ident, 3)
    tampered(manager, ident, status="failed", revision=3, error="The render failed.")
    projects.cleanup(manager)
    assert (manager.root / ident / "render-2" / "video.mp4").is_file()
    assert (manager.root / ident / "render-3").is_dir()
    assert not (manager.root / ident / "render-1").exists()


def test_a_newer_render_in_progress_leaves_the_project_alone(manager: CampaignManager, workspace: Path) -> None:
    ident = verified_job(manager, upload(workspace))
    tampered(manager, ident, status="rendering", revision=4)
    assert projects.cleanup(manager)["removed"] == []
    assert (manager.root / ident / "render-1").is_dir()


def test_nothing_verified_means_nothing_is_old(manager: CampaignManager, workspace: Path) -> None:
    ident = add_job(manager, upload(workspace), status="failed", revision=2)
    add_render(manager, ident, 1)
    assert projects.cleanup(manager)["removed"] == []
    assert (manager.root / ident / "render-1").is_dir()


def test_a_render_the_current_artifacts_point_at_is_protected(manager: CampaignManager, workspace: Path) -> None:
    """An older project may have its current files in an earlier folder."""
    ident = verified_job(manager, upload(workspace))
    tampered(manager, ident, _artifacts={"video": "render-1/video.mp4"})
    projects.cleanup(manager)
    assert (manager.root / ident / "render-1" / "video.mp4").is_file()


def test_cleanup_does_not_follow_a_linked_render(manager: CampaignManager, workspace: Path, outside: Path) -> None:
    ident = verified_job(manager, upload(workspace))
    (manager.root / ident / "render-2").rename(manager.root / ident / "moved")
    (manager.root / ident / "render-2").symlink_to(outside, target_is_directory=True)
    projects.cleanup(manager)
    assert (outside / "keep.txt").is_file()


def test_cleanup_only_touches_render_folders(manager: CampaignManager, workspace: Path) -> None:
    ident = verified_job(manager, upload(workspace))
    other = manager.root / ident / "render-notes"
    other.mkdir()
    projects.cleanup(manager)
    assert other.is_dir()


# ---- Thumbnails ----


def test_thumbnail_prefers_the_verified_cover(manager: CampaignManager, workspace: Path) -> None:
    ident = verified_job(manager, upload(workspace))
    assert projects.thumbnail(manager, ident) == (manager.root / ident / "render-3" / "cover.jpg").resolve()


def test_thumbnail_without_a_source_is_a_plain_not_found(manager: CampaignManager, workspace: Path) -> None:
    ident = add_job(manager, workspace / "vedit-sessions" / "gone" / "inbox" / "clip.mp4")
    with pytest.raises(CampaignError):
        projects.thumbnail(manager, ident)


@pytest.mark.render
def test_thumbnail_is_made_once_from_the_source(manager: CampaignManager, workspace: Path) -> None:
    from vedit.media import resolve_ffmpeg, run_ffmpeg

    resolve_ffmpeg()
    source = workspace / "vedit-sessions" / "s" / "inbox" / "clip.mp4"
    source.parent.mkdir(parents=True)
    run_ffmpeg(["-f", "lavfi", "-i", "testsrc2=size=320x240:rate=10:duration=1", "-pix_fmt", "yuv420p", str(source)])
    ident = add_job(manager, source, source={"name": "clip.mp4", "duration": 1.0, "has_video": True})
    made = projects.thumbnail(manager, ident)
    assert made == (manager.root / ident / "library-thumb.jpg").resolve() and made.stat().st_size > 0
    stamp = made.stat().st_mtime_ns
    assert projects.thumbnail(manager, ident).stat().st_mtime_ns == stamp


# ---- HTTP ----


def call(port: int, method: str, path: str, payload: object = None, headers: dict[str, str] | None = None) -> tuple[int, dict, bytes]:
    outgoing = dict(headers or {})
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
        outgoing.setdefault("Content-Type", "application/json")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(method, path, body=body, headers=outgoing)
        response = connection.getresponse()
        raw = response.read()
        return response.status, dict(response.getheaders()), raw
    finally:
        connection.close()


@pytest.fixture
def studio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[int, Path]]:
    root = tmp_path / "studio"
    root.mkdir()
    monkeypatch.chdir(root)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.delenv("VEDIT_META_ENABLED", raising=False)
    httpd = _LoopbackServer(("127.0.0.1", 0), StudioHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(httpd.server_address[1]), root.resolve()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)
        get_manager().close()


def test_http_library_rename_duplicate_delete(studio: tuple[int, Path]) -> None:
    port, root = studio
    manager = get_manager()
    source = upload(root)
    ident = verified_job(manager, source, revisions=2)
    status, _headers, raw = call(port, "GET", "/api/projects")
    body = json.loads(raw)
    assert status == 200 and body["projects"][0]["id"] == ident and body["storage"]["reclaimable_bytes"] == 5000
    status, _headers, raw = call(port, "POST", f"/api/projects/{ident}/rename", {"title": "Renamed"})
    assert status == 200 and json.loads(raw)["project"]["title"] == "Renamed"
    status, _headers, raw = call(port, "POST", f"/api/projects/{ident}/duplicate", {})
    assert status == 201
    copy = json.loads(raw)["job"]["id"]
    status, _headers, raw = call(port, "POST", "/api/projects/cleanup", {"dry_run": True})
    assert status == 200 and json.loads(raw)["freed_bytes"] == 5000
    status, _headers, raw = call(port, "POST", "/api/projects/cleanup", {})
    assert status == 200 and json.loads(raw)["removed"] == [{"id": ident, "revision": 1, "bytes": 5000}]
    status, headers, raw = call(port, "GET", f"/api/projects/{ident}/thumbnail")
    assert status == 200 and headers["Content-Type"].startswith("image/") and raw
    status, _headers, raw = call(port, "DELETE", f"/api/campaigns/{ident}")
    assert status == 200 and json.loads(raw)["deleted"] == ident
    assert source.is_file(), "the duplicate still uses it"
    assert call(port, "GET", f"/api/campaigns/{ident}")[0] == 404
    status, _headers, raw = call(port, "DELETE", f"/api/campaigns/{copy}")
    assert status == 200 and json.loads(raw)["source_removed"] == 1
    assert not source.exists()


def test_http_delete_is_refused_while_active(studio: tuple[int, Path]) -> None:
    port, root = studio
    ident = add_job(get_manager(), upload(root), status="rendering")
    status, _headers, raw = call(port, "DELETE", f"/api/campaigns/{ident}")
    assert status == 409 and "Cancel" in json.loads(raw)["error"]


@pytest.mark.parametrize("path", [
    "/api/campaigns/..%2F..%2F..%2Fetc", "/api/campaigns/%2e%2e", "/api/campaigns/..", "/api/campaigns/" + "0" * 32 + "/extra",
    "/api/campaigns", "/api/campaigns/", "/api/projects/" + "0" * 32,
])
def test_http_delete_of_anything_else_is_not_found(studio: tuple[int, Path], path: str) -> None:
    port, root = studio
    sentinel = root.parent / "sentinel.txt"
    sentinel.write_text("x", encoding="utf-8")
    assert call(port, "DELETE", path)[0] == 404
    assert sentinel.is_file()


@pytest.mark.parametrize(("method", "suffix", "payload"), [
    ("GET", "/api/projects", None),
    ("GET", "/api/projects/{id}/thumbnail", None),
    ("POST", "/api/projects/{id}/rename", {"title": "x"}),
    ("POST", "/api/projects/{id}/duplicate", {}),
    ("POST", "/api/projects/cleanup", {}),
    ("DELETE", "/api/campaigns/{id}", None),
])
@pytest.mark.parametrize("headers", [{"Host": "evil.example"}, {"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}])
def test_http_routes_refuse_other_origins(studio: tuple[int, Path], method: str, suffix: str, payload: object, headers: dict[str, str]) -> None:
    port, root = studio
    manager = get_manager()
    source = upload(root)
    ident = add_job(manager, source)
    assert call(port, method, suffix.replace("{id}", ident), payload, headers)[0] == 403
    assert source.is_file() and job_row(manager, ident)
    assert "name" not in job_row(manager, ident)


def test_http_mutations_need_json(studio: tuple[int, Path]) -> None:
    port, root = studio
    ident = add_job(get_manager(), upload(root))
    assert call(port, "POST", f"/api/projects/{ident}/rename", {"title": "x"}, {"Content-Type": "text/plain"})[0] == 415


def test_http_a_method_mismatch_says_so(studio: tuple[int, Path]) -> None:
    port, root = studio
    ident = add_job(get_manager(), upload(root))
    assert call(port, "GET", f"/api/projects/{ident}/rename")[0] == 405
    assert call(port, "GET", "/api/projects/cleanup")[0] == 405
    assert call(port, "POST", "/api/projects", {})[0] == 405


# ---- Restart ----


@pytest.mark.render
def test_retry_after_a_killed_render_replaces_its_partial_files(tmp_path: Path) -> None:
    """A killed encode leaves a file with no index behind. Retry must not read it."""
    from vedit.media import probe, run_ffmpeg

    root = tmp_path / "w"
    root.mkdir()
    source = root / "vedit-sessions" / "s" / "inbox" / "clip.mp4"
    source.parent.mkdir(parents=True)
    run_ffmpeg(["-f", "lavfi", "-i", "testsrc2=size=320x240:rate=15:duration=2", "-f", "lavfi", "-i", "sine=frequency=330:duration=2", "-shortest", "-pix_fmt", "yuv420p", str(source)])
    first = CampaignManager(root, start_worker=False)
    job = first.create({"paths": [str(source)], "goal": "A short cut", "target_duration": 1.5, "captions": False, "aspect": "16:9"})
    first.process(job["id"])
    assert first.get(job["id"])["status"] == "planned"
    queued = first.render(job["id"], {})
    work = first.root / job["id"] / f"render-{queued['revision']}"
    work.mkdir()
    # What ffmpeg had written when Studio was killed: a header, and no index.
    (work / "01_select_ranges.mp4").write_bytes(b"\x00\x00\x00\x20ftypisom" + b"\x00" * 36)
    with first._connect() as connection:
        connection.execute("UPDATE jobs SET status='rendering' WHERE id=?", (job["id"],))
    first.close()
    second = CampaignManager(root, start_worker=False)
    try:
        assert second.get(job["id"])["stage"] == "interrupted"
        second.retry(job["id"])
        second.process(job["id"])
        done = second.get(job["id"])
        assert done["status"] == "complete", done["error"]
        assert probe(second.artifact(job["id"], "video")).has_video
    finally:
        second.close()


@pytest.mark.render
def test_a_retry_never_clears_a_verified_version(tmp_path: Path) -> None:
    from vedit.media import run_ffmpeg

    root = tmp_path / "w"
    root.mkdir()
    source = root / "vedit-sessions" / "s" / "inbox" / "clip.mp4"
    source.parent.mkdir(parents=True)
    run_ffmpeg(["-f", "lavfi", "-i", "testsrc2=size=320x240:rate=15:duration=2", "-f", "lavfi", "-i", "sine=frequency=330:duration=2", "-shortest", "-pix_fmt", "yuv420p", str(source)])
    manager = CampaignManager(root, start_worker=False)
    try:
        job = manager.create({"paths": [str(source)], "goal": "A short cut", "target_duration": 1.5, "captions": False, "aspect": "16:9"})
        manager.process(job["id"])
        manager.render(job["id"], {})
        manager.process(job["id"])
        verified = manager.artifact(job["id"], "video", revision=1)
        second = manager.render(job["id"], {})
        assert second["revision"] == 2
        partial = manager.root / job["id"] / "render-2"
        partial.mkdir()
        (partial / "01_select_ranges.mp4").write_bytes(b"partial")
        with manager._lock, manager._connect() as connection:
            row = manager._load(connection, job["id"])
            row.update(status="failed", stage="interrupted", error="This job was interrupted.")
            manager._save(connection, row)
        manager.retry(job["id"])
        manager.process(job["id"])
        assert manager.get(job["id"])["status"] == "complete"
        assert verified.is_file() and manager.artifact(job["id"], "video", revision=2).is_file()
    finally:
        manager.close()


# ---- Projects made of several files ----


def test_duplicate_keeps_shared_sources_that_name_the_job(manager: CampaignManager, workspace: Path) -> None:
    """A script project's inbox folder holds its own id. The copy must keep pointing there."""
    ident = uuid.uuid4().hex
    first = upload(workspace, name="scene-01.mp4", folder=f"script-{ident}")
    second = first.with_name("scene-02.mp4")
    second.write_bytes(b"x")
    job = add_job(manager, first)
    tampered(manager, job, request={"paths": [str(first), str(second)], "goal": "Script", "budget_usd": 0.25})
    copy = projects.duplicate(manager, job)
    assert copy["request"]["paths"] == [str(first), str(second)]
    assert projects.delete(manager, job)["source_removed"] == 0
    assert first.is_file() and second.is_file()
    assert projects.delete(manager, copy["id"])["source_removed"] == 2
    assert not first.parent.parent.exists()


def test_duplicate_rewrites_asset_paths_and_leaves_the_family(manager: CampaignManager, workspace: Path) -> None:
    source = upload(workspace)
    job = add_job(manager, source, family=[{"id": "0" * 32, "role": "short", "index": 1, "title": "Short 1"}])
    asset = manager.root / job / "assets" / "logo.png"
    asset.parent.mkdir()
    asset.write_bytes(b"logo")
    tampered(manager, job, assets={"a1": {"path": str(asset), "kind": "image"}}, request={"paths": [str(source)], "goal": "x", "budget_usd": 0.25, "short": {"parent": "1" * 32}})
    copy = projects.duplicate(manager, job)
    stored = job_row(manager, copy["id"])
    assert Path(stored["assets"]["a1"]["path"]) == manager.root.resolve() / copy["id"] / "assets" / "logo.png"
    assert Path(stored["assets"]["a1"]["path"]).read_bytes() == b"logo"
    assert "short" not in stored["request"] and "family" not in stored


def test_a_parent_waits_for_its_running_shorts(manager: CampaignManager, workspace: Path) -> None:
    source = upload(workspace)
    parent = add_job(manager, source)
    short = add_job(manager, source, status="analyzing")
    tampered(manager, short, request={"paths": [str(source)], "goal": "x", "budget_usd": 0.25, "short": {"parent": parent}})
    with pytest.raises(CampaignError) as caught:
        projects.delete(manager, parent)
    assert caught.value.status == 409 and "shorts" in str(caught.value)


def test_a_deleted_short_leaves_its_family(manager: CampaignManager, workspace: Path) -> None:
    source = upload(workspace)
    parent = add_job(manager, source)
    short = add_job(manager, source, family=[{"id": parent, "role": "source", "index": 0, "title": "Original"}])
    other = add_job(manager, source, family=[{"id": parent, "role": "source", "index": 0, "title": "Original"}, {"id": short, "role": "short", "index": 1, "title": "Short 1"}])
    tampered(manager, parent, family=[{"id": short, "role": "short", "index": 1, "title": "Short 1"}, {"id": other, "role": "short", "index": 2, "title": "Short 2"}])
    projects.delete(manager, short)
    assert [row["id"] for row in job_row(manager, parent)["family"]] == [other]
    assert [row["id"] for row in job_row(manager, other)["family"]] == [parent]
    assert source.is_file()
