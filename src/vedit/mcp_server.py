"""Briefcut as an MCP server for coding agents.

``vedit mcp`` speaks the Model Context Protocol over stdio and exposes the
in-process ``CampaignManager``: import a clip, plan a story, read and edit its
timeline, add assets, render it, read the receipts, and fetch artifact paths.
The HTTP Studio is never started; the manager is the same process-wide one
``get_manager()`` returns, so the job database and session folders are shared
with Studio runs on the same workspace (one process at a time holds the
workspace lease).

Every project runs on Meta models: Muse Spark plans and reviews, SAM 3.1
tracks and cuts out, Meta Voice Transcribe writes the captions, and Muse Image
draws. FFmpeg renders on this machine. There is no local mode: the create tools
default to Meta and refuse ``provider="local"``.

Edits follow the Studio rule: a tool builds a draft from the saved plan, and
only the manager validates it (``timeline`` for a dry run, ``render`` to keep
it). Nothing here writes a plan or reaches the compiler on its own.

The workspace is ``VEDIT_WORKSPACE`` when set, otherwise the current
directory, mirroring the Briefcut launcher. Every tool returns plain JSON;
every actionable failure is a tool error carrying the manager's message and
never a traceback.
"""

from __future__ import annotations

import functools
import inspect
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback
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
from vedit.campaign import NO_NARRATION, CampaignError, CampaignManager, capabilities, get_manager, product_request
from vedit.edit_spec import default_edit, merge_edit
from vedit.errors import VeditError
from vedit.lint import MAX_MOMENTS, apply_patches
from vedit.media import plain_failure, probe
from vedit.project_media import MAX_SOURCE_BYTES

SERVER_NAME = "briefcut"
TERMINAL_STATES = frozenset({"planned", "complete", "failed", "cancelled"})
POLL_SECONDS = 0.5
_MAX_SOURCE_BYTES = MAX_SOURCE_BYTES
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_ARTIFACTS = ("source", "video", "cover", "bundle", "brief", "report", "filmstrip")
_DETAILS = ("summary", "segments", "spans", "captions", "edit")
_MAX_PAGE = 200
_MAX_PATCHES = 2 * MAX_MOMENTS
# Lists in the edit that can grow long. The summary counts them instead of listing them.
_LONG_EDIT = (("removed",), ("captions", "edits"))
_SAME = object()

# What an unexpected error message must never carry to the agent: an Authorization header,
# a bearer token, a key=value secret, or a long opaque token such as an API key.
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(authorization|proxy-authorization)\b\s*[:=]\s*(?:bearer\s+|basic\s+)?\S+"),
    re.compile(r"(?i)\bbearer\s+\S+"),
    re.compile(r"(?i)\b([a-z0-9_-]*(?:api[_-]?key|token|secret|password|passwd|credential)s?)\b(['\"]?\s*[:=]\s*['\"]?)[^\s'\",;]+"),
    # 24+ characters mixing letters and digits: a key or token, not a Python name.
    re.compile(r"\b(?=[A-Za-z_\-]*\d)(?=[\d_\-]*[A-Za-z])[A-Za-z0-9_\-]{24,}\b"),
)
_SECRET_ENV = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", re.IGNORECASE)

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


def _scrub(text: str) -> str:
    """``text`` with anything that looks like a credential replaced by [redacted]."""
    for name, value in os.environ.items():
        if _SECRET_ENV.search(name) and len(value.strip()) >= 8:
            text = text.replace(value.strip(), "[redacted]")
    for number, pattern in enumerate(_SECRET_PATTERNS):
        if number == 2:
            text = pattern.sub(lambda match: match.group(1) + match.group(2) + "[redacted]", text)
        elif number == 0:
            text = pattern.sub(lambda match: match.group(1) + ": [redacted]", text)
        else:
            text = pattern.sub("[redacted]", text)
    return text


def _unexpected(where: str, exc: BaseException) -> ToolError:
    """A tool error for a bug: the exception type and one scrubbed line, never a traceback.

    The full traceback goes to stderr (scrubbed too), where the operator reads
    the server log; stdout is the MCP channel and stays untouched.
    """
    trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    print(f"briefcut: unexpected error in {where}:\n" + _scrub(trace), file=sys.stderr, flush=True)
    lines = [line.strip() for line in str(exc).splitlines() if line.strip()]
    summary = _scrub(lines[0])[:200] if lines else "no details"
    return ToolError(f"Briefcut hit an unexpected {type(exc).__name__} in {where}: {summary}. "
                     "Nothing was changed by this call. The full error is in the Briefcut server log. Retry once, and report it if it repeats.")


def _call(fn, *args: Any, **kwargs: Any) -> Any:
    """Run a manager call, turning its actionable failures into tool errors, and any other failure into a specific one."""
    try:
        return fn(*args, **kwargs)
    except CampaignError as exc:
        raise ToolError(str(exc)) from exc
    except VeditError as exc:
        raise ToolError(plain_failure(exc)) from exc
    except ToolError:
        raise
    except Exception as exc:
        raise _unexpected(getattr(fn, "__name__", "a Briefcut call"), exc) from None


