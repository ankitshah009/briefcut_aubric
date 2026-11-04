"""Loopback studio HTTP server. Static UI plus the session plan/render API."""

from __future__ import annotations

import json
import mimetypes
import re
import shutil
import socketserver
import webbrowser
from contextlib import suppress
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit
from urllib.request import ProxyHandler, build_opener

from vedit import __version__
from vedit.agent.loop import _session_dir, load_session, run_session
from vedit.media import plain_failure, trace
from vedit.errors import BriefError, VeditError
from vedit.project_media import MAX_SOURCE_BYTES, MediaProblem, inspect_media
from vedit.studio import product

_MAX_JSON = 1_000_000
_MAX_UPLOAD = MAX_SOURCE_BYTES
# A form upload sits in memory while it is parsed, so it keeps a small limit.
# /api/upload streams to disk and can take the full size.
_MAX_FORM_UPLOAD = 200_000_000
_UPLOAD_MARGIN = 64 * 1024 * 1024
_VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".mts", ".m2ts", ".ts", ".mpg", ".mpeg", ".wmv", ".flv", ".3gp", ".ogv"}
_UPLOAD_CHUNK = 256 * 1024
_UPLOAD_TIMEOUT = 30
_LOCAL_HOSTS = ("127.0.0.1", "localhost")
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _static_root() -> Path:
    return Path(__file__).parent / "static"


def _json_bytes(payload: dict[str, Any]) -> bytes:
    def convert(value: Any) -> str:
        if isinstance(value, Path):
            return str(value)
        raise TypeError(type(value).__name__)

    return json.dumps(payload, ensure_ascii=False, default=convert).encode("utf-8")


def _media_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".css":
        return "text/css; charset=utf-8"
    if ext == ".js":
        return "text/javascript; charset=utf-8"
    if ext in {".html", ".htm"}:
        return "text/html; charset=utf-8"
    if ext == ".mp4":
        return "video/mp4"
    if ext == ".mov":
        return "video/quicktime"
    if ext == ".webm":
        return "video/webm"
    if ext == ".m4v":
        return "video/x-m4v"
    guessed, _encoding = mimetypes.guess_type(path.name)
    if guessed is None:
        return "application/octet-stream"
    if guessed.startswith("text/"):
        return f"{guessed}; charset=utf-8"
    return guessed


def _static_relative(decoded_path: str) -> str | None:
    """Map a request path onto a file under the static directory.

    ``/static/studio.css`` is the prefixed form. ``/studio.css`` is the form
    the editor document uses: its ``href`` and ``src`` are relative to ``/``,
    so the Mac window requests those names at the site root.
    """
    prefix = "/static/"
    if decoded_path.startswith(prefix):
        rel = decoded_path[len(prefix) :]
    elif decoded_path.startswith("/") and "/" not in decoded_path[1:]:
        rel = decoded_path[1:]
    else:
        return None
    if not rel or rel.endswith("/") or "\x00" in rel or "\\" in rel:
        return None
    return rel


def _static_file(decoded_path: str) -> Path | None:
    rel = _static_relative(decoded_path)
    if rel is None:
        return None
    pure = PurePosixPath(rel)
    if pure.is_absolute():
        return None
    parts = pure.parts
    if not parts or any(part in {".", ".."} or part.startswith("/") for part in parts):
        return None
    root = _static_root().resolve()
    candidate = root.joinpath(*parts).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


def _bool_field(payload: dict[str, Any], key: str, default: bool) -> bool:
    if key not in payload:
        return default
    value = payload[key]
    if not isinstance(value, bool):
        raise BriefError(f"{key} must be a boolean")
    return value


def _form_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    token = value.strip().lower()
    if token in _TRUE:
        return True
    if token in _FALSE:
        return False
    raise BriefError(f"expected true or false, got {value!r}")


def _run_args(payload: Any, *, force_render: bool | None) -> tuple[str, list[str], bool, bool, Path | None]:
    if not isinstance(payload, dict):
        raise BriefError("JSON object required")
    goal = payload.get("goal")
    if not isinstance(goal, str):
        raise BriefError("goal must be a string")
    if "paths" not in payload:
        paths: list[str] = []
    else:
        raw_paths = payload["paths"]
        if not isinstance(raw_paths, list) or any(not isinstance(item, str) for item in raw_paths):
            raise BriefError("paths must be a list of strings")
        paths = list(raw_paths)
    body_render = _bool_field(payload, "render", False)
    analyze = _bool_field(payload, "analyze", False)
    do_render = body_render if force_render is None else force_render
    if "root" not in payload or payload["root"] is None:
        root: Path | None = None
    elif isinstance(payload["root"], str):
        if not payload["root"].strip():
            raise BriefError("root must be a non-empty string or null")
        root = Path(payload["root"])
    else:
        raise BriefError("root must be a string or null")
    return goal, paths, do_render, analyze, root


