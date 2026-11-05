"""The plan carries the edit: validation, one clock, and the draft timeline route."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from vedit.campaign import CampaignError, CampaignManager, capabilities, validate_plan
from vedit.edit_spec import TRANSITIONS, default_edit
from vedit.media import probe, resolve_ffmpeg
from vedit.render_plan import assembly_op
from vedit.timeline import plan_timeline

SPEECH = [{"start": 0.0, "end": 2.0}, {"start": 6.0, "end": 8.0}]


def story(*ranges: tuple[float, float], **overrides) -> dict:
    segments = [{"start": start, "end": end} for start, end in ranges or [(0.0, 8.0)]]
    return {"title": "Bowl", "segments": segments, **overrides}


@pytest.fixture
def clip(tmp_path: Path) -> Path:
    source = tmp_path / "vedit-sessions" / "source-test" / "inbox" / "clip.mp4"
    source.parent.mkdir(parents=True)
    spec = importlib.util.spec_from_file_location("plan_edit_helpers", Path(__file__).with_name("test_e2e.py"))
    assert spec and spec.loader
    helpers = importlib.util.module_from_spec(spec)
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


def request(clip: Path, **overrides) -> dict:
    return {
        "paths": [str(clip)],
        "goal": "Show the product clearly",
        "target_duration": 1,
        "captions": False,
        **overrides,
    }


def test_a_plan_always_holds_a_complete_edit_and_its_timeline() -> None:
    plan = validate_plan(story((1.0, 3.0), (4.0, 5.0)), 10)
    assert plan["edit"] == default_edit()
    assert plan["duration"] == 3.0
    assert plan["timeline"]["duration"] == 3.0 and plan["timeline"]["transition_seconds"] == 0
    assert plan["timeline"]["spans"][1] == {
        "segment": 2,
        "source_start": 4.0,
        "source_end": 5.0,
        "output_start": 2.0,
        "output_end": 3.0,
        "speed": 1.0,
    }
    json.dumps(plan, allow_nan=False)


def test_duration_comes_from_the_clock_not_the_segment_lengths() -> None:
    original = {"analysis": {"speech": SPEECH}}
    plan = validate_plan(story(edit={"trim_pauses": True}), 10, original=original)
    assert plan["duration"] < 8 and plan["segments"][0].get("keep") is None
    assert plan["duration"] == plan_timeline(plan)["duration"] == plan["timeline"]["duration"]
    assert len(plan["timeline"]["spans"]) == 2
    fast = validate_plan(story((0.0, 8.0), (9.0, 13.0), edit={"transition": {"type": "fade", "seconds": 0.5}}), 20)
    assert fast["duration"] == 11.5 and fast["timeline"]["transition_seconds"] == 0.5


def test_per_segment_fields_are_bounded() -> None:
    motion = {"from": {"scale": 1, "x": 0.5, "y": 0.5}, "to": {"scale": 1.4, "x": 0.25, "y": 0.75}}
    plan = validate_plan(story(segments=[{"start": 0, "end": 4, "speed": 2, "volume": 0.5, "motion": motion}]), 10)
    assert plan["segments"][0]["speed"] == 2 and plan["segments"][0]["volume"] == 0.5
    assert plan["segments"][0]["motion"]["to"] == {"scale": 1.4, "x": 0.25, "y": 0.75}
    assert plan["duration"] == 2.0
    for bad in (
        {"speed": 0.1},
        {"speed": 5},
        {"speed": "fast"},
        {"speed": float("nan")},
        {"volume": -1},
        {"volume": 3},
        {"motion": "zoom"},
        {"motion": {"from": {}, "to": {"scale": 4}}},
        {"motion": {"from": {"x": 2}, "to": {}}},
        {"motion": {"to": {}}},
    ):
        with pytest.raises(CampaignError):
            validate_plan(story(segments=[{"start": 0, "end": 4, **bad}]), 10)


def test_client_keep_and_timeline_are_never_read() -> None:
    plan = validate_plan(story(segments=[{"start": 0, "end": 4, "keep": [[0, 0.5]]}], timeline={"duration": 1}), 10)
    assert "keep" not in plan["segments"][0] and plan["duration"] == 4


def test_the_segment_limit_is_thirty_two() -> None:
    segments = [(index * 2.0, index * 2.0 + 1.0) for index in range(33)]
    assert len(validate_plan(story(*segments[:32]), 100)["segments"]) == 32
    with pytest.raises(CampaignError, match="32"):
        validate_plan(story(*segments), 100)
    assert capabilities()["limits"]["segments"] == 32


def test_the_story_limit_reads_the_finished_length() -> None:
    assert validate_plan(story(segments=[{"start": 0, "end": 300, "speed": 2}]), 400)["duration"] == 150
    with pytest.raises(CampaignError, match="180"):
        validate_plan(story(segments=[{"start": 0, "end": 100, "speed": 0.25}]), 400)


@pytest.mark.parametrize(
    "edit",
    [
        {"look": {"preset": "sparkle"}},
        {"transition": {"type": "wipe;rm"}},
        {"fade": {"out": 99}},
        {"texts": "words"},
        "not an object",
        {"captions": {"color": "red"}},
    ],
)
def test_a_bad_edit_is_a_plain_400(edit) -> None:
    with pytest.raises(CampaignError) as caught:
        validate_plan(story(edit=edit), 10)
    assert caught.value.status == 400


def test_an_omitted_edit_keeps_the_saved_one() -> None:
    saved = validate_plan(story(edit={"look": {"preset": "warm"}}), 10)
    assert validate_plan(story(), 10, original=saved)["edit"]["look"]["preset"] == "warm"
    assert validate_plan(story(edit={}), 10, original=saved)["edit"]["look"]["preset"] == "none"


def test_assets_must_belong_to_the_project() -> None:
    edit = {"music": {"asset": "a" * 8}}
    assert validate_plan(story(edit=edit), 10)["edit"]["music"]["asset"] == "a" * 8  # shape only
    assert validate_plan(story(edit=edit), 10, assets={"a" * 8: {"kind": "audio"}})["edit"]["music"]["asset"] == "a" * 8
    with pytest.raises(CampaignError, match="not an asset"):
        validate_plan(story(edit=edit), 10, assets={})
    with pytest.raises(CampaignError, match="audio"):
        validate_plan(story(edit=edit), 10, assets={"a" * 8: {"kind": "image"}})


def test_timed_items_stay_inside_a_shorter_story() -> None:
    edit = {
        "texts": [{"text": "Early", "start": 1, "duration": 2}, {"text": "Late", "start": 20, "duration": 3}],
        "overlays": [{"asset": "a" * 8, "start": 30, "duration": 40}],
    }
    plan = validate_plan(story((0.0, 5.0), edit=edit), 10)
    assert plan["edit"]["texts"][0] == {**plan["edit"]["texts"][0], "start": 1, "duration": 2}
    assert plan["edit"]["texts"][1]["start"] == 2.0 and plan["edit"]["texts"][1]["duration"] == 3
    overlay = plan["edit"]["overlays"][0]
    assert overlay["start"] == 0 and overlay["duration"] == 5


def test_a_title_card_follows_the_story_title_until_it_is_rewritten() -> None:
    card = {"text": "Old name", "start": 0, "duration": 2, "style": "title"}
    saved = validate_plan(story(title="Old name", edit={"texts": [card]}), 10)
    renamed = validate_plan(dict(saved, title="New name"), 10, original=saved)
    assert renamed["edit"]["texts"][0]["text"] == "New name"
    rewritten = validate_plan(
        dict(saved, title="New name", edit=dict(saved["edit"], texts=[dict(card, text="My own words")])),
        10,
        original=saved,
    )
    assert rewritten["edit"]["texts"][0]["text"] == "My own words"


def test_timeline_captions_are_mapped_onto_the_output_clock() -> None:
    sentences = [
        {"start": 0.5, "end": 1.5, "text": "First thought."},
        {"start": 6.5, "end": 7.5, "text": "Second thought."},
    ]
    plan = validate_plan(
        story((0.0, 8.0), edit={"removed": [[2.0, 6.0]]}),
        10,
        original={"analysis": {"sentences": sentences, "speech": SPEECH}},
    )
    assert [(cue["start"], cue["text"]) for cue in plan["timeline"]["captions"]] == [
        (0.5, "First thought."),
        (2.5, "Second thought."),
    ]
    assert plan["timeline"]["captions"][1]["source_start"] == 6.5  # what an edit of that line is stored under


def test_capabilities_offer_the_editor_with_what_this_machine_can_run() -> None:
    editor = capabilities()["editor"]
    assert [row["id"] for row in editor["transitions"]] == [row[0] for row in TRANSITIONS]
    assert all(isinstance(row["available"], bool) for row in editor["transitions"])
    assert editor["defaults"] == default_edit()
    assert set(editor["features"]) >= {
        "transitions",
        "text",
        "captions",
        "overlays",
        "cutout",
        "zoom",
        "stabilize",
        "look",
        "voice",
        "music",
    }
    assert all(
        isinstance(row["available"], bool) for rows in ("zoom_modes", "cutout_modes", "looks") for row in editor[rows]
    )
    assert next(row for row in editor["looks"] if row["id"] == "none")["available"] is True


def test_a_machine_without_a_filter_is_told_so(monkeypatch) -> None:
    monkeypatch.setattr("vedit.campaign.has_filter", lambda name: name not in {"xfade", "perspective"})
    editor = capabilities()["editor"]
    assert (
        editor["features"]["transitions"] is False
        and editor["features"]["zoom"] is False
        and editor["features"]["text"] is True
    )
    assert {row["id"]: row["available"] for row in editor["transitions"]}["none"] is True
    assert {row["id"]: row["available"] for row in editor["transitions"]}["fade"] is False


def test_request_takes_a_template_and_a_partial_edit_over_the_brief(manager, clip) -> None:
    job = manager.create(
        request(
            clip,
            goal="Fade out at the end",
            template="cinematic",
            edit={"look": {"intensity": 0.5}, "zoom": {"mode": "none"}},
        )
    )
    manager.process(job["id"])
    edit = manager.get(job["id"])["plan"]["edit"]
    assert edit["template"] == "cinematic"
    assert edit["look"]["preset"] == "cinematic" and edit["look"]["intensity"] == 0.5
    assert edit["fade"] == {"in": 0.5, "out": 0.8}  # the template merges over the brief
    assert edit["zoom"]["mode"] == "none"  # the request has the last word
    assert edit["transition"]["type"] == "fadeblack"


def test_request_rejects_an_unknown_template_or_bad_edit(manager, clip) -> None:
    for bad in (
        {"template": "sparkle"},
        {"template": 3},
        {"edit": "warm"},
        {"edit": {"look": {"preset": "sparkle"}}},
        {"edit": {"fade": {"in": float("nan")}}},
    ):
        with pytest.raises(CampaignError):
            manager.create(request(clip, **bad))


def test_request_drops_unknown_edit_keys(manager, clip) -> None:
    job = manager.create(
        request(clip, edit={"look": {"preset": "warm", "shell": "rm"}, "command": "rm", "weird": float("nan")})
    )
    assert job["request"]["edit"] == {"look": {"preset": "warm"}}


def test_the_timeline_route_resolves_a_draft_without_saving(manager, clip, monkeypatch) -> None:
    job = manager.create(request(clip))
    with pytest.raises(CampaignError) as caught:
        manager.timeline(job["id"], {})
    assert caught.value.status == 409
    manager.process(job["id"])
    planned = manager.get(job["id"])

    def no_process(*args, **kwargs):
        raise AssertionError("the draft route must not start a process")

    monkeypatch.setattr("vedit.campaign.probe", no_process)
    monkeypatch.setattr("subprocess.run", no_process)
    draft = dict(
        planned["plan"],
        segments=[{"start": 0.1, "end": 1.1, "speed": 2}],
        edit=dict(planned["plan"]["edit"], removed=[[0.3, 0.5]]),
    )
    timeline = manager.timeline(job["id"], {"plan": draft})
    assert set(timeline) == {"spans", "duration", "transition_seconds", "captions"}
    assert timeline["duration"] == pytest.approx(0.4) and len(timeline["spans"]) == 2
    assert manager.get(job["id"])["plan"] == planned["plan"]
    assert manager.timeline(job["id"], {})["duration"] == planned["plan"]["duration"]
    with pytest.raises(CampaignError):
        manager.timeline(job["id"], {"plan": dict(draft, edit={"look": {"preset": "sparkle"}})})
    with pytest.raises(CampaignError):
        manager.timeline(job["id"], ["not", "an", "object"])
    with pytest.raises(CampaignError) as caught:
        manager.timeline("0" * 32, {})
    assert caught.value.status == 404


@pytest.mark.parametrize(
    "choice, suffix, size, rate, sound",
    [
        ({"quality": "720p", "fps": 24, "format": "webm"}, ".webm", (1280, 720), 24, True),
        ({"quality": "720p", "format": "mov"}, ".mov", (1280, 720), 30, True),
        ({"format": "gif"}, ".gif", (960, 540), 15, False),
    ],
)
def test_the_chosen_export_is_what_is_delivered_and_verified(
    manager, clip, monkeypatch, choice, suffix, size, rate, sound
) -> None:
    job = manager.create(request(clip, aspect="16:9", edit={"export": choice}))
    manager.process(job["id"])
    manager.render(job["id"], {})
    manager.process(job["id"])
    done = manager.get(job["id"])
    assert done["status"] == "complete", done["error"]
    video = manager.artifact(job["id"], "video")
    info = probe(video)
    assert video.suffix == suffix and (info.width, info.height) == size and info.has_audio is sound
    # A GIF stores delays in hundredths of a second, so its rate is only near the choice.
    assert info.fps == pytest.approx(rate, abs=2 if suffix == ".gif" else 0.1)
    with zipfile.ZipFile(manager.artifact(job["id"], "bundle")) as archive:
        assert f"video{suffix}" in archive.namelist()


def test_the_tracking_proxy_runs_on_the_finished_clock(manager, clip, tmp_path) -> None:
    plan = {
        "segments": [{"start": 0.1, "end": 0.9, "speed": 2.0}, {"start": 1.0, "end": 1.4}],
        "edit": {"removed": [[0.3, 0.5]], "transition": {"type": "fade", "seconds": 0.1}},
    }
    view = plan_timeline(plan)
    proxy = manager._analysis_proxy(tmp_path / "project", clip, probe(clip), assembly_op(view, plan["edit"]))
    assert probe(proxy).duration == pytest.approx(view["duration"], abs=0.07)


def test_every_setting_renders_together_on_one_clock(manager, clip, monkeypatch) -> None:
    """The whole editor at once: if two stages disagree about the clock, the probe shows it."""
    inbox = clip.parent
    ffmpeg = resolve_ffmpeg()
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=220:sample_rate=48000:duration=4",
            str(inbox / "bed.wav"),
        ],
        check=True,
    )
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=orange:s=120x60:d=0.04",
            "-frames:v",
            "1",
            str(inbox / "logo.png"),
        ],
        check=True,
    )
    job = manager.create(request(clip))
    manager.process(job["id"])
    bed = manager.add_asset(job["id"], {"path": str(inbox / "bed.wav")})
    logo = manager.add_asset(job["id"], {"path": str(inbox / "logo.png")})
    plan = manager.get(job["id"])["plan"]
    edit = dict(
        plan["edit"],
        look={**plan["edit"]["look"], "preset": "warm", "vignette": 0.3},
        transition={"type": "fade", "seconds": 0.15},
        zoom={"mode": "slow", "strength": 0.5},
        stabilize=True,
        denoise_picture=True,
        audio={**plan["edit"]["audio"], "enhance_voice": True, "denoise": True},
        texts=[{"text": "Made by hand", "start": 0, "duration": 0.5, "style": "title", "position": "center"}],
        logo={"asset": logo["id"]},
        overlays=[{"asset": logo["id"], "start": 0.1, "duration": 0.3, "position": "center"}],
        music={"asset": bed["id"], "volume_db": -20},
        fade={"in": 0.1, "out": 0.1},
        removed=[[0.5, 0.65]],
    )
    draft = dict(
        plan, edit=edit, segments=[{"start": 0.0, "end": 0.9, "speed": 1.25}, {"start": 1.0, "end": 1.4, "volume": 0.8}]
    )
    manager.render(job["id"], {"plan": draft})
    manager.process(job["id"])
    done = manager.get(job["id"])
    assert done["status"] == "complete", done["error"]
    output, approved = probe(manager.artifact(job["id"], "video")), done["plan"]
    assert output.has_video and output.has_audio
    assert output.duration == pytest.approx(approved["timeline"]["duration"], abs=0.07)
    assert approved["timeline"]["duration"] == approved["duration"] < 1.2
    brief = manager.artifact(job["id"], "brief").read_text()
    order = [
        "select_ranges",
        "stabilize",
        "denoise",
        "zoom",
        "look",
        "voice",
        "text",
        "logo",
        "overlay",
        "fade_in",
        "fade_out",
        "music",
        "loudnorm",
        "export",
    ]
    positions = [brief.index(f"op: {name}\n") for name in order]
    assert positions == sorted(positions)
    assert bed["id"] in brief and logo["id"] in brief


def test_receipts_report_a_request_changed_in_the_editor(manager, clip, monkeypatch) -> None:
    job = manager.create(request(clip, goal="Fade out at the end"))
    manager.process(job["id"])
    planned = manager.get(job["id"])["plan"]
    assert planned["decisions"] == ["Fade out 0.5 s"]
    manager.render(job["id"], {"plan": dict(planned, edit=dict(planned["edit"], fade={"in": 0, "out": 0}))})
    manager.process(job["id"])
    done = manager.get(job["id"])
    assert done["status"] == "complete", done["error"]
    approved = json.loads(manager.artifact(job["id"], "report").read_text())["approved_plan"]
    assert approved["decisions"] == [] and approved["ignored_edits"] == ["Fade out: changed in the editor"]
