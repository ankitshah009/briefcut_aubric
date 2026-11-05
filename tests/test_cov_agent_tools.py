"""The stdio tool protocol: argument checking, error envelopes, and real tools on a small real clip."""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest

from vedit.agent.tools import dispatch, serve_stdio, session_failure, tool_specs
from vedit.media import resolve_ffmpeg


def _have_ffmpeg() -> bool:
    try:
        resolve_ffmpeg()
        return True
    except Exception:
        return False


render = pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg required")


def _clip(path: Path, *, seconds: float = 1.0) -> Path:
    subprocess.run(
        [
            resolve_ffmpeg(),
            "-hide_banner",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=320x180:r=30",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            str(seconds),
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(path),
        ],
        check=True,
    )
    return path


def _serve(*lines: str) -> list[dict]:
    stdout = io.StringIO()
    assert serve_stdio(io.StringIO("\n".join(lines) + "\n"), stdout) == 0
    return [json.loads(line) for line in stdout.getvalue().splitlines()]


def test_specs_describe_every_tool_with_a_json_schema() -> None:
    specs = tool_specs()
    for spec in specs:
        assert spec["parameters"]["type"] == "object"
        assert isinstance(spec["parameters"]["properties"], dict)
        for name in spec["parameters"].get("required", []):
            assert name in spec["parameters"]["properties"], f"{spec['name']} requires an undeclared {name}"
    specs[0]["name"] = "changed"
    assert tool_specs()[0]["name"] != "changed", "callers get copies, not the registry"
    assert dispatch("specs") == {"ok": True, "result": tool_specs()}


@pytest.mark.parametrize("name", ["", "   ", None, 7])
def test_a_tool_name_that_is_not_a_word_is_refused(name: object) -> None:
    assert dispatch(name, {}) == {"ok": False, "error": "tool name must be a string"}  # type: ignore[arg-type]


def test_arguments_must_be_an_object_and_the_name_is_trimmed() -> None:
    assert dispatch("specs", [1]) == {"ok": False, "error": "arguments must be an object"}  # type: ignore[arg-type]
    assert dispatch("  specs  ")["ok"] is True
    assert dispatch("nope", None) == {"ok": False, "error": "unknown tool: nope"}


@pytest.mark.parametrize(
    ("tool", "arguments", "error"),
    [
        ("inventory", {}, "paths must be a list of strings"),
        ("inventory", {"paths": "a.mp4"}, "paths must be a list of strings"),
        ("inventory", {"paths": ["a.mp4", 3]}, "paths must be a list of strings"),
        ("inventory", {"paths": ["a.mp4"], "analyze": "maybe"}, "analyze must be a boolean"),
        ("inventory", {"paths": ["a.mp4"], "analyze": 2.5}, "analyze must be a boolean"),
        ("parse_intent", {}, "goal must be a string"),
        ("parse_intent", {"goal": 5}, "goal must be a string"),
        ("parse_intent", {"goal": "x", "sources": ["a"]}, "sources must be an object"),
        ("decide", {"goal": "x"}, "paths must be a list of strings"),
        ("critique", {"goal": "x"}, "path must be a string"),
        ("critique", {"goal": "x", "path": ""}, "path must be a string"),
        ("critique", {"goal": "x", "path": 3}, "path must be a string"),
        ("revise", {"brief": "x", "critique": {}, "goal": "x"}, "brief must be an object"),
        ("revise", {"brief": {}, "critique": [], "goal": "x"}, "critique must be an object"),
        ("render", {"goal": "x", "revise": "lots"}, "revise must be an integer"),
        ("render", {"goal": "x", "revise": True}, "revise must be an integer"),
        ("render", {"goal": "x", "revise": 1.5}, "revise must be an integer"),
        ("plan", {"goal": "x", "root": 4}, "root must be a string"),
    ],
)
def test_bad_arguments_come_back_as_errors_not_exceptions(tool: str, arguments: dict, error: str) -> None:
    assert dispatch(tool, arguments) == {"ok": False, "error": error}


def test_an_inventory_of_a_missing_file_names_it(tmp_path: Path) -> None:
    out = dispatch("inventory", {"paths": [str(tmp_path / "gone.mp4")]})
    assert out["ok"] is False and "file does not exist" in out["error"] and "gone.mp4" in out["error"]


def test_a_failed_session_without_fail_findings_still_has_a_reason() -> None:
    assert session_failure({"status": "failed"}) == "The session failed and left no critique."
    assert session_failure({"critique": {"findings": [{"severity": "warn", "message": "soft"}]}}) == (
        "The session failed and left no critique."
    )
    assert session_failure(
        {"critique": {"findings": [{"message": "hard"}, {"severity": "fail", "message": "x", "fix": "y"}]}}
    ) == ("hard; x (y)"), "a finding with no severity counts as a failure"


def test_parse_intent_flags_follow_the_goal() -> None:
    out = dispatch(
        "parse_intent", {"goal": "podcast interview, keep the pauses, no captions", "sources": {"main": "/a.mp4"}}
    )
    assert out["ok"] is True
    result = out["result"]
    assert result["platform"] == "podcast" and result["cut_silence"] is False and result["captions"] is False
    assert result["reframe"] == "16:9"


