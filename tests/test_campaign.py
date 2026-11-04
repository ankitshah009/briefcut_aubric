"""Campaign decisions, durable job transitions, and real compiler execution."""

from __future__ import annotations

import importlib.util
import json
import math
import sys
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from vedit.campaign import CampaignError, CampaignManager, remap_proxy_points, validate_plan
from vedit.compile import OPS
from vedit.compile import compile_brief
from vedit.ir import Brief
from vedit.media import probe
from vedit.pipeline import execute, write_codegen, write_report


@pytest.fixture
def clip(tmp_path: Path) -> Path:
    source = tmp_path / "vedit-sessions" / "source-test" / "inbox" / "clip.mp4"
    source.parent.mkdir(parents=True)
    spec = importlib.util.spec_from_file_location("campaign_test_media_helpers", Path(__file__).with_name("test_e2e.py"))
    assert spec and spec.loader
    helpers = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = helpers
    spec.loader.exec_module(helpers)
    if not helpers._have_ffmpeg():
        pytest.skip("ffmpeg required")
    return helpers._make_clip(source, seconds=1.5, color="teal")


@pytest.fixture
def manager(tmp_path: Path):
    instance = CampaignManager(tmp_path, start_worker=False)
    try:
        yield instance
    finally:
        instance.close()


def request(clip: Path, **overrides):
    return {"paths": [str(clip)], "goal": "Show the product clearly", "target_duration": 1, "captions": False, **overrides}


def story(**overrides):
    return {"title": "A clear view", "segments": [{"start": 0.2, "end": 1.2, "label": "Product", "reason": "Chosen from the source."}], "caption": "A look at the product.", "hashtags": ["making"], **overrides}


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, True, "0"])
def test_plan_rejects_nonfinite_or_non_numeric_times(bad) -> None:
    with pytest.raises(CampaignError):
        validate_plan(story(segments=[{"start": bad, "end": 1}]), 2)


def test_plan_enforces_source_bounds_and_preserves_editorial_order() -> None:
    for segments in ([{"start": 1, "end": 0}], [{"start": 0, "end": 3}], []):
        with pytest.raises(CampaignError):
            validate_plan(story(segments=segments), 2)
    plan = validate_plan(story(segments=[{"start": 1, "end": 2}, {"start": 0, "end": 0.8}], provider="forged", usage={"cost_usd": -99}), 2, original={"provider": "meta", "usage": {"cost_usd": 0.1}})
    assert [segment["start"] for segment in plan["segments"]] == [1, 0]
    assert plan["provider"] == "meta"
    assert plan["usage"] == {"cost_usd": 0.1}
    assert plan["duration"] == 1.8


@pytest.mark.parametrize("field,value", [("provider", []), ("aspect", {}), ("captions", "true"), ("target_duration", math.nan), ("budget_usd", -1), ("language", "../../en"), ("goal", ""), ("goal", "\ud800")])
def test_create_rejects_invalid_request_types(manager: CampaignManager, clip: Path, field, value) -> None:
    with pytest.raises(CampaignError) as error:
        manager.create(request(clip, **{field: value}))
    assert error.value.status == 400
    assert manager.list() == []


def test_create_requires_uploaded_source_and_no_external_symlinks(manager: CampaignManager, tmp_path: Path, clip: Path) -> None:
    private = tmp_path / "private.mp4"
    private.write_bytes(clip.read_bytes())
    symlink = clip.parent / "linked.mp4"
    symlink.symlink_to(private)
    for source in (private, symlink, Path("relative.mp4")):
        with pytest.raises(CampaignError):
            manager.create(request(source))


