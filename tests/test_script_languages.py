"""Script to video paces and splits a script in any language, not only one written with spaces."""

from __future__ import annotations

from vedit import director

JAPANESE = "今日は市場に行きました。新鮮な野菜をたくさん買いました。"


def test_a_script_without_spaces_is_not_one_word_long() -> None:
    assert director.count_words("The blank is rough.") == 4
    assert director.count_words(JAPANESE) >= 10
    # 26 characters of Japanese need longer on screen than four English words.
    assert director.reading_seconds(JAPANESE) > director.reading_seconds("The blank is rough.") + 2


def test_sentences_end_at_full_width_marks_with_no_space_after() -> None:
    assert director._sentences("他说：“你好。”然后走了。真的吗？是的！") == [
        "他说：“你好。”",
        "然后走了。",
        "真的吗？",
        "是的！",
    ]
    assert director._sentences("यह पहला वाक्य है। यह दूसरा है।") == ["यह पहला वाक्य है।", "यह दूसरा है।"]


def test_latin_sentences_keep_numbers_and_closing_quotes() -> None:
    assert director._sentences('It costs 3.5 dollars. He said "Hi." Then he left') == [
        "It costs 3.5 dollars.",
        'He said "Hi."',
        "Then he left",
    ]


def test_one_long_japanese_block_is_shared_into_scenes_without_inserted_spaces() -> None:
    script = "今日は市場に行きました。" * 12
    scenes = director.split_script(script)
    assert len(scenes) > 1
    assert "".join(scenes) == script and not any(" " in scene for scene in scenes)
