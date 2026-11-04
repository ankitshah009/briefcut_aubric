"""Meta failure modes that a stranger's key, account and network can hit, tested at the transport boundary.

Judges run Briefcut with their own keys, on their own accounts and networks. Each
test here drives a real HTTP exchange with a fake Meta on 127.0.0.1 (or an opener
that raises what urllib raises) and checks four things: retries are bounded and
never double-charge the ledger, the message is specific and plain, the call log
marks a failed call with its reason, and the key never leaves the request.
"""

from __future__ import annotations

import email.utils
import errno
import json
import socket
import ssl
import struct
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import URLError

import pytest

from vedit import meta, settings
from vedit.media import MediaInfo, StreamInfo, plain_failure

SECRET = "sk-robust-test-secret-0000-wxyz"
FORBIDDEN = (SECRET, "Bearer", "Authorization", "echoed remote text")
GOAL = "Open on the finished oak bowl."
STORY = {
    "title": "Made by hand",
    "summary": "The making, then the bowl.",
    "segments": [{"start": 0.0, "end": 2.0, "label": "The process", "reason": "Hands shape the bowl at 0 to 2 s."}],
    "caption": "A bowl, made by hand.",
    "hashtags": ["woodwork"],
    "cover_prompt": "The finished bowl.",
}
SAM_LINE = "<{frame}f>1<|box;x1=10;y1=10;x2=50;y2=50;w=96;h=54;c=0.9|>"


# --- Fixtures -------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    for name in ("VEDIT_META_MODE", "VEDIT_META_BASE_URL", "VEDIT_META_TIMEOUT", "VEDIT_META_REASONING", "MODEL_API_KEY", "VEDIT_META_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(meta, "_BASE", "http://127.0.0.1:9/v1")
    sleeps: list[float] = []
    monkeypatch.setattr(meta.time, "sleep", sleeps.append)  # backoff is recorded, never waited for
    return sleeps


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


def answer(status: int = 200, payload: Any = None, *, headers: dict[str, str] | None = None) -> tuple:
    return ("json", status, {} if payload is None else payload, headers or {})


def remote_error(code: str = "model_not_found") -> dict[str, Any]:
    # Meta's message is client-safe, but it is still never shown: it could echo anything.
    return {"error": {"type": "invalid_request_error", "code": code, "param": "model", "message": f"echoed remote text {SECRET}"}}


def completed(text: str) -> dict[str, Any]:
    return {"id": "resp_robust1", "status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
            "usage": {"input_tokens": 1200, "output_tokens": 300}}


class Fake(BaseHTTPRequestHandler):
    """Meta's routes on 127.0.0.1. ``script`` maps "METHOD /path" to answers used in turn; the last one repeats."""

    protocol_version = "HTTP/1.1"
    script: dict[str, list[tuple]] = {}
    seen: list[tuple[str, str]] = []
    authorizations: list[str] = []

    def log_message(self, *_: Any) -> None:
        pass

    def finish(self) -> None:
        try:
            super().finish()
        except OSError:
            pass  # the test reset this socket on purpose

    def _route(self) -> None:
        route = f"{self.command} {self.path.removeprefix('/v1')}"
        Fake.seen.append((self.command, self.path.removeprefix("/v1")))
        Fake.authorizations.append(self.headers.get("Authorization", ""))
        answers = Fake.script.get(route) or [answer()]
        kind, *rest = answers.pop(0) if len(answers) > 1 else answers[0]
        length = int(self.headers.get("Content-Length", "0"))
        if kind == "reset":
            self.rfile.read(min(length, 4096))
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            self.connection.close()
            self.close_connection = True
            return
        self.rfile.read(length)
        if kind == "hang":
            threading.Event().wait(rest[0])
            self.close_connection = True
            return
        if kind == "json":
            status, payload, headers = rest
            self._send(status, json.dumps(payload).encode(), "application/json", headers)
        elif kind == "raw":
            status, body, content_type = rest
            self._send(status, body, content_type, {})
        elif kind == "stream":
            chunks, finish = rest
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for chunk in chunks:
                self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
            if finish:
                self.wfile.write(b"0\r\n\r\n")
            else:
                self.wfile.write(b"4000\r\n" + b"data: {\"type\":")  # a chunk that never ends
            self.wfile.flush()
            self.close_connection = True

    def _send(self, status: int, body: bytes, content_type: str, headers: dict[str, str]) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_DELETE = _route


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch, enabled: None):
    Fake.script, Fake.seen, Fake.authorizations = {}, [], []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(meta, "_BASE", f"http://127.0.0.1:{server.server_address[1]}/v1")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    try:
        yield Fake
    finally:
        server.shutdown()
        server.server_close()


def story_routes(*responses: tuple) -> dict[str, list[tuple]]:
    return {"POST /files": [answer(payload={"id": "file-abc123"})],
            "POST /responses/input_tokens": [answer(payload={"input_tokens": 1200})],
            "POST /responses": list(responses),
            "DELETE /files/file-abc123": [answer(payload={"deleted": True})]}


def plan(clip: Path, work: Path, budget: float = 0.5) -> dict[str, Any]:
    return meta.plan_story(clip, goal=GOAL, duration=2.0, target_duration=2.0, work_dir=work, budget_usd=budget)


def ledger(work: Path) -> list[dict[str, Any]]:
    path = work / "meta-usage.json"
    return json.loads(path.read_text(encoding="utf-8"))["calls"] if path.is_file() else []


def written(work: Path) -> str:
    return "".join(path.read_text(encoding="utf-8", errors="replace") for path in work.rglob("*") if path.is_file())


