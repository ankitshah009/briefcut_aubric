"""Grade a deliverable against the intent and its platform contract."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from vedit.agent.model import Critique, Finding, Intent
from vedit.agent.policy import PlatformContract, contract_for
from vedit.errors import ProbeError
from vedit.media import probe


def critique_output(
    path: str | Path,
    intent: Intent,
    *,
    work_dir: Path | None = None,
    measure: bool = False,
    enforce_captions: bool = True,
) -> Critique:
    src = Path(path).expanduser()
    if not src.is_file():
        return _missing(f"Output {src} is missing (0 bytes on disk).")
    try:
        info = probe(src)
    except ProbeError as exc:
        detail = " ".join(str(exc).split()).rstrip(".")
        return _missing(f"Output {src} could not be probed (0 streams): {detail}.")
    primaries = info.video.color_primaries if info.video is not None else None
    checked = Path(work_dir) if work_dir is not None else None
    return _critique(
        intent,
        duration=_as_float(info.duration),
        width=int(info.width),
        height=int(info.height),
        has_video=bool(info.has_video),
        has_audio=bool(info.has_audio),
        work_dir=checked,
        measure=measure,
        media_path=info.path,
        color_primaries=primaries,
        enforce_captions=enforce_captions,
    )


def critique_report(report: dict, intent: Intent) -> Critique:
    outputs = report.get("outputs") if isinstance(report, dict) else None
    rows = [row for row in outputs if isinstance(row, dict)] if isinstance(outputs, list) else []
    chosen = next((row for row in rows if row.get("exists") and row.get("has_video")), None)
    if chosen is None:
        chosen = next((row for row in rows if row.get("exists")), None)
    if chosen is None:
        return _missing(f"Report lists {len(rows)} outputs and 0 of them exist.")
    checked: Path | None = None
    if isinstance(report, dict) and report.get("work_dir"):
        checked = Path(str(report["work_dir"]))
    return _critique(
        intent,
        duration=_as_float(chosen["duration"]) if "duration" in chosen else None,
        width=_as_int(chosen["width"]) if "width" in chosen else None,
        height=_as_int(chosen["height"]) if "height" in chosen else None,
        has_video=bool(chosen["has_video"]) if "has_video" in chosen else None,
        has_audio=bool(chosen["has_audio"]) if "has_audio" in chosen else None,
        work_dir=checked,
        measure=False,
        media_path=None,
        color_primaries=_report_primaries(chosen),
    )


def _critique(
    intent: Intent,
    *,
    duration: float | None,
    width: int | None,
    height: int | None,
    has_video: bool | None,
    has_audio: bool | None,
    work_dir: Path | None,
    measure: bool,
    media_path: Path | None,
    color_primaries: str | None,
    enforce_captions: bool = True,
) -> Critique:
    contract = contract_for(intent.platform)
    findings: list[Finding] = []
    _add(findings, _empty_finding(duration))
    _add(findings, _video_finding(contract, has_video))
    _add(findings, _audio_finding(intent, contract, has_audio))
    _add(findings, _frame_finding(contract, has_video, width, height))
    _add(findings, _duration_finding(intent, duration))
    if enforce_captions:
        _add(findings, _captions_finding(intent, work_dir))
    if measure and has_audio and media_path is not None:
        _add(findings, _loudness_finding(contract, media_path))
    shown_w = 0 if width is None else width
    shown_h = 0 if height is None else height
    _add(findings, _color_finding(contract, color_primaries, shown_w, shown_h))
    return _done(findings)


def _done(findings: list[Finding]) -> Critique:
    ok = not any(item.severity == "fail" for item in findings)
    return Critique(ok=ok, findings=tuple(findings))


def _missing(message: str) -> Critique:
    return Critique(
        ok=False,
        findings=(
            Finding(
                code="missing_output",
                severity="fail",
                message=message,
                fix="Add an export op and render again so the output file exists.",
            ),
        ),
    )


def _add(findings: list[Finding], item: Finding | None) -> None:
    if item is not None:
        findings.append(item)


def _empty_finding(duration: float | None) -> Finding | None:
    if duration is not None and duration > 0.05:
        return None
    shown = 0.0 if duration is None else duration
    if duration is None:
        message = "Duration is missing (0s readable), which is at or under the 0.05s minimum."
    else:
        message = f"Duration is {_fmt(shown)}s, which is at or under the 0.05s minimum."
    return Finding(
        code="empty_media",
        severity="fail",
        message=message,
        fix="Add a concat or slideshow op longer than 0.05s, then export again.",
    )


def _video_finding(contract: PlatformContract, has_video: bool | None) -> Finding | None:
    if has_video is not False or contract.width <= 0:
        return None
    return Finding(
        code="no_video",
        severity="fail",
        message=(
            f"Video is missing (0 video streams) but the {contract.name} contract "
            f"is {contract.width}x{contract.height}."
        ),
        fix=f"Add a fit or slideshow op and export at {contract.width}x{contract.height}.",
    )


def _audio_finding(intent: Intent, contract: PlatformContract, has_audio: bool | None) -> Finding | None:
    if has_audio is not False:
        return None
    platform = str(intent.platform or "").strip().lower()
    podcast = contract.name == "podcast" or platform == "podcast"
    preview = contract.name == "preview" or platform == "preview"
    if podcast:
        return Finding(
            code="no_audio",
            severity="fail",
            message="Audio is missing (0 audio streams) on the podcast deliverable.",
            fix="Add a music op before export so the podcast has an audio stream.",
        )
    if preview and not intent.music:
        message = "Audio is missing (0 audio streams) on the preview deliverable, which has no music."
        fix = "Add a music op before export if this preview should not be silent."
    elif intent.loudnorm and not preview:
        message = f"Audio is missing (0 audio streams) but loudnorm was requested for {contract.name}."
        fix = "Add a music op before the loudnorm op so the export has an audio stream."
    else:
        message = f"Audio is missing (0 audio streams) on the {contract.name} deliverable."
        fix = "Add a music op before export so the file has an audio stream."
    return Finding(code="no_audio", severity="warn", message=message, fix=fix)


def _frame_finding(
    contract: PlatformContract,
    has_video: bool | None,
    width: int | None,
    height: int | None,
) -> Finding | None:
    if not has_video or contract.width <= 0:
        return None
    w = 0 if width is None else width
    h = 0 if height is None else height
    if abs(w - contract.width) <= 8 and abs(h - contract.height) <= 8:
        return None
    if contract.reframe:
        fix = (
            f"Add a reframe op with aspect {contract.reframe} before export "
            f"so the frame is {contract.width}x{contract.height}."
        )
    else:
        fix = f"Add a fit op before export so the frame is {contract.width}x{contract.height}."
    return Finding(
        code="frame_size",
        severity="fail",
        message=(
            f"Frame is {w}x{h}, not the {contract.name} contract of "
            f"{contract.width}x{contract.height} (tolerance 8 pixels)."
        ),
        fix=fix,
    )


def _duration_finding(intent: Intent, duration: float | None) -> Finding | None:
    if intent.max_duration is None or duration is None:
        return None
    limit = float(intent.max_duration)
    if duration <= limit + 0.35:
        return None
    over = duration - limit
    return Finding(
        code="duration_over",
        severity="fail",
        message=(
            f"Duration is {_fmt(duration)}s, over the {_fmt(limit)}s limit "
            f"by {_fmt(over)}s (slack is 0.35s)."
        ),
        fix=f"Add a trim op ending at {_fmt(limit)}s, or a cut_silence op, before export.",
    )


def _captions_finding(intent: Intent, work_dir: Path | None) -> Finding | None:
    if not intent.captions:
        return None
    if work_dir is None:
        return Finding(
            code="captions_missing",
            severity="warn",
            message="Captions were requested but captions.srt was not checked (0 bytes seen).",
            fix="Add a captions op before export so captions.srt is written.",
        )
    srt = Path(work_dir) / "captions.srt"
    size = srt.stat().st_size if srt.is_file() else 0
    if size >= 20:
        return None
    return Finding(
        code="captions_missing",
        severity="fail",
        message=f"captions.srt is {size} bytes, under the 20 byte minimum.",
        fix="Add a captions op before export so captions.srt is written.",
    )


def _loudness_finding(contract: PlatformContract, media_path: Path) -> Finding | None:
    from vedit.analyze import measure_loudness

    target = float(contract.target_lufs)
    try:
        measured = measure_loudness(media_path)
        measured_i = float(measured["input_i"])
    except Exception as exc:
        detail = " ".join(str(exc).split()).rstrip(".") or "unknown error"
        return Finding(
            code="loudness",
            severity="warn",
            message=f"Loudness measurement failed with 0 readings ({detail}).",
            fix=f"Add a loudnorm op targeting {_fmt(target)} LUFS before export.",
        )
    delta = abs(measured_i - target)
    if delta <= 2.5:
        return None
    return Finding(
        code="loudness",
        severity="fail",
        message=(
            f"Integrated loudness is {_fmt(measured_i)} LUFS, {_fmt(delta)} LUFS "
            f"away from the {_fmt(target)} LUFS target."
        ),
        fix=f"Add a loudnorm op targeting {_fmt(target)} LUFS before export.",
    )


def _color_finding(
    contract: PlatformContract,
    primaries: str | None,
    width: int,
    height: int,
) -> Finding | None:
    if contract.name == "preview" or primaries is None:
        return None
    text = str(primaries).strip().lower()
    if text in {"", "bt709"}:
        return None
    return Finding(
        code="color_tags",
        severity="warn",
        message=f"Color primaries are {primaries}, not bt709, on the {width}x{height} frame.",
        fix="Add an export op with a bt709 preset so color primaries are bt709.",
    )


def _report_primaries(row: dict[str, Any]) -> str | None:
    raw = row.get("color_primaries")
    if raw:
        return str(raw)
    color = row.get("color")
    if isinstance(color, dict) and color.get("primaries"):
        return str(color["primaries"])
    return None


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number):
        return None
    return number


def _as_int(value: Any) -> int | None:
    number = _as_float(value)
    if number is None:
        return None
    return int(round(number))


def _fmt(value: float) -> str:
    text = f"{float(value):.3f}".rstrip("0").rstrip(".")
    return text or "0"
