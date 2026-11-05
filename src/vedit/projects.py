"""The project library: what each campaign is, what it costs on disk, and safe ways to change that.

Everything that removes a file lives here and follows the same rules: a job
folder is the campaigns folder plus a validated id, a stored path is trusted
only after it resolves to exactly ``<sessions>/<folder>/inbox/<file>``, and a
link is removed as a link, never followed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import threading
import time
import uuid
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import Any

from vedit.campaign import _ACTIVE, CampaignError, CampaignManager, _json, _now, _text
from vedit.errors import VeditError
from vedit.media import run_ffmpeg

_ID = re.compile(r"[a-f0-9]{32}")
_RENDER = re.compile(r"render-([1-9][0-9]*)")
_TRASH = ".deleting-"
# A job folder being deleted, or one render folder cleanup moved out of a job before removing it.
_TRASHED = re.compile(r"\.deleting-[a-f0-9]{32}(-[1-9][0-9]*)?")
# An upload nothing names is kept this long, so a project being created right now is never swept.
_ORPHAN_AGE_SECONDS = 24 * 60 * 60
# A copy starts its own history: no renders, no ledger, and no generated cover
# bound to the original's folder. Analysis copies regenerate when needed.
_NOT_COPIED = re.compile(r"render-\d+|analysis|meta|cover-\d+\.json|shorts-.*\.json|library-thumb\.jpg|\.deleting-.*")
_LINK_FROM_BYTES = 16 * 1024 * 1024
_LIMIT = 500
_THUMBNAIL_SLOTS = threading.BoundedSemaphore(2)
_LABELS = {
    "queued": "Queued", "analyzing": "Shaping the story", "planned": "Ready to edit", "rendering": "Making the video",
    "complete": "Verified", "failed": "Needs attention", "cancelled": "Cancelled", "interrupted": "Interrupted",
}


def _folder_size(path: Path, seen: set[tuple[int, int]] | None = None) -> int:
    """Bytes of regular files under a folder. Links are not followed; a hard link counts once."""
    seen = set() if seen is None else seen
    total = 0
    for root, _directories, files in os.walk(path, followlinks=False):
        for name in files:
            with suppress(OSError):
                info = os.lstat(os.path.join(root, name))
                if stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) not in seen:
                    seen.add((info.st_dev, info.st_ino))
                    total += info.st_size
    return total


def _job_folder(manager: CampaignManager, ident: Any) -> Path:
    if not isinstance(ident, str) or not _ID.fullmatch(ident):
        raise CampaignError("Campaign not found.", 404)
    return manager.root.resolve() / ident


def _inbox_path(manager: CampaignManager, raw: Any) -> Path | None:
    """A stored upload path, only when it is exactly ``<sessions>/<folder>/inbox/<file>``."""
    if not isinstance(raw, str) or not raw:
        return None
    path = Path(raw)
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        return None
    # Resolving the parent sees through `..` and a linked folder; the name stays
    # unresolved so that a linked file is removed as a link.
    parent = path.parent.resolve()
    try:
        relative = parent.relative_to(manager.sessions.resolve())
    except ValueError:
        return None
    if len(relative.parts) != 2 or relative.parts[1] != "inbox" or relative.parts[0] == "campaigns":
        return None
    return parent / path.name


def _stored_paths(job: dict[str, Any]) -> list[Any]:
    """Every upload a job's request names: its clips, and a script's own pictures."""
    request = job.get("request")
    if not isinstance(request, dict):
        return []
    paths = request.get("paths")
    found = list(paths) if isinstance(paths, list) else []
    script = request.get("script")
    images = script.get("images") if isinstance(script, dict) else None
    if isinstance(images, list):
        found.extend(images)
    return found


def _old_renders(manager: CampaignManager, job: dict[str, Any]) -> list[tuple[int, Path]]:
    """Render folders older than the latest verified version. Empty for anything in progress."""
    if job["status"] in _ACTIVE:
        return []
    latest = manager._public(job).get("verified_revision")
    if not isinstance(latest, int) or isinstance(latest, bool):
        return []
    folder = _job_folder(manager, job["id"])
    current = [job.get("_artifacts") or {}, (job.get("_verified_artifacts") or {}).get(str(latest)) or {}]
    protected = {PurePosixPath(value).parts[0] for artifacts in current for value in artifacts.values() if isinstance(value, str) and value}
    found = []
    with suppress(OSError):
        with os.scandir(folder) as entries:
            for entry in entries:
                match = _RENDER.fullmatch(entry.name)
                if match and int(match.group(1)) < latest and entry.name not in protected and entry.is_dir(follow_symlinks=False):
                    found.append((int(match.group(1)), Path(entry.path)))
    return sorted(found)


def _row(manager: CampaignManager, job: dict[str, Any]) -> dict[str, Any]:
    public = manager._public(job)
    plan = job.get("plan") if isinstance(job.get("plan"), dict) else {}
    source = job.get("source") if isinstance(job.get("source"), dict) else {}
    request = job.get("request") if isinstance(job.get("request"), dict) else {}
    result = public.get("result") if isinstance(public.get("result"), dict) else {}
    proof = result.get("proof") if isinstance(result.get("proof"), dict) else {}
    status = "interrupted" if job["status"] == "failed" and job.get("stage") == "interrupted" else job["status"]
    folder = _job_folder(manager, job["id"])
    seen: set[tuple[int, int]] = set()
    size = _folder_size(folder, seen)
    for raw in _stored_paths(job):
        path = _inbox_path(manager, raw)
        if path is not None:
            with suppress(OSError):
                info = path.lstat()
                if stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) not in seen:
                    seen.add((info.st_dev, info.st_ino))
                    size += info.st_size
    return {
        "id": job["id"],
        "title": job.get("name") or plan.get("title") or source.get("name") or request.get("goal") or "Untitled project",
        "status": status, "status_label": _LABELS.get(status, status),
        "created_at": job.get("created_at"), "updated_at": job.get("updated_at"),
        "duration": proof.get("duration") or plan.get("duration") or source.get("duration"),
        "source_name": source.get("name"),
        "revision": job.get("revision", 0), "verified_revision": public.get("verified_revision"),
        "versions": sum(1 for entry in public["versions"] if entry.get("status") == "verified"),
        "error": job.get("error"), "active": job["status"] in _ACTIVE,
        "size_bytes": size,
        "reclaimable_bytes": sum(_folder_size(path) for _revision, path in _old_renders(manager, job)),
        "thumbnail_url": f"/api/projects/{job['id']}/thumbnail",
    }


def library(manager: CampaignManager) -> dict[str, Any]:
    """Every project, newest first, and what the workspace holds on disk."""
    _sweep(manager)
    with manager._lock, manager._connect() as connection:
        rows = connection.execute("SELECT data FROM jobs ORDER BY updated_at DESC LIMIT ?", (_LIMIT,)).fetchall()
        every = [json.loads(row[0]) for row in connection.execute("SELECT data FROM jobs").fetchall()]
    projects = [_row(manager, json.loads(row[0])) for row in rows]
    orphan_bytes = sum(size for _folder, size in _orphan_inboxes(manager, every))
    return {
        "projects": projects,
        "storage": {
            "total_bytes": _folder_size(manager.sessions),
            "reclaimable_bytes": sum(project["reclaimable_bytes"] for project in projects) + orphan_bytes,
            "orphan_bytes": orphan_bytes,
            "workspace": str(manager.workspace),
        },
        "interrupted": sum(1 for project in projects if project["status"] == "interrupted"),
    }


def thumbnail(manager: CampaignManager, ident: str) -> Path:
    """The verified cover, or a small frame of the source made once and kept with the job."""
    with suppress(CampaignError, OSError):
        return manager.artifact(ident, "cover")
    folder = _job_folder(manager, ident)
    cached = folder / "library-thumb.jpg"
    if cached.is_file() and not cached.is_symlink() and cached.stat().st_size:
        return cached
    with manager._lock, manager._connect() as connection:
        job = manager._load(connection, ident)
    duration = (job.get("source") or {}).get("duration")
    request = job.get("request") if isinstance(job.get("request"), dict) else {}
    # A script project names pictures, not a clip, so only a clip or a combined project file has a frame.
    if not (request.get("paths") or job.get("_project_file")) or not isinstance(duration, (int, float)):
        raise CampaignError("This project has no picture yet.", 404)
    source = manager._project_source(job)
    pending = folder / ".library-thumb.part.jpg"
    with _THUMBNAIL_SLOTS:
        try:
            run_ffmpeg(["-ss", f"{min(duration * 0.1, 2.0):.3f}", "-i", str(source), "-frames:v", "1", "-vf", "scale=480:-2", str(pending)], timeout=30)
            pending.replace(cached)
        except (VeditError, OSError) as exc:
            with suppress(OSError):
                pending.unlink()
            raise CampaignError("This project has no picture yet.", 404) from exc
    return cached


def rename(manager: CampaignManager, ident: str, value: Any) -> dict[str, Any]:
    """Name the project. The video's own title stays in the plan and does not change."""
    if not isinstance(value, dict):
        raise CampaignError("Send a JSON object with a title.")
    title = _text(value.get("title", ""), "Project name", 120, required=True)
    with manager._lock, manager._connect() as connection:
        job = manager._load(connection, ident)
        job["name"] = title
        manager._save(connection, job)
    return _row(manager, job)