def test_doctor_tool_reports_the_checks_and_their_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    out = dispatch("doctor")
    assert out["ok"] is True
    result = out["result"]
    names = {check["name"] for check in result["checks"]}
    assert "ffmpeg" in names and "meta" in names
    meta = next(check for check in result["checks"] if check["name"] == "meta")
    assert meta["ok"] is False and meta["required"] is True and "no Meta API key" in meta["detail"]
    assert result["ok"] is False, "a required check that fails fails the whole doctor"


@render
def test_inventory_tool_names_and_measures_a_real_clip(tmp_path: Path) -> None:
    clip = _clip(tmp_path / "talk.mp4")
    out = dispatch("inventory", {"paths": [str(clip)], "analyze": "yes"})
    assert out["ok"] is True, out
    result = out["result"]
    assert result["primary"] == "main" and result["notes"] == []
    shot = result["shots"][0]
    assert shot["name"] == "main" and shot["kind"] == "video" and shot["path"] == str(clip.resolve())
    assert (shot["width"], shot["height"]) == (320, 180) and shot["has_audio"] is True
    assert shot["loudness_i"] is not None and shot["loudness_i"] < 0
    assert shot["silence_ratio"] == 0.0, "a steady tone has no pauses"
    assert shot["scene_count"] == 0

    quick = dispatch("inventory", {"paths": [str(clip)]})
    assert quick["result"]["shots"][0]["loudness_i"] is None, "nothing is measured unless asked"


@render
def test_decide_tool_returns_a_brief_with_reasons(tmp_path: Path) -> None:
    clip = _clip(tmp_path / "talk.mp4")
    out = dispatch("decide", {"goal": "youtube talk, no captions", "paths": [str(clip)]})
    assert out["ok"] is True, out
    result = out["result"]
    assert result["brief"]["sources"]["main"] == str(clip.resolve())
    ops = [op["op"] for op in result["brief"]["ops"]]
    assert ops[-1] == "export" and "captions" not in ops
    assert result["rationale"], "a decision explains itself"


@render
def test_critique_tool_judges_the_frame_against_the_goal(tmp_path: Path) -> None:
    clip = _clip(tmp_path / "talk.mp4")
    out = dispatch("critique", {"goal": "a short for tiktok", "path": str(clip), "work_dir": str(tmp_path)})
    assert out["ok"] is True, "the tool ran; the verdict is in the result"
    result = out["result"]
    assert result["ok"] is False
    codes = {finding["code"]: finding for finding in result["findings"]}
    assert (
        "frame_size" in codes
        and "320x180" in codes["frame_size"]["message"]
        and "1080x1920" in codes["frame_size"]["message"]
    )
    assert "captions_missing" in codes and codes["captions_missing"]["severity"] == "fail"


@render
def test_critique_tool_can_measure_loudness(tmp_path: Path) -> None:
    clip = _clip(tmp_path / "talk.mp4")
    out = dispatch("critique", {"goal": "a preview", "path": str(clip), "measure": "1"})
    assert out["ok"] is True
    codes = {finding["code"]: finding for finding in out["result"]["findings"]}
    assert "loudness" in codes, "a raw sine is nowhere near -16 LUFS"
    assert "LUFS" in codes["loudness"]["message"] and "loudnorm" in codes["loudness"]["fix"]


@render
def test_plan_tool_writes_a_session_without_rendering(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clip = _clip(tmp_path / "talk.mp4")
    root = tmp_path / "session"
    out = dispatch("plan", {"goal": f"preview of {clip}", "root": str(root)})
    assert out["ok"] is True, out
    result = out["result"]
    assert result["status"] == "planned" and result["primary_output"] is None
    assert result["root"] == str(root) and (root / "brief.yaml").is_file() and (root / "work" / "render.sh").is_file()
    assert not list(root.rglob("*.mp4")) or all(p == clip for p in root.rglob("*.mp4")), "nothing was rendered"

    monkeypatch.chdir(tmp_path)
    anonymous = dispatch("plan", {"goal": "preview", "paths": [str(clip)], "root": ""})
    assert anonymous["ok"] is True
    assert Path(anonymous["result"]["root"]).parent == tmp_path / "vedit-sessions", "an empty root means a fresh one"


def test_serve_stdio_handles_every_malformed_line_and_keeps_going() -> None:
    answers = _serve(
        "",
        "   ",
        "[1, 2]",
        "{}",
        '{"tool": "   "}',
        '{"tool": "specs", "arguments": [1]}',
        '{"name": "specs", "arguments": null}',
        '{"tool": "specs"}',
    )
    assert [a.get("error") for a in answers[:4]] == [
        "expected a JSON object",
        "missing tool name",
        "missing tool name",
        "arguments must be an object",
    ]
    assert answers[4]["ok"] is True and answers[5]["ok"] is True, (
        "`name` works as well as `tool`, and null arguments mean none"
    )
    assert len(answers) == 6, "blank lines produce no answer"
