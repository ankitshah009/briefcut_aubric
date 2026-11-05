"""The edit is bounded data, never a path or a command."""

from __future__ import annotations

import pytest

from vedit.edit_spec import (
    TEMPLATES,
    EditError,
    asset_ids,
    catalogue,
    default_edit,
    merge_edit,
    template_edit,
    validate_edit,
)

ASSETS = {"a1b2c3d4": {"kind": "audio"}, "e5f6a7b8": {"kind": "image"}}


def test_defaults_validate_and_change_nothing() -> None:
    assert validate_edit(None, duration=10) == default_edit()


def test_every_template_validates() -> None:
    for row in TEMPLATES:
        validate_edit(template_edit(row["id"]), duration=30)
    with pytest.raises(EditError):
        template_edit("nope")


def test_recap_template_does_not_enable_stabilization_by_default() -> None:
    assert template_edit("recap")["stabilize"] is False


def test_catalogue_lists_choices_and_templates() -> None:
    data = catalogue()
    assert {item["id"] for item in data["looks"]} >= {"none", "auto", "warm", "cinematic"}
    assert {item["id"] for item in data["transitions"]} >= {"none", "fade"}
    assert data["defaults"]["export"]["quality"] == "1080p"


@pytest.mark.parametrize(
    "patch",
    [
        {"look": {"preset": "'; rm -rf /"}},
        {"transition": {"type": "xfade=evil"}},
        {"look": {"intensity": float("nan")}},
        {"captions": {"color": "red; drop"}},
        {"captions": {"edits": {"abc": "x"}}},
        {"music": {"asset": "../../etc/passwd"}},
        {"logo": {"asset": "/tmp/logo.png"}},
        {"export": {"format": "exe"}},
        {"cutout": {"mode": "blur", "subject": ""}},
        {"texts": [{"text": "bad\u0007", "duration": 1}]},
        {"removed": [[3, 1]]},
    ],
)
def test_hostile_or_malformed_values_are_refused(patch) -> None:
    with pytest.raises(EditError):
        validate_edit(patch, duration=10, assets=ASSETS)


def test_assets_must_belong_to_the_project_and_match_their_kind() -> None:
    assert validate_edit({"music": {"asset": "a1b2c3d4"}}, duration=10, assets=ASSETS)["music"]["duck"] is True
    with pytest.raises(EditError, match="not an asset"):
        validate_edit({"music": {"asset": "ffffffff"}}, duration=10, assets=ASSETS)
    with pytest.raises(EditError, match="must be audio"):
        validate_edit({"music": {"asset": "e5f6a7b8"}}, duration=10, assets=ASSETS)


def test_text_items_are_bounded_to_the_story() -> None:
    clean = validate_edit({"texts": [{"text": "  Hello   there ", "start": 1, "duration": 2}]}, duration=10)
    assert clean["texts"][0]["text"] == "Hello there"
    with pytest.raises(EditError):
        validate_edit({"texts": [{"text": "x", "start": 11, "duration": 1}]}, duration=10)


def test_unknown_keys_are_dropped_and_merge_is_deep() -> None:
    clean = validate_edit({"run": "this", "look": {"preset": "warm", "exec": "x"}}, duration=10)
    assert "run" not in clean and "exec" not in clean["look"] and clean["look"]["preset"] == "warm"
    merged = merge_edit(default_edit(), {"look": {"preset": "mono"}, "texts": [{"text": "a"}]})
    assert merged["look"]["intensity"] == 1.0 and merged["texts"] == [{"text": "a"}]


def test_asset_ids_lists_every_reference() -> None:
    edit = default_edit()
    edit["music"] = {"asset": "a1b2c3d4"}
    edit["overlays"] = [{"asset": "e5f6a7b8"}]
    assert asset_ids(edit) == ["a1b2c3d4", "e5f6a7b8"]


@pytest.mark.parametrize("language", ["en", "pt-BR", "es-419", "zh-Hant-TW", "yue"])
def test_caption_languages_accept_the_same_tags_as_emphasis(language: str) -> None:
    from vedit.emphasis import _LANGUAGE

    assert validate_edit({"captions": {"language": language}}, duration=10)["captions"]["language"] == language
    assert _LANGUAGE.fullmatch(language)


@pytest.mark.parametrize("language", ["English", "e", "en_US", "zh-Hant-TW-x", "en-", "EN"])
def test_caption_languages_that_are_not_tags_are_refused(language: str) -> None:
    with pytest.raises(EditError, match="language code"):
        validate_edit({"captions": {"language": language}}, duration=10)
