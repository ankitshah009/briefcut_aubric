"""The manager, Studio routes and MCP tools reach today's engines: Wonder, punch words, verified find and the hero word.

Every Meta answer is stubbed at the engine's own seam, so these tests check
the wiring (what is passed, what is kept, what a client sees), not the models.
"""

from __future__ import annotations

import http.client
import importlib.util
import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from vedit import director, emphasis, hero_word, meta, meta_ledger, verify_find
from vedit.campaign import CampaignError, CampaignManager
from vedit.edit_spec import default_edit
from vedit.studio.server import StudioHandler, _LoopbackServer


def load_helpers() -> Any:
    spec = importlib.util.spec_from_file_location("wiring_test_media_helpers", Path(__file__).with_name("test_e2e.py"))
    assert spec and spec.loader
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)
    if not helpers._have_ffmpeg():
        pytest.skip("ffmpeg required")
    return helpers


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.delenv("VEDIT_META_ENABLED", raising=False)
    monkeypatch.delenv("VEDIT_META_MODE", raising=False)
    monkeypatch.delenv("VEDIT_META_DIRECTOR", raising=False)
    monkeypatch.setattr(meta, "_request", lambda *a, **k: pytest.fail("Unexpected provider HTTP call"))


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_API_KEY", "test-secret-never-write-to-disk")
    monkeypatch.setenv("VEDIT_META_ENABLED", "1")
    monkeypatch.setattr(
        meta, "transcribe_audio", lambda wav, **kwargs: {"cues": [], "provider": "meta", "usage": {"cost_usd": 0}}
    )
    monkeypatch.setattr(
        meta,
        "plan_story",
        lambda *a, **k: {
            "title": "Bowl",
            "summary": "s",
            "caption": "c",
            "cover_prompt": "p",
            "hashtags": [],
            "segments": [{"start": 0.2, "end": 1.4, "label": "A", "reason": "r"}],
            "usage": {},
            "warnings": [],
        },
    )


@pytest.fixture
def clip(tmp_path: Path) -> Path:
    source = tmp_path / "vedit-sessions" / "source-test" / "inbox" / "clip.mp4"
    source.parent.mkdir(parents=True)
    return load_helpers()._make_clip(source, seconds=1.6, color="teal")


@pytest.fixture
def manager(tmp_path: Path) -> Iterator[CampaignManager]:
    instance = CampaignManager(tmp_path, start_worker=False)
    try:
        yield instance
    finally:
        instance.close()


def request(clip: Path, **overrides: Any) -> dict[str, Any]:
    return {"paths": [str(clip)], "goal": "Show the bowl", "target_duration": 1.2, "captions": False, **overrides}


def planned(manager: CampaignManager, clip: Path, **overrides: Any) -> str:
    job = manager.create(request(clip, **overrides))
    manager.process(job["id"])
    assert manager.get(job["id"])["status"] == "planned", manager.get(job["id"])["error"]
    return job["id"]


def notes(**extra: Any) -> dict[str, Any]:
    return {
        "verdict": "Tight.",
        "strengths": [],
        "fixes": [],
        "caption_suggestion": "",
        "hook_score": 4,
        "provider": "meta",
        "usage": {"cost_usd": 0.001},
        "warnings": [],
        "checks": [{"name": "Pacing", "passed": True, "at": None, "note": "Moves."}],
        "patches": [],
        **extra,
    }


# --- C1 and C4: the director ----------------------------------------------------


@pytest.mark.parametrize("bad", ["muse-wonder", "Wonder", "", "gpt", 1, True, ["wonder"]])
def test_only_spark_or_wonder_can_direct(manager: CampaignManager, clip: Path, bad: Any) -> None:
    with pytest.raises(CampaignError) as refused:
        manager.create(request(clip, director=bad))
    assert refused.value.status == 400 and "spark or wonder" in str(refused.value)
    assert manager.list() == []


