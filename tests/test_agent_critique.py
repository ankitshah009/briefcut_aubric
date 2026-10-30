"""Critique fake reports and revise a brief. No media files and no ffmpeg."""

from __future__ import annotations

from vedit.agent.critique import critique_output, critique_report
from vedit.agent.model import Critique, Finding, Intent
from vedit.agent.revise import revise_brief
from vedit.ir import Brief, Canvas

_HARD = {"duration_over", "frame_size", "captions_missing"}


def _intent() -> Intent:
    return Intent(
        name="t",
        goal="short",
        platform="shorts",
        max_duration=30,
        captions=True,
        loudnorm=True,
        reframe="9:16",
        cut_silence=True,
        music=None,
        title=None,
        hook=None,
        cta=None,
        denoise=False,
        highpass=True,
        fade=False,
        watermark=None,
        images=(),
    )


def _finding(code: str, severity: str = "fail") -> Finding:
    return Finding(code=code, severity=severity, message=code, fix=code)


def _critique(findings: list[Finding]) -> Critique:
    failed = any(item.severity == "fail" for item in findings)
    return Critique(ok=not failed, findings=tuple(findings))


def _is_fail(finding: Finding) -> bool:
    return finding.severity == "fail"


def _codes(critique: Critique) -> set[str]:
    return {item.code for item in critique.findings}


def _fail_codes(critique: Critique) -> set[str]:
    return {item.code for item in critique.findings if _is_fail(item)}


def _report(tmp_path, *, duration: float, width: int, height: int) -> dict:
    return {
        "work_dir": str(tmp_path),
        "outputs": [
            {
                "path": "/tmp/out.mp4",
                "exists": True,
                "duration": duration,
                "width": width,
                "height": height,
                "has_video": True,
                "has_audio": True,
            }
        ],
    }


def _revised(brief: Brief, critique: Critique, intent: Intent) -> Brief:
    result = revise_brief(brief, critique, intent)
    if isinstance(result, tuple):
        return result[0]
    return result


def test_critique_report_flags_long_landscape_without_captions(tmp_path):
    report = _report(tmp_path, duration=80.0, width=1920, height=1080)
    critique = critique_report(report, _intent())
    assert critique.ok is False
    assert "duration_over" in _codes(critique)
    assert "frame_size" in _codes(critique)
    assert "captions_missing" in _fail_codes(critique)


def test_critique_report_accepts_vertical_short_with_srt(tmp_path):
    srt = tmp_path / "captions.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,500\nhello shorts\n", encoding="utf-8")
    assert srt.stat().st_size > 20
    report = _report(tmp_path, duration=20.0, width=1080, height=1920)
    critique = critique_report(report, _intent())
    fail_codes = _fail_codes(critique)
    assert critique.ok or not fail_codes
    assert fail_codes.isdisjoint(_HARD)


def test_critique_output_missing_file_is_not_an_exception(tmp_path):
    missing = tmp_path / "missing.mp4"
    assert not missing.exists()
    critique = critique_output(missing, _intent())
    assert critique.ok is False
    assert "missing_output" in _codes(critique)


def test_revise_brief_inserts_cut_and_captions_and_keeps_original_ops():
    brief = Brief(
        name="t",
        sources={"main": "/tmp/talk.mp4"},
        ops=[{"op": "export", "presets": ["youtube_1080p"]}],
        canvas=Canvas(),
    )
    critique = _critique(
        [
            _finding("duration_over"),
            _finding("captions_missing"),
        ]
    )
    revised = _revised(brief, critique, _intent())
    assert brief.ops == [{"op": "export", "presets": ["youtube_1080p"]}]
    names = [op["op"] for op in revised.ops]
    assert "cut_silence" in names
    assert "captions" in names
    assert names[-1] == "export"
