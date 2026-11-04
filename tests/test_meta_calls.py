"""The Meta calls panel: one call log that every paid call path writes, and one route that reads it."""

from __future__ import annotations

import base64
import http.client
import json
import subprocess
import sys
import threading
import time
import urllib.request
import wave
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from vedit import meta
from vedit.media import MediaInfo, StreamInfo
from vedit.studio.server import StudioHandler, _LoopbackServer

SECRET = "test-secret-never-write-to-disk"
STATIC = Path(meta.__file__).parent / "studio" / "static"
GOAL = "Open on the finished oak bowl, then show the lathe."
TRANSCRIPT_WORDS = "my grandmother taught me this"
STORY = {
    "title": "Made by hand",
    "summary": "The making, then the bowl.",
    "segments": [{"start": 0.0, "end": 2.0, "label": "The process", "reason": "Hands shape the bowl at 0 to 2 s."}],
    "caption": "A bowl, made by hand.",
    "hashtags": ["woodwork"],
    "cover_prompt": "The finished bowl.",
}
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j6XkAAAAASUVORK5CYII=")
FORBIDDEN = (SECRET, "Authorization", "Bearer", GOAL, TRANSCRIPT_WORDS, "Hands shape the bowl")


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    # Nothing here may reach the real API, whatever a test forgets to stub.
    monkeypatch.setattr(meta, "_BASE", "http://127.0.0.1:9/v1")
    monkeypatch.delenv("VEDIT_META_MODE", raising=False)
    monkeypatch.delenv("VEDIT_META_REASONING", raising=False)


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_API_KEY", SECRET)
    monkeypatch.setenv("VEDIT_META_ENABLED", "1")


@pytest.fixture
def clip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "source.mp4"
    path.write_bytes(b"test media bytes; probe is the I/O boundary")
    info = MediaInfo(path=path, duration=2.0, format_name="mov,mp4,m4a,3gp,3g2,mj2", size_bytes=path.stat().st_size, bit_rate=100_000,
                     streams=(StreamInfo(index=0, codec_type="video", codec_name="h264", width=96, height=54, fps=30),))
    monkeypatch.setattr(meta, "probe", lambda _: info)
    return path


class Http:
    """Meta's boundary, answering like the real API. Each request is recorded."""

    def __init__(self, *, text: str | None = None, on_response: Any = None) -> None:
        self.text = json.dumps(STORY) if text is None else text
        self.on_response = on_response
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((method, path))
        if method == "POST" and path == "/files":
            return {"id": "file-abc123"}
        if method == "DELETE":
            return {"id": "file-abc123", "deleted": True}
        if path == "/responses/input_tokens":
            return {"input_tokens": 1200}
        if path == "/responses":
            if self.on_response:
                self.on_response()
            return {"id": "resp_live42", "status": "completed",
                    "output": [{"type": "message", "content": [{"type": "output_text", "text": self.text}]}],
                    "usage": {"input_tokens": 1200, "output_tokens": 300, "video_frames_processed": 60}}
        if path in {"/images/generations", "/images/edits"}:
            return {"data": [{"b64_json": base64.b64encode(PNG).decode()}]}
        if path == "/asr/transcribe":
            return {"audioDurationMs": 1500, "turns": [{"startMs": 0, "endMs": 1400, "transcript": TRANSCRIPT_WORDS}]}
        pytest.fail(f"Unexpected provider request: {method} {path}")


def lines(work: Path) -> list[dict[str, Any]]:
    path = work / "meta-calls.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.is_file() else []


def plan(clip: Path, work: Path) -> dict[str, Any]:
    return meta.plan_story(clip, goal=GOAL, duration=10.0, target_duration=5.0, work_dir=work, budget_usd=0.5,
                           transcript=[{"start": 0.0, "end": 2.0, "text": TRANSCRIPT_WORDS}])


# --- The log ---

