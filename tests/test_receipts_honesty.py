"""Receipts state only what happened.

Regression tests for the scenario matrix (docs/evidence/scenario-matrix/README.md):
moments that crossed clip edges, an auto fix that dropped the brief's ending, a
length check that passed far-off cuts, and notes that claimed a transcript,
a caption check, or a target that did not exist.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from vedit import doctor, settings
from vedit.campaign import CampaignManager, validate_plan
from vedit.lint import apply_patches, brief_bookends, lint_plan, snap_to_clips
from vedit.media import resolve_ffmpeg

ROOT = Path(__file__).resolve().parents[1]
# Run A4: wanrong-walk, flower, then a photo, joined into one project file.
A4_CLIPS = [
    {"name": "wanrong-walk.mp4", "kind": "video", "start": 0.0, "end": 15.633},
    {"name": "flower.mp4", "kind": "video", "start": 15.633, "end": 20.633},
    {"name": "000024.jpg", "kind": "photo", "start": 20.633, "end": 23.633},
]
A4_GOAL = "A 15-second widescreen montage: the archival street scene, then the flower opening, and end on the photo of the colonnade. Add a title card."
CLIP_CHECK = "auto: each moment stays inside one clip"
ENDING_CHECK = "auto: the cut ends where the brief asked"
CAPTIONS = "auto: captions cover the speech"
SENTENCES = "auto: cuts land on sentence boundaries"
NOTE = "auto: note: the cut is well under the target"


def names(result: dict[str, Any]) -> list[str]:
    return [check["name"] for check in result["checks"]]


def named(result: dict[str, Any], name: str) -> dict[str, Any]:
    return next(check for check in result["checks"] if check["name"] == name or check["name"].startswith(name + " ("))


def moments(*ranges: tuple[float, float], labels: tuple[str, ...] = (), **extra: Any) -> dict[str, Any]:
    segments = [
        {"start": s, "end": e, "label": labels[i] if i < len(labels) else f"Moment {i + 1}", "reason": "Chosen."}
        for i, (s, e) in enumerate(ranges)
    ]
    return {"title": "Story", "segments": segments, **extra}


def ffmpeg(*args: str) -> None:
    subprocess.run([resolve_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def inbox(tmp_path: Path, name: str) -> Path:
    path = tmp_path / "vedit-sessions" / "source-honesty" / "inbox" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def video(path: Path, *, seconds: float, color: str, audio: str | None = "sine=f=440") -> Path:
    """A test clip. ``audio`` is a lavfi source: a tone, ``anullsrc`` for a silent track, or None for no track."""
    args = ["-f", "lavfi", "-i", f"color={color}:s=320x180:r=30:d={seconds}"]
    if audio:
        args += [
            "-f",
            "lavfi",
            "-t",
            str(seconds),
            "-i",
            audio + (":r=48000" if audio.startswith("sine") else "=r=48000:cl=stereo"),
        ]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if audio:
        args += ["-c:a", "aac", "-shortest"]
    ffmpeg(*args, str(path))
    return path


def photo(path: Path) -> Path:
    ffmpeg("-f", "lavfi", "-i", "color=red:s=320x180", "-frames:v", "1", "-update", "1", str(path))
    return path


@pytest.fixture
def manager(tmp_path: Path):
    instance = CampaignManager(tmp_path, start_worker=False)
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def meta_on(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Meta enabled with a fake key; every Voice Transcribe call is recorded and hears nothing."""
    from vedit import meta

    monkeypatch.setenv("MODEL_API_KEY", "test-secret-never-write-to-disk")
    monkeypatch.setenv("VEDIT_META_ENABLED", "1")
    sent: list[str] = []
    monkeypatch.setattr(
        meta,
        "transcribe_audio",
        lambda wav, **kw: sent.append(str(wav)) or {"cues": [], "provider": "meta", "usage": {"cost_usd": 0.0001}},
    )
    return sent


# --- 1. Moments stay inside one clip ---


def test_a_moment_that_strays_into_a_neighbouring_clip_is_trimmed_to_its_own() -> None:
    # Spark's A4 plan: "Flower opening" opened with 0.43 s of archival film; the photo with 0.13 s of the flower.
    segments = moments((0.0, 5.5), (15.2, 20.2), (20.5, 23.633))["segments"]
    snapped = snap_to_clips(segments, A4_CLIPS)
    assert [(item["start"], item["end"]) for item in snapped] == [(0.0, 5.5), (15.633, 20.2), (20.633, 23.633)]
    assert "start moved 0.43 s to 15.63 s" in snapped[1]["reason"]
    # Further than a second over the edge is not a nudge: the planner leaves it and the lint check fails.
    far = moments((13.0, 19.0))["segments"]
    assert snap_to_clips(far, A4_CLIPS) == far
    # One clip has no edges to cross.
    assert snap_to_clips(segments, A4_CLIPS[:1]) == segments


