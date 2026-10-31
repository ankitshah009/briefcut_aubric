"""Campaign HTTP contract and browser boundaries, with real job storage."""

from __future__ import annotations

import http.client
import importlib.util
import io
import json
import threading
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from vedit.studio.server import StudioHandler, _LoopbackServer


def request(
    port: int,
    method: str,
    path: str,
    payload: dict | list | bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    outgoing = dict(headers or {})
    if isinstance(payload, (dict, list)):
        body = json.dumps(payload).encode()
        outgoing.setdefault("Content-Type", "application/json")
    else:
        body = payload
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(method, path, body=body, headers=outgoing)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.delenv("VEDIT_META_ENABLED", raising=False)
    httpd = _LoopbackServer(("127.0.0.1", 0), StudioHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(httpd.server_address[1])
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)
        from vedit.campaign import get_manager

        manager = get_manager()
        if hasattr(manager, "close"):
            manager.close()


def test_capabilities_and_empty_history(server: int) -> None:
    status, headers, raw = request(server, "GET", "/api/capabilities")
    assert status == 200
    assert headers["Cache-Control"] == "no-store"
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert "local" in payload and "meta" in payload
    assert not payload["meta"].get("configured")
    status, _, raw = request(server, "GET", "/api/campaigns")
    assert status == 200
    assert json.loads(raw)["jobs"] == []


@pytest.mark.parametrize(
    "headers",
    [
        {"Host": "evil.example"},
        {"Host": "127.0.0.1:1"},
        {"Host": "127.0.0.1@evil.example"},
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
    ],
)
def test_campaign_rejects_cross_origin_access(server: int, headers: dict[str, str]) -> None:
    status, _, raw = request(server, "POST", "/api/campaigns", {}, headers)
    assert status == 403
    assert json.loads(raw)["ok"] is False
    status, _, raw = request(server, "GET", "/api/campaigns")
    assert status == 200
    assert json.loads(raw)["jobs"] == []


@pytest.mark.parametrize("payload", [b"{", b'\xff', b"null", b"[]"])
def test_campaign_requires_json_object(server: int, payload: bytes) -> None:
    status, _, raw = request(server, "POST", "/api/campaigns", payload, {"Content-Type": "application/json"})
    assert status == 400
    assert json.loads(raw)["ok"] is False


def test_campaign_rejects_form_and_unknown_resources(server: int) -> None:
    status, _, _ = request(server, "POST", "/api/campaigns", b"goal=hello", {"Content-Type": "text/plain"})
    assert status == 415
    status, _, _ = request(server, "GET", "/api/campaigns/missing-job")
    assert status == 404
    status, _, _ = request(server, "GET", "/api/campaigns/../../README.md")
    assert status == 404
    status, _, _ = request(server, "GET", "/api/campaigns/missing-job/artifact?name=../../README.md")
    assert status == 400


def test_campaign_rejects_local_files_outside_uploads(server: int, tmp_path: Path) -> None:
    private_file = tmp_path / "private.mp4"
    private_file.write_bytes(b"private")
    status, _, raw = request(
        server, "POST", "/api/campaigns", {"paths": [str(private_file)], "goal": "Make a short"}
    )
    assert status == 400
    assert json.loads(raw)["ok"] is False


def make_clip(path: Path) -> Path:
    helpers = Path(__file__).with_name("test_e2e.py")
    spec = importlib.util.spec_from_file_location("campaign_http_media_helpers", helpers)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not module._have_ffmpeg():
        pytest.skip("ffmpeg required")
    return module._make_clip(path, seconds=1.0, color="navy")


def test_uploaded_clip_plans_and_streams_after_reload(server: int, tmp_path: Path) -> None:
    source = make_clip(tmp_path / "source.mp4")
    data = source.read_bytes()
    status, _, raw = request(
        server,
        "POST",
        "/api/upload?name=source.mp4",
        data,
        {"Content-Type": "application/octet-stream"},
    )
    assert status == 201
    paths = json.loads(raw)["paths"]
    payload = {
        "paths": paths,
        "goal": "Make a short product video",
        "provider": "local",
        "target_duration": 15,
        "aspect": "9:16",
        "captions": False,
    }
    origin = {"Origin": f"http://127.0.0.1:{server}", "Sec-Fetch-Site": "same-origin"}
    status, _, raw = request(server, "POST", "/api/campaigns", payload, origin)
    assert status == 202, raw
    job = json.loads(raw)["job"]
    job_id = job["id"]
    deadline = time.monotonic() + 20
    while job["status"] in {"queued", "analyzing"} and time.monotonic() < deadline:
        time.sleep(0.05)
        status, _, raw = request(server, "GET", "/api/campaigns/" + job_id)
        assert status == 200
        job = json.loads(raw)["job"]
    assert job["status"] == "planned", job
    assert job["plan"]["segments"]
    status, _, raw = request(server, "GET", "/api/campaigns")
    assert status == 200
    assert any(item["id"] == job_id for item in json.loads(raw)["jobs"])

    url = f"/api/campaigns/{job_id}/artifact?name=source"
    status, headers, raw = request(server, "GET", url, headers={"Range": "bytes=0-127"})
    assert status == 206, raw
    assert headers["Content-Range"] == f"bytes 0-127/{len(data)}"
    assert raw == data[:128]
    assert headers["X-Content-Type-Options"] == "nosniff"
    status, headers, raw = request(server, "HEAD", url)
    assert status == 200
    assert int(headers["Content-Length"]) == len(data)
    assert not raw
    status, _, raw = request(server, "GET", url, headers={"Range": "bytes=999999999-"})
    assert status == 416 and raw == b""

    status, _, raw = request(server, "POST", "/api/campaigns", payload, origin)
    assert status == 202
    assert json.loads(raw)["job"]["id"] == job_id

    status, _, raw = request(server, "POST", f"/api/campaigns/{job_id}/render", {"plan": job["plan"]}, origin)
    assert status == 202, raw
    rendered = json.loads(raw)["job"]
    deadline = time.monotonic() + 60
    while rendered["status"] in {"queued", "rendering"} and time.monotonic() < deadline:
        time.sleep(0.1)
        status, _, raw = request(server, "GET", "/api/campaigns/" + job_id)
        assert status == 200
        rendered = json.loads(raw)["job"]
    assert rendered["status"] == "complete", rendered
    proof = rendered["result"]["proof"]
    assert proof["has_video"] and proof["has_audio"]
    assert (proof["width"], proof["height"]) == (1080, 1920)
    assert proof["color_space"] == "bt709"
    assert proof["color_range"] == "tv"
    assert 0.8 < proof["duration"] < 1.3
    assert "&v=1" in rendered["result"]["video_url"]
    status, _, raw = request(server, "GET", rendered["result"]["video_url"], headers={"Range": "bytes=0-127"})
    assert status == 206 and len(raw) == 128
    status, _, _ = request(server, "GET", rendered["result"]["video_url"] + "&v=2")
    assert status == 400
    status, _, _ = request(server, "GET", f"/api/campaigns/{job_id}/artifact?name=video&v=999")
    assert status == 404
    status, _, _ = request(server, "GET", f"/api/campaigns/{job_id}/artifact?name=video&v=0")
    assert status == 400
    status, headers, raw = request(server, "GET", rendered["result"]["report_url"])
    assert status == 200
    assert headers["Content-Disposition"].startswith("attachment;")
    assert json.loads(raw)["proof"] == proof
    status, _, raw = request(server, "GET", rendered["result"]["bundle_url"])
    assert status == 200
    with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
        assert {"video.mp4", "cover.jpg", "post.txt", "brief.yaml", "report.json"} <= set(bundle.namelist())
        assert json.loads(bundle.read("report.json"))["proof"] == proof
        assert len(bundle.read("video.mp4")) > 1000


def test_render_effect_override_validates_before_mutation_and_distinguishes_omission(server: int, tmp_path: Path) -> None:
    clip = make_clip(tmp_path / "effect-source.mp4")
    status, _, raw = request(server, "POST", "/api/upload?name=effect.mp4", clip.read_bytes(), {"Content-Type": "application/octet-stream"})
    assert status == 201
    recipe = {"preset": "push", "intensity": 0, "start": 0, "duration": 0.5}
    status, _, raw = request(server, "POST", "/api/campaigns", {"paths": json.loads(raw)["paths"], "goal": "Present the product", "captions": False, "effect": recipe})
    assert status == 202
    job = json.loads(raw)["job"]
    ident = job["id"]

    def poll_until_settled(timeout: float = 30) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, _, data = request(server, "GET", f"/api/campaigns/{ident}")
            assert status == 200
            current = json.loads(data)["job"]
            if current["status"] not in {"queued", "analyzing", "rendering"}:
                return current
            time.sleep(0.05)
        pytest.fail("Campaign did not finish within the integration deadline")

    planned = poll_until_settled()
    assert planned["status"] == "planned"
    status, _, raw = request(server, "POST", f"/api/campaigns/{ident}/render", {"effect": {**recipe, "duration": 30}})
    assert status == 400
    unchanged = poll_until_settled()
    assert unchanged["revision"] == 0
    assert unchanged["request"]["effect"] == recipe
    assert unchanged["plan"] == planned["plan"]
    status, _, raw = request(server, "POST", f"/api/campaigns/{ident}/render", {})
    assert status == 202
    assert json.loads(raw)["job"]["request"]["effect"] == recipe
    complete = poll_until_settled()
    assert complete["status"] == "complete", complete.get("error")
    assert complete["result"]["effect"] == recipe
    status, _, raw = request(server, "POST", f"/api/campaigns/{ident}/render", {"effect": None})
    assert status == 202
    removed = json.loads(raw)["job"]
    assert removed["request"]["effect"] is None
    assert removed["plan"]["effect"] is None
    assert removed["result"]["stale"] is True
    assert removed["result"]["effect"] == recipe
    # The accepted version remains downloadable while this new attempt runs.
    status, _, raw = request(server, "GET", removed["result"]["report_url"])
    assert status == 200
    assert json.loads(raw)["effect"] == recipe
    request(server, "POST", f"/api/campaigns/{ident}/cancel", {})



@pytest.mark.parametrize(
    "method, path",
    [("POST", "/api/run"), ("POST", "/api/plan"), ("POST", "/api/upload?name=a.mp4"), ("GET", "/api/session?root=x"), ("GET", "/api/media?root=x")],
)
def test_legacy_routes_reject_cross_origin_access(server: int, method: str, path: str) -> None:
    """Every /api route applies the Host/Origin check, not only campaigns."""
    body = {} if method == "POST" else None
    status, _, raw = request(server, method, path, body, {"Origin": "https://evil.example"})
    assert status == 403
    assert json.loads(raw)["ok"] is False


def test_run_requires_json_content_type(server: int) -> None:
    """A CORS-simple text/plain body must never start a render."""
    status, _, raw = request(server, "POST", "/api/run", b'{"goal":"keep","paths":[]}', {"Content-Type": "text/plain"})
    assert status == 415
    assert json.loads(raw)["ok"] is False


def test_render_tracking_override_must_be_boolean(server: int, tmp_path: Path) -> None:
    clip = make_clip(tmp_path / "tracking-source.mp4")
    status, _, raw = request(server, "POST", "/api/upload?name=tracking.mp4", clip.read_bytes(), {"Content-Type": "application/octet-stream"})
    assert status == 201
    status, _, raw = request(server, "POST", "/api/campaigns", {"paths": json.loads(raw)["paths"], "goal": "Present the product", "captions": False})
    assert status == 202
    ident = json.loads(raw)["job"]["id"]
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        status, _, data = request(server, "GET", f"/api/campaigns/{ident}")
        current = json.loads(data)["job"]
        if current["status"] not in {"queued", "analyzing", "rendering"}:
            break
        time.sleep(0.05)
    assert current["status"] == "planned"
    assert current.get("recovery") is None
    status, _, raw = request(server, "POST", f"/api/campaigns/{ident}/render", {"tracking": "off"})
    assert status == 400
    assert "tracking must be true or false" in json.loads(raw)["error"]
    status, _, raw = request(server, "GET", f"/api/campaigns/{ident}")
    assert json.loads(raw)["job"]["revision"] == 0


def _settle(server: int, ident: str, timeout: float = 90) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, _, data = request(server, "GET", f"/api/campaigns/{ident}")
        assert status == 200
        current = json.loads(data)["job"]
        if current["status"] not in {"queued", "analyzing", "rendering"}:
            return current
        time.sleep(0.05)
    pytest.fail("Campaign did not finish within the integration deadline")


def _verified_campaign(server: int, tmp_path: Path, *, seconds: float, target_duration: float, plan_segments: list[dict]) -> dict:
    helpers = Path(__file__).with_name("test_e2e.py")
    spec = importlib.util.spec_from_file_location("campaign_http_revise_helpers", helpers)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not module._have_ffmpeg():
        pytest.skip("ffmpeg required")
    clip = module._make_clip(tmp_path / "revise-source.mp4", seconds=seconds, color="olive")
    status, _, raw = request(server, "POST", "/api/upload?name=revise.mp4", clip.read_bytes(), {"Content-Type": "application/octet-stream"})
    assert status == 201
    status, _, raw = request(server, "POST", "/api/campaigns", {"paths": json.loads(raw)["paths"], "goal": "Show the product", "captions": False, "target_duration": target_duration})
    assert status == 202, raw
    ident = json.loads(raw)["job"]["id"]
    planned = _settle(server, ident)
    assert planned["status"] == "planned", planned.get("error")
    plan = dict(planned["plan"], segments=plan_segments)
    status, _, raw = request(server, "POST", f"/api/campaigns/{ident}/render", {"plan": plan})
    assert status == 202, raw
    complete = _settle(server, ident)
    assert complete["status"] == "complete", complete.get("error")
    return complete


def test_revise_reports_checks_and_versions_and_refuses_when_nothing_applies(server: int, tmp_path: Path) -> None:
    # A 1 s moment against a 15 s target fails the duration check, but there is
    # no footage to add: the lint has nothing deterministic to apply.
    complete = _verified_campaign(server, tmp_path, seconds=1.0, target_duration=15, plan_segments=[{"start": 0.0, "end": 1.0, "label": "All", "reason": "Everything."}])
    ident = complete["id"]
    result = complete["result"]
    names = [check["name"] for check in result["checks"]]
    assert names[:4] == ["auto: cuts land on sentence boundaries", "auto: duration matches the target", "auto: moments inside the source and in order", "auto: captions cover the speech"]
    assert result["total"] == len(result["checks"]) and result["passed"] == sum(1 for check in result["checks"] if check["passed"])
    assert result["checks"][1]["passed"] is False and result["patches"] == []
    assert len(complete["versions"]) == 1
    version = complete["versions"][0]
    assert version["revision"] == 1 and version["status"] == "verified" and version["source"] == "approved"
    assert version["proof"] == result["proof"] and version["checks"] == result["checks"] and version["applied_patches"] == []
    assert version["video_url"].endswith("name=video&v=1") and version["report_url"].endswith("name=report&v=1")
    assert version["timings"]["render_seconds"] > 0
    status, _, raw = request(server, "GET", version["report_url"])
    assert status == 200 and json.loads(raw)["checks"] == result["checks"]
    for mode, message in (("auto", "No automatic fixes to apply."), ("director", "No director's fixes to apply.")):
        status, _, raw = request(server, "POST", f"/api/campaigns/{ident}/revise", {"mode": mode})
        assert status == 409, raw
        assert json.loads(raw)["error"] == message
    status, _, raw = request(server, "POST", f"/api/campaigns/{ident}/revise", {"mode": "note", "note": "Tighten the opener."})
    assert status == 409
    assert json.loads(raw)["error"] == "Director's notes need Meta mode."
    status, _, raw = request(server, "POST", f"/api/campaigns/{ident}/revise", {"mode": "sideways"})
    assert status == 400
    status, _, raw = request(server, "GET", f"/api/campaigns/{ident}/revise")
    assert status == 405
    assert json.loads(request(server, "GET", f"/api/campaigns/{ident}")[2])["job"]["revision"] == 1


def test_revise_auto_applies_lint_patches_as_a_new_verified_version(server: int, tmp_path: Path) -> None:
    # A 3 s moment against a 0.5 s target: the lint proposes trimming the end.
    complete = _verified_campaign(server, tmp_path, seconds=3.0, target_duration=0.5, plan_segments=[{"start": 0.0, "end": 3.0, "label": "All", "reason": "Everything."}])
    ident = complete["id"]
    patches = complete["result"]["patches"]
    assert patches == [{"op": "trim_end", "segment": 1, "seconds": 0.5, "reason": patches[0]["reason"], "source": "auto"}]
    status, _, raw = request(server, "POST", f"/api/campaigns/{ident}/revise", {"mode": "auto"})
    assert status == 202, raw
    queued = json.loads(raw)["job"]
    assert queued["revision"] == 2 and queued["status"] == "queued"
    assert queued["result"]["stale"] is True and queued["result"]["revision"] == 1
    assert queued["revision_source"] == "auto"
    assert queued["applied_patches"][0]["before"] == {"start": 0.0, "end": 3.0} and queued["applied_patches"][0]["after"] == {"start": 0.0, "end": 0.5}
    assert queued["plan"]["segments"][0]["end"] == 0.5 and queued["plan"]["duration"] == 0.5
    assert "end moved to 0.50 s" in queued["plan"]["segments"][0]["reason"]
    revised = _settle(server, ident)
    assert revised["status"] == "complete", revised.get("error")
    assert revised["result"]["revision"] == 2 and abs(revised["result"]["proof"]["duration"] - 0.5) < 0.2
    assert revised["result"]["checks"][1]["passed"] is True and revised["result"]["patches"] == []
    assert [version["revision"] for version in revised["versions"]] == [1, 2]
    first, second = revised["versions"]
    assert first["status"] == "verified" and first["video_url"].endswith("&v=1")
    assert second["source"] == "auto" and second["applied_patches"][0]["op"] == "trim_end" and second["video_url"].endswith("&v=2")
    assert abs(second["proof"]["duration"] - 0.5) < 0.2 and abs(first["proof"]["duration"] - 3.0) < 0.2
    # Both verified files stay downloadable.
    for version in (first, second):
        status, _, raw = request(server, "GET", version["video_url"], headers={"Range": "bytes=0-127"})
        assert status == 206 and len(raw) == 128
    status, _, raw = request(server, "POST", f"/api/campaigns/{ident}/revise", {"mode": "auto"})
    assert status == 409