def _client_error(exc: Exception) -> str:
    """A brief error is about the request. Anything else can hold paths and commands."""
    return str(exc) if isinstance(exc, BriefError) else plain_failure(exc)


def _session_root(query: str) -> Path:
    values = parse_qs(query, keep_blank_values=True).get("root")
    if not values or values[0] == "":
        raise BriefError("root is required")
    path = Path(values[0])
    if not path.is_absolute():
        raise BriefError("root must be an absolute path")
    return path


def _safe_filename(raw: str) -> str:
    """A plain ASCII name that keeps its extension, so 写真.jpg stays a .jpg."""
    name = Path(Path(raw.replace("\\", "/")).name)
    suffix = _SAFE_NAME.sub("", name.suffix)[:16]
    stem = _SAFE_NAME.sub("_", name.stem).strip("._")
    if not stem and not suffix:
        return "clip.mp4"
    return (stem or "media")[:100] + suffix


def _store_clip(root: Path, filename: str, data: bytes) -> Path:
    inbox = (root / "inbox").resolve()
    root_resolved = root.resolve()
    inbox.relative_to(root_resolved)
    inbox.mkdir(parents=True, exist_ok=True)
    dest = inbox / _safe_filename(filename)
    stem, suffix = dest.stem, dest.suffix
    number = 2
    while dest.exists():
        dest = inbox / f"{stem}-{number}{suffix}"
        number += 1
    dest.write_bytes(data)
    stored = dest.resolve()
    stored.relative_to(root_resolved)
    return stored


def _boundary(content_type: str) -> bytes:
    for part in content_type.split(";")[1:]:
        key, _, value = part.strip().partition("=")
        if key.lower() == "boundary":
            token = value.strip().strip('"')
            if token:
                return token.encode("utf-8", "replace")
    raise BriefError("multipart boundary missing")


def _multipart_parts(body: bytes, boundary: bytes) -> list[tuple[str, str | None, bytes]]:
    marker = b"--" + boundary
    found: list[tuple[str, str | None, bytes]] = []
    for chunk in body.split(marker):
        if chunk.startswith(b"--"):
            break
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        elif chunk.startswith(b"\n"):
            chunk = chunk[1:]
        if not chunk.strip(b"\r\n"):
            continue
        sep = chunk.find(b"\r\n\r\n")
        if sep < 0:
            sep = chunk.find(b"\n\n")
            if sep < 0:
                continue
            header_raw, payload = chunk[:sep], chunk[sep + 2 :]
        else:
            header_raw, payload = chunk[:sep], chunk[sep + 4 :]
        if payload.endswith(b"\r\n"):
            payload = payload[:-2]
        elif payload.endswith(b"\n"):
            payload = payload[:-1]
        header = BytesParser().parsebytes(header_raw + b"\r\n\r\n")
        disposition = header.get("Content-Disposition", "")
        name = header.get_param("name", header="content-disposition")
        filename = header.get_filename()
        if not isinstance(name, str) or not name:
            continue
        if filename is None and "filename=" not in disposition.lower():
            found.append((name, None, payload))
        else:
            found.append((name, filename or "clip.mp4", payload))
    return found


def _output_file(root: Path) -> Path:
    if not (root / "session.json").is_file():
        raise FileNotFoundError(root / "session.json")
    session = load_session(root)
    primary = session.primary_output
    if not isinstance(primary, str) or not primary.strip():
        raise FileNotFoundError("no output")
    path = Path(primary)
    if not path.is_absolute():
        path = root / path
    resolved = path.resolve()
    resolved.relative_to(root.resolve())
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    return resolved


def _parse_range(header: str, size: int) -> tuple[int, int] | None:
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if match is None or size <= 0:
        return None
    start_s, end_s = match.group(1), match.group(2)
    if start_s == "" and end_s == "":
        return None
    if start_s == "":
        length = int(end_s)
        if length <= 0:
            return None
        start = max(0, size - length)
        end = size - 1
    else:
        start = int(start_s)
        end = int(end_s) if end_s else size - 1
    if start >= size or start > end:
        return None
    return start, min(end, size - 1)


