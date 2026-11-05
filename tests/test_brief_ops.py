"""The brief's editing instructions become edit settings, sentences, or a stated reason."""

from __future__ import annotations

from vedit.brief_ops import MAX_ITEM_CHARS, MAX_ITEMS, bounded_strings, brief_edit
from vedit.edit_spec import default_edit, merge_edit, validate_edit

SPEECH = [{"start": 0.0, "end": 10.0}, {"start": 12.0, "end": 25.0}]
PLAN = {
    "title": "The Oak Bowl",
    "duration": 25.0,
    "segments": [{"start": 0.0, "end": 25.0}],
    "analysis": {"speech": SPEECH},
}


def _texts(result: dict) -> list[dict]:
    return result["edit"].get("texts", [])


def test_demo_goal_trims_titles_and_fades() -> None:
    goal = "Make a 25-second vertical story about the oak bowl: open with the finished bowl, then show how it is made. Trim the pauses, add a title card, fade out at the end."
    result = brief_edit(goal, plan=PLAN, has_audio=True)
    assert result["edit"]["trim_pauses"] is True and result["edit"]["fade"] == {"out": 0.5}
    title = _texts(result)[0]
    assert title == {"text": "The Oak Bowl", "start": 0.0, "duration": 2.5, "style": "title", "position": "center"}
    assert result["decisions"] == [
        "Removed pauses longer than 0.6 s inside the selected moments",
        "Title card ‘The Oak Bowl’ for 2.5 s",
        "Fade out 0.5 s",
    ]
    assert result["ignored"] == []


def test_a_trim_that_finds_nothing_says_so() -> None:
    unbroken = dict(PLAN, analysis={"speech": [{"start": 0.0, "end": 25.0}]})
    result = brief_edit("trim the pauses", plan=unbroken, has_audio=True)
    assert result["edit"]["trim_pauses"] is True
    assert result["decisions"] == ["Found no pauses longer than 0.6 s inside the selected moments"]


def test_the_settings_are_a_valid_edit() -> None:
    goal = "denoise, trim the pauses, add a title card, hook: Watch this, cta: Order yours, fade, don't normalize"
    result = brief_edit(goal, plan=PLAN, has_audio=True)
    clean = validate_edit(merge_edit(default_edit(), result["edit"]), duration=25.0)
    assert clean["denoise_picture"] is True and clean["trim_pauses"] is True
    assert clean["audio"]["keep_loudness"] is True
    assert clean["fade"] == {"in": 0.5, "out": 0.5}
    assert [item["text"] for item in clean["texts"]] == ["The Oak Bowl", "Watch this", "Order yours"]


def test_quoted_title_wins_over_plan_title() -> None:
    result = brief_edit("add a title card 'The oak bowl' and fade in", plan=PLAN, has_audio=True)
    assert _texts(result)[0]["text"] == "The oak bowl" and result["edit"]["fade"] == {"in": 0.5}
    assert result["decisions"][0] == "Title card ‘The oak bowl’ for 2.5 s"
    direct = brief_edit('title "Made by hand"', plan=PLAN, has_audio=True)
    assert _texts(direct)[0]["text"] == "Made by hand"


def test_plain_fade_means_both_and_no_loudnorm_sets_the_flag() -> None:
    result = brief_edit("Keep it warm, fade, don't normalize", plan=PLAN, has_audio=True)
    assert result["edit"]["fade"] == {"in": 0.5, "out": 0.5}
    assert result["edit"]["audio"] == {"keep_loudness": True}
    assert "Kept the original loudness (no normalization)" in result["decisions"]
    assert brief_edit("no loudnorm", plan=PLAN, has_audio=True)["edit"]["audio"]["keep_loudness"] is True
    assert "audio" not in brief_edit("remove the pauses", plan=PLAN, has_audio=True)["edit"]
    assert "audio" not in brief_edit("no loudnorm", plan=PLAN, has_audio=False)["edit"]


def test_pause_trimming_needs_audio_and_respects_keep() -> None:
    silent = brief_edit("cut silence, remove dead air", plan=PLAN, has_audio=False)
    assert silent["edit"] == {} and silent["decisions"] == []
    assert silent["ignored"] == ["Pause trimming: this footage has no audio track to find pauses in"]
    kept = brief_edit("keep the pauses, trim the pauses later", plan=PLAN, has_audio=True)
    assert "trim_pauses" not in kept["edit"]
    for phrase in ("trim the pauses", "remove silence", "cut the dead air", "drop long pauses", "cut silence"):
        assert brief_edit(phrase, plan=PLAN, has_audio=True)["edit"].get("trim_pauses") is True, phrase


