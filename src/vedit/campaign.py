"""Durable local campaign jobs, with approval between planning and rendering.

The model supplies editorial data; only the compiler supplies executable steps.
SQLite is the source of truth, so closing the browser never owns a render's life.
"""

from __future__ import annotations

import hashlib
import fcntl
import json
import math
import os
import queue
import re
import shutil
import sqlite3
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vedit.agent.explain import local_transcriber_ready
from vedit.compile import compile_brief
from vedit.effects import REQUIRED_EFFECT_FILTERS, EffectError, effect_catalogue, validate_effect
from vedit.errors import RenderCancelled, VeditError
from vedit.ir import Brief, Canvas, brief_dumps
from vedit.captions import Cue, write_ass, write_srt
from vedit.media import has_filter, probe, resolve_ffmpeg, resolve_ffprobe, run_ffmpeg
from vedit.pipeline import Pipeline, execute, write_codegen, write_report
from vedit.presets import get_preset

_ACTIVE = {"queued", "analyzing", "rendering"}
_ASPECTS = {"9:16": "shorts_9x16", "16:9": "youtube_1080p", "1:1": "square_1x1"}
_META_OPERATIONS = ("story", "tracking", "cover", "transcribe")
_CAPTION_PROVIDERS = {"local", "meta"}
_MAX_SOURCE_BYTES = 200_000_000
_MAX_SOURCE_SECONDS = 1_200
_MAX_JOBS = 12
_ID = re.compile(r"^[a-f0-9]{32}$")
_LANGUAGE = re.compile(r"^(?:auto|[a-z]{2,3}(?:-[A-Za-z]{2,8})?)$")
_KIND = re.compile(r"^[a-z][a-z_-]{0,23}$")


