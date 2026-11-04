"""Tamper-evident receipts: stable ids, exact tamper reports, the CLI, the MCP tool, a real render."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from vedit import evidence
from vedit.cli import main
from vedit.evidence import RECEIPTS, bundle_receipts, canonical, evidence_id, root_id, seal, verify, write_receipts

pytest.importorskip("mcp")
from mcp.client.client import Client  # noqa: E402

from vedit import mcp_server  # noqa: E402
from vedit.campaign import CampaignError  # noqa: E402

JOB = "b" * 32
CHECKS = [
    {"name": "lint: hook in the first 2 seconds", "passed": True, "at": 0.4, "note": "Opens on the product.", "basis": "derived", "by": "lint", "inputs": ["meta-voice-transcribe"]},
    {"name": "auto: requested edits applied", "passed": True, "at": None, "note": "The brief asked for no specific edits.", "basis": "derived", "by": "brief-parser"},
    {"name": "director: pacing", "passed": False, "at": 1.25, "note": "The middle drags.", "basis": "inferred", "by": "muse-spark"},
]
PROOF = {"duration": 1.5, "width": 1080, "height": 1920, "fps": 30.0, "has_audio": True, "has_video": True,
         "color_space": "bt709", "color_range": "tv", "color_primaries": "bt709", "color_transfer": "bt709", "size_bytes": 4096}
STREAMS = SimpleNamespace(format_name="mov,mp4", streams=(
    SimpleNamespace(index=0, codec_type="video", codec_name="h264", width=1080, height=1920, fps=30.0, duration=1.5, sample_rate=None, channels=None, pix_fmt="yuv420p"),
    SimpleNamespace(index=1, codec_type="audio", codec_name="aac", width=None, height=None, fps=None, duration=1.5, sample_rate=48000, channels=2, pix_fmt=None),
))


def _meta_log(project: Path) -> None:
    lines = [
        {"event": "start", "id": "call1", "at": 1793819617.37, "operation": "story", "model": "muse-spark", "purpose": "Plan the story", "reserved_usd": 0.02, "budget_usd": 0.25, "size": {}, "pid": 1, "v": 1},
        {"event": "end", "id": "call1", "at": 1793819620.0, "status": "done", "size": {"input_tokens": 900, "output_tokens": 200}, "cost_usd": 0.0123, "cost_is_estimate": False, "request_id": "resp_abc123", "v": 1},
    ]
    (project / "meta").mkdir(parents=True, exist_ok=True)
    (project / "meta" / "meta-calls.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")


def _render_dir(tmp_path: Path, *, note: str | None = "Hold the bowl a beat longer.") -> tuple[Path, dict[str, Any]]:
    """A render folder laid out like the campaign's, with receipts.json and campaign.zip."""
    project = tmp_path / "project"
    work = project / "render-2"
    work.mkdir(parents=True)
    _meta_log(project)
    video = work / "video.mp4"
    video.write_bytes(bytes(range(256)) * 16)
    (work / "report.json").write_text(json.dumps({"proof": PROOF, "checks": CHECKS}), encoding="utf-8")
    (work / "post.txt").write_text("A look at the product.\n", encoding="utf-8")
    plan = {"segments": [{"start": 0.2, "end": 1.7}], "duration": 1.5, "edit": {"look": {"preset": "warm"}},
            "analysis": {"transcribed_by": "meta-voice-transcribe", "transcript": [{"start": 0.0, "end": 1.0, "text": "hi", "estimated": True}]}}
    result = {"checks": CHECKS, "proof": PROOF, "revision": 2, "caption_provider": "meta", "applied_patches": [{"op": "hold"}]}
    files = [(work / "report.json", "report.json"), (work / "post.txt", "post.txt")]
    receipts = bundle_receipts(result, video, plan=plan, project_root=project, base=work, info=STREAMS, files=files, video_name="video.mp4", note=note)
    write_receipts(receipts, work / RECEIPTS)
    with zipfile.ZipFile(work / "campaign.zip", "w") as archive:
        for path, name in [(video, "video.mp4"), *files, (work / RECEIPTS, RECEIPTS)]:
            archive.write(path, name)
    return work, receipts


def _rezip(source: Path, dest: Path, **replace: bytes) -> Path:
    with zipfile.ZipFile(source) as old, zipfile.ZipFile(dest, "w") as new:
        for name in old.namelist():
            new.writestr(name, replace.get(name.replace(".", "_"), old.read(name)))
    return dest


