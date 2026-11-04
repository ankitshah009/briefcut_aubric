"""Typed failures. Messages are written for a human standing at a terminal."""


class VeditError(Exception):
    """Any editor failure the user can act on.

    ``str(error)`` is for a terminal and may hold paths and commands. ``plain``,
    when set, is the sentence safe to show the person who chose the file.
    """

    def __init__(self, *args: object, plain: str | None = None) -> None:
        super().__init__(*args)
        self.plain = plain


class BinaryMissing(VeditError):
    """ffmpeg, ffprobe, whisper-cli, or magick is not on PATH."""


class ProbeError(VeditError):
    """ffprobe could not read a media file."""


class BriefError(VeditError):
    """The YAML/JSON brief is malformed or missing a required field."""


class CompileError(VeditError):
    """The brief cannot be turned into a render pipeline."""


class RenderError(VeditError):
    """An FFmpeg (or other) step failed. ``step`` and ``detail`` carry its name and stderr."""

    def __init__(self, *args: object, plain: str | None = None, step: str = "", detail: str = "") -> None:
        super().__init__(*args, plain=plain)
        self.step = step
        self.detail = detail


class RenderCancelled(RenderError):
    """The user cancelled a render and its running processes were stopped."""


class AnalysisError(VeditError):
    """Silence / scene / loudness analysis failed."""


class CaptionError(VeditError):
    """Transcription or caption burn-in failed."""