def _copy_file(source: str, destination: str) -> None:
    # Clips can be gigabytes. A hard link is free, and neither side edits a media file in place.
    if os.path.getsize(source) >= _LINK_FROM_BYTES:
        with suppress(OSError):
            os.link(source, destination)
            return
    shutil.copy2(source, destination)


def _skip_copied(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if _NOT_COPIED.fullmatch(name)}


def duplicate(manager: CampaignManager, ident: str) -> dict[str, Any]:
    """A new project at the review step with the same plan and the same stored source.

    It has no renders, versions, or spend of its own yet, and shares the inbox
    copy with the original, so deleting one keeps the file for the other.
    """
    folder = _job_folder(manager, ident)
    with manager._lock, manager._connect() as connection:
        job = manager._load(connection, ident)
        if job["status"] in _ACTIVE:
            raise CampaignError("Wait for this project to finish, then duplicate it.", 409)
        if not job.get("plan") or not job.get("source"):
            raise CampaignError("Create a story plan before duplicating this project.", 409)
        new_id, now = uuid.uuid4().hex, _now()
        # Stored URLs and asset paths name the job, so the new id replaces the old one
        # throughout. The request is the exception: it names files both projects share.
        copy = json.loads(_json(job).replace(ident, new_id))
        copy["request"] = {key: value for key, value in json.loads(_json(job["request"])).items() if key != "short"}
        for name in ("versions", "applied_patches", "note", "note_summary", "recovery", "revision_source", "timings", "storage", "family"):
            copy.pop(name, None)
        budget = job["request"]["budget_usd"]
        fingerprint = hashlib.sha256(f"duplicate|{new_id}".encode()).hexdigest()
        title = job.get("name") or job["plan"].get("title") or "Untitled project"
        copy.update(
            id=new_id, status="planned", stage="review", progress=1, message="Review the story and approve the render.",
            created_at=now, updated_at=now, result=None, error=None, cancel_requested=False, revision=0, verified_revision=None,
            budget={"cap_usd": budget, "reserved_usd": 0, "remaining_usd": budget}, name=f"{title} copy"[:120], duplicated_from=ident,
            _action="plan", _artifacts={}, _verified_artifacts={}, _fingerprint=fingerprint,
        )
        destination = manager.root.resolve() / new_id
        try:
            if folder.is_dir() and not folder.is_symlink():
                shutil.copytree(folder, destination, symlinks=True, ignore=_skip_copied, copy_function=_copy_file)
            else:
                destination.mkdir()
            connection.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?)", (new_id, fingerprint, copy["status"], now, now, _json(copy)))
        except OSError as exc:
            shutil.rmtree(destination, ignore_errors=True)
            raise CampaignError("The project could not be copied. Check free disk space and try again.") from exc
    return manager._public(copy)


