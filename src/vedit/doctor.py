"""Host capability report. The first command anyone should run."""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from typing import Any

from vedit.captions import find_whisper_model
from vedit.media import (
    FULL_FFMPEG,
    has_encoder,
    has_filter,
    resolve_ffmpeg,
    resolve_ffprobe,
    resolve_font,
    version_line,
    which_optional,
)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = False


def run_doctor() -> list[Check]:
    checks: list[Check] = []
    py_ok = sys.version_info >= (3, 13)
    checks.append(
        Check(
            "python",
            py_ok,
            f"{sys.version.split()[0]} ({sys.executable})",
            required=True,
        )
    )
    try:
        ffmpeg = resolve_ffmpeg()
        checks.append(Check("ffmpeg", True, f"{version_line()} @ {ffmpeg}", required=True))
    except Exception as exc:
        checks.append(Check("ffmpeg", False, str(exc), required=True))
        return checks
    try:
        probe = resolve_ffprobe()
        checks.append(Check("ffprobe", True, probe, required=True))
    except Exception as exc:
        checks.append(Check("ffprobe", False, str(exc), required=True))

    full = FULL_FFMPEG.is_file()
    checks.append(
        Check(
            "ffmpeg-full",
            full or (has_filter("drawtext") and has_filter("subtitles")),
            str(FULL_FFMPEG) if full else "not installed — brew install ffmpeg-full for drawtext/subtitles",
        )
    )
    for filt, why in (
        ("xfade", "transitions / slideshows"),
        ("silencedetect", "silence cutting"),
        ("loudnorm", "EBU R128 loudness"),
        ("sidechaincompress", "music ducking"),
        ("drawtext", "titles"),
        ("subtitles", "caption burn-in"),
        ("hqdn3d", "denoise"),
        ("eq", "color"),
        ("overlay", "watermarks / PIP"),
        ("chromakey", "green screen"),
        ("scdet", "scene detect"),
    ):
        ok = has_filter(filt)
        checks.append(Check(f"filter:{filt}", ok, why if ok else f"missing — needed for {why}"))

    checks.append(Check("encoder:libx264", has_encoder("libx264"), "software H.264", required=True))
    vt = has_encoder("h264_videotoolbox")
    checks.append(
        Check(
            "encoder:h264_videotoolbox",
            vt,
            "Apple Silicon HW encode (auto on Darwin; --no-hw to force libx264)" if vt else "not available — software libx264",
        )
    )
    checks.append(Check("encoder:aac", has_encoder("aac") or has_encoder("aac_at"), "AAC audio"))

    try:
        font = resolve_font()
        checks.append(Check("font", True, str(font)))
    except Exception as exc:
        checks.append(Check("font", False, str(exc)))

    whisper = which_optional("whisper-cli")
    model = find_whisper_model()
    stub = find_whisper_model(allow_stub=True)
    if whisper and model:
        checks.append(Check("whisper-cli", True, f"{whisper}  model={model}"))
    elif whisper and stub:
        checks.append(
            Check(
                "whisper-cli",
                False,
                f"{whisper} found but only the empty brew stub ({stub.name}). Set VEDIT_WHISPER_MODEL to a real ggml .bin",
            )
        )
    elif whisper:
        checks.append(
            Check(
                "whisper-cli",
                False,
                f"{whisper} found but no ggml model. Set VEDIT_WHISPER_MODEL or drop ggml-base.en.bin in ~/.cache/whisper/",
            )
        )
    else:
        checks.append(Check("whisper-cli", False, "not on PATH — brew install whisper-cpp (captions optional)"))

    magick = which_optional("magick", "convert")
    checks.append(Check("imagemagick", bool(magick), magick or "optional title fallback"))
    sox = which_optional("sox")
    checks.append(Check("sox", bool(sox), sox or "optional audio diagnostics"))
    checks.append(Check("ffmpeg-full preferred", full, "VEDIT_FFMPEG can point at any build"))
    return checks


def doctor_dict() -> dict[str, Any]:
    checks = run_doctor()
    return {
        "ok": all(c.ok for c in checks if c.required),
        "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail, "required": c.required} for c in checks],
    }


def format_doctor(checks: list[Check]) -> str:
    lines = ["vedit doctor", ""]
    width = max(len(c.name) for c in checks)
    for check in checks:
        mark = "OK " if check.ok else "NO "
        req = " [required]" if check.required and not check.ok else ""
        lines.append(f"  {mark} {check.name.ljust(width)}  {check.detail}{req}")
    required_fail = [c for c in checks if c.required and not c.ok]
    lines.append("")
    if required_fail:
        lines.append("Host is not ready. Fix the required rows, then re-run `vedit doctor`.")
    else:
        lines.append("Required tools are present. Optional gaps only disable the features named above.")
    return "\n".join(lines) + "\n"


def suggest_whisper_model() -> str:
    dest = shutil.which("whisper-cli")
    return (
        "Download a real model (one-time, free):\n"
        "  mkdir -p ~/.cache/whisper && cd ~/.cache/whisper\n"
        "  curl -LO https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin\n"
        "  export VEDIT_WHISPER_MODEL=$HOME/.cache/whisper/ggml-base.en.bin\n"
        + (f"  whisper-cli is at: {dest}\n" if dest else "")
    )
