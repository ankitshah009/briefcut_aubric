"""In private mode, Studio says Muse Glimmer turns the director's note into changes, not Muse Spark."""

from pathlib import Path

from vedit import campaign

STUDIO = (Path(__file__).resolve().parents[1] / "src/vedit/studio/static/studio.js").read_text()


def test_capabilities_say_whether_private_mode_is_on(monkeypatch):
    monkeypatch.setattr(campaign, "_private_mode", lambda: True)
    assert campaign.capabilities()["private_mode"] is True
    monkeypatch.setattr(campaign, "_private_mode", lambda: False)
    assert campaign.capabilities()["private_mode"] is False


def test_the_note_hint_names_the_model_that_will_read_the_note():
    assert 'state.capabilities.private_mode ? "Muse Glimmer, on this computer, turns" : "Muse Spark turns"' in STUDIO