def _removable_inbox(manager: CampaignManager, connection: Any, job: dict[str, Any]) -> tuple[list[Path], int]:
    """Inbox files only this job uses. Any mention in any other job's record keeps the file."""
    others = [row[0] for row in connection.execute("SELECT data FROM jobs WHERE id<>?", (job["id"],))]
    removable, kept = [], 0
    for raw in _stored_paths(job):
        path = _inbox_path(manager, raw)
        if path is None:
            kept += 1
            continue
        names = {json.dumps(item, ensure_ascii=False)[1:-1] for item in (raw, str(path))}
        shared = any(name in data for name in names for data in others)
        # A session folder holding anything else belongs to an older workflow too.
        try:
            alone = all(item.name == "inbox" for item in path.parent.parent.iterdir())
        except OSError:
            alone = False
        if shared or not alone:
            kept += 1
        else:
            removable.append(path)
    return removable, kept


def _remove_inbox(paths: list[Path]) -> int:
    freed = 0
    for path in paths:
        with suppress(OSError):
            info = path.lstat()
            path.unlink()
            freed += info.st_size if stat.S_ISREG(info.st_mode) else 0
        # rmdir, never rmtree: a folder with anything left in it stays.
        with suppress(OSError):
            path.parent.rmdir()
            path.parent.parent.rmdir()
    return freed