def _edit_receipts(work: Path, change) -> dict[str, Any]:
    receipts = json.loads((work / RECEIPTS).read_text(encoding="utf-8"))
    change(receipts)
    (work / RECEIPTS).write_text(json.dumps(receipts, indent=2), encoding="utf-8")
    return receipts


def _changed(report: dict[str, Any]) -> list[str]:
    return [item["item"] for item in report["changed"]]


# --- ids --------------------------------------------------------------------

def test_ids_ignore_key_order_float_noise_whitespace_and_unicode_form() -> None:
    one = {"name": "café", "at": 0.30000000000000004, "nested": {"b": 2.0, "a": [1, 0.1 + 0.2]}, "passed": True}
    two = {"passed": True, "nested": {"a": [1.0, 0.3], "b": 2}, "at": 0.3, "name": "café"}
    assert canonical(one) == canonical(two) == '{"at":0.3,"name":"café","nested":{"a":[1,0.3],"b":2},"passed":true}'
    assert evidence_id(one) == evidence_id(two)
    assert evidence_id(one).startswith("ev_") and len(evidence_id(one)) == 3 + 64
    # The id never covers itself, and real changes always move it.
    assert evidence_id({**one, "evidence_id": "ev_anything"}) == evidence_id(one)
    assert evidence_id({**one, "passed": False}) != evidence_id(one)
    assert evidence_id({**one, "at": 0.31}) != evidence_id(one)
    assert canonical({"x": float("nan"), "y": -0.0}) == '{"x":null,"y":0}'
    with pytest.raises(TypeError):
        canonical({"x": object()})


def test_seal_and_root_are_deterministic(tmp_path: Path) -> None:
    sealed = seal({"kind": "check", "b": 1.0000000001, "a": "x"})
    assert sealed == {"kind": "check", "a": "x", "b": 1, "evidence_id": evidence_id({"a": "x", "b": 1, "kind": "check"})}
    work, receipts = _render_dir(tmp_path)
    again = json.loads((work / RECEIPTS).read_text(encoding="utf-8"))
    assert root_id(again) == receipts["evidence_id"]


def test_receipts_hold_the_file_probe_checks_meta_calls_plan_versions_and_limits(tmp_path: Path) -> None:
    work, receipts = _render_dir(tmp_path)
    video = receipts["video"]
    assert video["sha256"] == evidence.file_sha256(work / "video.mp4")[0] and video["size_bytes"] == 4096 and video["path"] == "video.mp4"
    assert receipts["probe"]["color"] == {"space": "bt709", "range": "tv", "primaries": "bt709", "transfer": "bt709"}
    assert [stream["codec_type"] for stream in receipts["probe"]["streams"]] == ["video", "audio"]
    assert [check["name"] for check in receipts["checks"]] == [check["name"] for check in CHECKS]
    assert receipts["checks"][0]["inputs"] == ["meta-voice-transcribe"] and receipts["checks"][1]["inputs"] == []
    assert len({check["evidence_id"] for check in receipts["checks"]}) == 3
    assert (receipts["passed"], receipts["total"]) == (2, 3)
    call = receipts["meta_calls"]["calls"][0]
    assert (call["model"], call["purpose"], call["cost_usd"], call["request_id"]) == ("muse-spark", "Plan the story", 0.0123, "resp_abc123")
    assert receipts["meta_calls"]["log"] == "meta/meta-calls.jsonl"
    assert receipts["plan"]["edit_sha256"] == evidence.digest({"look": {"preset": "warm"}})
    assert receipts["plan"]["note"] == "Hold the bowl a beat longer." and receipts["plan"]["revision"] == 2
    assert receipts["versions"]["briefcut"] and receipts["versions"]["receipts"] == 1
    limits = " ".join(receipts["limitations"]["items"])
    assert "not a signature" in limits and "Word times are estimated" in limits and "1 check is a model's judgment" in limits
    assert {item["name"] for item in receipts["files"]["files"]} == {"report.json", "post.txt"}
    text = (work / RECEIPTS).read_text(encoding="utf-8")
    assert str(tmp_path) not in text, "receipts must not leak absolute paths"


# --- verify -----------------------------------------------------------------

