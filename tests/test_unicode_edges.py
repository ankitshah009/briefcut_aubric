"""Words and file names in any script survive the places Briefcut trims or sends them."""

from __future__ import annotations

from urllib.parse import unquote

import pytest

from vedit.hero_word import _display, _norm
from vedit.studio.server import _attachment


@pytest.mark.parametrize(
    "said, shown, compared",
    [
        ("नमस्ते", "नमस्ते", "नमस्ते"),  # the final vowel sign is a combining mark and must stay
        ("oak,", "oak", "oak"),
        ('"Oak!"', "Oak!", "oak"),
        ("อ่าง.", "อ่าง", "อ่าง"),
        ("キッチン。", "キッチン", "キッチン"),
        ("...", "", ""),
    ],
)
def test_hero_words_keep_their_marks(said: str, shown: str, compared: str) -> None:
    assert _display(said) == shown
    assert _norm(said) == compared


def test_a_download_keeps_a_unicode_name() -> None:
    header = _attachment("キッチン 修理 🎬.mp4")
    assert header.startswith('attachment; filename="')
    fallback, encoded = header.split("; filename*=UTF-8''")
    assert fallback.isascii()
    assert unquote(encoded) == "キッチン 修理 🎬.mp4"


def test_heard_and_written_words_line_up_with_combining_marks() -> None:
    from vedit.director import _word_map

    heard = [{"text": "नमस्ते"}, {"text": "दोस्तों,"}]
    assert _word_map(heard, ["नमस्ते", "दोस्तों"]) == [0, 1]
