"""Host capability report. The first command anyone should run."""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vedit.errors import VeditError
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

# Where a person creates the one key Briefcut needs.
META_KEY_URL = "https://dev.meta.ai"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = False


def package_manager() -> str | None:
    """The package manager a person on this machine would use, if any."""
    if sys.platform == "darwin":
        known = (shutil.which("brew"), "/opt/homebrew/bin/brew", "/usr/local/bin/brew")
        return "brew" if any(path and Path(path).is_file() for path in known) else None
    for name, tool in (("apt", "apt-get"), ("dnf", "dnf"), ("pacman", "pacman"), ("brew", "brew")):
        if shutil.which(tool):
            return name
    return None


_HOMEBREW = {
    "label": "Install Homebrew (brew.sh), the Mac package manager.",
    "command": '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"',
}


def ffmpeg_install() -> dict[str, Any]:
    """The exact commands that give this machine an FFmpeg Briefcut can use."""
    manager = package_manager()
    if sys.platform == "darwin":
        steps = [] if manager == "brew" else [_HOMEBREW]
        steps.append(
            {
                "label": "Install FFmpeg with titles and captions. Homebrew's plain ffmpeg has neither.",
                "command": "brew install ffmpeg-full",
            }
        )
        return {"steps": steps, "note": ""}
    commands = {
        "apt": "sudo apt-get update && sudo apt-get install -y ffmpeg fonts-dejavu-core fonts-noto-core fonts-noto-cjk",
        "pacman": "sudo pacman -S --needed ffmpeg ttf-dejavu",
        "brew": "brew install ffmpeg-full",
    }
    if manager in commands:
        return {"steps": [{"label": "Install FFmpeg.", "command": commands[manager]}], "note": ""}
    if manager == "dnf":
        return {
            "steps": [{"label": "Install FFmpeg.", "command": "sudo dnf install ffmpeg"}],
            "note": "On Fedora this package comes from RPM Fusion; enable that repository first.",
        }
    return {
        "steps": [],
        "note": "Install FFmpeg with your system's package manager. It needs libx264, drawtext, and subtitles.",
    }


def _ffmpeg_hint() -> str:
    steps = ffmpeg_install()["steps"]
    return steps[-1]["command"] if steps else "install an FFmpeg with drawtext and subtitles"


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
    except (VeditError, OSError) as exc:
        checks.append(Check("ffmpeg", False, str(exc), required=True))
        return checks
    try:
        probe = resolve_ffprobe()
        checks.append(Check("ffprobe", True, probe, required=True))
    except (VeditError, OSError) as exc:
        checks.append(Check("ffprobe", False, str(exc), required=True))

    full = FULL_FFMPEG.is_file()
    titles = has_filter("drawtext") and has_filter("subtitles")
    checks.append(
        Check(
            "ffmpeg-full",
            full or titles,
            str(FULL_FFMPEG)
            if full
            else "this build has drawtext and subtitles"
            if titles
            else f"no drawtext/subtitles here — {_ffmpeg_hint()}",
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
    if sys.platform == "darwin":
        # Hardware encode is an Apple option; on Linux the line would only read as a missing part.
        vt = has_encoder("h264_videotoolbox")
        checks.append(
            Check(
                "encoder:h264_videotoolbox",
                vt,
                "Apple Silicon HW encode (auto on Darwin; --no-hw to force libx264)"
                if vt
                else "not available, so software libx264 is used",
            )
        )
    checks.append(Check("encoder:aac", has_encoder("aac") or has_encoder("aac_at"), "AAC audio"))

    try:
        font = resolve_font()
        checks.append(Check("font", True, str(font)))
    except (VeditError, OSError) as exc:
        checks.append(Check("font", False, str(exc)))

    checks.append(meta_check())

    magick = which_optional("magick", "convert")
    checks.append(Check("imagemagick", bool(magick), magick or "optional title fallback"))
    sox = which_optional("sox")
    checks.append(Check("sox", bool(sox), sox or "optional audio diagnostics"))
    try:
        from vedit import blender_cards

        checks.append(Check(**blender_cards.doctor_check()))
    except (VeditError, OSError):  # an optional renderer must never hide the other checks
        checks.append(Check("blender", False, "Blender could not be checked, so 3D motion cards are hidden."))
    if sys.platform == "darwin":
        checks.append(Check("ffmpeg-full preferred", full, "VEDIT_FFMPEG can point at any build"))
    return checks


def meta_check() -> Check:
    """Briefcut's models are all Meta models, so a working key is required."""
    try:
        from vedit import meta

        status = meta.capabilities()
    except (VeditError, OSError, ValueError):  # an unreadable config must not hide the other checks
        return Check(
            "meta",
            False,
            "Meta settings could not be read. Open Settings in Studio and save your key again.",
            required=True,
        )
    if status.get("available") and status.get("last_problem"):
        # The key is set, but Meta refused the last call with it (401, 402, a certificate). Settings alone are not proof.
        return Check("meta", False, str(status.get("reason") or status["last_problem"]), required=True)
    if status.get("available"):
        return Check("meta", True, f"key configured, Meta calls allowed, mode {status.get('mode')}", required=True)
    if not status.get("configured"):
        return Check(
            "meta",
            False,
            f"no Meta API key. Create one at {META_KEY_URL}, then paste it in Studio Settings or set MODEL_API_KEY",
            required=True,
        )
    return Check(
        "meta",
        False,
        str(status.get("reason") or "Meta calls are not allowed. Turn them on in Settings."),
        required=True,
    )


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
        lines.append("Required tools and Meta access are present. Optional gaps only disable the features named above.")
    return "\n".join(lines) + "\n"
