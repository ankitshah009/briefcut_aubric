"""The MCP server drives the campaign manager end to end over an in-memory client."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp")

from mcp.client.client import Client  # noqa: E402

from vedit import mcp_server  # noqa: E402
from vedit.campaign import CampaignError, validate_plan  # noqa: E402
from vedit.media import resolve_ffmpeg  # noqa: E402

FIXTURE = Path(__file__).resolve().parents[1] / "examples" / "demo" / "talk.mp4"
JOB = "a" * 32
NEW_TOOLS = {"briefcut_status", "briefcut_timeline", "briefcut_edit", "briefcut_add_asset", "briefcut_generate_background"}


def _have_ffmpeg() -> bool:
    try:
        resolve_ffmpeg()
        return True
    except Exception:
        return False


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("VEDIT_WORKSPACE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.delenv("VEDIT_META_ENABLED", raising=False)
    yield tmp_path
    mcp_server.shutdown()


def _payload(result: Any) -> Any:
    text = "".join(item.text for item in result.content if getattr(item, "type", "") == "text")
    is_error = getattr(result, "is_error", None)
    if is_error is None:
        is_error = getattr(result, "isError", False)
    if is_error:
        return {"tool_error": text}
    return json.loads(text)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def offline_create(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Let an end-to-end test plan without Meta.

    The MCP tools refuse local mode. ``product_request`` is the one seam between
    a tool and the manager, so swapping it lets the real manager plan with its
    model-free path, and records what the tool would have sent.
    """
    real = mcp_server.product_request
    sent: list[dict[str, Any]] = []

    def offline(value: Any, **kwargs: Any) -> Any:
        checked = real(value, **kwargs)
        sent.append(checked)
        return {**checked, "provider": "local", "caption_provider": "local"}

    monkeypatch.setattr(mcp_server, "product_request", offline)
    return sent


@pytest.mark.render
@pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg required")
def test_import_plan_render_receipts_and_artifact_over_mcp(workspace: Path, offline_create: list[dict[str, Any]]) -> None:
    clip = workspace / "upload" / "talk.mp4"
    clip.parent.mkdir()
    shutil.copyfile(FIXTURE, clip)
    server = mcp_server.build_server()

    async def flow() -> None:
        async with Client(server) as client:
            listed = await client.list_tools()
            names = {tool.name for tool in listed.tools}
            assert {"briefcut_import_clip", "briefcut_create_story", "briefcut_wait", "briefcut_render", "briefcut_revise", "briefcut_receipts", "briefcut_artifact_path", "briefcut_cancel", "briefcut_find_moments", "briefcut_make_shorts", "briefcut_script_to_video"} | NEW_TOOLS <= names

            imported = _payload(await client.call_tool("briefcut_import_clip", {"path": str(clip)}))
            assert "tool_error" not in imported, imported
            assert imported["bytes"] == FIXTURE.stat().st_size
            inbox = Path(imported["paths"][0])
            assert inbox.is_file()
            assert inbox.relative_to(workspace / "vedit-sessions").parts[1] == "inbox"

            job = _payload(await client.call_tool("briefcut_create_story", {
                "paths": imported["paths"], "goal": "Show the talk clearly",
                "target_duration": 3, "aspect": "9:16", "captions": False,
            }))
            assert "tool_error" not in job, job
            assert offline_create[-1]["provider"] == "meta" and offline_create[-1]["caption_provider"] == "meta"
            job_id = job["id"]

            planned = _payload(await client.call_tool("briefcut_wait", {"job_id": job_id, "timeout_seconds": 60}))
            assert planned.get("status") == "planned", planned
            assert planned["plan"]["segments"]

            rendering = _payload(await client.call_tool("briefcut_render", {"job_id": job_id, "plan": planned["plan"]}))
            assert "tool_error" not in rendering, rendering
            complete = _payload(await client.call_tool("briefcut_wait", {"job_id": job_id, "timeout_seconds": 120}))
            assert complete.get("status") == "complete", complete

            receipts = _payload(await client.call_tool("briefcut_receipts", {"job_id": job_id}))
            assert "tool_error" not in receipts, receipts
            assert receipts["checks"] and receipts["passed"] == receipts["total"]
            assert receipts["revision"] == complete["verified_revision"]
            proof = receipts["proof"]
            assert proof["has_video"] and proof["has_audio"] and proof["duration"] > 0
            assert receipts["versions"] and receipts["versions"][-1]["status"] == "verified"

            for name in ("video", "report", "source"):
                artifact = _payload(await client.call_tool("briefcut_artifact_path", {"job_id": job_id, "name": name}))
                assert "tool_error" not in artifact, artifact
                path = Path(artifact["path"])
                assert path.is_absolute() and path.is_file() and artifact["bytes"] > 0
            report = json.loads(Path(_payload(await client.call_tool("briefcut_artifact_path", {"job_id": job_id, "name": "report"}))["path"]).read_text())
            assert report

            missing = _payload(await client.call_tool("briefcut_artifact_path", {"job_id": job_id, "name": "cover", "revision": 99}))
            assert "tool_error" in missing and "revision" in missing["tool_error"]
            listed_jobs = _payload(await client.call_tool("briefcut_list", {}))
            assert any(item["id"] == job_id for item in listed_jobs["jobs"])

    _run(flow())