def _orphan_inboxes(manager: CampaignManager, jobs: list[dict[str, Any]]) -> list[tuple[Path, int]]:
    """Upload folders no job names, a day or more old: ``<sessions>/<folder>/inbox/<files>`` and nothing else.

    Every upload makes such a folder; a project that is never created, or an
    asset the project copied, leaves it behind. Any mention of the folder in any
    job record keeps it, as does anything beside the inbox, which marks an
    older workflow's session.
    """
    sessions = manager.sessions.resolve()
    named = set()
    for job in jobs:
        for raw in _stored_paths(job):
            path = _inbox_path(manager, raw)
            if path is not None:
                named.add(path.parent.parent.name)
    records = [_json(job) for job in jobs]
    cutoff = time.time() - _ORPHAN_AGE_SECONDS
    found = []
    with suppress(OSError):
        for entry in os.scandir(sessions):
            if entry.name == "campaigns" or entry.name.startswith(".") or entry.name in named or not entry.is_dir(follow_symlinks=False):
                continue
            if any(entry.name in data for data in records):
                continue
            folder = sessions / entry.name
            try:
                children = list(os.scandir(folder))
                inbox = folder / "inbox"
                if [child.name for child in children] != ["inbox"] or not inbox.is_dir() or inbox.is_symlink():
                    continue
                files = list(os.scandir(inbox))
                if any(child.is_dir(follow_symlinks=False) for child in files):
                    continue
                newest = max([entry.stat(follow_symlinks=False).st_mtime, inbox.lstat().st_mtime] + [child.stat(follow_symlinks=False).st_mtime for child in files])
            except OSError:
                continue
            if newest > cutoff:
                continue
            size = sum(child.stat(follow_symlinks=False).st_size for child in files if child.is_file(follow_symlinks=False))
            found.append((folder, size))
    return sorted(found)


def _remove_orphan(folder: Path) -> int:
    """Remove an orphan upload folder the way ``_remove_inbox`` does: unlink each file, then rmdir, never rmtree."""
    freed = 0
    inbox = folder / "inbox"
    with suppress(OSError):
        for child in list(os.scandir(inbox)):
            path = inbox / child.name
            with suppress(OSError):
                info = path.lstat()
                path.unlink()
                freed += info.st_size if stat.S_ISREG(info.st_mode) else 0
    with suppress(OSError):
        inbox.rmdir()
        folder.rmdir()
    return freed


def _sweep(manager: CampaignManager) -> None:
    """Finish a delete or cleanup a crash interrupted. Only `.deleting-<id>` and `.deleting-<id>-<n>` folders are touched."""
    with suppress(OSError):
        for entry in manager.root.iterdir():
            if _TRASHED.fullmatch(entry.name) and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)


