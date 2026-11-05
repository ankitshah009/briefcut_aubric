"""First-run setup: what this machine still needs, and how to get it.

Built on vedit.doctor. Briefcut needs two things: an FFmpeg that can render
titles and captions, and a Meta API key. Every model Briefcut calls is a Meta
model (Muse Spark, SAM 3.1, Meta Voice Transcribe, Muse Image), so nothing
here downloads a model, and setup never touches the network.
"""

from __future__ import annotations

import os
import platform
import re
import sys
from contextlib import suppress
from typing import Any

from vedit import __version__, media, settings
from vedit.doctor import META_KEY_URL, doctor_dict, ffmpeg_install
from vedit.errors import VeditError

# Filters a normal Studio render reaches for. FFmpeg builds differ on the first
# two; Homebrew's plain `ffmpeg` has neither, which is why it is not enough.
STUDIO_FILTERS = {
    "drawtext": "titles and text on the video",
    "subtitles": "captions burned into the video",
    "silencedetect": "finding speech and pauses",
    "loudnorm": "evening out loudness",
}


def _ffmpeg_item() -> dict[str, Any]:
    install = ffmpeg_install()
    item: dict[str, Any] = {
        "id": "ffmpeg",
        "title": "FFmpeg",
        "required": True,
        "ok": False,
        "why": "Briefcut reads your footage and renders the video with FFmpeg. Nothing can be made without it.",
        "install": install,
        "missing_filters": [],
    }
    report = _ffmpeg_report()
    if report is None:
        item["summary"] = "FFmpeg is not installed, or Briefcut cannot run it."
        return item
    missing, encoder = report
    item["missing_filters"] = missing
    if not encoder:
        item["summary"] = "This FFmpeg cannot encode H.264 video (libx264 is missing). Install the build below."
    elif missing:
        item["summary"] = "This FFmpeg is missing: " + "; ".join(missing) + ". Install the build below."
    else:
        item["ok"] = True
        item["summary"] = "FFmpeg is ready, with titles and captions."
    return item


def _ffmpeg_report() -> tuple[list[str], bool] | None:
    """The Studio filters this FFmpeg lacks and whether it has libx264, or None when it cannot be run."""
    try:
        tools = (media.resolve_ffmpeg(), media.resolve_ffprobe())
        if not all(os.access(tool, os.X_OK) for tool in tools):
            return None
        missing = [f"{name} ({why})" for name, why in STUDIO_FILTERS.items() if not media.has_filter(name)]
        return missing, media.has_encoder("libx264")
    except (VeditError, OSError):
        return None


def _meta_item() -> dict[str, Any]:
    from vedit import meta

    cloud = meta.capabilities()
    key = settings.meta_key_state()
    if cloud.get("available"):
        summary = "Meta is connected. Briefcut can plan, track, transcribe, and review."
    elif not key["configured"]:
        summary = "No Meta API key yet."
    else:
        summary = cloud.get("reason") or "Allow Meta calls in Settings."
    return {
        "id": "meta",
        "title": "Meta API key",
        "required": True,
        "ok": bool(cloud.get("available")),
        "why": "Briefcut runs on Meta models. Muse Spark plans the story, SAM 3.1 follows the subject, and Meta Voice Transcribe writes the captions.",
        "summary": summary,
        "action": "settings",
        "key_url": META_KEY_URL,
        "key_configured": bool(key["configured"]),
    }


def refresh_tools() -> None:
    """Forget what was learned about FFmpeg, so "Check again" sees a fresh install."""
    media._filter_names.cache_clear()
    media._encoder_names.cache_clear()
    media.ffmpeg_major.cache_clear()
    media.filter_options.cache_clear()


def setup_status(*, refresh: bool = False) -> dict[str, Any]:
    if refresh:
        refresh_tools()
    items = [_ffmpeg_item(), _meta_item()]
    try:
        checks = doctor_dict()["checks"]
    except (VeditError, OSError):
        checks = []
    return {
        "ready": all(item["ok"] for item in items if item["required"]),
        "items": items,
        "checks": checks,
        "host": host_capabilities(),
    }


def host_capabilities() -> dict[str, Any]:
    """What this machine can do, so nothing Mac-only is offered elsewhere."""
    mac = sys.platform == "darwin"
    hardware = False
    if mac:
        with suppress(VeditError, OSError):
            hardware = media.has_encoder("h264_videotoolbox")
    return {
        "platform": "macos" if mac else "linux" if sys.platform.startswith("linux") else sys.platform,
        "hardware_encoder": hardware,
        "keychain": settings.keychain_available(),
        "native_app": mac,
    }


_SECRET = (
    re.compile(r"(?i)\bbearer\s+[^\s\"']+"),
    re.compile(r"(?i)\b(?:api[_-]?key|token|secret|password)\b\s*[=:]\s*[^\s\"']+"),
)
# Anything rooted where a person's files live, up to the end of the line: names may hold spaces.
_PERSONAL_PATH = re.compile(r"(?:/Users/|/home/|/root/|/private/|/var/folders/|/tmp/|/Volumes/|/mnt/|/media/|~/).*")


def scrub(text: str, *, secrets: list[str] | None = None) -> str:
    """Remove keys and personal paths from text a person may paste in public."""
    for secret in secrets or []:
        if secret:
            text = text.replace(secret, "[key removed]")
    for pattern in _SECRET:
        text = pattern.sub("[key removed]", text)
    return _PERSONAL_PATH.sub("[path removed]", text)


def diagnostics(last_job: dict[str, Any] | None) -> str:
    """Versions, doctor results, and the last job's error, safe to paste into a bug report.

    Built from an allowlist: doctor detail text, titles, goals, file names, and
    transcripts are never read into it, so they cannot leak. `scrub` then covers
    the one free-text field, the job's error.
    """
    lines = [
        f"Briefcut {__version__}",
        f"Python {platform.python_version()}",
        f"System {platform.platform()}",
    ]
    try:
        report = doctor_dict()
        version = media.version_line().split(" Copyright")[0]
    except (VeditError, OSError):
        report, version = {"ok": False, "checks": []}, "unavailable"
    lines.append(f"FFmpeg {scrub(version)}")
    lines.append("Doctor: " + ("required tools present" if report["ok"] else "required tools missing"))
    lines.extend(
        f"  {'ok' if check['ok'] else 'no'} {check['name']}" + (" [required]" if check["required"] else "")
        for check in report["checks"]
    )
    key = settings.meta_key_state()
    lines.append(
        f"Meta: key {'configured' if key['configured'] else 'not configured'}, {'enabled' if settings.meta_enabled() else 'disabled'}, mode {settings.meta_mode()}"
    )
    if last_job is None:
        lines.append("Last job: none")
    else:
        request = last_job.get("request")
        if not isinstance(request, dict):
            request = {}
        lines.append(
            f"Last job: status {last_job.get('status')}, stage {last_job.get('stage')}, revision {last_job.get('revision')}, provider {request.get('provider')}"
        )
        error = last_job.get("error")
        if isinstance(error, str) and error:
            lines.append("Last error: " + " ".join(scrub(error, secrets=[settings.meta_api_key()]).split())[:400])
    return "\n".join(lines) + "\n"
