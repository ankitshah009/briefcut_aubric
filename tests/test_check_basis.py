"""Every receipt says how it knows: measured by code, probed from the file, or a model's opinion.

The vocabulary is REA's evidence confidence (observed, derived, inferred). These
tests hold the labels to what produced each check, keep the counts and names the
evidence files quote, and keep old saved versions (no ``basis``) rendering.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from vedit.campaign import CampaignManager
from vedit.lint import BASES, lint_plan, subject_cutout_check, subject_in_frame_check

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "vedit" / "studio" / "static"
STUDIO = STATIC / "studio.js"

SENTENCES = [{"start": 0.0, "end": 4.0, "text": "Hello there."}, {"start": 4.5, "end": 8.0, "text": "This is the product."}]
SPEECH = [{"start": 0.0, "end": 4.0}, {"start": 4.5, "end": 8.0}]
BOXES = [{"time": t / 10, "x": 0.4, "y": 0.2, "width": 0.2, "height": 0.6} for t in range(0, 40)]
CUTOUT = {"frames": 120, "coverage": 0.99, "flicker": 0.01, "filled_frames": 0, "empty_frames": 0, "first_missing": None}
LENGTH_8 = "auto: length fits the target (\u2264 8 s)"
LENGTH_4 = "auto: length fits the target (\u2264 4 s)"
# With a transcript and captions, every lint check has something to measure. Without them, those checks are left out.
LINT_NAMES = ["auto: cuts land on sentence boundaries", LENGTH_8,
              "auto: moments inside the source and in order", "auto: captions cover the speech"]
MEASURED_NOTHING = [LENGTH_8, "auto: moments inside the source and in order"]


def _named(checks: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return next(check for check in checks if check["name"] == name)


# --- lint.py: every builder is code measuring the cut ---

def test_every_lint_check_is_derived_and_names_its_instrument() -> None:
    plans = [
        {"segments": [{"start": 0.0, "end": 4.0}, {"start": 4.5, "end": 8.0}]},
        {"segments": [{"start": 1.0, "end": 6.0}]},  # mid-sentence, short: failing checks too
    ]
    for plan in plans:
        for kwargs, names in (({}, MEASURED_NOTHING), ({"sentences": SENTENCES, "captions": [{"start": 0.0, "end": 7.5}], "speech": SPEECH}, LINT_NAMES)):
            result = lint_plan(plan, target_duration=8, duration=20, **kwargs)
            assert [check["name"] for check in result["checks"] if not check.get("advisory")] == names
            for check in result["checks"]:
                assert check["basis"] == "derived" and check["by"] == "lint", check
                assert "inputs" not in check, "nobody said which model wrote the transcript, so none is named"


def test_lint_names_the_models_whose_data_it_measured_and_only_those() -> None:
    plan = {"segments": [{"start": 0.0, "end": 4.0}, {"start": 4.5, "end": 8.0}]}
    result = lint_plan(plan, target_duration=8, duration=20, sentences=SENTENCES, captions=[{"start": 0.0, "end": 7.5}], speech=SPEECH,
                       heard_by="local-whisper", captioned_by="meta-voice-transcribe")
    checks = result["checks"]
    assert _named(checks, "auto: cuts land on sentence boundaries")["inputs"] == ["local-whisper"]
    assert _named(checks, "auto: captions cover the speech")["inputs"] == ["meta-voice-transcribe", "local-whisper"]
    assert "inputs" not in _named(checks, LENGTH_8)
    assert "inputs" not in _named(checks, "auto: moments inside the source and in order")
    # The same model for both is named once.
    same = lint_plan(plan, target_duration=8, duration=20, sentences=SENTENCES, captions=[{"start": 0.0, "end": 7.5}], speech=SPEECH,
                     heard_by="local-whisper", captioned_by="local-whisper")
    assert _named(same["checks"], "auto: captions cover the speech")["inputs"] == ["local-whisper"]


def test_a_check_that_read_nothing_names_no_input() -> None:
    plan = {"segments": [{"start": 0.0, "end": 4.0}]}
    result = lint_plan(plan, target_duration=4, duration=20, heard_by="local-whisper", captioned_by="local-whisper")
    # No transcript and no captions: those checks had nothing to measure, so they are left out, not passed.
    assert [check["name"] for check in result["checks"]] == [LENGTH_4, "auto: moments inside the source and in order"]
    assert all("inputs" not in check for check in result["checks"])


def test_subject_builders_are_derived_from_sam_data() -> None:
    framed = subject_in_frame_check(BOXES, duration=4.0, aspect="9:16", source_width=1920, source_height=1080)
    cutout = subject_cutout_check(CUTOUT)
    for check in (framed, cutout):
        assert check["basis"] == "derived" and check["by"] == "lint" and check["inputs"] == ["sam-3.1"], check
    for empty in (subject_in_frame_check([], duration=4.0, aspect="9:16", source_width=1920, source_height=1080), subject_cutout_check(None)):
        assert empty["note"] == "not requested" and empty["basis"] == "derived" and "inputs" not in empty
    assert set(BASES) == {"observed", "derived", "inferred"}


# --- campaign grading: measured checks and the director's opinion side by side ---

def _plan(method: str = "meta", **analysis: Any) -> dict[str, Any]:
    stored = {"method": method, "speech": [{"start": 0.0, "end": 4.0}], "transcript": [{"start": 0.0, "end": 4.0, "text": "Hello there."}],
              "sentences": [{"start": 0.0, "end": 4.0, "text": "Hello there."}]}
    stored.update(analysis)
    return {"segments": [{"start": 0.0, "end": 4.0}], "duration": 4.0, "analysis": stored}


def _review(**extra: Any) -> dict[str, Any]:
    return dict({"provider": "meta", "model": "muse-spark-1.3", "verdict": "Clear.", "hook_score": 4, "patches": [], "checks": [
        {"name": "hook inside 3 s", "passed": True, "at": 0.5, "note": "Opens on the product."},
        {"name": "pacing", "passed": False, "at": 2.0, "note": "The middle drags."}]}, **extra)


def _grade(tmp_path: Path, plan: dict[str, Any], *, provider: str = "meta", review: dict[str, Any] | None = None) -> dict[str, Any]:
    (tmp_path / "captions.srt").write_text("1\n00:00:00,000 --> 00:00:04,000\nHello there.\n", encoding="utf-8")
    request = {"captions": True, "caption_provider": provider, "target_duration": 4, "aspect": "9:16"}
    return CampaignManager._grade(plan, request, 20.0, tmp_path, review, edits={"decisions": ["Captions are on"], "ignored": []},
                                  tracking=BOXES, cutout=CUTOUT, cut_duration=4.0, source_size=(1920, 1080))


def test_grading_labels_director_checks_as_muse_spark_opinions_and_the_rest_as_measured(tmp_path: Path) -> None:
    graded = _grade(tmp_path, _plan(transcribed=True, transcribed_by="meta-voice-transcribe"), review=_review())
    checks = graded["checks"]
    assert [check["name"] for check in checks] == [LINT_NAMES[0], LENGTH_4, *LINT_NAMES[2:]] + [
        "auto: requested edits applied", "auto: subject stays in frame (SAM 3.1)", "auto: subject cutout covers the subject",
        "director: hook inside 3 s", "director: pacing"], "names are quoted by evidence files and docs"
    assert graded["total"] == len(checks) and graded["passed"] == sum(1 for check in checks if check["passed"])
    for check in checks:
        assert check["basis"] in BASES and check["by"], check
    for check in checks[-2:]:
        assert check["basis"] == "inferred" and check["by"] == "muse-spark" and "inputs" not in check
    edits = _named(checks, "auto: requested edits applied")
    assert edits["basis"] == "derived" and edits["by"] == "brief-parser" and "inputs" not in edits
    assert _named(checks, "auto: subject stays in frame (SAM 3.1)")["inputs"] == ["sam-3.1"]
    assert _named(checks, "auto: subject cutout covers the subject")["inputs"] == ["sam-3.1"]
    # Meta Voice Transcribe heard the plan and the captions: one model, named once.
    assert _named(checks, "auto: cuts land on sentence boundaries")["inputs"] == ["meta-voice-transcribe"]
    assert _named(checks, "auto: captions cover the speech")["inputs"] == ["meta-voice-transcribe"]
    assert sum(check["basis"] == "derived" for check in checks) == 7 and sum(check["basis"] == "inferred" for check in checks) == 2


def test_a_review_without_a_provider_is_still_muse_spark_and_a_failed_review_adds_no_opinions(tmp_path: Path) -> None:
    review = _review()
    review.pop("provider")
    graded = _grade(tmp_path, _plan(), review=review)
    assert [check["by"] for check in graded["checks"] if check["basis"] == "inferred"] == ["muse-spark", "muse-spark"]
    failed = _grade(tmp_path, _plan(), review={"error": "Meta's rate limit was reached."})
    assert not any(check["basis"] == "inferred" for check in failed["checks"])
    assert not any(check["name"].startswith("director: ") for check in failed["checks"])


def test_inputs_follow_where_the_words_came_from_and_never_guess(tmp_path: Path) -> None:
    # A Meta-mode project captions with Meta whatever caption provider an old client sent.
    (tmp_path / "captions.srt").write_text("1\n00:00:00,000 --> 00:00:04,000\nHello there.\n", encoding="utf-8")
    meta_mode = CampaignManager._grade(_plan(transcribed=True, transcribed_by="meta-voice-transcribe"), {"provider": "meta", "captions": True, "caption_provider": "local", "target_duration": 4},
                                       20.0, tmp_path, None)
    assert _named(meta_mode["checks"], "auto: captions cover the speech")["inputs"] == ["meta-voice-transcribe"]
    # A plan saved before Briefcut went Meta-only names nobody; its transcript was local Whisper's.
    legacy = _grade(tmp_path, _plan())
    assert _named(legacy["checks"], "auto: cuts land on sentence boundaries")["inputs"] == ["local-whisper"]
    assert _named(legacy["checks"], "auto: captions cover the speech")["inputs"] == ["meta-voice-transcribe", "local-whisper"]
    # Old local captions reused the plan's transcript, which local Whisper wrote.
    local = _grade(tmp_path, _plan("local-analysis"), provider="local")
    assert _named(local["checks"], "auto: captions cover the speech")["inputs"] == ["local-whisper"]
    # A script's words are the writer's own; without Whisper nobody listened.
    script = _grade(tmp_path, _plan("script"), provider="local")
    assert "inputs" not in _named(script["checks"], "auto: cuts land on sentence boundaries")
    assert "inputs" not in _named(script["checks"], "auto: captions cover the speech")
    timed = _grade(tmp_path, _plan("script", transcribed=True), provider="local")
    assert _named(timed["checks"], "auto: cuts land on sentence boundaries")["inputs"] == ["local-whisper"]
    # A planner this code does not know names nothing.
    unknown = _grade(tmp_path, _plan("someone-else"), provider="local")
    assert "inputs" not in _named(unknown["checks"], "auto: cuts land on sentence boundaries")
    assert "inputs" not in _named(unknown["checks"], "auto: captions cover the speech")
    # Nothing listened during planning: no transcript to compare with, so no model is named.
    late = _grade(tmp_path, _plan("local-analysis", transcript=[], sentences=[]), provider="local")
    names = [check["name"] for check in late["checks"]]
    assert "auto: captions cover the speech" not in names and "auto: cuts land on sentence boundaries" not in names


# --- Studio: the tag and the breakdown, and old versions without a basis ---

def _js_block(source: str, start: str) -> str:
    """The source of one top-level function or ``const`` line, matched by braces outside strings."""
    begin = source.index(start)
    if start.startswith("const "):
        return source[begin:source.index(";\n", begin) + 1]
    depth, index, quote = 0, source.index("{", begin), ""
    while True:
        char = source[index]
        if quote:
            if char == "\\":
                index += 1
            elif char == quote:
                quote = ""
        elif char in "\"'`":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[begin:index + 1]
        index += 1


def test_studio_keeps_the_basis_and_paints_all_three_labels() -> None:
    source = STUDIO.read_text(encoding="utf-8")
    normalize = _js_block(source, "function normalizeChecks(")
    assert "basis: checkBases.includes(check.basis) ? check.basis" in normalize
    assert "by: typeof check.by" in normalize and "inputs: Array.isArray(check.inputs)" in normalize
    labels = _js_block(source, "const basisLabels")
    for label in ('"Measured"', '"Probed"', '"Model opinion"'):
        assert label in labels
    paint = _js_block(source, "function paintChecks(")
    assert "basisLabels[check.basis]" in paint and "basisTitle(check)" in paint
    assert 'element("span", "sr", ", ")' in paint, "screen readers hear the tag as part of the check"
    assert '(basis ? " · " + basis : "")' in paint, "the breakdown shows only when checks carry a basis"
    assert "judgment of the cut, not a measurement." in _js_block(source, "function basisTitle(")
    assert ".check-basis {" in (STATIC / "studio.css").read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js to run the Studio check helpers")
def test_studio_helpers_normalize_old_and_new_checks() -> None:
    source = STUDIO.read_text(encoding="utf-8")
    parts = [_js_block(source, name) for name in ("const checkBases", "const basisLabels", "const instrumentNames", "function instrumentName(",
                                                  "function basisTitle(", "function basisBreakdown(", "function normalizeChecks(")]
    measured = [{"name": f"auto: {i}", "passed": True, "at": None, "note": "", "basis": "derived", "by": "lint"} for i in range(11)]
    measured.append({"name": "auto: captions cover the speech", "passed": True, "at": None, "note": "", "basis": "derived", "by": "lint", "inputs": ["meta-voice-transcribe", "local-whisper"]})
    opinions = [{"name": f"director: {i}", "passed": i < 2, "at": 1, "note": "", "basis": "inferred", "by": "muse-spark"} for i in range(4)]
    old = [{"name": "auto: duration matches the target", "passed": True, "at": None, "note": "4.0 s cut vs 4.0 s target"}]
    script = "\n".join(parts) + f"""
