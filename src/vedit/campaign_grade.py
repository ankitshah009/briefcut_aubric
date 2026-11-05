"""Grade a finished render: the deterministic lint, the requested edits, SAM's boxes, and the director's checks.

Everything here reads data a render already produced. Nothing calls a model,
so a receipt can be recomputed from the files alone.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from vedit.campaign_plan import _caption_provider, _plan_analysis
from vedit.captions import TRANSCRIBER, read_srt, translation_target
from vedit.director import describe_patch
from vedit.lint import hero_word_check, lint_plan, subject_cutout_check, subject_in_frame_check
from vedit.story import sentences_from_transcript

META_TRANSCRIBER = "meta-voice-transcribe"
LOCAL_TRANSCRIBER = "local-whisper"


def heard_by(analysis: dict[str, Any]) -> str | None:
    """The model that wrote the plan's transcript, or None when no model did.

    Meta Voice Transcribe is the only transcriber, and a plan it heard says so
    in ``transcribed_by``. A script's words are the writer's own. A plan saved
    before Briefcut went Meta-only names nobody; its transcript, if any, was
    written by local Whisper, so that is what the receipt says.
    """
    if analysis.get("transcribed_by") == TRANSCRIBER and analysis.get("transcribed"):
        return TRANSCRIBER
    method = analysis.get("method")
    if method == "script":
        return LOCAL_TRANSCRIBER if analysis.get("transcribed") else None
    if method in ("local-analysis", "meta"):
        heard = analysis.get("transcribed") or analysis.get("transcript") or analysis.get("words")
        return LOCAL_TRANSCRIBER if heard else None
    return None


def tracked_subject(request: dict[str, Any]) -> str | None:
    """The subject string tracking ran on, or None when no tracking was requested."""
    if request.get("tracking_disabled"):
        return None
    return request["subject"] if request.get("provider") == "meta" and request.get("subject") else None


def hero_spoken(plan: dict[str, Any], spoken: Any) -> dict[str, Any] | None:
    """The edit's hero word, with when Spark's pick is said when the word and window are still that pick."""
    hero = plan["edit"].get("hero_word")
    if not hero:
        return None
    if (
        isinstance(spoken, dict)
        and spoken.get("word") == hero["word"]
        and isinstance(spoken.get("start"), (int, float))
        and abs(float(spoken["start"]) - float(hero["start"])) < 1e-3
        and spoken.get("spoken")
    ):
        return dict(hero, spoken=spoken["spoken"])
    return dict(hero)


def emphasis_result(
    plan: dict[str, Any], picks: list[dict[str, Any]] | None, request: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """The punch words this render draws, each with the model's ``why`` when a model picked it.

    Translated captions draw none, so with ``request`` a translation reports none.
    """
    captions = plan["edit"].get("captions") or {}
    if not captions.get("emphasize") or (request is not None and translation_target(captions, request, plan=plan)):
        return []
    whys = {
        (pick.get("cue_key"), pick.get("word_index")): pick.get("why", "")
        for pick in picks or []
        if isinstance(pick, dict)
    }
    return [
        dict(pick, why=whys.get((pick["cue_key"], pick["word_index"]), "")) for pick in captions.get("emphasis") or []
    ]


def _plan_sentences(analysis: dict[str, Any], duration: float) -> list[dict[str, Any]]:
    """The sentences a planner measured, or ones grouped from its transcript when it kept only cues."""
    sentences = [item for item in (analysis.get("sentences") or []) if isinstance(item, dict)]
    if sentences or not analysis.get("transcript"):
        return sentences
    return sentences_from_transcript(
        [item for item in analysis["transcript"] if isinstance(item, dict)], duration=duration
    )


def _burned_captions(work: Path, request: dict[str, Any]) -> list[dict[str, float]] | None:
    """The caption cues the render really burned, read back from its SRT; None when captions were not requested.

    Requested and none burned is a cut with no captions, not a check that was never asked for.
    """
    if not request.get("captions"):
        return None
    srt = work / "captions.srt"
    if not srt.is_file():
        return []
    try:
        return [{"start": cue.start, "end": cue.end} for cue in read_srt(srt)]
    except (OSError, ValueError):
        return []


def _edits_check(edits: dict[str, Any] | None) -> dict[str, Any]:
    """Whether every instruction read from the brief made it into the render, in one sentence."""
    applied = list((edits or {}).get("decisions") or [])
    ignored = list((edits or {}).get("ignored") or [])
    if applied or ignored:
        note = "Applied: " + "; ".join(applied) + "." if applied else "Nothing from the brief was applied."
        if ignored:
            note += " Not applied: " + "; ".join(ignored) + "."
    else:
        note = "The brief asked for no specific edits."
    return {
        "name": "auto: requested edits applied",
        "passed": not ignored,
        "at": None,
        "note": note,
        "basis": "derived",
        "by": "brief-parser",
    }


def _director_checks(review: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The director's checks and patches as display data. Muse Spark or Muse Wonder reviewed, as the review says."""
    director = (
        str(review.get("director") or "muse-spark")
        if review.get("provider", "meta") == "meta"
        else str(review.get("provider"))
    )
    checks = [
        {
            "name": "director: " + str(check.get("name", "")),
            "passed": bool(check.get("passed")),
            "at": check.get("at"),
            "note": str(check.get("note", "")),
            "basis": "inferred",
            "by": director,
        }
        for check in review.get("checks") or []
        if isinstance(check, dict)
    ]
    patches = [dict(patch, source="director") for patch in review.get("patches") or [] if isinstance(patch, dict)]
    return checks, patches


