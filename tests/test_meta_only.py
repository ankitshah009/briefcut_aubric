"""Briefcut calls only Meta models at runtime. These files are where a model could be called."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "src" / "vedit"
RUNTIME = (
    "captions.py",
    "story.py",
    "campaign.py",
    "director.py",
    "media.py",
    "compile.py",
    "render_plan.py",
    "lint.py",
    "meta.py",
)
# A local speech model, a local speech synthesizer, or any other model runtime.
FORBIDDEN = re.compile(
    r"faster_whisper|WhisperModel|whisper-cli\"|\"-ojf\"|which\(\"say\"\)|\[say\b|import (?:torch|onnxruntime|transformers|whisper)\b"
)


@pytest.mark.parametrize("name", RUNTIME)
def test_runtime_code_starts_no_local_model(name: str) -> None:
    text = (SOURCE / name).read_text(encoding="utf-8")
    found = [line.strip() for line in text.splitlines() if FORBIDDEN.search(line)]
    assert not found, f"{name} reaches a non-Meta model: {found}"


def test_the_retired_finders_find_nothing() -> None:
    from vedit.captions import find_whisper_model
    from vedit.media import find_whisper_cli

    assert find_whisper_cli() is None and find_whisper_model() is None and find_whisper_model(allow_stub=True) is None


def test_the_only_transcriber_is_meta_voice_transcribe() -> None:
    from vedit import captions, meta

    assert captions.TRANSCRIBER == "meta-voice-transcribe" and meta._TRANSCRIBE_MODEL == "muse-voice-transcribe-1.0"