const out = {{}};
const fresh = normalizeChecks({json.dumps(measured + opinions)});
const saved = normalizeChecks({json.dumps(old + [{"name": "x", "passed": False, "basis": "guess", "by": 3, "inputs": "sam-3.1"}])});
out.breakdown = basisBreakdown(fresh);
out.one = basisBreakdown(normalizeChecks([{json.dumps(opinions[0])}]));
out.old = basisBreakdown(saved);
out.saved = saved;
out.opinion = basisTitle(fresh[fresh.length - 1]);
out.measuredTitle = basisTitle(fresh[11]);
out.plainTitle = basisTitle(fresh[0]);
out.probed = basisTitle(normalizeChecks([{{ name: "p", passed: true, basis: "observed", by: "ffprobe" }}])[0]);
out.keeps = fresh[11];
process.stdout.write(JSON.stringify(out));
"""
    ran = subprocess.run([shutil.which("node") or "node", "-e", script], cwd=ROOT, check=True, capture_output=True, text=True)
    out = json.loads(ran.stdout)
    assert out["breakdown"] == "12 measured, 4 model opinions"
    assert out["one"] == "1 model opinion"
    assert out["old"] == "", "a version saved before labels shows no breakdown"
    assert [(item["basis"], item["by"], item["inputs"]) for item in out["saved"]] == [("", "", []), ("", "", [])]
    assert out["saved"][0]["note"] == "4.0 s cut vs 4.0 s target" and out["saved"][0]["passed"] is True
    assert out["opinion"] == "Muse Spark’s judgment of the cut, not a measurement."
    assert out["measuredTitle"] == "Measured by Briefcut’s checker, not judged by a model. Its data came from Meta Voice Transcribe and an earlier local transcript."
    assert out["plainTitle"] == "Measured by Briefcut’s checker, not judged by a model."
    assert out["probed"] == "Read from the finished file by ffprobe."
    assert out["keeps"]["basis"] == "derived" and out["keeps"]["inputs"] == ["meta-voice-transcribe", "local-whisper"]
