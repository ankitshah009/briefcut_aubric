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
from vedit.campaign import CampaignError  # noqa: E402
from vedit.media import resolve_ffmpeg  # noqa: E402

FIXTURE = Path(__file__).resolve().parents[1] / "examples" / "demo" / "talk.mp4"


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


@pytest.mark.render
@pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg required")
def test_import_plan_render_receipts_and_artifact_over_mcp(workspace: Path) -> None:
    clip = workspace / "upload" / "talk.mp4"
    clip.parent.mkdir()
    shutil.copyfile(FIXTURE, clip)
    server = mcp_server.build_server()

    async def flow() -> None:
        async with Client(server) as client:
            listed = await client.list_tools()
            names = {tool.name for tool in listed.tools}
            assert {"briefcut_import_clip", "briefcut_create_story", "briefcut_wait", "briefcut_render", "briefcut_revise", "briefcut_receipts", "briefcut_artifact_path", "briefcut_cancel"} <= names

            imported = _payload(await client.call_tool("briefcut_import_clip", {"path": str(clip)}))
            assert "tool_error" not in imported, imported
            assert imported["bytes"] == FIXTURE.stat().st_size
            inbox = Path(imported["paths"][0])
            assert inbox.is_file()
            assert inbox.relative_to(workspace / "vedit-sessions").parts[1] == "inbox"

            job = _payload(await client.call_tool("briefcut_create_story", {
                "paths": imported["paths"], "goal": "Show the talk clearly", "provider": "local",
                "target_duration": 3, "aspect": "9:16", "captions": False,
            }))
            assert "tool_error" not in job, job
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
