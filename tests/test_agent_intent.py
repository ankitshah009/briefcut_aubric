from vedit.agent.intent import parse_intent
from vedit.agent.model import Intent


def test_short_under_30():
    intent = parse_intent("cut silence and caption talk.mp4 as a short under 30s")
    assert intent.platform == "shorts"
    assert intent.max_duration == 30
    assert intent.cut_silence is True
    assert intent.captions is True
    assert intent.reframe == "9:16"
    assert intent.loudnorm is True
    assert intent.highpass is True


def test_podcast_no_captions():
    intent = parse_intent("podcast interview.mp4 with bed.wav, no captions")
    assert intent.platform == "podcast"
    assert intent.captions is False
    assert intent.music.endswith("bed.wav")
    assert intent.cut_silence is True


def test_preview_keeps_pauses():
    intent = parse_intent("quick preview of clip.mp4, keep the pauses")
    assert intent.platform == "preview"
    assert intent.cut_silence is False
    assert intent.captions is False
    assert intent.loudnorm is False
    assert intent.highpass is False
    assert intent.reframe is None


def test_square_and_title():
    intent = parse_intent('square launch film title "Launch day"')
    assert intent.platform == "square"
    assert intent.reframe == "1:1"
    assert intent.title == "Launch day"


def test_intent_roundtrip():
    original = Intent(platform="shorts", max_duration=30, images=["a.png", "b.png"])
    restored = Intent.from_dict(original.to_dict())
    assert restored.platform == "shorts"
    assert restored.max_duration == 30
    assert list(restored.images) == ["a.png", "b.png"]
    assert original.to_dict()["images"] == ["a.png", "b.png"]


def test_unknown_words_do_not_raise():
    intent = parse_intent("make it feel expensive and calm for the board")
    assert isinstance(intent, Intent)
    assert intent.platform == "youtube"