class CampaignError(Exception):
    """An actionable message safe to return to a local client."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class _Cancelled(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(_json(value) + "\n", encoding="utf-8")
    temporary.replace(path)


def _number(value: Any, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CampaignError(f"{name} must be a number.")
    try:
        number = float(value)
    except OverflowError as exc:
        raise CampaignError(f"{name} must be a finite number.") from exc
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise CampaignError(f"{name} must be between {minimum:g} and {maximum:g}.")
    return number


def _text(value: Any, name: str, limit: int, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise CampaignError(f"{name} must be text.")
    value = value.strip()
    if len(value) > limit or (required and not value):
        raise CampaignError(f"{name} must contain {'1–' if required else 'at most '}{limit} characters.")
    if any(ord(character) < 32 and character not in "\n\t" for character in value):
        raise CampaignError(f"{name} contains unsupported control characters.")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise CampaignError(f"{name} contains invalid Unicode text.")
    return value


def _boolean(payload: dict[str, Any], name: str, default: bool) -> bool:
    value = payload.get(name, default)
    if not isinstance(value, bool):
        raise CampaignError(f"{name} must be true or false.")
    return value


def capabilities() -> dict[str, Any]:
    """Inspect local availability without a network call or exposing credentials."""
    try:
        ffmpeg, ffprobe = resolve_ffmpeg(), resolve_ffprobe()
        ready = all(os.access(path, os.X_OK) for path in (ffmpeg, ffprobe))
        reason = "Local rendering is ready." if ready else "Install FFmpeg and ffprobe, then restart Studio."
    except VeditError:
        ready, reason = False, "Install FFmpeg and ffprobe, then restart Studio."
    captions = local_transcriber_ready()
    try:
        from vedit import meta

        cloud = meta.capabilities()
    except ImportError:
        cloud = {"configured": False, "enabled": False, "reason": "Meta integration is unavailable in this installation."}
    return {
        "local": {
            "ready": ready,
            "captions": captions,
            "reason": reason,
            "caption_reason": "Local speech model is ready." if captions else "Install a local Whisper model to enable captions.",
        },
        "meta": cloud,
        "effects": _effect_support(),
        "limits": {"upload_bytes": _MAX_SOURCE_BYTES, "source_seconds": _MAX_SOURCE_SECONDS, "segments": 8, "target_seconds": 180},
    }


def _effect_support() -> dict[str, Any]:
    try:
        missing = [name for name in REQUIRED_EFFECT_FILTERS if not has_filter(name)]
    except VeditError:
        missing = list(REQUIRED_EFFECT_FILTERS)
    return {
        "supported": not missing,
        "presets": effect_catalogue(),
        "missing_filters": missing,
        "reason": "Local motion effects are ready." if not missing else "Motion effects need FFmpeg filters: " + ", ".join(missing) + ". Install ffmpeg-full and restart Studio.",
    }


def validate_plan(value: Any, duration: float, *, original: dict[str, Any] | None = None) -> dict[str, Any]:
    """Accept editorial values only, never paths, operations, or provider claims."""
    if not isinstance(value, dict):
        raise CampaignError("The story plan must be an object.")
    segments = value.get("segments")
    if not isinstance(segments, list) or not 1 <= len(segments) <= 8:
        raise CampaignError("Choose between one and eight story segments.")
    clean = []
    for index, segment in enumerate(segments, 1):
        if not isinstance(segment, dict):
            raise CampaignError(f"Segment {index} must be an object.")
        start = _number(segment.get("start"), f"Segment {index} start", 0, duration)
        end = _number(segment.get("end"), f"Segment {index} end", 0, duration)
        if end - start < 0.1:
            raise CampaignError(f"Segment {index} must last at least 0.1 seconds and end after its start.")
        item = {
            "start": round(start, 6), "end": round(end, 6),
            "label": _text(segment.get("label", f"Moment {index}"), f"Segment {index} label", 100),
            "reason": _text(segment.get("reason", "Selected by you."), f"Segment {index} reason", 600),
        }
        # Editorial metadata from the planner: optional, bounded, never executable.
        score = segment.get("score")
        if isinstance(score, (int, float)) and not isinstance(score, bool) and math.isfinite(float(score)) and 0 <= float(score) <= 1:
            item["score"] = round(float(score), 3)
        kind = segment.get("kind")
        if isinstance(kind, str) and _KIND.fullmatch(kind):
            item["kind"] = kind
        clean.append(item)
    total = sum(segment["end"] - segment["start"] for segment in clean)
    if total > 180.05:
        raise CampaignError("The finished story may be at most 180 seconds. Shorten or remove a segment.")
    hashtags = value.get("hashtags", [])
    if not isinstance(hashtags, list) or len(hashtags) > 15:
        raise CampaignError("Use at most 15 hashtags.")
    hashtags = [_text(tag, "Hashtag", 80) for tag in hashtags]
    trusted = original or {}
    return {
        "title": _text(value.get("title", "Your campaign"), "Title", 120, required=True),
        "summary": _text(value.get("summary", ""), "Summary", 1500),
        "segments": clean,
        "caption": _text(value.get("caption", ""), "Post copy", 2200),
        "hashtags": hashtags,
        "cover_prompt": _text(value.get("cover_prompt", ""), "Cover instructions", 1200),
        "provider": trusted.get("provider", "local"),
        "usage": trusted.get("usage", {}),
        "source_duration": duration,
        "duration": round(total, 3),
        "warnings": trusted.get("warnings", []),
        "analysis": _analysis(trusted.get("analysis"), duration),
    }


def _analysis(value: Any, duration: float) -> dict[str, Any] | None:
    """Bounded, display-only measurement data from a trusted planner result."""
    if not isinstance(value, dict):
        return None
    try:
        method = value.get("method", "local-analysis")
        if not isinstance(method, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", method):
            method = "local-analysis"
        scenes = sorted({round(_number(item, "Scene time", 0, duration), 3) for item in (value.get("scenes") or [])[:400]})
        speech = []
        for item in (value.get("speech") or [])[:400]:
            if isinstance(item, dict):
                start = _number(item.get("start"), "Speech start", 0, duration)
                end = _number(item.get("end"), "Speech end", 0, duration)
                if end > start:
                    speech.append({"start": round(start, 3), "end": round(end, 3)})
        transcript, size = [], 0
        for item in (value.get("transcript") or [])[:600]:
            if not isinstance(item, dict):
                continue
            start = _number(item.get("start"), "Cue start", 0, duration)
            end = _number(item.get("end"), "Cue end", 0, duration)
            text = _text(item.get("text", ""), "Cue text", 300)
            size += len(text.encode("utf-8"))
            if end > start and text and size <= 60_000:
                transcript.append({"start": round(start, 3), "end": round(end, 3), "text": text})
        notes = [_text(note, "Analysis note", 300) for note in (value.get("notes") or [])[:8] if isinstance(note, str)]
        return {"method": method, "scenes": scenes, "speech": speech, "transcript": transcript, "duration": round(duration, 3), "notes": notes}
    except CampaignError:
        return None


def _effect(value: Any, *, duration: float | None = None) -> dict[str, Any] | None:
    try:
        effect = validate_effect(value, clip_duration=duration)
        if effect is not None and effect["intensity"] > 0:
            support = _effect_support()
            if not support["supported"]:
                raise CampaignError(support["reason"])
        # Preserve an omitted duration until the approved story length is known;
        # the shared validator can then choose a fitting default for short clips.
        if effect is not None and duration is None and isinstance(value, dict) and "duration" not in value:
            effect.pop("duration", None)
        return effect
    except EffectError as exc:
        raise CampaignError(str(exc)) from exc


def remap_proxy_points(points: Any, *, source_width: int, source_height: int, proxy_width: int, proxy_height: int, duration: float) -> list[dict[str, float]]:
    """Undo a fitted proxy's letterbox; keep coordinates in the original frame."""
    if not isinstance(points, list) or not points or min(source_width, source_height, proxy_width, proxy_height) <= 0:
        raise CampaignError("The subject track has no usable geometry. Try a clearer subject description.")
    scale = min(proxy_width / source_width, proxy_height / source_height)
    fitted_width, fitted_height = round(source_width * scale), round(source_height * scale)
    left, top = (proxy_width - fitted_width) / 2, (proxy_height - fitted_height) / 2
    result: list[dict[str, float]] = []
    for point in points:
        if not isinstance(point, dict):
            raise CampaignError("The subject track contains invalid geometry.")
        time = _number(point.get("time"), "Tracking timestamp", 0, duration + 0.15)
        if time >= duration:
            continue
        x = _number(point.get("x"), "Tracked x", 0, 1) * proxy_width
        y = _number(point.get("y"), "Tracked y", 0, 1) * proxy_height
        width = _number(point.get("width"), "Tracked width", 0.000001, 1) * proxy_width
        height = _number(point.get("height"), "Tracked height", 0.000001, 1) * proxy_height
        x1, y1 = max(left, x), max(top, y)
        x2, y2 = min(left + fitted_width, x + width), min(top + fitted_height, y + height)
        area = max(0, x2 - x1) * max(0, y2 - y1)
        if area < width * height * 0.5:
            raise CampaignError("The detected subject falls mostly outside the picture. Refine the subject description or turn tracking off.")
        result.append({"time": time, "x": max(0, (x1 - left) / fitted_width), "y": max(0, (y1 - top) / fitted_height), "width": (x2 - x1) / fitted_width, "height": (y2 - y1) / fitted_height})
    if not result:
        raise CampaignError("No subject track overlaps the selected story. Adjust the selection or turn tracking off.")
    return result


