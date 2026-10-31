"""Briefcut as an MCP server for coding agents.

``vedit mcp`` speaks the Model Context Protocol over stdio and exposes the
local ``CampaignManager``: import a clip, plan a story, render it, read the
receipts, and fetch artifact paths. The HTTP Studio is never started; the
manager is the same process-wide one ``get_manager()`` returns, so the job
database and session folders are shared with Studio runs on the same
workspace (one process at a time holds the workspace lease).

The workspace is ``VEDIT_WORKSPACE`` when set, otherwise the current
directory, mirroring the Briefcut launcher. Every tool returns plain JSON;
every actionable failure is a tool error carrying the manager's message and
never a traceback.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator

import anyio

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore[no-redef]
    from mcp.server.fastmcp.exceptions import ToolError  # type: ignore[no-redef]

from vedit import __version__
from vedit.campaign import CampaignError, CampaignManager, get_manager
from vedit.errors import VeditError
from vedit.media import probe

SERVER_NAME = "briefcut"
TERMINAL_STATES = frozenset({"planned", "complete", "failed", "cancelled"})
POLL_SECONDS = 0.5
_MAX_SOURCE_BYTES = 200_000_000
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_ARTIFACTS = ("source", "video", "cover", "bundle", "brief", "report")

_STATE: dict[str, Any] = {"manager": None}
_STATE_LOCK = threading.Lock()


def workspace() -> Path:
    """The folder whose ``vedit-sessions/`` holds inbox clips and campaign jobs."""
    raw = os.environ.get("VEDIT_WORKSPACE", "").strip()
    return Path(raw).expanduser().resolve() if raw else Path.cwd().resolve()


def _manager() -> CampaignManager:
    """The process-wide manager for the workspace, created on first use.

    ``get_manager()`` keys managers by the current directory, so when
    ``VEDIT_WORKSPACE`` points elsewhere this moves into it first, exactly as
    the desktop launcher does before starting Studio.
    """
    with _STATE_LOCK:
        manager = _STATE["manager"]
        if manager is not None and not manager._closed:
            return manager
        target = workspace()
        if Path.cwd().resolve() != target:
            target.mkdir(parents=True, exist_ok=True)
            os.chdir(target)
        try:
            manager = get_manager()
        except CampaignError as exc:
            raise ToolError(str(exc)) from exc
        _STATE["manager"] = manager
        return manager


def shutdown(*, wait: bool = True) -> None:
    """Close the manager this server created, if any. Safe to call twice."""
    with _STATE_LOCK:
        manager, _STATE["manager"] = _STATE["manager"], None
    if manager is not None and not manager._closed:
        manager.close(wait=wait)


def _call(fn, *args: Any, **kwargs: Any) -> Any:
    """Run a manager call, turning its actionable failures into tool errors."""
    try:
        return fn(*args, **kwargs)
    except CampaignError as exc:
        raise ToolError(str(exc)) from exc
    except VeditError as exc:
        raise ToolError(str(exc)) from exc


def _safe_filename(raw: str) -> str:
    name = Path(raw.replace("\\", "/")).name
    name = _SAFE_NAME.sub("_", name).strip("._")
    return (name or "clip.mp4")[:120]


def _receipts(job: dict[str, Any]) -> dict[str, Any]:
    result = job.get("result")
    if not isinstance(result, dict):
        raise ToolError(f"Campaign {job['id']} is {job['status']}; receipts exist once a render has been verified. " + (job.get("error") or job.get("message") or ""))
    return {
        "job_id": job["id"],
        "status": job["status"],
        "revision": result.get("revision"),
        "verified_revision": job.get("verified_revision"),
        "stale": result.get("stale", False),
        "passed": result.get("passed"),
        "total": result.get("total"),
        "checks": result.get("checks") or [],
        "proof": result.get("proof"),
        "patches": result.get("patches") or [],
        "review": result.get("review"),
        "warnings": result.get("warnings") or [],
        "versions": job.get("versions") or [],
    }


@asynccontextmanager
async def _lifespan(_server: Any) -> AsyncIterator[dict[str, Any]]:
    try:
        yield {}
    finally:
        shutdown()


def build_server() -> MCPServer:
    """Create the MCP server with every Briefcut tool registered."""
    server = MCPServer(
        SERVER_NAME,
        instructions=(
            "Briefcut builds short videos locally from one clip. Flow: briefcut_import_clip -> "
            "briefcut_create_story -> briefcut_wait (status planned) -> optionally edit job.plan -> "
            "briefcut_render -> briefcut_wait (status complete) -> briefcut_receipts (checks and proof) -> "
            "briefcut_artifact_path to read video/cover/bundle/brief/report from disk. "
            "A job is verified only when receipts show passed == total and proof has_video; never claim a render "
            "worked from a status alone."
        ),
        version=__version__,
        lifespan=_lifespan,
    )

    @server.tool()
    def briefcut_import_clip(path: str) -> dict[str, Any]:
        """Copy a local video file into a fresh inbox folder the campaign manager accepts.

        The file must be at most 200 MB and probe as video (ffprobe finds a video
        stream). Returns ``paths`` (the one inbox path to pass to
        briefcut_create_story), ``bytes``, and the probed duration and frame size.
        """
        source = Path(path).expanduser()
        if not source.is_absolute():
            raise ToolError("Give an absolute path to the clip.")
        if not source.is_file():
            raise ToolError(f"No file at {source}.")
        size = source.stat().st_size
        if size == 0:
            raise ToolError("The clip is empty. Choose a media file.")
        if size > _MAX_SOURCE_BYTES:
            raise ToolError(f"The clip is {size / 1_000_000:.0f} MB; the limit is 200 MB. Trim or transcode it first.")
        try:
            info = probe(source)
        except VeditError as exc:
            raise ToolError(f"ffprobe cannot read {source.name}: {exc}") from exc
        video = info.video
        if video is None:
            raise ToolError(f"{source.name} has no video stream. Import a video, not audio.")
        manager = _manager()
        manager.sessions.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        root = Path(tempfile.mkdtemp(prefix=f"source-{stamp}-", dir=manager.sessions))
        inbox = root / "inbox"
        inbox.mkdir()
        dest = inbox / _safe_filename(source.name)
        try:
            shutil.copyfile(source, dest)
        except OSError as exc:
            shutil.rmtree(root, ignore_errors=True)
            raise ToolError(f"Could not copy the clip into the workspace: {exc}") from exc
        return {
            "paths": [str(dest)],
            "bytes": size,
            "duration": info.duration,
            "width": video.width,
            "height": video.height,
            "has_audio": info.audio is not None,
        }

    @server.tool()
    def briefcut_create_story(
        paths: list[str],
        goal: str,
        provider: str = "local",
        target_duration: float = 30,
        aspect: str = "9:16",
        captions: bool = True,
        caption_provider: str = "local",
        language: str = "en",
        subject: str = "",
        budget_usd: float = 0.25,
        generate_cover: bool = False,
        review: bool = False,
        effect: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Queue a story plan for one imported clip and return the job.

        ``paths`` is the single inbox path from briefcut_import_clip. ``provider``
        is ``local`` (deterministic, free) or ``meta`` (needs MODEL_API_KEY and
        VEDIT_META_ENABLED=1). ``aspect`` is 9:16, 16:9, or 1:1; ``target_duration``
        is 0.5-180 seconds. ``effect`` is an optional effect dict as Studio sends
        it. Planning runs in the background: call briefcut_wait until the status
        is ``planned``, then read ``job.plan`` and render it.
        """
        request: dict[str, Any] = {
            "paths": list(paths),
            "goal": goal,
            "provider": provider,
            "target_duration": target_duration,
            "aspect": aspect,
            "captions": captions,
            "caption_provider": caption_provider,
            "language": language,
            "subject": subject,
            "budget_usd": budget_usd,
            "generate_cover": generate_cover,
            "review": review,
        }
        if effect is not None:
            request["effect"] = effect
        return _call(_manager().create, request)

    @server.tool()
    def briefcut_get(job_id: str) -> dict[str, Any]:
        """Return the current public job: status, stage, progress, plan, result, versions."""
        return _call(_manager().get, job_id)

    @server.tool()
    def briefcut_list() -> dict[str, Any]:
        """List the most recent campaigns in this workspace as ``jobs``, newest first."""
        return {"jobs": _call(_manager().list)}

    @server.tool()
    async def briefcut_wait(job_id: str, timeout_seconds: float = 600) -> dict[str, Any]:
        """Poll the job every 0.5 s until it is planned, complete, failed, or cancelled.

        Returns the job in that terminal state. Raises a tool error if
        ``timeout_seconds`` passes first; the job keeps running, so call again.
        """
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        while True:
            job = _call(_manager().get, job_id)
            if job["status"] in TERMINAL_STATES:
                return job
            if time.monotonic() >= deadline:
                raise ToolError(f"Timed out after {timeout_seconds:g}s; campaign {job_id} is still {job['status']} ({job.get('message') or job.get('stage')}). Call briefcut_wait again or briefcut_cancel.")
            await anyio.sleep(POLL_SECONDS)

    @server.tool()
    def briefcut_render(job_id: str, plan: dict[str, Any] | None = None) -> dict[str, Any]:
        """Render a planned job, optionally from an edited plan, and return the job.

        ``plan`` defaults to the saved plan; pass a modified copy of ``job.plan``
        (segments, title, caption, hashtags) to render your own cut. Then call
        briefcut_wait until the status is ``complete`` and read briefcut_receipts.
        """
        payload: dict[str, Any] = {}
        if plan is not None:
            payload["plan"] = plan
        return _call(_manager().render, job_id, payload)

    @server.tool()
    def briefcut_revise(job_id: str, mode: str, note: str | None = None) -> dict[str, Any]:
        """Render version N+1 of a verified job from patches; version N stays available.

        ``mode`` is ``auto`` (apply the deterministic lint patches from the last
        receipts), ``director`` (apply Meta's review patches), or ``note`` (turn
        a free-text ``note`` into patches with Meta first). Returns the queued
        job; wait for ``complete`` and compare receipts.
        """
        payload: dict[str, Any] = {"mode": mode}
        if note is not None:
            payload["note"] = note
        return _call(_manager().revise, job_id, payload)

    @server.tool()
    def briefcut_receipts(job_id: str) -> dict[str, Any]:
        """Return the verification receipts of the last verified render.

        Includes ``revision``, ``passed``/``total`` with the named ``checks``,
        the ffprobe ``proof`` of the delivered file (duration, width, height,
        has_video, has_audio), lint and review ``patches`` that briefcut_revise can
        apply, and the ``versions`` history.
        """
        return _receipts(_call(_manager().get, job_id))

    @server.tool()
    def briefcut_artifact_path(job_id: str, name: str, revision: int | None = None) -> dict[str, Any]:
        """Return the absolute on-disk path of an artifact so you can read it directly.

        ``name`` is one of source, video, cover, bundle, brief, report.
        ``revision`` pins an older verified version; the default is the current one.
        """
        if name not in _ARTIFACTS:
            raise ToolError(f"Choose one of {', '.join(_ARTIFACTS)} as the artifact name.")
        path = _call(_manager().artifact, job_id, name, revision=revision)
        return {"path": str(path), "name": name, "revision": revision, "bytes": path.stat().st_size}

    @server.tool()
    def briefcut_cancel(job_id: str) -> dict[str, Any]:
        """Stop a queued or running job. Finished work and the saved plan are kept."""
        return _call(_manager().cancel, job_id)

    @server.tool()
    def briefcut_retry(job_id: str) -> dict[str, Any]:
        """Re-queue a failed or cancelled job from its saved source and plan."""
        return _call(_manager().retry, job_id)

    return server


def serve_stdio() -> int:
    """Run the MCP server on stdio until the client closes the pipe."""
    server = build_server()
    try:
        server.run("stdio")
    finally:
        shutdown()
    return 0