def _guard(fn):
    """Wrap a tool so an unexpected exception reaches the agent as a specific, scrubbed message.

    The MCP library reports any other exception only as "Error executing tool <name>".
    """
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def run_async(*args: Any, **kwargs: Any) -> Any:
            try:
                return await fn(*args, **kwargs)
            except ToolError:
                raise
            except Exception as exc:
                raise _unexpected(fn.__name__, exc) from None
        return run_async

    @functools.wraps(fn)
    def run(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except Exception as exc:
            raise _unexpected(fn.__name__, exc) from None
    return run


def _tools(server: MCPServer):
    """``server.tool`` with every tool wrapped by ``_guard``."""
    def tool(*args: Any, **kwargs: Any):
        register = server.tool(*args, **kwargs)
        return lambda fn: register(_guard(fn))
    return tool


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


def _local_file(raw: str, what: str) -> tuple[Path, int]:
    """An absolute, non-empty local file within the upload limit, and its size."""
    source = Path(raw).expanduser()
    if not source.is_absolute():
        raise ToolError(f"Give an absolute path to the {what}.")
    if not source.is_file():
        raise ToolError(f"No file at {source}.")
    size = source.stat().st_size
    if size == 0:
        raise ToolError(f"The {what} is empty. Choose a media file.")
    if size > _MAX_SOURCE_BYTES:
        raise ToolError(f"The {what} is {size / 1_000_000:.0f} MB; the limit is {_MAX_SOURCE_BYTES / 1_000_000_000:g} GB. Trim or transcode it first.")
    return source, size


def _stage(manager: CampaignManager, source: Path, what: str) -> Path:
    """Copy a file into a fresh inbox folder. The manager reads uploads from nowhere else."""
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
        raise ToolError(f"Could not copy the {what} into the workspace: {exc}") from exc
    return dest


def _planned(job: dict[str, Any]) -> dict[str, Any]:
    plan = job.get("plan")
    if not isinstance(plan, dict) or not job.get("source"):
        raise ToolError(f"Campaign {job['id']} is {job['status']} and has no story plan yet. Call briefcut_wait until it is planned.")
    return plan


def _changed(value: Any, base: Any) -> Any:
    """The parts of ``value`` that differ from ``base``, or ``_SAME``."""
    if isinstance(value, dict) and isinstance(base, dict):
        out = {}
        for key, item in value.items():
            diff = _changed(item, base[key]) if key in base else item
            if diff is not _SAME:
                out[key] = diff
        return out or _SAME
    return _SAME if value == base else value


def _changes(before: Any, after: Any, prefix: str = "") -> list[dict[str, Any]]:
    """Each setting that differs, as a dotted path with its old and new value."""
    if isinstance(before, dict) and isinstance(after, dict):
        rows: list[dict[str, Any]] = []
        for key in list(before) + [key for key in after if key not in before]:
            rows.extend(_changes(before.get(key), after.get(key), f"{prefix}{key}."))
        return rows
    return [] if before == after else [{"path": prefix.rstrip("."), "before": before, "after": after}]


def _without_long(edit: dict[str, Any]) -> dict[str, Any]:
    trimmed = {key: dict(value) if isinstance(value, dict) else value for key, value in edit.items()}
    for path in _LONG_EDIT:
        holder = trimmed
        for key in path[:-1]:
            holder = holder.get(key) if isinstance(holder.get(key), dict) else {}
        holder.pop(path[-1], None)
    return trimmed


def _edit_summary(edit: dict[str, Any]) -> dict[str, Any]:
    """Only the settings that differ from a blank edit. Long lists are counted, not listed."""
    shown = _changed(_without_long(edit), _without_long(default_edit()))
    return {} if shown is _SAME else shown


def _moments(plan: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for index, segment in enumerate(plan.get("segments") or [], 1):
        row = {"segment": index, "start": segment.get("start"), "end": segment.get("end"), "label": segment.get("label")}
        for key in ("speed", "volume", "kind"):
            if segment.get(key) is not None:
                row[key] = segment[key]
        rows.append(row)
    return rows


def _cue(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key != "words"}


def _clock(view: dict[str, Any]) -> dict[str, Any]:
    return {"duration": view.get("duration"), "transition_seconds": view.get("transition_seconds"),
            "span_count": len(view.get("spans") or []), "caption_count": len(view.get("captions") or [])}


def _meta_uses(job: dict[str, Any], before: dict[str, Any], after: dict[str, Any], captions_on: bool) -> tuple[list[str], list[str]]:
    """What a draft newly sends to Meta at render, and what it asks for that will not happen.

    The person agreed to Meta when the project was created, so these are
    disclosed in the result rather than asked for again.
    """
    request = job["request"]
    uses: list[str] = []
    warnings: list[str] = []
    language = (after.get("captions") or {}).get("language")
    earlier = (before.get("captions") or {}).get("language")
    if isinstance(language, str) and language and language.lower() != str(request.get("language") or "").lower():
        if not captions_on:
            warnings.append("Captions are off, so the caption language has no effect. Pass captions true to burn them.")
        else:
            # Turning captions on also starts a translation that was set while they were off.
            if language != earlier or not request.get("captions"):
                uses.append(f"Translate the captions to {language}. The caption text goes to Meta on this project's budget.")
            cloud = capabilities()["meta"]
            if not cloud.get("available"):
                warnings.append(f"Translated captions need Meta, which is not available: {cloud.get('reason')} The render will fail until it is, or clear the caption language.")
    cutout = after.get("cutout") or {}
    old = before.get("cutout") or {}
    if cutout.get("mode", "none") != "none":
        if request.get("provider") != "meta":
            warnings.append("This project was not made with Meta, so the render skips the cutout. Create a new project to use one.")
        else:
            if old.get("mode", "none") == "none" or cutout.get("subject") != old.get("subject"):
                uses.append(f"Cut out \"{cutout.get('subject')}\". A proxy of the footage goes to Meta SAM on this project's budget, unless that subject was already tracked.")
            if not (capabilities()["meta"].get("features") or {}).get("cutout"):
                warnings.append("A cutout needs Meta and its mask decoder on this machine, and one is not ready. Check briefcut_status before rendering.")
    hero, old_hero = after.get("hero_word"), before.get("hero_word")
    if hero:
        if request.get("provider") != "meta":
            warnings.append("This project was not made with Meta, so the render leaves the hero word out. SAM must cut the subject out behind it.")
        elif not old_hero or any(hero.get(key) != old_hero.get(key) for key in ("subject", "start", "end")):
            uses.append(f"Cut out \"{hero.get('subject')}\" behind the hero word. A copy of its window only goes to Meta SAM on this project's budget.")
    if after.get("motion_card") and not capabilities().get("motion_cards"):
        warnings.append("This machine has no Blender, so the render leaves the motion card out. Install Blender 4.2 or later, or set VEDIT_BLENDER.")
    wants = (after.get("captions") or {}).get("emphasize") and not (after.get("captions") or {}).get("emphasis")
    if wants and not (before.get("captions") or {}).get("emphasize"):
        if not captions_on:
            warnings.append("Captions are off, so punch words have no effect. Pass captions true to burn them.")
        elif request.get("provider") != "meta":
            warnings.append("This project was not made with Meta, so the captions stay plain. Muse Spark picks the punch words.")
        else:
            uses.append("Pick a punch word in the captions. The caption text goes to Muse Spark on this project's budget.")
    return uses, warnings


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
            "Briefcut edits video with Meta models and renders it with FFmpeg on this machine. "
            "It needs a Meta API key: call briefcut_status first, and if meta.available is false, stop and tell the person to add a key (dev.meta.ai) in Studio Settings. "
            "Flow: briefcut_import_clip, briefcut_create_story, briefcut_wait until planned, briefcut_timeline to read the cut, "
            "briefcut_edit to change it (a dry run unless render is true) or briefcut_render, briefcut_wait until complete, "
            "briefcut_receipts, then briefcut_artifact_path to read files. "
            "More: briefcut_find_moments searches a project (SAM 3.1 checks what Muse Spark found), briefcut_make_shorts cuts 2 to 5 shorts, "
            "briefcut_script_to_video turns a script into a caption-led video, briefcut_add_asset adds music, a logo, or a picture, "
            "briefcut_hero_word proposes one spoken word to show behind the subject, briefcut_motion_card designs a 3D title card, "
            "and briefcut_revise renders version N+1 from fixes or a note. "
            "A render is verified only when receipts show passed == total and proof has_video. A status alone proves nothing. "
            "Creating a project sends its footage to Meta on the project's budget; each later tool or setting that sends more says so."
        ),
        version=__version__,
        lifespan=_lifespan,
    )
    tool = _tools(server)

    @tool()
    def briefcut_import_clip(path: str) -> dict[str, Any]:
        """Copy a video file on this machine into a fresh inbox folder the campaign manager accepts.

        The file must be at most 4 GB and probe as video (ffprobe finds a video
        stream). Returns ``paths`` (the one inbox path to pass to
        briefcut_create_story), ``bytes``, and the probed duration and frame size.
        """
        source, size = _local_file(path, "clip")
        try:
            info = probe(source)
        except VeditError as exc:
            raise ToolError(f"{source.name}: {plain_failure(exc)}") from exc
        video = info.video
        if video is None:
            raise ToolError(f"{source.name} has no video stream. Import a video, not audio.")
        dest = _stage(_manager(), source, "clip")
        return {
            "paths": [str(dest)],
            "bytes": size,
            "duration": info.duration,
            "width": video.width,
            "height": video.height,
            "has_audio": info.audio is not None,
        }

    @tool()
    def briefcut_create_story(
        paths: list[str],
        goal: str,
        provider: str = "meta",
        target_duration: float = 30,
        aspect: str = "9:16",
        captions: bool = True,
        caption_provider: str = "meta",
        language: str | None = None,
        subject: str = "",
        budget_usd: float | None = None,
        generate_cover: bool = False,
        review: bool = False,
        effect: dict[str, Any] | None = None,
        director: str | None = None,
    ) -> dict[str, Any]:
        """Queue a story plan from one or more imported clips and return the job.

        ``paths`` contains inbox paths returned by briefcut_import_clip. Pass 1 to 30 clips or photos.
        The footage goes to Meta: Muse Spark plans the story and, with
        ``captions``, Meta Voice Transcribe writes the captions, all on this
        project's ``budget_usd``. Needs a Meta API key (briefcut_status shows
        ``meta.available``). ``provider`` and ``caption_provider`` are always
        ``meta``; there is no local mode. ``aspect`` is 9:16, 16:9, or 1:1;
        ``target_duration`` is 0.5-180 seconds. ``effect`` is an optional effect
        dict as Studio sends it. ``director`` picks who reviews the cut when
        ``review`` is on: ``spark`` (Muse Spark, the default) or ``wonder``
        (Muse Wonder, which also listens; it needs a ``budget_usd`` of about
        $0.40 or more, and a lower budget gets a note saying what to raise it to).
        ``language`` is the spoken language code, or ``auto`` to let Meta detect
        it; ``meta.languages`` in briefcut_status lists the codes (``heard`` marks
        the ones Voice Transcribe hears). Leave ``language`` and ``budget_usd``
        out to use the defaults saved in Studio Settings.
        Long sources work, up to the 2-hour project limit (talks, podcasts,
        lectures, vlogs). Past 10 minutes, Muse Spark watches the footage in
        parts of up to 10 minutes and logs candidate moments, then one text-only
        editor call (Muse Wonder when ``director`` is ``wonder``) picks them over
        the whole transcript. The expected Meta cost is worked out first: a
        ``budget_usd`` that cannot cover it fails the job with the amount to
        raise it to, before anything is sent. ``job.message`` names the part
        being read ("Planning 3 of 9: 20:00 to 30:00").
        Planning runs in the background: call briefcut_wait until the status is
        ``planned``, then read ``job.plan`` and render it.
        """
        request: dict[str, Any] = {
            "paths": list(paths),
            "goal": goal,
            "provider": provider,
            "target_duration": target_duration,
            "aspect": aspect,
            "captions": captions,
            "caption_provider": caption_provider,
            "subject": subject,
            "generate_cover": generate_cover,
            "review": review,
        }
        # Left out, the project takes the person's Settings defaults rather than a value fixed here.
        if language is not None:
            request["language"] = language
        if budget_usd is not None:
            request["budget_usd"] = budget_usd
        if effect is not None:
            request["effect"] = effect
        if director is not None:
            request["director"] = director
        # Refused before the manager opens, so a local request never takes the workspace lease.
        checked = _call(product_request, request)
        return _call(_manager().create, checked)

    @tool()
    def briefcut_find_moments(job_id: str, query: str, verify: bool = True, drop_unseen: bool = False) -> dict[str, Any]:
        """Find moments matching words or visible content in an existing project.

        Searching the project's transcript is free. Muse Spark can also search
        the footage. With ``verify`` (the default), SAM 3.1 then looks for the
        searched thing in up to 5 moments Spark found, on the project's budget
        (about $0.01 per moment, nothing for a search about speech). Each
        moment carries ``verified`` (true, false or null when not checked),
        ``coverage`` (share of sampled frames SAM masked it in), ``flagged``
        (SAM never saw it), ``thumbnail`` and ``thumbnail_url``.
        ``drop_unseen`` moves never-seen moments to ``verification.dropped``.
        ``verification`` holds ``status``, ``phrase`` (what SAM looked for),
        ``receipt``, ``dropped`` and ``spend``. Results also include
        ``searched``, ``warnings``, and the project's Meta budget ledger.
        """
        return _call(_manager().find, job_id, {"query": query, "verify": verify, "drop_unseen": drop_unseen})

    @tool()
    def briefcut_make_shorts(
        job_id: str,
        count: int = 3,
        target_duration: float = 30,
        aspect: str = "9:16",
        budget_usd: float | None = None,
        auto_render: bool = True,
    ) -> dict[str, Any]:
        """Create 2 to 5 sibling short projects from distinct moments in a story.

        With ``auto_render`` true (the default), each queued short renders after
        its plan is made. Wait on the returned job ids and inspect their receipts.
        Long sources (up to 2 hours) work: the shorts reuse the candidate moments
        the story's plan already logged, and one text-only call picks shorts that
        never overlap each other or the story, each opening on its own hook.
        """
        payload: dict[str, Any] = {"count": count, "target_duration": target_duration, "aspect": aspect, "auto_render": auto_render}
        if budget_usd is not None:
            payload["budget_usd"] = budget_usd
        return {"jobs": _call(_manager().shorts, job_id, payload)}

    @tool()
    def briefcut_script_to_video(
        script: str,
        aspect: str = "9:16",
        provider: str = "meta",
        budget_usd: float | None = None,
        language: str | None = None,
        voice: str | None = None,
        images: list[str] | None = None,
        review: bool = False,
        auto_refine: int = 0,
        template: str = "none",
        edit: dict[str, Any] | None = None,
        director: str | None = None,
    ) -> dict[str, Any]:
        """Create a caption-led video project from a script and return its queued job.

        The script becomes on-screen captions, scene by scene. There is no
        synthetic voice: Meta offers no text-to-speech, so leave ``voice`` out
        (any voice is refused). Scenes become editable clips in the normal
        Studio timeline. Needs a Meta API key. With no images, Muse Image draws
        one picture per scene. ``images`` must be absolute paths to PNG, JPEG,
        or WebP files on this machine. ``provider`` is always ``meta``. ``language`` is the script's language
        code, or ``auto``. Leave ``language`` and ``budget_usd`` out to use the
        defaults saved in Studio Settings.
        """
        payload: dict[str, Any] = {
            "script": script, "aspect": aspect, "provider": provider,
            "images": list(images or []), "review": review,
            "auto_refine": auto_refine, "template": template,
        }
        if budget_usd is not None:
            payload["budget_usd"] = budget_usd
        if language is not None:
            payload["language"] = language
        if voice is not None:
            # Kept only so an agent that asks for a voice hears why, instead of a schema error.
            raise ToolError(NO_NARRATION + " Leave voice out.")
        if edit is not None:
            payload["edit"] = edit
        if director is not None:
            payload["director"] = director
        checked = _call(product_request, payload)
        return {"job": _call(_manager().create_script, checked)}

    @tool()
    def briefcut_get(job_id: str) -> dict[str, Any]:
        """Return the current public job: status, stage, progress, plan, result, versions."""
        return _call(_manager().get, job_id)

    @tool()
    def briefcut_list() -> dict[str, Any]:
        """List the most recent campaigns in this workspace as ``jobs``, newest first."""
        return {"jobs": _call(_manager().list)}

    @tool()
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

    @tool()
    def briefcut_render(job_id: str, plan: dict[str, Any] | None = None, captions: bool | None = None, aspect: str | None = None) -> dict[str, Any]:
        """Render a planned job, optionally from an edited plan, and return the job.

        ``plan`` defaults to the saved plan; pass a modified copy of ``job.plan``
        (segments, title, caption, hashtags) to render your own cut. For small
        changes, briefcut_edit is easier. ``captions`` turns burned captions on
        or off and ``aspect`` reframes to 9:16, 16:9, or 1:1; both keep the
        moments. Then call briefcut_wait until the status is ``complete`` and
        read briefcut_receipts.
        """
        payload: dict[str, Any] = {}
        if plan is not None:
            payload["plan"] = plan
        if captions is not None:
            payload["captions"] = captions
        if aspect is not None:
            payload["aspect"] = aspect
        return _call(_manager().render, job_id, payload)

    @tool()
    def briefcut_revise(job_id: str, mode: str, note: str | None = None, director: str | None = None) -> dict[str, Any]:
        """Render version N+1 of a verified job from patches; version N stays available.

        ``mode`` is ``auto`` (apply the deterministic lint patches from the last
        receipts), ``director`` (apply Meta's review patches), or ``note`` (turn
        a free-text ``note`` into patches with Meta first). ``director``
        (``spark`` or ``wonder``) picks who reviews version N+1 and later
        versions. Returns the queued job; wait for ``complete`` and compare receipts.
        """
        payload: dict[str, Any] = {"mode": mode}
        if note is not None:
            payload["note"] = note
        if director is not None:
            payload["director"] = director
        return _call(_manager().revise, job_id, payload)

    @tool()
    def briefcut_receipts(job_id: str) -> dict[str, Any]:
        """Return the verification receipts of the last verified render.

        Includes ``revision``, ``passed``/``total`` with the named ``checks``,
        the ffprobe ``proof`` of the delivered file (duration, width, height,
        has_video, has_audio), lint and review ``patches`` that briefcut_revise can
        apply, and the ``versions`` history.
        """
        return _receipts(_call(_manager().get, job_id))

    @tool()
    def briefcut_verify(job_id: str, revision: int | None = None) -> dict[str, Any]:
        """Re-hash the job's downloadable bundle against its receipts.json and say what changed.

        Every check, the probe, the plan, the Meta calls and the video's sha256
        carry an evidence id, and one root id covers them all. Returns ``ok``,
        the root ``evidence_id``, each ``item`` checked and the ``changed`` ones.
        It also compares the root with the one the job recorded at render time.
        Hashes only: no network. Anyone can run the same check with ``vedit verify``.
        """
        from vedit.evidence import ReceiptsMissing, verify

        job = _call(_manager().get, job_id)
        result = job.get("result")
        if not isinstance(result, dict):
            raise ToolError(f"Campaign {job['id']} is {job['status']}; receipts exist once a render has been verified.")
        if revision is None:
            recorded = result.get("evidence_id")
        else:
            recorded = next((entry.get("evidence_id") for entry in job.get("versions") or [] if isinstance(entry, dict) and entry.get("revision") == revision), None)
        bundle = _call(_manager().artifact, job_id, "bundle", revision=revision)
        try:
            report = verify(bundle, expect=recorded)
        except ReceiptsMissing as exc:
            raise ToolError(str(exc)) from None
        report["receipts"] = Path(report["receipts"].split("!", 1)[0]).name + "!receipts.json"
        return {"job_id": job["id"], "revision": result.get("revision") if revision is None else revision, "bundle": str(bundle), **report}

    @tool()
    def briefcut_artifact_path(job_id: str, name: str, revision: int | None = None) -> dict[str, Any]:
        """Return the absolute on-disk path of an artifact so you can read it directly.

        ``name`` is one of source, video, cover, bundle, brief, report, filmstrip.
        ``filmstrip`` is a JPEG contact sheet of the source frames, a quick way to
        see the footage. ``revision`` pins an older verified version; the default
        is the current one. The source and filmstrip have no revisions.
        """
        if name not in _ARTIFACTS:
            raise ToolError(f"Choose one of {', '.join(_ARTIFACTS)} as the artifact name.")
        if name == "filmstrip":
            if revision is not None:
                raise ToolError("The filmstrip does not have render revisions.")
            path = _call(_manager().filmstrip, job_id)
        else:
            path = _call(_manager().artifact, job_id, name, revision=revision)
        return {"path": str(path), "name": name, "revision": revision, "bytes": path.stat().st_size}

    @tool()
    def briefcut_cancel(job_id: str) -> dict[str, Any]:
        """Stop a queued or running job. Finished work and the saved plan are kept."""
        return _call(_manager().cancel, job_id)

    @tool()
    def briefcut_retry(job_id: str) -> dict[str, Any]:
        """Re-queue a failed or cancelled job from its saved source and plan."""
        return _call(_manager().retry, job_id)

    @tool()
    def briefcut_status() -> dict[str, Any]:
        """Report whether Briefcut can work here, and what Meta and this machine can do.

        Makes no network call and never shows the API key. ``ready`` is true
        only when both requirements hold: ``meta.available`` (a Meta API key is
        configured and Meta calls are allowed) and ``render.ready`` (FFmpeg can
        render). ``missing`` says what to fix, in plain words. ``meta.features``
        lists the Meta features available. ``meta.last_problem`` is why the last
        Meta call with this key failed (such as a rejected key or no credits),
        or null; it clears when a call succeeds or the key changes. ``editor.choices`` holds the ids
        briefcut_edit accepts that this machine's FFmpeg can run.
        """
        caps = _call(capabilities)
        render, cloud, editor, script = caps["render"], caps["meta"], caps["editor"], caps["script"]
        missing = [text for ok, text in (
            (bool(cloud.get("available")), "Meta: " + str(cloud.get("reason") or "Add a Meta API key in Studio Settings.") + " Create a key at https://dev.meta.ai."),
            (bool(render["ready"]), "FFmpeg: " + render["reason"]),
        ) if not ok]

        def ids(rows: list[dict[str, Any]]) -> list[str]:
            return [row["id"] for row in rows if row.get("available", True)]

        export = editor["export"]
        return {
            "version": __version__,
            "workspace": str(workspace()),
            "ready": not missing,
            "missing": missing,
            "render": {"ready": render["ready"], "reason": render["reason"]},
            "meta": {key: cloud.get(key) for key in ("configured", "enabled", "available", "mode", "reason", "features", "prices", "prices_as_of", "directors", "private", "languages", "last_problem")},
            "host": caps["host"],
            "effects": {"supported": caps["effects"]["supported"], "reason": caps["effects"]["reason"], "presets": ids(caps["effects"]["presets"])},
            "editor": {
                "features": editor["features"],
                "choices": {
                    "template": ids(editor["templates"]),
                    "look.preset": ids(editor["looks"]),
                    "transition.type": ids(editor["transitions"]),
                    "captions.style": ids(editor["caption_styles"]),
                    "captions.position": list(editor["caption_positions"]),
                    "zoom.mode": ids(editor["zoom_modes"]),
                    "cutout.mode": ids(editor["cutout_modes"]),
                    "texts.style": ids(editor["text_styles"]),
                    "texts.position": list(editor["text_positions"]),
                    "logo.position": list(editor["overlay_positions"]),
                    "hero_word.font": ids(editor["hero_word"]["fonts"]),
                    "hero_word.color": list(editor["hero_word"]["colors"]),
                    "hero_word.side": list(editor["hero_word"]["sides"]),
                    "motion_card.kind": ids(editor["motion_card"]["kinds"]),
                    "motion_card.style": ids(editor["motion_card"]["styles"]),
                    "motion_card.camera": ids(editor["motion_card"]["cameras"]),
                    "motion_card.palette": list(editor["motion_card"]["palettes"]),
                    "export.quality": list(export["qualities"]),
                    "export.fps": list(export["fps"]),
                    "export.format": list(export["formats"]),
                },
            },
            "script": {key: script.get(key) for key in ("narration", "generates_images", "default_voice", "reason", "image_reason")}
            | {"voices": [row["id"] for row in script.get("voices") or []]},
            "director": caps["director"],
            "motion_cards": caps["motion_cards"],
            "limits": caps["limits"],
        }

    @tool()
    def briefcut_timeline(job_id: str, detail: str = "summary", offset: int = 0, limit: int = 50) -> dict[str, Any]:
        """Read a planned job's cut on the output clock, summary first.

        ``detail`` is ``summary`` (length, moments, counts, assets, and the
        settings that differ from a blank edit), ``segments`` (each moment in
        full), ``spans`` (each kept source range placed on the output clock),
        ``captions`` (the cues that will burn, without word timing; a cue's
        ``key`` is what ``edit.captions.edits`` uses), or ``edit`` (the full
        validated edit). Lists page with ``offset`` and ``limit`` (1 to 200)
        and return ``total`` and ``next_offset``.
        """
        if detail not in _DETAILS:
            raise ToolError(f"Choose one of {', '.join(_DETAILS)} as the detail.")
        if offset < 0 or not 1 <= limit <= _MAX_PAGE:
            raise ToolError(f"Use an offset of 0 or more and a limit from 1 to {_MAX_PAGE}.")
        manager = _manager()
        job = _call(manager.get, job_id)
        plan = _planned(job)
        view = _call(manager.timeline, job_id, {})
        edit = plan.get("edit") or default_edit()
        if detail == "edit":
            return {"job_id": job_id, "detail": detail, "revision": job["revision"], "edit": edit}
        if detail == "summary":
            request = job["request"]
            return {
                "job_id": job_id, "detail": detail, "status": job["status"], "revision": job["revision"],
                "verified_revision": job.get("verified_revision"), "title": plan.get("title"),
                "provider": request.get("provider"), "aspect": request.get("aspect"), "captions_on": request.get("captions"),
                "language": request.get("language"), "source_duration": plan.get("source_duration"),
                **_clock(view),
                "segments": _moments(plan),
                "edit": _edit_summary(edit),
                "removed_ranges": len(edit.get("removed") or []),
                "caption_edits": len((edit.get("captions") or {}).get("edits") or {}),
                "assets": [{key: record.get(key) for key in ("id", "kind", "name", "duration")} for record in (job.get("assets") or {}).values()],
                "warnings": plan.get("warnings") or [],
            }
        rows = {"segments": plan.get("segments") or [], "spans": view.get("spans") or [], "captions": [_cue(cue) for cue in view.get("captions") or []]}[detail]
        page = rows[offset:offset + limit]
        following = offset + len(page)
        return {"job_id": job_id, "detail": detail, "revision": job["revision"], "duration": view.get("duration"),
                "total": len(rows), "offset": offset, "limit": limit, "next_offset": following if following < len(rows) else None, "items": page}

    @tool()
    def briefcut_edit(
        job_id: str,
        edit: dict[str, Any] | None = None,
        patches: list[dict[str, Any]] | None = None,
        render: bool = False,
        captions: bool | None = None,
        aspect: str | None = None,
    ) -> dict[str, Any]:
        """Change a planned job's settings or moments without sending the whole plan.

        ``edit`` is a partial edit merged into the saved one, such as
        {"look": {"preset": "warm"}}, {"captions": {"style": "bold", "language": "es"}},
        or {"cutout": {"mode": "blur", "subject": "the person"}}. Objects merge;
        lists and plain values replace. briefcut_status lists the valid ids.
        ``patches`` change moments, numbered from 1 as briefcut_timeline shows:
        {"op": "trim_start" or "trim_end", "segment": n, "seconds": new source time},
        {"op": "split", "segment": n, "seconds": source time},
        {"op": "move_before", "segment": n, "seconds": other moment number},
        {"op": "set_speed", "segment": n, "value": rate}, {"op": "remove", "segment": n},
        or {"op": "set_edit", "path": "look.preset", "value": "warm"}. Patches apply
        first, then ``edit`` merges on top.

        The draft is validated exactly as Studio validates an edit. By default
        nothing is saved: you get the changes, the new clock, and warnings. Pass
        ``render`` true to keep the draft and render version N+1; ``captions``
        and ``aspect`` then apply as in briefcut_render. Two settings send more
        to Meta at render, on the project's budget: a caption language other
        than the project's (caption text) and a cutout (a proxy of the footage
        to Meta SAM). When the draft adds either, ``meta`` lists it.
        """
        if edit is None and not patches:
            raise ToolError("Send an edit, patches, or both.")
        if patches and len(patches) > _MAX_PATCHES:
            raise ToolError(f"Send at most {_MAX_PATCHES} patches at once.")
        manager = _manager()
        job = _call(manager.get, job_id)
        plan = _planned(job)
        if render and job["status"] not in TERMINAL_STATES:
            raise ToolError(f"Campaign {job_id} is {job['status']}. Wait for it with briefcut_wait, then edit.")
        before = plan.get("edit") or default_edit()
        draft: dict[str, Any] = dict(plan, edit=before)
        applied: list[dict[str, Any]] = []
        if patches:
            # The agent is the author here; a patch never claims to come from the director.
            tagged = [dict(patch, source="agent") for patch in patches]
            draft, applied = apply_patches(draft, tagged, duration=float(job["source"]["duration"]))
            if not any(item.get("skipped") is None for item in applied):
                why = "; ".join(dict.fromkeys(str(item.get("skipped")) for item in applied))
                raise ToolError(f"No patch could be applied ({why}). Nothing changed.")
        if edit is not None:
            draft["edit"] = merge_edit(draft["edit"], edit)
        # The manager is the only judge: the same check Studio's preview runs.
        view = _call(manager.timeline, job_id, {"plan": draft})
        captions_on = bool(job["request"].get("captions")) if captions is None else captions
        uses, warnings = _meta_uses(job, before, draft["edit"], captions_on)
        moved = [{key: item.get(key) for key in ("op", "segment", "summary", "skipped")} for item in applied]
        changes = _changes(before, draft["edit"])
        idle = not changes and not any(item.get("skipped") is None for item in applied)
        if idle:
            warnings.append("This draft changes nothing.")
        result: dict[str, Any] = {"job_id": job_id, "rendered": False, "changes": changes, "patches": moved,
                                  "meta": uses, "warnings": warnings}
        if not render:
            result.update(_clock(view), segments=_moments(draft),
                          next="Nothing was saved. Call again with render true to keep this draft and render it.")
            return result
        if idle and captions is None and aspect is None:
            raise ToolError("This draft changes nothing, so there is nothing to render. briefcut_render renders the saved plan again.")
        payload: dict[str, Any] = {"plan": draft}
        if captions is not None:
            payload["captions"] = captions
        if aspect is not None:
            payload["aspect"] = aspect
        queued = _call(manager.render, job_id, payload)
        if queued["revision"] == job["revision"]:
            # The manager hands back a render already in flight instead of queuing this draft.
            raise ToolError(f"Campaign {job_id} is already rendering, so this draft was not kept. Wait for it with briefcut_wait, then edit again.")
        saved = queued.get("plan") or {}
        result.update(rendered=True, status=queued["status"], revision=queued["revision"],
                      changes=_changes(before, saved.get("edit") or {}), segments=_moments(saved), **_clock(saved.get("timeline") or {}),
                      next="Call briefcut_wait until the status is complete, then briefcut_receipts.")
        return result

    @tool()
    def briefcut_hero_word(job_id: str, subject: str | None = None, font: str | None = None, color: str | None = None) -> dict[str, Any]:
        """Ask Muse Spark for this cut's hero word: one spoken word shown huge behind the subject for 1 to 3 seconds.

        Spark reads the cut's caption words only and proposes; Briefcut checks
        the word is said, inside one shot, and returns a bounded
        ``hero_word`` edit. Nothing renders and nothing is saved. ``subject``
        is what the word goes behind, such as "the person in the red jacket".
        It defaults to the project's subject, and is needed when the project has
        none: Briefcut never guesses one, and without it the call is refused
        before anything is spent. ``font`` and ``color`` are ids briefcut_status lists
        under ``hero_word.font`` and ``hero_word.color``. Costs well under a
        cent of the project's Meta budget; at render, SAM 3.1 masks the subject
        over the word's window only (about $0.01 to $0.02). To apply it, call
        briefcut_edit with {"hero_word": <the returned hero_word>} and render
        true, or briefcut_render with the plan's edit updated. ``adjusted``
        says what Briefcut changed in Spark's proposal and why.
        """
        options = {name: value for name, value in (("font", font), ("color", color)) if value is not None}
        suggestion = _call(_manager().suggest_hero_word, job_id, subject=subject, **options)
        return dict(suggestion, next="Apply it with briefcut_edit(job_id, edit={\"hero_word\": hero_word}, render=true). Nothing was saved yet.")

    @tool()
    def briefcut_motion_card(job_id: str, kind: str = "intro_title", backdrop: bool = True) -> dict[str, Any]:
        """Ask Muse Spark to design a 3D motion card for this cut, rendered later by Blender on this machine.

        ``kind`` is ``intro_title``, ``end_card`` or ``lower_third``. Spark reads
        the goal and the cut's caption words only and picks closed choices
        (text, subtitle, style, palette, camera, length); Briefcut validates them
        into a ``motion_card`` edit. With ``backdrop`` (not for a lower third),
        Muse Image paints the plate the card opens on for $0.01 and it is kept
        as a project image. Spark's call costs under a cent. Nothing renders and
        the plan is not changed. Needs Blender here: briefcut_status shows
        ``motion_cards``. To apply it, call briefcut_edit with
        {"motion_card": <the returned motion_card>} and render true.
        """
        suggestion = _call(_manager().suggest_motion_card, job_id, kind=kind, backdrop=backdrop)
        return dict(suggestion, next="Apply it with briefcut_edit(job_id, edit={\"motion_card\": motion_card}, render=true). The plan was not changed.")

    @tool()
    def briefcut_add_asset(job_id: str, path: str) -> dict[str, Any]:
        """Add an audio file or image from this machine to a project, for music, a logo, an overlay, or a cutout background.

        ``path`` is an absolute path. The file is copied into the project and
        stays on this machine. Returns the asset record. Use its ``id`` with
        briefcut_edit, such as {"music": {"asset": id}}, {"logo": {"asset": id}},
        or {"cutout": {"mode": "image", "subject": "the person", "asset": id}}.
        """
        source, _size = _local_file(path, "file")
        manager = _manager()
        dest = _stage(manager, source, "file")
        try:
            return _call(manager.add_asset, job_id, {"path": str(dest)})
        finally:
            # The project keeps its own copy, so the staged upload is not needed.
            shutil.rmtree(dest.parent.parent, ignore_errors=True)

    @tool()
    def briefcut_generate_background(job_id: str, prompt: str, consent: bool) -> dict[str, Any]:
        """Generate a background picture with Meta and add it to the project as an image asset.

        This sends the prompt words to Meta Muse Image, never footage, and
        spends about $0.01 of the project's Meta budget. It needs Meta to be
        available. Set ``consent`` true to confirm the cost. Use the returned ``id`` with
        briefcut_edit as {"cutout": {"mode": "image", "subject": "...", "asset": id}}.
        """
        return _call(_manager().generate_background, job_id, {"prompt": prompt, "consent": consent})

    return server


def serve_stdio() -> int:
    """Run the MCP server on stdio until the client closes the pipe."""
    server = build_server()
    try:
        server.run("stdio")
    finally:
        shutdown()
    return 0