@pytest.mark.parametrize("where", ["zip", "dir", "project", "file"])
def test_an_untouched_bundle_verifies(tmp_path: Path, where: str) -> None:
    work, receipts = _render_dir(tmp_path)
    target = {"zip": work / "campaign.zip", "dir": work, "project": work.parent, "file": work / RECEIPTS}[where]
    report = verify(target, expect=receipts["evidence_id"])
    assert report["ok"], report["changed"]
    assert report["evidence_id"] == receipts["evidence_id"]
    names = [item["item"] for item in report["items"]]
    assert {"video.mp4", "report.json", "post.txt", "root id", "expected root", "probe record", "meta calls record"} <= set(names)
    assert sum(name.startswith("check ") for name in names) == 3
    assert verify(target, expect=evidence.short_id(receipts["evidence_id"]))["ok"]


def test_one_byte_of_the_video_breaks_it_and_names_the_video(tmp_path: Path) -> None:
    work, _ = _render_dir(tmp_path)
    data = bytearray((work / "video.mp4").read_bytes())
    data[1000] ^= 1
    tampered = _rezip(work / "campaign.zip", tmp_path / "tampered.zip", video_mp4=bytes(data))
    report = verify(tampered)
    assert not report["ok"]
    assert _changed(report) == ["video.mp4"]
    assert "sha256 differs" in report["changed"][0]["detail"]
    (work / "video.mp4").write_bytes(bytes(data))
    assert _changed(verify(work)) == ["video.mp4"]


def test_a_flipped_check_names_that_check_and_the_count(tmp_path: Path) -> None:
    work, _ = _render_dir(tmp_path)
    _edit_receipts(work, lambda receipts: receipts["checks"][2].update(passed=True))
    report = verify(work)
    # The stored id still fits the root, so the record itself is named, and the count that no longer adds up.
    assert _changed(report) == ['check 2 "director: pacing"', "passed/total"]


def test_an_edited_check_note_names_only_that_check(tmp_path: Path) -> None:
    work, _ = _render_dir(tmp_path)
    _edit_receipts(work, lambda receipts: receipts["checks"][0].update(note="Opens on a perfect hook."))
    assert _changed(verify(work)) == ['check 0 "lint: hook in the first 2 seconds"']


def test_an_edited_revision_note_names_the_plan_record(tmp_path: Path) -> None:
    work, _ = _render_dir(tmp_path)
    _edit_receipts(work, lambda receipts: receipts["plan"].update(note="Make it longer."))
    assert _changed(verify(work)) == ["plan record"]


def test_a_recomputed_record_id_still_breaks_the_root(tmp_path: Path) -> None:
    work, receipts = _render_dir(tmp_path)

    def forge(data: dict[str, Any]) -> None:
        data["checks"][1]["note"] = "Forged."
        data["checks"][1]["evidence_id"] = evidence_id(data["checks"][1])

    _edit_receipts(work, forge)
    assert _changed(verify(work)) == ["root id"]
    # A forger who also recomputes the root makes a consistent file. Only a root from a trusted place catches it.
    forged = _edit_receipts(work, lambda data: data.update(evidence_id=root_id(data)))
    assert verify(work)["ok"]
    report = verify(work, expect=receipts["evidence_id"])
    assert _changed(report) == ["expected root"] and forged["evidence_id"] != receipts["evidence_id"]


def test_other_bundle_files_missing_or_changed_are_named(tmp_path: Path) -> None:
    work, _ = _render_dir(tmp_path)
    tampered = _rezip(work / "campaign.zip", tmp_path / "report.zip", report_json=b'{"proof": "forged"}')
    assert _changed(verify(tampered)) == ["report.json"]
    (work / "post.txt").unlink()
    report = verify(work)
    assert _changed(report) == ["post.txt"] and report["changed"][0]["detail"] == "post.txt is missing"


def test_a_path_that_leaves_the_folder_is_never_read(tmp_path: Path) -> None:
    work, _ = _render_dir(tmp_path)
    (tmp_path / "secret.txt").write_text("x", encoding="utf-8")

    def escape(data: dict[str, Any]) -> None:
        data["files"]["files"][0]["path"] = "../../secret.txt"
        data["files"]["evidence_id"] = evidence_id(data["files"])
        data["evidence_id"] = root_id(data)

    _edit_receipts(work, escape)
    report = verify(work)
    assert _changed(report) == ["report.json"] and "not a path inside this folder" in report["changed"][0]["detail"]


def test_nothing_to_verify_raises(tmp_path: Path) -> None:
    with zipfile.ZipFile(tmp_path / "old.zip", "w") as archive:
        archive.writestr("video.mp4", b"old")
    with pytest.raises(evidence.ReceiptsMissing, match="before Briefcut wrote receipts"):
        verify(tmp_path / "old.zip")
    with pytest.raises(evidence.ReceiptsMissing):
        verify(tmp_path / "nowhere")
    (tmp_path / RECEIPTS).write_text('{"format": "other"}', encoding="utf-8")
    with pytest.raises(evidence.ReceiptsMissing, match="not a Briefcut receipts file"):
        verify(tmp_path)