class StudioHandler(BaseHTTPRequestHandler):
    server_version = "vedit-studio"

    def do_GET(self) -> None:
        split = urlsplit(self.path)
        path = unquote(split.path)
        if product.owns("GET", path):
            product.handle(self, "GET", path, split.query)
            return
        if path == "/api/capabilities" or path == "/api/campaigns" or path.startswith("/api/campaigns/"):
            self._handle_campaign(path, split.query, mutation=False)
            return
        if path == "/":
            self._serve_index()
            return
        if path.startswith("/static/") or _static_relative(path) is not None:
            self._serve_static(path)
            return
        if path == "/api/health":
            self._send_json(200, {"ok": True, "product": "vedit", "version": __version__})
            return
        if path == "/api/session":
            if self._campaign_origin_allowed(mutation=False):
                self._serve_session(split.query)
            return
        if path == "/api/media":
            if self._campaign_origin_allowed(mutation=False):
                self._serve_media(split.query, send_body=True)
            return
        self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        split = urlsplit(self.path)
        path = unquote(split.path)
        if product.owns("POST", path):
            product.handle(self, "POST", path, split.query)
            return
        if path in {"/api/campaigns", "/api/script"} or path.startswith("/api/campaigns/"):
            self._handle_campaign(path, split.query, mutation=True)
            return
        # Loopback is not authentication: every mutating route checks Host/Origin.
        if path in {"/api/upload", "/api/run", "/api/plan"} and not self._campaign_origin_allowed(mutation=True):
            return
        if path == "/api/upload":
            self._handle_upload(split.query)
            return
        if path == "/api/run":
            self._handle_run(force_render=None)
            return
        if path == "/api/plan":
            self._handle_run(force_render=False)
            return
        self._send_json(404, {"ok": False, "error": "not found"})

    def do_HEAD(self) -> None:
        split = urlsplit(self.path)
        path = unquote(split.path)
        if path.startswith("/api/campaigns/") and path.endswith(("/artifact", "/filmstrip")):
            self._handle_campaign(path, split.query, mutation=False, send_body=False)
            return
        if path == "/api/media":
            if self._campaign_origin_allowed(mutation=False):
                self._serve_media(split.query, send_body=False)
            return
        self._send_json(404, {"ok": False, "error": "not found"})

    def do_PUT(self) -> None:
        self._send_json(404, {"ok": False, "error": "not found"})

    def do_DELETE(self) -> None:
        split = urlsplit(self.path)
        path = unquote(split.path)
        if product.owns("DELETE", path):
            product.handle(self, "DELETE", path, split.query)
            return
        self._send_json(404, {"ok": False, "error": "not found"})

    def do_PATCH(self) -> None:
        self._send_json(404, {"ok": False, "error": "not found"})

    def do_OPTIONS(self) -> None:
        self._send_json(404, {"ok": False, "error": "not found"})

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        status = int(code)
        if status == 501:
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        self._send_json(status, {"ok": False, "error": message or "error"})

    def _campaign_origin_allowed(self, *, mutation: bool) -> bool:
        """Loopback is not authentication: reject rebinding and cross-site requests."""
        hosts = self.headers.get_all("Host", [])
        allowed = False
        host = hosts[0] if len(hosts) == 1 else ""
        try:
            parsed = urlsplit("//" + host)
            port = parsed.port if parsed.port is not None else 80
            allowed = (
                parsed.hostname in _LOCAL_HOSTS
                and parsed.username is None
                and parsed.password is None
                and not parsed.path
                and not parsed.query
                and not parsed.fragment
                and port == self.server.server_address[1]
            )
        except ValueError:
            pass
        if self.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
            allowed = False
        origins = self.headers.get_all("Origin", [])
        if origins and (len(origins) != 1 or origins[0] != "http://" + host):
            allowed = False
        if not allowed:
            self.close_connection = mutation
            self._send_json(403, {"ok": False, "error": "Open this editor on its local address to continue."})
        return allowed

    def _handle_campaign(
        self, path: str, query: str, *, mutation: bool, send_body: bool = True
    ) -> None:
        if not self._campaign_origin_allowed(mutation=mutation):
            return
        # Lazy import keeps the existing session API independent of job storage.
        from vedit.campaign import CampaignError, capabilities, get_manager, product_request

        try:
            if path == "/api/capabilities" and not mutation:
                self._send_json(200, {"ok": True, **capabilities()})
                return
            match = re.fullmatch(r"/api/campaigns(?:/([A-Za-z0-9_-]+)(?:/(render|revise|retry|cancel|artifact|timeline|find|shorts|assets|background|filmstrip|waveform|meta-calls))?)?", path)
            if match is None and path != "/api/script":
                self._send_json(404, {"ok": False, "error": "Campaign route not found."})
                return
            job_id, action = match.groups() if match else (None, None)
            manager = get_manager()
            if not mutation:
                if job_id is None:
                    self._send_json(200, {"ok": True, "jobs": manager.list()})
                elif action is None:
                    self._send_json(200, {"ok": True, "job": manager.get(job_id)})
                elif action == "artifact":
                    parameters = parse_qs(query, keep_blank_values=True)
                    names = parameters.get("name", [])
                    if len(names) != 1 or names[0] not in {"source", "video", "cover", "bundle", "brief", "report"}:
                        raise CampaignError("Choose source, video, cover, bundle, brief, or report.")
                    versions = parameters.get("v", [])
                    if versions and (len(versions) != 1 or not re.fullmatch(r"[1-9][0-9]{0,8}", versions[0])):
                        raise CampaignError("Choose one positive verified revision.")
                    revision = int(versions[0]) if versions else None
                    target = manager.artifact(job_id, names[0], revision=revision)
                    attachment = names[0] not in {"source", "video", "cover"}
                    ctype = _media_type(target)
                    if names[0] in {"source", "video"} and not ctype.startswith("video/"):
                        # Probe validates contents, not extensions. An uploaded
                        # video named .html must never execute as an HTML page.
                        ctype, attachment = "application/octet-stream", True
                    self._serve_path(target, send_body=send_body, attachment=attachment, content_type=ctype)
                elif action == "filmstrip":
                    self._serve_path(manager.filmstrip(job_id), send_body=send_body, content_type="image/jpeg")
                elif action == "assets":
                    ids = parse_qs(query).get("id", [])
                    if len(ids) != 1:
                        raise CampaignError("Choose one asset.")
                    target = manager.asset_file(job_id, ids[0])
                    ctype = _media_type(target)
                    # Same rule as the source: only plain pictures and sound display in place.
                    plain = ctype.startswith(("image/", "audio/"))
                    self._serve_path(target, send_body=send_body, attachment=not plain, content_type=ctype if plain else "application/octet-stream")
                elif action == "waveform":
                    self._send_json(200, {"ok": True, **manager.waveform(job_id)})
                elif action == "meta-calls":
                    # The project's Meta call log: purposes, models, sizes and costs only.
                    from vedit import meta

                    job = manager.get(job_id)
                    budget = (job.get("request") or {}).get("budget_usd")
                    report = meta.call_log(manager.root / job["id"], budget_usd=budget if isinstance(budget, (int, float)) and not isinstance(budget, bool) else None)
                    self._send_json(200, {"ok": True, "status": job.get("status"), **report})
                else:
                    self._send_json(405, {"ok": False, "error": "This action requires POST."})
                return
            if action in {"artifact", "filmstrip", "waveform", "meta-calls"} or (job_id is not None and action is None):
                self._send_json(405, {"ok": False, "error": "This resource requires GET."})
                return
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                self.close_connection = True
                self._send_json(415, {"ok": False, "error": "Send an application/json body."})
                return
            old_timeout = self.connection.gettimeout()
            try:
                self.connection.settimeout(_UPLOAD_TIMEOUT)
                ok, payload = self._read_json()
            except TimeoutError:
                self.close_connection = True
                self._send_json(408, {"ok": False, "error": "The request timed out. Try again."})
                return
            finally:
                self.connection.settimeout(old_timeout)
            if not ok:
                return
            if not isinstance(payload, dict):
                raise CampaignError("Send a JSON object.")
            if action == "assets":
                self._send_json(201, {"ok": True, "asset": manager.add_asset(job_id, payload)})
                return
            if action == "background":
                self._send_json(201, {"ok": True, "asset": manager.generate_background(job_id, payload)})
                return
            if action == "timeline":
                self._send_json(200, {"ok": True, "timeline": manager.timeline(job_id, payload)})
                return
            if action == "find":
                self._send_json(200, {"ok": True, **manager.find(job_id, payload)})
                return
            if action == "shorts":
                self._send_json(202, {"ok": True, "jobs": manager.shorts(job_id, payload)})
                return
            if path == "/api/script":
                self._send_json(202, {"ok": True, "job": manager.create_script(product_request(payload))})
                return
            if job_id is None:
                # Every Studio project runs on Meta. The manager never sees a local request from here.
                job = manager.create(product_request(payload))
            elif action == "render":
                job = manager.render(job_id, payload)
            elif action == "revise":
                job = manager.revise(job_id, payload)
            elif action == "retry":
                job = manager.retry(job_id)
            elif action == "cancel":
                job = manager.cancel(job_id)
            else:
                raise CampaignError("Campaign route not found.", status=404)
            self._send_json(202, {"ok": True, "job": job})
        except CampaignError as exc:
            self._send_json(exc.status, {"ok": False, "error": str(exc)})
        except UnicodeDecodeError:
            self._send_json(400, {"ok": False, "error": "Send JSON encoded as UTF-8."})
        except (FileNotFoundError, NotADirectoryError):
            self._send_json(404, {"ok": False, "error": "This artifact is no longer available. Render the campaign again."})
        except Exception as exc:
            # Provider responses and filesystem exceptions may contain credentials
            # or private filenames. Never forward these to an HTTP client.
            self.log_error("campaign request failed (%s)", type(exc).__name__)
            self._send_json(500, {"ok": False, "error": "The campaign service could not finish this request. Restart the editor and try again."})

    def _serve_index(self) -> None:
        root = _static_root().resolve()
        candidate = (root / "index.html").resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        if not candidate.is_file():
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        self._send_file(candidate)

    def _serve_static(self, path: str) -> None:
        target = _static_file(path)
        if target is None:
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        self._send_file(target)

    def _serve_session(self, query: str) -> None:
        try:
            root = _session_root(query)
        except BriefError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return
        if not (root / "session.json").is_file():
            self._send_json(404, {"ok": False, "error": "session.json missing"})
            return
        try:
            session = load_session(root)
        except FileNotFoundError as exc:
            if not (root / "session.json").is_file():
                self._send_json(404, {"ok": False, "error": "session.json missing"})
            else:
                self._send_json(400, {"ok": False, "error": str(exc)})
            return
        except VeditError as exc:
            self._send_json(400, {"ok": False, "error": _client_error(exc)})
            return
        self._send_json(200, {"ok": True, "session": session.to_dict()})

    def _serve_media(self, query: str, *, send_body: bool) -> None:
        try:
            root = _session_root(query)
            target = _output_file(root)
        except BriefError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return
        except (FileNotFoundError, ValueError):
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        self._serve_path(target, send_body=send_body)

    def _serve_path(
        self, target: Path, *, send_body: bool, attachment: bool = False, content_type: str | None = None
    ) -> None:
        try:
            size = target.stat().st_size
        except OSError:
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        ctype = content_type or _media_type(target)
        requested = self.headers.get("Range")
        if requested:
            span = _parse_range(requested, size)
            if span is None:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            start, end = span
            length = end - start + 1
            self.send_response(206)
            self.send_header("Content-Type", ctype)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            if attachment:
                self.send_header("Content-Disposition", f'attachment; filename="{_safe_filename(target.name)}"')
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(length))
            self.end_headers()
            if send_body:
                self._write_slice(target, start, length)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        if attachment:
            self.send_header("Content-Disposition", f'attachment; filename="{_safe_filename(target.name)}"')
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        if send_body:
            self._write_slice(target, 0, size)

    def _write_slice(self, path: Path, start: int, length: int) -> None:
        remaining = length
        with path.open("rb") as handle:
            handle.seek(start)
            while remaining > 0:
                chunk = handle.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _handle_upload(self, query: str) -> None:
        """Store one video, photo, or audio file, with bounded memory, for later requests.

        What the file really is comes from probing it, never its name. Video is
        judged when a project is created, as it always was, so only a file that
        claims to be something else is refused here.
        """
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/octet-stream":
            self.close_connection = True
            self._send_json(415, {"ok": False, "error": "Send the file as application/octet-stream."})
            return
        size = self._body_size(_MAX_UPLOAD, label="upload")
        if size is None:
            return
        if size == 0:
            self._send_json(400, {"ok": False, "error": "The file is empty. Choose a media file."})
            return
        filename = parse_qs(query).get("name", ["clip.mp4"])[0]
        root: Path | None = None
        pending: Path | None = None
        old_timeout = self.connection.gettimeout()
        try:
            root = _session_dir(None, "source")
            if shutil.disk_usage(root).free < size + _UPLOAD_MARGIN:
                raise BriefError("There is not enough free disk space for this file. Free some space, then try again.")
            inbox = root / "inbox"
            inbox.mkdir()
            dest = inbox / _safe_filename(filename)
            pending = inbox / ".upload.part"
            self.connection.settimeout(_UPLOAD_TIMEOUT)
            remaining = size
            with pending.open("xb") as handle:
                while remaining:
                    chunk = self.rfile.read(min(_UPLOAD_CHUNK, remaining))
                    if not chunk:
                        raise BriefError("Clip transfer was interrupted. Try again.")
                    handle.write(chunk)
                    remaining -= len(chunk)
            pending.replace(dest)
        except (OSError, BriefError) as exc:
            with suppress(OSError):
                if pending is not None:
                    pending.unlink(missing_ok=True)
                if root is not None:
                    if (root / "inbox").is_dir():
                        (root / "inbox").rmdir()
                    root.rmdir()
            self.close_connection = True
            message = "Clip transfer timed out. Try again." if isinstance(exc, TimeoutError) else str(exc)
            self._send_json(400, {"ok": False, "error": message})
            return
        finally:
            self.connection.settimeout(old_timeout)
        kind: str | None = None
        try:
            kind = inspect_media(dest).kind
        except MediaProblem as exc:
            if dest.suffix.lower() not in _VIDEO_SUFFIXES:
                with suppress(OSError):
                    dest.unlink(missing_ok=True)
                    inbox.rmdir()
                    root.rmdir()
                self._send_json(415, {"ok": False, "error": str(exc)})
                return
        except VeditError:
            pass  # With no ffprobe here, the file is judged when it is used.
        self._send_json(201, {"ok": True, "paths": [str(dest)], "bytes": size, "kind": kind})

    def _handle_run(self, *, force_render: bool | None) -> None:
        content_type = self.headers.get("Content-Type", "")
        try:
            if content_type.lower().startswith("multipart/form-data"):
                parsed = self._read_multipart(force_render)
                if parsed is None:
                    return
                goal, paths, do_render, analyze, root = parsed
                trace("multipart-stored")
            else:
                if not content_type.lower().startswith("application/json"):
                    # A CORS-simple content type must never trigger a render.
                    self._send_json(415, {"ok": False, "error": "Send JSON with Content-Type: application/json."})
                    return
                ok, payload = self._read_json()
                if not ok:
                    return
                goal, paths, do_render, analyze, root = _run_args(payload, force_render=force_render)
            trace("run-session-enter")
            session = run_session(goal, paths, root=root, render=do_render, analyze=analyze)
            trace("run-session-exit")
        except (VeditError, FileNotFoundError) as exc:
            self._send_json(400, {"ok": False, "error": _client_error(exc)})
            return
        self._send_json(200, {"ok": True, "session": session.to_dict()})

    def _read_multipart(
        self, force_render: bool | None
    ) -> tuple[str, list[str], bool, bool, Path | None] | None:
        raw = self._read_body(_MAX_FORM_UPLOAD, label="upload")
        if raw is None:
            return None
        content_type = self.headers.get("Content-Type", "")
        parts = _multipart_parts(raw, _boundary(content_type))
        fields: dict[str, str] = {}
        files: dict[str, tuple[str, bytes]] = {}
        for name, filename, payload in parts:
            if filename is None:
                fields[name] = payload.decode("utf-8", "replace")
            else:
                files[name] = (filename, payload)
        goal = fields.get("goal", "").strip()
        if not goal:
            raise BriefError("Write what you want done.")
        do_render = _form_bool(fields.get("render"), False) if force_render is None else force_render
        analyze = _form_bool(fields.get("analyze"), False)
        if fields.get("root", "").strip():
            root = Path(fields["root"]).expanduser()
            if not root.is_absolute():
                root = (Path.cwd() / root).resolve()
        else:
            root = _session_dir(None, goal)
        chosen = files.get("clip") or files.get("file")
        paths: list[str] = []
        if chosen is not None and chosen[1]:
            stored = _store_clip(root, chosen[0], chosen[1])
            paths = [str(stored)]
        elif fields.get("paths", "").strip():
            paths = [line.strip() for line in fields["paths"].splitlines() if line.strip()]
        return goal, paths, do_render, analyze, root

    def _read_json(self) -> tuple[bool, Any]:
        raw = self._read_body(_MAX_JSON, label="JSON body")
        if raw is None:
            return False, None
        try:
            return True, json.loads(raw)
        except json.JSONDecodeError:
            self._send_json(400, {"ok": False, "error": "invalid JSON"})
            return False, None

    def _body_size(self, limit: int, *, label: str) -> int | None:
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            self._send_json(400, {"ok": False, "error": "Send a body with Content-Length."})
            return None
        raw_len = self.headers.get("Content-Length")
        if raw_len is None:
            self.close_connection = True
            self._send_json(400, {"ok": False, "error": "missing Content-Length"})
            return None
        try:
            size = int(raw_len)
        except ValueError:
            self.close_connection = True
            self._send_json(400, {"ok": False, "error": "invalid Content-Length"})
            return None
        if size < 0:
            self.close_connection = True
            self._send_json(400, {"ok": False, "error": "invalid Content-Length"})
            return None
        if size > limit:
            self.close_connection = True
            self._send_json(400, {"ok": False, "error": f"{label} exceeds {limit} bytes"})
            return None
        return size

    def _read_body(self, limit: int, *, label: str) -> bytes | None:
        size = self._body_size(limit, label=label)
        if size is None:
            return None
        old_timeout = self.connection.gettimeout()
        try:
            self.connection.settimeout(_UPLOAD_TIMEOUT)
            raw = self.rfile.read(size)
        except (OSError, TimeoutError):
            self.close_connection = True
            self._send_json(400, {"ok": False, "error": f"{label} transfer timed out. Try again."})
            return None
        finally:
            self.connection.settimeout(old_timeout)
        if len(raw) != size:
            self.close_connection = True
            self._send_json(400, {"ok": False, "error": f"{label} transfer was interrupted. Try again."})
            return None
        return raw

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        try:
            body = _json_bytes(payload)
        except (TypeError, ValueError):
            status = 500
            body = b'{"ok": false, "error": "response is not JSON serializable"}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        try:
            data = path.read_bytes()
        except OSError:
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        self.send_response(200)
        self.send_header("Content-Type", _media_type(path))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class _LoopbackServer(ThreadingHTTPServer):
    """Loopback server that does not reverse-resolve its own address.

    ``HTTPServer.server_bind`` calls ``socket.getfqdn``. On a machine whose
    resolver never answers for 127.0.0.1, that call blocks and the Mac window
    stays on "Opening the editor."

    The accept queue holds a whole page load. The default of 5 resets connections
    when a render keeps this process busy, which leaves the editor half loaded.
    """

    request_queue_size = 128

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = port


