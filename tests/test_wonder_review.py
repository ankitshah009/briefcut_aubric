"""Muse Wonder as the senior director: same review, a model that also listens.

Only ``meta._request`` is replaced. Every request Briefcut would send is
recorded and compared with what Meta documents for Muse Wonder: an MP4 uploaded
through the Files API with purpose ``user_data``, referenced by an
``input_file`` block on the Responses API, at Standard-mode prices of $4 in and
$20 out per million tokens, with no contributor variant.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from vedit import meta

REVIEW = {
    "verdict": "Opens on the finished bowl, then earns it with process.",
    "strengths": ["Clear sequence", "Calm, even narration"],
    "fixes": [{"what": "Hold the oiled bowl a beat longer.", "where_seconds": 2.5}],
    "caption_suggestion": "From raw oak to finished bowl.",
    "hook_score": 4,
    "checks": [
        {"name": "Shows the bowl start to finish", "passed": True, "at": 0.5, "note": "Finished bowl, then the raw blank."},
        {"name": "Captions match the voice", "passed": False, "at": 4.2, "note": "The voice says 'twenty four'; the caption shows 24."},
    ],
    "patches": [
        {"op": "trim_end", "segment": 1, "seconds": 4.0, "path": None, "value": None, "text": None, "reason": "Let the line land."},
        {"op": "set_edit", "segment": None, "seconds": None, "path": "captions.scale", "value": 1.2, "text": None, "reason": "Small captions"},
    ],
}

PLAN = {
    "duration": 7.0,
    "source_duration": 40.0,
    "segments": [{"label": "Finished bowl", "start": 0.5, "end": 4.5}, {"label": "Raw oak", "start": 20.0, "end": 23.0}],
    "analysis": {"sentences": [{"start": 0.6, "end": 4.4, "text": "This is the finished oak bowl."},
                               {"start": 20.1, "end": 22.9, "text": "Here is the raw oak blank."}]},
}

GOAL = "Show how the oak bowl is made, start to finish"
WONDER_IN, WONDER_OUT = 4.00 / 1_000_000, 20.00 / 1_000_000


class Http:
    """Meta's boundary: answers like the Responses API and records every request."""

    def __init__(self, review: Any = None, *, input_tokens: int = 6000, output_tokens: int = 3000) -> None:
        self.review = copy.deepcopy(REVIEW if review is None else review)
        self.input_tokens, self.output_tokens = input_tokens, output_tokens
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def __call__(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((method, path, kwargs))
        if method == "POST" and path == "/files":
            return {"id": "file-wonder1", "object": "file", "purpose": "user_data"}
        if method == "DELETE" and path == "/files/file-wonder1":
            return {"id": "file-wonder1", "deleted": True}
        if method == "POST" and path == "/responses/input_tokens":
            return {"object": "response.input_tokens", "input_tokens": self.input_tokens}
        if method == "POST" and path == "/responses":
            model = kwargs["payload"]["model"]
            return {"id": "resp_" + model.replace(".", "_"), "status": "completed", "model": model,
                    "output": [{"type": "message", "role": "assistant",
                                "content": [{"type": "output_text", "text": json.dumps(self.review)}]}],
                    "usage": {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                              "total_tokens": self.input_tokens + self.output_tokens}}
        pytest.fail(f"Unexpected provider request: {method} {path}")

    def routes(self) -> list[tuple[str, str]]:
        return [(method, path) for method, path, _ in self.calls]

    def payloads(self, path: str) -> list[dict[str, Any]]:
        return [kwargs["payload"] for method, route, kwargs in self.calls if method == "POST" and route == path]

    def prompt(self) -> str:
        return self.payloads("/responses")[-1]["input"][0]["content"][0]["text"]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("VEDIT_META_DIRECTOR", "VEDIT_META_MODE", "VEDIT_META_REASONING"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MODEL_API_KEY", "test-secret-never-write-to-disk")
    monkeypatch.setenv("VEDIT_META_ENABLED", "1")


@pytest.fixture
def cut(tmp_path: Path) -> Path:
    # The plan carries the duration, so the review never needs to probe these bytes.
    path = tmp_path / "review.mp4"
    path.write_bytes(b"finished cut bytes; Meta's boundary is stubbed")
    return path


def review(cut: Path, tmp_path: Path, http: Http, monkeypatch: pytest.MonkeyPatch, *, work: str = "work", **kwargs: Any) -> dict[str, Any]:
    monkeypatch.setattr(meta, "_request", http)
    return meta.review_cut(cut, goal=GOAL, plan=copy.deepcopy(PLAN), work_dir=tmp_path / work, **kwargs)


def ledger(work: Path) -> list[dict[str, Any]]:
    return json.loads((work / "meta-usage.json").read_text(encoding="utf-8"))["calls"]


def call_log(work: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (work / "meta-calls.jsonl").read_text(encoding="utf-8").splitlines()]


# --- The default director stays Spark ---


def test_the_default_director_is_spark(cut: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    http = Http()
    result = review(cut, tmp_path, http, monkeypatch)
    assert result["model"] == "muse-spark-1.3" and result["director"] == "muse-spark"
    assert [payload["model"] for payload in http.payloads("/responses")] == ["muse-spark-1.3"]
    prompt = http.prompt()
    assert "Trust these words over what you think you heard" in prompt, "Spark is told not to rely on its ears"
    assert "listen to the whole video" not in prompt and "trust your ears" not in prompt
    # Spark's cost is still Spark's price.
    assert result["usage"]["cost_usd"] == pytest.approx(6000 * 1.25e-6 + 3000 * 4.25e-6)
    assert ledger(tmp_path / "work")[0]["model"] == "muse-spark-1.3"


def test_contributor_mode_still_picks_contributor_spark_by_default(cut: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VEDIT_META_MODE", "contributor")
    http = Http()
    result = review(cut, tmp_path, http, monkeypatch)
    assert result["model"] == "muse-spark-1.3-contributor" and result["director"] == "muse-spark"
    assert result["usage"]["cost_usd"] == pytest.approx(6000 * 0.10e-6 + 3000 * 0.20e-6)


@pytest.mark.parametrize("choice, env, expected", [
    (None, "wonder", "muse-wonder-1.0"),
    (None, " Muse-Wonder ", "muse-wonder-1.0"),
    ("spark", "wonder", "muse-spark-1.3"),
    ("wonder", "", "muse-wonder-1.0"),
    (None, "", "muse-spark-1.3"),
])
def test_the_director_comes_from_the_caller_then_the_environment(
    cut: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, choice: str | None, env: str, expected: str,
) -> None:
    monkeypatch.setenv("VEDIT_META_DIRECTOR", env)
    result = review(cut, tmp_path, Http(), monkeypatch, director=choice, budget_usd=1.0)
    assert result["model"] == expected


@pytest.mark.parametrize("choice", ["glimmer", "muse-wonder-full", 3, ""])
def test_an_unknown_director_is_refused_before_any_request(cut: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, choice: Any) -> None:
    http = Http()
    if choice == "":
        monkeypatch.setenv("VEDIT_META_DIRECTOR", "opus")
        choice = None
    with pytest.raises(meta.MetaError, match="'spark' or 'wonder'"):
        review(cut, tmp_path, http, monkeypatch, director=choice, budget_usd=1.0)
    assert http.calls == []


# --- What Wonder receives matches Meta's docs ---


def test_wonder_request_matches_the_documented_shape(cut: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    http = Http()
    result = review(cut, tmp_path, http, monkeypatch, director="wonder", budget_usd=1.0)
    # Upload once, count, answer, delete: the documented two-step video flow.
    assert http.routes() == [("POST", "/files"), ("POST", "/responses/input_tokens"), ("POST", "/responses"), ("DELETE", "/files/file-wonder1")]
    upload = http.calls[0][2]["body"]
    head = upload.head.decode()
    assert 'name="purpose"\r\n\r\nuser_data' in head and "Content-Type: video/mp4" in head
    assert 'filename="source.mp4"' in head, "the original file name never leaves the machine"
    counted, answered = http.payloads("/responses/input_tokens")[0], http.payloads("/responses")[0]
    assert counted == answered, "the token count prices exactly the request that is sent"
    assert answered["model"] == "muse-wonder-1.0"
    assert answered["store"] is False and answered["max_output_tokens"] == meta._OUTPUT_TOKENS
    assert answered["reasoning"] == {"effort": "medium"}
    content = answered["input"][0]["content"]
    assert answered["input"][0]["role"] == "user" and [part["type"] for part in content] == ["input_text", "input_file"]
    assert content[1] == {"type": "input_file", "file_id": "file-wonder1"}, "the whole video, picture and sound, in one part"
    assert answered["text"]["format"] == {"type": "json_schema", "name": "vedit_review", "strict": True, "schema": meta._review_schema()}
    assert result["model"] == "muse-wonder-1.0" and result["director"] == "muse-wonder" and result["provider"] == "meta"


def test_wonder_is_asked_to_listen_and_to_check_the_words_by_ear(cut: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    http = Http()
    review(cut, tmp_path, http, monkeypatch, director="wonder", budget_usd=1.0)
    prompt = http.prompt()
    judging, _, reference = prompt.partition("Everything below is reference for writing patches only")
    assert "listen to the whole video and add audio checks" in judging
    assert "burned-in captions show the words the voice says, while it says them" in judging
    assert "evenly loud across the cuts" in judging and "audible pop" in judging
    assert "trust your ears" in judging and "Trust these words over what you think you heard" not in prompt
    assert "[0.1–3.9] This is the finished oak bowl." in judging, "the recognized words are on the finished clock"
    # Everything Spark is told still holds for Wonder.
    assert "Never mention how the video was made" in judging and "A length named in the goal is a ceiling" in judging
    assert "Whether a sentence is cut off is measured separately" in judging
    assert "1. Finished bowl: source 0.50–4.50s" in reference


def test_wonder_ignores_contributor_mode_because_model_api_has_none(cut: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VEDIT_META_MODE", "contributor")
    http = Http()
    result = review(cut, tmp_path, http, monkeypatch, director="wonder", budget_usd=1.0)
    assert http.payloads("/responses")[0]["model"] == "muse-wonder-1.0"
    assert result["usage"]["cost_usd"] == pytest.approx(6000 * WONDER_IN + 3000 * WONDER_OUT)


# --- Budget: reserve at Wonder's prices, refuse clearly ---


def test_wonder_reserves_and_charges_at_its_own_prices(cut: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    http = Http(input_tokens=6000, output_tokens=3000)
    result = review(cut, tmp_path, http, monkeypatch, director="wonder", budget_usd=0.40)
    reserved = 6000 * WONDER_IN + meta._OUTPUT_TOKENS * WONDER_OUT
    work = tmp_path / "work"
    [entry] = ledger(work)
    assert entry["operation"] == "review" and entry["model"] == "muse-wonder-1.0" and entry["status"] == "completed"
    assert entry["reserved_usd"] == pytest.approx(reserved)
    assert entry["usage"]["cost_usd"] == pytest.approx(6000 * WONDER_IN + 3000 * WONDER_OUT) == result["usage"]["cost_usd"]
    assert "cost_is_estimate" not in entry["usage"] and entry["request_id"] == "resp_muse-wonder-1_0"
    start, end = call_log(work)
    assert start["model"] == "muse-wonder-1.0" and "Muse Wonder" in start["purpose"] and start["size"] == {"input_tokens": 6000}
    assert end["cost_usd"] == pytest.approx(0.084) and end["cost_is_estimate"] is False


def test_a_default_budget_cannot_cover_wonder_and_the_video_never_leaves(cut: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """$0.25, the default campaign budget, is below Wonder's output allowance alone."""
    http = Http()
    with pytest.raises(meta.MetaError) as refused:
        review(cut, tmp_path, http, monkeypatch, director="wonder", budget_usd=0.25)
    message = str(refused.value)
    allowance = meta._OUTPUT_TOKENS * WONDER_OUT
    assert f"${allowance:.4f}" in message and "before the video is counted" in message
    assert "$4 in and $20 out per million tokens). " in message and "Standard mode only" not in message
    assert "Raise the campaign budget to at least $0.33, or choose Muse Spark as the director." in message
    assert http.calls == [], "nothing is uploaded for a review the budget cannot pay for"
    assert not (tmp_path / "work" / "meta-usage.json").exists()


def test_earlier_spend_counts_against_wonder(cut: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work = tmp_path / "work"
    # A finished call holds what Meta billed ($0.10), not what it reserved ($0.30).
    meta._ledger(work, {"id": "plan1", "operation": "story", "model": "muse-spark-1.3", "reserved_usd": 0.30, "budget_usd": 0.40,
                        "status": "completed", "usage": {"cost_usd": 0.10}})
    http = Http()
    with pytest.raises(meta.MetaError, match=r"\$0\.1000 of the \$0\.4000 campaign budget is already reserved.*at least \$0\.43"):
        review(cut, tmp_path, http, monkeypatch, director="wonder", budget_usd=0.40)
    assert http.calls == []


def test_a_long_cut_is_refused_after_counting_and_its_upload_deleted(cut: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    http = Http(input_tokens=200_000)
    with pytest.raises(meta.MetaError) as refused:
        review(cut, tmp_path, http, monkeypatch, director="wonder", budget_usd=0.50)
    need = 200_000 * WONDER_IN + meta._OUTPUT_TOKENS * WONDER_OUT
    assert f"${need:.4f}" in str(refused.value) and "200,000 input tokens" in str(refused.value)
    assert "at least $1.13" in str(refused.value)
    assert http.routes() == [("POST", "/files"), ("POST", "/responses/input_tokens"), ("DELETE", "/files/file-wonder1")]
    assert not (tmp_path / "work" / "meta-usage.json").exists(), "nothing was reserved, so nothing is held"


def test_spark_still_refuses_an_empty_budget_without_naming_another_model(cut: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    http = Http()
    with pytest.raises(meta.MetaError, match="budget") as refused:
        review(cut, tmp_path, http, monkeypatch, budget_usd=0.01)
    assert "A Muse Spark review" in str(refused.value) and "Muse Wonder" not in str(refused.value)
    assert http.calls == []


# --- The output schema is Spark's ---


def test_wonder_and_spark_return_the_same_shape(cut: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spark = review(cut, tmp_path, Http(), monkeypatch, work="spark")
    wonder = review(cut, tmp_path, Http(), monkeypatch, work="wonder", director="wonder", budget_usd=1.0)
    assert set(spark) == set(wonder) >= {"verdict", "strengths", "fixes", "caption_suggestion", "hook_score", "checks", "patches",
                                         "provider", "model", "director", "usage", "warnings"}
    differ = {"model", "director", "usage"}
    assert {key: spark[key] for key in spark if key not in differ} == {key: wonder[key] for key in wonder if key not in differ}
    assert [patch["source"] for patch in wonder["patches"]] == ["director", "director"]
    assert wonder["patches"][1] == {"op": "set_edit", "segment": None, "seconds": None, "path": "captions.scale", "value": 1.2,
                                    "reason": "Small captions", "source": "director"}
    assert wonder["checks"][1] == {"name": "Captions match the voice", "passed": False, "at": 4.2,
                                   "note": "The voice says 'twenty four'; the caption shows 24."}


def test_each_director_keeps_its_own_cache(cut: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    http = Http()
    review(cut, tmp_path, http, monkeypatch, budget_usd=1.0)
    review(cut, tmp_path, http, monkeypatch, director="wonder", budget_usd=1.0)
    assert [payload["model"] for payload in http.payloads("/responses")] == ["muse-spark-1.3", "muse-wonder-1.0"]
    again = review(cut, tmp_path, http, monkeypatch, director="wonder", budget_usd=1.0)
    assert again["usage"]["cached"] is True and again["usage"]["charged_this_call_usd"] == 0
    assert again["model"] == "muse-wonder-1.0" and again["director"] == "muse-wonder"
    assert len(http.payloads("/responses")) == 2, "a cached review is not bought again"


def test_a_malformed_wonder_answer_keeps_its_reservation(cut: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    http = Http({**REVIEW, "hook_score": "great"})
    with pytest.raises(meta.MetaError, match="hook score"):
        review(cut, tmp_path, http, monkeypatch, director="wonder", budget_usd=1.0)
    [entry] = ledger(tmp_path / "work")
    assert entry["status"] == "failed_or_incomplete" and entry["model"] == "muse-wonder-1.0"
    assert http.routes()[-1] == ("DELETE", "/files/file-wonder1")


def test_the_word_null_from_a_strict_schema_is_no_text(cut: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Live Muse Wonder (2026-11-04) wrote the string "null" for caption_suggestion and both patch reasons."""
    live = {**REVIEW, "caption_suggestion": "null", "patches": [
        {"op": "set_edit", "segment": None, "seconds": None, "path": "zoom.mode", "value": "slow", "text": None, "reason": "null"},
        {"op": "set_speed", "segment": 2, "seconds": None, "path": None, "value": 0.85, "text": None, "reason": " NULL "},
    ]}
    http = Http(live)
    result = review(cut, tmp_path, http, monkeypatch, director="wonder", budget_usd=1.0)
    assert result["caption_suggestion"] == ""
    assert [(patch["op"], patch["reason"]) for patch in result["patches"]] == [("set_edit", ""), ("set_speed", "")]
    assert result["patches"][0]["value"] == "slow" and result["patches"][1]["value"] == 0.85, "the edits themselves are kept"
    assert "never write the word null in a text field" in http.prompt()
    assert meta._display_text("Nullify the hum", 100) == "Nullify the hum", "only the bare word is dropped"
