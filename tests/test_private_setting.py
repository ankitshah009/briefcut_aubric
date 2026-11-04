"""The private-mode setting is validated, saved and read back by the open-weights router."""

from __future__ import annotations

from pathlib import Path

import pytest

from vedit import open_weights, settings


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("VEDIT_HOME", str(tmp_path))
    monkeypatch.delenv("VEDIT_PRIVATE_MODEL", raising=False)
    return tmp_path


def test_private_mode_is_off_until_chosen(config: Path) -> None:
    assert settings.public()["private_model"] == ""
    assert open_weights.enabled() is False


def test_choosing_glimmer_turns_private_mode_on_and_off_again(config: Path) -> None:
    assert settings.update({"private_model": "glimmer"})["private_model"] == "glimmer"
    assert open_weights.enabled() is True
    assert settings.update({"private_model": ""})["private_model"] == ""
    assert "private_model" not in settings.load()
    assert open_weights.enabled() is False


@pytest.mark.parametrize("value", ["llama", "GLIMMER ", True, 1, None])
def test_anything_else_is_refused_and_nothing_is_saved(config: Path, value: object) -> None:
    with pytest.raises(settings.SettingsError):
        settings.update({"private_model": value, "default_language": "es"})
    assert "default_language" not in settings.load()


def test_the_environment_wins_and_is_reported(config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VEDIT_PRIVATE_MODEL", "glimmer")
    assert open_weights.enabled() is True
    assert settings.public()["private_model_source"] == "environment"
