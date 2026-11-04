"""Meta Voice Transcribe text is mended once, where it enters Briefcut."""

from __future__ import annotations

import pytest

from vedit.captions import heard_cues, mend_joined_doubles


@pytest.mark.parametrize(
    "heard, mended",
    [
        ("ThisThis is the finished oak bowl.", "This is the finished oak bowl."),
        ("And HereHere is the blank.", "And Here is the blank."),
        ("It's It's fine.", "It's It's fine."),  # separate doubles are polish_cues' job
    ],
)
def test_a_word_joined_to_its_copy_is_mended(heard: str, mended: str) -> None:
    assert mend_joined_doubles(heard) == mended


@pytest.mark.parametrize("word", ["mama", "Mama", "tutu", "Bonbon", "couscous", "Dodo", "OKOK", "Bora Bora"])
def test_real_words_that_repeat_stay_as_they_are(word: str) -> None:
    assert mend_joined_doubles(word) == word


def test_heard_lines_carry_the_mended_text() -> None:
    lines = heard_cues([{"start": 0.0, "end": 2.4, "text": "ThisThis is the finished oak bowl."}])
    assert " ".join(line.text for line in lines) == "This is the finished oak bowl."