def count(route: tuple[str, str]) -> int:
    return Fake.seen.count(route)


def clean_text(text: str) -> None:
    for word in FORBIDDEN:
        assert word not in text


class Raising:
    """An opener that fails the way urllib does before any byte reaches Meta."""

    def __init__(self, error: BaseException) -> None:
        self.error, self.calls = error, 0

    def open(self, request: Any, *, timeout: float) -> Any:
        self.calls += 1
        raise self.error


# --- HTTP statuses --------------------------------------------------------

@pytest.mark.parametrize(("status", "path", "model", "words", "billed"), [
    (400, "/responses", "muse-spark-1.3", ("refused the request as invalid", "model_not_found", "nothing was billed"), False),
    (401, "/responses", "muse-spark-1.3", ("rejected the API key", "MODEL_API_KEY", "ending in wxyz", "dev.meta.ai"), False),
    (402, "/responses", "muse-spark-1.3", ("billing or credits",), False),
    (403, "/responses", "muse-wonder-1.0", ("not allowed to use muse-wonder-1.0", "Muse Spark as the director"), False),
    (403, "/responses", "sam-3.1", ("not allowed to use sam-3.1", "Cutouts"), False),
    (404, "/responses", "muse-spark-9.9", ("does not offer muse-spark-9.9", "vedit meta check"), False),
    (413, "/files", "", ("upload size",), False),
    (413, "/asr/transcribe", "", ("32 MB",), False),
    (500, "/responses", "muse-spark-1.3", ("internal error", "may have been billed"), None),
    (502, "/responses", "muse-spark-1.3", ("unavailable (HTTP 502", "may have been billed"), None),
    (503, "/responses", "muse-spark-1.3", ("unavailable (HTTP 503",), None),
    (504, "/responses", "muse-spark-1.3", ("unavailable (HTTP 504",), None),
])
def test_each_status_gets_its_own_plain_message_without_meta_text_or_the_key(
        fake: type[Fake], status: int, path: str, model: str, words: tuple[str, ...], billed: bool | None) -> None:
    fake.script = {f"POST {path}": [answer(status, remote_error())]}
    payload = {"model": model} if model else None
    with pytest.raises(meta.MetaError) as caught:
        meta._request("POST", path, payload=payload, body=None if payload else b"x")
    message = str(caught.value)
    for word in words:
        assert word in message
    clean_text(message)
    assert caught.value.status == status and caught.value.billed is billed and caught.value.plain == message
    assert fake.authorizations == ["Bearer " + SECRET] * len(fake.seen)
    # A paid call is sent once: a 5xx may have run, so trying again could bill twice.
    assert len(fake.seen) == (1 if path != "/files" or status < 500 else 3)


def test_free_calls_retry_5xx_with_backoff_and_paid_calls_never_do(fake: type[Fake], clean: list[float]) -> None:
    fake.script = {"POST /responses/input_tokens": [answer(503), answer(502), answer(payload={"input_tokens": 7})],
                   "POST /responses": [answer(503)]}
    assert meta._request("POST", "/responses/input_tokens", payload={"model": "muse-spark-1.3"}, retry_safe=True) == {"input_tokens": 7}
    assert count(("POST", "/responses/input_tokens")) == 3 and len(clean) == 2
    assert 0.5 <= clean[0] <= 1.0 and 1.0 <= clean[1] <= 2.0, "exponential backoff with jitter"
    with pytest.raises(meta.MetaError, match="HTTP 503"):
        meta._request("POST", "/responses", payload={"model": "muse-spark-1.3"})
    assert count(("POST", "/responses")) == 1


# --- 429 ------------------------------------------------------------------

def test_a_rate_limited_paid_call_honours_retry_after_and_settles_one_reservation(fake: type[Fake], clip: Path, tmp_path: Path, clean: list[float]) -> None:
    fake.script = story_routes(answer(429, remote_error("rate_limit_exceeded"), headers={"Retry-After": "2"}), answer(429), answer(payload=completed(json.dumps(STORY))))
    work = tmp_path / "work"
    result = plan(clip, work)
    assert result["title"] == "Made by hand"
    assert count(("POST", "/responses")) == 3 and clean[0] >= 2 and len(clean) == 2
    [call] = ledger(work)
    assert call["status"] == "completed" and call["usage"]["cost_usd"] > 0, "one call, one reservation, however many 429s"
    [row] = meta.call_log(work)["calls"]
    assert row["status"] == "done" and "reason" not in row


def test_a_long_retry_after_hands_back_with_the_wait_and_frees_the_budget(fake: type[Fake], clip: Path, tmp_path: Path, clean: list[float]) -> None:
    later = email.utils.formatdate(time.time() + 120, usegmt=True)
    fake.script = story_routes(answer(429, remote_error("rate_limit_exceeded"), headers={"Retry-After": later}))
    work = tmp_path / "work"
    with pytest.raises(meta.MetaError, match=r"rate limit was reached.*wait 1[12]\d s.*Nothing was billed") as caught:
        plan(clip, work)
    assert caught.value.retry_after == pytest.approx(120, abs=3) and clean == [] and count(("POST", "/responses")) == 1
    [call] = ledger(work)
    assert call["status"] == "failed_or_incomplete" and call["reserved_usd"] == 0 and call["released"] is True
    panel = meta.call_log(work, budget_usd=0.5)
    [row] = panel["calls"]
    assert row["status"] == "failed" and "rate limit" in row["reason"] and row["released"] is True
    assert row["cost_usd"] == 0 and panel["totals"]["held_usd"] == 0 and panel["totals"]["remaining_usd"] == 0.5
    clean_text(written(work))