def test_the_chosen_director_reviews_and_signs_its_checks(
    manager: CampaignManager, clip: Path, enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[Any] = []

    def review(video: Path, **kwargs: Any) -> dict[str, Any]:
        asked.append(kwargs.get("director"))
        label = "muse-wonder" if kwargs.get("director") == "wonder" else "muse-spark"
        return notes(model="muse-wonder-1.0" if label == "muse-wonder" else "muse-spark-1.3", director=label)

    monkeypatch.setattr(meta, "review_cut", review)
    ident = planned(manager, clip, provider="meta", review=True, director="wonder", budget_usd=1.0)
    assert manager.get(ident)["request"]["director"] == "wonder"
    manager.render(ident, {})
    manager.process(ident)
    done = manager.get(ident)
    assert done["status"] == "complete", done["error"]
    assert asked == ["wonder"]
    assert (
        done["result"]["review"]["model"] == "muse-wonder-1.0" and done["result"]["review"]["director"] == "muse-wonder"
    )
    signed = [check for check in done["result"]["checks"] if check["name"].startswith("director: ")]
    assert signed and all(check["by"] == "muse-wonder" and check["basis"] == "inferred" for check in signed)

    # A revision can hand the next version to the other director.
    with pytest.raises(CampaignError) as refused:
        manager.revise(ident, {"mode": "auto", "director": "claude"})
    assert refused.value.status == 400
    manager._update(
        ident,
        result=dict(
            done["result"],
            patches=[{"op": "trim_end", "segment": 1, "seconds": 1.3, "reason": "Tighter.", "source": "auto"}],
        ),
    )
    manager.revise(ident, {"mode": "auto", "director": "spark"})
    manager.process(ident)
    again = manager.get(ident)
    assert again["status"] == "complete", again["error"]
    assert asked == ["wonder", "spark"] and again["request"]["director"] == "spark"
    assert all(
        check["by"] == "muse-spark" for check in again["result"]["checks"] if check["name"].startswith("director: ")
    )


def test_an_unset_director_leaves_the_choice_to_the_environment(
    manager: CampaignManager, clip: Path, enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[Any] = []
    monkeypatch.setattr(
        meta,
        "review_cut",
        lambda video, **kwargs: (
            asked.append(kwargs.get("director")) or notes(model="muse-spark-1.3", director="muse-spark")
        ),
    )
    ident = planned(manager, clip, provider="meta", review=True)
    assert "director" not in manager.get(ident)["request"]
    manager.render(ident, {})
    manager.process(ident)
    assert manager.get(ident)["status"] == "complete"
    assert asked == [None], "None lets VEDIT_META_DIRECTOR choose"


def test_a_budget_too_small_for_wonder_says_what_to_raise_it_to(
    manager: CampaignManager, clip: Path, enabled: None
) -> None:
    # The real review_cut runs: it refuses before any upload, so no HTTP stub is needed.
    ident = planned(manager, clip, provider="meta", review=True, director="wonder", budget_usd=0.25)
    manager.render(ident, {})
    manager.process(ident)
    done = manager.get(ident)
    assert done["status"] == "complete", "a review that cannot run is a warning, never a failed render"
    review = done["result"]["review"]
    assert review["director"] == "muse-wonder" and "Raise the campaign budget to at least $0.3" in review["error"]
    assert "or choose Muse Spark as the director." in review["error"]
    assert any(
        warning.startswith("Director's notes unavailable: A Muse Wonder review")
        for warning in done["result"]["warnings"]
    )
    assert done["result"]["proof"]["has_video"] is True


def test_capabilities_name_both_directors() -> None:
    from vedit.campaign import capabilities

    caps = capabilities()
    assert caps["director"]["choices"] == ["spark", "wonder"]
    cloud = meta.capabilities()
    assert cloud["directors"]["wonder"] == "muse-wonder-1.0" and cloud["directors"]["default"] == "spark"
    assert cloud["directors"]["wonder_min_budget_usd"] == 0.33 and "muse-wonder-1.0" not in cloud["models"].values(), (
        "Wonder is optional"
    )


# --- C2: punch words ------------------------------------------------------------


def _emphasis_job(manager: CampaignManager, clip: Path, **overrides: Any) -> tuple[str, dict[str, Any], dict[str, Any]]:
    ident = planned(manager, clip, **overrides)
    job = manager._load(manager._connect(), ident)
    job["request"]["captions"] = True
    plan = dict(
        job["plan"],
        edit=dict(job["plan"]["edit"], captions=dict(job["plan"]["edit"]["captions"], emphasize=True, emphasis=[])),
    )
    return ident, job, plan


def test_render_asks_spark_for_punch_words_and_keeps_them(
    manager: CampaignManager, clip: Path, enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}
    cues = [{"key": "200", "text": "The oak bowl shines."}]

    def suggest(
        lines: Any, goal: str, language: str, *, work_dir: Path, budget_usd: float, **kwargs: Any
    ) -> dict[str, Any]:
        seen.update(lines=lines, goal=goal, language=language, work_dir=work_dir, budget=budget_usd)
        picks = [{"cue_key": "200", "word_index": 1, "word": "oak", "why": "Names the wood."}]
        return {
            "emphasis": picks,
            "edit": emphasis.edit_patch(picks),
            "warnings": ["1 pick was moved."],
            "model": "muse-spark-1.3",
        }

    ident, job, plan = _emphasis_job(manager, clip, provider="meta", budget_usd=0.4)
    monkeypatch.setattr("vedit.captions.story_cues", lambda plan, view=None: cues)
    monkeypatch.setattr(emphasis, "suggest_emphasis", suggest)
    warnings: list[str] = []
    picks = manager._emphasis(ident, job, plan, warnings)
    assert picks == [{"cue_key": "200", "word_index": 1, "word": "oak", "why": "Names the wood."}]
    assert seen == {
        "lines": cues,
        "goal": "Show the bowl",
        "language": job["request"]["language"],
        "work_dir": manager.root / ident / "meta",
        "budget": 0.4,
    }
    assert (
        plan["edit"]["captions"]["emphasis"] == [{"cue_key": "200", "word_index": 1, "word": "oak"}]
        and plan["edit"]["captions"]["emphasize"] is True
    )
    assert manager.get(ident)["plan"]["edit"]["captions"]["emphasis"] == plan["edit"]["captions"]["emphasis"], (
        "the next version reuses the picks"
    )
    assert warnings == ["1 pick was moved."]
    assert CampaignManager._emphasis_result(plan, picks) == picks
    assert CampaignManager._emphasis_result(plan, None) == [dict(picks[0], why="")], (
        "a pick saved earlier is still reported"
    )
    # Picks already named are never asked for again.
    monkeypatch.setattr(emphasis, "suggest_emphasis", lambda *a, **k: pytest.fail("asked twice"))
    assert manager._emphasis(ident, job, plan, []) is None


@pytest.mark.parametrize(
    "failure",
    [
        emphasis.EmphasisError("There are no caption lines to emphasise."),
        meta.MetaError("Meta's rate limit was reached."),
        RuntimeError("secret upstream detail"),
    ],
)
def test_a_failed_punch_word_request_is_a_warning(
    manager: CampaignManager, clip: Path, enabled: None, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise failure

    ident, job, plan = _emphasis_job(manager, clip, provider="meta")
    monkeypatch.setattr("vedit.captions.story_cues", lambda plan, view=None: [])
    monkeypatch.setattr(emphasis, "suggest_emphasis", broken)
    before = json.dumps(plan["edit"], sort_keys=True)
    warnings: list[str] = []
    assert manager._emphasis(ident, job, plan, warnings) is None
    assert json.dumps(plan["edit"], sort_keys=True) == before
    assert len(warnings) == 1 and warnings[0].startswith("Punch words were not added:") and "secret" not in warnings[0]


def test_punch_words_are_not_asked_for_without_meta_or_captions(
    manager: CampaignManager, clip: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(emphasis, "suggest_emphasis", lambda *a, **k: pytest.fail("no Meta consent"))
    ident, job, plan = _emphasis_job(manager, clip)
    warnings: list[str] = []
    assert manager._emphasis(ident, job, plan, warnings) is None and "does not use Meta" in warnings[0]
    job["request"]["captions"] = False
    assert manager._emphasis(ident, job, plan, []) is None


def test_a_render_reports_its_punch_words(manager: CampaignManager, clip: Path) -> None:
    ident = planned(manager, clip)
    manager.render(ident, {})
    manager.process(ident)
    done = manager.get(ident)
    assert done["status"] == "complete", done["error"]
    assert done["result"]["emphasis"] == [] and done["result"]["hero_word"] is None
    report = json.loads(manager.artifact(ident, "report").read_text())
    assert report["emphasis"] == [] and report["hero_word"] is None


def test_a_meta_render_draws_sparks_punch_word(
    manager: CampaignManager, clip: Path, enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        meta,
        "transcribe_audio",
        lambda wav, **kw: {
            "cues": [{"start": 0.3, "end": 1.2, "text": "Made by hand."}],
            "provider": "meta",
            "usage": {"cost_usd": 0.0001},
        },
    )
    asked: list[Any] = []

    def suggest(cues: Any, goal: str, language: str, **kwargs: Any) -> dict[str, Any]:
        lines = emphasis.emphasis_lines(cues)
        asked.append(lines)
        picks = [{"cue_key": lines[0]["key"], "word_index": 2, "word": lines[0]["words"][2], "why": "The craft."}]
        return {"emphasis": picks, "edit": emphasis.edit_patch(picks), "warnings": [], "model": "muse-spark-1.3"}

    monkeypatch.setattr(emphasis, "suggest_emphasis", suggest)
    ident = planned(manager, clip, provider="meta", captions=True)
    plan = manager.get(ident)["plan"]
    plan = dict(plan, edit=dict(plan["edit"], captions=dict(plan["edit"]["captions"], emphasize=True, emphasis=[])))
    manager.render(ident, {"plan": plan})
    manager.process(ident)
    done = manager.get(ident)
    assert done["status"] == "complete", done["error"]
    if not asked:
        pytest.skip("this cut kept no caption line to emphasise: " + "; ".join(done["result"]["warnings"]))
    [pick] = done["result"]["emphasis"]
    assert pick["word"].startswith("hand") and pick["why"] == "The craft."
    assert done["plan"]["edit"]["captions"]["emphasis"] == [
        {key: pick[key] for key in ("cue_key", "word_index", "word")}
    ]
    ass = (manager.root / ident / "render-1" / "captions.ass").read_text(encoding="utf-8").upper()
    assert "8CE83C" in ass, "the punch word is drawn in the accent color"


def test_the_director_may_turn_punch_words_on() -> None:
    field = director.EDIT_FIELDS["captions.emphasize"]
    assert field.kind == "flag" and director.coerce_edit_value(field, "true") is True


# --- C3: find, checked by SAM ----------------------------------------------------


def _found(start: float, end: float, reason: str, score: float = 0.9) -> dict[str, Any]:
    return {"start": start, "end": end, "reason": reason, "label": reason[:60], "source": "meta", "score": score}


def _meta_find(
    manager: CampaignManager, clip: Path, monkeypatch: pytest.MonkeyPatch, *, subject: str | None = "wooden bowl"
) -> tuple[str, dict[str, Any]]:
    ident = planned(manager, clip, provider="meta")
    monkeypatch.setattr(
        meta,
        "find_moments",
        lambda proxy, **kwargs: {
            "moments": [_found(0.2, 0.9, "A bowl on the bench."), _found(1.0, 1.5, "Hands at the lathe.", 0.85)],
            "subject": subject,
            "warnings": [],
            "provider": "meta",
            "usage": {},
        },
    )
    calls: dict[str, Any] = {}
    return ident, calls


def test_find_checks_spark_with_sam_and_serves_its_thumbnails(
    manager: CampaignManager, clip: Path, enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    ident, calls = _meta_find(manager, clip, monkeypatch)
    folder = manager.root / ident / "meta" / "verified-find"

    def check(
        moments: list[dict[str, Any]], query: str, source: Path, work_dir: Path, budget: float, **kwargs: Any
    ) -> dict[str, Any]:
        calls.update(query=query, source=source, work_dir=work_dir, budget=budget, **kwargs)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "wooden-bowl-200.jpg").write_bytes(b"\xff\xd8\xff\xe0jpeg")
        first = dict(
            moments[0],
            verified=True,
            coverage=0.9,
            thumbnail=str(folder / "wooden-bowl-200.jpg"),
            verification="SAM masked it.",
            evidence={"phrase": "wooden bowl", "by": "sam-3.1"},
        )
        second = dict(
            moments[1],
            verified=False,
            coverage=0.0,
            thumbnail="/etc/passwd",
            flagged=True,
            verification="SAM saw none.",
            score=0.42,
        )
        receipt = {
            "name": "sam: “wooden bowl” is on screen in the found moments",
            "passed": False,
            "at": 1.0,
            "basis": "derived",
            "by": "sam-3.1",
            "inputs": ["muse-spark"],
        }
        return {
            "status": "verified",
            "phrase": "wooden bowl",
            "kind": "visual",
            "reason": "1 verified",
            "moments": [first, second],
            "dropped": [],
            "spend": [{"model": "sam-3.1", "cost_usd": 0.01}],
            "spent_usd": 0.01,
            "warnings": [],
            "receipt": receipt,
            "model": "sam-3.1",
        }

    monkeypatch.setattr(verify_find, "verify_moments", check)
    found = manager.find(ident, {"query": "the bowl"})
    assert calls["phrase"] == "wooden bowl", "Spark names what SAM looks for"
    assert (
        calls["unseen"] == "flag" and calls["work_dir"] == manager.root / ident / "meta" and calls["out_dir"] == folder
    )
    assert calls["source"].name.endswith(".mp4") and calls["budget"] == manager.get(ident)["request"]["budget_usd"]
    best, doubted = found["moments"]
    assert (best["verified"], best["coverage"], best["flagged"], best["thumbnail"]) == (
        True,
        0.9,
        False,
        "wooden-bowl-200.jpg",
    )
    assert best["thumbnail_url"] == f"/api/campaigns/{ident}/find-thumbnail?name=wooden-bowl-200.jpg"
    assert (doubted["verified"], doubted["flagged"], doubted["thumbnail"], doubted["thumbnail_url"]) == (
        False,
        True,
        None,
        None,
    ), "a path outside the folder is never shown"
    assert set(found["verification"]) >= {"status", "phrase", "receipt", "dropped", "spend"}
    assert found["verification"]["status"] == "verified" and found["verification"]["receipt"]["by"] == "sam-3.1"
    assert manager.found_thumbnail(ident, "wooden-bowl-200.jpg") == (folder / "wooden-bowl-200.jpg").resolve()
    outside = manager.root / ident / "secret.jpg"
    outside.write_bytes(b"x")
    (folder / "linked.jpg").symlink_to(outside)
    for name in (
        "../secret.jpg",
        "a/b.jpg",
        "wooden-bowl-200.png",
        "..jpg",
        "linked.jpg",
        "missing.jpg",
        None,
        "WOODEN.jpg",
    ):
        with pytest.raises(CampaignError) as refused:
            manager.found_thumbnail(ident, name)
        assert refused.value.status == 404, name


def test_a_spoken_search_never_hands_sam_a_phrase(
    manager: CampaignManager, clip: Path, enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    ident, calls = _meta_find(manager, clip, monkeypatch, subject="pricing slide")
    monkeypatch.setattr(
        verify_find,
        "verify_moments",
        lambda moments, *a, **k: (
            calls.update(k)
            or {
                "status": "skipped",
                "phrase": None,
                "moments": moments,
                "dropped": [],
                "spend": [],
                "spent_usd": 0.0,
                "warnings": [],
                "receipt": None,
            }
        ),
    )
    found = manager.find(ident, {"query": "where I talk about pricing", "drop_unseen": True})
    assert calls["phrase"] is None and calls["unseen"] == "drop"
    assert found["verification"]["status"] == "skipped"
    assert all(
        moment["verified"] is None and moment["thumbnail_url"] is None and moment["flagged"] is False
        for moment in found["moments"]
    )


def test_find_without_the_check_or_with_a_failed_check_keeps_sparks_moments(
    manager: CampaignManager, clip: Path, enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    ident, _ = _meta_find(manager, clip, monkeypatch)
    monkeypatch.setattr(verify_find, "verify_moments", lambda *a, **k: pytest.fail("verify is off"))
    off = manager.find(ident, {"query": "the bowl", "verify": False})
    assert off["verification"]["status"] == "skipped" and "turned off" in off["verification"]["reason"]
    assert len(off["moments"]) == 2 and all(
        moment["verified"] is None and moment["coverage"] is None for moment in off["moments"]
    )

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise meta.MetaError("SAM needs a clip with a picture to check.")

    monkeypatch.setattr(verify_find, "verify_moments", broken)
    failed = manager.find(ident, {"query": "the bowl"})
    assert (
        failed["verification"]["status"] == "failed" and len(failed["moments"]) == 2 and "footage" in failed["searched"]
    )
    assert "SAM could not check the found moments: SAM needs a clip with a picture to check." in failed["warnings"]
    with pytest.raises(CampaignError):
        manager.find(ident, {"query": "the bowl", "verify": "yes"})


def test_a_merged_moment_keeps_sams_check() -> None:
    said = {"start": 3.0, "end": 6.0, "reason": "Said.", "source": "transcript", "score": 0.5}
    seen = {
        "start": 3.5,
        "end": 6.5,
        "reason": "Seen.",
        "source": "meta",
        "score": 0.9,
        "verified": True,
        "coverage": 0.75,
        "thumbnail": "/x/bowl.jpg",
        "evidence": {"by": "sam-3.1"},
        "flagged": False,
        "verification": "SAM masked it.",
    }
    [merged] = director.merge_moments([said], [seen])
    assert merged["source"] == "both" and merged["verified"] is True and merged["coverage"] == 0.75
    assert (
        merged["thumbnail"] == "/x/bowl.jpg"
        and merged["evidence"] == {"by": "sam-3.1"}
        and merged["verification"] == "SAM masked it."
    )
    plain = director.merge_moments(
        [said],
        [
            dict(
                seen,
                verified=None,
                coverage=None,
                thumbnail=None,
                evidence=None,
                flagged=None,
                verification="Not checked.",
            )
        ],
    )
    assert plain[0]["verification"] == "Not checked."


def test_spark_names_the_subject_for_sam(enabled: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Http:
        def __init__(self, answer: dict[str, Any]) -> None:
            self.answer, self.payloads = answer, []

        def __call__(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
            self.payloads.append((path, kwargs.get("payload")))
            if path == "/files":
                return {"id": "file-abc123"}
            if method == "DELETE":
                return {"deleted": True}
            if path == "/responses/input_tokens":
                return {"input_tokens": 100}
            return {
                "id": "r",
                "status": "completed",
                "usage": {"input_tokens": 100, "output_tokens": 20},
                "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(self.answer)}]}],
            }

    video = tmp_path / "proxy.mp4"
    video.write_bytes(b"media")
    monkeypatch.setattr(meta, "probe", lambda _: type("Info", (), {"duration": 10.0})())
    http = Http({"moments": [{"start": 1.0, "end": 2.0, "reason": "A bowl."}], "subject": "  Wooden   Bowl "})
    monkeypatch.setattr(meta, "_request", http)
    found = meta.find_moments(video, query="the bowl", duration=10.0, work_dir=tmp_path / "work")
    assert found["subject"] == "wooden bowl"
    schema = next(payload for path, payload in http.payloads if path == "/responses")["text"]["format"]["schema"]
    assert schema["required"] == ["moments", "subject"] and set(schema["properties"]) == {"moments", "subject"}
    assert (
        meta.find_moments(video, query="the bowl", duration=10.0, work_dir=tmp_path / "work")["subject"]
        == "wooden bowl"
    ), "the cache keeps it"
    for raw in ("", "a very long phrase of many words", None, 3, "x" * 61):
        assert meta._found_subject({"subject": raw}) is None


def test_sams_check_says_what_it_was_for(enabled: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def sam_text(call: dict[str, Any]) -> tuple[str, dict[str, Any], list[str]]:
        seen["purpose"] = call["purpose"]
        return "", {}, []

    monkeypatch.setattr(
        meta,
        "_sam_call",
        lambda source, **kwargs: {
            "subject": kwargs["subject"],
            "fps": 30.0,
            "duration": 1.0,
            "cache": tmp_path / "masks.json",
        },
    )
    monkeypatch.setattr(meta, "_sam_text", sam_text)
    meta.subject_masks(
        tmp_path / "w.mp4",
        subject="the bowl",
        duration=1.0,
        work_dir=tmp_path,
        purpose="Check “the bowl” in a found moment",
    )
    assert seen["purpose"] == "Check “the bowl” in a found moment"
    meta.subject_masks(tmp_path / "w.mp4", subject="the bowl", duration=1.0, work_dir=tmp_path)
    assert seen["purpose"] == "Cut out “the bowl”"
    assert meta_ledger._PURPOSES["emphasis"] == "Pick the punch words in the captions"


# --- C5: the hero word ------------------------------------------------------------


def _suggestion(**extra: Any) -> dict[str, Any]:
    return {
        "word": "oak",
        "start": 0.2,
        "end": 1.0,
        "side": "center",
        "reason": "Names the wood.",
        "spoken": [0.26, 0.5],
        "estimated": False,
        "adjusted": ["Spark asked for 0.5 s; the word holds for at least 1 s when there is room."],
        "proposal": {"word": "oak", "start": 0.3, "end": 0.8, "side": "center", "reason": "Names the wood."},
        "provider": "meta",
        "model": "muse-spark-1.3",
        "usage": {"cost_usd": 0.0004},
        **extra,
    }


def test_hero_word_is_suggested_checked_and_not_rendered(
    manager: CampaignManager, clip: Path, enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def suggest(plan: dict[str, Any], transcript: Any = None, **kwargs: Any) -> dict[str, Any]:
        seen.update(transcript=transcript, **kwargs)
        return _suggestion()

    monkeypatch.setattr(hero_word, "suggest_hero_word", suggest)
    ident = planned(manager, clip, provider="meta", subject="the bowl", budget_usd=0.3, aspect="16:9")
    result = manager.suggest_hero_word(ident, font="condensed", color="#ffd60a")
    assert (
        seen["work_dir"] == manager.root / ident / "meta"
        and seen["budget_usd"] == 0.3
        and seen["aspect"] == "16:9"
        and seen["subject"] == "the bowl"
    )
    assert isinstance(seen["transcript"], list)
    hero = result["hero_word"]
    assert (hero["word"], hero["start"], hero["end"], hero["subject"], hero["font"], hero["color"]) == (
        "oak",
        0.2,
        1.0,
        "the bowl",
        "condensed",
        "#FFD60A",
    )
    assert result["proposal"]["start"] == 0.3 and result["adjusted"] and result["usage"] == {"cost_usd": 0.0004}
    job = manager.get(ident)
    assert job["status"] == "planned" and job["plan"]["edit"]["hero_word"] is None, (
        "a suggestion changes nothing until it is rendered"
    )
    assert CampaignManager._hero_spoken(
        dict(job["plan"], edit=dict(job["plan"]["edit"], hero_word=hero)),
        manager._load(manager._connect(), ident)["_hero_spoken"],
    )["spoken"] == [0.26, 0.5]
    for options, status in (({"font": "comic"}, 400), ({"color": "#123456"}, 400)):
        with pytest.raises(CampaignError) as refused:
            manager.suggest_hero_word(ident, **options)
        assert refused.value.status == status

    def unsaid(*args: Any, **kwargs: Any) -> Any:
        raise hero_word.HeroWordError("A hero word needs speech, and no words were heard in this cut.")

    monkeypatch.setattr(hero_word, "suggest_hero_word", unsaid)
    with pytest.raises(CampaignError) as refused:
        manager.suggest_hero_word(ident)
    assert refused.value.status == 409 and "needs speech" in str(refused.value)


def test_hero_word_needs_a_meta_project(manager: CampaignManager, clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hero_word, "suggest_hero_word", lambda *a, **k: pytest.fail("no Meta consent"))
    ident = planned(manager, clip)
    with pytest.raises(CampaignError) as refused:
        manager.suggest_hero_word(ident)
    assert refused.value.status == 409 and "Meta mode" in str(refused.value)


def _hero_plan(manager: CampaignManager, ident: str) -> dict[str, Any]:
    job = manager.get(ident)
    hero = {
        "word": "oak",
        "start": 0.2,
        "end": 1.0,
        "subject": "the bowl",
        "side": "center",
        "font": "heavy",
        "color": "#FFFFFF",
        "reason": "",
    }
    return dict(job["plan"], edit=dict(job["plan"]["edit"], hero_word=hero))


def test_render_masks_the_hero_window_and_writes_the_side_back(
    manager: CampaignManager, clip: Path, enabled: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}
    matte = tmp_path / "hero.mkv"

    def window(source: Path, plan: dict[str, Any], hero: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        seen.update(source=source, hero=hero, **kwargs)
        return {
            "path": matte,
            "stats": {"frames": 24, "window_frames": 24, "coverage": 1.0, "hidden": 0.2},
            "side": "top",
            "side_note": "Spark asked for center, so the word sits top instead, 20% behind the subject.",
            "subject_box": [0.3, 0.4, 0.7, 0.9],
            "usage": {},
            "warnings": [],
        }

    monkeypatch.setattr(hero_word, "window_matte", window)
    ident = planned(manager, clip, provider="meta", aspect="16:9")
    job = manager._load(manager._connect(), ident)
    plan = _hero_plan(manager, ident)
    warnings: list[str] = []
    path, stats = manager._hero_matte(job, clip, plan, 30.0, warnings)
    assert path == matte and stats["hidden"] == 0.2
    assert (
        seen["work_dir"] == manager.root / ident
        and seen["fps"] == 30.0
        and seen["aspect"] == "16:9"
        and seen["hero"]["word"] == "oak"
    )
    assert plan["edit"]["hero_word"]["side"] == "top" and plan["edit"]["hero_word"]["subject_box"] == [
        0.3,
        0.4,
        0.7,
        0.9,
    ]
    assert manager.get(ident)["plan"]["edit"]["hero_word"]["side"] == "top"
    assert warnings == ["Spark asked for center, so the word sits top instead, 20% behind the subject."]

    def lost(*args: Any, **kwargs: Any) -> Any:
        raise hero_word.HeroWordError(
            "SAM could not find “the bowl” during the hero word, so it is left out. Name the subject more clearly."
        )

    monkeypatch.setattr(hero_word, "window_matte", lost)
    warnings = []
    assert manager._hero_matte(job, clip, _hero_plan(manager, ident), 30.0, warnings) == (None, None)
    assert warnings[0].startswith("The hero word was left out: SAM could not find")


def test_a_hero_word_render_layers_the_word_and_signs_its_receipt(
    manager: CampaignManager, clip: Path, enabled: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vedit.media import run_ffmpeg

    matte = tmp_path / "hero.mkv"
    # A subject in the lower middle of the frame, for every frame of the 0.8 s window.
    run_ffmpeg(
        [
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=160x90:r=30:d=0.8",
            "-vf",
            "drawbox=x=50:y=40:w=60:h=50:color=white:t=fill,format=gray",
            "-c:v",
            "ffv1",
            str(matte),
        ],
        timeout=60,
    )
    monkeypatch.setattr(
        hero_word,
        "window_matte",
        lambda source, plan, hero, **kwargs: {
            "path": matte,
            "stats": {"frames": 24, "window_frames": 24, "coverage": 1.0, "hidden": 0.25},
            "side": "top",
            "side_note": "",
            "subject_box": [0.31, 0.44, 0.69, 1.0],
            "usage": {},
            "warnings": [],
        },
    )
    ident = planned(manager, clip, provider="meta", aspect="16:9")
    manager._update(ident, _hero_spoken={"word": "oak", "start": 0.2, "spoken": [0.25, 0.5]})
    manager.render(ident, {"plan": _hero_plan(manager, ident)})
    manager.process(ident)
    done = manager.get(ident)
    assert done["status"] == "complete", done["error"]
    assert done["result"]["hero_word"]["side"] == "top" and done["result"]["hero_word"]["subject_box"] == [
        0.31,
        0.44,
        0.69,
        1.0,
    ]
    steps = [
        step["name"] for step in json.loads((manager.root / ident / "render-1" / "project.json").read_text())["steps"]
    ]
    assert "hero_word" in steps
    [check] = [item for item in done["result"]["checks"] if item["name"] == "auto: hero word sits behind the subject"]
    assert check["passed"] is True and check["by"] == "lint" and check["inputs"] == ["sam-3.1", "muse-spark"], check
    assert done["result"]["passed"] == done["result"]["total"] and done["result"]["proof"]["has_video"] is True


def test_hero_word_is_skipped_without_meta(
    manager: CampaignManager, clip: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hero_word, "window_matte", lambda *a, **k: pytest.fail("no Meta consent"))
    ident = planned(manager, clip)
    warnings: list[str] = []
    assert manager._hero_matte(
        manager._load(manager._connect(), ident), clip, _hero_plan(manager, ident), 30.0, warnings
    ) == (None, None)
    assert "does not use Meta" in warnings[0]


def test_the_hero_receipt_is_derived_by_lint_from_both_models(
    manager: CampaignManager, clip: Path, tmp_path: Path
) -> None:
    ident = planned(manager, clip)
    plan = _hero_plan(manager, ident)
    request = manager.get(ident)["request"]
    stats = {"frames": 24, "window_frames": 24, "coverage": 1.0, "hidden": 0.2}
    graded = CampaignManager._grade(
        plan, request, 1.6, tmp_path, None, hero=dict(plan["edit"]["hero_word"], spoken=[0.26, 0.5]), hero_stats=stats
    )
    [check] = [item for item in graded["checks"] if item["name"] == "auto: hero word sits behind the subject"]
    assert (
        check["passed"] is True
        and check["basis"] == "derived"
        and check["by"] == "lint"
        and check["inputs"] == ["sam-3.1", "muse-spark"]
    )
    graded = CampaignManager._grade(plan, request, 1.6, tmp_path, None, hero=plan["edit"]["hero_word"], hero_stats=None)
    [check] = [item for item in graded["checks"] if item["name"] == "auto: hero word sits behind the subject"]
    assert check["passed"] is False and "no subject matte" in check["note"]
    assert not [
        item
        for item in CampaignManager._grade(plan, request, 1.6, tmp_path, None)["checks"]
        if "hero word" in item["name"]
    ]


def test_the_catalogue_lists_hero_word_choices() -> None:
    from vedit.campaign import capabilities

    choices = capabilities()["editor"]["hero_word"]
    assert {row["id"] for row in choices["fonts"]} == {"heavy", "condensed", "rounded"} and "#FFFFFF" in choices[
        "colors"
    ]
    assert choices["sides"] == ["center", "top", "left", "right"]


# --- Studio routes ---------------------------------------------------------------


def call(port: int, method: str, path: str, payload: Any = None) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        body = json.dumps(payload).encode() if payload is not None else None
        connection.request(
            method, path, body=body, headers={"Content-Type": "application/json"} if body is not None else {}
        )
        response = connection.getresponse()
        return response.status, {key.lower(): value for key, value in response.getheaders()}, response.read()
    finally:
        connection.close()


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VEDIT_WORKSPACE", str(tmp_path))
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

        get_manager().close()


def test_routes_serve_find_thumbnails_and_hero_word_suggestions(
    server: int, clip: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vedit.campaign import get_manager

    manager = get_manager()
    job = manager.create(request(clip))
    folder = manager.root / job["id"] / "meta" / "verified-find"
    folder.mkdir(parents=True)
    (folder / "bowl-200.jpg").write_bytes(b"\xff\xd8\xff\xe0jpeg")
    (manager.root / job["id"] / "meta" / "meta-usage.json").write_text("{}")
    status, headers, body = call(server, "GET", f"/api/campaigns/{job['id']}/find-thumbnail?name=bowl-200.jpg")
    assert status == 200 and headers["content-type"] == "image/jpeg" and body == b"\xff\xd8\xff\xe0jpeg"
    for name in ("..%2Fmeta-usage.json", "meta-usage.json", "missing.jpg", ""):
        assert call(server, "GET", f"/api/campaigns/{job['id']}/find-thumbnail?name={name}")[0] in {400, 404}, name
    assert call(server, "GET", f"/api/campaigns/{job['id']}/find-thumbnail")[0] == 400
    assert call(server, "POST", f"/api/campaigns/{job['id']}/find-thumbnail", {})[0] == 405

    asked: list[tuple[str, dict[str, Any]]] = []

    def suggest(self: CampaignManager, ident: str, **kwargs: Any) -> dict[str, Any]:
        asked.append((ident, kwargs))
        return {"hero_word": {"word": "oak"}, "proposal": {}, "adjusted": [], "usage": {}}

    monkeypatch.setattr(CampaignManager, "suggest_hero_word", suggest)
    status, _, body = call(
        server, "POST", f"/api/campaigns/{job['id']}/hero-word", {"subject": "the bowl", "font": "rounded"}
    )
    assert status == 200 and json.loads(body) == {
        "ok": True,
        "hero_word": {"word": "oak"},
        "proposal": {},
        "adjusted": [],
        "usage": {},
    }
    assert asked == [(job["id"], {"subject": "the bowl", "font": "rounded"})]
    assert call(server, "POST", f"/api/campaigns/{job['id']}/hero-word", {"font": 3})[0] == 400
    assert call(server, "GET", f"/api/campaigns/{job['id']}/hero-word")[0] == 405


def test_the_studio_create_route_takes_a_director(server: int, clip: Path, enabled: None) -> None:
    status, _, body = call(
        server,
        "POST",
        "/api/campaigns",
        {"paths": [str(clip)], "goal": "Show the bowl", "captions": False, "target_duration": 1, "director": "wonder"},
    )
    assert status == 202, body
    assert json.loads(body)["job"]["request"]["director"] == "wonder"
    status, _, body = call(
        server,
        "POST",
        "/api/campaigns",
        {"paths": [str(clip)], "goal": "Show the bowl", "captions": False, "target_duration": 1, "director": "llama"},
    )
    assert status == 400 and "spark or wonder" in json.loads(body)["error"]


def test_a_draft_says_what_a_hero_word_and_punch_words_send() -> None:
    from vedit.mcp_server import _meta_uses

    job = {"request": {"provider": "meta", "language": "en", "captions": True}}
    before = default_edit()
    after = dict(
        before,
        hero_word={"word": "oak", "start": 0.2, "end": 1.0, "subject": "the bowl"},
        captions=dict(before["captions"], emphasize=True, emphasis=[]),
    )
    uses, warnings = _meta_uses(job, before, after, True)
    assert (
        any("behind the hero word" in use for use in uses) and any("punch word" in use for use in uses) and not warnings
    )
    uses, warnings = _meta_uses({"request": {"provider": "local", "language": "en"}}, before, after, False)
    assert not uses and len(warnings) == 2


# --- C6: the 3D motion card ---------------------------------------------------------


def _card(**extra: Any) -> dict[str, Any]:
    return {
        "kind": "intro_title",
        "text": "Oak Bowl Start To Finish",
        "subtitle": "From Wet Blank To Table Ready",
        "style": "warm_wood",
        "palette": "oak",
        "camera": "dolly_in",
        "duration": 1.5,
        "reason": "Wood for woodwork.",
        **extra,
    }


def test_motion_card_is_designed_painted_and_kept_without_rendering(
    manager: CampaignManager, clip: Path, enabled: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vedit import blender_cards
    from vedit.media import run_ffmpeg

    plate = tmp_path / "plate.png"
    run_ffmpeg(
        ["-loglevel", "error", "-f", "lavfi", "-i", "color=c=brown:s=96x160", "-frames:v", "1", str(plate)], timeout=60
    )
    seen: dict[str, Any] = {}

    def suggest(plan: dict[str, Any], transcript: Any = None, **kwargs: Any) -> dict[str, Any]:
        seen.update(transcript=transcript, **kwargs)
        return {
            "card": _card(aspect="9:16"),
            "backdrop_prompt": "Soft oak shavings on a bench.",
            "notes": [],
            "proposal": _card(backdrop_prompt="Soft oak shavings on a bench."),
            "usage": {"cost_usd": 0.0004},
            "model": "muse-spark-1.3",
        }

    def paint(prompt: str, **kwargs: Any) -> dict[str, Any]:
        seen.update(painted=prompt, size=(kwargs["width"], kwargs["height"]), paint_dir=kwargs["work_dir"])
        return {"path": str(plate), "usage": {"cost_usd": 0.01}}

    monkeypatch.setattr(blender_cards, "blender_available", lambda: True)
    monkeypatch.setattr(blender_cards, "suggest_card", suggest)
    monkeypatch.setattr(blender_cards, "generate_backdrop", paint)
    # A card lasts at least 1.5 s, so this story is the whole 1.6 s clip.
    monkeypatch.setattr(
        meta,
        "plan_story",
        lambda *a, **k: {
            "title": "Bowl",
            "summary": "s",
            "caption": "c",
            "cover_prompt": "p",
            "hashtags": [],
            "segments": [{"start": 0.0, "end": 1.6, "label": "A", "reason": "r"}],
            "usage": {},
            "warnings": [],
        },
    )
    ident = planned(manager, clip, provider="meta", budget_usd=0.3, target_duration=1.6)
    assert manager.get(ident)["plan"]["duration"] >= 1.5
    result = manager.suggest_motion_card(ident)
    assert (
        seen["goal"] == "Show the bowl"
        and seen["kind"] == "intro_title"
        and seen["backdrop"] is True
        and seen["budget_usd"] == 0.3
    )
    assert (
        seen["work_dir"] == manager.root / ident / "meta"
        and seen["aspect"] == "9:16"
        and isinstance(seen["transcript"], list)
    )
    assert seen["painted"] == "Soft oak shavings on a bench." and seen["size"] == (1080, 1920)
    card, kept = result["motion_card"], result["backdrop"]
    assert kept, result["warnings"]
    assert card["text"] == "Oak Bowl Start To Finish" and card["backdrop"] == kept["id"] and kept["kind"] == "image"
    assert result["poster"] == f"/api/campaigns/{ident}/assets?id={kept['id']}" and result["usage"] == {
        "cost_usd": 0.0004
    }
    job = manager.get(ident)
    assert kept["id"] in job["assets"] and "path" not in job["assets"][kept["id"]], (
        "the plate is a project image; its path stays private"
    )
    assert job["plan"]["edit"]["motion_card"] is None, "a suggestion changes nothing until it is rendered"

    def broken(prompt: str, **kwargs: Any) -> Any:
        raise meta.MetaError("Meta's rate limit was reached.")

    monkeypatch.setattr(blender_cards, "generate_backdrop", broken)
    plain = manager.suggest_motion_card(ident)
    assert "backdrop" not in plain["motion_card"] and plain["backdrop"] is None and plain["poster"] is None
    assert plain["warnings"] == [
        "The backdrop was not added: Meta's rate limit was reached. The card opens over the footage."
    ]
    monkeypatch.setattr(blender_cards, "generate_backdrop", lambda *a, **k: pytest.fail("a lower third has no plate"))
    third = manager.suggest_motion_card(ident, kind="lower_third")
    assert seen["kind"] == "lower_third" and seen["backdrop"] is False and "no backdrop" in third["warnings"][0]
    for options in ({"kind": "trailer"}, {"backdrop": "yes"}):
        with pytest.raises(CampaignError) as refused:
            manager.suggest_motion_card(ident, **options)
        assert refused.value.status == 400


def test_motion_card_needs_meta_and_blender(
    manager: CampaignManager, clip: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vedit import blender_cards

    monkeypatch.setattr(blender_cards, "suggest_card", lambda *a, **k: pytest.fail("nothing may be asked"))
    ident = planned(manager, clip)
    with pytest.raises(CampaignError) as refused:
        manager.suggest_motion_card(ident)
    assert refused.value.status == 409 and "Meta mode" in str(refused.value)
    monkeypatch.setenv("MODEL_API_KEY", "test-secret-never-write-to-disk")
    monkeypatch.setenv("VEDIT_META_ENABLED", "1")
    manager._update(ident, request=dict(manager.get(ident)["request"], provider="meta"))
    monkeypatch.setattr(blender_cards, "blender_available", lambda: False)
    with pytest.raises(CampaignError) as refused:
        manager.suggest_motion_card(ident)
    assert refused.value.status == 409 and "Blender" in str(refused.value)


def test_motion_cards_show_in_capabilities_status_and_doctor(monkeypatch: pytest.MonkeyPatch) -> None:
    from vedit import blender_cards
    from vedit.campaign import capabilities
    from vedit.doctor import run_doctor

    monkeypatch.setattr(blender_cards, "blender_available", lambda: False)
    assert capabilities()["motion_cards"] is False
    monkeypatch.setattr(blender_cards, "blender_available", lambda: True)
    caps = capabilities()
    assert caps["motion_cards"] is True
    assert {"kinds", "styles", "palettes", "cameras"} <= set(caps["editor"]["motion_card"])
    monkeypatch.setattr(
        blender_cards,
        "doctor_check",
        lambda: {
            "name": "blender",
            "ok": False,
            "required": False,
            "detail": "Blender not found; 3D motion cards are hidden.",
        },
    )
    [line] = [check for check in run_doctor() if check.name == "blender"]
    assert line.ok is False and line.required is False and "hidden" in line.detail


def test_the_motion_card_route_forwards_its_choices(server: int, clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vedit.campaign import get_manager

    job = get_manager().create(request(clip))
    asked: list[tuple[str, dict[str, Any]]] = []

    def suggest(self: CampaignManager, ident: str, **kwargs: Any) -> dict[str, Any]:
        asked.append((ident, kwargs))
        return {"motion_card": {"text": "Oak"}, "proposal": {}, "usage": {}, "poster": None}

    monkeypatch.setattr(CampaignManager, "suggest_motion_card", suggest)
    status, _, body = call(
        server, "POST", f"/api/campaigns/{job['id']}/motion-card", {"kind": "end_card", "backdrop": False}
    )
    assert status == 200 and json.loads(body)["motion_card"] == {"text": "Oak"}
    status, _, body = call(server, "POST", f"/api/campaigns/{job['id']}/motion-card", {})
    assert status == 200
    assert asked == [(job["id"], {"kind": "end_card", "backdrop": False}), (job["id"], {})]
    assert call(server, "POST", f"/api/campaigns/{job['id']}/motion-card", {"backdrop": "no"})[0] == 400
    assert call(server, "GET", f"/api/campaigns/{job['id']}/motion-card")[0] == 405
    status, _, body = call(server, "GET", "/api/capabilities")
    assert isinstance(json.loads(body)["motion_cards"], bool)


# --- Private mode: Muse Glimmer on this computer ----------------------------------


def test_private_mode_reads_a_note_on_this_mac_without_meta(
    manager: CampaignManager, clip: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ident = planned(manager, clip)
    manager.render(ident, {})
    manager.process(ident)
    assert manager.get(ident)["status"] == "complete"
    with pytest.raises(CampaignError) as refused:
        manager.revise(ident, {"mode": "note", "note": "Trim the end."})
    assert refused.value.status == 409 and "private mode" in str(refused.value)

    monkeypatch.setenv("VEDIT_PRIVATE_MODEL", "glimmer")
    said: list[str] = []

    def glimmer(**kwargs: Any) -> dict[str, Any]:
        said.append(manager.get(ident)["message"])
        return {
            "patches": [
                {"op": "trim_end", "segment": 1, "seconds": 1.3, "reason": "Trim the end.", "source": "director"}
            ],
            "summary": "Shorter.",
        }

    monkeypatch.setattr(meta, "patch_plan", glimmer)
    manager.revise(ident, {"mode": "note", "note": "Trim the end."})
    manager.process(ident)
    done = manager.get(ident)
    assert done["status"] == "complete", done["error"]
    assert said == [f"Asking Muse Glimmer on {meta.this_computer()} to turn your note into changes to the story."]
    assert done["revision"] == 2 and done["result"]["proof"]["has_video"] is True


def test_capabilities_say_whether_private_mode_is_on(monkeypatch: pytest.MonkeyPatch) -> None:
    assert meta.capabilities()["private"] == {
        "enabled": False,
        "model": "meta-models/Muse-Glimmer-30B",
        "runs_on": meta.this_computer() + " (llama.cpp)",
    }
    monkeypatch.setenv("VEDIT_PRIVATE_MODEL", "glimmer")
    assert meta.capabilities()["private"]["enabled"] is True
    monkeypatch.setenv("VEDIT_PRIVATE_MODEL", "maybe")
    status = meta.capabilities()["private"]
    assert status["enabled"] is False and "must be 'glimmer'" in status["error"]