def test_import_rejects_non_video_and_relative_paths(workspace: Path) -> None:
    server = mcp_server.build_server()
    text = workspace / "notes.txt"
    text.write_text("not a video")

    async def flow() -> None:
        async with Client(server) as client:
            relative = _payload(await client.call_tool("briefcut_import_clip", {"path": "talk.mp4"}))
            assert "tool_error" in relative and "absolute" in relative["tool_error"]
            missing = _payload(await client.call_tool("briefcut_import_clip", {"path": str(workspace / "nope.mp4")}))
            assert "tool_error" in missing and "No file" in missing["tool_error"]
            if _have_ffmpeg():
                bad = _payload(await client.call_tool("briefcut_import_clip", {"path": str(text)}))
                assert "tool_error" in bad and "Traceback" not in bad["tool_error"]

    _run(flow())


def test_campaign_error_becomes_tool_error_without_traceback(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Stub:
        _closed = False

        def get(self, ident: str) -> dict[str, Any]:
            raise CampaignError("Campaign not found.", 404)

        def create(self, value: Any) -> dict[str, Any]:
            raise CampaignError("Choose one uploaded video for this campaign.")

    monkeypatch.setattr(mcp_server, "_manager", lambda: Stub())
    server = mcp_server.build_server()

    async def flow() -> None:
        async with Client(server) as client:
            result = await client.call_tool("briefcut_get", {"job_id": "0" * 32})
            payload = _payload(result)
            assert "tool_error" in payload
            assert "Campaign not found." in payload["tool_error"]
            assert "Traceback" not in payload["tool_error"] and "CampaignError" not in payload["tool_error"]
            created = _payload(await client.call_tool("briefcut_create_story", {"paths": [], "goal": "x"}))
            assert set(created) == {"tool_error"} and "Choose one uploaded video" in created["tool_error"]
            receipts = _payload(await client.call_tool("briefcut_receipts", {"job_id": "0" * 32}))
            assert "Campaign not found." in receipts["tool_error"]

    _run(flow())


def test_wait_times_out_with_a_tool_error(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Stub:
        _closed = False

        def get(self, ident: str) -> dict[str, Any]:
            return {"id": ident, "status": "analyzing", "stage": "analyzing", "message": "Reading your clip."}

    monkeypatch.setattr(mcp_server, "_manager", lambda: Stub())
    monkeypatch.setattr(mcp_server, "POLL_SECONDS", 0.01)
    server = mcp_server.build_server()

    async def flow() -> None:
        async with Client(server) as client:
            result = _payload(await client.call_tool("briefcut_wait", {"job_id": "a" * 32, "timeout_seconds": 0.05}))
            assert "tool_error" in result and "Timed out" in result["tool_error"] and "analyzing" in result["tool_error"]

    _run(flow())


def test_find_shorts_and_script_tools_forward_campaign_requests(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, tuple[Any, ...]]] = []

    class Stub:
        _closed = False

        def find(self, job_id: str, value: dict[str, Any]) -> dict[str, Any]:
            calls.append(("find", (job_id, value)))
            return {"moments": [], "searched": ["transcript"], "warnings": [], "budget": {}}

        def shorts(self, job_id: str, value: dict[str, Any]) -> list[dict[str, Any]]:
            calls.append(("shorts", (job_id, value)))
            return [{"id": "short-1", "status": "queued"}, {"id": "short-2", "status": "queued"}]

        def create_script(self, value: dict[str, Any]) -> dict[str, Any]:
            calls.append(("script", (value,)))
            return {"id": "script-1", "status": "queued"}

    monkeypatch.setattr(mcp_server, "_manager", lambda: Stub())
    server = mcp_server.build_server()

    async def flow() -> None:
        async with Client(server) as client:
            found = _payload(await client.call_tool("briefcut_find_moments", {"job_id": "a" * 32, "query": "the bowl"}))
            assert found["searched"] == ["transcript"]
            shorts = _payload(await client.call_tool("briefcut_make_shorts", {
                "job_id": "a" * 32, "count": 2, "target_duration": 12, "aspect": "9:16",
                "budget_usd": 0.5, "auto_render": False,
            }))
            assert len(shorts["jobs"]) == 2
            script = _payload(await client.call_tool("briefcut_script_to_video", {
                "script": "A bowl begins as a block. The grain guides every cut.", "aspect": "16:9",
                "provider": "meta", "budget_usd": 0.5, "language": "en", "voice": "en_US-lessac-medium",
                "images": ["/tmp/scene.png"], "review": True, "auto_refine": 1,
                "template": "tutorial", "edit": {"captions": {"style": "bold"}},
            }))
            assert script["job"]["id"] == "script-1"

    _run(flow())
    assert calls[0] == ("find", ("a" * 32, {"query": "the bowl"}))
    assert calls[1][0] == "shorts" and calls[1][1][1] == {
        "count": 2, "target_duration": 12.0, "aspect": "9:16", "auto_render": False, "budget_usd": 0.5,
    }
    assert calls[2] == ("script", ({
        "script": "A bowl begins as a block. The grain guides every cut.", "aspect": "16:9",
        "provider": "meta", "budget_usd": 0.5, "language": "en", "images": ["/tmp/scene.png"],
        "review": True, "auto_refine": 1, "template": "tutorial", "voice": "en_US-lessac-medium",
        "edit": {"captions": {"style": "bold"}}, "caption_provider": "meta",
    },))


# ---------------------------------------------------------------------------
# Timeline, edit, assets, and status. The stub manager below answers
# ``timeline`` and ``render`` with the real ``validate_plan``, exactly as
# CampaignManager does, so every edit here meets the real edit_spec checks.
# ---------------------------------------------------------------------------

_WORDS = [
    {"start": 0.5, "end": 0.8, "text": "This"}, {"start": 0.8, "end": 1.0, "text": "is"}, {"start": 1.0, "end": 1.3, "text": "the"},
    {"start": 1.3, "end": 2.0, "text": "bowl."}, {"start": 6.5, "end": 6.8, "text": "And"}, {"start": 6.8, "end": 7.1, "text": "now"},
    {"start": 7.1, "end": 7.3, "text": "it"}, {"start": 7.3, "end": 7.6, "text": "is"}, {"start": 7.6, "end": 8.0, "text": "done."},
]
_LINES = [{"start": 0.5, "end": 2.0, "text": "This is the bowl."}, {"start": 6.5, "end": 8.0, "text": "And now it is done."}]


def _planned_job(**request: Any) -> dict[str, Any]:
    analysis = {"method": "local-analysis", "transcript": _LINES, "sentences": _LINES, "words": _WORDS}
    plan = validate_plan({"title": "Bowl", "segments": [{"start": 0.0, "end": 4.0, "label": "Open"}, {"start": 6.0, "end": 9.0, "label": "Close"}]},
                         12.0, original={"analysis": analysis})
    return {
        "id": JOB, "status": "planned", "revision": 0, "verified_revision": None, "plan": plan, "source": {"duration": 12.0},
        "request": {"provider": "local", "aspect": "9:16", "captions": True, "language": "en", **request}, "assets": {},
    }


def _refused(payload: Any, words: str) -> bool:
    """A tool error whose message carries the manager's own words, and no traceback."""
    message = payload.get("tool_error", "") if isinstance(payload, dict) else ""
    return words in message and "Traceback" not in message


class _EditStub:
    _closed = False

    def __init__(self, job: dict[str, Any], sessions: Path | None = None) -> None:
        self.job = job
        self.rendered: list[dict[str, Any]] = []
        self.sessions = sessions

    def get(self, ident: str) -> dict[str, Any]:
        if ident != self.job["id"]:
            raise CampaignError("Campaign not found.", 404)
        return json.loads(json.dumps(self.job))

    def timeline(self, ident: str, value: dict[str, Any]) -> dict[str, Any]:
        job = self.get(ident)
        if not job.get("plan"):
            raise CampaignError("Create a story plan before editing its timeline.", 409)
        return validate_plan(value.get("plan", job["plan"]), job["source"]["duration"], original=job["plan"], assets=job.get("assets"))["timeline"]

    def render(self, ident: str, value: dict[str, Any]) -> dict[str, Any]:
        job = self.get(ident)
        plan = validate_plan(value.get("plan", job["plan"]), job["source"]["duration"], original=job["plan"])
        self.rendered.append(value)
        self.job.update(plan=plan, status="queued", revision=job["revision"] + 1)
        return self.get(ident)


def _serve(monkeypatch: pytest.MonkeyPatch, stub: Any):
    monkeypatch.setattr(mcp_server, "_manager", lambda: stub)
    return mcp_server.build_server()


def test_timeline_is_summary_first_and_pages_its_lists(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _EditStub(_planned_job())
    stub.job["plan"]["edit"]["look"]["preset"] = "warm"
    stub.job["plan"]["edit"]["removed"] = [[10.0, 10.5], [11.0, 11.5]]
    server = _serve(monkeypatch, stub)

    async def flow() -> None:
        async with Client(server) as client:
            summary = _payload(await client.call_tool("briefcut_timeline", {"job_id": JOB}))
            assert "tool_error" not in summary, summary
            assert summary["duration"] == pytest.approx(7.0)
            assert summary["span_count"] == 2 and summary["caption_count"] == 2
            assert [row["segment"] for row in summary["segments"]] == [1, 2]
            assert summary["segments"][0] == {"segment": 1, "start": 0.0, "end": 4.0, "label": "Open"}
            # Only what differs from a blank edit, with long lists counted instead of listed.
            assert summary["edit"] == {"look": {"preset": "warm"}}
            assert summary["removed_ranges"] == 2 and summary["caption_edits"] == 0
            assert summary["captions_on"] is True and summary["assets"] == []

            first = _payload(await client.call_tool("briefcut_timeline", {"job_id": JOB, "detail": "captions", "limit": 1}))
            assert first["total"] == 2 and first["next_offset"] == 1 and len(first["items"]) == 1
            cue = first["items"][0]
            assert cue["key"] == "500" and cue["text"] == "This is the bowl." and "words" not in cue
            last = _payload(await client.call_tool("briefcut_timeline", {"job_id": JOB, "detail": "spans", "offset": 1, "limit": 1}))
            assert last["next_offset"] is None and last["items"][0]["segment"] == 2
            assert last["items"][0]["output_start"] == pytest.approx(4.0)
            full = _payload(await client.call_tool("briefcut_timeline", {"job_id": JOB, "detail": "edit"}))
            assert full["edit"]["look"]["preset"] == "warm" and full["edit"]["captions"]["style"] == "boxed"

            bad = _payload(await client.call_tool("briefcut_timeline", {"job_id": JOB, "detail": "everything"}))
            assert "tool_error" in bad and "summary" in bad["tool_error"]
            too_many = _payload(await client.call_tool("briefcut_timeline", {"job_id": JOB, "detail": "spans", "limit": 500}))
            assert "tool_error" in too_many and "200" in too_many["tool_error"]
            unknown = _payload(await client.call_tool("briefcut_timeline", {"job_id": "b" * 32}))
            assert _refused(unknown, "Campaign not found.")

    _run(flow())
    stub.job.update(plan=None, status="analyzing")

    async def unplanned() -> None:
        async with Client(server) as client:
            waiting = _payload(await client.call_tool("briefcut_timeline", {"job_id": JOB}))
            assert "tool_error" in waiting and "briefcut_wait" in waiting["tool_error"] and "analyzing" in waiting["tool_error"]

    _run(unplanned())


def test_edit_is_a_validated_dry_run_until_render_keeps_it(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _EditStub(_planned_job())
    server = _serve(monkeypatch, stub)
    change = {"look": {"preset": "warm"}, "captions": {"style": "bold"}}
    trim = [{"op": "trim_end", "segment": 1, "seconds": 3.5}]

    async def flow() -> None:
        async with Client(server) as client:
            draft = _payload(await client.call_tool("briefcut_edit", {"job_id": JOB, "edit": change, "patches": trim}))
            assert "tool_error" not in draft, draft
            assert draft["rendered"] is False and "Nothing was saved" in draft["next"]
            assert {"path": "look.preset", "before": "none", "after": "warm"} in draft["changes"]
            assert {"path": "captions.style", "before": "boxed", "after": "bold"} in draft["changes"]
            assert draft["patches"][0]["op"] == "trim_end" and draft["patches"][0]["skipped"] is None
            assert draft["duration"] == pytest.approx(6.5) and draft["segments"][0]["end"] == pytest.approx(3.5)
            assert draft["meta"] == [] and draft["warnings"] == []
            assert stub.rendered == [] and stub.job["plan"]["edit"]["look"]["preset"] == "none"

            # Invalid values are refused by edit_spec through the manager, with its own words.
            style = _payload(await client.call_tool("briefcut_edit", {"job_id": JOB, "edit": {"captions": {"style": "comic"}}}))
            assert _refused(style, "Caption style is not one of the available choices.")
            strength = _payload(await client.call_tool("briefcut_edit", {"job_id": JOB, "edit": {"look": {"intensity": 5}}, "render": True}))
            assert "tool_error" in strength and "Look strength must be between 0 and 1" in strength["tool_error"]
            skipped = _payload(await client.call_tool("briefcut_edit", {"job_id": JOB, "patches": [{"op": "remove", "segment": 9}]}))
            assert "tool_error" in skipped and "unknown moment" in skipped["tool_error"]
            empty = _payload(await client.call_tool("briefcut_edit", {"job_id": JOB}))
            assert "tool_error" in empty and "Send an edit" in empty["tool_error"]
            unknown = _payload(await client.call_tool("briefcut_edit", {"job_id": "b" * 32, "edit": change}))
            assert _refused(unknown, "Campaign not found.")
            assert stub.rendered == []

            kept = _payload(await client.call_tool("briefcut_edit", {"job_id": JOB, "edit": change, "patches": trim, "render": True}))
            assert "tool_error" not in kept, kept
            assert kept["rendered"] is True and kept["revision"] == 1 and kept["status"] == "queued"
            assert {"path": "look.preset", "before": "none", "after": "warm"} in kept["changes"]
            assert kept["duration"] == pytest.approx(6.5) and "briefcut_wait" in kept["next"]
            payload = stub.rendered[-1]
            assert set(payload) == {"plan"}
            assert payload["plan"]["edit"]["look"]["preset"] == "warm" and payload["plan"]["segments"][0]["end"] == pytest.approx(3.5)
            assert stub.job["plan"]["edit"]["captions"]["style"] == "bold"

            busy = _payload(await client.call_tool("briefcut_edit", {"job_id": JOB, "edit": {"look": {"preset": "cool"}}, "render": True}))
            assert "tool_error" in busy and "queued" in busy["tool_error"]
            stub.job["status"] = "complete"
            same = _payload(await client.call_tool("briefcut_edit", {"job_id": JOB, "edit": change, "render": True}))
            assert "tool_error" in same and "changes nothing" in same["tool_error"]
            framed = _payload(await client.call_tool("briefcut_edit", {"job_id": JOB, "edit": {"look": {"preset": "cool"}}, "render": True,
                                                                       "captions": False, "aspect": "16:9"}))
            assert framed["rendered"] is True and framed["revision"] == 2
            assert stub.rendered[-1]["captions"] is False and stub.rendered[-1]["aspect"] == "16:9"
            assert len(stub.rendered) == 2

    _run(flow())


def test_edit_names_meta_uses_and_renders_them_on_the_projects_consent(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _EditStub(_planned_job())
    monkeypatch.setattr(mcp_server, "capabilities", lambda: {"meta": {"available": False, "reason": "Meta is off."}})
    server = _serve(monkeypatch, stub)
    spanish = {"captions": {"language": "es"}}

    async def flow() -> None:
        async with Client(server) as client:
            draft = _payload(await client.call_tool("briefcut_edit", {"job_id": JOB, "edit": spanish}))
            assert len(draft["meta"]) == 1 and "caption text goes to Meta" in draft["meta"][0]
            assert any("Meta is off." in warning and "render will fail" in warning for warning in draft["warnings"])
            assert stub.rendered == []
            # The person agreed to Meta when the project was made; the result says what goes, and it renders.
            rendered = _payload(await client.call_tool("briefcut_edit", {"job_id": JOB, "edit": spanish, "render": True}))
            assert rendered["rendered"] is True and "caption text goes to Meta" in rendered["meta"][0]
            assert stub.rendered[-1]["plan"]["edit"]["captions"]["language"] == "es"
            gone = _payload(await client.call_tool("briefcut_edit", {"job_id": JOB, "edit": spanish, "render": True, "allow_meta": True}))
            assert "tool_error" in gone, "allow_meta is no longer a parameter"

            stub.job["status"] = "complete"
            silent = _payload(await client.call_tool("briefcut_edit", {"job_id": JOB, "edit": {"captions": {"language": "fr"}}, "captions": False}))
            assert silent["meta"] == [] and any("Captions are off" in warning for warning in silent["warnings"])
            # A project saved before Briefcut was Meta-only never uploads footage for a cutout, and says so.
            cutout = _payload(await client.call_tool("briefcut_edit", {"job_id": JOB, "edit": {"cutout": {"mode": "blur", "subject": "the bowl"}}}))
            assert cutout["meta"] == [] and any("not made with Meta" in warning for warning in cutout["warnings"])
            unnamed = _payload(await client.call_tool("briefcut_edit", {"job_id": JOB, "edit": {"cutout": {"mode": "blur"}}}))
            assert "tool_error" in unnamed and "Name the subject" in unnamed["tool_error"]

    _run(flow())
    stub.job["request"]["provider"] = "meta"

    async def meta_project() -> None:
        async with Client(server) as client:
            blur = {"cutout": {"mode": "blur", "subject": "the bowl"}}
            draft = _payload(await client.call_tool("briefcut_edit", {"job_id": JOB, "edit": blur}))
            assert len(draft["meta"]) == 1 and "Meta SAM" in draft["meta"][0]
            assert any("mask decoder" in warning for warning in draft["warnings"])
            cutout = _payload(await client.call_tool("briefcut_edit", {"job_id": JOB, "edit": blur, "render": True}))
            assert cutout["rendered"] is True and "Meta SAM" in cutout["meta"][0]

            # A translation saved while captions were off starts when they are turned on, and says so.
            stub.job["status"] = "complete"
            stub.job["request"]["captions"] = False
            stub.job["plan"]["edit"]["captions"]["language"] = "fr"
            turned_on = _payload(await client.call_tool("briefcut_edit", {"job_id": JOB, "edit": {"look": {"preset": "mono"}}, "captions": True, "render": True}))
            assert turned_on["rendered"] is True and any("Translate the captions to fr" in use for use in turned_on["meta"])
            stub.job["status"] = "complete"
            stays_off = _payload(await client.call_tool("briefcut_edit", {"job_id": JOB, "edit": {"look": {"preset": "warm"}}, "render": True}))
            assert stays_off["rendered"] is True and stays_off["meta"] == []

    _run(meta_project())
    assert len(stub.rendered) == 4 and "captions" not in stub.rendered[-1]


def test_edit_does_not_claim_a_render_the_manager_did_not_queue(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class InFlight(_EditStub):
        def render(self, ident: str, value: dict[str, Any]) -> dict[str, Any]:
            # CampaignManager.render returns the running job unchanged when a render is already in flight.
            return self.get(ident)

    server = _serve(monkeypatch, InFlight(_planned_job()))

    async def flow() -> None:
        async with Client(server) as client:
            result = _payload(await client.call_tool("briefcut_edit", {"job_id": JOB, "edit": {"look": {"preset": "warm"}}, "render": True}))
            assert _refused(result, "already rendering, so this draft was not kept")

    _run(flow())


def test_add_asset_stages_the_file_for_the_manager_and_removes_the_copy(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sessions = workspace / "vedit-sessions"
    seen: list[tuple[str, Path, bytes]] = []

    class Stub(_EditStub):
        def add_asset(self, ident: str, value: dict[str, Any]) -> dict[str, Any]:
            path = Path(value["path"])
            seen.append((ident, path, path.read_bytes()))
            return {"id": "c" * 12, "kind": "image", "name": path.name, "duration": None, "width": 4, "height": 4}

    logo = workspace / "brand logo.png"
    logo.write_bytes(b"\x89PNG fake")
    server = _serve(monkeypatch, Stub(_planned_job(), sessions))

    async def flow() -> None:
        async with Client(server) as client:
            record = _payload(await client.call_tool("briefcut_add_asset", {"job_id": JOB, "path": str(logo)}))
            assert record["id"] == "c" * 12 and record["name"] == "brand_logo.png"
            relative = _payload(await client.call_tool("briefcut_add_asset", {"job_id": JOB, "path": "logo.png"}))
            assert "tool_error" in relative and "absolute" in relative["tool_error"]

    _run(flow())
    ident, staged, body = seen[0]
    assert ident == JOB and body == b"\x89PNG fake"
    assert staged.relative_to(sessions).parts[1] == "inbox"
    assert not staged.parent.parent.exists()


def test_add_asset_and_background_use_the_real_manager_gates(workspace: Path) -> None:
    note = workspace / "music.mp3"
    note.write_bytes(b"not really audio")
    server = mcp_server.build_server()

    async def flow() -> None:
        async with Client(server) as client:
            listed = {tool.name: tool for tool in (await client.list_tools()).tools}
            background = listed["briefcut_generate_background"]
            assert "Meta" in background.description and "$0.01" in background.description
            schema = getattr(background, "input_schema", None) or background.inputSchema
            assert "consent" in schema["required"] and "prompt" in schema["required"]
            assert "allow_meta" not in listed["briefcut_edit"].description

            missing = _payload(await client.call_tool("briefcut_add_asset", {"job_id": JOB, "path": str(note)}))
            assert _refused(missing, "Campaign not found.")
            refused = _payload(await client.call_tool("briefcut_generate_background", {"job_id": JOB, "prompt": "a calm studio", "consent": False}))
            assert _refused(refused, "Confirm the cost before generating a background.")
            unknown = _payload(await client.call_tool("briefcut_generate_background", {"job_id": JOB, "prompt": "a calm studio", "consent": True}))
            assert _refused(unknown, "Campaign not found.")

    _run(flow())
    # The staged copy is gone after a refused add; only the manager's own folders remain.
    assert not [path for path in (workspace / "vedit-sessions").glob("source-*") if path.exists()]


def test_status_reports_capabilities_offline_and_never_the_key(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vedit import meta

    secret = "sk-briefcut-test-secret-0123456789"
    monkeypatch.setenv("MODEL_API_KEY", secret)
    monkeypatch.setenv("VEDIT_META_ENABLED", "1")

    def offline(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("briefcut_status must not call Meta")

    monkeypatch.setattr(meta, "_request", offline)
    server = mcp_server.build_server()

    async def flow() -> dict[str, Any]:
        async with Client(server) as client:
            return _payload(await client.call_tool("briefcut_status", {}))

    status = _run(flow())
    assert "tool_error" not in status, status
    assert secret not in json.dumps(status)
    assert "local" not in status and set(status["render"]) == {"ready", "reason"}
    assert status["ready"] is status["render"]["ready"] and status["meta"]["available"] is True
    assert status["meta"]["configured"] is True and status["meta"]["enabled"] is True
    assert set(status["meta"]["features"]) >= {"story", "translate", "cutout", "background"}
    choices = status["editor"]["choices"]
    assert "none" in choices["look.preset"] and "bold" in choices["captions.style"] and "mp4" in choices["export.format"]
    assert "set_edit" in status["director"]["patch_ops"] and status["limits"]["segments"] >= 2
    assert status["workspace"] == str(workspace.resolve())
    # Status reads capabilities only; it never takes the workspace lease.
    assert mcp_server._STATE["manager"] is None


@pytest.mark.render
@pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg required")
def test_edit_renders_a_validated_change_end_to_end(workspace: Path, offline_create: list[dict[str, Any]]) -> None:
    clip = workspace / "upload" / "talk.mp4"
    clip.parent.mkdir()
    shutil.copyfile(FIXTURE, clip)
    server = mcp_server.build_server()

    async def flow() -> None:
        async with Client(server) as client:
            imported = _payload(await client.call_tool("briefcut_import_clip", {"path": str(clip)}))
            job = _payload(await client.call_tool("briefcut_create_story", {
                "paths": imported["paths"], "goal": "Show the talk clearly",
                "target_duration": 3, "aspect": "9:16", "captions": False,
            }))
            job_id = job["id"]
            planned = _payload(await client.call_tool("briefcut_wait", {"job_id": job_id, "timeout_seconds": 60}))
            assert planned.get("status") == "planned", planned

            summary = _payload(await client.call_tool("briefcut_timeline", {"job_id": job_id}))
            assert "tool_error" not in summary, summary
            assert summary["duration"] > 0 and summary["segments"] and summary["span_count"] >= 1

            invalid = _payload(await client.call_tool("briefcut_edit", {"job_id": job_id, "edit": {"transition": {"type": "spin"}}, "render": True}))
            assert _refused(invalid, "Transition is not one of the available choices.")
            dry = _payload(await client.call_tool("briefcut_edit", {"job_id": job_id, "edit": {"look": {"preset": "warm"}}}))
            assert dry["rendered"] is False and {"path": "look.preset", "before": "none", "after": "warm"} in dry["changes"]
            unchanged = _payload(await client.call_tool("briefcut_get", {"job_id": job_id}))
            assert unchanged["plan"]["edit"]["look"]["preset"] == "none" and unchanged["revision"] == planned["revision"]

            kept = _payload(await client.call_tool("briefcut_edit", {"job_id": job_id, "edit": {"look": {"preset": "warm"}, "fade": {"out": 0.3}}, "render": True}))
            assert "tool_error" not in kept, kept
            assert kept["rendered"] is True and kept["revision"] == planned["revision"] + 1
            complete = _payload(await client.call_tool("briefcut_wait", {"job_id": job_id, "timeout_seconds": 120}))
            assert complete.get("status") == "complete", complete
            receipts = _payload(await client.call_tool("briefcut_receipts", {"job_id": job_id}))
            assert receipts["checks"] and receipts["passed"] == receipts["total"] and receipts["proof"]["has_video"]
            assert complete["plan"]["edit"]["look"]["preset"] == "warm" and complete["plan"]["edit"]["fade"]["out"] == pytest.approx(0.3)

            strip = _payload(await client.call_tool("briefcut_artifact_path", {"job_id": job_id, "name": "filmstrip"}))
            assert "tool_error" not in strip, strip
            assert Path(strip["path"]).is_file() and Path(strip["path"]).read_bytes()[:2] == b"\xff\xd8" and strip["bytes"] > 0
            pinned = _payload(await client.call_tool("briefcut_artifact_path", {"job_id": job_id, "name": "filmstrip", "revision": 1}))
            assert "tool_error" in pinned and "revisions" in pinned["tool_error"]

    _run(flow())


# ---------------------------------------------------------------------------
# Meta is the only mode.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("tool", "arguments"), [
    ("briefcut_create_story", {"paths": ["/x.mp4"], "goal": "Show it", "provider": "local"}),
    ("briefcut_create_story", {"paths": ["/x.mp4"], "goal": "Show it", "caption_provider": "local"}),
    ("briefcut_script_to_video", {"script": "One line.", "provider": "local"}),
])
def test_create_tools_refuse_local_mode_before_the_manager(workspace: Path, monkeypatch: pytest.MonkeyPatch, tool: str, arguments: dict[str, Any]) -> None:
    monkeypatch.setattr(mcp_server, "_manager", lambda: pytest.fail("a local request must not reach the manager"))
    server = mcp_server.build_server()

    async def flow() -> dict[str, Any]:
        async with Client(server) as client:
            return _payload(await client.call_tool(tool, arguments))

    refused = _run(flow())
    assert _refused(refused, "Meta models only")


def test_create_tools_default_to_meta(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, dict[str, Any]]] = []

    class Stub:
        _closed = False

        def create(self, value: dict[str, Any]) -> dict[str, Any]:
            sent.append(("story", value))
            return {"id": "story-1", "status": "queued"}

        def create_script(self, value: dict[str, Any]) -> dict[str, Any]:
            sent.append(("script", value))
            return {"id": "script-1", "status": "queued"}

    server = _serve(monkeypatch, Stub())

    async def flow() -> None:
        async with Client(server) as client:
            listed = {item.name: item for item in (await client.list_tools()).tools}
            for name in ("briefcut_create_story", "briefcut_script_to_video"):
                schema = getattr(listed[name], "input_schema", None) or listed[name].inputSchema
                assert schema["properties"]["provider"]["default"] == "meta"
                assert "whisper" not in listed[name].description.lower()
            story = getattr(listed["briefcut_create_story"], "input_schema", None) or listed["briefcut_create_story"].inputSchema
            assert story["properties"]["caption_provider"]["default"] == "meta"
            await client.call_tool("briefcut_create_story", {"paths": ["/x.mp4"], "goal": "Show it"})
            await client.call_tool("briefcut_script_to_video", {"script": "One line."})

    _run(flow())
    assert [(kind, value["provider"]) for kind, value in sent] == [("story", "meta"), ("script", "meta")]
    assert sent[0][1]["caption_provider"] == "meta"


def test_status_reads_the_real_capabilities_and_names_meta_as_the_requirement(workspace: Path) -> None:
    # No stub: the shape briefcut_status reads is the one campaign.capabilities() returns today.
    from vedit.campaign import capabilities

    real = capabilities()
    assert "local" not in real and {"render", "meta", "editor", "script", "director", "limits", "host", "effects"} <= set(real)
    server = mcp_server.build_server()

    async def flow() -> dict[str, Any]:
        async with Client(server) as client:
            return _payload(await client.call_tool("briefcut_status", {}))

    status = _run(flow())
    assert "tool_error" not in status, status
    assert status["ready"] is False and status["meta"]["available"] is False
    assert any(line.startswith("Meta:") and "https://dev.meta.ai" in line for line in status["missing"])
    assert status["render"] == {"ready": real["render"]["ready"], "reason": real["render"]["reason"]}
    assert "whisper" not in json.dumps(status).lower()
