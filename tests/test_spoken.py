from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from vedit.captions import find_whisper_model, transcribe

JFK = Path("/opt/homebrew/share/whisper-cpp/jfk.wav")


def _have_stt() -> bool:
    if find_whisper_model() and shutil.which("whisper-cli"):
        return True
    try:
        import faster_whisper  # noqa: F401

        return True
    except Exception:
        return False


@pytest.mark.speech
@pytest.mark.skipif(not JFK.is_file(), reason="jfk.wav not installed with whisper-cpp")
@pytest.mark.skipif(not _have_stt(), reason="no local whisper")
def test_jfk_transcription_contains_known_words(tmp_path: Path):
    cues = transcribe(JFK, tmp_path)
    assert cues
    blob = " ".join(c.text for c in cues).lower()
    assert any(word in blob for word in ("ask", "country", "president", "fellow", "american"))
    assert all(c.end > c.start for c in cues)
