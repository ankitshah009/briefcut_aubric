from __future__ import annotations

import http.client
import json
import os
import socket
import socketserver
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote

import pytest

from vedit.packaging import create_dmg, stage_app

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "src" / "vedit" / "studio" / "static" / "index.html"


def test_stage_app_and_create_dmg(tmp_path: Path) -> None:
    app = stage_app(tmp_path / "Vedit.app", package_root=ROOT)
    plist = (app / "Contents" / "Info.plist").read_text(encoding="utf-8")
    assert "ai.aubric.briefcut" in plist
    assert "Briefcut" in plist
    executable = plist.find("CFBundleExecutable")
    assert executable != -1
    assert "vedit" in plist[executable : executable + 160]

    assert "15.0" in plist
    assert "NSAllowsLocalNetworking" in plist
    assert "public.app-category.video" in plist

    launcher = app / "Contents" / "MacOS" / "vedit"
    assert launcher.is_file()
    assert os.access(launcher, os.X_OK)
    assert (app / "Contents" / "Resources" / "src" / "vedit" / "cli.py").is_file()
    blob = launcher.read_bytes()
    assert b"--open" not in blob
    assert b"webbrowser" not in blob
    if sys.platform == "darwin":
        assert blob[:4] in {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}
    else:
        text = blob.decode("utf-8")
        assert "vedit studio" in text
        assert "PYTHONPATH" in text

    recorded: list[list[str]] = []

    def fake(argv: list[str]) -> None:
        recorded.append([str(part) for part in argv])

    dest = tmp_path / "Vedit.dmg"
    create_dmg(app, dest, run=fake)
    assert recorded
    argv = recorded[0]
    assert argv[0] == "hdiutil"
    assert "UDZO" in argv
    assert str(dest) in argv
    assert dest.exists()
    assert app.exists()


def test_studio_health_and_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    with _studio_server() as port:
        status, raw = _exchange("GET", port, "/api/health")
        assert status == 200
        body = json.loads(raw.decode())
        assert body["ok"] is True

        status, raw = _exchange(
            "POST",
            port,
            "/api/run",
            {"goal": "edit the board video", "paths": [], "render": False, "analyze": False},
            timeout=20,
        )
        payload = json.loads(raw.decode())
        assert isinstance(payload, dict)
        if status == 422:
            # No media: the session is saved and returned, but a failed plan is not a success.
            assert payload["ok"] is False and payload["session"]["status"] == "failed"
            assert "no media paths" in payload["error"]
        else:
            assert status == 400


def test_a_failed_session_is_a_422_with_the_critique_as_the_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    with _studio_server() as port:
        status, raw = _exchange(
            "POST",
            port,
            "/api/run",
            {"goal": "edit the board video", "paths": [], "render": False, "analyze": False},
            timeout=20,
        )
    payload = json.loads(raw.decode())
    assert status == 422, payload
    assert payload["ok"] is False and payload["session"]["status"] == "failed"
    assert payload["error"].startswith("no media paths")
    assert Path(payload["session"]["root"]).parent == tmp_path / "vedit-sessions", (
        "the failed session is still saved in the workspace"
    )


@pytest.mark.parametrize("route", ["/api/run", "/api/plan"])
def test_a_session_root_outside_the_workspace_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    """The server writes session.json, brief.yaml and uploads into root, so root is confined to vedit-sessions."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    elsewhere = tmp_path / "elsewhere"
    monkeypatch.chdir(workspace)
    bad_roots = [
        str(elsewhere),
        str(workspace),
        str(workspace / "vedit-sessions"),
        str(workspace / "vedit-sessions" / "campaigns"),
        str(workspace / "vedit-sessions" / "a" / "deep"),
        str(workspace / "vedit-sessions" / ".hidden"),
        "../elsewhere",
    ]
    with _studio_server() as port:
        for root in bad_roots:
            status, raw = _exchange("POST", port, route, {"goal": "keep it", "paths": [], "root": root}, timeout=20)
            assert status == 400 and json.loads(raw)["ok"] is False, (root, raw)
        status, raw = _exchange("GET", port, "/api/session?root=" + quote(str(elsewhere), safe=""))
        assert status == 400, raw
        status, raw = _exchange("GET", port, "/api/media?root=" + quote(str(elsewhere), safe=""))
        assert status == 400, raw
        # Inside the workspace the same request is served: it fails only for want of media.
        status, raw = _exchange(
            "POST",
            port,
            route,
            {"goal": "keep it", "paths": [], "root": str(workspace / "vedit-sessions" / "mine")},
            timeout=20,
        )
        assert status == 422 and json.loads(raw)["session"]["root"] == str(workspace / "vedit-sessions" / "mine"), raw
    assert not elsewhere.exists() and not list(workspace.glob("*.json"))
    assert (workspace / "vedit-sessions" / "mine" / "session.json").is_file()


def test_media_paths_must_be_uploads_in_the_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Like CampaignManager._source: a path is accepted only at vedit-sessions/<folder>/inbox/<file>."""
    workspace = tmp_path / "workspace"
    inbox = workspace / "vedit-sessions" / "source-1" / "inbox"
    inbox.mkdir(parents=True)
    uploaded = inbox / "clip.mp4"
    uploaded.write_bytes(b"not probed here")
    secret = tmp_path / "secret.mp4"
    secret.write_bytes(b"x")
    monkeypatch.chdir(workspace)
    bad_paths = [
        str(secret),
        str(workspace / "vedit-sessions" / "source-1" / "clip.mp4"),
        str(inbox / "missing.mp4"),
        str(workspace / "vedit-sessions" / "campaigns" / "inbox" / "clip.mp4"),
        "clip.mp4",
        str(inbox / ".hidden.mp4"),
    ]
    with _studio_server() as port:
        for path in bad_paths:
            status, raw = _exchange("POST", port, "/api/plan", {"goal": "keep it", "paths": [path]}, timeout=20)
            assert status == 400 and json.loads(raw)["ok"] is False, (path, raw)
        status, raw = _exchange("POST", port, "/api/plan", {"goal": "keep it", "paths": [str(uploaded)]}, timeout=20)
        assert status in {400, 422}, raw  # the upload is reached and judged as media, not refused as a path
        assert "uploaded through Studio" not in json.loads(raw)["error"]