class CampaignManager:
    """One bounded worker for the local machine, durable state for every action."""

    def __init__(self, workspace: Path | str | None = None, *, start_worker: bool = True):
        self.workspace = Path(workspace or Path.cwd()).resolve()
        self.sessions = self.workspace / "vedit-sessions"
        self.root = self.sessions / "campaigns"
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "campaigns.sqlite3"
        self._lease = (self.root / ".worker.lock").open("a+")
        try:
            fcntl.flock(self._lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lease.close()
            raise CampaignError("Another Studio is already processing this workspace. Use that window or stop it before restarting.", 409) from exc
        self._lock = threading.RLock()
        self._events: dict[str, threading.Event] = {}
        self._active_processes = 0
        self._idle = threading.Event()
        self._idle.set()
        self._queue: queue.Queue[str | None] = queue.Queue(maxsize=_MAX_JOBS + 1)
        self._closed = False
        self._worker: threading.Thread | None = None
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, data TEXT NOT NULL)")
            connection.execute("CREATE INDEX IF NOT EXISTS jobs_fingerprint ON jobs(fingerprint)")
            connection.execute("CREATE INDEX IF NOT EXISTS jobs_updated ON jobs(updated_at DESC)")
            interrupted = connection.execute("SELECT data FROM jobs WHERE status IN ('queued','analyzing','rendering')").fetchall()
            for row in interrupted:
                job = json.loads(row[0])
                job.update(status="failed", stage="interrupted", progress=0, message="Studio stopped before this job finished. Retry to continue from the saved plan.", error="This job was interrupted. Your source and approved plan are saved.", cancel_requested=False, updated_at=_now())
                self._save(connection, job)
        if start_worker:
            self._worker = threading.Thread(target=self._work, name="vedit-campaigns", daemon=True)
            self._worker.start()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=10)
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @staticmethod
    def _save(connection: sqlite3.Connection, job: dict[str, Any]) -> None:
        connection.execute("UPDATE jobs SET status=?, updated_at=?, data=? WHERE id=?", (job["status"], job["updated_at"], _json(job), job["id"]))

    @staticmethod
    def _public(job: dict[str, Any]) -> dict[str, Any]:
        result = {key: value for key, value in job.items() if not key.startswith("_")}
        verified = job.get("result")
        if isinstance(verified, dict):
            # Older projects predate explicit result revisions; a completed
            # project's job revision is its last verified revision.
            revision = verified.get("revision", job.get("verified_revision") or job["revision"])
            result["result"] = dict(verified, revision=revision, stale=revision != job["revision"])
            for name in ("video", "cover", "bundle", "brief", "report"):
                if f"{name}_url" in result["result"]:
                    result["result"][f"{name}_url"] = CampaignManager._url(job["id"], name, revision=revision)
            result["verified_revision"] = revision
        else:
            result["verified_revision"] = None
        return result

    def _load(self, connection: sqlite3.Connection, ident: str) -> dict[str, Any]:
        if not isinstance(ident, str) or not _ID.fullmatch(ident):
            raise CampaignError("Campaign not found.", 404)
        row = connection.execute("SELECT data FROM jobs WHERE id=?", (ident,)).fetchone()
        if row is None:
            raise CampaignError("Campaign not found.", 404)
        return json.loads(row[0])

    def _update(self, ident: str, **changes: Any) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            job = self._load(connection, ident)
            if changes.get("status") in {"planned", "complete"} and (job.get("cancel_requested") or self._event(ident).is_set() or self._closed):
                raise _Cancelled()
            if changes.get("status") == "complete" and isinstance(changes.get("_artifacts"), dict):
                history = dict(job.get("_verified_artifacts", {}))
                previous = self._public(job)
                if previous.get("verified_revision") is not None and job.get("_artifacts"):
                    history[str(previous["verified_revision"])] = job["_artifacts"]
                history[str(changes["verified_revision"])] = changes["_artifacts"]
                changes["_verified_artifacts"] = history
            job.update(changes, updated_at=_now())
            self._save(connection, job)
            return job

    def _source(self, raw: Any) -> Path:
        if not isinstance(raw, str) or not raw or not Path(raw).is_absolute():
            raise CampaignError("Upload a clip in Studio before creating a campaign.")
        try:
            path = Path(raw).resolve()
        except (OSError, ValueError) as exc:
            raise CampaignError("The source path is invalid. Upload the clip again.") from exc
        try:
            relative = path.relative_to(self.sessions.resolve())
        except ValueError as exc:
            raise CampaignError("Use a clip uploaded through Studio.") from exc
        if len(relative.parts) != 3 or relative.parts[1] != "inbox" or relative.parts[0] == "campaigns" or path.name.startswith("."):
            raise CampaignError("Use a clip uploaded through Studio.")
        if not path.is_file() or not 0 < path.stat().st_size <= _MAX_SOURCE_BYTES:
            raise CampaignError("The source is missing, empty, or larger than 200 MB. Upload it again.")
        return path

    def _request(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise CampaignError("Send a JSON object with a goal and an uploaded clip.")
        paths = value.get("paths")
        if not isinstance(paths, list) or len(paths) != 1:
            raise CampaignError("Choose one uploaded video for this campaign.")
        source = self._source(paths[0])
        provider = value.get("provider", "local")
        if not isinstance(provider, str) or provider not in {"local", "meta"}:
            raise CampaignError("Choose local or meta as the planning provider.")
        aspect = value.get("aspect", "9:16")
        if not isinstance(aspect, str) or aspect not in _ASPECTS:
            raise CampaignError("Choose 9:16, 16:9, or 1:1 as the frame shape.")
        language = value.get("language", "en")
        if not isinstance(language, str) or not _LANGUAGE.fullmatch(language):
            raise CampaignError("Choose a caption language code, such as en, es, or auto.")
        caption_provider = value.get("caption_provider", "local")
        if not isinstance(caption_provider, str) or caption_provider not in _CAPTION_PROVIDERS:
            raise CampaignError("Choose local or meta as the caption provider.")
        request = {
            "paths": [str(source)],
            "goal": _text(value.get("goal", ""), "Goal", 2000, required=True),
            "provider": provider,
            "target_duration": _number(value.get("target_duration", 30), "Target duration", 0.5, 180),
            "aspect": aspect,
            "captions": _boolean(value, "captions", False),
            "caption_provider": caption_provider,
            "language": language,
            "subject": _text(value.get("subject", ""), "Subject", 200),
            "budget_usd": _number(value.get("budget_usd", 0.25), "Campaign budget", 0.01, 5),
            "generate_cover": _boolean(value, "generate_cover", False),
            "effect": _effect(value.get("effect")),
        }
        if provider == "local" and request["generate_cover"]:
            raise CampaignError("Generated covers need Meta mode. A cover frame is always included locally.")
        if provider == "meta" or (request["captions"] and caption_provider == "meta"):
            cloud = capabilities()["meta"]
            if not cloud.get("configured") or not cloud.get("enabled"):
                raise CampaignError("Meta is not enabled. Configure MODEL_API_KEY and VEDIT_META_ENABLED=1 on the server, or choose Local edit.", 409)
        return request

    def create(self, value: Any) -> dict[str, Any]:
        request = self._request(value)
        key = _text(value.get("idempotency_key", ""), "Request key", 100)
        fingerprint = hashlib.sha256((_json(request) + "|" + key).encode()).hexdigest()
        with self._lock, self._connect() as connection:
            if self._closed:
                raise CampaignError("Studio is shutting down. Restart it and try again.", 503)
            existing = connection.execute("SELECT data FROM jobs WHERE fingerprint=? AND status NOT IN ('failed','cancelled') ORDER BY updated_at DESC LIMIT 1", (fingerprint,)).fetchone()
            if existing:
                return self._public(json.loads(existing[0]))
            if connection.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','analyzing','rendering')").fetchone()[0] >= _MAX_JOBS or self._queue.full():
                raise CampaignError("The edit queue is full. Wait for a job to finish, then try again.", 429)
            ident, now = uuid.uuid4().hex, _now()
            job = {
                "id": ident, "status": "queued", "stage": "queued", "progress": 0,
                "message": "Waiting to read your clip.", "created_at": now, "updated_at": now,
                "request": request, "plan": None, "result": None, "source": None, "error": None,
                "source_url": self._url(ident, "source"), "cancel_requested": False, "revision": 0,
                "verified_revision": None,
                "budget": {"cap_usd": request["budget_usd"], "reserved_usd": 0, "remaining_usd": request["budget_usd"]},
                "_action": "plan", "_artifacts": {}, "_fingerprint": fingerprint,
            }
            connection.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?)", (ident, fingerprint, job["status"], now, now, _json(job)))
            (self.root / ident).mkdir()
            connection.commit()
            self._events[ident] = threading.Event()
            self._queue.put_nowait(ident)
            return self._public(job)

    def get(self, ident: str) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            return self._public(self._load(connection, ident))

    def list(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute("SELECT data FROM jobs ORDER BY updated_at DESC LIMIT 50").fetchall()
            return [self._public(json.loads(row[0])) for row in rows]

    def render(self, ident: str, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise CampaignError("Send a JSON object containing the approved plan.")
        with self._lock, self._connect() as connection:
            job = self._load(connection, ident)
            if job["status"] in _ACTIVE:
                if job["_action"] == "render":
                    return self._public(job)
                raise CampaignError("Wait for the story plan before rendering.", 409)
            if not job.get("plan") or not job.get("source"):
                raise CampaignError("Create a story plan before rendering.", 409)
            plan = validate_plan(value.get("plan", job["plan"]), job["source"]["duration"], original=job["plan"])
            effect = _effect(value.get("effect", job["request"].get("effect")), duration=plan["duration"])
            plan["effect"] = effect
            if self._queue.full() or self._closed:
                raise CampaignError("The render queue is unavailable. Try again shortly.", 503)
            previous = self._public(job)
            job["result"], job["verified_revision"] = previous["result"], previous["verified_revision"]
            if job["result"]:
                job["result"]["stale"] = True
            job["request"]["effect"] = effect
            job.update(plan=plan, status="queued", stage="queued", progress=0, message="Your approved story is queued for rendering. Your previous verified version remains available." if job["result"] else "Your approved story is queued for rendering.", error=None, cancel_requested=False, revision=job["revision"] + 1, updated_at=_now(), _action="render")
            self._save(connection, job)
            connection.commit()
            self._events[ident] = threading.Event()
            self._queue.put_nowait(ident)
            return self._public(job)

    def retry(self, ident: str) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            job = self._load(connection, ident)
            if job["status"] in _ACTIVE:
                return self._public(job)
            if job["status"] not in {"failed", "cancelled"}:
                raise CampaignError("This campaign does not need a retry.", 409)
            self._source(job["request"]["paths"][0])
            if self._queue.full() or self._closed:
                raise CampaignError("The edit queue is unavailable. Try again shortly.", 503)
            job.update(status="queued", stage="queued", progress=0, message="Retry queued. Your approved choices are preserved.", error=None, cancel_requested=False, updated_at=_now())
            self._save(connection, job)
            connection.commit()
            self._events[ident] = threading.Event()
            self._queue.put_nowait(ident)
            return self._public(job)

    def cancel(self, ident: str) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            job = self._load(connection, ident)
            if job["status"] in {"complete", "failed", "cancelled"}:
                return self._public(job)
            self._event(ident).set()
            if job["status"] in {"queued", "planned"}:
                job.update(status="cancelled", stage="cancelled", message="Campaign cancelled. Your clip and plan are saved.", cancel_requested=True)
            else:
                job.update(cancel_requested=True, message="Stopping the current processing step. Your verified work remains available.")
            job["updated_at"] = _now()
            self._save(connection, job)
            return self._public(job)

    def artifact(self, ident: str, name: str, *, revision: int | None = None) -> Path:
        if name not in {"source", "video", "cover", "bundle", "brief", "report"}:
            raise CampaignError("Artifact not found.", 404)
        with self._lock, self._connect() as connection:
            job = self._load(connection, ident)
        if name == "source":
            if revision is not None:
                raise CampaignError("The source does not have render revisions.", 400)
            if not job.get("source") or not job["source"].get("has_video"):
                raise CampaignError("The clip is still being checked. Try again shortly.", 409)
            return self._source(job["request"]["paths"][0])
        if not isinstance(job.get("result"), dict):
            raise CampaignError("This download is available after the render is verified.", 409)
        artifacts = job.get("_artifacts", {})
        if revision is not None:
            if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
                raise CampaignError("Choose a positive verified revision.")
            current = self._public(job)["verified_revision"]
            artifacts = job.get("_verified_artifacts", {}).get(str(revision))
            if artifacts is None and revision == current:
                # Completed projects created before revision history retain
                # their exact current verified path as a compatibility fallback.
                artifacts = job.get("_artifacts", {})
            if artifacts is None:
                raise CampaignError("That verified revision is not available.", 404)
        raw = artifacts.get(name)
        if not raw:
            raise CampaignError("Artifact not found.", 404)
        path = (self.root / ident / raw).resolve()
        try:
            path.relative_to((self.root / ident).resolve())
        except ValueError as exc:
            raise CampaignError("Artifact not found.", 404) from exc
        if not path.is_file():
            raise CampaignError("This output was removed. Render the saved plan again.", 404)
        return path

    @staticmethod
    def _url(ident: str, name: str, *, revision: int | None = None) -> str:
        suffix = f"&v={revision}" if revision is not None else ""
        return f"/api/campaigns/{ident}/artifact?name={name}{suffix}"

    def _check_cancel(self, ident: str) -> None:
        if self._event(ident).is_set():
            raise _Cancelled()
        with self._lock, self._connect() as connection:
            job = self._load(connection, ident)
        if job.get("cancel_requested") or job["status"] == "cancelled":
            raise _Cancelled()

    def _event(self, ident: str) -> threading.Event:
        with self._lock:
            return self._events.setdefault(ident, threading.Event())

    def _work(self) -> None:
        try:
            while not self._closed:
                try:
                    ident = self._queue.get(timeout=0.25)
                except queue.Empty:
                    continue
                try:
                    if ident is None:
                        return
                    self.process(ident)
                finally:
                    self._queue.task_done()
        finally:
            self._release_lease()

    def process(self, ident: str) -> None:
        """Execute one queued item; exposed for deterministic worker tests."""
        event = self._event(ident)
        with self._lock:
            self._active_processes += 1
            self._idle.clear()
        try:
            self._check_cancel(ident)
            with self._lock, self._connect() as connection:
                job = self._load(connection, ident)
            if job["status"] != "queued":
                return
            if job["_action"] == "render":
                self._render(job)
            else:
                self._plan(job)
        except (_Cancelled, RenderCancelled):
            self._update(ident, status="cancelled", stage="cancelled", message="Campaign cancelled. Your clip, plan, and any previous verified version are saved.", error=None)
        except Exception as exc:
            # Provider errors are intentionally sanitized by that boundary.
            # Render exceptions can contain full commands and paths: do not expose them.
            if isinstance(exc, CampaignError):
                message = str(exc)
            elif type(exc).__name__ == "MetaError" and type(exc).__module__ == "vedit.meta":
                message = str(exc)
            elif isinstance(exc, VeditError):
                message = "The video could not be processed. Check that it plays, run vedit doctor, then retry. Your plan is saved."
            else:
                message = "The edit could not finish. Your clip and plan are saved. Restart Studio and retry."
            self._update(ident, status="failed", stage="failed", message=message, error=message)
        finally:
            try:
                with self._lock, self._connect() as connection:
                    job = self._load(connection, ident)
                self._update(ident, budget=self._usage_summary(self.root / ident, job["request"]["budget_usd"]))
            except CampaignError:
                # An unreadable ledger must block new calls, but must not hide
                # the primary failure which led to the retry screen.
                pass
            finally:
                with self._lock:
                    self._active_processes -= 1
                    if self._active_processes == 0:
                        self._idle.set()
                    if self._events.get(ident) is event:
                        self._events.pop(ident, None)
                    if self._closed and self._worker is None and self._active_processes == 0:
                        self._release_lease()

    @staticmethod
    def _tracked_subject(request: dict[str, Any]) -> str | None:
        """The subject string tracking ran on, or None when no tracking was requested."""
        return request["subject"] if request.get("provider") == "meta" and request.get("subject") else None

    @staticmethod
    def _usage_summary(root: Path, cap: float) -> dict[str, Any]:
        """Reserve every attempted provider call, including uncertain failures.

        Successful cache reads do not append a new call id. We intentionally keep
        the larger reservation after success: small refunds cannot hide retries.
        ``operations`` splits the same reservations by Meta operation (story,
        tracking, cover) so a reader can see which step held the money.
        """
        reservations: dict[str, float] = {}
        attribution: dict[str, tuple[str, str]] = {}
        try:
            for ledger in root.glob("**/meta-usage.json"):
                data = json.loads(ledger.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or not isinstance(data.get("calls"), list):
                    raise ValueError
                for entry in data["calls"]:
                    if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                        raise ValueError
                    reserved = _number(entry.get("reserved_usd"), "Recorded Meta reservation", 0, 100)
                    usage = entry.get("usage", {})
                    actual = _number(usage.get("cost_usd", 0), "Recorded Meta usage", 0, 100) if isinstance(usage, dict) else 0
                    reservations[entry["id"]] = max(reservations.get(entry["id"], 0), reserved, actual)
                    operation = entry.get("operation")
                    model = entry.get("model")
                    attribution[entry["id"]] = (operation if isinstance(operation, str) and operation else "other", model if isinstance(model, str) else "")
        except (ValueError, OSError, CampaignError) as exc:
            raise CampaignError("The campaign cost record is unreadable. Preserve this project and start a new campaign; no further Meta calls were made.") from exc
        total = round(sum(reservations.values()), 8)
        operations: dict[str, dict[str, Any]] = {name: {"reserved_usd": 0.0, "calls": 0, "model": ""} for name in _META_OPERATIONS}
        for call_id, amount in reservations.items():
            operation, model = attribution[call_id]
            bucket = operations.setdefault(operation, {"reserved_usd": 0.0, "calls": 0, "model": ""})
            bucket["reserved_usd"] = round(bucket["reserved_usd"] + amount, 8)
            bucket["calls"] += 1
            if model:
                bucket["model"] = model
        return {"cap_usd": cap, "reserved_usd": total, "remaining_usd": max(0, round(cap - total, 8)), "operations": operations}

    def _plan(self, job: dict[str, Any]) -> None:
        ident, request = job["id"], job["request"]
        root = self.root / ident
        started = time.monotonic()
        self._update(ident, status="analyzing", stage="reading", progress=0.12, message="Checking the source video.")
        source = self._source(request["paths"][0])
        info = probe(source)
        if not info.has_video or not math.isfinite(info.duration) or not 0.1 < info.duration <= _MAX_SOURCE_SECONDS:
            raise CampaignError("Choose a playable video between 0.1 seconds and 20 minutes long.")
        details = {"name": source.name, "duration": info.duration, "width": getattr(info, "display_width", info.width), "height": getattr(info, "display_height", info.height), "has_audio": info.has_audio, "has_video": info.has_video}
        self._update(ident, source=details)
        self._check_cancel(ident)
        warnings = []
        if request["captions"] and request.get("caption_provider", "local") == "local" and not local_transcriber_ready():
            warnings.append("Captions are unavailable: install a local Whisper model and retry, or render without captions. This render will omit captions.")
        if not info.has_audio and request["captions"]:
            warnings.append("This source has no audio. Captions cannot be generated.")
        self._update(ident, stage="planning", progress=0.45, message="Finding a story in your footage." if request["provider"] == "meta" else "Preparing an editable local cut.")
        cache = root / "model-plan.json"
        if request["provider"] == "meta":
            from vedit import meta

            if cache.is_file():
                proposed = json.loads(cache.read_text(encoding="utf-8"))
            else:
                self._update(ident, stage="preparing", progress=0.3, message="Making a small analysis copy; your original stays unchanged.")
                proxy = self._analysis_proxy(root, source, info, cancel_event=self._event(ident))
                self._check_cancel(ident)
                self._update(ident, stage="planning", progress=0.55, message="Finding a story in your footage.")
                proposed = meta.plan_story(proxy, goal=request["goal"], duration=info.duration, target_duration=min(info.duration, request["target_duration"]), work_dir=root / "meta", language=request["language"], budget_usd=request["budget_usd"])
                _write_json(cache, proposed)
            proposed["provider"] = "meta"
        else:
            from vedit.story import plan_local_story

            # Deterministic signal analysis: scene cuts, speech energy, optional
            # local transcript keywords. Labeled as such; it is not AI understanding.
            self._update(ident, stage="planning", progress=0.5, message="Measuring scene cuts and speech in your footage.")
            proposed = plan_local_story(source, goal=request["goal"], target_duration=request["target_duration"], work_dir=root / "analysis" / "local-story", language=request["language"] if request["language"] != "auto" else "en", transcribe=request["captions"] or local_transcriber_ready())
            proposed["provider"] = "local"
            if request["subject"]:
                warnings.append("Subject tracking needs Meta mode. This local cut will use a center crop.")
        warnings.extend(proposed.get("warnings", []))
        proposed["warnings"] = warnings
        plan = validate_plan(proposed, info.duration, original=proposed)
        # Settings are visible during review; final timing is checked against
        # the user's approved ranges when they ask to render.
        plan["effect"] = request.get("effect")
        _write_json(root / "plan.json", plan)
        self._check_cancel(ident)
        timings = dict(job.get("timings") or {}, plan_seconds=round(time.monotonic() - started, 3))
        self._update(ident, status="planned", stage="review", progress=1, message="Review the story and approve the render.", plan=plan, error=None, timings=timings)

    def _disk_preflight(self, phase: str, duration: float, info: Any, *, effect: bool = False) -> dict[str, int | str]:
        """Estimate new working files, leaving a configurable free-space cushion.

        The estimate includes the selected intermediate, final encode, archive,
        and audio work. Existing accepted revisions are already reflected in the
        volume's free-space reading and are never counted as disposable space.
        """
        try:
            minimum_mb = float(os.environ.get("VEDIT_MIN_FREE_DISK_MB", "64"))
            if not math.isfinite(minimum_mb) or not 0 <= minimum_mb <= 1_048_576:
                raise ValueError
        except ValueError as exc:
            raise CampaignError("VEDIT_MIN_FREE_DISK_MB must be a finite number between 0 and 1048576.") from exc
        duration = _number(duration, "Working duration", 0.01, _MAX_SOURCE_SECONDS)
        if phase == "analysis":
            # Small 30fps analysis MP4 plus its selected-range intermediate.
            bytes_per_second = 2_000_000
        else:
            width = max(1, getattr(info, "display_width", info.width))
            height = max(1, getattr(info, "display_height", info.height))
            input_rate = info.size_bytes / max(0.1, info.duration)
            intermediate_rate = min(60_000_000, max(2_000_000, width * height * 30 * 0.12, input_rate * 2))
            # Delivery master + ZIP copy + possible encode retry, and PCM audio.
            bytes_per_second = intermediate_rate + 3 * 3_000_000 + 768_000
            if effect:
                bytes_per_second += 8_000_000
        required = math.ceil(duration * bytes_per_second + 4 * 1024 * 1024 + minimum_mb * 1024 * 1024)
        try:
            available = int(shutil.disk_usage(self.root).free)
        except OSError as exc:
            raise CampaignError("Free disk space could not be checked. Verify the project drive is available and retry.") from exc
        if available < required:
            raise CampaignError(f"Not enough free disk space for {phase}. Estimated required: {required / 1024**2:.1f} MB; available: {available / 1024**2:.1f} MB. Free space on the project drive and retry; your verified version is preserved.")
        return {"stage": phase, "required_bytes": required, "available_bytes": available}

    def _analysis_proxy(self, root: Path, source: Path, info: Any, ranges: list[dict[str, float]] | None = None, *, cancel_event: threading.Event | None = None) -> Path:
        """Compile a canonical MP4 proxy without moving any editorial timestamps.

        Story planning reads the whole source. Tracking reads only the approved
        ranges in playback order, so its time axis is the finished story's time.
        """
        identity = {"size": source.stat().st_size, "mtime_ns": source.stat().st_mtime_ns, "ranges": ranges}
        key = hashlib.sha256(_json(identity).encode()).hexdigest()[:20]
        work = root / "analysis" / key
        proxy = work / "analysis.mp4"
        expected_duration = sum(part["end"] - part["start"] for part in ranges) if ranges else info.duration
        if proxy.is_file() and proxy.stat().st_size:
            checked = probe(proxy)
            if checked.has_video and (checked.width, checked.height) == (960, 540) and abs(checked.fps - 30) < 0.05 and abs(checked.duration - expected_duration) < 0.3:
                return proxy
        self._disk_preflight("analysis", expected_duration, info)
        work.mkdir(parents=True, exist_ok=True)
        # Fit before selecting so the selection compiler fuses downscaling into
        # its small proxy encode instead of writing a full-resolution temporary.
        ops = [{"op": "fit"}]
        if ranges:
            ops.append({"op": "select_ranges", "ranges": ranges})
        ops.append({"op": "export", "preset": "web_preview", "path": str(proxy)})
        brief = Brief(name="analysis", sources={"footage": str(source)}, ops=ops, canvas=Canvas(width=960, height=540, fps=30), hw=False, work_dir=str(work))
        compiler = compile_brief(brief, work_dir=work)
        (work / "brief.yaml").write_text(brief_dumps(brief), encoding="utf-8")
        write_codegen(compiler.pipeline)
        json.loads((work / "project.json").read_text(encoding="utf-8"))
        (work / "render.sh").read_text(encoding="utf-8")
        reports = execute(compiler.pipeline, progress=False, cancel_event=cancel_event)
        write_report(compiler.pipeline, reports, finals=compiler.finals)
        checked = probe(proxy)
        if not checked.has_video or (checked.width, checked.height) != (960, 540) or abs(checked.fps - 30) > 0.05 or abs(checked.duration - expected_duration) > 0.3:
            raise CampaignError("The analysis copy did not match the source. Try another video or use Local edit.")
        return proxy

    def _meta_captions(self, root: Path, work: Path, source: Path, ranges: list[dict[str, float]], duration: float,
                       request: dict[str, Any], *, preset_canvas: Any, cancel_event: threading.Event) -> Path | None:
        """Transcribe only the approved speech with Meta; cues land on the assembled clock.

        The WAV holds the selected ranges in playback order, so turn timestamps
        already match the finished edit without remapping. Returns the ASS file,
        or None when no speech was heard. Provider failures raise their own
        actionable error; captions never silently switch providers.
        """
        from vedit import meta

        if duration > 600:
            raise CampaignError("Meta transcription covers up to 10 minutes of selected audio. Shorten the story or use local captions.")
        if cancel_event.is_set():
            raise _Cancelled()
        wav = work / "speech-16k.wav"
        parts = []
        for index, part in enumerate(ranges):
            parts.append(f"[0:a]atrim=start={part['start']:.6f}:end={part['end']:.6f},asetpts=PTS-STARTPTS[a{index}]")
        chain = "".join(f"[a{index}]" for index in range(len(ranges)))
        graph = ";".join(parts) + f";{chain}concat=n={len(ranges)}:v=0:a=1,aformat=sample_fmts=s16:channel_layouts=mono,aresample=16000[out]"
        run_ffmpeg(["-i", str(source), "-filter_complex", graph, "-map", "[out]", "-c:a", "pcm_s16le", "-ar", "16000", "-ac", "1", str(wav)])
        if cancel_event.is_set():
            raise _Cancelled()
        result = meta.transcribe_audio(wav, duration=duration, work_dir=root / "meta", language=request["language"], budget_usd=request["budget_usd"])
        cues = [Cue(float(cue["start"]), float(cue["end"]), str(cue["text"])) for cue in result.get("cues", [])]
        if not cues:
            return None
        write_srt(cues, work / "captions.srt")
        write_ass(cues, work / "captions.ass", canvas_width=preset_canvas.width, canvas_height=preset_canvas.height)
        return work / "captions.ass"

    def _render(self, job: dict[str, Any]) -> None:
        ident, request, plan = job["id"], job["request"], job["plan"]
        root = self.root / ident
        work = root / f"render-{job['revision']}"
        started = time.monotonic()
        self._update(ident, status="rendering", stage="preparing", progress=0.05, message="Preparing your approved edit.")
        source = self._source(request["paths"][0])
        info = probe(source)
        plan = validate_plan(plan, info.duration, original=plan)
        effect = _effect(request.get("effect"), duration=plan["duration"])
        plan["effect"] = effect
        warnings = list(plan.get("warnings", []))
        preset = _ASPECTS[request["aspect"]]
        ops: list[dict[str, Any]] = []
        ranges = [{"start": segment["start"], "end": segment["end"]} for segment in plan["segments"]]
        duration = sum(segment["end"] - segment["start"] for segment in plan["segments"])
        storage = self._disk_preflight("render", duration, info, effect=effect is not None and effect["intensity"] > 0)
        self._update(ident, storage=storage)
        self._check_cancel(ident)
        work.mkdir(exist_ok=True)
        if request["provider"] == "meta" and request["subject"]:
            from vedit import meta

            self._update(ident, stage="tracking", progress=0.12, message="Following your chosen subject through the footage.")
            cache_key = hashlib.sha256(_json(ranges).encode()).hexdigest()[:20]
            cache = root / f"tracking-{cache_key}.json"
            if cache.is_file():
                tracking = json.loads(cache.read_text(encoding="utf-8"))
            else:
                proxy = self._analysis_proxy(root, source, info, ranges, cancel_event=self._event(ident))
                self._check_cancel(ident)
                proxy_info = probe(proxy)
                tracking = meta.track_subject(proxy, subject=request["subject"], duration=proxy_info.duration, work_dir=root / "meta", budget_usd=request["budget_usd"])
                if tracking.get("points"):
                    tracking["points"] = remap_proxy_points(tracking["points"], source_width=getattr(info, "display_width", info.width), source_height=getattr(info, "display_height", info.height), proxy_width=proxy_info.width, proxy_height=proxy_info.height, duration=duration)
                _write_json(cache, tracking)
            if not tracking.get("points"):
                raise CampaignError("The chosen subject was not found. Create a new plan with a clearer subject description or leave tracking off.")
            ops.extend([{"op": "select_ranges", "ranges": ranges}, {"op": "track_reframe", "aspect": request["aspect"], "points": tracking["points"]}])
            warnings.extend(tracking.get("warnings", []))
        else:
            ops.append({"op": "reframe", "aspect": request["aspect"], "mode": "fill"})
            ops.append({"op": "select_ranges", "ranges": ranges})
        if effect is not None:
            ops.append({"op": "spatial_effect", **effect})
        caption_provider = request.get("caption_provider", "local") if request["captions"] and info.has_audio else None
        if caption_provider == "meta":
            from vedit import meta

            self._update(ident, stage="transcribing", progress=0.16, message="Asking Meta to transcribe the approved speech.")
            subtitle = self._meta_captions(root, work, source, ranges, duration, request, preset_canvas=get_preset(preset).canvas, cancel_event=self._event(ident))
            warnings = [warning for warning in warnings if not warning.startswith("Captions are unavailable")]
            if subtitle is None:
                warnings.append("Meta heard no speech in the selected audio. This video has no captions.")
            else:
                ops.append({"op": "captions", "file": str(subtitle), "burn": True, "style": "boxed"})
        elif caption_provider == "local":
            if local_transcriber_ready():
                ops.append({"op": "captions", "language": request["language"], "burn": True, "style": "boxed", "format": "ass"})
                warnings = [warning for warning in warnings if not warning.startswith("Captions are unavailable")]
            elif not any(warning.startswith("Captions are unavailable") for warning in warnings):
                warnings.append("Captions are unavailable: install a local Whisper model. This video has no captions.")
        if info.has_audio:
            ops.append({"op": "loudnorm", "i": -16})
        ops.append({"op": "thumbnail", "at": min(0.5, duration / 3), "path": str(work / "cover.jpg")})
        ops.append({"op": "export", "preset": preset, "path": str(work / "video.mp4")})
        brief = Brief(name="campaign", sources={"footage": str(source)}, ops=ops, canvas=get_preset(preset).canvas, work_dir=str(work))
        (work / "brief.yaml").write_text(brief_dumps(brief), encoding="utf-8")
        compiler = compile_brief(brief, work_dir=work)
        write_codegen(compiler.pipeline)
        # Verify that the persisted program is readable before any long encode.
        json.loads((work / "project.json").read_text(encoding="utf-8"))
        if not (work / "render.sh").read_text(encoding="utf-8").startswith("#!/usr/bin/env bash"):
            raise CampaignError("The render program could not be saved. Check free disk space and retry.")
        self._check_cancel(ident)
        reports = []
        steps = compiler.pipeline.steps
        for index, step in enumerate(steps):
            self._check_cancel(ident)
            self._update(ident, stage="encoding", progress=0.2 + 0.55 * index / max(1, len(steps)), message=f"Step {index + 1} of {len(steps)}: {step.description}")
            # Each execution directory keeps its own trace without overwriting
            # the complete project.json which a person approved above.
            piece = Pipeline(name=compiler.pipeline.name, work_dir=work / "execution" / f"{index + 1:02d}", steps=[step])
            reports.extend(execute(piece, progress=False, cancel_event=self._event(ident)))
        self._check_cancel(ident)
        self._update(ident, stage="verifying", progress=0.82, message="Checking the actual exported video.")
        report_path = write_report(compiler.pipeline, reports, finals=compiler.finals)
        video = work / "video.mp4"
        output = probe(video)
        target = get_preset(preset).canvas
        if not output.has_video or (output.width, output.height) != (target.width, target.height) or output.has_audio != info.has_audio:
            raise CampaignError("The export did not pass picture and sound verification. Retry the saved plan.")
        if abs(output.duration - duration) > max(0.3, len(plan["segments"]) / 15):
            raise CampaignError("The exported duration did not match the approved story. Retry the saved plan.")
        stream = output.video
        proof = {"duration": output.duration, "width": output.width, "height": output.height, "fps": output.fps, "has_audio": output.has_audio, "has_video": output.has_video, "color_space": stream.color_space if stream else None, "color_range": stream.color_range if stream else None, "color_primaries": stream.color_primaries if stream else None, "color_transfer": stream.color_trc if stream else None, "size_bytes": output.size_bytes}
        if proof["color_space"] != "bt709" or proof["color_range"] != "tv":
            raise CampaignError("The export did not pass color verification. Retry the saved plan.")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["proof"] = proof
        report["warnings"] = warnings
        report["approved_plan"] = plan
        report["effect"] = effect
        _write_json(report_path, report)
        cover = work / "cover.jpg"
        cover_kind = "frame"
        if request["generate_cover"]:
            from vedit import meta

            self._update(ident, stage="cover", progress=0.88, message="Creating the requested campaign cover.")
            cover_cache = root / f"cover-{job['revision']}.json"
            if cover_cache.is_file():
                generated = json.loads(cover_cache.read_text(encoding="utf-8"))
            else:
                generated = meta.generate_cover(cover, prompt=plan.get("cover_prompt") or plan["title"], work_dir=root / "meta", budget_usd=request["budget_usd"])
                _write_json(cover_cache, generated)
            generated_path = Path(generated["path"]).resolve()
            try:
                generated_path.relative_to((root / "meta").resolve())
            except ValueError as exc:
                raise CampaignError("The cover could not be saved. Retry the render.") from exc
            cover = work / ("generated-cover" + generated_path.suffix.lower())
            shutil.copyfile(generated_path, cover)
            cover_kind = "generated"
            warnings.extend(generated.get("warnings", []))
        self._check_cancel(ident)
        self._update(ident, stage="packaging", progress=0.95, message="Packing your video, cover, copy, and verified edit.")
        report["warnings"] = warnings
        report["budget"] = self._usage_summary(root, request["budget_usd"])
        tracked_subject = self._tracked_subject(request)
        report["tracked_subject"] = tracked_subject
        report["caption_provider"] = caption_provider
        _write_json(report_path, report)
        copy = plan["caption"] + ("\n\n" + " ".join(tag if tag.startswith("#") else "#" + tag for tag in plan["hashtags"]) if plan["hashtags"] else "")
        (work / "post.txt").write_text(copy + "\n", encoding="utf-8")
        _write_json(work / "story.json", plan)
        bundle = work / "campaign.zip"
        pending_bundle = work / "campaign.zip.part"
        with zipfile.ZipFile(pending_bundle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for file, name in [(video, "video.mp4"), (cover, "cover" + cover.suffix.lower()), (work / "post.txt", "post.txt"), (work / "story.json", "story.json"), (work / "brief.yaml", "brief.yaml"), (report_path, "report.json")]:
                self._check_cancel(ident)
                archive.write(file, name, compress_type=zipfile.ZIP_STORED if file.suffix.lower() in {".mp4", ".jpg", ".png", ".webp"} else zipfile.ZIP_DEFLATED)
            for name in ("captions.srt", "captions.ass"):
                if (work / name).is_file():
                    archive.write(work / name, name)
        pending_bundle.replace(bundle)
        artifacts = {"video": str(video.relative_to(root)), "cover": str(cover.relative_to(root)), "bundle": str(bundle.relative_to(root)), "brief": str((work / "brief.yaml").relative_to(root)), "report": str(report_path.relative_to(root))}
        result = {f"{name}_url": self._url(ident, name, revision=job["revision"]) for name in artifacts}
        result.update(proof=proof, warnings=warnings, cover_kind=cover_kind, caption=plan["caption"], hashtags=plan["hashtags"], provider=request["provider"], revision=job["revision"], stale=False, effect=effect, tracked_subject=tracked_subject, caption_provider=caption_provider)
        self._check_cancel(ident)
        timings = dict(job.get("timings") or {}, render_seconds=round(time.monotonic() - started, 3), render_revision=job["revision"])
        self._update(ident, status="complete", stage="complete", progress=1, message="Your campaign is ready. The video passed export verification.", result=result, verified_revision=job["revision"], error=None, _artifacts=artifacts, timings=timings)

    def close(self, *, wait: bool = True) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                with self._connect() as connection:
                    rows = connection.execute("SELECT data FROM jobs WHERE status IN ('queued','analyzing','rendering')").fetchall()
                    for row in rows:
                        job = json.loads(row[0])
                        self._event(job["id"]).set()
                        changes = {"cancel_requested": True, "updated_at": _now(), "message": "Studio is stopping. Your clip, plan, and verified version are saved."}
                        job.update(changes)
                        self._save(connection, job)
        if self._worker is not None and self._worker is not threading.current_thread():
            if wait:
                self._worker.join(timeout=60)
        elif self._worker is None:
            if wait:
                self._idle.wait(timeout=60)
            if self._idle.is_set():
                self._release_lease()

    def _release_lease(self) -> None:
        with self._lock:
            if self._active_processes:
                return
            if self._worker is not None and self._worker.is_alive() and threading.current_thread() is not self._worker:
                return
            if not self._lease.closed:
                fcntl.flock(self._lease.fileno(), fcntl.LOCK_UN)
                self._lease.close()


# Short alias is convenient for native callers and integration tests.
Manager = CampaignManager
_MANAGERS: dict[Path, CampaignManager] = {}
_MANAGERS_LOCK = threading.Lock()


def get_manager() -> CampaignManager:
    workspace = Path.cwd().resolve()
    with _MANAGERS_LOCK:
        manager = _MANAGERS.get(workspace)
        if manager is None or manager._closed:
            manager = CampaignManager(workspace)
            _MANAGERS[workspace] = manager
        return manager