def test_denoise_hook_and_cta_use_timed_text() -> None:
    result = brief_edit("denoise. hook: Watch this bowl appear, cta: Order yours today", plan=PLAN, has_audio=True)
    assert result["edit"]["denoise_picture"] is True
    hook, cta = _texts(result)
    assert (
        hook["text"] == "Watch this bowl appear"
        and hook["start"] == 0
        and hook["duration"] == 3.0
        and hook["position"] == "top"
    )
    assert (
        cta["text"] == "Order yours today"
        and cta["start"] == 22.0
        and cta["duration"] == 3.0
        and cta["position"] == "bottom"
    )
    assert result["decisions"][0] == "Reduced picture noise"
    assert result["decisions"][1].startswith("Hook ‘Watch this bowl appear’")
    assert result["decisions"][2].startswith("Call to action ‘Order yours today’")
    # With a title card the hook waits for the card to leave.
    with_title = brief_edit("add a title card. hook: Watch this", plan=PLAN, has_audio=True)
    assert [item["start"] for item in _texts(with_title)] == [0, 2.5]


def test_cta_is_timed_from_the_real_end_of_the_story() -> None:
    plan = dict(
        PLAN,
        segments=[{"start": 0.0, "end": 10.0}, {"start": 20.0, "end": 30.0}],
        analysis={"speech": [{"start": 0.0, "end": 2.0}, {"start": 8.0, "end": 10.0}, {"start": 20.0, "end": 30.0}]},
    )
    # Trimming the 6 s pause shortens the 20 s story; the card follows the real end.
    trimmed = brief_edit("trim the pauses, cta: Learn more", plan=plan, has_audio=True)
    assert _texts(trimmed)[0]["start"] < 17.0
    # A template's transition also moves the end.
    overlapped = brief_edit(
        "cta: Learn more", plan=plan, has_audio=True, alongside={"transition": {"type": "fade", "seconds": 1.0}}
    )
    assert _texts(overlapped)[0]["start"] == 16.0
    short = brief_edit("cta: Learn more", plan=dict(plan, segments=[{"start": 0.0, "end": 2.0}]), has_audio=True)
    assert _texts(short)[0]["start"] == 0 and _texts(short)[0]["duration"] == 2.0
    unknown = brief_edit("cta: Learn more", plan={"title": "x"}, has_audio=True)
    assert unknown["edit"] == {} and "not known yet" in unknown["ignored"][0]


def test_the_final_edit_decides_what_the_receipts_claim() -> None:
    goal = "trim the pauses, add a title card, hook: Watch this, cta: Order yours, fade out, denoise, no loudnorm"
    asked = brief_edit(goal, plan=PLAN, has_audio=True)
    assert len(asked["decisions"]) == 7 and asked["ignored"] == []
    approved = merge_edit(default_edit(), asked["edit"])
    assert brief_edit(goal, plan=PLAN, has_audio=True, final=approved)["decisions"] == asked["decisions"]
    changed = merge_edit(
        approved,
        {
            "trim_pauses": False,
            "denoise_picture": False,
            "fade": {"out": 0},
            "texts": [],
            "audio": {"keep_loudness": False},
        },
    )
    result = brief_edit(goal, plan=PLAN, has_audio=True, final=changed)
    assert result["decisions"] == []
    assert [line.split(":")[0] for line in result["ignored"]] == [
        "Pause trimming",
        "Picture noise reduction",
        "Title card",
        "Hook",
        "Call to action",
        "Fade out",
        "Original loudness",
    ]
    assert all(line.endswith("changed in the editor") for line in result["ignored"])


def test_unsupported_requests_are_listed_and_unknown_words_ignored() -> None:
    result = brief_edit("add our watermark logo.png and music bed.mp3, make it sparkle", plan=PLAN, has_audio=True)
    assert result["edit"] == {}
    assert len(result["ignored"]) == 2
    assert result["ignored"][0].startswith("Watermark") and result["ignored"][1].startswith("Music bed")
    assert brief_edit("Show the product clearly", plan=PLAN, has_audio=True) == {
        "edit": {},
        "decisions": [],
        "ignored": [],
    }


def test_title_text_is_bounded_and_unescaped() -> None:
    long_title = "A " * 60 + "bowl"
    result = brief_edit("add a title card", plan={"title": long_title}, has_audio=True)
    card = _texts(result)[0]["text"]
    assert len(card) <= 81  # 80 chars plus the ellipsis
    raw = brief_edit("add a title card", plan={"title": "Bowl: 100% oak 'n' ash"}, has_audio=True)
    assert _texts(raw)[0]["text"] == "Bowl: 100% oak 'n' ash"  # the compile layer escapes
    empty = brief_edit("add a title card", plan={"title": ""}, has_audio=True)
    assert "texts" not in empty["edit"] and empty["ignored"] == [
        "Title card: the brief and the plan give no title text"
    ]
    for sentence in result["decisions"]:
        assert len(sentence) <= MAX_ITEM_CHARS


def test_bounded_strings_limits_count_and_length() -> None:
    items = ["x" * 500] * 40 + [3, "", "  ", "bad\x00char"]
    out = bounded_strings(items)
    assert len(out) == MAX_ITEMS
    assert all(len(item) <= MAX_ITEM_CHARS for item in out)
    assert bounded_strings("not a list") == [] and bounded_strings(None) == []
