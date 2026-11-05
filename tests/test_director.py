"""The director's loop: patches, auto-refine, find, shorts, and script to video."""

from __future__ import annotations

import base64
import copy
import http.client
import importlib.util
import json
import math
import random
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from vedit import director, meta
from vedit.campaign import CampaignError, CampaignManager, capabilities, validate_plan
from vedit.edit_spec import default_edit, validate_edit
from vedit.lint import MAX_MOMENTS, MIN_SEGMENT, apply_patches
from vedit.media import probe, run_ffmpeg
from vedit.studio.server import StudioHandler, _LoopbackServer
from vedit.timeline import MAX_SPEED, MIN_SPEED, plan_timeline

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j6XkAAAAASUVORK5CYII=")


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.delenv("VEDIT_META_ENABLED", raising=False)
    monkeypatch.delenv("VEDIT_META_MODE", raising=False)

    def unexpected(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Unexpected provider HTTP call")

    monkeypatch.setattr(meta, "_request", unexpected)


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_API_KEY", "test-secret-never-write-to-disk")
    monkeypatch.setenv("VEDIT_META_ENABLED", "1")
    # Meta-mode plans hear the source with Meta Voice Transcribe; these tests are about other calls.
    monkeypatch.setattr(
        meta, "transcribe_audio", lambda wav, **kwargs: {"cues": [], "provider": "meta", "usage": {"cost_usd": 0}}
    )


def story(*ranges: tuple[float, float], **extra: Any) -> dict[str, Any]:
    return {
        "title": "Bowl",
        "segments": [
            {"start": lo, "end": hi, "label": f"Moment {index}", "reason": "Chosen."}
            for index, (lo, hi) in enumerate(ranges, 1)
        ],
        "hashtags": [],
        "caption": "",
        **extra,
    }


# --- Patch vocabulary -----------------------------------------------------------


def test_a_note_about_look_size_and_speed_becomes_structured_changes() -> None:
    plan = story((0.0, 5.0), (6.0, 12.0))
    fixed, applied = apply_patches(
        plan,
        [
            {"op": "set_edit", "path": "look.preset", "value": "warm", "reason": "warmer", "source": "director"},
            {"op": "set_edit", "path": "captions.scale", "value": 1.4, "reason": "bigger", "source": "director"},
            {"op": "set_speed", "segment": 2, "value": 1.5, "reason": "faster", "source": "director"},
        ],
        duration=20,
    )
    assert [item["skipped"] for item in applied] == [None, None, None]
    assert fixed["edit"]["look"]["preset"] == "warm" and fixed["edit"]["captions"]["scale"] == 1.4
    assert fixed["segments"][1]["speed"] == 1.5
    assert fixed["duration"] == 9.0, "the story length comes from the one clock"
    assert [item["summary"] for item in applied] == [
        "Look: Warm (was Original).",
        "Caption size: 1.4× (was 1×).",
        "Moment 2 plays faster (1.5×).",
    ]
    assert "edit" not in plan and "speed" not in plan["segments"][1], "the input plan is untouched"
    assert validate_plan(fixed, 20, original=plan)["edit"]["look"]["preset"] == "warm"


@pytest.mark.parametrize(
    "path,value,reason",
    [
        ("look.preset", "sepia", "not one of the available choices"),
        ("look.intensity", 3, "between"),
        ("transition.type", "explode", "not one of the available choices"),
        ("transition.seconds", 9, "between"),
        ("captions.scale", 7, "between"),
        ("zoom.mode", 1, "not one of the available choices"),
        ("trim_pauses", "yes", "true or false"),
        ("fade.in", -1, "between"),
        ("texts", {"text": ""}, "empty"),
        ("music.volume_db", -6, "not in use"),
    ],
)
def test_a_bad_setting_is_skipped_and_reported_never_raised(path: str, value: Any, reason: str) -> None:
    plan = story((0.0, 5.0))
    fixed, applied = apply_patches(plan, [{"op": "set_edit", "path": path, "value": value}], duration=20)
    assert reason in applied[0]["skipped"]
    assert "edit" not in fixed


@pytest.mark.parametrize(
    "path", ["export.format", "cutout.mode", "music", "__class__", "look", "../../etc", "", None, 3, ["look.preset"]]
)
def test_only_allowlisted_paths_can_be_set(path: Any) -> None:
    _, applied = apply_patches(story((0.0, 5.0)), [{"op": "set_edit", "path": path, "value": "warm"}], duration=20)
    assert applied[0]["skipped"] == "that setting cannot be changed by a patch"


def test_a_setting_already_in_place_changes_nothing() -> None:
    plan = validate_plan(story((0.0, 5.0)), 20)
    fixed, applied = apply_patches(plan, [{"op": "set_edit", "path": "look.preset", "value": "none"}], duration=20)
    assert applied[0]["skipped"] == "already set that way" and fixed["edit"] == plan["edit"]


def test_text_is_appended_and_stays_on_the_story() -> None:
    plan = story((0.0, 5.0))
    fixed, applied = apply_patches(
        plan,
        [
            {
                "op": "set_edit",
                "path": "texts",
                "value": {"text": "From raw oak", "start": 0.0, "duration": 2.0, "style": "title", "position": "top"},
            },
            {
                "op": "set_edit",
                "path": "texts",
                "value": {"text": "Too late", "start": 9.0, "duration": 2.0, "style": "label", "position": "center"},
            },
        ],
        duration=20,
    )
    assert fixed["edit"]["texts"][0]["text"] == "From raw oak" and len(fixed["edit"]["texts"]) == 1
    assert applied[0]["summary"] == "Add the text “From raw oak” at 0.0 s."
    assert applied[1]["skipped"] == "that time is past the end of the story"


@pytest.mark.parametrize(
    "value,skipped",
    [
        (5.0, "speed must be between"),
        (0.1, "speed must be between"),
        (None, "missing speed"),
        ("fast", "missing speed"),
        (math.nan, "missing speed"),
        (True, "missing speed"),
        (1.0, "already at that speed"),
    ],
)
def test_speed_bounds(value: Any, skipped: str) -> None:
    fixed, applied = apply_patches(story((0.0, 5.0)), [{"op": "set_speed", "segment": 1, "value": value}], duration=20)
    assert skipped in applied[0]["skipped"] and "speed" not in fixed["segments"][0]


def test_speed_that_would_run_past_the_story_limit_is_skipped() -> None:
    fixed, applied = apply_patches(story((0.0, 100.0)), [{"op": "set_speed", "segment": 1, "value": 0.5}], duration=120)
    assert applied[0]["skipped"] == "would run past 180 s" and "speed" not in fixed["segments"][0]


def test_split_lands_on_a_sentence_edge_and_later_patches_reach_the_half_they_mean() -> None:
    sentences = [{"start": 6.0, "end": 9.0, "text": "One."}, {"start": 9.0, "end": 12.0, "text": "Two."}]
    plan = story((0.0, 5.0), (6.0, 12.0), analysis={"sentences": sentences})
    fixed, applied = apply_patches(
        plan,
        [
            # Sent in the wrong order on purpose: splits run first.
            {"op": "set_speed", "segment": 2, "seconds": 11.0, "value": 1.5},
            {"op": "split", "segment": 2, "seconds": 8.8},
        ],
        duration=20,
    )
    assert [(s["start"], s["end"], s.get("speed")) for s in fixed["segments"]] == [
        (0.0, 5.0, None),
        (6.0, 9.0, None),
        (9.0, 12.0, 1.5),
    ]
    assert applied[0]["summary"] == "Split moment 2 at 9.0 s."
    assert fixed["segments"][2]["label"].endswith("(continued)")
    assert fixed["duration"] == pytest.approx(5 + 3 + 3 / 1.5)


def test_split_refuses_slivers_and_a_full_story() -> None:
    _, applied = apply_patches(
        story((0.0, 5.0)),
        [
            {"op": "split", "segment": 1, "seconds": 0.2},
            {"op": "split", "segment": 1, "seconds": 9.0},
            {"op": "split", "segment": 1},
        ],
        duration=20,
    )
    assert [item["skipped"] for item in applied] == ["each half must last at least 0.5 s"] * 2 + ["missing seconds"]
    full = story(*[(float(index), index + 0.9) for index in range(MAX_MOMENTS)])
    _, applied = apply_patches(full, [{"op": "split", "segment": 1, "seconds": 0.5}], duration=60)
    assert applied[0]["skipped"] == f"a story holds at most {MAX_MOMENTS} moments"


def test_split_halves_move_and_disappear_together() -> None:
    plan = story((0.0, 4.0), (6.0, 12.0), (14.0, 16.0))
    fixed, _ = apply_patches(
        plan,
        [{"op": "split", "segment": 2, "seconds": 9.0}, {"op": "move_before", "segment": 2, "seconds": 1}],
        duration=20,
    )
    assert [(s["start"], s["end"]) for s in fixed["segments"]] == [(6.0, 9.0), (9.0, 12.0), (0.0, 4.0), (14.0, 16.0)]
    fixed, _ = apply_patches(
        plan, [{"op": "split", "segment": 2, "seconds": 9.0}, {"op": "remove", "segment": 2}], duration=20
    )
    assert [(s["start"], s["end"]) for s in fixed["segments"]] == [(0.0, 4.0), (14.0, 16.0)]
    fixed, _ = apply_patches(
        plan,
        [
            {"op": "split", "segment": 2, "seconds": 9.0},
            {"op": "trim_end", "segment": 2, "seconds": 11.0},
            {"op": "trim_start", "segment": 2, "seconds": 7.0},
        ],
        duration=20,
    )
    assert [(s["start"], s["end"]) for s in fixed["segments"]] == [(0.0, 4.0), (7.0, 9.0), (9.0, 11.0), (14.0, 16.0)]


def test_a_whole_moment_changes_speed_after_a_split() -> None:
    plan = story((0.0, 4.0), (6.0, 12.0), (14.0, 16.0))
    fixed, applied = apply_patches(
        plan,
        [{"op": "split", "segment": 2, "seconds": 9.0}, {"op": "set_speed", "segment": 2, "value": 2.0}],
        duration=20,
    )
    assert [(s["start"], s["end"], s.get("speed")) for s in fixed["segments"]] == [
        (0.0, 4.0, None),
        (6.0, 9.0, 2.0),
        (9.0, 12.0, 2.0),
        (14.0, 16.0, None),
    ]
    assert all("plays at 2× speed" in s["reason"] for s in fixed["segments"][1:3])
    assert applied[1]["skipped"] is None and fixed["duration"] == pytest.approx(4 + 3 + 2)
    # A source time still picks one half.
    fixed, _ = apply_patches(
        plan,
        [
            {"op": "split", "segment": 2, "seconds": 9.0},
            {"op": "set_speed", "segment": 2, "seconds": 10.0, "value": 2.0},
        ],
        duration=20,
    )
    assert [s.get("speed") for s in fixed["segments"]] == [None, None, 2.0, None]
    # When the whole moment would run the story past its limit, neither half keeps the speed.
    long = story((0.0, 100.0), (110.0, 170.0))
    fixed, applied = apply_patches(
        long,
        [{"op": "split", "segment": 1, "seconds": 50.0}, {"op": "set_speed", "segment": 1, "value": 0.5}],
        duration=200,
    )
    assert applied[1]["skipped"] == "would run past 180 s" and not any("speed" in s for s in fixed["segments"])
    assert fixed["duration"] == 160.0


def test_a_move_after_a_split_is_noted_on_the_first_half() -> None:
    # Moment 2's second half spans 2.5 to 4 s, so a target moment number of 3 must not be read as a time inside it.
    plan = story((0.0, 1.0), (1.5, 4.0), (5.0, 6.0))
    fixed, applied = apply_patches(
        plan,
        [{"op": "split", "segment": 2, "seconds": 2.5}, {"op": "move_before", "segment": 2, "seconds": 3}],
        duration=20,
    )
    assert [(s["start"], s["end"]) for s in fixed["segments"]] == [(0.0, 1.0), (1.5, 2.5), (2.5, 4.0), (5.0, 6.0)]
    assert applied[1]["skipped"] is None and applied[1]["before"] == {"start": 1.5, "end": 2.5}
    assert (
        "moved before moment 3" in fixed["segments"][1]["reason"]
        and "moved before" not in fixed["segments"][2]["reason"]
    )


def test_a_split_keeps_a_move_continuous() -> None:
    plan = story((0.0, 4.0))
    plan["segments"][0]["motion"] = {
        "from": {"scale": 1.0, "x": 0.5, "y": 0.5},
        "to": {"scale": 2.0, "x": 0.5, "y": 0.5},
    }
    fixed, _ = apply_patches(plan, [{"op": "split", "segment": 1, "seconds": 1.0}], duration=20)
    first, second = fixed["segments"]
    assert first["motion"]["to"]["scale"] == second["motion"]["from"]["scale"] == 1.25
    assert first["motion"]["from"]["scale"] == 1.0 and second["motion"]["to"]["scale"] == 2.0


def _random_patch(rng: random.Random, moments: int) -> Any:
    op = rng.choice(["trim_start", "trim_end", "remove", "move_before", "set_edit", "set_speed", "split", "explode"])
    segment = rng.choice([rng.randint(1, moments), rng.randint(-2, moments + 3), None, True, "1"])
    seconds = rng.choice([rng.uniform(-2, 70), rng.randint(1, moments), None, math.inf, math.nan, "3"])
    if op == "set_edit":
        path = rng.choice([*director.EDIT_FIELDS, "export.format", "look", None])
        value = rng.choice(
            [
                True,
                False,
                "warm",
                "fade",
                "boxed",
                "#FFD60A",
                rng.uniform(-3, 9),
                None,
                {"text": "Hello", "start": rng.uniform(0, 40), "duration": 2.0},
                {"text": ""},
                [1, 2],
            ]
        )
        return {"op": op, "path": path, "value": value, "source": "director"}
    if op == "set_speed":
        return {
            "op": op,
            "segment": segment,
            "value": rng.choice([rng.uniform(0.1, 6), 1, None, "x", math.nan]),
            "seconds": seconds,
            "source": "director",
        }
    return {"op": op, "segment": segment, "seconds": seconds, "source": rng.choice(["director", "auto"])}


@pytest.mark.parametrize("seed", range(60))
def test_patches_never_raise_and_never_break_the_plan(seed: int) -> None:
    rng = random.Random(seed)
    edges = sorted(rng.sample(range(60), 2 * rng.randint(1, 6)))
    ranges = [(float(edges[i]), float(edges[i + 1])) for i in range(0, len(edges), 2)]
    sentences = [{"start": float(t), "end": float(t + 3), "text": "A sentence."} for t in range(0, 60, 3)]
    plan = validate_plan(
        story(*ranges), 60, original={"analysis": {"sentences": sentences, "speech": [{"start": 0.0, "end": 60.0}]}}
    )
    before = copy.deepcopy(plan)
    patches = [_random_patch(rng, len(ranges)) for _ in range(rng.randint(1, 24))] + ["not a patch", None, 3]
    fixed, applied = apply_patches(plan, patches, duration=60)
    assert plan == before, "the input plan is never mutated"
    assert len(applied) == sum(1 for patch in patches if isinstance(patch, dict)), (
        "every patch is reported exactly once"
    )
    assert all(isinstance(item["summary"], str) and item["summary"] for item in applied)
    segments = fixed["segments"]
    assert 1 <= len(segments) <= MAX_MOMENTS
    for segment in segments:
        assert (
            0 <= segment["start"] and segment["end"] <= 60 and segment["end"] - segment["start"] >= MIN_SEGMENT - 1e-6
        )
        assert MIN_SPEED <= segment.get("speed", 1) <= MAX_SPEED and "keep" not in segment
    placed = sorted((s["start"], s["end"]) for s in segments)
    assert all(left[1] <= right[0] + 1e-6 for left, right in zip(placed, placed[1:])), "moments never overlap"
    assert validate_edit(fixed["edit"], duration=86_400.0) == fixed["edit"]
    assert fixed["duration"] == plan_timeline(fixed)["duration"] <= 180
    assert validate_plan(fixed, 60, original=plan)["duration"] == fixed["duration"]


def test_every_field_describes_itself_in_plain_words() -> None:
    edit = default_edit()
    samples = {"number": 0.5, "flag": True, "color": "#FFD60A"}
    for field in director.EDIT_FIELDS.values():
        value = (
            {"text": "Hi", "start": 1.0}
            if field.kind == "append"
            else field.choices[0]
            if field.kind == "choice"
            else samples[field.kind]
        )
        words = director.describe_patch(
            {"op": "set_edit", "path": field.path, "value": value, "before": director.read_edit(edit, field.path)}
        )
        assert words.endswith(".") and "_" not in words and field.path not in words, field.path


def test_model_values_are_coerced_only_when_unambiguous() -> None:
    fields = director.EDIT_FIELDS
    assert director.coerce_edit_value(fields["captions.scale"], "1.3") == 1.3
    assert director.coerce_edit_value(fields["captions.scale"], "big") is None
    assert director.coerce_edit_value(fields["trim_pauses"], "true") is True
    assert director.coerce_edit_value(fields["look.preset"], "sepia") is None
    assert director.coerce_edit_value(fields["captions.color"], "#ffd60a") == "#FFD60A"
    assert director.coerce_edit_value(fields["captions.color"], "red") is None
    placed = director.coerce_edit_value(
        fields["texts"], "From raw oak", seconds=1.5, text={"duration": 2.0, "style": "title", "position": "top"}
    )
    assert placed == {"text": "From raw oak", "start": 1.5, "duration": 2.0, "style": "title", "position": "top"}
    assert director.coerce_edit_value(fields["texts"], "   ") is None
    # A model may send a list or a dict where a word belongs; the patch keeps the defaults instead of crashing a billed answer.
    odd = director.coerce_edit_value(
        fields["texts"], "From raw oak", text={"style": ["title"], "position": {"top": True}}
    )
    assert odd["style"] == director.TEXT_DEFAULTS["style"] and odd["position"] == director.TEXT_DEFAULTS["position"]
    assert (
        director.coerce_edit_value(fields["texts"], "From raw oak", text={"style": None, "position": 3})["style"]
        == "label"
    )


# --- Provider boundary ----------------------------------------------------------


class Http:
    """Only the provider HTTP boundary is replaced; all adapter logic runs."""

    def __init__(self, answer: Any = None, *, input_tokens: int = 1000, images: int = 1) -> None:
        self.answer, self.input_tokens, self.images = answer, input_tokens, images
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def __call__(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((method, path, kwargs))
        if method == "POST" and path == "/files":
            return {"id": "file-abc123", "status": "uploaded"}
        if method == "DELETE":
            return {"id": "file-abc123", "deleted": True}
        if method == "POST" and path == "/responses/input_tokens":
            return {"input_tokens": self.input_tokens}
        if method == "POST" and path == "/responses":
            return {
                "id": "r",
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(self.answer)}]}],
                "usage": {"input_tokens": self.input_tokens, "output_tokens": 200},
            }
        if method == "POST" and path == "/images/generations":
            return {"data": [{"b64_json": base64.b64encode(PNG).decode()}] * self.images}
        pytest.fail(f"Unexpected provider HTTP request: {method} {path}")

    def payload(self, path: str) -> dict[str, Any]:
        return next(kwargs["payload"] for _, called, kwargs in self.calls if called == path and "payload" in kwargs)

    def count(self, path: str) -> int:
        return sum(1 for _, called, _ in self.calls if called == path)


