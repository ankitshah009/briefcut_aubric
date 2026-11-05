"""Long sources (10 minutes to 2 hours): planned in windows, picked by one editor call, cut into shorts.

Meta is stubbed at its boundary. Muse Spark's per-window answer is a stub of
``meta.plan_candidates``; the editor calls run the real ``edit_long_story`` and
``pick_long_shorts`` against a fake ``meta._request``, so their id checks,
schema and ledger are the shipped code.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from vedit import campaign as campaign_module
from vedit import meta
from vedit.campaign import CampaignManager
from vedit.captions import Cue
from vedit.media import resolve_ffmpeg
from vedit.story import long_windows, sentences_from_transcript

SOURCE_SECONDS = 2700.0  # 45 minutes
CUE_EVERY = 6.0


def _cues() -> list[Cue]:
    """One spoken sentence every six seconds, five seconds long, for the whole source."""
    count = int(SOURCE_SECONDS // CUE_EVERY)
    return [
        Cue(index * CUE_EVERY, index * CUE_EVERY + 5.0, f"Point number {index} is explained here.")
        for index in range(count)
    ]


@pytest.fixture(scope="module")
def long_clip(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A 45-minute source made cheaply: a tiny colour picture and a sine tone, a few frames a second."""
    try:
        ffmpeg = resolve_ffmpeg()
    except Exception:
        pytest.skip("ffmpeg required")
    out = tmp_path_factory.mktemp("long") / "talk-45min.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=teal:s=160x90:r=5:d={SOURCE_SECONDS}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate=16000:duration={SOURCE_SECONDS}",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-g",
            "50",
            "-c:a",
            "aac",
            "-b:a",
            "24k",
            "-shortest",
            "-movflags",
            "+faststart",
            str(out),
        ],
        check=True,
    )
    return out


@pytest.fixture
def clip(tmp_path: Path, long_clip: Path) -> Path:
    inbox = tmp_path / "vedit-sessions" / "source-test" / "inbox"
    inbox.mkdir(parents=True)
    target = inbox / long_clip.name
    shutil.copyfile(long_clip, target)
    return target


@pytest.fixture
def manager(tmp_path: Path):
    instance = CampaignManager(tmp_path, start_worker=False)
    try:
        yield instance
    finally:
        instance.close()


class Http:
    """Meta's HTTP boundary for the text-only editor calls. It answers with ids it reads from the prompt."""

    def __init__(self, *, unknown: bool = False) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.unknown = unknown
        self.lock = threading.Lock()

    def __call__(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        with self.lock:
            self.calls.append((method, path, kwargs))
        payload = kwargs.get("payload") or {}
        if method == "POST" and path == "/responses/input_tokens":
            return {"input_tokens": 9_000}
        if method == "POST" and path == "/responses":
            prompt = payload["input"][0]["content"][0]["text"]
            ids = re.findall(r"^(w\d+-\d+) \[", prompt, flags=re.MULTILINE)
            name = payload["text"]["format"]["name"]
            story = {
                "title": "The long talk",
                "summary": "Four points.",
                "caption": "",
                "cover_prompt": "The speaker.",
                "hashtags": ["talk"],
            }
            if name == "vedit_long_edit":
                picks = [ids[0], ids[7], ids[14], ids[-1]] if len(ids) > 15 else [ids[0], ids[-1]]
                if self.unknown:
                    picks = [ids[0], "w99-1"]
                answer = dict(story, picks=[{"id": ident, "why": "It moves the story on."} for ident in picks])
            elif name == "vedit_long_shorts":
                pairs = [(ids[1], ids[2]), (ids[4], ids[5]), (ids[8], ids[9])]
                answer = {
                    "shorts": [
                        dict(
                            story,
                            title=f"Short {number}",
                            hook="A strong first line.",
                            picks=[{"id": first, "why": "Hook."}, {"id": second, "why": "Payoff."}],
                        )
                        for number, (first, second) in enumerate(pairs, 1)
                    ]
                }
            else:
                pytest.fail(f"Unexpected schema {name}")
            return {
                "id": "resp_test",
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(answer)}]}],
                "usage": {"input_tokens": 9_000, "output_tokens": 900},
            }
        pytest.fail(f"Unexpected provider request: {method} {path}")

    def paid(self) -> int:
        return sum(method == "POST" and path == "/responses" for method, path, _ in self.calls)