def test_studio_index() -> None:
    if not INDEX.is_file():
        pytest.skip("src/vedit/studio/static/index.html missing")
    with _studio_server() as port:
        status, raw = _exchange("GET", port, "/")
        assert status == 200
        text = raw.decode("utf-8", "replace")
        assert "Briefcut" in text
        assert "Aubric, Inc." in text


class _AssetRefs(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.refs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        table = {key: value or "" for key, value in attrs}
        if tag == "link" and "stylesheet" in table.get("rel", "") and table.get("href"):
            self.refs.append(table["href"])
        elif tag == "script" and table.get("src"):
            self.refs.append(table["src"])


def _browser_path(ref: str) -> str:
    """Resolve a document URL the way a browser does for a page at ``/``."""
    if ref.startswith(("http://", "https://")):
        raise AssertionError(f"editor asset must stay on the studio origin: {ref}")
    path = ref.split("?", 1)[0].split("#", 1)[0]
    if path.startswith("/"):
        return path
    return "/" + path


def test_editor_document_loads_its_assets() -> None:
    """GET the stylesheet and script the served editor document actually names."""
    with _studio_server() as port:
        status, raw = _exchange("GET", port, "/")
        assert status == 200
        page = raw.decode("utf-8")
        assert 'id="result"' in page
        parser = _AssetRefs()
        parser.feed(page)
        assert parser.refs
        seen: set[str] = set()
        for ref in parser.refs:
            path = _browser_path(ref)
            code, ctype, body = _fetch(port, path)
            assert code == 200, path
            assert body
            if path.endswith(".css"):
                assert ctype.startswith("text/css"), ctype
                seen.add("css")
            elif path.endswith(".js"):
                assert "javascript" in ctype, ctype
                seen.add("js")
            else:
                raise AssertionError(f"unexpected editor asset {path}")
        assert seen == {"css", "js"}
        code, _ctype, body = _fetch(port, "/static/../server.py")
        assert code == 404
        assert b"def serve" not in body


def test_both_look_previews_read_one_shared_catalogue() -> None:
    static = ROOT / "src" / "vedit" / "studio" / "static"
    preview = (static / "preview.js").read_text(encoding="utf-8")
    media = (static / "mediabin.js").read_text(encoding="utf-8")
    styles = (static / "look-styles.js").read_text(encoding="utf-8")
    assert "window.BriefcutLookStyles.preview" in preview
    assert "window.BriefcutLookStyles.inspector" in media
    assert "const LOOKS = {" not in preview
    assert "const LOOK_HINTS = {" not in media
    assert "vintage:" in styles and "cinematic:" in styles


def test_studio_holds_a_burst_of_connections() -> None:
    """A page load opens many connections at once, and a render can keep the server busy."""
    width = 48
    failures: list[str] = []

    def fetch(port: int, barrier: threading.Barrier) -> None:
        try:
            barrier.wait()
            with socket.create_connection(("127.0.0.1", port), timeout=20) as connection:
                connection.sendall(
                    f"GET /studio.css HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n".encode()
                )
                assert connection.recv(16).startswith(b"HTTP/1.")
        except (OSError, AssertionError) as exc:
            failures.append(type(exc).__name__)

    with _studio_server() as port:
        for _ in range(5):
            barrier = threading.Barrier(width)
            workers = [threading.Thread(target=fetch, args=(port, barrier)) for _ in range(width)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
    assert not failures


def test_studio_binds_without_reverse_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("studio bind must not reverse-resolve 127.0.0.1")

    monkeypatch.setattr(socket, "getfqdn", explode)
    monkeypatch.setattr(socket, "gethostbyaddr", explode)
    with _studio_server() as port:
        status, raw = _exchange("GET", port, "/", timeout=2)
    assert status == 200
    text = raw.decode("utf-8", "replace")
    assert 'id="goal"' in text
    assert 'id="clip"' in text
    assert 'id="player"' in text


def test_upload_preserves_bytes_and_isolates_duplicate_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    data = bytes(range(256)) * 4096 + b"\r\n--veditboundary\x00"
    paths = []
    with _studio_server() as port:
        for _ in range(2):
            status, raw = _exchange("POST", port, "/api/upload?name=..%2Fclip.mp4", data)
            assert status == 201
            payload = json.loads(raw)
            assert payload["bytes"] == len(data)
            path = Path(payload["paths"][0])
            path.relative_to(tmp_path)
            assert path.name == "clip.mp4"
            assert path.read_bytes() == data
            paths.append(path)
    assert paths[0] != paths[1]
    assert not list(tmp_path.rglob("*.part"))


@pytest.mark.parametrize("mode", ["empty", "oversize", "interrupted", "timeout", "content_type", "chunked"])
def test_failed_upload_leaves_no_partial_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("vedit.studio.server._MAX_UPLOAD", 100)
    monkeypatch.setattr("vedit.studio.server._UPLOAD_TIMEOUT", 0.1)
    with _studio_server() as port:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            size = {"empty": 0, "oversize": 101}.get(mode, 10)
            headers = {"Content-Length": str(size), "Content-Type": "application/octet-stream"}
            if mode == "content_type":
                headers["Content-Type"] = "text/plain"
            if mode == "chunked":
                headers["Transfer-Encoding"] = "chunked"
            conn.request("POST", "/api/upload?name=clip.mp4", body=b"", headers=headers)
            if mode == "interrupted":
                conn.send(b"short")
                conn.sock.shutdown(socket.SHUT_WR)
            response = conn.getresponse()
            assert response.status in {400, 415}
            assert json.loads(response.read())["error"]
        finally:
            conn.close()
    assert not list(tmp_path.rglob("*.part"))
    assert not list(tmp_path.rglob("clip.mp4"))
    assert not list((tmp_path / "vedit-sessions").glob("source-*"))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def _studio_server() -> Iterator[int]:
    from vedit.studio.server import serve

    holder: dict[str, socketserver.TCPServer] = {}
    errors: list[BaseException] = []
    orig_init = socketserver.TCPServer.__init__

    def init(server, server_address, handler, bind_and_activate=True):
        orig_init(server, server_address, handler, bind_and_activate)
        holder["server"] = server

    socketserver.TCPServer.__init__ = init
    port = _free_port()
    thread = _spawn(serve, port, errors)
    ready = False
    try:
        ready = _wait_health(port, thread, errors)
        socketserver.TCPServer.__init__ = orig_init
        if not ready:
            detail = errors[0] if errors else "no health response"
            raise RuntimeError(f"studio server did not start on 127.0.0.1:{port}: {detail}")
        yield port
    finally:
        socketserver.TCPServer.__init__ = orig_init
        _stop_server(holder.get("server"), thread, ready)


def _spawn(
    serve: Callable[..., None],
    port: int,
    errors: list[BaseException],
) -> threading.Thread:
    def run() -> None:
        try:
            serve("127.0.0.1", port, open_browser=False)
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def _wait_health(port: int, thread: threading.Thread, errors: list[BaseException]) -> bool:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if errors or not thread.is_alive():
            return False
        try:
            status, raw = _exchange("GET", port, "/api/health", timeout=0.5)
            if status == 200 and json.loads(raw.decode()).get("ok") is True:
                return True
        except (OSError, TimeoutError, http.client.HTTPException, json.JSONDecodeError):
            pass
        time.sleep(0.05)
    return False


def _stop_server(
    server: socketserver.TCPServer | None,
    thread: threading.Thread | None,
    ready: bool,
) -> None:
    if server is not None and ready:
        stopper = threading.Thread(target=_quiet, args=(server.shutdown,), daemon=True)
        stopper.start()
        stopper.join(timeout=2)
        _quiet(server.server_close)
    elif server is not None and thread is not None and not thread.is_alive():
        _quiet(server.server_close)
    if thread is not None:
        thread.join(timeout=2)


def _quiet(func: Callable[[], None]) -> None:
    try:
        func()
    except Exception:
        pass


def _fetch(port: int, path: str, timeout: float = 5) -> tuple[int, str, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        ctype = response.getheader("Content-Type") or ""
        return response.status, ctype, response.read()
    finally:
        conn.close()


def _exchange(
    method: str,
    port: int,
    path: str,
    body: dict | bytes | None = None,
    timeout: float = 5,
    content_type: str | None = None,
) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        data = None
        headers: dict[str, str] = {}
        if isinstance(body, dict):
            data = json.dumps(body).encode()
            headers["Content-Type"] = content_type or "application/json"
            headers["Content-Length"] = str(len(data))
        elif isinstance(body, bytes):
            data = body
            headers["Content-Type"] = content_type or "application/octet-stream"
            headers["Content-Length"] = str(len(data))
        conn.request(method, path, body=data, headers=headers)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()