def ledger(work: Path) -> list[dict[str, Any]]:
    return json.loads((work / "meta-usage.json").read_text())["calls"]


@pytest.fixture
def video(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "proxy.mp4"
    path.write_bytes(b"test media bytes")
    monkeypatch.setattr(meta, "probe", lambda _: type("Info", (), {"duration": 10.0})())
    return path


def test_patch_plan_gets_catalogue_ids_and_the_current_edit(
    enabled: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    http = Http(
        {
            "patches": [
                {
                    "op": "set_edit",
                    "segment": None,
                    "seconds": None,
                    "path": "look.preset",
                    "value": "warm",
                    "text": None,
                    "reason": "warmer",
                },
                {
                    "op": "set_edit",
                    "segment": None,
                    "seconds": 1.0,
                    "path": "texts",
                    "value": "From raw oak",
                    "text": {"duration": 2.0, "style": "title", "position": "top"},
                    "reason": "title",
                },
                {
                    "op": "set_speed",
                    "segment": 2,
                    "seconds": None,
                    "path": None,
                    "value": 1.5,
                    "text": None,
                    "reason": "faster",
                },
                {
                    "op": "split",
                    "segment": 2,
                    "seconds": 21.0,
                    "path": None,
                    "value": None,
                    "text": None,
                    "reason": "split",
                },
                {
                    "op": "set_edit",
                    "segment": None,
                    "seconds": None,
                    "path": "export.format",
                    "value": "gif",
                    "text": None,
                    "reason": "not allowed",
                },
                {
                    "op": "set_edit",
                    "segment": None,
                    "seconds": None,
                    "path": "look.preset",
                    "value": "sepia",
                    "text": None,
                    "reason": "not a choice",
                },
                {
                    "op": "set_speed",
                    "segment": 2,
                    "seconds": None,
                    "path": None,
                    "value": 9,
                    "text": None,
                    "reason": "out of range",
                },
                {
                    "op": "set_speed",
                    "segment": 9,
                    "seconds": None,
                    "path": None,
                    "value": 1.5,
                    "text": None,
                    "reason": "no such moment",
                },
                {
                    "op": "split",
                    "segment": 1,
                    "seconds": 99.0,
                    "path": None,
                    "value": None,
                    "text": None,
                    "reason": "past the source",
                },
            ],
            "summary": "Warmer and faster.",
        }
    )
    monkeypatch.setattr(meta, "_request", http)
    plan = {
        "duration": 7.0,
        "source_duration": 40.0,
        "edit": dict(default_edit(), trim_pauses=True),
        "segments": [
            {"label": "Opener", "start": 0.5, "end": 4.5},
            {"label": "Sanding", "start": 20.0, "end": 23.0, "speed": 1.25},
        ],
    }
    result = meta.patch_plan(
        goal="Bowl story", note="warmer, faster sanding", plan=plan, sentences=None, work_dir=tmp_path / "work"
    )
    assert [patch["op"] for patch in result["patches"]] == ["set_edit", "set_edit", "set_speed", "split"]
    assert result["patches"][0] == {
        "op": "set_edit",
        "segment": None,
        "seconds": None,
        "path": "look.preset",
        "value": "warm",
        "reason": "warmer",
        "source": "director",
    }
    assert result["patches"][1]["value"] == {
        "text": "From raw oak",
        "start": 1.0,
        "duration": 2.0,
        "style": "title",
        "position": "top",
    }
    prompt = http.payload("/responses")["input"][0]["content"][0]["text"]
    assert "look.preset: one of none, auto, warm, cool, vivid, cinematic, film, mono, noir, vintage" in prompt
    assert "captions.scale: number 0.5 to 2" in prompt and "transition.type: one of none, fade" in prompt
    assert '"trim_pauses":true' in prompt, "the model sees the edit as it stands"
    assert "2. Sanding: source 20.00–23.00s, speed 1.25x, plays at 4.00–6.40s of the finished video" in prompt
    schema = http.payload("/responses")["text"]["format"]["schema"]["properties"]["patches"]["items"]
    assert set(schema["required"]) == set(schema["properties"]) and schema["additionalProperties"] is False


def test_the_review_judges_what_it_sees_and_keeps_length_a_ceiling(
    enabled: None, video: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = {
        "verdict": "Warm and clear.",
        "strengths": [],
        "fixes": [],
        "caption_suggestion": "",
        "hook_score": 4,
        "checks": [],
        "patches": [
            {
                "op": "set_edit",
                "segment": None,
                "seconds": None,
                "path": "captions.scale",
                "value": 1.3,
                "text": None,
                "reason": "Small captions",
            }
        ],
    }
    http = Http(review)
    monkeypatch.setattr(meta, "_request", http)
    plan = {
        "duration": 5.0,
        "source_duration": 40.0,
        "segments": [{"label": "Opener", "start": 2.0, "end": 7.0}],
        "analysis": {
            "sentences": [
                {"start": 3.0, "end": 5.0, "text": "This is the bowl."},
                {"start": 8.0, "end": 9.0, "text": "This was cut."},
            ]
        },
    }
    result = meta.review_cut(video, goal="A 25-second bowl story", plan=plan, work_dir=tmp_path / "work")
    assert result["patches"][0]["path"] == "captions.scale"
    prompt = http.payload("/responses")["input"][0]["content"][0]["text"]
    judging, _, reference = prompt.partition("Everything below is reference for writing patches only")
    assert reference, "the outline and the settings are marked as reference"
    assert "Judge only what a viewer sees and hears in the attached video." in judging
    assert "Never mention how the video was made" in judging
    assert "a reshoot, or a new export" in judging
    assert "A length named in the goal is a ceiling, not an exact duration" in judging
    assert "1. Opener: source 2.00–7.00s" not in judging and "1. Opener: source 2.00–7.00s" in reference
    assert "Approved story outline" not in prompt
    assert "[1.0–3.0] This is the bowl." in judging, "the words are given on the finished clock"
    assert "This was cut." not in prompt, "a line the cut left out is not in the video"
    assert "Whether a sentence is cut off is measured separately" in judging and "no mid-sentence" not in prompt


FOUND = {
    "moments": [
        {"start": 12.0, "end": 15.0, "reason": "Hands sand the bowl."},
        {"start": 30.0, "end": 31.0, "reason": "Sawdust falls."},
        {"start": 15.0, "end": 12.0, "reason": "Backwards."},
        {"start": 5.0, "end": 99.0, "reason": "Past the end."},
        {"start": 7.0, "end": 8.0, "reason": ""},
        {"start": 39.9, "end": 40.0, "reason": "Too short to play."},
    ]
}


def test_find_moments_is_checked_against_the_duration_and_cached(
    enabled: None, video: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    http = Http(FOUND)
    monkeypatch.setattr(meta, "_request", http)
    work = tmp_path / "work"
    result = meta.find_moments(video, query="sanding", duration=40.0, work_dir=work)
    assert [(m["start"], m["end"]) for m in result["moments"]] == [(12.0, 15.0), (30.0, 31.0), (39.9, 40.0)]
    assert result["moments"][0]["source"] == "meta" and result["moments"][0]["score"] > result["moments"][1]["score"]
    assert [call["operation"] for call in ledger(work)] == ["find"] and ledger(work)[0]["status"] == "completed"
    assert http.count("/files") == 1, "the proxy is uploaded for this search"
    format_ = http.payload("/responses")["text"]["format"]
    assert format_["name"] == "vedit_find" and format_["strict"] is True
    assert meta.find_moments(video, query="sanding", duration=40.0, work_dir=work)["usage"]["cached"] is True
    assert http.count("/responses") == 1


def test_find_respects_the_budget(enabled: None, video: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(meta, "_request", Http(FOUND))
    with pytest.raises(meta.MetaError, match="campaign budget"):
        meta.find_moments(video, query="sanding", duration=40.0, work_dir=tmp_path / "work", budget_usd=0.001)


def _short(title: str, *ranges: tuple[float, float]) -> dict[str, Any]:
    return {
        "title": title,
        "summary": "A short.",
        "caption": "Watch.",
        "cover_prompt": "Bowl.",
        "hook": "Stops the scroll.",
        "hashtags": ["oak"],
        "segments": [{"start": lo, "end": hi, "label": "Hook", "reason": "Visible."} for lo, hi in ranges],
    }


def test_plan_shorts_keeps_only_distinct_stories(
    enabled: None, video: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    http = Http(
        {
            "shorts": [
                _short("One", (0.0, 5.0)),
                _short("Overlaps one", (4.0, 8.0)),
                _short("Used", (30.0, 33.0)),
                _short("Too long", (9.0, 30.0)),
                _short("Two", (10.0, 14.0)),
            ]
        }
    )
    monkeypatch.setattr(meta, "_request", http)
    work = tmp_path / "work"
    result = meta.plan_shorts(
        video, goal="Bowl story", duration=40.0, count=2, target_duration=6.0, work_dir=work, used=[[29.0, 34.0]]
    )
    assert [short["title"] for short in result["shorts"]] == ["One", "Two"]
    assert [call["operation"] for call in ledger(work)] == ["shorts"]
    prompt = http.payload("/responses")["input"][0]["content"][0]["text"]
    assert "Find 2 distinct shorts" in prompt and "29.0–34.0s" in prompt
    again = meta.plan_shorts(
        video, goal="Bowl story", duration=40.0, count=2, target_duration=6.0, work_dir=work, used=[[29.0, 34.0]]
    )
    assert again["usage"]["cached"] is True and [short["title"] for short in again["shorts"]] == ["One", "Two"]


def test_script_prompts_share_a_style_and_forbid_text(
    enabled: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    http = Http(
        {
            "style": "Warm watercolor.",
            "scenes": [
                {"title": "The blank", "image_prompt": "A rough oak blank."},
                {"title": "The lathe", "image_prompt": "A lathe turning."},
            ],
        }
    )
    monkeypatch.setattr(meta, "_request", http)
    work = tmp_path / "work"
    result = meta.script_prompts(["It starts as a blank.", "The lathe shapes it."], aspect="9:16", work_dir=work)
    assert result["style"] == "Warm watercolor." and result["scenes"][0]["title"] == "The blank"
    assert all(
        scene["prompt"].startswith("Warm watercolor.") and scene["prompt"].endswith(director.IMAGE_RULE)
        for scene in result["scenes"]
    )
    assert "No text" in director.IMAGE_RULE
    assert not any(path == "/files" for _, path, _ in http.calls), "a script is text; nothing is uploaded"
    prompt = http.payload("/responses")["input"][0]["content"][0]["text"]
    assert "Never ask for text, letters, numbers, captions, signs, logos or watermarks" in prompt and "9:16" in prompt
    assert [call["operation"] for call in ledger(work)] == ["script"]
    assert (
        meta.script_prompts(["It starts as a blank.", "The lathe shapes it."], aspect="9:16", work_dir=work)["usage"][
            "cached"
        ]
        is True
    )
    monkeypatch.setattr(meta, "_request", Http({"style": "s", "scenes": [{"title": "t", "image_prompt": "p"}]}))
    with pytest.raises(meta.MetaError, match="one image prompt per scene"):
        meta.script_prompts(["One.", "Two."], aspect="1:1", work_dir=work)


def test_generate_image_is_sized_cached_and_charged_per_image(
    enabled: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    http = Http()
    monkeypatch.setattr(meta, "_request", http)
    work = tmp_path / "work"
    first = meta.generate_image(prompt="A bowl.", size=director.IMAGE_SIZES["9:16"], work_dir=work)
    payload = http.payload("/images/generations")
    assert payload["model"] == "muse-image-1.0" and payload["size"] == "1024x1792" and payload["output_format"] == "png"
    assert payload["tool_enablement"] == {
        "enable_image_search": False,
        "enable_web_search": False,
        "enable_shell": False,
    }
    assert Path(first["path"]).read_bytes() == PNG
    assert ledger(work)[0]["operation"] == "image" and ledger(work)[0]["usage"]["cost_usd"] == 0.01
    assert meta.generate_image(prompt="A bowl.", size="1024x1792", work_dir=work)["usage"]["cached"] is True
    assert http.count("/images/generations") == 1
    with pytest.raises(meta.MetaError):
        meta.generate_image(prompt="A bowl.", size="tall", work_dir=work)
    monkeypatch.setattr(meta, "_request", Http(images=2))
    with pytest.raises(meta.MetaError, match="single image"):
        meta.generate_image(prompt="Another bowl.", size="1024x1024", work_dir=work)
    with pytest.raises(meta.MetaError, match="campaign budget"):
        meta.generate_image(prompt="A third bowl.", size="1024x1024", work_dir=work, budget_usd=0.015)


# --- When the loop stops --------------------------------------------------------

PASS = {"name": "a", "passed": True}
FAIL = {"name": "b", "passed": False}
FIX = [{"op": "trim_end", "segment": 1, "seconds": 2.0}]


def decide(
    checks: list[dict[str, Any]],
    patches: list[dict[str, Any]] | None = None,
    *,
    review: dict[str, Any] | None = None,
    rounds: int = 0,
    limit: int = 2,
    worse_than: int | None = None,
    remaining: float = 1.0,
    cost: float = 0.01,
) -> str | None:
    return director.refine_stop(
        {"checks": checks, "patches": patches or [], "review": review},
        rounds=rounds,
        limit=limit,
        worse_than=worse_than,
        remaining_usd=remaining,
        round_cost_usd=cost,
    )


def test_the_loop_continues_only_while_a_fix_can_help() -> None:
    assert decide([PASS, FAIL], FIX) is None
    assert decide([PASS, PASS], FIX) == "All checks passed."
    assert decide([PASS, FAIL]) == "Stopped: 1 check still fails and no fixes remain."
    assert decide([FAIL, FAIL]) == "Stopped: 2 checks still fail and no fixes remain."
    assert (
        decide([FAIL], FIX, rounds=2) == "Stopped after 2 rounds, the limit set for this project. 1 check still fails."
    )
    assert (
        decide([FAIL, FAIL, PASS], FIX, worse_than=1)
        == "Stopped: this version fails 2 checks and the one before it failed 1. Both remain available."
    )
    assert decide([FAIL], FIX, worse_than=1) is None, "holding steady may still improve"
    assert (
        decide([FAIL], FIX, remaining=0.004, cost=0.01)
        == "Stopped: another round needs about $0.0100 and $0.0040 of the budget remains."
    )
    assert (
        decide([FAIL], FIX, review={"error": "Meta's rate limit was reached."})
        == "Stopped: the review of this version did not complete. Meta's rate limit was reached."
    )


def load_helpers() -> Any:
    spec = importlib.util.spec_from_file_location(
        "director_test_media_helpers", Path(__file__).with_name("test_e2e.py")
    )
    assert spec and spec.loader
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)
    if not helpers._have_ffmpeg():
        pytest.skip("ffmpeg required")
    return helpers


@pytest.fixture
def clip(tmp_path: Path) -> Path:
    source = tmp_path / "vedit-sessions" / "source-test" / "inbox" / "clip.mp4"
    source.parent.mkdir(parents=True)
    return load_helpers()._make_clip(source, seconds=4, color="teal")


@pytest.fixture
def manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[CampaignManager]:
    instance = CampaignManager(tmp_path, start_worker=False)
    try:
        yield instance
    finally:
        instance.close()


def reviews(monkeypatch: pytest.MonkeyPatch, answers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A scripted Muse Spark: the next verdict each time a finished cut is reviewed."""
    seen: list[dict[str, Any]] = []

    def fake(
        video: Path, *, goal: str, plan: dict, work_dir: Path, language: str, budget_usd: float, **kwargs: Any
    ) -> dict[str, Any]:
        seen.append({"plan": plan, "video": video})
        answer = answers[min(len(seen) - 1, len(answers) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return {
            "verdict": "Reviewed.",
            "strengths": [],
            "fixes": [],
            "caption_suggestion": "",
            "hook_score": 3,
            "provider": "meta",
            "usage": {"cost_usd": 0.001, "estimated_max_usd": 0.004},
            "warnings": [],
            "checks": [],
            "patches": [],
            **answer,
        }

    monkeypatch.setattr(meta, "review_cut", fake)
    return seen


def fails(patches: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "checks": [{"name": "Pacing", "passed": False, "at": 1.0, "note": "Drags."}],
        "patches": patches
        if patches is not None
        else [{"op": "trim_end", "segment": 1, "seconds": 2.5, "reason": "Tighten.", "source": "director"}],
    }


def tighter(seconds: float) -> dict[str, Any]:
    return fails([{"op": "trim_end", "segment": 1, "seconds": seconds, "reason": "Tighten.", "source": "director"}])


def passes() -> dict[str, Any]:
    return {"checks": [{"name": "Pacing", "passed": True, "at": 1.0, "note": "Moves well."}], "patches": []}


def approve(manager: CampaignManager, clip: Path, *, rounds: int, budget: float = 0.25) -> str:
    job = manager.create(
        {
            "paths": [str(clip)],
            "goal": "Show the product clearly",
            "target_duration": 3,
            "captions": False,
            "review": True,
            "auto_refine": rounds,
            "budget_usd": budget,
        }
    )
    manager.process(job["id"])
    manager.render(job["id"], {"plan": story((0.0, 3.0))})
    return job["id"]


def drain(manager: CampaignManager, ident: str, *, limit: int = 8) -> dict[str, Any]:
    """Run the queue the way the worker would, one queued render at a time."""
    for _ in range(limit):
        job = manager.get(ident)
        if job["status"] != "queued":
            return job
        manager.process(ident)
    pytest.fail("the loop did not stop")


def test_auto_refine_needs_a_review_and_a_small_whole_number(
    manager: CampaignManager, clip: Path, enabled: None
) -> None:
    base = {"paths": [str(clip)], "goal": "Show it", "target_duration": 3, "captions": False, "review": True}
    for rounds in (4, -1, 1.5, True, "2"):
        with pytest.raises(CampaignError, match="whole number of rounds"):
            manager.create(dict(base, auto_refine=rounds))
    with pytest.raises(CampaignError, match="Turn the review on"):
        manager.create(dict(base, review=False, auto_refine=1))


def test_refining_stops_when_all_checks_pass_and_every_version_stays(
    manager: CampaignManager, clip: Path, enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = reviews(monkeypatch, [tighter(2.5), passes()])
    ident = approve(manager, clip, rounds=3)
    job = drain(manager, ident)
    assert (
        job["status"] == "complete"
        and job["revision"] == 2
        and job["result"]["revision"] == 2
        and job["result"]["stale"] is False
    )
    assert [(version["revision"], version["source"], version["status"]) for version in job["versions"]] == [
        (1, "approved", "verified"),
        (2, "auto_refine", "verified"),
    ]
    assert job["versions"][1]["applied_patches"][0]["summary"] == "Moment 1 ends at 2.5 s."
    assert (
        job["auto_refine"]["stopped"] == "All checks passed."
        and job["auto_refine"]["rounds"] == 1
        and job["auto_refine"]["active"] is False
    )
    assert job["versions"][-1]["stopped"] == "All checks passed." and job["message"].endswith("All checks passed.")
    assert len(seen) == 2
    first, second = (manager.artifact(ident, "video", revision=revision) for revision in (1, 2))
    assert first != second and first.is_file() and second.is_file()
    assert abs(probe(first).duration - 3) < 0.3 and abs(probe(second).duration - 2.5) < 0.3, (
        "version 1 is still the file it was"
    )


def test_refining_stops_at_the_round_limit(
    manager: CampaignManager, clip: Path, enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = reviews(monkeypatch, [tighter(2.8), tighter(2.6), tighter(2.4), tighter(2.2)])
    ident = approve(manager, clip, rounds=2)
    job = drain(manager, ident)
    assert [version["source"] for version in job["versions"]] == ["approved", "auto_refine", "auto_refine"]
    assert job["auto_refine"]["stopped"].startswith("Stopped after 2 rounds") and len(seen) == 3


def test_refining_stops_when_no_fix_remains(
    manager: CampaignManager, clip: Path, enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    reviews(monkeypatch, [fails([])])
    job = drain(manager, approve(manager, clip, rounds=3))
    assert job["revision"] == 1 and job["auto_refine"]["stopped"].endswith("no fixes remain.")


def test_refining_stops_when_no_fix_can_be_applied(
    manager: CampaignManager, clip: Path, enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    reviews(monkeypatch, [tighter(0.1)])
    job = drain(manager, approve(manager, clip, rounds=3))
    assert (
        job["revision"] == 1
        and job["status"] == "complete"
        and "could be applied safely" in job["auto_refine"]["stopped"]
    )


def test_refining_stops_when_the_fixes_lead_back_to_a_version_already_made(
    manager: CampaignManager, clip: Path, enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    reviews(monkeypatch, [tighter(2.5), tighter(3.0)])
    job = drain(manager, approve(manager, clip, rounds=3))
    assert (
        job["revision"] == 2
        and job["auto_refine"]["stopped"] == "Stopped: the remaining fixes lead back to a version already made."
    )


def test_a_version_that_got_worse_stops_the_loop_and_both_remain(
    manager: CampaignManager, clip: Path, enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    worse = tighter(2.2)
    worse["checks"] = [
        {"name": "Pacing", "passed": False, "at": 1.0, "note": "Drags."},
        {"name": "Hook", "passed": False, "at": 0.0, "note": "Weak."},
    ]
    reviews(monkeypatch, [tighter(2.5), worse])
    job = drain(manager, approve(manager, clip, rounds=3))
    assert job["revision"] == 2 and job["auto_refine"]["stopped"].startswith("Stopped: this version fails")
    assert (
        manager.artifact(job["id"], "video", revision=1).is_file()
        and manager.artifact(job["id"], "video", revision=2).is_file()
    )


def test_the_budget_stops_the_loop_before_it_buys_a_render(
    manager: CampaignManager, clip: Path, enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = reviews(monkeypatch, [tighter(2.5)])

    def charge(root: Path, cap: float) -> dict[str, Any]:
        return {
            "cap_usd": cap,
            "reserved_usd": cap - 0.001,
            "remaining_usd": 0.001,
            "operations": {"review": {"reserved_usd": 0.01, "calls": 1, "model": "m"}},
        }

    monkeypatch.setattr(CampaignManager, "_usage_summary", staticmethod(charge))
    job = drain(manager, approve(manager, clip, rounds=3))
    assert job["revision"] == 1 and len(seen) == 1
    assert (
        job["auto_refine"]["stopped"] == "Stopped: another round needs about $0.0100 and $0.0010 of the budget remains."
    )


def test_budget_exhausted_mid_loop_keeps_every_verified_version(
    manager: CampaignManager, clip: Path, enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second review cannot reserve its cost: that version is verified and has no notes, and the loop ends there."""
    reviews(
        monkeypatch,
        [
            tighter(2.5),
            meta.MetaError(
                "This review needs up to $0.0040; $0.0100 is already reserved against the $0.0100 campaign budget."
            ),
        ],
    )
    job = drain(manager, approve(manager, clip, rounds=3))
    assert job["status"] == "complete" and job["revision"] == 2 and job["result"]["revision"] == 2
    assert job["auto_refine"]["stopped"].startswith("Stopped: the review of this version did not complete.")
    assert "campaign budget" in job["auto_refine"]["stopped"]
    assert [version["status"] for version in job["versions"]] == ["verified", "verified"]
    assert any("Director's notes unavailable" in warning for warning in job["result"]["warnings"])
    assert (
        manager.artifact(job["id"], "video", revision=1).is_file()
        and manager.artifact(job["id"], "video", revision=2).is_file()
    )


def test_a_real_ledger_runs_out_mid_loop(
    manager: CampaignManager, clip: Path, enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reservation is the real one: two reviews fit under the cap and a third does not."""
    calls = {"review": 0}

    def fake(
        video: Path, *, goal: str, plan: dict, work_dir: Path, language: str, budget_usd: float, **kwargs: Any
    ) -> dict[str, Any]:
        calls["review"] += 1
        meta._reserve(Path(work_dir), "review", "muse-spark-1.3", 0.03, budget_usd)
        return {
            "verdict": "Reviewed.",
            "strengths": [],
            "fixes": [],
            "caption_suggestion": "",
            "hook_score": 3,
            "provider": "meta",
            "usage": {"cost_usd": 0.03},
            "warnings": [],
            **tighter(2.8 - 0.2 * calls["review"]),
        }

    monkeypatch.setattr(meta, "review_cut", fake)
    job = drain(manager, approve(manager, clip, rounds=3, budget=0.07))
    assert job["status"] == "complete" and calls["review"] == 2
    assert job["auto_refine"]["stopped"].startswith("Stopped: another round needs about $0.0300")
    assert job["budget"]["reserved_usd"] <= 0.07 and job["budget"]["operations"]["review"]["calls"] == 2


def test_a_round_that_fails_leaves_the_verified_version_current(
    manager: CampaignManager, clip: Path, enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    reviews(monkeypatch, [tighter(2.5)])
    ident = approve(manager, clip, rounds=3)
    manager.process(ident)
    assert manager.get(ident)["status"] == "queued" and manager.get(ident)["revision"] == 2
    verified = manager.get(ident)["result"]["video_url"]
    monkeypatch.setattr(
        manager,
        "_render",
        lambda job: (_ for _ in ()).throw(CampaignError("The export did not pass picture and sound verification.")),
    )
    manager.process(ident)
    job = manager.get(ident)
    assert job["status"] == "complete" and job["error"] is None and job["revision"] == 1
    assert job["result"]["revision"] == 1 and job["result"]["stale"] is False and job["result"]["video_url"] == verified
    assert [(version["revision"], version["status"], version["source"]) for version in job["versions"]] == [
        (1, "verified", "approved"),
        (2, "failed", "auto_refine"),
    ]
    assert "version 2 could not be made" in job["auto_refine"]["stopped"] and job["auto_refine"]["active"] is False
    assert job["plan"]["segments"][0]["end"] == 3.0, "the plan is the one that was verified"


def test_cancelling_a_round_leaves_the_verified_version_current(
    manager: CampaignManager, clip: Path, enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    reviews(monkeypatch, [tighter(2.5)])
    ident = approve(manager, clip, rounds=3)
    manager.process(ident)
    assert manager.cancel(ident)["status"] == "cancelled"
    manager.process(ident)
    job = manager.get(ident)
    assert (
        job["status"] == "complete"
        and job["revision"] == 1
        and job["auto_refine"]["stopped"] == "Auto-refine was cancelled."
    )


def test_a_restart_during_a_round_leaves_the_verified_version_current(
    tmp_path: Path, clip: Path, enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    reviews(monkeypatch, [tighter(2.5)])
    first = CampaignManager(tmp_path, start_worker=False)
    ident = approve(first, clip, rounds=3)
    first.process(ident)
    assert first.get(ident)["status"] == "queued"
    first.close()
    second = CampaignManager(tmp_path, start_worker=False)
    try:
        job = second.get(ident)
        assert (
            job["status"] == "complete"
            and job["revision"] == 1
            and job["auto_refine"]["stopped"] == "Stopped: Studio closed during auto-refine."
        )
    finally:
        second.close()


def test_a_note_is_not_second_guessed_by_the_loop(
    manager: CampaignManager, clip: Path, enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = reviews(monkeypatch, [passes(), tighter(2.0)])
    monkeypatch.setattr(
        meta,
        "patch_plan",
        lambda **kwargs: {
            "patches": [
                {"op": "set_edit", "path": "look.preset", "value": "warm", "reason": "warmer", "source": "director"}
            ],
            "summary": "Warmer.",
        },
    )
    ident = approve(manager, clip, rounds=3)
    assert drain(manager, ident)["auto_refine"]["stopped"] == "All checks passed."
    manager.revise(ident, {"mode": "note", "note": "warmer"})
    job = drain(manager, ident)
    assert job["revision"] == 2 and job["versions"][1]["source"] == "note" and len(seen) == 2


def test_auto_refine_can_be_chosen_at_render_time_on_a_meta_story_only(
    manager: CampaignManager, clip: Path, enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = manager.create({"paths": [str(clip)], "goal": "Show it", "target_duration": 3, "captions": False})
    manager.process(job["id"])
    with pytest.raises(CampaignError, match="Auto-refine needs Meta's review") as refused:
        manager.render(job["id"], {"auto_refine": 2})
    assert refused.value.status == 409
    with pytest.raises(CampaignError, match="whole number"):
        manager.render(job["id"], {"auto_refine": 9})
    assert manager.render(job["id"], {"auto_refine": 0})["request"]["auto_refine"] == 0


# --- A note on a real render ----------------------------------------------------


def test_a_note_changes_the_look_and_the_speed_of_the_real_file(
    manager: CampaignManager, clip: Path, enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    reviews(monkeypatch, [passes()])
    monkeypatch.setattr(
        meta,
        "patch_plan",
        lambda **kwargs: {
            "summary": "Warmer and quicker.",
            "patches": [
                {"op": "set_edit", "path": "look.preset", "value": "warm", "reason": "warmer", "source": "director"},
                {"op": "set_speed", "segment": 1, "value": 2.0, "reason": "quicker", "source": "director"},
                {
                    "op": "set_edit",
                    "path": "export.format",
                    "value": "gif",
                    "reason": "not allowed",
                    "source": "director",
                },
            ],
        },
    )
    ident = approve(manager, clip, rounds=0)
    manager.process(ident)
    manager.revise(ident, {"mode": "note", "note": "warmer and quicker"})
    job = drain(manager, ident)
    assert job["status"] == "complete", job["error"]
    assert job["plan"]["edit"]["look"]["preset"] == "warm" and job["plan"]["segments"][0]["speed"] == 2.0
    assert job["note_summary"] == "Warmer and quicker."
    changes = job["versions"][1]["applied_patches"]
    assert [change["summary"] for change in changes if change["skipped"] is None] == [
        "Look: Warm (was Original).",
        "Moment 1 plays faster (2×).",
    ]
    assert [change["skipped"] for change in changes if change["skipped"]] == [
        "that setting cannot be changed by a patch"
    ]
    assert abs(job["result"]["proof"]["duration"] - 1.5) < 0.3, "half the length at double speed, measured on the file"
    assert abs(probe(manager.artifact(ident, "video", revision=1)).duration - 3) < 0.3


# --- Find -----------------------------------------------------------------------


SENTENCES = [
    {"start": 0.0, "end": 3.0, "text": "This is the finished oak bowl."},
    {"start": 3.0, "end": 6.0, "text": "We sand it by hand until the grain shines."},
    {"start": 6.0, "end": 9.0, "text": "Sanding takes the longest of any step."},
    {"start": 9.0, "end": 12.0, "text": "Oil brings the color up."},
]


def test_transcript_search_meets_word_forms_and_ranks_the_closest_first() -> None:
    found = director.find_in_transcript("sanding", SENTENCES)
    assert [moment["start"] for moment in found] == [6.0, 3.0], "the exact phrase ranks first and “sand” still matches"
    assert found[0]["reason"].startswith("Said here: “Sanding takes") and found[0]["source"] == "transcript"
    assert director.find_in_transcript("the oiled bowl", SENTENCES)[0]["start"] in {0.0, 9.0}
    assert director.find_in_transcript("submarine", SENTENCES) == []
    assert director.find_in_transcript("", SENTENCES) == []
    assert (
        director.find_in_transcript(
            "oil", [{"start": 1, "end": 0, "text": "oil"}, "nope", {"start": True, "end": 2, "text": "oil"}]
        )
        == []
    )


def test_two_searches_agreeing_are_one_moment_and_rank_first() -> None:
    said = {"start": 3.0, "end": 6.0, "reason": "Said.", "source": "transcript", "score": 0.5}
    shown = {"start": 3.5, "end": 6.5, "reason": "Seen.", "source": "meta", "score": 0.9}
    other = {"start": 20.0, "end": 22.0, "reason": "Elsewhere.", "source": "meta", "score": 0.85}
    merged = director.merge_moments([said], [other, shown])
    assert (
        merged[0]["source"] == "both"
        and merged[0]["score"] == 1.0
        and merged[0]["reason"] == "Said. Seen."
        and len(merged) == 2
    )


def planned_story(manager: CampaignManager, clip: Path, **extra: Any) -> str:
    job = manager.create(
        {"paths": [str(clip)], "goal": "Show the bowl", "target_duration": 2, "captions": False, **extra}
    )
    manager.process(job["id"])
    return job["id"]


def seed_analysis(manager: CampaignManager, ident: str, sentences: list[dict[str, Any]]) -> None:
    job = manager.get(ident)
    manager._update(
        ident,
        plan=dict(
            job["plan"],
            analysis={
                "method": "local-analysis",
                "scenes": [],
                "speech": [],
                "transcript": [],
                "sentences": sentences,
                "duration": 4.0,
                "notes": [],
            },
        ),
    )


def test_find_on_a_local_project_costs_nothing_and_uploads_nothing(manager: CampaignManager, clip: Path) -> None:
    ident = planned_story(manager, clip)
    seed_analysis(manager, ident, SENTENCES)
    found = manager.find(ident, {"query": "sanding"})
    assert [moment["start"] for moment in found["moments"]] == [6.0, 3.0] and found["searched"] == ["transcript"]
    assert found["budget"]["reserved_usd"] == 0
    seed_analysis(manager, ident, [])
    assert manager.find(ident, {"query": "sanding"})["warnings"] == [
        "This project has no transcript to search. Searching the picture needs a story made with Meta director."
    ]
    with pytest.raises(CampaignError):
        manager.find(ident, {"query": "   "})
    with pytest.raises(CampaignError):
        manager.find(ident, {"query": "x" * 201})


def test_find_on_a_meta_project_asks_spark_about_the_proxy_and_keeps_the_ledger(
    manager: CampaignManager, clip: Path, enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        meta,
        "plan_story",
        lambda *a, **k: {
            "title": "Bowl",
            "summary": "s",
            "caption": "c",
            "cover_prompt": "p",
            "hashtags": [],
            "segments": [{"start": 0.0, "end": 2.0, "label": "A", "reason": "r"}],
            "usage": {},
            "warnings": [],
        },
    )
    ident = planned_story(manager, clip, provider="meta")
    seed_analysis(manager, ident, SENTENCES)
    http = Http({"moments": [{"start": 1.0, "end": 2.5, "reason": "A hand holds sandpaper."}]})
    monkeypatch.setattr(meta, "_request", http)
    found = manager.find(ident, {"query": "sanding"})
    assert found["searched"] == ["transcript", "footage"] and "A hand holds sandpaper." in [
        moment["reason"] for moment in found["moments"]
    ]
    assert {moment["source"] for moment in found["moments"]} == {"meta", "transcript"}
    assert (
        found["budget"]["operations"]["find"]["calls"] == 1
        and manager.get(ident)["budget"]["operations"]["find"]["calls"] == 1
    )
    monkeypatch.setattr(
        meta,
        "_request",
        lambda *a, **k: (_ for _ in ()).throw(
            meta.MetaError("Meta's rate limit was reached. Wait briefly, then retry.")
        ),
    )
    degraded = manager.find(ident, {"query": "oil finish"})
    assert degraded["moments"] and degraded["searched"] == ["transcript"], "the free half still answers"
    assert degraded["warnings"] == [
        "The footage search did not complete: Meta's rate limit was reached. Wait briefly, then retry."
    ]


# --- Long to shorts -------------------------------------------------------------


def speech_signals(duration: float, sentences: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "duration": duration,
        "has_audio": True,
        "scenes": [],
        "silences": [],
        "transcript": sentences,
        "warnings": [],
    }


LONG = [
    {"start": float(t), "end": float(t + 5), "text": f"Sentence number {t} says something new about the oak bowl."}
    for t in range(0, 60, 5)
]


def test_local_shorts_use_distinct_moments() -> None:
    from vedit.story import StoryError, build_plan

    used: list[tuple[float, float]] = []
    plans = []
    for variation in range(1, 4):
        plan = build_plan(
            goal="Bowl story",
            duration=60.0,
            target_duration=12.0,
            scenes=[],
            silences=[],
            transcript=LONG,
            has_audio=True,
            exclude=list(used),
            variation=variation,
        )
        plans.append(plan)
        used.extend((segment["start"], segment["end"]) for segment in plan["segments"])
    ranges = sorted(used)
    assert all(left[1] <= right[0] + 0.15 for left, right in zip(ranges, ranges[1:])), (
        "no moment repeats across the family"
    )
    assert len({plan["segments"][0]["start"] for plan in plans}) == 3
    with pytest.raises(StoryError, match="already used"):
        build_plan(
            goal="Bowl story",
            duration=60.0,
            target_duration=12.0,
            scenes=[],
            silences=[],
            transcript=LONG,
            has_audio=True,
            exclude=[(0.0, 60.0)],
        )


def test_variation_changes_what_is_chosen_first() -> None:
    from vedit.story import build_plan

    first = {
        variation: build_plan(
            goal="Bowl story",
            duration=60.0,
            target_duration=10.0,
            scenes=[],
            silences=[],
            transcript=LONG,
            has_audio=True,
            variation=variation,
        )["segments"][0]["start"]
        for variation in range(4)
    }
    assert len(set(first.values())) >= 3


def test_shorts_are_linked_siblings_that_render_on_their_own(
    manager: CampaignManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "vedit-sessions" / "long" / "inbox" / "long.mp4"
    source.parent.mkdir(parents=True)
    load_helpers()._make_clip(source, seconds=12, color="navy")

    def measured(path: Path, work: Path, **kwargs: Any) -> dict[str, Any]:
        Path(work).mkdir(parents=True, exist_ok=True)
        return {
            "duration": probe(path).duration,
            "has_audio": True,
            "scenes": [],
            "silences": [],
            "transcript": [
                {"start": float(t), "end": float(t + 2), "text": f"Line {t} is about the bowl."}
                for t in range(0, 12, 2)
            ],
            "words": [],
            "transcribed": True,
            "warnings": [],
        }

    monkeypatch.setattr("vedit.story.analyze_source", measured)
    job = manager.create({"paths": [str(source)], "goal": "Show the bowl", "target_duration": 3, "captions": False})
    manager.process(job["id"])
    shorts = manager.shorts(job["id"], {"count": 3, "target_duration": 3})
    assert [short["status"] for short in shorts] == ["queued"] * 3
    assert {short["request"]["paths"][0] for short in shorts} == {str(source)}
    for short in shorts:
        manager.process(short["id"])  # the plan
        assert manager.get(short["id"])["status"] == "queued", "asking for shorts was the approval"
        manager.process(short["id"])  # the render
    finished = [manager.get(short["id"]) for short in shorts]
    assert all(item["status"] == "complete" for item in finished), [item["error"] for item in finished]
    assert all(item["result"]["proof"]["height"] == 1920 for item in finished)
    moments = sorted((segment["start"], segment["end"]) for item in finished for segment in item["plan"]["segments"])
    assert all(left[1] <= right[0] + 0.15 for left, right in zip(moments, moments[1:])), moments
    assert len({item["plan"]["title"] for item in finished}) == 3 and finished[0]["plan"]["title"].startswith(
        "Short 1:"
    )
    parent = manager.get(job["id"])
    assert {row["id"] for row in parent["family"]} == {short["id"] for short in shorts}
    assert all(row["role"] == "short" and row["status"] == "complete" for row in parent["family"])
    sibling = {row["id"]: row for row in finished[0]["family"]}
    assert sibling[job["id"]]["role"] == "source" and shorts[1]["id"] in sibling and shorts[0]["id"] not in sibling


def test_shorts_can_wait_for_approval_and_are_bounded(manager: CampaignManager, clip: Path) -> None:
    ident = planned_story(manager, clip)
    for count in (1, 6, "3", True):
        with pytest.raises(CampaignError, match="between 2 and 5"):
            manager.shorts(ident, {"count": count})
    with pytest.raises(CampaignError, match="too short"):
        manager.shorts(ident, {"count": 5})
    shorts = manager.shorts(ident, {"count": 2, "auto_render": False})
    manager.process(shorts[0]["id"])
    assert manager.get(shorts[0]["id"])["status"] == "planned"


def test_meta_shorts_share_one_answer_and_each_takes_its_own_hook(
    manager: CampaignManager, tmp_path: Path, enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "vedit-sessions" / "long" / "inbox" / "long.mp4"
    source.parent.mkdir(parents=True)
    load_helpers()._make_clip(source, seconds=12, color="navy")
    monkeypatch.setattr(
        meta,
        "plan_story",
        lambda *a, **k: {
            "title": "Bowl",
            "summary": "s",
            "caption": "c",
            "cover_prompt": "p",
            "hashtags": [],
            "segments": [{"start": 0.0, "end": 2.0, "label": "A", "reason": "r"}],
            "usage": {},
            "warnings": [],
        },
    )
    heard: list[Path] = []
    monkeypatch.setattr(
        meta,
        "transcribe_audio",
        lambda wav, **kwargs: (
            heard.append(wav)
            or {
                "cues": [{"start": 1.0, "end": 2.0, "text": "The blank is rough."}],
                "provider": "meta",
                "usage": {"cost_usd": 0},
            }
        ),
    )
    job = manager.create(
        {"paths": [str(source)], "goal": "Show the bowl", "provider": "meta", "target_duration": 3, "captions": False}
    )
    manager.process(job["id"])
    http = Http({"shorts": [_short("First hook", (3.0, 5.0)), _short("Second hook", (6.0, 8.0))]})
    monkeypatch.setattr(meta, "_request", http)
    shorts = manager.shorts(job["id"], {"count": 3, "auto_render": False})
    for short in shorts:
        manager.process(short["id"])
    done = [manager.get(short["id"]) for short in shorts]
    assert [item["plan"]["title"] for item in done[:2]] == ["First hook", "Second hook"]
    assert done[0]["plan"]["provider"] == "meta" and done[0]["plan"]["segments"][0]["start"] == 3.0
    assert http.count("/responses") == 1, "one Spark call for the family"
    assert len(heard) == 1, "the family's source is heard by Meta once; each short reuses the transcript"
    assert (
        done[0]["plan"]["analysis"]["transcript"][0]["text"] == "The blank is rough."
        and done[0]["plan"]["analysis"]["transcribed_by"] == "meta-voice-transcribe"
    )
    assert done[2]["status"] == "failed" and "found 2 distinct shorts" in done[2]["error"]
    assert done[0]["budget"]["operations"]["shorts"]["calls"] == 1 and done[1]["budget"]["reserved_usd"] == 0


# --- Script to video ------------------------------------------------------------


def test_paragraphs_are_scenes_and_one_block_is_shared_out_by_sentences() -> None:
    assert director.split_script("First idea here.\n\nSecond idea here.\n\n  \n\nThird.") == [
        "First idea here.",
        "Second idea here.",
        "Third.",
    ]
    assert director.split_script("Scene 1: The blank.\n\nSCENE 2 - The lathe.\n\n# Scene 3. The oil.") == [
        "The blank.",
        "The lathe.",
        "The oil.",
    ]
    block = " ".join(f"Sentence {index} has about eight or so plain words." for index in range(12))
    scenes = director.split_script(block)
    assert 2 <= len(scenes) <= director.MAX_SCENES and " ".join(scenes) == block
    assert all(scene.endswith(".") for scene in scenes), "a scene never ends mid-sentence"
    assert director.split_script("   \n ") == []
    assert director.split_script("One short line.") == ["One short line."]


def test_the_scene_limit_always_holds_and_no_words_are_lost() -> None:
    text = "\n\n".join(f"Paragraph {index} has a few words." for index in range(20))
    scenes = director.split_script(text)
    assert len(scenes) == director.MAX_SCENES and " ".join(scenes) == " ".join(text.split())
    long = " ".join(
        f"Long sentence number {index} runs for quite a few more plain words than usual." for index in range(10)
    )
    assert all(
        len(scene.split()) <= director.SCENE_MAX_WORDS * 1.5
        for scene in director.split_script(long + "\n\nA second paragraph.")
    )


def test_cues_share_a_scene_by_words_and_the_writer_keeps_the_spelling() -> None:
    cues = director.scene_cues("One two three. Four.", 2.0, 6.0)
    assert [(cue["start"], cue["end"], cue["text"]) for cue in cues] == [
        (2.0, 5.0, "One two three."),
        (5.0, 6.0, "Four."),
    ]
    heard = [
        {"start": 0.0, "end": 0.5, "text": "the"},
        {"start": 0.5, "end": 1.0, "text": "brief"},
        {"start": 1.0, "end": 1.5, "text": "cut"},
        {"start": 1.5, "end": 2.0, "text": "bowl"},
    ]
    words, sentences = director.place_script_words(heard, ["The Briefcut bowl."], [(0.0, 2.2)])
    assert [word["text"] for word in words] == ["The", "brief", "cut", "bowl."], "a word Whisper split stays as heard"
    assert [word["start"] for word in words] == [0.0, 0.5, 1.0, 1.5], "timing does not move inside a scene"
    assert sentences == [{"start": 0.0, "end": 2.0, "text": "The brief cut bowl."}]
    words, _ = director.place_script_words(
        [{"start": 0, "end": 1, "text": "Muse"}, {"start": 1, "end": 2, "text": "spark"}], ["Muse Spark."], [(0.0, 2.0)]
    )
    assert [word["text"] for word in words] == ["Muse", "Spark."]


def test_words_keep_to_their_scene_even_when_whisper_drifts_past_the_edge() -> None:
    """Live, "The" opened scene two but Whisper timed it to end just after the cut, so it left its sentence behind."""
    heard = [
        {"start": 4.51, "end": 5.0, "text": "trunk."},
        {"start": 5.03, "end": 5.16, "text": "The"},
        {"start": 5.16, "end": 5.6, "text": "rough"},
        {"start": 5.6, "end": 6.0, "text": "blank."},
    ]
    words, sentences = director.place_script_words(heard, ["Its trunk.", "The rough blank."], [(0.0, 5.1), (5.1, 9.0)])
    assert [(cue["start"], cue["end"], cue["text"]) for cue in sentences] == [
        (4.51, 5.0, "trunk."),
        (5.1, 6.0, "The rough blank."),
    ]
    assert words[1] == {"start": 5.1, "end": 5.16, "text": "The"}


def test_a_scene_nothing_lined_up_with_still_gets_sentences() -> None:
    heard = [{"start": 0.0, "end": 0.5, "text": "One"}, {"start": 0.5, "end": 1.0, "text": "two."}]
    words, sentences = director.place_script_words(heard, ["One two.", "Silver birch."], [(0.0, 2.0), (2.0, 4.0)])
    assert [cue["text"] for cue in sentences] == ["One two.", "Silver birch."] and sentences[1]["start"] == 2.0
    _, sentences = director.place_script_words([], ["One. Two."], [(0.0, 2.0)])
    assert [(cue["start"], cue["end"]) for cue in sentences] == [(0.0, 1.0), (1.0, 2.0)]


def test_nothing_narrates_a_script() -> None:
    """Meta has no text-to-speech and Briefcut calls only Meta models, so no voice is offered or made."""
    script = capabilities()["script"]
    assert script["narration"] is False and script["voices"] == [] and script["default_voice"] is None
    assert "caption-led" in script["reason"] and "no synthetic voice" in script["reason"]
    assert not any(hasattr(director, name) for name in ("narrate", "narration_status", "voice_for", "_say_voices"))


def test_a_script_refuses_politely_without_meta_or_images(manager: CampaignManager, tmp_path: Path) -> None:
    with pytest.raises(CampaignError, match="needs Meta") as refused:
        manager.create_script({"script": "A short line.", "aspect": "9:16"})
    assert refused.value.status == 409
    for bad in (
        {},
        {"script": ""},
        {"script": "word " * 500},
        {"script": "Fine.", "aspect": "2:1"},
        {"script": "Fine.", "provider": "cloud"},
        {"script": "Fine.", "voice": "Nobody"},
        {"script": "Fine.", "images": ["/etc/passwd"]},
    ):
        with pytest.raises(CampaignError):
            manager.create_script(bad)


def test_a_small_budget_is_refused_before_anything_is_generated(manager: CampaignManager, enabled: None) -> None:
    with pytest.raises(CampaignError, match="for pictures"):
        manager.create_script({"script": "One.\n\nTwo.\n\nThree.", "budget_usd": 0.02})


def write_png(path: Path, color: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(["-f", "lavfi", "-i", f"color=c={color}:s=540x960:d=0.1", "-frames:v", "1", str(path)])
    return path


def script_answers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scenes: int) -> dict[str, list[Any]]:
    calls: dict[str, list[Any]] = {"images": []}
    monkeypatch.setattr(
        meta,
        "script_prompts",
        lambda lines, **kwargs: {
            "style": "Warm.",
            "scenes": [
                {"title": f"Beat {index}", "prompt": f"Warm. Picture {index}. {director.IMAGE_RULE}"}
                for index in range(1, scenes + 1)
            ],
            "usage": {},
        },
    )

    def image(*, prompt: str, size: str, work_dir: Path, budget_usd: float) -> dict[str, Any]:
        calls["images"].append((prompt, size))
        return {
            "path": str(write_png(tmp_path / "made" / f"{len(calls['images'])}.png", "orange")),
            "provider": "meta",
            "usage": {"cost_usd": 0.01},
            "warnings": [],
        }

    monkeypatch.setattr(meta, "generate_image", image)
    return calls


def test_a_script_becomes_a_normal_editable_project(
    manager: CampaignManager, tmp_path: Path, enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = script_answers(monkeypatch, tmp_path, 2)
    first = director.reading_seconds("The blank is rough.")
    total = first + director.reading_seconds("The lathe shapes it.")
    job = manager.create_script(
        {"script": "The blank is rough.\n\nThe lathe shapes it.", "aspect": "9:16", "voice": None}
    )
    assert job["status"] == "queued" and job["request"]["paths"] == []
    manager.process(job["id"])
    planned = manager.get(job["id"])
    assert planned["status"] == "planned", planned["error"]
    assert [segment["label"] for segment in planned["plan"]["segments"]] == ["Beat 1", "Beat 2"]
    assert all("picture generated by Muse Image" in segment["reason"] for segment in planned["plan"]["segments"])
    assert planned["plan"]["segments"][0]["start"] == 0 and planned["plan"]["segments"][1]["start"] == pytest.approx(
        first, abs=0.1
    )
    record = planned["plan"]["script"]
    assert (
        [scene["image"] for scene in record["scenes"]] == ["generated", "generated"]
        and record["narrated"] is False
        and record["voice"] is None
    )
    assert record["scenes"][0]["prompt"].endswith(director.IMAGE_RULE) and record["model"] == "muse-image-1.0"
    assert any(
        "generated by Muse Image" in warning and "None of it is real footage" in warning
        for warning in planned["plan"]["warnings"]
    )
    assert calls["images"][0][1] == "1024x1792"
    assert planned["source"]["has_audio"] is False and planned["request"]["target_duration"] == pytest.approx(
        total, abs=0.1
    )
    assert planned["plan"]["edit"]["zoom"]["mode"] == "slow", "a still moves"
    manager.render(job["id"], {})
    manager.process(job["id"])
    done = manager.get(job["id"])
    assert done["status"] == "complete", done["error"]
    assert abs(done["result"]["proof"]["duration"] - total) < 0.3
    assert done["result"]["proof"]["height"] == 1920


def test_a_script_is_caption_led_and_says_so(
    manager: CampaignManager, tmp_path: Path, enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    script_answers(monkeypatch, tmp_path, 2)
    job = manager.create_script({"script": "The blank is rough and heavy.\n\nThe lathe shapes it."})
    manager.process(job["id"])
    planned = manager.get(job["id"])
    assert planned["status"] == "planned", planned["error"]
    assert planned["source"]["has_audio"] is False and planned["plan"]["script"]["narrated"] is False
    assert any("caption-led" in warning and "no synthetic voice" in warning for warning in planned["plan"]["warnings"])
    assert "Caption-led, with no narration." in planned["plan"]["summary"]
    cues = planned["plan"]["analysis"]["transcript"]
    assert [cue["text"] for cue in cues] == ["The blank is rough and heavy.", "The lathe shapes it."]
    manager.render(job["id"], {})
    manager.process(job["id"])
    done = manager.get(job["id"])
    assert done["status"] == "complete", done["error"]
    srt = manager.root / job["id"] / "render-1" / "captions.srt"
    assert "The blank is rough and heavy." in srt.read_text(), "the captions are the script itself"


def test_a_script_runs_on_your_own_images_without_meta(
    manager: CampaignManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = write_png(tmp_path / "vedit-sessions" / "mine" / "inbox" / "photo.png", "green")
    job = manager.create_script({"script": "One thing.\n\nAnother thing.", "provider": "local", "images": [str(image)]})
    manager.process(job["id"])
    planned = manager.get(job["id"])
    assert planned["status"] == "planned", planned["error"]
    assert planned["plan"]["provider"] == "local" and [
        scene["image"] for scene in planned["plan"]["script"]["scenes"]
    ] == ["supplied", "supplied"]
    assert all("your image" in segment["reason"] for segment in planned["plan"]["segments"])
    assert not any("Muse Image" in warning for warning in planned["plan"]["warnings"])
    assert planned["budget"]["reserved_usd"] == 0


# --- HTTP -----------------------------------------------------------------------


def call(port: int, method: str, path: str, payload: Any = None) -> tuple[int, dict[str, Any]]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        body = json.dumps(payload).encode() if payload is not None else None
        connection.request(method, path, body=body, headers={"Content-Type": "application/json"} if body else {})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    monkeypatch.chdir(tmp_path)
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


def settle(port: int, ident: str) -> dict[str, Any]:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        job = call(port, "GET", f"/api/campaigns/{ident}")[1]["job"]
        if job["status"] not in {"queued", "analyzing", "rendering"}:
            return job
        time.sleep(0.05)
    pytest.fail("the campaign did not settle")


def test_routes_for_find_shorts_and_script(
    server: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: None
) -> None:
    # Studio is Meta-only: the project goes through the product route with Meta stubbed at the adapter.
    monkeypatch.setattr(
        meta,
        "plan_story",
        lambda *a, **k: {
            "title": "Bowl",
            "summary": "s",
            "caption": "c",
            "cover_prompt": "p",
            "hashtags": [],
            "segments": [{"start": 0.0, "end": 2.0, "label": "A", "reason": "r"}],
            "usage": {},
            "warnings": [],
        },
    )
    searched: list[str] = []
    monkeypatch.setattr(
        meta, "find_moments", lambda *a, query, **k: searched.append(query) or {"moments": [], "warnings": []}
    )
    monkeypatch.setattr(
        meta,
        "plan_shorts",
        lambda *a, **k: {"shorts": [_short("First hook", (0.0, 1.8)), _short("Second hook", (2.0, 3.8))]},
    )
    clip = load_helpers()._make_clip(tmp_path / "up.mp4", seconds=4, color="olive")
    connection = http.client.HTTPConnection("127.0.0.1", server, timeout=10)
    connection.request(
        "POST", "/api/upload?name=up.mp4", body=clip.read_bytes(), headers={"Content-Type": "application/octet-stream"}
    )
    paths = json.loads(connection.getresponse().read())["paths"]
    connection.close()
    status, body = call(
        server,
        "POST",
        "/api/campaigns",
        {"paths": paths, "goal": "Show the bowl", "captions": False, "target_duration": 2, "provider": "local"},
    )
    assert status == 400 and "Meta models only" in body["error"]
    status, body = call(
        server,
        "POST",
        "/api/campaigns",
        {"paths": paths, "goal": "Show the bowl", "captions": False, "target_duration": 2},
    )
    assert status == 202, body
    assert body["job"]["request"]["provider"] == "meta" and body["job"]["request"]["caption_provider"] == "meta"
    ident = body["job"]["id"]
    planned = settle(server, ident)
    assert planned["status"] == "planned", planned.get("error")
    assert planned["plan"]["provider"] == "meta"
    status, body = call(server, "POST", f"/api/campaigns/{ident}/find", {"query": "bowl"})
    assert status == 200 and body["ok"] is True and body["moments"] == [] and body["budget"]["reserved_usd"] == 0
    assert searched == ["bowl"] and "footage" in body["searched"]
    status, body = call(server, "POST", f"/api/campaigns/{ident}/find", {"query": ""})
    assert status == 400 and body["ok"] is False
    status, body = call(server, "POST", f"/api/campaigns/{ident}/shorts", {"count": 2, "auto_render": False})
    assert status == 202 and len(body["jobs"]) == 2
    first = settle(server, body["jobs"][0]["id"])
    assert first["status"] == "planned", first.get("error")
    assert first["plan"]["title"] == "First hook" and first["family"][0] == {
        "id": ident,
        "role": "source",
        "index": 0,
        "title": first["family"][0]["title"],
        "status": "planned",
    }
    assert call(server, "POST", f"/api/campaigns/{ident}/shorts", {"count": 9})[0] == 400
    status, body = call(server, "POST", "/api/script", {})
    assert status == 400, "the route exists and refuses an empty script"
    status, body = call(server, "POST", "/api/script", {"script": "A short line.", "provider": "local"})
    assert status == 400 and "Meta models only" in body["error"]
    monkeypatch.setenv("VEDIT_META_ENABLED", "0")
    status, body = call(server, "POST", "/api/script", {"script": "A short line.", "aspect": "9:16"})
    assert status == 409 and "needs Meta" in body["error"]
    assert call(server, "GET", f"/api/campaigns/{ident}/find")[0] == 405
    status, caps = call(server, "GET", "/api/capabilities")
    assert (
        caps["director"]["auto_refine_rounds"] == 3
        and "set_speed" in caps["director"]["patch_ops"]
        and "look.preset" in caps["director"]["edit_paths"]
    )
    assert {"narration", "voices", "default_voice", "reason", "generates_images"} <= set(caps["script"])


def test_shorts_leave_nothing_behind_when_a_child_cannot_be_queued(
    manager: CampaignManager, clip: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ident = planned_story(manager, clip)
    original = manager._add_job
    calls: list[str] = []

    def flaky(request: dict[str, Any], key: str, **kwargs: Any) -> dict[str, Any]:
        calls.append(key)
        if len(calls) == 2:
            raise CampaignError("The edit queue is full. Wait for a job to finish, then try again.", 429)
        return original(request, key, **kwargs)

    monkeypatch.setattr(manager, "_add_job", flaky)
    with pytest.raises(CampaignError, match="queue is full"):
        manager.shorts(ident, {"count": 2, "auto_render": False})
    parent = manager.get(ident)
    assert not parent.get("family"), "no phantom family rows"
    orphans = [job for job in manager.list() if job["id"] != ident]
    assert [job["status"] for job in orphans] == ["cancelled"], (
        "the short made before the failure does not sit in the queue spending budget"
    )
    monkeypatch.setattr(manager, "_add_job", original)
    shorts = manager.shorts(ident, {"count": 2, "auto_render": False})
    assert [short["status"] for short in shorts] == ["queued", "queued"]
    assert [row["id"] for row in manager.get(ident)["family"]] == [short["id"] for short in shorts]


def test_shorts_refuse_a_script_video(manager: CampaignManager, tmp_path: Path) -> None:
    image = write_png(tmp_path / "vedit-sessions" / "mine" / "inbox" / "photo.png", "green")
    job = manager.create_script({"script": "One thing.\n\nAnother thing.", "provider": "local", "images": [str(image)]})
    manager.process(job["id"])
    assert manager.get(job["id"])["status"] == "planned"
    with pytest.raises(CampaignError, match="footage story, not a script") as refused:
        manager.shorts(job["id"], {"count": 2})
    assert refused.value.status == 409


LONG_READ = "\n\n".join(["Go."] * 12 + [" ".join(["word"] * 400)])  # 412 words, under the word cap, 190 s to read


def test_a_script_too_long_to_read_is_refused_before_any_picture_is_paid_for(
    manager: CampaignManager, tmp_path: Path, enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vedit.campaign import _MAX_STORY_SECONDS

    scenes = director.split_script(LONG_READ)
    assert (
        director.count_words(LONG_READ) <= director.MAX_SCRIPT_WORDS
        and sum(director.reading_seconds(s) for s in scenes) > _MAX_STORY_SECONDS
    )
    calls = script_answers(monkeypatch, tmp_path, len(scenes))
    with pytest.raises(CampaignError, match="seconds to read"):
        manager.create_script({"script": LONG_READ, "aspect": "9:16", "budget_usd": 5})
    assert calls["images"] == [] and manager.list() == []
    # The worker checks again before the image branch, so an older queued job is refused before Muse Image runs.
    monkeypatch.setattr(
        meta, "script_prompts", lambda *a, **k: pytest.fail("prompts were written for a script that cannot be made")
    )
    request = {
        "paths": [],
        "goal": "g",
        "provider": "meta",
        "target_duration": _MAX_STORY_SECONDS,
        "aspect": "9:16",
        "captions": True,
        "caption_provider": "local",
        "language": "en",
        "subject": "",
        "budget_usd": 5,
        "generate_cover": False,
        "effect": None,
        "review": False,
        "auto_refine": 0,
        "template": "none",
        "edit": default_edit(),
        "script": {"text": LONG_READ, "scenes": scenes, "voice": None, "images": []},
    }
    job = manager._add_job(request, "long", extras={"_action": "script"})
    manager.process(job["id"])
    failed = manager.get(job["id"])
    assert failed["status"] == "failed" and "seconds to read" in failed["error"]
    assert calls["images"] == [] and failed["budget"]["reserved_usd"] == 0