@pytest.fixture
def stubbed(monkeypatch: pytest.MonkeyPatch, manager: CampaignManager) -> dict[str, Any]:
    """Meta enabled, transcript and measurements stubbed, window proxies recorded instead of encoded."""
    monkeypatch.setenv("MODEL_API_KEY", "test-secret-never-write-to-disk")
    monkeypatch.setenv("VEDIT_META_ENABLED", "1")
    monkeypatch.setenv("VEDIT_META_MODE", "standard")
    state: dict[str, Any] = {"heard": 0, "windows": [], "candidate_calls": [], "messages": [], "http": Http()}
    cues = _cues()

    def transcribe(source: Path, work: Path, **kwargs: Any) -> list[Cue]:
        state["heard"] += 1
        return list(cues)

    def analyze_source(source: Path, work_dir: Path, **kwargs: Any) -> dict[str, Any]:
        heard = kwargs.get("heard") or []
        return {
            "duration": SOURCE_SECONDS,
            "has_audio": True,
            "scenes": [float(value) for value in range(97, int(SOURCE_SECONDS), 97)],
            "transcribed": True,
            "silences": [],
            "warnings": [],
            "transcript": [{"start": cue.start, "end": cue.end, "text": cue.text} for cue in heard],
        }

    def proxy(
        root: Path, source: Path, info: Any, assembly: Any = None, *, cancel_event: Any = None, window: Any = None
    ) -> Path:
        assert window is not None, "a long source is never sent as one proxy"
        state["windows"].append(tuple(window))
        path = root / "analysis" / f"window-{window[0]:.0f}" / "analysis.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"proxy")
        return path

    def plan_candidates(source: Path, *, offset: float, duration: float, **kwargs: Any) -> dict[str, Any]:
        """Six sentence-aligned candidates per window, on the source clock, as the real call returns them."""
        state["candidate_calls"].append((offset, duration))
        if state.get("on_candidates"):
            state["on_candidates"](len(state["candidate_calls"]))
        first = int(-(-offset // CUE_EVERY))
        moments = []
        for step in range(6):
            index = first + step * 10
            start, end = index * CUE_EVERY, (index + 1) * CUE_EVERY + 5.0
            if end <= offset + duration:
                moments.append(
                    {
                        "start": start,
                        "end": end,
                        "label": f"Point {index}",
                        "reason": f"Point {index} and {index + 1} are said.",
                        "score": 0.6 + step * 0.05,
                    }
                )
        return {
            "moments": moments,
            "provider": "meta",
            "model": "muse-spark-1.3",
            "usage": {"cost_usd": 0.05},
            "warnings": [],
        }

    original_update = manager._update

    def update(ident: str, **changes: Any) -> dict[str, Any]:
        if "message" in changes:
            state["messages"].append(changes["message"])
        return original_update(ident, **changes)

    monkeypatch.setattr(campaign_module, "transcribe_speech", transcribe)
    monkeypatch.setattr("vedit.story.analyze_source", analyze_source)
    monkeypatch.setattr(manager, "_analysis_proxy", proxy)
    monkeypatch.setattr(manager, "_update", update)
    monkeypatch.setattr(meta, "plan_candidates", plan_candidates)
    monkeypatch.setattr(meta, "_request", state["http"])
    return state


def _request(clip: Path, **overrides: Any) -> dict[str, Any]:
    return {
        "paths": [str(clip)],
        "goal": "Cut the four strongest points of this talk into one story.",
        "target_duration": 60,
        "captions": False,
        "provider": "meta",
        "budget_usd": 5,
        **overrides,
    }


def _boundaries() -> set[float]:
    sentences = sentences_from_transcript(
        [{"start": cue.start, "end": cue.end, "text": cue.text} for cue in _cues()], duration=SOURCE_SECONDS
    )
    return {round(float(sentence[key]), 2) for sentence in sentences for key in ("start", "end")}


def _candidates(manager: CampaignManager, ident: str) -> list[dict[str, Any]]:
    [path] = (manager.root / ident / "long-form").glob("candidates-*.json")
    return json.loads(path.read_text(encoding="utf-8"))["candidates"]


def test_the_window_length_is_published() -> None:
    from vedit.campaign import capabilities

    assert capabilities()["limits"]["meta_window_seconds"] == meta.WINDOW_SECONDS == 600
    assert capabilities()["limits"]["source_seconds"] == 7200


def test_windows_cover_the_source_in_parts_of_at_most_ten_minutes() -> None:
    sentences = [{"start": cue.start, "end": cue.end} for cue in _cues()]
    for duration in (601.0, 2700.0, 7200.0):
        windows = long_windows(duration, sentences=[item for item in sentences if item["end"] < duration], longest=600)
        assert windows[0][0] == 0 and windows[-1][1] == round(duration, 3)
        assert all(high - low <= 600 + 1e-6 for low, high in windows)
        assert all(windows[index][1] == windows[index + 1][0] for index in range(len(windows) - 1))
    # With no punctuation and no transcript, a measured pause or a scene cut still gives the cut.
    assert long_windows(900, pauses=[470.0]) == [(0.0, 470.0), (470.0, 900.0)]
    assert long_windows(900, scenes=[430.0]) == [(0.0, 430.0), (430.0, 900.0)]
    assert long_windows(500) == [(0.0, 500.0)]


def test_a_45_minute_source_plans_across_windows_and_keeps_to_candidates(
    manager: CampaignManager, clip: Path, stubbed: dict[str, Any]
) -> None:
    job = manager.create(_request(clip))
    manager.process(job["id"])
    planned = manager.get(job["id"])
    assert planned["status"] == "planned", planned["error"]
    plan = planned["plan"]
    # Transcribed once; watched in five windows of at most ten minutes, each its own proxy.
    assert stubbed["heard"] == 1
    windows = sorted(stubbed["windows"])
    assert len(windows) == 5 and windows[0][0] == 0 and windows[-1][1] == SOURCE_SECONDS
    assert all(high - low <= meta.WINDOW_SECONDS for low, high in windows)
    assert sorted(stubbed["candidate_calls"]) == [(low, round(high - low, 3)) for low, high in windows]
    # Every window cut lies on a sentence boundary.
    assert all(round(low, 2) in _boundaries() for low, _ in windows[1:])
    # Progress names each window on the source clock.
    assert any(re.fullmatch(r"Planning 1 of 5: 0:00 to \d+:\d\d", message) for message in stubbed["messages"])
    assert any(message.startswith("Planning 5 of 5: ") for message in stubbed["messages"])
    # One editor call over the whole transcript picked real candidates only, in its order, with candidate times.
    assert stubbed["http"].paid() == 1
    candidates = {(item["start"], item["end"]) for item in _candidates(manager, job["id"])}
    assert len(candidates) == 30
    assert [(segment["start"], segment["end"]) in candidates for segment in plan["segments"]] == [True] * 4
    assert [segment["start"] for segment in plan["segments"]] == sorted(
        segment["start"] for segment in plan["segments"]
    )
    prompt = stubbed["http"].calls[-1][2]["payload"]["input"][0]["content"][0]["text"]
    assert "Point number 449 is explained here." in prompt  # the editor read the transcript to the very end
    # The cut keeps to the target and to whole sentences.
    assert plan["duration"] <= 60 + 0.05
    edges = _boundaries()
    assert all(
        round(segment["start"], 2) in edges and round(segment["end"], 2) in edges for segment in plan["segments"]
    )
    assert any(
        "in 5 parts of up to 10 minutes" in note and "30 candidate moments" in note
        for note in plan["analysis"]["notes"]
    )
    # The whole transcript stays on the plan, for find and captions.
    assert len(plan["analysis"]["transcript"]) == len(_cues())
    summary = json.loads((manager.root / job["id"] / "long-form" / "plan-summary.json").read_text(encoding="utf-8"))
    assert summary["candidates"] == 30 and summary["editor"] == "muse-spark" and len(summary["picked"]) == 4
    # The editor's call is on the project's ledger, settled at what Meta billed.
    calls = json.loads((manager.root / job["id"] / "meta" / "meta-usage.json").read_text(encoding="utf-8"))["calls"]
    edit = [call for call in calls if call["operation"] == "edit"]
    assert len(edit) == 1 and edit[0]["status"] == "completed" and edit[0]["usage"]["input_tokens"] == 9_000
    # Find reads the whole transcript of a long source, and uploads nothing.
    before = len(stubbed["http"].calls)
    found = manager.find(job["id"], {"query": "Point number 400"})
    assert found["searched"] == ["transcript"] and len(stubbed["http"].calls) == before
    assert any(2390 <= moment["start"] <= 2410 for moment in found["moments"])
    assert any("read its whole transcript" in warning for warning in found["warnings"])


def test_the_editor_cannot_pick_a_moment_that_is_not_a_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MODEL_API_KEY", "test-secret-never-write-to-disk")
    monkeypatch.setenv("VEDIT_META_ENABLED", "1")
    http = Http(unknown=True)
    monkeypatch.setattr(meta, "_request", http)
    candidates = [
        {
            "id": f"w1-{index}",
            "start": index * 20.0,
            "end": index * 20.0 + 10,
            "label": f"Point {index}",
            "reason": "Said.",
            "score": 0.7,
        }
        for index in range(1, 6)
    ]
    with pytest.raises(meta.MetaError, match="w99-1.*not one of the candidate moments"):
        meta.edit_long_story(
            goal="The best points.",
            duration=1200,
            target_duration=60,
            candidates=candidates,
            work_dir=tmp_path / "meta",
            budget_usd=1,
        )
    [call] = json.loads((tmp_path / "meta" / "meta-usage.json").read_text(encoding="utf-8"))["calls"]
    assert call["operation"] == "edit" and call["status"] == "failed_or_incomplete"  # billed, so it stays on the budget
    # A good answer takes its times from the candidates, never from the model.
    good = Http()
    monkeypatch.setattr(meta, "_request", good)
    story = meta.edit_long_story(
        goal="The best points.",
        duration=1200,
        target_duration=60,
        candidates=candidates,
        work_dir=tmp_path / "meta2",
        budget_usd=1,
    )
    assert [(segment["start"], segment["end"]) for segment in story["segments"]] == [(20.0, 30.0), (100.0, 110.0)]
    assert story["editor"] == "muse-spark" and story["model"] == "muse-spark-1.3"
    # A long video repeats itself; the length is a ceiling, so the editor is told never to pad with a repeated beat.
    prompt = good.calls[-1][2]["payload"]["input"][0]["content"][0]["text"]
    assert "ceiling, not a goal" in prompt and "same beat" in prompt


def test_the_editor_can_be_muse_wonder_at_wonder_prices(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_API_KEY", "test-secret-never-write-to-disk")
    monkeypatch.setenv("VEDIT_META_ENABLED", "1")
    http = Http()
    monkeypatch.setattr(meta, "_request", http)
    candidates = [
        {
            "id": f"w{window}-{index}",
            "start": window * 600 + index * 20.0,
            "end": window * 600 + index * 20.0 + 10,
            "label": "Point",
            "reason": "Said.",
            "score": 0.7,
        }
        for window in range(1, 4)
        for index in range(1, 6)
    ]
    story = meta.edit_long_story(
        goal="The best points.",
        duration=2400,
        target_duration=60,
        candidates=candidates,
        work_dir=tmp_path / "meta",
        budget_usd=2,
        director="wonder",
    )
    assert story["editor"] == "muse-wonder" and story["model"] == meta._WONDER_MODEL
    assert http.calls[-1][2]["payload"]["model"] == meta._WONDER_MODEL
    assert story["usage"]["cost_usd"] == pytest.approx(
        9_000 * meta._WONDER_INPUT_PRICE + 900 * meta._WONDER_OUTPUT_PRICE
    )


def test_a_long_source_the_budget_cannot_cover_is_refused_with_the_amount(
    manager: CampaignManager, clip: Path, stubbed: dict[str, Any]
) -> None:
    job = manager.create(_request(clip, budget_usd=0.25))
    manager.process(job["id"])
    failed = manager.get(job["id"])
    assert failed["status"] == "failed"
    estimate = meta.estimate_long_plan(SOURCE_SECONDS, 5)
    assert estimate["expected_usd"] > 0.25
    assert f"expected to cost about ${estimate['expected_usd']:.2f}" in failed["error"]
    assert "The project budget is $0.25." in failed["error"] and "Raise the budget to at least $" in failed["error"]
    assert "Nothing was sent to Meta." in failed["error"]
    # Refused before anything was heard, encoded or uploaded.
    assert stubbed["heard"] == 0 and stubbed["windows"] == [] and stubbed["http"].calls == []


def test_cancel_stops_new_windows_and_keeps_the_finished_ones(
    manager: CampaignManager, clip: Path, stubbed: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(campaign_module, "_LONG_POOL", 1)
    job = manager.create(_request(clip))
    stubbed["on_candidates"] = lambda count: manager.cancel(job["id"]) if count == 2 else None
    manager.process(job["id"])
    cancelled = manager.get(job["id"])
    assert cancelled["status"] == "cancelled", cancelled["error"]
    assert len(stubbed["candidate_calls"]) == 2  # windows 3 to 5 never started
    assert stubbed["http"].paid() == 0  # the editor never ran
    saved = sorted(path.name.split("-")[1] for path in (manager.root / job["id"] / "long-form").glob("window-*.json"))
    assert saved == ["1", "2"]  # a retry reads only the windows that did not finish


def test_shorts_from_a_long_source_reuse_the_candidates_and_never_overlap(
    manager: CampaignManager, clip: Path, stubbed: dict[str, Any]
) -> None:
    job = manager.create(_request(clip))
    manager.process(job["id"])
    parent = manager.get(job["id"])
    assert parent["status"] == "planned", parent["error"]
    watched = len(stubbed["candidate_calls"])
    shorts = manager.shorts(job["id"], {"count": 3, "target_duration": 30, "auto_render": False})
    for short in shorts:
        manager.process(short["id"])
    plans = [manager.get(short["id"]) for short in shorts]
    assert [item["status"] for item in plans] == ["planned"] * 3, [item["error"] for item in plans]
    # No window was watched again: the shorts read the parent's candidates, with one more text call for all three.
    assert len(stubbed["candidate_calls"]) == watched
    assert stubbed["http"].paid() == 2
    candidates = {(item["start"], item["end"]) for item in _candidates(manager, job["id"])}
    ranges = [(segment["start"], segment["end"]) for item in plans for segment in item["plan"]["segments"]]
    ranges += [(segment["start"], segment["end"]) for segment in parent["plan"]["segments"]]
    assert all(pair in candidates for pair in ranges)
    ranges.sort()
    assert all(ranges[index][1] <= ranges[index + 1][0] for index in range(len(ranges) - 1)), ranges
    assert all(15 <= item["plan"]["duration"] <= 30 + 0.05 for item in plans)
    assert len({item["plan"]["title"] for item in plans}) == 3