def _running_studio(port: int) -> str | None:
    """The address of a Studio already answering on this port, if that is what it is."""
    url = f"http://127.0.0.1:{port}"
    try:
        with build_opener(ProxyHandler({})).open(url + "/api/health", timeout=1) as response:
            return url if json.load(response).get("product") == "vedit" else None
    except (OSError, ValueError, AttributeError):
        return None


def open_studio(port: int = 8765) -> int:
    """Open Studio for a person: join one already running, or take a free port."""
    running = _running_studio(port)
    if running is not None:
        print(f"Briefcut is already running at {running}", flush=True)
        webbrowser.open(running + "/")
        return 0
    try:
        httpd = _LoopbackServer(("127.0.0.1", port), StudioHandler)
    except OSError:
        httpd = _LoopbackServer(("127.0.0.1", 0), StudioHandler)
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    print(f"Briefcut is open at {url}\nPress Control-C to stop it.", flush=True)
    webbrowser.open(url + "/")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nBriefcut stopped.", flush=True)
    finally:
        httpd.server_close()
    return 0


def serve(host: str = "127.0.0.1", port: int = 8765, *, open_browser: bool = False) -> None:
    """Bind the studio on loopback and serve until interrupted."""
    if host not in _LOCAL_HOSTS:
        raise BriefError(f"refusing to bind {host!r}; studio allows only 127.0.0.1 or localhost")
    httpd = _LoopbackServer((host, port), StudioHandler)
    url = f"http://{host}:{port}"
    print(f"vedit studio {url}", flush=True)
    if open_browser:
        webbrowser.open(url + "/")
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
