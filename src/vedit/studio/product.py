"""Studio routes for setup, settings, the project library, and diagnostics.

Every route passes the same Host and Origin check as the campaign routes.
Loopback is not authentication: a page on another site must not be able to
read settings or delete a project.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from vedit.settings import SettingsError

_PROJECT = re.compile(r"/api/projects/([a-f0-9]{32})/(thumbnail|rename|duplicate)")
_CAMPAIGN = re.compile(r"/api/campaigns/([^/]+)")
# Every model is a Meta model, so setup downloads nothing: there is no model route.
_READS = {"/api/setup", "/api/settings", "/api/projects", "/api/diagnostics"}
_WRITES = {"/api/settings", "/api/settings/test", "/api/projects/cleanup"}
_TIMEOUT = 30


def owns(method: str, path: str) -> bool:
    """True when this module answers the request."""
    if method == "DELETE":
        return _CAMPAIGN.fullmatch(path) is not None
    return path.startswith(("/api/setup", "/api/settings", "/api/projects", "/api/diagnostics"))


def handle(handler: Any, method: str, path: str, query: str) -> None:
    mutation = method != "GET"
    if not handler._campaign_origin_allowed(mutation=mutation):
        return
    from vedit.campaign import CampaignError

    try:
        if method == "GET":
            _read(handler, path, query)
        elif method == "POST":
            _write(handler, path)
        else:
            _delete(handler, path)
    except (CampaignError, SettingsError) as exc:
        handler._send_json(exc.status, {"ok": False, "error": str(exc)})
    except (FileNotFoundError, NotADirectoryError):
        handler._send_json(404, {"ok": False, "error": "This is no longer available."})
    except Exception as exc:  # noqa: BLE001  filesystem and provider errors can hold paths or keys
        # Logged by type only; the client sees a fixed sentence and never the message.
        handler.log_error("product request failed (%s)", type(exc).__name__)
        handler._send_json(
            500, {"ok": False, "error": "Studio could not finish this request. Restart it and try again."}
        )


def _read(handler: Any, path: str, query: str) -> None:
    from vedit import onboarding, projects, settings
    from vedit.campaign import get_manager

    match = _PROJECT.fullmatch(path)
    if match and match.group(2) == "thumbnail":
        _serve_thumbnail(handler, projects.thumbnail(get_manager(), match.group(1)))
    elif path == "/api/setup":
        handler._send_json(200, {"ok": True, **onboarding.setup_status(refresh="refresh=1" in query.split("&"))})
    elif path == "/api/settings":
        handler._send_json(200, {"ok": True, "settings": settings.public()})
    elif path == "/api/projects":
        handler._send_json(200, {"ok": True, **projects.library(get_manager())})
    elif path == "/api/diagnostics":
        handler._send_json(200, {"ok": True, "text": onboarding.diagnostics(_last_job(get_manager()))})
    elif path in _WRITES or match:
        handler._send_json(405, {"ok": False, "error": "This action requires POST."})
    else:
        handler._send_json(404, {"ok": False, "error": "not found"})


def _serve_thumbnail(handler: Any, target: Path) -> None:
    from vedit.studio.server import _displays_inline, _media_type

    ctype = _media_type(target)
    # Same rule as asset files: a picture shows in place, but SVG can run script, so it never does.
    if not ctype.startswith("image/") or not _displays_inline(ctype):
        ctype = "application/octet-stream"
    handler._serve_path(target, send_body=True, content_type=ctype)


def _last_job(manager: Any) -> dict[str, Any] | None:
    import json

    with manager._lock, manager._connect() as connection:
        row = connection.execute("SELECT data FROM jobs ORDER BY updated_at DESC LIMIT 1").fetchone()
    return json.loads(row[0]) if row else None


def _json_body(handler: Any) -> dict[str, Any] | None:
    """The JSON object sent, or None after answering with what was wrong."""
    if handler.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
        # A CORS-simple content type must never reach a mutation.
        handler.close_connection = True
        handler._send_json(415, {"ok": False, "error": "Send an application/json body."})
        return None
    old_timeout = handler.connection.gettimeout()
    try:
        handler.connection.settimeout(_TIMEOUT)
        ok, payload = handler._read_json()
    except TimeoutError:
        handler.close_connection = True
        handler._send_json(408, {"ok": False, "error": "The request timed out. Try again."})
        return None
    finally:
        handler.connection.settimeout(old_timeout)
    if not ok:
        return None
    if not isinstance(payload, dict):
        handler._send_json(400, {"ok": False, "error": "Send a JSON object."})
        return None
    return payload


def _write(handler: Any, path: str) -> None:
    from vedit import meta, projects, settings
    from vedit.campaign import get_manager

    match = _PROJECT.fullmatch(path)
    known = path in _WRITES or (match is not None and match.group(2) != "thumbnail")
    if not known:
        handler.close_connection = True
        status, error = (405, "This resource requires GET.") if path in _READS or match else (404, "not found")
        handler._send_json(status, {"ok": False, "error": error})
        return
    payload = _json_body(handler)
    if payload is None:
        return
    if path == "/api/settings":
        handler._send_json(200, {"ok": True, "settings": settings.update(payload)})
    elif path == "/api/settings/test":
        handler._send_json(200, {"ok": True, "access": meta.check_access()})
    elif path == "/api/projects/cleanup":
        dry_run = payload.get("dry_run", False)
        if not isinstance(dry_run, bool):
            handler._send_json(400, {"ok": False, "error": "dry_run must be true or false."})
            return
        handler._send_json(200, {"ok": True, **projects.cleanup(get_manager(), dry_run=dry_run)})
    elif match and match.group(2) == "rename":
        handler._send_json(200, {"ok": True, "project": projects.rename(get_manager(), match.group(1), payload)})
    elif match and match.group(2) == "duplicate":
        handler._send_json(201, {"ok": True, "job": projects.duplicate(get_manager(), match.group(1))})


def _delete(handler: Any, path: str) -> None:
    from vedit import projects
    from vedit.campaign import get_manager

    # This route reads no body. Do not leave one in the stream for the next request.
    if handler.headers.get("Content-Length", "0").strip() not in {"", "0"} or handler.headers.get("Transfer-Encoding"):
        handler.close_connection = True
    match = _CAMPAIGN.fullmatch(path)
    if match is None:
        handler._send_json(404, {"ok": False, "error": "not found"})
        return
    handler._send_json(200, {"ok": True, **projects.delete(get_manager(), match.group(1))})
