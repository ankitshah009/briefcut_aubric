"""Curated footage effects through the campaign approval and export boundary."""

from __future__ import annotations

import importlib.util
import json
import math
import zipfile
from pathlib import Path

import pytest

from vedit.campaign import CampaignError, CampaignManager, capabilities


@pytest.fixture
def project(tmp_path: Path):
    clip = tmp_path / "vedit-sessions" / "source-effects" / "inbox" / "clip.mp4"
    clip.parent.mkdir(parents=True)
    spec = importlib.util.spec_from_file_location("campaign_effect_media", Path(__file__).with_name("test_e2e.py"))
    assert spec and spec.loader
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    if not helper._have_ffmpeg():
        pytest.skip("FFmpeg required")
    helper._make_clip(clip, seconds=1.2, color="navy")
    manager = CampaignManager(tmp_path, start_worker=False)
    try:
        yield manager, {"paths": [str(clip)], "goal": "Present this product", "captions": False, "target_duration": 1}
    finally:
        manager.close()


def test_effect_catalogue_is_available_without_meta_access(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    result = capabilities()
    assert result["effects"]["supported"] is True
    assert {item["id"] for item in result["effects"]["presets"]} == {"tilt", "float", "push"}


def test_missing_filter_disables_effects_but_preserves_standard_edits(project, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, request = project
    monkeypatch.setattr("vedit.campaign.has_filter", lambda name: name != "perspective")
    support = capabilities()["effects"]
    assert support["supported"] is False
    assert support["missing_filters"] == ["perspective"]
    assert "ffmpeg-full" in support["reason"]
    assert manager.create(request)["status"] == "queued"
    with pytest.raises(CampaignError, match="perspective"):
        manager.create({**request, "effect": {"preset": "tilt"}})


@pytest.mark.parametrize(
    "effect",
    [
        "tilt",
        {"preset": "unknown"},
        {"preset": "push", "intensity": 2},
        {"preset": "float", "start": -1},
        {"preset": "tilt", "duration": 0},
        {"preset": "push", "intensity": math.nan},
    ],
)
def test_effect_values_are_validated_at_job_creation(project, effect) -> None:
    manager, request = project
    with pytest.raises(CampaignError):
        manager.create({**request, "effect": effect})
    assert manager.list() == []


def test_effect_timing_is_checked_against_approved_story_and_omitted_duration_fits(project) -> None:
    manager, request = project
    job = manager.create({**request, "effect": {"preset": "tilt"}})
    manager.process(job["id"])
    with pytest.raises(CampaignError):
        manager.render(job["id"], {"effect": {"preset": "tilt", "start": 0.8, "duration": 0.5}})
    assert manager.get(job["id"])["status"] == "planned"
    approved = manager.render(job["id"], {})
    assert approved["plan"]["effect"]["duration"] <= 1
    assert approved["request"]["effect"] == approved["plan"]["effect"]


def test_effect_override_renders_locally_and_is_preserved_in_download_package(project) -> None:
    manager, request = project
    job = manager.create(request)
    manager.process(job["id"])
    recipe = {"preset": "push", "intensity": 0.35, "start": 0.1, "duration": 0.6}
    approved = manager.render(job["id"], {"effect": recipe})
    assert approved["id"] == job["id"]
    manager.process(job["id"])
    complete = manager.get(job["id"])
    assert complete["status"] == "complete", complete.get("error")
    assert complete["result"]["effect"] == recipe
    assert complete["result"]["proof"]["has_video"] is True
    assert complete["result"]["proof"]["has_audio"] is True
    assert complete["result"]["proof"]["color_space"] == "bt709"
    report = json.loads(manager.artifact(job["id"], "report").read_text())
    assert report["effect"] == recipe
    assert report["approved_plan"]["effect"] == recipe
    with zipfile.ZipFile(manager.artifact(job["id"], "bundle")) as bundle:
        assert json.loads(bundle.read("story.json"))["effect"] == recipe
        assert json.loads(bundle.read("report.json"))["effect"] == recipe
        brief = bundle.read("brief.yaml").decode()
        assert brief.index("select_ranges") < brief.index("spatial_effect") < brief.index("export")
    assert complete["budget"]["reserved_usd"] == 0