def delete(manager: CampaignManager, ident: str) -> dict[str, Any]:
    """Remove a project: its record, its job folder, and an inbox copy nothing else uses."""
    folder = _job_folder(manager, ident)
    trash = folder.with_name(_TRASH + ident)
    size = 0
    with manager._lock, manager._connect() as connection:
        job = manager._load(connection, ident)
        if job["status"] in _ACTIVE:
            raise CampaignError("This project is still working. Cancel it, then delete it.", 409)
        # A short reads its parent's record while it plans.
        working = connection.execute("SELECT data FROM jobs WHERE status IN ('queued','analyzing','rendering')").fetchall()
        if any(((json.loads(row[0]).get("request") or {}).get("short") or {}).get("parent") == ident for row in working):
            raise CampaignError("This project's shorts are still working. Wait for them, then delete it.", 409)
        removable, kept = _removable_inbox(manager, connection, job)
        connection.execute("DELETE FROM jobs WHERE id=?", (ident,))
        for row in connection.execute("SELECT data FROM jobs WHERE data LIKE ?", (f'%"family"%{ident}%',)).fetchall():
            relative = json.loads(row[0])
            relative["family"] = [item for item in relative.get("family") or [] if not (isinstance(item, dict) and item.get("id") == ident)]
            manager._save(connection, relative)
        try:
            if folder.is_symlink():
                # A linked job folder is not ours to empty.
                folder.unlink()
            elif folder.is_dir():
                size = _folder_size(folder)
                # One atomic step: a crash leaves either the job or a folder _sweep can finish.
                folder.rename(trash)
        except OSError as exc:
            connection.rollback()
            raise CampaignError("This project's folder could not be removed. Check its permissions and try again.") from exc
    shutil.rmtree(trash, ignore_errors=True)
    size += _remove_inbox(removable)
    return {"deleted": ident, "freed_bytes": size, "source_removed": len(removable), "source_kept": kept}


def cleanup(manager: CampaignManager, *, dry_run: bool = False) -> dict[str, Any]:
    """Delete renders of versions older than each project's latest verified one.

    The latest verified version is never touched; neither is any project still
    working, or a failed attempt newer than it. The older versions leave the
    history, because their files no longer exist. Upload folders no project
    names and a day old go too; the result lists them as ``orphans``.
    """
    _sweep(manager)
    with manager._lock, manager._connect() as connection:
        jobs = [json.loads(row[0]) for row in connection.execute("SELECT data FROM jobs").fetchall()]
    freed, removed, changed = 0, [], []
    for job in jobs:
        for revision, path in _old_renders(manager, job):
            size = _folder_size(path)
            freed += size
            removed.append({"id": job["id"], "revision": revision, "bytes": size})
            changed.append(job["id"])
    orphans = [{"folder": folder.name, "bytes": size} for folder, size in _orphan_inboxes(manager, jobs)]
    freed += sum(item["bytes"] for item in orphans)
    if dry_run:
        return {"freed_bytes": freed, "removed": removed, "orphans": orphans, "dry_run": True}
    trash: list[Path] = []
    for ident in sorted(set(changed)):
        # Under the lock only the record changes and each folder moves aside in one
        # step. The slow rmtree runs after, so polls and the worker never wait on it.
        with manager._lock, manager._connect() as connection:
            job = manager._load(connection, ident)
            old = _old_renders(manager, job)
            if not old:
                continue
            gone = {revision for revision, _path in old}
            job["versions"] = [entry for entry in job.get("versions") or [] if not (isinstance(entry, dict) and entry.get("revision") in gone and entry.get("status") == "verified")]
            artifacts = dict(job.get("_verified_artifacts") or {})
            for revision in gone:
                artifacts.pop(str(revision), None)
            job["_verified_artifacts"] = artifacts
            manager._save(connection, job)
            for revision, path in old:
                aside = manager.root.resolve() / f"{_TRASH}{ident}-{revision}"
                with suppress(OSError):
                    path.rename(aside)
                    trash.append(aside)
    for aside in trash:
        shutil.rmtree(aside, ignore_errors=True)
    for item in orphans:
        _remove_orphan(manager.sessions.resolve() / item["folder"])
    return {"freed_bytes": freed, "removed": removed, "orphans": orphans, "dry_run": False}