def test_a_429_without_retry_after_backs_off_three_times_then_stops(fake: type[Fake], clean: list[float]) -> None:
    fake.script = {"POST /responses": [answer(429)]}
    with pytest.raises(meta.MetaError, match="rate limit was reached") as caught:
        meta._request("POST", "/responses", payload={"model": "muse-spark-1.3"})
    assert count(("POST", "/responses")) == 3 and len(clean) == 2 and caught.value.billed is False


def test_retry_after_reads_seconds_and_dates_and_ignores_nonsense() -> None:
    assert meta._retry_after({"Retry-After": "7"}) == 7
    assert meta._retry_after({"Retry-After": email.utils.formatdate(time.time() + 30, usegmt=True)}) == pytest.approx(30, abs=2)
    assert meta._retry_after({"Retry-After": email.utils.formatdate(time.time() - 30, usegmt=True)}) == 0, "a clock behind Meta's never waits backwards"
    assert meta._retry_after({"Retry-After": "soon"}) is None and meta._retry_after({}) is None and meta._retry_after(None) is None


# --- Network: connect, send, read -----------------------------------------

def cert_error(why: str) -> ssl.SSLCertVerificationError:
    error = ssl.SSLCertVerificationError(1, f"[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: {why}")
    error.verify_message = why
    return error


@pytest.mark.parametrize(("reason", "words", "tries"), [
    (socket.gaierror(8, "nodename nor servname provided"), ("could not look up api.meta.ai (DNS)",), 3),
    (ConnectionRefusedError(61, "Connection refused"), ("refused the connection",), 3),
    (OSError(errno.ENETUNREACH, "Network is unreachable"), ("looks offline",), 3),
    ("unknown url type: htps", ("network timeout",), 1),
    (TimeoutError("timed out"), ("network timeout of 180 s", "VEDIT_META_TIMEOUT"), 1),
    (ConnectionResetError(54, "Connection reset by peer"), ("dropped while Briefcut was sending",), 1),
    (cert_error("certificate is not yet valid"), ("date or time is wrong",), 1),
    (cert_error("certificate has expired"), ("date or time is wrong",), 1),
    (cert_error("self-signed certificate in certificate chain"), ("replaced api.meta.ai's security certificate",), 1),
    (cert_error("unable to get local issuer certificate"), ("SSL_CERT_FILE",), 1),
])
def test_a_request_meta_never_received_says_why_and_is_never_billed(
        enabled: None, monkeypatch: pytest.MonkeyPatch, reason: BaseException, words: tuple[str, ...], tries: int) -> None:
    monkeypatch.setattr(meta, "_BASE", "https://api.meta.ai/v1")
    opener = Raising(URLError(reason))
    monkeypatch.setattr(meta, "build_opener", lambda *_: opener)
    with pytest.raises(meta.MetaError) as caught:
        meta._request("POST", "/responses", payload={"model": "muse-spark-1.3"})
    for word in words:
        assert word in str(caught.value)
    assert "Nothing" in str(caught.value) or "nothing" in str(caught.value)
    assert caught.value.billed is False
    # A paid call goes again only when the connection never opened, so it cannot run twice.
    assert opener.calls == tries
    clean_text(str(caught.value))


def test_a_real_refused_port_is_reported_and_retried(enabled: None, monkeypatch: pytest.MonkeyPatch) -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    monkeypatch.setattr(meta, "_BASE", f"http://127.0.0.1:{port}/v1")
    with pytest.raises(meta.MetaError, match="refused the connection") as caught:
        meta._request("GET", "/models", retry_safe=True)
    assert caught.value.billed is False


