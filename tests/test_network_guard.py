"""The test suite's network guard: Meta and the internet are unreachable, this machine is not."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError
from urllib.request import Request

import pytest

from conftest import NO_NETWORK, loopback_url
from vedit import meta


@pytest.mark.parametrize(
    "url",
    [
        "https://api.meta.ai/v1/responses",
        "https://graph.facebook.com/",
        "http://10.0.0.1/",
        "http://127.0.0.2/",
        "http://localhost.example.com/",
        "http://user@127.0.0.1/",
        "ftp://127.0.0.1/",
        "http://[::2]/",
        "not a url",
    ],
)
def test_anything_but_loopback_is_refused_offline(url: str) -> None:
    assert not loopback_url(url)
    with pytest.raises(URLError, match="tests make no network calls"):
        meta.build_opener().open(Request(url) if "://" in url else url, timeout=1)


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:8080/v1", "http://localhost/v1", "http://[::1]:9/v1", "https://127.0.0.1/"]
)
def test_loopback_names_are_allowed(url: str) -> None:
    assert loopback_url(url)


def test_a_fake_server_on_this_machine_is_reached_through_metas_opener() -> None:
    class Hello(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server's name
            self.send_response(200)
            self.send_header("Content-Length", "5")
            self.end_headers()
            self.wfile.write(b"hello")

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Hello)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/ping"
        with meta.build_opener(meta._NoRedirect()).open(Request(url), timeout=5) as response:
            assert response.read() == b"hello"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_meta_requests_fail_fast_without_a_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_API_KEY", "test-secret-never-write-to-disk")
    monkeypatch.setenv("VEDIT_META_ENABLED", "1")
    assert meta._BASE.startswith("https://") and not loopback_url(meta._BASE)
    with pytest.raises(meta.MetaError, match="network timeout"):
        meta._request("GET", "/models")
    assert NO_NETWORK.startswith("tests make no network calls")
