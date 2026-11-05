"""Bugs found in the backend review of 2026-11-04, each pinned by the test that proved it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from vedit import captions, meta, open_weights, settings
from vedit.timeline import Span, map_cues

THAI = "สวัสดีครับ วันนี้อากาศดี"  # Thai writes no space between words, one between phrases
THAI_LONG = "สวัสดีครับ วันนี้อากาศดีมาก เราจะไปเที่ยวทะเลกัน และกินอาหารทะเลด้วยกัน"


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VEDIT_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.delenv("VEDIT_META_ENABLED", raising=False)
    monkeypatch.delenv("VEDIT_PRIVATE_MODEL", raising=False)

    def unexpected(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("no live Meta call in this review")

    monkeypatch.setattr(meta, "_request", unexpected)


# --- timeline.map_cues: a wordless line kept whole lost the spaces between Thai phrases ---


def test_a_wholly_kept_thai_line_keeps_its_phrase_spaces() -> None:
    spans = [Span(0, 0.0, 10.0, 0.0, 10.0, 1.0)]
    assert [cue["text"] for cue in map_cues([{"start": 1.0, "end": 3.0, "text": THAI}], spans)] == [THAI]


def test_a_spaced_chinese_line_kept_whole_is_left_as_heard() -> None:
    spans = [Span(0, 0.0, 10.0, 0.0, 10.0, 1.0)]
    assert map_cues([{"start": 1.0, "end": 3.0, "text": "我们 今天 去"}], spans)[0]["text"] == "我们 今天 去"


def test_a_thai_line_split_by_a_cut_keeps_the_spaces_inside_each_part() -> None:
    # The cut falls in the second phrase, so the first part holds the whole first phrase and its space.
    spans = [Span(0, 0.0, 2.6, 0.0, 2.6, 1.0), Span(1, 2.6, 10.0, 3.0, 10.4, 1.0)]
    parts = [cue["text"] for cue in map_cues([{"start": 1.0, "end": 3.0, "text": THAI}], spans)]
    assert "".join(parts).replace(" ", "") == THAI.replace(" ", ""), "no letter is lost or doubled"
    assert all(part in THAI for part in parts), "each part is the line as written, spaces included"
    assert any(" " in part for part in parts)


def test_latin_lines_split_by_a_cut_read_as_before() -> None:
    spans = [Span(0, 0.0, 2.0, 0.0, 2.0, 1.0), Span(1, 2.0, 10.0, 2.5, 10.5, 1.0)]
    parts = [cue["text"] for cue in map_cues([{"start": 1.0, "end": 3.0, "text": "one two three four"}], spans)]
    assert parts == ["one two", "three four"]


# --- captions.wrap_cue_text: a long Thai line lost its phrase spaces when wrapped (captions.srt) ---


def test_wrapping_a_long_thai_line_keeps_its_phrase_spaces() -> None:
    lines = captions.wrap_cue_text(THAI_LONG)
    assert len(lines) == 2
    assert all(line in THAI_LONG for line in lines), lines
    assert "".join(lines).replace(" ", "") == THAI_LONG.replace(" ", "")


def test_wrapping_latin_text_is_unchanged() -> None:
    text = "The finished bowl sits on the bench, oiled and ready for the market stall"
    lines = captions.wrap_cue_text(text)
    assert len(lines) == 2 and " ".join(lines) == text


def test_the_srt_file_keeps_thai_phrase_spaces(tmp_path: Path) -> None:
    captions.write_srt([captions.Cue(0.0, 3.0, THAI_LONG)], tmp_path / "captions.srt")
    body = (tmp_path / "captions.srt").read_text(encoding="utf-8").split("\n")[2:4]
    assert all(line in THAI_LONG for line in body), body


# --- meta._retry_after: a Retry-After of non-ASCII digits escaped as a ValueError traceback ---


@pytest.mark.parametrize("value", ["²", "١٢", "৩"])
def test_a_retry_after_header_in_unicode_digits_is_ignored_not_raised(value: str) -> None:
    assert meta._retry_after({"Retry-After": value}) is None


def test_an_ascii_retry_after_still_counts() -> None:
    assert meta._retry_after({"Retry-After": "7"}) == 7.0


# --- meta.patch_plan, private mode: a patch whose op is not text escaped as a TypeError ---


PLAN = {
    "duration": 7.0,
    "source_duration": 40.0,
    "segments": [{"label": "Opener", "start": 0.5, "end": 4.5}, {"label": "Reveal", "start": 20.0, "end": 23.0}],
}


def test_a_glimmer_patch_with_a_list_for_its_op_is_refused_in_words(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("VEDIT_PRIVATE_MODEL", "glimmer")
    answers = [
        {
            "patches": [
                {
                    "op": ["trim_end"],
                    "segment": 2,
                    "seconds": 23.5,
                    "path": None,
                    "value": None,
                    "text": None,
                    "reason": "x",
                }
            ],
            "summary": "Holds the reveal.",
        },
        {
            "patches": [
                {
                    "op": "trim_end",
                    "segment": 2,
                    "seconds": 23.5,
                    "path": None,
                    "value": None,
                    "text": None,
                    "reason": "Hold it.",
                }
            ],
            "summary": "Holds the reveal.",
        },
    ]
    prompts: list[str] = []

    def structured(work_dir: Path, operation: str, *, check: Any, prompt: str, **_: Any) -> dict[str, Any]:
        prompts.append(prompt)
        return dict(check(answers.pop(0)), provider="meta", usage={"cost_usd": 0.0})

    monkeypatch.setattr(open_weights, "structured", structured)
    result = meta.patch_plan(
        goal="g", note="Hold the reveal longer.", plan=PLAN, sentences=None, work_dir=tmp_path / "work"
    )
    assert result["patches"] == [
        {"op": "trim_end", "segment": 2, "seconds": 23.5, "reason": "Hold it.", "source": "director"}
    ]
    assert "Patch 1 (?): " in prompts[1] and "op must be one of the listed operations" in prompts[1]


# --- settings: a private_model that is not text raised TypeError (unhashable) ---


@pytest.mark.parametrize("value", [[], {}, ["glimmer"]])
def test_a_private_model_that_is_not_text_is_a_settings_error(value: Any) -> None:
    with pytest.raises(settings.SettingsError, match="Private mode"):
        settings.update({"private_model": value})


def test_a_damaged_private_model_in_config_does_not_break_settings() -> None:
    settings.config_path().parent.mkdir(parents=True, exist_ok=True)
    settings.config_path().write_text(json.dumps({"private_model": ["glimmer"]}), encoding="utf-8")
    assert settings.public()["private_model"] == ""


# --- settings env loader (owned by another agent; reported, not fixed here) ---


def test_an_empty_value_followed_by_a_comment_sets_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / ".env.local"
    env.write_text("MODEL_API_KEY= # paste your key here\n", encoding="utf-8")
    monkeypatch.setenv("MODEL_API_KEY", "placeholder")  # recorded, so teardown removes whatever the loader sets
    monkeypatch.delenv("MODEL_API_KEY")
    assert settings.load_env_file(env) == []
    import os

    assert "MODEL_API_KEY" not in os.environ


# --- meta.call_log: JSON of the wrong shape in the call log or ledger raised instead of being skipped ---


def test_a_damaged_call_log_never_breaks_the_meta_calls_panel(tmp_path: Path) -> None:
    work = tmp_path / "job" / "meta"
    work.mkdir(parents=True)
    rows = [
        {
            "event": "start",
            "id": "a1",
            "at": 1.0,
            "operation": "story",
            "model": "muse-spark-1.3",
            "pid": 2**70,
            "v": 1,
        },
        {"event": "start", "id": "b2", "at": 2.0, "operation": "find", "model": "muse-spark-1.3", "v": 1},
        {"event": "end", "id": "b2", "status": ["done"], "v": 1},
    ]
    (work / "meta-calls.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    (work / "meta-usage.json").write_text(
        json.dumps({"calls": [{"id": "c3", "status": {"x": 1}, "reserved_usd": 0.01}]}), encoding="utf-8"
    )
    log = meta.call_log(tmp_path)
    assert {row["id"]: row["status"] for row in log["calls"]} == {"a1": "running", "b2": "failed", "c3": "stopped"}


# --- captions.write_ass (the brief `captions` op): heard text could open an ASS override tag ---


def test_heard_text_in_the_brief_captions_ass_is_literal(tmp_path: Path) -> None:
    import re

    path = tmp_path / "captions.ass"
    captions.write_ass([captions.Cue(0.0, 2.0, r"Use {curly} braces in C:\new folder")], path)
    dialogue = next(line for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("Dialogue:"))
    text = dialogue.split(",,0,0,0,,", 1)[1]
    assert not re.search(r"(?<!\\)[{}]", text), "a bare brace opens an override block, and libass hides what is inside"
    assert "\\n" not in text and "\\N" not in text.replace("\\\u2060", ""), (
        "a heard backslash must not become a line break"
    )


# --- Live bug 1: a finished call stayed reserved at its maximum, so Muse Wonder could review a $0.60 project once ---


def _wonder_http(output_tokens: int) -> Any:
    review = {
        "verdict": "Opens on the finished bowl.",
        "strengths": ["Clear"],
        "fixes": [],
        "caption_suggestion": "Oak to bowl.",
        "hook_score": 4,
        "checks": [],
        "patches": [],
    }

    def http(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        if method == "POST" and path == "/files":
            return {"id": "file-wonder1", "object": "file", "purpose": "user_data"}
        if method == "DELETE":
            return {"id": "file-wonder1", "deleted": True}
        if path == "/responses/input_tokens":
            return {"object": "response.input_tokens", "input_tokens": 6000}
        if path == "/responses":
            return {
                "id": "resp_wonder",
                "status": "completed",
                "model": kwargs["payload"]["model"],
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": json.dumps(review)}],
                    }
                ],
                "usage": {"input_tokens": 6000, "output_tokens": output_tokens, "total_tokens": 6000 + output_tokens},
            }
        pytest.fail(f"unexpected {method} {path}")

    return http


@pytest.fixture
def meta_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_API_KEY", "test-secret-never-write-to-disk")
    monkeypatch.setenv("VEDIT_META_ENABLED", "1")
    for name in (
        "VEDIT_META_DIRECTOR",
        "VEDIT_META_MODE",
        "VEDIT_META_REASONING",
        "VEDIT_META_BASE_URL",
        "VEDIT_META_TIMEOUT",
    ):
        monkeypatch.delenv(name, raising=False)


REVIEW_PLAN = {
    "duration": 7.0,
    "source_duration": 40.0,
    "segments": [
        {"label": "Finished bowl", "start": 0.5, "end": 4.5},
        {"label": "Raw oak", "start": 20.0, "end": 23.0},
    ],
}


def test_two_wonder_reviews_in_a_row_fit_a_sixty_cent_budget(
    meta_on: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        meta, "_request", _wonder_http(output_tokens=4000)
    )  # 6000 in, 4000 out: $0.104 billed, ~$0.352 reserved
    work = tmp_path / "work"
    for version in (1, 2):
        cut = tmp_path / f"v{version}.mp4"
        cut.write_bytes(f"version {version} of the cut".encode())
        result = meta.review_cut(
            cut, goal="Show the bowl", plan=dict(REVIEW_PLAN), work_dir=work, director="wonder", budget_usd=0.60
        )
        assert result["usage"]["cost_usd"] == pytest.approx(0.104)
    calls = json.loads((work / "meta-usage.json").read_text())["calls"]
    assert [call["status"] for call in calls] == ["completed", "completed"]
    totals = meta.call_log(tmp_path, budget_usd=0.60)["totals"]
    assert totals["held_usd"] == pytest.approx(0.208) and totals["remaining_usd"] == pytest.approx(0.392)


def test_a_call_still_running_or_uncertain_holds_its_reservation() -> None:
    assert meta.held_usd({"reserved_usd": 0.35, "status": "reserved"}) == pytest.approx(0.35)
    assert meta.held_usd({"reserved_usd": 0.35, "status": "failed_or_incomplete"}) == pytest.approx(0.35)
    estimated = {"reserved_usd": 0.35, "status": "completed", "usage": {"cost_usd": 0.35, "cost_is_estimate": True}}
    assert meta.held_usd(estimated) == pytest.approx(0.35)
    billed = {"reserved_usd": 0.35, "status": "completed", "usage": {"cost_usd": 0.10}}
    assert meta.held_usd(billed) == pytest.approx(0.10)
    overrun = {"reserved_usd": 0.10, "status": "completed", "usage": {"cost_usd": 0.12, "overran_usd": 0.02}}
    assert meta.held_usd(overrun) == pytest.approx(0.12), "an overrun counts what Meta billed"
    assert meta.held_usd({"reserved_usd": 0, "released": True, "status": "failed_or_incomplete"}) == 0


def test_a_wonder_refusal_in_contributor_mode_names_the_standard_rate(
    meta_on: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VEDIT_META_MODE", "contributor")
    monkeypatch.setattr(meta, "_request", _wonder_http(output_tokens=4000))
    cut = tmp_path / "v1.mp4"
    cut.write_bytes(b"cut")
    with pytest.raises(meta.MetaError) as refused:
        meta.review_cut(
            cut,
            goal="Show the bowl",
            plan=dict(REVIEW_PLAN),
            work_dir=tmp_path / "work",
            director="wonder",
            budget_usd=0.25,
        )
    assert "Standard mode only" not in str(refused.value)
    assert (
        "Muse Wonder has no contributor price, so it is billed at the standard rate even in contributor mode."
        in str(refused.value)
    )


# --- Live bug 3: find put SAM's verdict and thumbnail on a moment SAM never looked at ---


def test_a_whole_clip_moment_does_not_swallow_a_short_hit_or_lend_it_sams_verdict() -> None:
    from vedit import director

    said = {"start": 0.383, "end": 3.58, "reason": "Said here.", "source": "transcript", "score": 0.75}
    seen = {
        "start": 0.0,
        "end": 40.021,
        "reason": "The bowl is on the lathe throughout.",
        "source": "meta",
        "score": 0.9,
        "verified": True,
        "coverage": 0.8,
        "thumbnail": "/x/the-wooden-bowl-18511.jpg",
        "flagged": False,
        "evidence": {"by": "sam-3.1", "window": [18.511, 21.511]},
        "verification": "SAM masked it.",
    }
    merged = director.merge_moments([said], [seen])
    hit = next(moment for moment in merged if moment["start"] == 0.383)
    assert (
        hit["source"] == "transcript" and "verified" not in hit and "thumbnail" not in hit and "verification" not in hit
    )
    whole = next(moment for moment in merged if moment["start"] == 0.0)
    assert whole["verified"] is True and whole["thumbnail"] == "/x/the-wooden-bowl-18511.jpg"


def test_sams_verdict_moves_only_when_its_window_lies_in_the_surviving_moment() -> None:
    from vedit import director

    said = {"start": 10.0, "end": 14.0, "reason": "Said.", "source": "transcript", "score": 0.5}
    elsewhere = {
        "start": 9.5,
        "end": 14.5,
        "reason": "Seen.",
        "source": "meta",
        "score": 0.9,
        "verified": True,
        "coverage": 0.7,
        "thumbnail": "/x/t.jpg",
        "evidence": {"by": "sam-3.1", "window": [14.2, 17.2]},
        "verification": "SAM masked it.",
    }
    [twin] = director.merge_moments([said], [elsewhere])
    assert twin["source"] == "both" and "verified" not in twin and "thumbnail" not in twin
    inside = dict(elsewhere, evidence={"by": "sam-3.1", "window": [10.5, 13.5]})
    [twin] = director.merge_moments([said], [inside])
    assert twin["verified"] is True and twin["thumbnail"] == "/x/t.jpg" and twin["evidence"]["window"] == [10.5, 13.5]


# --- director.split_script: a Thai script written one phrase per line ran its phrases together ---


def test_thai_script_lines_keep_a_space_between_phrases() -> None:
    from vedit import director

    assert director.split_script("สวัสดีครับ\nวันนี้อากาศดีมาก\n\nเราจะไปเที่ยวทะเลกัน") == [
        "สวัสดีครับ วันนี้อากาศดีมาก",
        "เราจะไปเที่ยวทะเลกัน",
    ]
    assert director.split_script("我们今天\n去公园。") == ["我们今天去公园。"], "Chinese lines still join with no space"
    assert director.split_script("Scene 1\nHello there\nfriend.") == ["Hello there friend."]


def test_merged_thai_scenes_keep_a_space_between_them() -> None:
    from vedit import director

    scenes = director.split_script("สวัสดีครับ\n\nวันนี้\n\nอากาศดีมาก", max_scenes=2)
    assert len(scenes) == 2 and " " in "".join(scenes) and "".join(scenes).replace(" ", "") == "สวัสดีครับวันนี้อากาศดีมาก"
