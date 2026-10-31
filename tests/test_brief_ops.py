"""The brief's editing instructions become compile ops, sentences, or a stated reason."""

from __future__ import annotations

from vedit.brief_ops import MAX_ITEM_CHARS, MAX_ITEMS, bounded_strings, edit_decisions

PLAN = {"title": "The Oak Bowl", "duration": 25.0}


def _ops(result: dict) -> list[str]:
    return [op["op"] for op in result["ops"]]


def test_demo_goal_trims_titles_and_fades() -> None:
    goal = "Make a 25-second vertical story about the oak bowl: open with the finished bowl, then show how it is made. Trim the pauses, add a title card, fade out at the end."
    result = edit_decisions(goal, plan=PLAN, has_audio=True, canvas=(1080, 1920))
    assert _ops(result) == ["cut_silence", "title", "fade_out"]
    cut = result["ops"][0]
    assert cut["min_silence"] == 0.6 and cut["threshold_db"] == -32.0
    title = result["ops"][1]
    assert title["text"] == "The Oak Bowl" and title["start"] == 0 and title["duration"] == 2.5 and title["box"] is True
    assert 0.3 <= title["y_ratio"] <= 0.7  # inside the 9:16 safe area
    assert 36 <= title["size"] <= 96
    assert result["ops"][2] == {"op": "fade_out", "duration": 0.5}
    assert result["decisions"] == [
        "Removed pauses longer than 0.6 s inside the selected moments",
        "Title card ‘The Oak Bowl’ for 2.5 s",
        "Fade out 0.5 s",
    ]
    assert result["ignored"] == [] and result["loudnorm"] is True


def test_quoted_title_wins_over_plan_title() -> None:
    result = edit_decisions("add a title card 'The oak bowl' and fade in", plan=PLAN, has_audio=True)
    assert _ops(result) == ["title", "fade_in"]
    assert result["ops"][0]["text"] == "The oak bowl"
    assert result["decisions"][0] == "Title card ‘The oak bowl’ for 2.5 s"
    direct = edit_decisions('title "Made by hand"', plan=PLAN, has_audio=True)
    assert direct["ops"][0]["text"] == "Made by hand"


def test_plain_fade_means_both_and_no_loudnorm_sets_the_flag() -> None:
    result = edit_decisions("Keep it warm, fade, don't normalize", plan=PLAN, has_audio=True)
    assert _ops(result) == ["fade_in", "fade_out"]
    assert result["loudnorm"] is False
    assert "Kept the original loudness (no normalization)" in result["decisions"]
    assert edit_decisions("no loudnorm", plan=PLAN, has_audio=True)["loudnorm"] is False
    assert edit_decisions("remove the pauses", plan=PLAN, has_audio=True)["loudnorm"] is True


def test_pause_trimming_needs_audio_and_respects_keep() -> None:
    silent = edit_decisions("cut silence, remove dead air", plan=PLAN, has_audio=False)
    assert _ops(silent) == [] and silent["decisions"] == []
    assert silent["ignored"] == ["Pause trimming: this footage has no audio track to find pauses in"]
    kept = edit_decisions("keep the pauses, trim the pauses later", plan=PLAN, has_audio=True)
    assert "cut_silence" not in _ops(kept)
    for phrase in ("trim the pauses", "remove silence", "cut the dead air", "drop long pauses", "cut silence"):
        assert "cut_silence" in _ops(edit_decisions(phrase, plan=PLAN, has_audio=True)), phrase


def test_denoise_hook_and_cta_use_timed_text() -> None:
    result = edit_decisions("denoise. hook: Watch this bowl appear, cta: Order yours today", plan=PLAN, has_audio=True, canvas=(1080, 1920))
    assert _ops(result) == ["denoise", "title", "title"]
    hook, cta = result["ops"][1], result["ops"][2]
    assert hook["text"] == "Watch this bowl appear" and hook["start"] == 0 and hook["duration"] == 3.0
    assert cta["text"] == "Order yours today" and cta["start"] == 22.0 and cta["duration"] == 3.0
    assert result["decisions"][0] == "Reduced picture noise"
    assert result["decisions"][1].startswith("Hook ‘Watch this bowl appear’")
    assert result["decisions"][2].startswith("Call to action ‘Order yours today’")
    # With a title card the hook waits for the card to leave.
    with_title = edit_decisions("add a title card. hook: Watch this", plan=PLAN, has_audio=True)
    assert [op["start"] for op in with_title["ops"]] == [0, 2.5]


def test_cta_is_declined_with_a_reason_when_the_final_length_is_unknown() -> None:
    result = edit_decisions("trim the pauses, cta: Learn more", plan=PLAN, has_audio=True)
    assert _ops(result) == ["cut_silence"]
    assert len(result["ignored"]) == 1 and result["ignored"][0].startswith("Call to action ‘Learn more’")
    unknown = edit_decisions("cta: Learn more", plan={"title": "x"}, has_audio=True)
    assert unknown["ops"] == [] and "not known yet" in unknown["ignored"][0]


def test_unsupported_requests_are_listed_and_unknown_words_ignored() -> None:
    result = edit_decisions("add our watermark logo.png and music bed.mp3, make it sparkle", plan=PLAN, has_audio=True)
    assert result["ops"] == []
    assert len(result["ignored"]) == 2
    assert result["ignored"][0].startswith("Watermark") and result["ignored"][1].startswith("Music bed")
    assert edit_decisions("Show the product clearly", plan=PLAN, has_audio=True) == {"ops": [], "decisions": [], "ignored": [], "loudnorm": True}


def test_title_text_is_bounded_wrapped_and_unescaped() -> None:
    long_title = "A " * 60 + "bowl"
    result = edit_decisions("add a title card", plan={"title": long_title}, has_audio=True, canvas=(1080, 1920))
    card = result["ops"][0]["text"]
    assert len(card.replace("\n", " ")) <= 81  # 80 chars plus the ellipsis
    assert card.count("\n") <= 2
    assert result["ops"][0]["size"] >= 36
    raw = edit_decisions("add a title card", plan={"title": "Bowl: 100% oak 'n' ash"}, has_audio=True)
    assert raw["ops"][0]["text"] == "Bowl: 100% oak 'n' ash"  # the compile layer escapes
    empty = edit_decisions("add a title card", plan={"title": ""}, has_audio=True)
    assert empty["ops"] == [] and empty["ignored"] == ["Title card: the brief and the plan give no title text"]
    for sentence in result["decisions"]:
        assert len(sentence) <= MAX_ITEM_CHARS


def test_bounded_strings_limits_count_and_length() -> None:
    items = ["x" * 500] * 40 + [3, "", "  ", "bad\x00char"]
    out = bounded_strings(items)
    assert len(out) == MAX_ITEMS
    assert all(len(item) <= MAX_ITEM_CHARS for item in out)
    assert bounded_strings("not a list") == [] and bounded_strings(None) == []