def grade_render(
    plan: dict[str, Any],
    request: dict[str, Any],
    duration: float,
    work: Path,
    review: dict[str, Any] | None,
    *,
    edits: dict[str, Any] | None = None,
    tracking: list[dict[str, Any]] | None = None,
    cutout: dict[str, Any] | None = None,
    hero: dict[str, Any] | None = None,
    hero_stats: dict[str, Any] | None = None,
    cut_duration: float | None = None,
    source_size: tuple[float, float] | None = None,
    clips: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Lint the approved plan against the source analysis and merge the director's checks.

    Sentences and speech bands come from the plan's stored analysis when a
    planner measured them; captions are read back from the SRT the render
    actually burned. A check with nothing to measure is left out, not passed.
    ``clips`` are the project's clip ranges, so a moment that crosses one is caught.

    Every check says how it knows (``basis``) and what produced it (``by``).
    Code measured the lint, the requested edits and SAM's boxes (``derived``);
    the director's checks are Muse Spark's or Muse Wonder's judgment (``inferred``). ``inputs``
    names the models whose data a measurement read, only when the plan says so.
    """
    analysis = _plan_analysis(plan)
    sentences = _plan_sentences(analysis, duration)
    speech = [item for item in (analysis.get("speech") or []) if isinstance(item, dict)] or None
    captions = _burned_captions(work, request)
    transcriber = heard_by(analysis)
    # With no transcript these bands are only energy, and a music bed would read as talk nobody captioned.
    # A transcript that was made and holds no speech is still a measurement: there was nothing to caption.
    transcribed = bool(analysis.get("transcript")) or (bool(analysis.get("transcribed")) and transcriber is not None)
    heard = (speech or []) if captions is not None and transcribed else None
    captioned_by = None
    if captions is not None:
        provider = _caption_provider(request)
        if provider == "meta":
            captioned_by = META_TRANSCRIBER
        elif provider == "local":
            # The caption check needs the plan's transcript, and local captions are cut from it.
            captioned_by = transcriber
    graded = lint_plan(
        plan,
        target_duration=request["target_duration"],
        duration=duration,
        sentences=sentences or None,
        captions=captions,
        speech=heard,
        heard_by=transcriber,
        captioned_by=captioned_by,
        clips=clips,
    )
    checks = [*graded["checks"], _edits_check(edits)]
    patches = list(graded["patches"])
    if tracking:
        # SAM's boxes are evidence about the delivered cut; grade them like any other claim.
        width, height = source_size or (16, 9)
        checks.append(
            subject_in_frame_check(
                tracking,
                duration=cut_duration or float(plan["duration"]),
                aspect=request.get("aspect", "9:16"),
                source_width=width,
                source_height=height,
            )
        )
    if cutout:
        checks.append(subject_cutout_check(cutout))
    if hero:
        # Code measured it from Spark's word and SAM's matte: derived, by lint, reading both models.
        checks.append(hero_word_check(hero, hero_stats, duration=float(plan["duration"])))
    if isinstance(review, dict) and "error" not in review:
        director_checks, director_patches = _director_checks(review)
        checks.extend(director_checks)
        patches.extend(director_patches)
    patches = [dict(patch, summary=describe_patch(patch)) for patch in patches]
    # An advisory note is shown with the checks but is not a check: it never counts as a pass.
    counted = [check for check in checks if not check.get("advisory")]
    return {
        "checks": checks,
        "passed": sum(1 for check in counted if check["passed"]),
        "total": len(counted),
        "patches": patches,
    }