def test_local_plan_is_honest_idempotent_and_saved(manager: CampaignManager, clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    job = manager.create(request(clip, captions=True, subject="product"))
    assert manager.create(request(clip, captions=True, subject="product"))["id"] == job["id"]
    assert job["status"] == "queued"
    manager.process(job["id"])
    planned = manager.get(job["id"])
    assert planned["status"] == "planned"
    assert planned["plan"]["provider"] == "local"
    assert "does not choose a story with AI" in planned["plan"]["summary"]
    assert planned["plan"]["segments"][0]["start"] == 0
    assert planned["plan"]["duration"] == 1
    assert any("Captions are unavailable" in warning for warning in planned["plan"]["warnings"])
    assert any("center crop" in warning for warning in planned["plan"]["warnings"])
    assert planned["budget"]["reserved_usd"] == 0
    assert not any(key.startswith("_") for key in planned)
    assert manager.artifact(job["id"], "source") == clip
    with pytest.raises(CampaignError) as error:
        manager.artifact(job["id"], "video")
    assert error.value.status == 409


def test_cancel_and_retry_preserve_intent(manager: CampaignManager, clip: Path) -> None:
    job = manager.create(request(clip))
    cancelled = manager.cancel(job["id"])
    assert cancelled["status"] == "cancelled"
    manager.process(job["id"])
    assert manager.get(job["id"])["status"] == "cancelled"
    retry = manager.retry(job["id"])
    assert retry["request"] == job["request"]
    manager.process(job["id"])
    assert manager.get(job["id"])["status"] == "planned"


def test_restart_preserves_plan_and_recovers_interrupted_jobs(tmp_path: Path, clip: Path) -> None:
    first = CampaignManager(tmp_path, start_worker=False)
    job = first.create(request(clip))
    first.process(job["id"])
    approved = first.render(job["id"], {"plan": story()})
    assert approved["status"] == "queued"
    first.close()
    second = CampaignManager(tmp_path, start_worker=False)
    try:
        restored = second.get(job["id"])
        assert restored["status"] == "failed"
        assert restored["stage"] == "interrupted"
        assert restored["plan"]["segments"][0]["start"] == 0.2
        assert second.retry(job["id"])["status"] == "queued"
    finally:
        second.close()


def test_second_manager_cannot_recover_a_live_workers_jobs(manager: CampaignManager, tmp_path: Path, clip: Path) -> None:
    job = manager.create(request(clip))
    with pytest.raises(CampaignError) as error:
        CampaignManager(tmp_path, start_worker=False)
    assert error.value.status == 409
    assert manager.get(job["id"])["status"] == "queued"


def test_cost_receipts_reserve_uncertain_calls_and_deduplicate_ids(tmp_path: Path) -> None:
    root = tmp_path / "meta"
    root.mkdir()
    (root / "meta-usage.json").write_text(json.dumps({"calls": [
        {"id": "first", "reserved_usd": 0.1, "status": "failed_or_incomplete"},
        {"id": "second", "reserved_usd": 0.02, "usage": {"cost_usd": 0.01}, "status": "completed"},
        {"id": "first", "reserved_usd": 0.1},
    ]}))
    summary = CampaignManager._usage_summary(tmp_path, 0.25)
    # The uncertain call keeps its whole reservation ($0.10, counted once); the finished one holds what it billed ($0.01).
    assert {key: summary[key] for key in ("cap_usd", "reserved_usd", "remaining_usd")} == {"cap_usd": 0.25, "reserved_usd": 0.11, "remaining_usd": 0.14}
    assert set(summary["operations"]) == {"story", "tracking", "cover", "transcribe", "review", "patch", "translate", "other"}
    (root / "meta-usage.json").write_text('{"calls": "corrupt"}')
    with pytest.raises(CampaignError, match="unreadable"):
        CampaignManager._usage_summary(tmp_path, 0.25)


def test_budget_breakdown_names_each_meta_operation_and_tracked_subject(tmp_path: Path) -> None:
    ledger = tmp_path / "meta"
    ledger.mkdir()
    (ledger / "meta-usage.json").write_text(json.dumps({"calls": [
        {"id": "s1", "operation": "story", "model": "llama-4", "reserved_usd": 0.05, "status": "failed_or_incomplete"},
        {"id": "s1", "operation": "story", "model": "llama-4", "reserved_usd": 0.05, "usage": {"cost_usd": 0.02}, "status": "completed"},
        {"id": "t1", "operation": "tracking", "model": "sam-3.1", "reserved_usd": 0.03, "usage": {"cost_usd": 0.04}, "status": "completed"},
        {"id": "t2", "operation": "tracking", "model": "sam-3.1", "reserved_usd": 0.03, "status": "failed_or_incomplete"},
    ]}))
    summary = CampaignManager._usage_summary(tmp_path, 0.5)
    assert summary["reserved_usd"] == 0.12
    assert summary["remaining_usd"] == 0.38
    assert summary["cap_usd"] == 0.5
    operations = summary["operations"]
    assert operations["story"] == {"reserved_usd": 0.05, "calls": 1, "model": "llama-4"}
    assert operations["tracking"] == {"reserved_usd": 0.07, "calls": 2, "model": "sam-3.1"}
    assert operations["cover"] == {"reserved_usd": 0.0, "calls": 0, "model": ""}
    assert round(sum(entry["reserved_usd"] for entry in operations.values()), 8) == summary["reserved_usd"]
    assert CampaignManager._tracked_subject({"provider": "meta", "subject": "the red bicycle"}) == "the red bicycle"
    assert CampaignManager._tracked_subject({"provider": "meta", "subject": ""}) is None
    assert CampaignManager._tracked_subject({"provider": "local", "subject": "the red bicycle"}) is None


def test_meta_mode_fails_explicitly_without_access(manager: CampaignManager, clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.delenv("VEDIT_META_ENABLED", raising=False)
    with pytest.raises(CampaignError, match="Meta is not enabled"):
        manager.create(request(clip, provider="meta"))


def test_real_render_uses_approved_ranges_and_packages_verified_output(manager: CampaignManager, clip: Path) -> None:
    job = manager.create(request(clip, aspect="1:1"))
    manager.process(job["id"])
    approved = story(segments=[{"start": 0.7, "end": 1.2}, {"start": 0.1, "end": 0.4}])
    rendering = manager.render(job["id"], {"plan": approved})
    assert manager.render(job["id"], {"plan": approved})["revision"] == rendering["revision"]
    manager.process(job["id"])
    complete = manager.get(job["id"])
    assert complete["status"] == "complete", complete["error"]
    assert complete["result"]["proof"]["width"] == 1080
    assert complete["result"]["proof"]["height"] == 1080
    assert complete["result"]["proof"]["has_video"] is True
    assert complete["result"]["proof"]["has_audio"] is True
    assert complete["result"]["proof"]["color_space"] == "bt709"
    assert complete["result"]["proof"]["color_range"] == "tv"
    video = manager.artifact(job["id"], "video")
    assert abs(probe(video).duration - 0.8) < 0.15
    report = json.loads(manager.artifact(job["id"], "report").read_text())
    assert report["approved_plan"]["segments"][0]["start"] == 0.7
    assert report["proof"] == complete["result"]["proof"]
    assert report["tracked_subject"] is None
    assert complete["result"]["tracked_subject"] is None
    assert set(report["budget"]["operations"]) >= {"story", "tracking", "cover"}
    with zipfile.ZipFile(manager.artifact(job["id"], "bundle")) as archive:
        assert {"video.mp4", "cover.jpg", "post.txt", "brief.yaml", "story.json", "report.json"} <= set(archive.namelist())
        assert archive.read("video.mp4") == video.read_bytes()
        assert b"A look at the product." in archive.read("post.txt")
    rerender = manager.render(job["id"], {})
    assert rerender["revision"] == complete["revision"] + 1


def test_portrait_mov_proxy_preserves_time_and_remaps_letterboxed_subject(manager: CampaignManager, clip: Path) -> None:
    portrait = clip.parent / "phone.mov"
    work = manager.root / "make-portrait"
    brief = Brief(name="phone", sources={"original": str(clip)}, ops=[{"op": "reframe", "aspect": "9:16"}, {"op": "export", "preset": "shorts_9x16", "path": str(portrait)}])
    compiler = compile_brief(brief, work_dir=work)
    write_codegen(compiler.pipeline)
    json.loads((work / "project.json").read_text())
    (work / "render.sh").read_text()
    reports = execute(compiler.pipeline, progress=False)
    write_report(compiler.pipeline, reports, finals=compiler.finals)
    source_info = probe(portrait)
    assert (source_info.width, source_info.height) == (1080, 1920)
    project = manager.root / "proxy-test"
    proxy = manager._analysis_proxy(project, portrait, source_info)
    info = probe(proxy)
    assert (info.width, info.height, info.fps) == (960, 540, 30)
    assert abs(info.duration - source_info.duration) < 0.1
    original_stat = proxy.stat().st_mtime_ns
    assert manager._analysis_proxy(project, portrait, source_info) == proxy
    assert proxy.stat().st_mtime_ns == original_stat
    # The portrait occupies 304x540 pixels inside a 960x540 analysis canvas.
    # Source-normalized box (.2,.1,.3,.4) must survive that transformation.
    points = [{"time": 0.2, "x": (328 + 0.2 * 304) / 960, "y": 0.1, "width": 0.3 * 304 / 960, "height": 0.4}]
    mapped = remap_proxy_points(points, source_width=1080, source_height=1920, proxy_width=960, proxy_height=540, duration=info.duration)
    for key, expected in {"time": 0.2, "x": 0.2, "y": 0.1, "width": 0.3, "height": 0.4}.items():
        assert mapped[0][key] == pytest.approx(expected)
    selected = {"op": "select_ranges", "ranges": [{"start": 0.7, "end": 1.2}, {"start": 0.1, "end": 0.4}]}
    selected_proxy = manager._analysis_proxy(project, portrait, source_info, selected)
    assert selected_proxy != proxy
    assert abs(probe(selected_proxy).duration - 0.8) < 0.15
    assert probe(portrait).size_bytes == source_info.size_bytes
    with pytest.raises(CampaignError, match="outside the picture"):
        remap_proxy_points([{"time": 0, "x": 0, "y": 0, "width": 0.1, "height": 0.2}], source_width=1080, source_height=1920, proxy_width=960, proxy_height=540, duration=1)



def test_caption_provider_is_validated_and_meta_captions_need_access(manager: CampaignManager, clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.delenv("VEDIT_META_ENABLED", raising=False)
    with pytest.raises(CampaignError):
        manager.create(request(clip, captions=True, caption_provider="whisper-cloud"))
    with pytest.raises(CampaignError) as error:
        manager.create(request(clip, captions=True, caption_provider="meta"))
    assert error.value.status == 409
    # Meta captions without the captions switch are inert, so local planning still works offline.
    job = manager.create(request(clip, captions=False, caption_provider="meta"))
    assert job["request"]["caption_provider"] == "meta"
    assert manager.create(request(clip))["request"]["caption_provider"] == "local"


def test_meta_captions_transcribe_only_the_approved_speech_and_burn_it(manager: CampaignManager, clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vedit import meta

    monkeypatch.setenv("MODEL_API_KEY", "test-secret-never-write-to-disk")
    monkeypatch.setenv("VEDIT_META_ENABLED", "1")
    seen: dict[str, object] = {}

    def fake_transcribe(wav: Path, *, duration: float, work_dir: Path, language: str, budget_usd: float):
        info = probe(wav)
        seen.update(duration=duration, wav_duration=info.duration, channels=info.audio.channels, rate=info.audio.sample_rate, language=language, work_dir=work_dir)
        # Doubled words at a turn edge and a split sentence, as Meta Voice Transcribe returns them.
        return {"cues": [{"start": 0.1, "end": 0.4, "text": "made by by hand,"}, {"start": 0.4, "end": 0.7, "text": "hand, the the finished bowl."}], "provider": "meta", "usage": {"cost_usd": 0.0001}}

    monkeypatch.setattr(meta, "transcribe_audio", fake_transcribe)
    job = manager.create(request(clip, captions=True, caption_provider="meta", language="es"))
    manager.process(job["id"])
    assert manager.get(job["id"])["status"] == "planned"
    assert not any("Captions are unavailable" in warning for warning in manager.get(job["id"])["plan"]["warnings"])
    manager.render(job["id"], {"plan": story(segments=[{"start": 0.7, "end": 1.2}, {"start": 0.1, "end": 0.4}])})
    manager.process(job["id"])
    complete = manager.get(job["id"])
    assert complete["status"] == "complete", complete["error"]
    assert abs(seen["duration"] - 0.8) < 0.01 and abs(seen["wav_duration"] - 0.8) < 0.05
    assert (seen["channels"], seen["rate"], seen["language"]) == (1, 16000, "es")
    assert Path(seen["work_dir"]).name == "meta"
    assert complete["result"]["caption_provider"] == "meta"
    report = json.loads(manager.artifact(job["id"], "report").read_text())
    assert report["caption_provider"] == "meta"
    assert "captions_transcribe" not in report["steps"]
    with zipfile.ZipFile(manager.artifact(job["id"], "bundle")) as archive:
        assert {"captions.srt", "captions.ass"} <= set(archive.namelist())
        srt = archive.read("captions.srt")
        assert b"00:00:00,100 --> 00:00:00,700\nMade by hand, the finished bowl." in srt
        assert b"the the" not in srt and b"by by" not in srt


def test_captions_op_burns_a_provided_subtitle_file_without_transcribing(tmp_path: Path, clip: Path) -> None:
    subtitle = tmp_path / "given.srt"
    subtitle.write_text("1\n00:00:00,100 --> 00:00:00,900\nHello\n", encoding="utf-8")
    brief = Brief(name="given", sources={"main": str(clip)}, ops=[{"op": "captions", "file": str(subtitle), "burn": True}, {"op": "export", "preset": "web_preview", "path": str(tmp_path / "out.mp4")}], hw=False, work_dir=str(tmp_path / "work"))
    compiler = compile_brief(brief, work_dir=tmp_path / "work")
    names = [step.name for step in compiler.pipeline.steps]
    assert "captions_transcribe" not in names
    assert any("given.srt" in " ".join(step.argv) for step in compiler.pipeline.steps)
    with pytest.raises(Exception):
        compile_brief(Brief(name="bad", sources={"main": str(clip)}, ops=[{"op": "captions", "file": str(tmp_path / "missing.ass")}, {"op": "export", "preset": "web_preview"}], hw=False, work_dir=str(tmp_path / "w2")), work_dir=tmp_path / "w2")


def test_missing_subject_offers_center_crop_recovery_and_renders_without_tracking(manager: CampaignManager, clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vedit import meta

    monkeypatch.setenv("MODEL_API_KEY", "test-secret-never-write-to-disk")
    monkeypatch.setenv("VEDIT_META_ENABLED", "1")
    calls: list[str] = []

    def fake_track(proxy: Path, *, subject: str, duration: float, work_dir: Path, budget_usd: float):
        calls.append(subject)
        return {"points": [], "object_id": None, "provider": "meta", "usage": {"cost_usd": 0.01}, "warning": "SAM found no matching subject."}

    def fake_plan(proxy: Path, **kwargs):
        return {"title": "Tracked", "summary": "A plan.", "caption": "", "cover_prompt": "", "hashtags": [], "provider": "meta", "usage": {},
                "segments": [{"start": 0.1, "end": 1.1, "label": "Moment", "reason": "Visible."}]}

    monkeypatch.setattr(meta, "track_subject", fake_track)
    monkeypatch.setattr(meta, "plan_story", fake_plan)
    job = manager.create(request(clip, provider="meta", subject="purple unicorn"))
    manager.process(job["id"])
    assert manager.get(job["id"])["status"] == "planned"
    manager.render(job["id"], {})
    manager.process(job["id"])
    failed = manager.get(job["id"])
    assert failed["status"] == "failed"
    assert failed["recovery"] == {"option": "center_crop", "message": "Meta could not find “purple unicorn”. Render this version with a center crop instead, or create a new plan with a clearer subject."}
    assert calls == ["purple unicorn"]
    with pytest.raises(CampaignError, match="tracking must be true or false"):
        manager.render(job["id"], {"tracking": "no"})
    queued = manager.render(job["id"], {"tracking": False})
    assert queued.get("recovery") is None
    assert queued["request"]["tracking_disabled"] is True and queued["request"]["subject"] == "purple unicorn"
    manager.process(job["id"])
    complete = manager.get(job["id"])
    assert complete["status"] == "complete", complete["error"]
    assert complete.get("recovery") is None
    assert complete["result"]["tracked_subject"] is None
    assert "Subject tracking was turned off for this version; a center crop was used." in complete["result"]["warnings"]
    assert calls == ["purple unicorn"]  # the second render never asked Meta again
    report = json.loads(manager.artifact(job["id"], "report").read_text())
    assert report["tracked_subject"] is None
    assert "track_reframe" not in json.dumps(report)
    # Re-enabling tracking is explicit, and the subject was kept on record.
    enabled = manager.render(job["id"], {"tracking": True})
    assert enabled["request"]["tracking_disabled"] is False


def test_meta_plan_carries_local_analysis_and_grounded_transcript(manager: CampaignManager, clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vedit import meta

    monkeypatch.setenv("MODEL_API_KEY", "test-secret-never-write-to-disk")
    monkeypatch.setenv("VEDIT_META_ENABLED", "1")
    heard: list[dict[str, object]] = []

    def fake_transcribe(wav: Path, **kwargs):
        heard.append(kwargs)
        return {"cues": [{"start": 0.1, "end": 1.2, "text": "Made by hand."}], "provider": "meta", "usage": {"cost_usd": 0.0001}}

    monkeypatch.setattr(meta, "transcribe_audio", fake_transcribe)
    monkeypatch.setattr("vedit.story.analyze_source", lambda source, work_dir, **kw: {
        "duration": 1.5, "has_audio": True, "scenes": [0.5], "transcribed": kw.get("heard") is not None,
        "silences": [], "transcript": [{"start": c.start, "end": c.end, "text": c.text} for c in kw.get("heard") or []], "warnings": [],
    })
    seen: dict[str, object] = {}

    def fake_plan_story(proxy, **kwargs):
        seen.update(kwargs)
        return {**story(segments=[{"start": 0.2, "end": 1.2, "label": "Product", "reason": "Visible bowl."}]), "summary": "s", "cover_prompt": "", "provider": "meta", "usage": {"cost_usd": 0.001}, "warnings": []}

    monkeypatch.setattr(meta, "plan_story", fake_plan_story)
    job = manager.create(request(clip, provider="meta"))
    manager.process(job["id"])
    planned = manager.get(job["id"])
    assert planned["status"] == "planned", planned["error"]
    assert seen["transcript"] == [{"start": 0.1, "end": 1.2, "text": "Made by hand."}]
    analysis = planned["plan"]["analysis"]
    assert analysis["method"] == "meta"
    assert analysis["scenes"] == [0.5]
    assert analysis["speech"] and analysis["transcript"][0]["text"] == "Made by hand."
    # Meta Voice Transcribe heard the source once, and the plan says which model wrote its transcript.
    assert len(heard) == 1 and Path(heard[0]["work_dir"]).name == "meta"
    assert analysis["transcribed_by"] == "meta-voice-transcribe"
    assert any("Muse Spark selected the moments" in note for note in analysis["notes"])
    assert any("Meta Voice Transcribe wrote the transcript" in note for note in analysis["notes"])
    segment = planned["plan"]["segments"][0]
    assert segment["kind"] == "speech" and 0.5 <= segment["score"] <= 1.0
    assert planned["plan"]["provider"] == "meta"


def test_review_requires_meta_access_even_in_local_mode(manager: CampaignManager, clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.delenv("VEDIT_META_ENABLED", raising=False)
    with pytest.raises(CampaignError) as refused:
        manager.create(request(clip, review=True))
    assert refused.value.status == 409
    with pytest.raises(CampaignError):
        manager.create(request(clip, review="yes"))


def test_directors_notes_are_attached_to_the_verified_render(manager: CampaignManager, clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vedit import meta

    monkeypatch.setenv("MODEL_API_KEY", "test-secret-never-write-to-disk")
    monkeypatch.setenv("VEDIT_META_ENABLED", "1")
    seen: dict[str, object] = {}

    def fake_review(video: Path, *, goal: str, plan: dict, work_dir: Path, language: str, budget_usd: float, **kwargs):
        info = probe(video)
        seen.update(width=info.width, height=info.height, goal=goal, plan_duration=plan.get("duration"), work_dir=work_dir)
        return {"verdict": "Tight and clear.", "strengths": ["Good opener"], "fixes": [{"what": "Hold the end.", "where_seconds": 0.8}], "caption_suggestion": "A clear look.", "hook_score": 4, "provider": "meta", "usage": {"cost_usd": 0.0004}, "warnings": []}

    monkeypatch.setattr(meta, "review_cut", fake_review)
    job = manager.create(request(clip, review=True))
    manager.process(job["id"])
    manager.render(job["id"], {"plan": story()})
    manager.process(job["id"])
    complete = manager.get(job["id"])
    assert complete["status"] == "complete", complete["error"]
    assert complete["result"]["review"]["hook_score"] == 4
    assert (seen["width"], seen["height"]) == (540, 960), "a vertical video is reviewed vertical"
    assert seen["goal"] == "Show the product clearly" and abs(float(seen["plan_duration"]) - 1.0) < 0.2
    assert Path(seen["work_dir"]).name == "meta"
    report = json.loads(manager.artifact(job["id"], "report").read_text())
    assert report["review"]["verdict"] == "Tight and clear."


def test_failed_review_warns_but_keeps_the_verified_render(manager: CampaignManager, clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vedit import meta

    monkeypatch.setenv("MODEL_API_KEY", "test-secret-never-write-to-disk")
    monkeypatch.setenv("VEDIT_META_ENABLED", "1")

    def failing_review(video: Path, **kwargs):
        raise meta.MetaError("Meta's rate limit was reached. Wait briefly, then retry.")

    monkeypatch.setattr(meta, "review_cut", failing_review)
    job = manager.create(request(clip, review=True))
    manager.process(job["id"])
    manager.render(job["id"], {"plan": story()})
    manager.process(job["id"])
    complete = manager.get(job["id"])
    assert complete["status"] == "complete", complete["error"]
    assert complete["result"]["review"] == {"error": "Meta's rate limit was reached. Wait briefly, then retry.", "director": "muse-spark"}
    assert any(warning.startswith("Director's notes unavailable") for warning in complete["result"]["warnings"])
    assert complete["result"]["proof"]["has_video"] is True


def test_meta_plan_is_snapped_to_transcript_sentences(manager: CampaignManager, clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Muse Spark chose 0.0–2.7 ("...oiled and"); the campaign finishes the sentence where the speech ends before review."""
    from vedit import meta

    monkeypatch.setenv("MODEL_API_KEY", "test-secret-never-write-to-disk")
    monkeypatch.setenv("VEDIT_META_ENABLED", "1")
    # Meta Voice Transcribe answers in turns, several sentences each, with no word times.
    turns = [
        {"start": 0.0, "end": 8.0, "text": "This is the finished oak bowl, oiled and ready for the table. It started as a rough blank from a fallen tree."},
        {"start": 8.0, "end": 16.0, "text": "Here is the raw oak blank, still wet and heavy. The grain tells me where the bowl wants to be."},
        {"start": 32.0, "end": 40.0, "text": "A coat of food safe oil brings out the color. 24 hours later, the bowl is ready to use."},
    ]
    monkeypatch.setattr(meta, "transcribe_audio", lambda wav, **kw: {"cues": turns, "provider": "meta", "usage": {"cost_usd": 0.0001}})
    measured = replace(probe(clip), duration=40.0, file_duration=40.0)
    monkeypatch.setattr("vedit.campaign.probe", lambda path: measured)
    monkeypatch.setattr("vedit.campaign.check_playable", lambda info: None)
    workshop = [
        {"start": 0.0, "end": 2.69, "text": "This is the finished oak bowl, oiled and"},
        {"start": 2.69, "end": 5.0, "text": "ready for the table."},
        {"start": 5.0, "end": 7.44, "text": "It started as a rough blank from a fallen"},
        {"start": 7.44, "end": 8.0, "text": "tree."},
        {"start": 8.0, "end": 11.2, "text": "Here is the raw oak blank, still wet and"},
        {"start": 11.2, "end": 12.0, "text": "heavy."},
        {"start": 12.0, "end": 16.0, "text": "The grain tells me where the bowl wants to be."},
        {"start": 32.0, "end": 35.13, "text": "A coat of food safe oil brings out the"},
        {"start": 35.13, "end": 36.0, "text": "color."},
        {"start": 36.0, "end": 40.0, "text": "24 hours later, the bowl is ready to use."},
    ]
    from vedit.analyze import Range

    heard = [(0.53, 3.72), (4.74, 7.3), (8.56, 11.38), (12.41, 14.78), (32.53, 35.1), (36.13, 39.19)]
    silences = [Range(a, b) for a, b in zip([0.0] + [e for _, e in heard], [s for s, _ in heard] + [40.0]) if b > a]
    def measure(source, work_dir, **kw):
        lines = [{"start": c.start, "end": c.end, "text": c.text} for c in kw["heard"]]
        assert all(len(line["text"]) <= 42 for line in lines), "turns are split into caption-sized lines"
        return {"duration": 40.0, "has_audio": True, "scenes": [8.0, 16.0, 24.0, 32.0], "silences": silences, "transcript": lines, "transcribed": True, "warnings": []}

    monkeypatch.setattr("vedit.story.analyze_source", measure)
    monkeypatch.setattr(CampaignManager, "_analysis_proxy", lambda self, root, source, info, ranges=None, cancel_event=None: clip)

    def fake_plan_story(proxy, **kwargs):
        return {
            "title": "Oak Bowl: Finished to Start", "summary": "s", "caption": "", "cover_prompt": "", "hashtags": [],
            "segments": [
                {"start": 0.0, "end": 2.7, "label": "Finished bowl reveal", "reason": "Rests on speech [0.0–2.7] 'This is the finished oak bowl, oiled and'."},
                {"start": 8.0, "end": 11.2, "label": "Raw oak blank", "reason": "Rests on speech [8.0–11.2]."},
                {"start": 32.0, "end": 35.1, "label": "Oil finish", "reason": "Rests on speech [32.0–35.1]."},
            ],
            "provider": "meta", "usage": {"cost_usd": 0.001}, "warnings": [],
        }

    monkeypatch.setattr(meta, "plan_story", fake_plan_story)
    job = manager.create(request(clip, provider="meta", target_duration=20))
    manager.process(job["id"])
    planned = manager.get(job["id"])
    assert planned["status"] == "planned", planned["error"]
    segments = planned["plan"]["segments"]
    # Sentence ends are clamped to measured speech (+0.15 s), so "oiled and" is finished at 3.97 s, not at the next sentence's first phoneme.
    assert [(s["start"], s["end"]) for s in segments] == [(0.0, 3.97), (8.0, 11.63), (32.0, 35.35)]
    assert segments[0]["reason"].endswith("end moved to sentence end at 4.0 s.")
    assert segments[0]["label"] == "Finished bowl reveal" and planned["plan"]["duration"] == pytest.approx(10.95, abs=0.01)
    analysis = planned["plan"]["analysis"]
    assert analysis["sentences"][0] == {"start": 0.28, "end": 3.97, "text": "This is the finished oak bowl, oiled and ready for the table."}
    assert len(analysis["sentences"]) == 6
    # The validator keeps the sentence list bounded and display-only.
    checked = validate_plan(planned["plan"], 40.0, original={"analysis": {"sentences": [{"start": 0, "end": 5, "text": "x" * 601}, {"start": 1, "end": 2, "text": "ok."}, "junk"]}})
    assert checked["analysis"] is None  # an oversize sentence invalidates the untrusted block as a whole
    checked = validate_plan(planned["plan"], 40.0, original={"analysis": {"sentences": [{"start": 1, "end": 2, "text": "ok."}, "junk"]}})
    assert checked["analysis"]["sentences"] == [{"start": 1.0, "end": 2.0, "text": "ok."}]


def test_brief_edits_drive_the_render_and_the_receipts(manager: CampaignManager, clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    goal = "Show the finished bowl. Trim the pauses, add a title card, fade out at the end, cta: Order yours"
    job = manager.create(request(clip, goal=goal))
    manager.process(job["id"])
    planned = manager.get(job["id"])
    assert planned["status"] == "planned", planned["error"]
    # Decisions are visible before any render, derived from the brief alone.
    # A steady tone has no pauses, and the receipt does not pretend it found any.
    assert planned["plan"]["decisions"][0] == "Found no pauses longer than 0.6 s inside the selected moments"
    assert planned["plan"]["decisions"][1].startswith("Title card ‘")
    assert planned["plan"]["decisions"][2].startswith("Call to action ‘Order yours’")
    assert planned["plan"]["decisions"][3] == "Fade out 0.5 s"
    assert planned["plan"]["ignored_edits"] == []
    # The brief lands in the edit, the same settings Studio changes.
    edit = planned["plan"]["edit"]
    assert edit["trim_pauses"] is True and edit["fade"]["out"] == 0.5
    assert [item["style"] for item in edit["texts"]] == ["title", "callout"]
    manager.render(job["id"], {"plan": story(title="The oak bowl")})
    manager.process(job["id"])
    complete = manager.get(job["id"])
    assert complete["status"] == "complete", complete["error"]
    brief = manager.artifact(job["id"], "brief").read_text()
    text_op = "text" if "text" in OPS else "title"
    for op in ("select_ranges", text_op, "fade_out", "loudnorm"):
        assert f"op: {op}" in brief, op
    assert "The oak bowl" in brief and "op: cut_silence" not in brief
    assert brief.index("op: select_ranges") < brief.index(f"op: {text_op}") < brief.index("op: fade_out") < brief.index("op: loudnorm")
    work = manager.root / job["id"] / "render-1"
    project = json.loads((work / "project.json").read_text())
    names = [step["name"] for step in project["steps"]]
    export = next(step for step in project["steps"] if step["name"].startswith("export_"))
    assert names.index("select_ranges") < names.index(export["name"]) and "cut_silence" not in names
    commands = " ".join(arg for step in project["steps"] for arg in step["argv"])
    assert ("texts.ass" in commands or "drawtext=" in commands) and "--fade-out 0.5" in " ".join(export["argv"])
    assert not (work / "keep_ranges.json").exists()
    report = json.loads(manager.artifact(job["id"], "report").read_text())
    assert report["approved_plan"]["decisions"][1] == "Title card ‘The oak bowl’ for 2.5 s"
    assert report["edit"]["trim_pauses"] is True and report["edit"]["texts"][0]["text"] == "The oak bowl"
    assert report["timeline"]["duration"] == report["approved_plan"]["duration"] and report["timeline"]["spans"]
    check = next(item for item in report["checks"] if item["name"] == "auto: requested edits applied")
    assert check["passed"] is True and check["at"] is None
    assert "Applied: Found no pauses" in check["note"] and "Call to action" in check["note"]
    assert complete["result"]["proof"]["has_video"] and complete["result"]["proof"]["has_audio"]
    assert abs(probe(manager.artifact(job["id"], "video")).duration - 1.0) < 0.3


def test_brief_can_turn_loudness_normalization_off(manager: CampaignManager, clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    job = manager.create(request(clip, goal="Show the product clearly. Do not normalize the audio."))
    manager.process(job["id"])
    assert manager.get(job["id"])["plan"]["decisions"] == ["Kept the original loudness (no normalization)"]
    manager.render(job["id"], {"plan": story()})
    manager.process(job["id"])
    assert manager.get(job["id"])["status"] == "complete", manager.get(job["id"])["error"]
    assert "op: loudnorm" not in manager.artifact(job["id"], "brief").read_text()
    report = json.loads(manager.artifact(job["id"], "report").read_text())
    check = next(item for item in report["checks"] if item["name"] == "auto: requested edits applied")
    assert check["passed"] is True and check["note"] == "Applied: Kept the original loudness (no normalization)."
    plain = manager.create(request(clip))
    manager.process(plain["id"])
    assert manager.get(plain["id"])["plan"]["decisions"] == [] and manager.get(plain["id"])["plan"]["ignored_edits"] == []


def test_validate_plan_bounds_the_decision_sentences() -> None:
    plan = validate_plan(story(decisions=["x" * 400] * 30 + [7, ""], ignored_edits="not a list"), 1.5)
    assert len(plan["decisions"]) == 12
    assert all(len(item) <= 200 for item in plan["decisions"])
    assert plan["ignored_edits"] == []
    assert validate_plan(story(), 1.5)["decisions"] == []
    kept = validate_plan(story(decisions=["Fade out 0.5 s"], ignored_edits=["Watermark: upload a logo"]), 1.5)
    assert kept["decisions"] == ["Fade out 0.5 s"] and kept["ignored_edits"] == ["Watermark: upload a logo"]


def test_meta_captions_follow_the_trimmed_clock(manager: CampaignManager, clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vedit import meta

    monkeypatch.setenv("MODEL_API_KEY", "test-secret-never-write-to-disk")
    monkeypatch.setenv("VEDIT_META_ENABLED", "1")
    monkeypatch.setattr(meta, "transcribe_audio", lambda wav, **kw: {"cues": [{"start": 0.1, "end": 0.7, "text": "Made by hand."}], "provider": "meta", "usage": {"cost_usd": 0.0001}})
    job = manager.create(request(clip, captions=True, caption_provider="meta", goal="Trim the pauses and fade out"))
    manager.process(job["id"])
    manager.render(job["id"], {"plan": story()})
    manager.process(job["id"])
    complete = manager.get(job["id"])
    assert complete["status"] == "complete", complete["error"]
    work = manager.root / job["id"] / "render-1"
    # A steady tone has no pauses, so the cues keep their times.
    assert "00:00:00,100 --> 00:00:00,700" in (work / "captions.srt").read_text()
    names = [step["name"] for step in json.loads((work / "project.json").read_text())["steps"]]
    assert "captions_transcribe" not in names and "cut_silence" not in names and "select_ranges" in names


def test_meta_captions_warn_when_an_edit_key_does_not_match_the_remote_transcript(manager: CampaignManager, clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vedit import meta

    monkeypatch.setenv("MODEL_API_KEY", "test-secret-never-write-to-disk")
    monkeypatch.setenv("VEDIT_META_ENABLED", "1")
    monkeypatch.setattr(meta, "transcribe_audio", lambda wav, **kw: {"cues": [{"start": 0.1, "end": 0.7, "text": "Made by hand."}], "provider": "meta", "usage": {"cost_usd": 0.0001}})
    job = manager.create(request(clip, captions=True, caption_provider="meta"))
    manager.process(job["id"])
    plan = story(edit={"captions": {"edits": {"1200": "My edited line"}}})
    manager.render(job["id"], {"plan": plan})
    manager.process(job["id"])
    complete = manager.get(job["id"])
    assert complete["status"] == "complete", complete["error"]
    assert any("1 caption edit(s) did not match Meta's transcript" in warning for warning in complete["result"]["warnings"])
    assert "My edited line" not in (manager.root / job["id"] / "render-1" / "captions.srt").read_text()


def test_meta_analysis_proxy_preflights_long_media_before_encoding(manager: CampaignManager, clip: Path, tmp_path: Path) -> None:
    from types import SimpleNamespace

    info = SimpleNamespace(duration=601.0, display_height=720, square_width=1280, width=1280)
    root = manager.root / "preflight"
    with pytest.raises(CampaignError, match="10 minutes"):
        manager._analysis_proxy(root, clip, info)
    assert not (root / "analysis").exists()


def test_ts_source_preview_is_normalized_to_browser_mp4(manager: CampaignManager, clip: Path) -> None:
    spec = importlib.util.spec_from_file_location("campaign_real_footage_helpers", Path(__file__).with_name("test_real_footage.py"))
    assert spec and spec.loader
    helpers = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = helpers
    spec.loader.exec_module(helpers)
    transport = clip.with_suffix(".ts")
    helpers.mpeg_ts(transport)
    job = manager.create(request(transport))
    manager.process(job["id"])
    source_preview = manager.artifact(job["id"], "source")
    assert source_preview.suffix == ".mp4"
    assert probe(source_preview).has_video


def test_render_can_change_captions_and_frame_without_a_new_story(manager: CampaignManager, clip: Path) -> None:
    job = manager.create(request(clip, aspect="9:16"))
    manager.process(job["id"])
    planned = manager.get(job["id"])
    with pytest.raises(CampaignError):
        manager.render(job["id"], {"plan": planned["plan"], "aspect": "4:3"})
    with pytest.raises(CampaignError):
        manager.render(job["id"], {"plan": planned["plan"], "captions": "yes"})
    queued = manager.render(job["id"], {"plan": planned["plan"], "captions": True, "aspect": "1:1"})
    assert queued["request"]["captions"] is True
    assert queued["request"]["aspect"] == "1:1"
    assert queued["plan"]["segments"] == planned["plan"]["segments"]