def test_the_clip_check_fails_and_proposes_the_trim_when_a_moment_still_crosses() -> None:
    plan = moments((0.0, 5.5), (13.0, 19.0))
    result = lint_plan(plan, target_duration=15, duration=23.633, clips=A4_CLIPS)
    check = named(result, CLIP_CHECK)
    assert check["passed"] is False and check["basis"] == "derived" and check["by"] == "lint"
    assert check["note"] == "Moment 2 plays clip 2 with 2.63 s of clip 1."
    assert [(p["op"], p["segment"], p["seconds"]) for p in result["patches"]] == [("trim_start", 2, 15.633)]
    fixed, applied = apply_patches(plan, result["patches"], duration=23.633)
    assert [item["skipped"] for item in applied] == [None]
    assert named(lint_plan(fixed, target_duration=15, duration=23.633, clips=A4_CLIPS), CLIP_CHECK)["passed"] is True
    # A single-clip source has nothing to check, so there is no check at all.
    assert CLIP_CHECK not in names(lint_plan(plan, target_duration=15, duration=23.633, clips=A4_CLIPS[:1]))


def test_a_multi_clip_meta_plan_is_snapped_to_clip_edges_and_records_the_named_ending(
    manager: CampaignManager, tmp_path: Path, meta_on: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from vedit import meta

    first = video(inbox(tmp_path, "first.mp4"), seconds=2.0, color="teal")
    second = video(inbox(tmp_path, "second.mp4"), seconds=2.0, color="purple")
    still = photo(inbox(tmp_path, "colonnade.jpg"))

    def plan_story(proxy: Path, **kwargs: Any) -> dict[str, Any]:
        # Both later moments open a few frames early, inside the previous clip.
        return {
            "title": "Teal to purple to photo",
            "summary": "s",
            "caption": "",
            "cover_prompt": "",
            "hashtags": [],
            "provider": "meta",
            "usage": {},
            "warnings": [],
            "segments": [
                {"start": 0.0, "end": 1.5, "label": "Teal", "reason": "Visible."},
                {"start": 1.8, "end": 3.5, "label": "Purple", "reason": "Visible."},
                {"start": 3.9, "end": 6.0, "label": "Colonnade photo", "reason": "Visible."},
            ],
        }

    monkeypatch.setattr(meta, "plan_story", plan_story)
    job = manager.create(
        {
            "paths": [str(first), str(second), str(still)],
            "goal": "Teal, then purple, and end on the photo.",
            "target_duration": 6,
            "captions": False,
            "provider": "meta",
            "aspect": "16:9",
        }
    )
    manager.process(job["id"])
    planned = manager.get(job["id"])
    assert planned["status"] == "planned", planned.get("error")
    edges = [(clip["start"], clip["end"]) for clip in planned["source"]["clips"]]
    assert len(edges) == 3
    for segment in planned["plan"]["segments"]:
        assert sum(1 for low, high in edges if min(segment["end"], high) - max(segment["start"], low) > 1e-6) == 1, (
            segment
        )
    assert (
        planned["plan"]["segments"][1]["start"] == edges[1][0]
        and planned["plan"]["segments"][2]["start"] == edges[2][0]
    )
    ending = planned["plan"]["bookends"]["ending"]
    assert ending["label"] == "Colonnade photo" and ending["start"] == edges[2][0]
    assert "opening" not in planned["plan"]["bookends"]
    graded = CampaignManager._grade(
        planned["plan"],
        planned["request"],
        planned["source"]["duration"],
        tmp_path,
        None,
        clips=planned["source"]["clips"],
    )
    assert named(graded, CLIP_CHECK)["passed"] and named(graded, ENDING_CHECK)["passed"]


# --- 2. The auto fix keeps the brief's ending ---


def test_brief_bookends_reads_a_named_opening_or_ending() -> None:
    assert brief_bookends(A4_GOAL) == {"opening": False, "ending": True}, "'the flower opening, and' names no opening"
    assert brief_bookends("Start with the hook and finish on the logo.") == {"opening": True, "ending": True}
    assert brief_bookends("A calm walk through the garden.") == {"opening": False, "ending": False}
    assert brief_bookends(None) == {"opening": False, "ending": False}


def test_the_auto_fix_trims_the_lowest_ranked_moment_and_never_drops_the_named_ending() -> None:
    # Run A4 v3: a 20 s cut on a 15 s target. The old fix removed the colonnade photo the brief ended on.
    plan = moments(
        (0.0, 9.0),
        (15.633, 20.633),
        (20.633, 23.633),
        labels=("Archival", "Flower opening", "Colonnade photo"),
        bookends={"ending": {"label": "Colonnade photo", "start": 20.633, "end": 23.633}},
    )
    plan["segments"][0]["score"], plan["segments"][1]["score"] = 0.2, 0.9
    result = lint_plan(plan, target_duration=15, duration=23.633, clips=A4_CLIPS)
    assert named(result, "auto: length fits the target")["passed"] is False
    assert [(p["op"], p["segment"]) for p in result["patches"]] == [("trim_end", 1)], (
        "the lowest-scored moment absorbs the excess"
    )
    fixed, _ = apply_patches(plan, result["patches"], duration=23.633)
    assert [item["label"] for item in fixed["segments"]] == ["Archival", "Flower opening", "Colonnade photo"]
    assert fixed["duration"] == pytest.approx(15.0)
    after = lint_plan(fixed, target_duration=15, duration=23.633, clips=A4_CLIPS)
    assert all(check["passed"] for check in after["checks"]), after["checks"]
    assert (
        named(after, ENDING_CHECK)["note"]
        == "The last moment is still “Colonnade photo”, which the planner placed last for the brief's ending."
    )
    # Even when only the ending could absorb it, the ending is shortened, never dropped.
    tight = moments(
        (0.0, 1.0),
        (20.633, 23.633),
        labels=("Archival", "Colonnade photo"),
        bookends={"ending": {"label": "Colonnade photo", "start": 20.633, "end": 23.633}},
    )
    patches = lint_plan(tight, target_duration=2.5, duration=23.633)["patches"]
    assert ("remove", 2) not in [(p["op"], p["segment"]) for p in patches]


def test_the_ending_check_fails_when_the_named_ending_is_gone() -> None:
    plan = moments(
        (0.0, 5.5),
        (15.633, 20.633),
        labels=("Archival", "Flower opening"),
        bookends={"ending": {"label": "Colonnade photo", "start": 20.633, "end": 23.633}},
    )
    check = named(lint_plan(plan, target_duration=15, duration=23.633), ENDING_CHECK)
    assert check["passed"] is False
    assert (
        check["note"]
        == "The brief names an ending, and the planner placed “Colonnade photo” last for it. It is no longer in the cut."
    )
    # A plan that records no ending gets no ending check.
    assert ENDING_CHECK not in names(lint_plan(moments((0.0, 5.0)), target_duration=5, duration=10))


def test_validate_plan_keeps_the_bookends_and_bounds_them() -> None:
    raw = dict(
        moments((0.0, 4.0), (5.0, 9.0)),
        bookends={"ending": {"label": "  Logo \n card ", "start": 5.0, "end": 99.0}, "opening": "nope", "x": 1},
    )
    plan = validate_plan(raw, 10.0, original=raw)
    assert plan["bookends"] == {"ending": {"label": "Logo card", "start": 5.0, "end": 10.0}}
    # A client that sends the plan back without bookends keeps the planner's.
    again = validate_plan(moments((0.0, 4.0), (5.0, 9.0)), 10.0, original=plan)
    assert again["bookends"] == plan["bookends"]


# --- 3. The target is a ceiling, said plainly ---


def test_the_length_check_names_its_ceiling_and_passes_a_short_cut_with_a_separate_note() -> None:
    # The README's example: 25 s asked, 18.9 s delivered. It passed as "duration matches the target".
    result = lint_plan(moments((0.0, 18.9)), target_duration=25, duration=60)
    check = named(result, "auto: length fits the target")
    assert check["name"] == "auto: length fits the target (≤ 25 s)"
    assert check["passed"] is True and check["note"] == "18.9 s cut, within the 25.0 s target."
    assert NOTE not in names(result), "76% of the target is not far under it"
    short = lint_plan(moments((0.0, 10.0)), target_duration=25, duration=60)
    note = named(short, NOTE)
    assert note["passed"] is True and note["advisory"] is True
    assert (
        note["note"]
        == "The cut runs 10.0 s, 40% of the 25.0 s target. The target is a ceiling, so this is not a failure."
    )
    # A little over passes inside the allowance; more fails and proposes the trim.
    assert (
        named(lint_plan(moments((0.0, 26.0)), target_duration=25, duration=60), "auto: length fits the target")[
            "passed"
        ]
        is True
    )
    over = lint_plan(moments((0.0, 27.0)), target_duration=25, duration=60)
    assert (
        named(over, "auto: length fits the target")["note"]
        == "27.0 s cut, 2.0 s over the 25.0 s target. The allowance is 1.2 s."
    )
    assert [(p["op"], p["seconds"]) for p in over["patches"]] == [("trim_end", 25.0)]


def test_a_target_longer_than_the_footage_says_so() -> None:
    check = named(lint_plan(moments((0.0, 3.0)), target_duration=5, duration=3.0), "auto: length fits the target")
    assert check["name"] == "auto: length fits the target (≤ 3 s)"
    assert check["note"] == "3.0 s cut, within the 3.0 s target. You asked for 5.0 s, but the footage runs 3.0 s."


# --- 4. Notes say only what was measured ---


def _plan(**analysis: Any) -> dict[str, Any]:
    return {"segments": [{"start": 0.0, "end": 4.0}], "duration": 4.0, "analysis": {"method": "meta", **analysis}}


def test_requested_captions_are_never_reported_as_not_requested(tmp_path: Path) -> None:
    request = {"captions": True, "caption_provider": "meta", "target_duration": 4}
    heard = {
        "transcribed": True,
        "transcribed_by": "meta-voice-transcribe",
        "speech": [{"start": 0.0, "end": 4.0}],
        "transcript": [{"start": 0.0, "end": 4.0, "text": "Hello there."}],
        "sentences": [{"start": 0.0, "end": 4.0, "text": "Hello there."}],
    }
    # Requested, the cut has speech, and no caption file was burned: 0% coverage, a failure.
    graded = CampaignManager._grade(_plan(**heard), request, 20.0, tmp_path, None)
    check = named(graded, CAPTIONS)
    assert check["passed"] is False and check["note"] == "0% of 4.0 s of speech has a caption"
    # Meta transcribed and heard no speech: a measurement, and the note says what it found.
    empty = CampaignManager._grade(
        _plan(transcribed=True, transcribed_by="meta-voice-transcribe", transcript=[], sentences=[], speech=[]),
        request,
        20.0,
        tmp_path,
        None,
    )
    check = named(empty, CAPTIONS)
    assert (
        check["passed"] is True
        and check["note"]
        == "Captions were requested. The transcript holds no speech in this cut, so there was nothing to caption."
    )
    assert check["inputs"] == ["meta-voice-transcribe"]
    # No transcript was made, or captions were off: nothing was measured, so neither check appears.
    for plan, asked in ((_plan(transcript=[], sentences=[]), request), (_plan(**heard), dict(request, captions=False))):
        graded = CampaignManager._grade(plan, asked, 20.0, tmp_path, None)
        assert CAPTIONS not in names(graded) and all("not requested" not in check["note"] for check in graded["checks"])
    assert SENTENCES not in names(
        CampaignManager._grade(_plan(transcript=[], sentences=[]), request, 20.0, tmp_path, None)
    )


def test_a_silent_track_is_never_sent_and_no_receipt_says_meta_heard_it(
    manager: CampaignManager, tmp_path: Path, meta_on: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from vedit import meta

    clip = video(inbox(tmp_path, "silent.mp4"), seconds=2.0, color="teal", audio="anullsrc")
    monkeypatch.setattr(
        meta,
        "plan_story",
        lambda proxy, **kw: {
            "title": "Quiet",
            "summary": "s",
            "caption": "",
            "cover_prompt": "",
            "hashtags": [],
            "provider": "meta",
            "usage": {},
            "warnings": [],
            "segments": [{"start": 0.2, "end": 1.8, "label": "Teal", "reason": "Visible."}],
        },
    )
    job = manager.create(
        {"paths": [str(clip)], "goal": "A quiet look.", "target_duration": 2, "captions": True, "provider": "meta"}
    )
    manager.process(job["id"])
    planned = manager.get(job["id"])
    assert planned["status"] == "planned", planned.get("error")
    assert meta_on == [], "silence is never sent to Meta Voice Transcribe"
    analysis = planned["plan"]["analysis"]
    assert not analysis.get("transcribed") and "transcribed_by" not in analysis
    assert (
        "The sound track is silent, so it was not sent to Meta Voice Transcribe. This plan has no transcript."
        in planned["plan"]["warnings"]
    )
    manager.render(job["id"], {})
    manager.process(job["id"])
    done = manager.get(job["id"])
    assert done["status"] == "complete", done.get("error")
    assert meta_on == [], "nor at render"
    warnings = done["result"]["warnings"]
    assert not any("Meta heard" in warning for warning in warnings), warnings
    assert "No speech was found in the kept audio. This video has no captions." in warnings
    checks = done["result"]["checks"]
    assert CAPTIONS not in [check["name"] for check in checks] and SENTENCES not in [check["name"] for check in checks]
    assert all(check["note"] not in ("not requested", "no transcript") for check in checks)


def test_a_photo_only_project_states_the_clamped_length_and_does_not_report_a_sound_track(
    manager: CampaignManager, tmp_path: Path
) -> None:
    # Run B5: one photo, 5 s asked, silently given a 3 s project. The target is a ceiling, so the clamp stays, and is stated.
    still = photo(inbox(tmp_path, "one.jpg"))
    job = manager.create(
        {"paths": [str(still)], "goal": "One photo.", "target_duration": 5, "captions": True, "aspect": "1:1"}
    )
    manager.process(job["id"])
    planned = manager.get(job["id"])
    assert planned["status"] == "planned", planned.get("error")
    assert planned["source"]["duration"] == pytest.approx(3.0, abs=0.05)
    warnings = planned["plan"]["warnings"]
    assert (
        "You asked for 5.0 s, but the footage runs 3.0 s, so the cut is at most 3.0 s. "
        "Each photo holds for 3 s. Set a longer photo length to fill more."
    ) in warnings
    assert "This source has no audio. Captions cannot be generated." in warnings
    assert not any(warning.startswith("This clip's sound track is silent") for warning in warnings), warnings
    # Asked for a longer hold, the photo fills the ask and nothing is said about a clamp.
    longer = manager.create(
        {
            "paths": [str(still)],
            "goal": "One photo.",
            "target_duration": 5,
            "photo_seconds": 5,
            "captions": False,
            "aspect": "1:1",
        }
    )
    manager.process(longer["id"])
    held = manager.get(longer["id"])
    assert held["source"]["duration"] == pytest.approx(5.0, abs=0.05)
    assert not any("You asked for" in warning for warning in held["plan"]["warnings"])


def test_an_ask_longer_than_the_footage_is_stated(manager: CampaignManager, tmp_path: Path) -> None:
    clip = video(inbox(tmp_path, "short.mp4"), seconds=1.5, color="teal")
    job = manager.create({"paths": [str(clip)], "goal": "A short look.", "target_duration": 5, "captions": False})
    manager.process(job["id"])
    planned = manager.get(job["id"])
    assert planned["status"] == "planned", planned.get("error")
    assert (
        "You asked for 5.0 s, but the footage runs 1.5 s, so the cut is at most 1.5 s." in planned["plan"]["warnings"]
    )


# --- 5. .env.local: one loader, the environment wins, empty never overrides ---


def test_load_env_file_lets_the_environment_win_and_reads_comments_like_a_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / ".env.local"
    env.write_text(
        "MODEL_API_KEY=\nVEDIT_META_ENABLED=1 # on\nVEDIT_META_MODE='contributor' # cheaper\n", encoding="utf-8"
    )
    monkeypatch.setenv("MODEL_API_KEY", "from-shell")
    monkeypatch.delenv("VEDIT_META_ENABLED", raising=False)
    monkeypatch.delenv("VEDIT_META_MODE", raising=False)
    assert sorted(settings.load_env_file(env)) == ["VEDIT_META_ENABLED", "VEDIT_META_MODE"]
    assert os.environ["MODEL_API_KEY"] == "from-shell", "an empty value in the file never overrides"
    assert os.environ["VEDIT_META_ENABLED"] == "1" and os.environ["VEDIT_META_MODE"] == "contributor"
    # A # inside a word is part of the value: a key may hold one.
    env.write_text("MODEL_API_KEY=ab#cd\n", encoding="utf-8")
    monkeypatch.setenv("MODEL_API_KEY", "")
    assert settings.load_env_file(env) == ["MODEL_API_KEY"] and os.environ["MODEL_API_KEY"] == "ab#cd"


def test_vedit_doctor_and_setup_read_env_local_through_the_shared_loader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from vedit import cli, onboarding

    env = tmp_path / ".env.local"
    env.write_text("MODEL_API_KEY=test-key-from-env-local\nVEDIT_META_ENABLED=1\n", encoding="utf-8")
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.delenv("VEDIT_META_ENABLED", raising=False)
    loads: list[str] = []
    monkeypatch.setattr(settings, "load_checkout_env", lambda: loads.append("doctor") or settings.load_env_file(env))
    monkeypatch.setattr(cli, "run_doctor", lambda: [doctor.meta_check()])
    try:
        assert cli.main(["doctor"]) == 0, capsys.readouterr().out
        assert "OK  meta" in capsys.readouterr().out and loads == ["doctor"]
        monkeypatch.setattr(onboarding, "setup_status", lambda refresh=False: {"ready": True, "items": []})
        assert cli.main(["setup"]) == 0 and loads == ["doctor", "doctor"]
    finally:
        os.environ.pop("MODEL_API_KEY", None)
        os.environ.pop("VEDIT_META_ENABLED", None)


def test_doctor_fails_the_meta_row_after_meta_refused_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from vedit import meta

    reason = "Meta is set up, but the last call with this key failed: Meta rejected the API key (401)."
    monkeypatch.setattr(
        meta,
        "capabilities",
        lambda: {
            "configured": True,
            "enabled": True,
            "available": True,
            "mode": "standard",
            "reason": reason,
            "last_problem": "Meta rejected the API key (401).",
        },
    )
    check = doctor.meta_check()
    assert check.ok is False and check.required and check.detail == reason
    monkeypatch.setattr(
        meta,
        "capabilities",
        lambda: {
            "configured": True,
            "enabled": True,
            "available": True,
            "mode": "standard",
            "reason": "ok",
            "last_problem": None,
        },
    )
    assert doctor.meta_check().ok is True


def _studio_loader(env_file: Path, environment: dict[str, str]) -> dict[str, str]:
    """Run studio.sh's own loader function under bash with a clean environment, and return what it exported."""
    script = (ROOT / "scripts" / "studio.sh").read_text(encoding="utf-8")
    start = script.index("load_env_local() {")
    body = script[start : script.index("\n}\n", start) + 3]
    names = ("MODEL_API_KEY", "VEDIT_META_ENABLED", "VEDIT_META_MODE", "VEDIT_FFMPEG", "VEDIT_X", "VEDIT_Q")
    probe = body + 'load_env_local "$1"\n' + "".join(f'printf "%s=%s\\n" {name} "${{{name}:-}}"\n' for name in names)
    out = subprocess.run(
        ["bash", "-c", "set -euo pipefail\n" + probe, "studio", str(env_file)],
        env={"PATH": os.environ["PATH"], **environment},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return dict(line.split("=", 1) for line in out.splitlines())


def test_studio_sh_keeps_a_shell_key_and_never_runs_the_file(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    env = tmp_path / ".env.local"
    env.write_text(
        "# copied from .env.example\nMODEL_API_KEY=\nVEDIT_META_ENABLED=1 # on\nVEDIT_META_MODE=standard\n"
        f'export VEDIT_FFMPEG="/opt/a b/ffmpeg"\nVEDIT_X=$(touch {marker})\nVEDIT_Q=ab#cd',
        encoding="utf-8",
    )
    seen = _studio_loader(env, {"MODEL_API_KEY": "from-shell", "VEDIT_META_MODE": "contributor"})
    assert seen["MODEL_API_KEY"] == "from-shell", "the empty line in the file must not wipe the exported key"
    assert seen["VEDIT_META_MODE"] == "contributor", "the shell wins over the file"
    assert (
        seen["VEDIT_META_ENABLED"] == "1" and seen["VEDIT_FFMPEG"] == "/opt/a b/ffmpeg" and seen["VEDIT_Q"] == "ab#cd"
    )
    assert not marker.exists() and seen["VEDIT_X"].startswith("$(touch"), "a line is data, never a command"
    # With nothing in the shell, the file fills it in.
    env.write_text("MODEL_API_KEY=from-file\n", encoding="utf-8")
    assert _studio_loader(env, {})["MODEL_API_KEY"] == "from-file"
    assert '. "$project_dir/.env.local"' not in (ROOT / "scripts" / "studio.sh").read_text(encoding="utf-8")


def test_env_example_keeps_comments_on_their_own_lines() -> None:
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            assert " #" not in line.split("=", 1)[1], line