def test_a_call_logs_its_start_while_it_runs_and_its_end_with_sizes_cost_and_response_id(enabled: None, clip: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work = tmp_path / "work"
    during: dict[str, Any] = {}

    def look() -> None:
        during["lines"] = lines(work)
        during["panel"] = meta.call_log(work)

    monkeypatch.setattr(meta, "_request", Http(on_response=look))
    result = plan(clip, work)

    start = during["lines"][0]
    assert len(during["lines"]) == 1 and start["event"] == "start"
    assert during["panel"]["calls"][0]["status"] == "running" and during["panel"]["totals"]["running"] == 1
    assert (start["operation"], start["model"], start["purpose"]) == ("story", meta._story_model(), "Plan the story")
    assert start["size"] == {"input_tokens": 1200} and start["reserved_usd"] > 0 and start["budget_usd"] == 0.5

    begin, end = lines(work)
    assert begin == start and end["event"] == "end" and end["id"] == start["id"]
    assert end["status"] == "done" and end["request_id"] == "resp_live42" and 0 <= end["seconds"] < 30
    assert end["size"] == {"input_tokens": 1200, "output_tokens": 300, "frames": 60}
    assert end["cost_usd"] == result["usage"]["cost_usd"] and end["cost_is_estimate"] is False

    [row] = meta.call_log(work, budget_usd=0.5)["calls"]
    assert row["status"] == "done" and row["cost_kind"] == "actual" and row["cost_usd"] == result["usage"]["cost_usd"]
    assert row["purpose"] == "Plan the story" and row["request_id"] == "resp_live42" and row["seconds"] == end["seconds"]
    assert set(row) == {"id", "started_at", "operation", "model", "purpose", "status", "seconds", "size", "reserved_usd", "cost_usd", "cost_kind", "request_id"}


def test_a_cached_answer_is_not_a_call_and_a_failed_one_is_logged_as_failed(enabled: None, clip: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work = tmp_path / "work"
    monkeypatch.setattr(meta, "_request", Http())
    plan(clip, work)
    plan(clip, work)
    assert [line["event"] for line in lines(work)] == ["start", "end"]

    broken = tmp_path / "broken"
    monkeypatch.setattr(meta, "_request", Http(text="not json"))
    with pytest.raises(meta.MetaError):
        plan(clip, broken)
    end = lines(broken)[-1]
    assert end["status"] == "failed" and end["request_id"] == "resp_live42"
    [row] = meta.call_log(broken)["calls"]
    assert row["status"] == "failed" and row["cost_usd"] > 0, "a billed failure still counts against the budget"


def test_every_paid_path_says_what_it_asked_in_plain_words(enabled: None, clip: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(meta, "_request", Http(text=""))
    work = tmp_path / "work"
    meta.subject_masks(clip, subject="the wooden bowl", duration=2.0, work_dir=work, budget_usd=0.5)
    meta.generate_background("A warm  workshop\nat dusk", size="1792x1024", work_dir=work, budget_usd=0.5)
    wav = tmp_path / "speech.wav"
    with wave.open(str(wav), "wb") as handle:
        handle.setnchannels(1), handle.setsampwidth(2), handle.setframerate(16_000), handle.writeframes(b"\0\0" * 24_000)
    meta.transcribe_audio(wav, duration=1.5, work_dir=work, budget_usd=0.5)
    monkeypatch.setattr(meta, "_request", Http(text=json.dumps({"lines": [{"index": 1, "text": "Hola"}]})))
    meta.translate_cues([{"start": 0, "end": 1, "text": TRANSCRIPT_WORDS}], "es", work_dir=work, budget_usd=0.5)
    monkeypatch.setattr(meta, "_request", Http(text=json.dumps({"moments": []})))
    meta.find_moments(clip, query="the lathe", duration=2.0, work_dir=work, budget_usd=0.5)

    panel = meta.call_log(work, budget_usd=0.5)
    rows = {row["operation"]: row for row in panel["calls"]}
    assert rows["tracking"]["purpose"] == "Cut out “the wooden bowl”" and rows["tracking"]["model"] == "sam-3.1"
    assert rows["tracking"]["size"]["frames"] == 60 and rows["tracking"]["cost_kind"] == "actual"
    assert rows["image"]["purpose"] == "Generate a background: “A warm workshop at dusk”" and rows["image"]["size"] == {"images": 1}
    assert rows["transcribe"]["purpose"] == "Transcribe speech for captions" and rows["transcribe"]["size"]["audio_seconds"] == 1
    assert rows["translate"]["purpose"] == "Translate 1 caption line to Spanish"
    assert rows["find"]["purpose"] == "Find “the lathe” in the footage"
    assert all(row["status"] == "done" for row in panel["calls"]) and panel["totals"]["calls"] == 5
    raw = (work / "meta-calls.jsonl").read_text(encoding="utf-8")
    assert TRANSCRIPT_WORDS not in raw and "Hola" not in raw and SECRET not in raw


def test_tracking_names_the_subject_and_counts_the_frames_it_reserved(enabled: None, clip: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    work = tmp_path / "work"
    monkeypatch.setattr(meta, "_request", Http(text="", on_response=lambda: seen.update(start=lines(work)[0])))
    meta.track_subject(clip, subject="the wooden bowl", duration=2.0, work_dir=work, budget_usd=0.5)
    assert seen["start"]["purpose"] == "Track “the wooden bowl”" and seen["start"]["size"] == {"frames": 62}


# --- The panel's reading of it ---

def test_totals_split_spend_from_reservations_and_match_the_budget_rule(tmp_path: Path) -> None:
    work = tmp_path / "meta"
    now = time.time()
    meta._log_call(work, {"event": "start", "id": "a" * 32, "at": now - 50, "operation": "story", "model": "muse-spark-1.3", "purpose": "Plan the story", "reserved_usd": 0.02, "size": {"input_tokens": 9}})
    meta._log_call(work, {"event": "end", "id": "a" * 32, "at": now - 10, "status": "done", "seconds": 40, "cost_usd": 0.005, "cost_is_estimate": False, "size": {"output_tokens": 4}})
    meta._log_call(work, {"event": "start", "id": "b" * 32, "at": now - 5, "operation": "review", "model": "muse-spark-1.3", "purpose": "Review the finished cut", "reserved_usd": 0.03, "pid": None})
    (work / "meta-calls.jsonl").open("a").write('{"event": "end", "id": "torn')
    panel = meta.call_log(tmp_path, budget_usd=0.25)
    assert [row["purpose"] for row in panel["calls"]] == ["Review the finished cut", "Plan the story"], "newest first"
    done, running = panel["calls"][1], panel["calls"][0]
    assert done["size"] == {"input_tokens": 9, "output_tokens": 4} and done["cost_usd"] == 0.005 and done["cost_kind"] == "actual"
    assert running["status"] == "running" and running["cost_usd"] == 0.03 and running["cost_kind"] == "reserved"
    assert panel["totals"] == {"calls": 2, "running": 1, "spent_usd": 0.035, "held_usd": 0.05, "budget_usd": 0.25, "remaining_usd": 0.2}


def test_a_call_whose_process_died_is_stopped_not_running_forever(tmp_path: Path) -> None:
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    meta._log_call(tmp_path, {"event": "start", "id": "c" * 32, "at": time.time(), "operation": "tracking", "model": "sam-3.1", "purpose": "Track it", "reserved_usd": 0.01, "pid": gone.pid})
    [row] = meta.call_log(tmp_path)["calls"]
    assert row["status"] == "stopped" and row["cost_usd"] == 0.01


def test_projects_from_before_the_log_still_show_their_calls(tmp_path: Path) -> None:
    (tmp_path / "meta").mkdir()
    (tmp_path / "meta" / "meta-usage.json").write_text(json.dumps({"calls": [
        {"id": "d" * 32, "operation": "cover", "model": "muse-image-1.0", "reserved_usd": 0.01, "status": "completed", "started_at": 1_761_900_000,
         "usage": {"cost_usd": 0.01}},
        {"id": "e" * 32, "operation": "story", "model": "muse-spark-1.3", "reserved_usd": 0.004, "status": "reserved", "started_at": 1_761_899_000},
    ]}))
    rows = {row["operation"]: row for row in meta.call_log(tmp_path)["calls"]}
    assert rows["cover"]["purpose"] == "Make a cover from a frame" and rows["cover"]["status"] == "done" and rows["cover"]["cost_kind"] == "actual"
    assert rows["story"]["status"] == "stopped" and rows["story"]["cost_kind"] == "reserved"


# --- The route, with the real HTTP client sending a real Authorization header ---

class FakeMeta(BaseHTTPRequestHandler):
    authorization: list[str] = []

    def log_message(self, *_: Any) -> None:
        pass

    def _answer(self, payload: dict[str, Any]) -> None:
        FakeMeta.authorization.append(self.headers.get("Authorization", ""))
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.path == "/v1/files":
            self._answer({"id": "file-abc123"})
        elif self.path == "/v1/responses/input_tokens":
            self._answer({"input_tokens": 1200})
        else:
            self._answer({"id": "resp_wire7", "status": "completed", "usage": {"input_tokens": 1200, "output_tokens": 300},
                          "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(STORY)}]}]})

    def do_DELETE(self) -> None:
        self._answer({"id": "file-abc123", "deleted": True})


@pytest.fixture
def studio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[int, Any]]:
    monkeypatch.chdir(tmp_path)
    from vedit.campaign import get_manager

    manager = get_manager()
    httpd = _LoopbackServer(("127.0.0.1", 0), StudioHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(httpd.server_address[1]), manager
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)
        manager.close()


def project(manager: Any, *, status: str = "complete") -> str:
    ident = "f" * 32
    now = "2026-11-04T18:00:00+00:00"
    job = {"id": ident, "status": status, "stage": status, "revision": 1, "request": {"goal": GOAL, "budget_usd": 0.5}, "created_at": now, "updated_at": now}
    with manager._connect() as connection:
        connection.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?)", (ident, "fingerprint", status, now, now, json.dumps(job)))
    (manager.root / ident).mkdir()
    return ident


def get(port: int, path: str, method: str = "GET") -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(method, path, body=b"{}" if method == "POST" else None, headers={"Content-Type": "application/json"} if method == "POST" else {})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def test_the_route_shows_calls_and_never_the_key_headers_prompt_or_transcript(studio: tuple[int, Any], enabled: None, clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    port, manager = studio
    ident = project(manager)
    FakeMeta.authorization = []
    fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeMeta)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    # The real client and its Authorization header, sent only to this loopback fake.
    monkeypatch.setattr(meta, "_BASE", f"http://127.0.0.1:{fake.server_address[1]}/v1")
    monkeypatch.setattr(meta, "build_opener", urllib.request.build_opener)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    try:
        plan(clip, manager.root / ident / "meta")
    finally:
        fake.shutdown()
        fake.server_close()
    assert FakeMeta.authorization and all(value == "Bearer " + SECRET for value in FakeMeta.authorization), "the real client sent the key"

    status, raw = get(port, f"/api/campaigns/{ident}/meta-calls")
    assert status == 200
    payload = json.loads(raw)
    [row] = payload["calls"]
    assert (row["purpose"], row["status"], row["request_id"], row["cost_kind"]) == ("Plan the story", "done", "resp_wire7", "actual")
    assert payload["totals"]["budget_usd"] == 0.5 and payload["totals"]["remaining_usd"] < 0.5 and payload["status"] == "complete"
    log = (manager.root / ident / "meta" / "meta-calls.jsonl").read_text(encoding="utf-8")
    for text in (raw.decode("utf-8"), log):
        for forbidden in FORBIDDEN:
            assert forbidden not in text


def test_the_route_404s_for_an_unknown_project_and_is_read_only(studio: tuple[int, Any]) -> None:
    port, manager = studio
    for path in (f"/api/campaigns/{'0' * 32}/meta-calls", "/api/campaigns/not-a-project/meta-calls"):
        status, raw = get(port, path)
        assert status == 404 and json.loads(raw)["ok"] is False
    ident = project(manager)
    status, _ = get(port, f"/api/campaigns/{ident}/meta-calls", method="POST")
    assert status == 405


def test_a_project_with_no_calls_yet_returns_an_empty_list(studio: tuple[int, Any]) -> None:
    port, manager = studio
    ident = project(manager, status="analyzing")
    status, raw = get(port, f"/api/campaigns/{ident}/meta-calls")
    assert status == 200
    assert json.loads(raw) == {"ok": True, "status": "analyzing", "calls": [],
                               "totals": {"calls": 0, "running": 0, "spent_usd": 0.0, "held_usd": 0.0, "budget_usd": 0.5, "remaining_usd": 0.5}}


# --- The page ---

def test_the_editor_page_loads_the_panel_and_its_script() -> None:
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    script = (STATIC / "meta-calls.js").read_text(encoding="utf-8")
    styles = (STATIC / "meta-calls.css").read_text(encoding="utf-8")
    assert '<script src="meta-calls.js"></script>' in page and '<link rel="stylesheet" href="meta-calls.css">' in page
    assert page.index('src="settings.js"') < page.index('src="meta-calls.js"'), "after the editor it listens to"
    for ident in ("meta-calls", "meta-calls-toggle", "meta-calls-list", "meta-calls-empty", "meta-calls-total"):
        assert f'id="{ident}"' in page and f'"{ident}"' in script
    assert "All calls go to the Meta Model API from this computer." in page
    assert "No Meta calls yet. They appear here as Briefcut works." in page
    assert "/meta-calls" in script and "innerHTML" not in script, "server text goes in as textContent"
    assert "Local mode" not in page and "—" not in script and "—" not in styles
    assert "var(--paper)" in styles and "#" not in styles.split("@keyframes")[0].replace("#meta", ""), "theme colors come from the shared tokens"
