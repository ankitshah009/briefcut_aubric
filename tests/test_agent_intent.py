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


def test_the_adjective_short_does_not_pick_the_shorts_frame():
    assert parse_intent("make a short product video").platform == "youtube"
    assert parse_intent("short intro for the team").platform == "youtube"
    assert parse_intent("a youtube short").platform == "shorts"
    assert parse_intent("cut shorts from talk.mp4").platform == "shorts"
    assert parse_intent("short under 20s of talk.mp4").platform == "shorts"


def test_captions_are_refused_in_every_plain_wording():
    for goal in ("no subtitles please", "don't add captions to clip.mp4", "do not burn in subtitles", "without captions", "no captions"):
        assert parse_intent(goal).captions is False, goal
    assert parse_intent("caption clip.mp4").captions is True


def test_an_audio_only_primary_is_never_its_own_music_bed():
    assert parse_intent("podcast from voice.wav", sources={"main": "/x/voice.wav"}, primary="main").music is None
    assert parse_intent("podcast", sources={"main": "/x/voice.wav"}).music is None, "main is the inventory's name for the primary"
    assert parse_intent("podcast", sources={"main": "/x/voice.wav", "bed": "/x/bed.mp3"}, primary="main").music == "/x/bed.mp3"
    assert parse_intent("podcast", sources={"talk": "/x/talk.mp4", "music": "/x/bed.mp3"}, primary="talk").music == "/x/bed.mp3"