def test_a_read_timeout_on_a_paid_call_is_sent_once_and_stays_reserved(fake: type[Fake], clip: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(meta, "_RESPONSE_TIMEOUT", 0.4)
    fake.script = story_routes(("hang", 1.5))
    work = tmp_path / "work"
    with pytest.raises(meta.MetaError, match=r"took longer than 0.4 s.*may have been billed.*VEDIT_META_TIMEOUT") as caught:
        plan(clip, work)
    assert caught.value.billed is None and count(("POST", "/responses")) == 1
    assert count(("DELETE", "/files/file-abc123")) == 1, "the upload is still deleted"
    [call] = ledger(work)
    assert call["status"] == "failed_or_incomplete" and call["reserved_usd"] > 0 and "released" not in call
    [row] = meta.call_log(work)["calls"]
    assert row["status"] == "failed" and "took longer" in row["reason"] and row["cost_kind"] == "reserved"


def test_a_read_timeout_on_a_free_call_is_retried(fake: type[Fake], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(meta, "_TIMEOUT", 0.4)
    fake.script = {"GET /models": [("hang", 1.0), answer(payload={"data": [{"id": "muse-spark-1.3"}]})]}
    assert meta._request("GET", "/models", retry_safe=True)["data"][0]["id"] == "muse-spark-1.3"
    assert count(("GET", "/models")) == 2


def test_a_reset_mid_upload_is_retried_because_uploads_are_free(fake: type[Fake], clip: Path, tmp_path: Path) -> None:
    big = clip.with_name("big.mp4")
    big.write_bytes(bytes(16_000_000))
    fake.script = story_routes(answer(payload=completed(json.dumps(STORY))))
    fake.script["POST /files"] = [("reset",), answer(payload={"id": "file-abc123"})]
    work = tmp_path / "work"
    assert plan(big, work)["title"] == "Made by hand"
    assert count(("POST", "/files")) == 2 and count(("POST", "/responses")) == 1
    [call] = ledger(work)
    assert call["status"] == "completed"


def test_a_cutout_whose_upload_keeps_resetting_frees_its_reservation(fake: type[Fake], clip: Path, tmp_path: Path) -> None:
    big = clip.with_name("big.mp4")
    big.write_bytes(bytes(16_000_000))
    fake.script = {"POST /files": [("reset",)]}
    work = tmp_path / "work"
    with pytest.raises(meta.MetaError, match="(?i)connection") as caught:
        meta.track_subject(big, subject="the bowl", duration=2.0, work_dir=work, budget_usd=0.05)
    assert caught.value.billed is False and count(("POST", "/files")) == 3 and count(("POST", "/responses")) == 0
    [call] = ledger(work)
    assert call["reserved_usd"] == 0 and call["released"] is True, "SAM never ran, so the frames it reserved come back"
    [row] = meta.call_log(work)["calls"]
    assert row["status"] == "failed" and row["released"] is True and row["reason"]


# --- SAM streams ----------------------------------------------------------

def sse(kind: str, **data: Any) -> bytes:
    return f"event: {kind}\ndata: {json.dumps({'type': kind, **data})}\n\n".encode()


def deltas(frames: int) -> list[bytes]:
    return [sse("response.created", response={"status": "in_progress"})] + [
        sse("response.output_text.delta", delta=SAM_LINE.format(frame=frame) + "\n") for frame in range(frames)]


@pytest.mark.parametrize(("chunks", "finish", "words"), [
    (deltas(20), True, "ended early"),
    (deltas(20) + [b"data: {\"type\": \"response.output_te"], True, "cut off or garbled"),
    (deltas(20), False, "connection closed partway"),
    (deltas(5) + [sse("error", error={"code": "server_overloaded", "message": f"echoed remote text {SECRET}"})], True, "server_overloaded"),
    ([sse("response.completed", response={"status": "incomplete", "incomplete_details": {"reason": "content_filter"}, "usage": {"video_frames_processed": 60}})],
     True, "did not finish"),
])
def test_a_truncated_sam_stream_fails_with_a_reason_and_keeps_its_reservation(
        fake: type[Fake], clip: Path, tmp_path: Path, chunks: list[bytes], finish: bool, words: str) -> None:
    fake.script = {"POST /files": [answer(payload={"id": "file-abc123"})], "POST /responses": [("stream", chunks, finish)],
                   "DELETE /files/file-abc123": [answer(payload={"deleted": True})]}
    work = tmp_path / "work"
    with pytest.raises(meta.MetaError, match=words) as caught:
        meta.subject_masks(clip, subject="the bowl", duration=2.0, work_dir=work, budget_usd=0.05)
    clean_text(str(caught.value))
    assert count(("POST", "/responses")) == 1 and count(("DELETE", "/files/file-abc123")) == 1
    assert not list(work.rglob("*.sam")), "a partial mask stream is never kept as if it were whole"
    [call] = ledger(work)
    assert call["status"] == "failed_or_incomplete" and "released" not in call, "frames may have been billed"
    [row] = meta.call_log(work)["calls"]
    assert row["status"] == "failed" and row["reason"] and row["cost_usd"] > 0, "counted: reserved, or what Meta said it billed"
    clean_text(written(work))


# --- Malformed and off-schema answers --------------------------------------

class Http:
    """Meta's boundary answering every route; ``text`` is the model's output."""

    def __init__(self, text: str) -> None:
        self.text, self.calls = text, []

    def __call__(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((method, path))
        if path == "/files":
            return {"id": "file-abc123"}
        if method == "DELETE":
            return {"deleted": True}
        if path == "/responses/input_tokens":
            return {"input_tokens": 1200}
        return completed(self.text)


@pytest.mark.parametrize(("text", "words"), [
    ("null", "invalid story"),
    ("{not json", "malformed story JSON"),
    (json.dumps({key: value for key, value in STORY.items() if key != "title"}), "did not match the format Briefcut asked for: title"),
    (json.dumps(dict(STORY, segments=[dict(STORY["segments"][0], start="0")])), "did not match the format Briefcut asked for: segment start must be a finite number"),
    (json.dumps(dict(STORY, hashtags="woodwork")), "invalid hashtags"),
])
def test_an_answer_off_the_schema_is_refused_whole_and_stays_billed(
        enabled: None, clip: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str, words: str) -> None:
    monkeypatch.setattr(meta, "_request", Http(text))
    work = tmp_path / "work"
    with pytest.raises(meta.MetaError, match=words) as caught:
        plan(clip, work)
    assert str(caught.value).startswith("Meta")
    [call] = ledger(work)
    assert call["status"] == "failed_or_incomplete" and call["usage"]["cost_usd"] > 0 and "released" not in call
    [row] = meta.call_log(work)["calls"]
    assert row["status"] == "failed" and row["reason"].startswith("Meta")


def test_extra_fields_in_an_answer_are_ignored_not_fatal(enabled: None, clip: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(meta, "_request", Http(json.dumps(dict(STORY, mood="warm", segments=[dict(STORY["segments"][0], extra=1)]))))
    result = plan(clip, tmp_path / "work")
    assert "mood" not in result and set(result["segments"][0]) == {"start", "end", "label", "reason"}


@pytest.mark.parametrize(("body", "content_type", "words"), [
    (b"<html><body>Sign in to the hotel Wi-Fi</body></html>", "text/html; charset=utf-8", "web page answered"),
    (b"{\"truncated\": ", "application/json", "unreadable response"),
    (b"[1, 2]", "application/json", "unexpected shape"),
])
def test_a_200_that_is_not_meta_json_is_named(fake: type[Fake], body: bytes, content_type: str, words: str) -> None:
    fake.script = {"GET /models": [("raw", 200, body, content_type)]}
    result = meta.check_access()
    assert result["ok"] is False and words in result["reason"]


# --- Transcription --------------------------------------------------------

def wav(path: Path, seconds: float = 1.5) -> Path:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes(bytes(int(16_000 * seconds) * 2))
    return path


def test_an_empty_transcription_completes_with_a_warning_and_bills_only_the_audio(fake: type[Fake], tmp_path: Path) -> None:
    fake.script = {"POST /asr/transcribe": [answer(payload={"audioDurationMs": 1500, "turns": []})]}
    work = tmp_path / "work"
    result = meta.transcribe_audio(wav(tmp_path / "speech.wav"), duration=1.5, work_dir=work)
    assert result["cues"] == [] and "heard no speech" in result["warning"]
    [call] = ledger(work)
    assert call["status"] == "completed" and call["usage"]["cost_usd"] == pytest.approx(1 / 3600 * 0.18)


def test_a_transcription_rate_limit_is_retried_and_its_model_named_on_refusal(fake: type[Fake], tmp_path: Path) -> None:
    fake.script = {"POST /asr/transcribe": [answer(429), answer(payload={"audioDurationMs": 1500, "turns": [{"startMs": 0, "endMs": 1400, "transcript": "hello"}]})]}
    result = meta.transcribe_audio(wav(tmp_path / "speech.wav"), duration=1.5, work_dir=tmp_path / "work")
    assert result["cues"][0]["text"] == "hello" and count(("POST", "/asr/transcribe")) == 2
    fake.script = {"POST /asr/transcribe": [answer(403, remote_error("permission_denied"))]}
    with pytest.raises(meta.MetaError, match="not allowed to use muse-voice-transcribe-1.0.*Meta captions need it"):
        meta.transcribe_audio(wav(tmp_path / "other.wav", 1.2), duration=1.2, work_dir=tmp_path / "other")
    assert ledger(tmp_path / "other")[0]["reserved_usd"] == 0, "a refused transcription is not billed"
    # Meta bills no transcription that fails before a transcript, so a 5xx is retried and its money comes back.
    fake.script = {"POST /asr/transcribe": [answer(500, remote_error("server_error"))]}
    before = count(("POST", "/asr/transcribe"))
    with pytest.raises(meta.MetaError, match="internal error") as caught:
        meta.transcribe_audio(wav(tmp_path / "third.wav", 1.1), duration=1.1, work_dir=tmp_path / "third")
    assert "may have been billed" not in str(caught.value) and count(("POST", "/asr/transcribe")) - before == 3
    assert ledger(tmp_path / "third")[0]["reserved_usd"] == 0


# --- Budget ---------------------------------------------------------------

def test_running_out_of_budget_mid_project_says_how_much_to_raise_it(enabled: None, clip: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    http = Http(json.dumps(STORY))
    monkeypatch.setattr(meta, "_request", http)
    work = tmp_path / "work"
    plan(clip, work, budget=0.072)  # reserves about $0.0711, then holds the $0.0028 Meta billed
    with pytest.raises(meta.MetaError) as caught:
        meta.plan_story(clip, goal="A second, different cut.", duration=2.0, target_duration=2.0, work_dir=work, budget_usd=0.072)
    message = str(caught.value)
    assert "$0.0028 of the $0.0720 campaign budget is already reserved" in message and "short" in message
    assert "Raise the campaign budget to at least $0.08" in message and "Nothing was billed" in message
    assert caught.value.billed is False and len(ledger(work)) == 1, "a refused call reserves nothing"
    assert http.calls.count(("DELETE", "/files/file-abc123")) == 2, "the upload made before the count is still deleted"


# --- Accounts: contributor mode, key sources -------------------------------

@pytest.mark.parametrize(("mode", "model", "hint"), [
    ("contributor", "muse-spark-1.3-contributor", "VEDIT_META_MODE=standard"),
    ("standard", "muse-spark-1.3", "vedit meta check"),
])
def test_a_model_this_account_cannot_use_is_named_with_the_way_out(
        fake: type[Fake], clip: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, model: str, hint: str) -> None:
    monkeypatch.setenv("VEDIT_META_MODE", mode)
    fake.script = story_routes(answer(payload=completed(json.dumps(STORY))))
    fake.script["POST /responses/input_tokens"] = [answer(403, remote_error("model_not_enabled"))]
    with pytest.raises(meta.MetaError, match=f"not allowed to use {model} .*{hint}"):
        plan(clip, tmp_path / "work")
    assert count(("POST", "/responses/input_tokens")) == 1, "a refusal is not retried"
    assert count(("POST", "/responses")) == 0 and count(("DELETE", "/files/file-abc123")) == 1
    assert ledger(tmp_path / "work") == []


@pytest.mark.parametrize("source", ["environment", "settings", "env_local"])
def test_a_rejected_key_says_which_key_was_sent_and_never_the_key(fake: type[Fake], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str) -> None:
    monkeypatch.delenv("MODEL_API_KEY")
    monkeypatch.delenv("VEDIT_META_ENABLED")
    if source == "settings":
        settings.update({"meta_api_key": SECRET, "meta_enabled": True})
    else:
        monkeypatch.setenv("VEDIT_META_ENABLED", "1")
        if source == "environment":
            monkeypatch.setenv("MODEL_API_KEY", SECRET)
        else:
            env = tmp_path / ".env.local"
            env.write_text(f"MODEL_API_KEY='{SECRET}'\n", encoding="utf-8")
            monkeypatch.setenv("MODEL_API_KEY", "")  # empty counts as unset; monkeypatch removes what the loader sets
            assert settings.load_env_file(env) == ["MODEL_API_KEY"]
    fake.script = {"GET /models": [answer(401, remote_error("invalid_api_key"))]}
    result = meta.check_access()
    assert result["ok"] is False and "rejected the API key" in result["reason"] and "ending in wxyz" in result["reason"]
    assert ("saved in Settings" if source == "settings" else ".env.local") in result["reason"]
    clean_text(result["reason"])
    assert count(("GET", "/models")) == 1


@pytest.mark.parametrize("damaged", ["“" + SECRET + "”", SECRET[:10] + " " + SECRET[10:], SECRET + " x"])
def test_a_key_damaged_by_copy_and_paste_is_refused_before_the_network(enabled: None, monkeypatch: pytest.MonkeyPatch, damaged: str) -> None:
    monkeypatch.setenv("MODEL_API_KEY", damaged)
    monkeypatch.setattr(meta, "build_opener", lambda *_: pytest.fail("a damaged key must never be sent"))
    with pytest.raises(meta.MetaError, match="copy and paste") as caught:
        meta._request("GET", "/models")
    assert SECRET not in str(caught.value)


def test_a_key_pasted_with_its_bearer_prefix_is_sent_once(fake: type[Fake], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_API_KEY", "Bearer " + SECRET)
    fake.script = {"GET /models": [answer(payload={"data": []})]}
    meta._request("GET", "/models")
    assert fake.authorizations == ["Bearer " + SECRET]


# --- Overrides for other regions, gateways and slow links -------------------

def test_a_base_url_override_reaches_that_gateway(fake: type[Fake], monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = meta._BASE
    monkeypatch.setattr(meta, "_BASE", "https://api.meta.ai/v1")
    monkeypatch.setenv("VEDIT_META_BASE_URL", gateway + "/")
    fake.script = {"GET /models": [answer(payload={"data": [{"id": "muse-spark-1.3"}]})]}
    assert meta.check_access()["ok"] is True and fake.seen == [("GET", "/models")]


@pytest.mark.parametrize("bad", ["http://api.meta.ai/v1", "https://user:pw@api.meta.ai/v1", "https://api.meta.ai/v1?x=1", "ftp://api.meta.ai", "https://:99999/v1"])
def test_an_unsafe_base_url_is_refused_and_doctor_says_so(enabled: None, monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    from vedit.doctor import meta_check

    monkeypatch.setenv("VEDIT_META_BASE_URL", bad)
    status = meta.capabilities()
    assert status["available"] is False and "VEDIT_META_BASE_URL" in status["reason"]
    check = meta_check()
    assert check.ok is False and "VEDIT_META_BASE_URL" in check.detail
    monkeypatch.setattr(meta, "build_opener", lambda *_: pytest.fail("the key must not go to an unsafe address"))
    with pytest.raises(meta.MetaError, match="VEDIT_META_BASE_URL"):
        meta._request("GET", "/models")


def test_the_timeout_can_be_raised_for_long_clips_and_a_bad_value_is_explained(enabled: None, monkeypatch: pytest.MonkeyPatch) -> None:
    assert meta._timeout("/responses") == 180 and meta._timeout("/asr/transcribe") == 180 and meta._timeout("/files") == 90
    monkeypatch.setenv("VEDIT_META_TIMEOUT", "900")
    assert meta._timeout("/responses") == 900 and meta._timeout("/files") == 900
    for bad in ("2", "ten", "nan", "99999"):
        monkeypatch.setenv("VEDIT_META_TIMEOUT", bad)
        status = meta.capabilities()
        assert status["available"] is False and "VEDIT_META_TIMEOUT" in status["reason"]


# --- Uploads and cleanup ----------------------------------------------------

def test_an_upload_meta_already_removed_is_not_reported_as_left_behind(fake: type[Fake], clip: Path, tmp_path: Path) -> None:
    fake.script = story_routes(answer(payload=completed(json.dumps(STORY))))
    fake.script["DELETE /files/file-abc123"] = [answer(404, remote_error("file_not_found"))]
    work = tmp_path / "work"
    assert plan(clip, work)["warnings"] == []
    assert not (work / "meta-cleanup.json").exists() and count(("DELETE", "/files/file-abc123")) == 1


def test_an_upload_that_cannot_be_deleted_is_still_reported(fake: type[Fake], clip: Path, tmp_path: Path) -> None:
    fake.script = story_routes(answer(payload=completed(json.dumps(STORY))))
    fake.script["DELETE /files/file-abc123"] = [answer(500)]
    work = tmp_path / "work"
    assert "could not be deleted" in plan(clip, work)["warnings"][0]
    assert json.loads((work / "meta-cleanup.json").read_text())["file_id"] == "file-abc123"
    assert count(("DELETE", "/files/file-abc123")) == 3, "deleting is free, so it is retried"


def test_a_failure_inside_an_upload_is_never_swallowed_by_its_cleanup(fake: type[Fake], clip: Path, tmp_path: Path) -> None:
    fake.script = story_routes(answer(400, remote_error("invalid_value")))
    fake.script["DELETE /files/file-abc123"] = [answer(404)]
    with pytest.raises(meta.MetaError, match="refused the request as invalid"):
        plan(clip, tmp_path / "work")


# --- Where the words go ----------------------------------------------------

def test_a_meta_message_reaches_mcp_and_studio_whole() -> None:
    message = "Meta rejected the API key (HTTP 401). Copy a working key from https://dev.meta.ai/keys and paste it in Settings."
    assert plain_failure(meta.MetaError(message)) == message, "MCP's ToolError and Studio's job error show it unchanged"


def test_no_failure_path_writes_the_key_or_a_header_anywhere(fake: type[Fake], clip: Path, tmp_path: Path) -> None:
    fake.script = story_routes(answer(500, remote_error("server_error")))
    work = tmp_path / "work"
    with pytest.raises(meta.MetaError) as caught:
        plan(clip, work)
    clean_text(str(caught.value) + repr(caught.value.__dict__))
    clean_text(written(work))
    clean_text(json.dumps(meta.call_log(work)))
    assert caught.value.__context__ is None or SECRET not in repr(caught.value.__context__)


# --- What status says after a failure ----------------------------------------

def test_status_repeats_a_rejected_key_until_a_call_succeeds_or_the_key_changes(fake: type[Fake], monkeypatch: pytest.MonkeyPatch) -> None:
    assert meta.capabilities()["last_problem"] is None
    fake.script = {"GET /models": [answer(401, remote_error("invalid_api_key"))]}
    meta.check_access()
    status = meta.capabilities()
    assert status["available"] is True, "a retry after fixing the key must still be allowed"
    assert status["reason"].startswith("Meta is set up, but the last call with this key failed: Meta rejected the API key")
    assert "ending in wxyz" in status["reason"] and status["last_problem"]
    clean_text(json.dumps(status))
    monkeypatch.setenv("MODEL_API_KEY", SECRET[:-4] + "abcd")
    assert meta.capabilities()["last_problem"] is None, "a new key starts clean"
    monkeypatch.setenv("MODEL_API_KEY", SECRET)
    assert meta.capabilities()["last_problem"]
    fake.script = {"GET /models": [answer(payload={"data": [{"id": "muse-spark-1.3"}]})]}
    assert meta.check_access()["ok"] is True
    assert meta.capabilities()["last_problem"] is None and meta.capabilities()["reason"].startswith("Meta is ready")


def test_status_repeats_a_clock_or_certificate_problem(enabled: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(meta, "_BASE", "https://api.meta.ai/v1")
    monkeypatch.setattr(meta, "build_opener", lambda *_: Raising(URLError(cert_error("certificate is not yet valid"))))
    assert "date or time" in meta.check_access()["reason"]
    assert "date or time" in meta.capabilities()["reason"]


def test_a_model_refusal_does_not_mark_the_whole_key_broken(fake: type[Fake]) -> None:
    fake.script = {"POST /responses": [answer(403, remote_error("model_not_enabled"))]}
    with pytest.raises(meta.MetaError):
        meta._request("POST", "/responses", payload={"model": "muse-wonder-1.0"})
    assert meta.capabilities()["last_problem"] is None


# --- Advertised prices ------------------------------------------------------

@pytest.mark.parametrize(("mode", "per_million"), [("standard", (1.25, 4.25)), ("contributor", (0.10, 0.20))])
def test_the_advertised_prices_are_the_constants_that_are_charged(enabled: None, monkeypatch: pytest.MonkeyPatch, mode: str, per_million: tuple[float, float]) -> None:
    from vedit import director

    monkeypatch.setenv("VEDIT_META_MODE", mode)
    status = meta.capabilities()
    price_in, price_out = meta._story_prices()
    assert status["prices_as_of"] == meta._PRICES_AS_OF == "2026-11-04"
    assert status["prices"] == {"input_per_million": pytest.approx(price_in * 1e6), "output_per_million": pytest.approx(price_out * 1e6),
                                "sam_per_thousand_frames": pytest.approx(meta._FRAME_PRICE * 1000), "cover_per_image": meta._IMAGE_PRICE,
                                "transcribe_per_hour": meta._TRANSCRIBE_PRICE_PER_HOUR}
    # And the constants are the 2026-11-04 dev.meta.ai pricing page.
    assert (round(price_in * 1e6, 4), round(price_out * 1e6, 4)) == per_million
    assert meta._FRAME_PRICE * 1000 == pytest.approx(0.20) and meta._TRANSCRIBE_PRICE_PER_HOUR == 0.18
    assert meta._IMAGE_PRICE == director.IMAGE_PRICE_USD == 0.01, "one image price for the adapter and the director"
    assert (meta._WONDER_INPUT_PRICE * 1e6, meta._WONDER_OUTPUT_PRICE * 1e6) == (pytest.approx(4.0), pytest.approx(20.0))


# --- A hard cap for video calls -----------------------------------------------

# (video seconds, what /responses/input_tokens counted, what Meta billed), from live runs in docs/evidence.
MEASURED = [(15.6, 488, 4300), (15.6, 459, 4319), (5.005, 470, 1550), (4.838, 470, 1225), (4.838, 453, 1256),
            (23.633, 486, 6478), (40.021, 1078, 11696), (18.933, 2173, 6579), (17.667, 2174, 6373)]
WONDER_MEASURED = [(18.933, 1933, 5401)]


def allowance(seconds: float, *, listens: bool = False, sound: bool = True) -> int:
    rate = meta._VIDEO_TOKENS_PER_SECOND + (meta._LISTEN_TOKENS_PER_SECOND if listens and sound else 0)
    return meta._VIDEO_TOKENS_FIXED + int(-(-seconds * rate // 1))


@pytest.mark.parametrize(("seconds", "counted", "billed"), MEASURED + WONDER_MEASURED)
def test_the_video_allowance_covers_every_live_measurement(seconds: float, counted: int, billed: int) -> None:
    listens = (seconds, counted, billed) in WONDER_MEASURED
    assert counted + allowance(seconds, listens=listens) >= billed


def request_with(file_id: str, model: str = "muse-spark-1.3") -> dict[str, Any]:
    return {"model": model, "input": [{"type": "message", "role": "user", "content": [
        {"type": "input_text", "text": "x"}, {"type": "input_file", "file_id": file_id}]}]}


def test_the_count_adds_the_video_meta_leaves_out_and_more_for_a_model_that_listens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(meta._UPLOADS, "file-v1", (10.0, True))
    spark = meta._with_media_allowance(request_with("file-v1"), {"input_tokens": 500})
    assert spark == {"input_tokens": 500 + allowance(10.0), "counted_input_tokens": 500, "media_tokens_allowance": allowance(10.0)}
    wonder = meta._with_media_allowance(request_with("file-v1", "muse-wonder-1.0"), {"input_tokens": 500})
    assert wonder["input_tokens"] == 500 + allowance(10.0, listens=True) > spark["input_tokens"]
    monkeypatch.setitem(meta._UPLOADS, "file-v2", (10.0, False))
    assert meta._with_media_allowance(request_with("file-v2", "muse-wonder-1.0"), {"input_tokens": 500})["input_tokens"] == spark["input_tokens"]
    assert meta._with_media_allowance(request_with("file-unknown"), {"input_tokens": 500}) == {"input_tokens": 500}, "text only adds nothing"


def test_a_story_reservation_includes_the_uploaded_video(fake: type[Fake], clip: Path, tmp_path: Path) -> None:
    fake.script = story_routes(answer(payload=completed(json.dumps(STORY))))
    fake.script["POST /responses/input_tokens"] = [answer(payload={"input_tokens": 488})]
    work = tmp_path / "work"
    plan(clip, work)
    tokens = 488 + allowance(2.0)
    price_in, price_out = meta._story_prices()
    [call] = ledger(work)
    assert call["reserved_usd"] == pytest.approx(tokens * price_in + meta._OUTPUT_TOKENS * price_out)
    start = json.loads((work / "meta-calls.jsonl").read_text().splitlines()[0])
    assert start["size"]["input_tokens"] == tokens
    assert not meta._UPLOADS, "an upload's length is forgotten once it is deleted"


def test_an_overrun_is_warned_counted_and_shrinks_what_is_left(fake: type[Fake], clip: Path, tmp_path: Path) -> None:
    overrun = dict(completed(json.dumps(STORY)), usage={"input_tokens": 120_000, "output_tokens": 300})
    fake.script = story_routes(answer(payload=overrun))
    work = tmp_path / "work"
    result = plan(clip, work, budget=0.2)
    price_in, price_out = meta._story_prices()
    billed = 120_000 * price_in + 300 * price_out
    [call] = ledger(work)
    reserved = call["reserved_usd"]
    assert billed > reserved and call["usage"]["cost_usd"] == pytest.approx(billed)
    assert call["usage"]["overran_usd"] == pytest.approx(billed - reserved, abs=1e-8)
    assert any("more than the" in warning and "later calls have that much less room" in warning for warning in result["warnings"])
    [row] = meta.call_log(work, budget_usd=0.2)["calls"]
    assert row["cost_kind"] == "actual" and row["cost_usd"] == pytest.approx(billed)
    # Two reservations would fit in $0.20; the true cost plus the next reservation does not, so the cap holds.
    assert 2 * reserved < 0.2 < billed + reserved
    with pytest.raises(meta.MetaError, match=rf"\${billed:.4f} of the \$0.2000 campaign budget is already reserved"):
        meta.plan_story(clip, goal="Another cut.", duration=2.0, target_duration=2.0, work_dir=work, budget_usd=0.2)


def test_an_overrun_on_a_found_moment_search_reaches_its_warnings(fake: type[Fake], clip: Path, tmp_path: Path) -> None:
    text = json.dumps({"moments": [{"start": 0.0, "end": 1.0, "reason": "The bowl is on screen."}], "subject": "bowl"})
    fake.script = story_routes(answer(payload=dict(completed(text), usage={"input_tokens": 90_000, "output_tokens": 100})))
    result = meta.find_moments(clip, query="the bowl", duration=2.0, work_dir=tmp_path / "work", budget_usd=0.5)
    assert result["moments"] and any("more than the" in warning for warning in result["warnings"])


def test_a_sam_call_billed_for_more_frames_than_reserved_says_so(fake: type[Fake], clip: Path, tmp_path: Path) -> None:
    done = sse("response.completed", response={"status": "completed", "usage": {"video_frames_processed": 5000}})
    fake.script = {"POST /files": [answer(payload={"id": "file-abc123"})], "POST /responses": [("stream", [done], True)],
                   "DELETE /files/file-abc123": [answer(payload={"deleted": True})]}
    masks = meta.subject_masks(clip, subject="the bowl", duration=2.0, work_dir=tmp_path / "work", budget_usd=1.0)
    assert masks["usage"]["overran_usd"] > 0 and any("more than the" in warning for warning in masks["warnings"])