# --- CLI ----------------------------------------------------------------------

def test_cli_exit_codes_and_plain_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    work, receipts = _render_dir(tmp_path)
    assert main(["verify", str(work / "campaign.zip")]) == 0
    out = capsys.readouterr().out
    assert receipts["evidence_id"] in out and "Verified. All" in out and "OK       video.mp4: sha256 matches" in out
    assert main(["verify", str(work), "--expect", receipts["evidence_id"]]) == 0
    capsys.readouterr()

    data = bytearray((work / "video.mp4").read_bytes())
    data[0] ^= 0xFF
    _rezip(work / "campaign.zip", tmp_path / "bad.zip", video_mp4=bytes(data))
    assert main(["verify", str(tmp_path / "bad.zip")]) == 1
    out = capsys.readouterr().out
    assert "CHANGED  video.mp4: sha256 differs" in out and "Not verified. 1 of" in out and "OK " not in out

    assert main(["verify", str(work), "--expect", "ev_000000000000"]) == 1
    assert "CHANGED  expected root" in capsys.readouterr().out
    assert main(["verify", str(work), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert main(["verify", str(tmp_path / "missing")]) == 2
    assert "does not exist" in capsys.readouterr().err


# --- MCP ----------------------------------------------------------------------

class _VerifyStub:
    _closed = False

    def __init__(self, job: dict[str, Any], bundle: Path | None) -> None:
        self.job = job
        self.bundle = bundle

    def get(self, ident: str) -> dict[str, Any]:
        if ident != self.job["id"]:
            raise CampaignError("Campaign not found.", 404)
        return json.loads(json.dumps(self.job))

    def artifact(self, ident: str, name: str, *, revision: int | None = None) -> Path:
        assert name == "bundle"
        if self.bundle is None:
            raise CampaignError("This download is available after the render is verified.", 409)
        return self.bundle


def _payload(result: Any) -> Any:
    text = "".join(item.text for item in result.content if getattr(item, "type", "") == "text")
    if getattr(result, "is_error", None) or getattr(result, "isError", False):
        return {"tool_error": text}
    return json.loads(text)


def _call_verify(monkeypatch: pytest.MonkeyPatch, stub: _VerifyStub, **arguments: Any) -> Any:
    monkeypatch.setattr(mcp_server, "_manager", lambda: stub)
    server = mcp_server.build_server()

    async def flow() -> Any:
        async with Client(server) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert "briefcut_verify" in names
            return _payload(await client.call_tool("briefcut_verify", {"job_id": JOB, **arguments}))

    return asyncio.run(flow())


def test_mcp_verify_checks_the_bundle_against_the_recorded_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work, receipts = _render_dir(tmp_path)
    job = {"id": JOB, "status": "complete", "result": {"revision": 2, "evidence_id": receipts["evidence_id"]},
           "versions": [{"revision": 2, "status": "verified", "evidence_id": receipts["evidence_id"]}]}
    report = _call_verify(monkeypatch, _VerifyStub(job, work / "campaign.zip"))
    assert report["ok"] is True and report["evidence_id"] == receipts["evidence_id"] and report["revision"] == 2
    assert any(item["item"] == "expected root" and item["ok"] for item in report["items"])
    assert report["receipts"] == "campaign.zip!receipts.json"

    data = bytearray((work / "video.mp4").read_bytes())
    data[7] ^= 1
    bad = _rezip(work / "campaign.zip", tmp_path / "bad.zip", video_mp4=bytes(data))
    report = _call_verify(monkeypatch, _VerifyStub(job, bad), revision=2)
    assert report["ok"] is False and [item["item"] for item in report["changed"]] == ["video.mp4"]

    planned = _call_verify(monkeypatch, _VerifyStub({"id": JOB, "status": "planned", "result": None}, None))
    assert "receipts exist once a render has been verified" in planned["tool_error"]
    old = tmp_path / "old.zip"
    with zipfile.ZipFile(old, "w") as archive:
        archive.writestr("video.mp4", b"x")
    legacy = _call_verify(monkeypatch, _VerifyStub(job, old))
    assert "before Briefcut wrote receipts" in legacy["tool_error"]


# --- a real render ------------------------------------------------------------

def _helpers():
    spec = importlib.util.spec_from_file_location("evidence_test_media_helpers", Path(__file__).with_name("test_e2e.py"))
    assert spec and spec.loader
    helpers = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = helpers
    spec.loader.exec_module(helpers)
    return helpers


@pytest.mark.render
def test_a_real_render_writes_receipts_that_verify_and_catch_tampering(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    helpers = _helpers()
    if not helpers._have_ffmpeg():
        pytest.skip("ffmpeg required")
    from vedit.campaign import CampaignManager

    source =tmp_path / "vedit-sessions" / "source-test" / "inbox" / "clip.mp4"
    source.parent.mkdir(parents=True)
    clip = helpers._make_clip(source, seconds=1.5, color="teal")
    manager = CampaignManager(tmp_path, start_worker=False)
    try:
        job = manager.create({"paths": [str(clip)], "goal": "Show the product clearly", "target_duration": 1, "captions": False})
        manager.process(job["id"])
        manager.render(job["id"], {"plan": {"title": "A clear view", "segments": [{"start": 0.2, "end": 1.2, "label": "Product", "reason": "Chosen."}], "caption": "A look.", "hashtags": []}})
        manager.process(job["id"])
        complete = manager.get(job["id"])
        assert complete["status"] == "complete", complete["error"]
        bundle = manager.artifact(job["id"], "bundle")
        report_path = manager.artifact(job["id"], "report")
        video = manager.artifact(job["id"], "video")
    finally:
        manager.close()
    receipts_path = report_path.with_name(RECEIPTS)
    assert receipts_path.is_file(), "receipts.json sits next to report.json"
    receipts = json.loads(receipts_path.read_text(encoding="utf-8"))
    assert complete["result"]["evidence_id"] == receipts["evidence_id"] == complete["versions"][-1]["evidence_id"]
    assert receipts["video"]["sha256"] == evidence.file_sha256(video)[0]
    assert receipts["probe"]["has_video"] is True and receipts["probe"]["streams"]
    assert receipts["total"] == complete["result"]["total"] and len(receipts["checks"]) == receipts["total"]
    with zipfile.ZipFile(bundle) as archive:
        assert RECEIPTS in archive.namelist()
        assert json.loads(archive.read(RECEIPTS)) == receipts
    assert main(["verify", str(bundle), "--expect", receipts["evidence_id"]]) == 0
    assert main(["verify", str(report_path.parent.parent)]) == 0
    capsys.readouterr()
    data = bytearray(video.read_bytes())
    data[len(data) // 2] ^= 1
    shutil.copyfile(bundle, tmp_path / "copy.zip")
    _rezip(tmp_path / "copy.zip", tmp_path / "tampered.zip", video_mp4=bytes(data))
    assert main(["verify", str(tmp_path / "tampered.zip")]) == 1
    assert "CHANGED  video.mp4: sha256 differs" in capsys.readouterr().out


@pytest.mark.render
def test_a_receipt_that_cannot_be_sealed_warns_and_keeps_the_verified_render(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    helpers = _helpers()
    if not helpers._have_ffmpeg():
        pytest.skip("ffmpeg required")
    from vedit.campaign import CampaignManager

    def broken(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise ValueError("no")

    monkeypatch.setattr(evidence, "bundle_receipts", broken)
    source = tmp_path / "vedit-sessions" / "source-test" / "inbox" / "clip.mp4"
    source.parent.mkdir(parents=True)
    clip = helpers._make_clip(source, seconds=1.5, color="teal")
    manager = CampaignManager(tmp_path, start_worker=False)
    try:
        job = manager.create({"paths": [str(clip)], "goal": "Show the product clearly", "target_duration": 1, "captions": False})
        manager.process(job["id"])
        manager.render(job["id"], {"plan": {"title": "A clear view", "segments": [{"start": 0.2, "end": 1.2, "label": "Product", "reason": "Chosen."}], "caption": "A look.", "hashtags": []}})
        manager.process(job["id"])
        complete = manager.get(job["id"])
        bundle = manager.artifact(job["id"], "bundle")
    finally:
        manager.close()
    assert complete["status"] == "complete", complete["error"]
    assert complete["result"]["evidence_id"] is None
    assert any("receipts could not be written" in warning for warning in complete["result"]["warnings"])
    with zipfile.ZipFile(bundle) as archive:
        assert RECEIPTS not in archive.namelist() and "video.mp4" in archive.namelist()
