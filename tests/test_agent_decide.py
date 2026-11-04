"""Platform contracts and editorial decisions. No media, no ffmpeg."""

from __future__ import annotations

import pytest

from vedit.agent.decide import decide, silence_threshold_db
from vedit.agent.model import Intent, Inventory, SourceShot
from vedit.agent.policy import canvas_for, contract_for
from vedit.errors import BriefError


def test_shorts_contract() -> None:
    shorts = contract_for("shorts")
    assert shorts.presets == ("shorts_9x16",)
    assert shorts.width == 1080
    assert shorts.height == 1920
    assert shorts.target_lufs == -14
    assert shorts.max_duration == 60


def test_reels_aliases_shorts() -> None:
    assert contract_for("Reels") == contract_for("shorts")


def test_podcast_contract() -> None:
    podcast = contract_for("podcast")
    assert podcast.presets == ("podcast_video", "podcast_audio")
    assert podcast.target_lufs == -16


def test_unknown_platform_raises() -> None:
    with pytest.raises(BriefError):
        contract_for("nope")


def test_preview_canvas_width() -> None:
    assert canvas_for(contract_for("preview")).width == 960


def _talk_short() -> tuple[SourceShot, Intent]:
    shot = SourceShot(
        name="main",
        path="/tmp/talk.mp4",
        kind="video",
        duration=120,
        width=1920,
        height=1080,
        fps=30,
        has_video=True,
        has_audio=True,
        size_bytes=10,
        silence_ratio=0.2,
        scene_count=4,
        loudness_i=-18,
    )
    intent = Intent(
        platform="shorts",
        max_duration=30,
        cut_silence=True,
        captions=True,
        loudnorm=True,
        music=None,
        title=None,
        hook=None,
        cta=None,
        reframe="9:16",
        denoise=False,
        highpass=True,
        fade=False,
        watermark=None,
        images=(),
        name="board-short",
        goal="short",
    )
    return shot, intent


def test_decide_captions_use_the_saved_spoken_language(monkeypatch: pytest.MonkeyPatch) -> None:
    shot, intent = _talk_short()
    monkeypatch.setattr("vedit.agent.decide.settings.default_language", lambda: "auto")
    ops = _ops(decide(intent, Inventory(shots=(shot,), primary="main", notes=())))
    assert _op(ops, "captions")["language"] == "auto", "Meta detects the language; it is never fixed to English"
    monkeypatch.setattr("vedit.agent.decide.settings.default_language", lambda: "es")
    ops = _ops(decide(intent, Inventory(shots=(shot,), primary="main", notes=())))
    assert _op(ops, "captions")["language"] == "es"


def test_decide_shorts_from_long_talk() -> None:
    shot = SourceShot(
        name="main",
        path="/tmp/talk.mp4",
        kind="video",
        duration=120,
        width=1920,
        height=1080,
        fps=30,
        has_video=True,
        has_audio=True,
        size_bytes=10,
        silence_ratio=0.2,
        scene_count=4,
        loudness_i=-18,
    )
    intent = Intent(
        platform="shorts",
        max_duration=30,
        cut_silence=True,
        captions=True,
        loudnorm=True,
        music=None,
        title=None,
        hook=None,
        cta=None,
        reframe="9:16",
        denoise=False,
        highpass=True,
        fade=False,
        watermark=None,
        images=(),
        name="board-short",
        goal="short",
    )
    decision = decide(intent, Inventory(shots=(shot,), primary="main", notes=()))
    ops = _ops(decision)
    silence = _op(ops, "cut_silence")
    assert silence["min_silence"] == pytest.approx(0.28)
    reframe = _op(ops, "reframe")
    assert reframe["aspect"] == "9:16"
    captions = _op(ops, "captions")
    assert captions["burn"] is True
    assert captions["style"] == "boxed"
    assert _op(ops, "loudnorm")["op"] == "loudnorm"
    export = ops[-1]
    assert export["op"] == "export"
    assert "shorts_9x16" in _presets(export)
    assert _filled(decision.rationale)
    assert _filled(decision.risks)


def test_decide_images_become_slideshow_without_silence_cut() -> None:
    slides = (
        _image("slide1", "/tmp/slide1.png"),
        _image("slide2", "/tmp/slide2.png"),
    )
    intent = Intent(
        platform="youtube",
        max_duration=None,
        cut_silence=False,
        captions=False,
        loudnorm=False,
        music=None,
        title=None,
        hook=None,
        cta=None,
        reframe="16:9",
        denoise=False,
        highpass=False,
        fade=False,
        watermark=None,
        images=(),
        name="slides",
        goal="youtube",
    )
    decision = decide(intent, Inventory(shots=slides, primary="slide1", notes=()))
    names = [op["op"] for op in _ops(decision)]
    assert "slideshow" in names
    assert "export" in names
    assert "cut_silence" not in names


def _image(name: str, path: str) -> SourceShot:
    return SourceShot(
        name=name,
        path=path,
        kind="image",
        duration=0,
        width=1920,
        height=1080,
        fps=30,
        has_video=False,
        has_audio=False,
        size_bytes=10,
        silence_ratio=None,
        scene_count=None,
        loudness_i=None,
    )


def _ops(decision: object) -> list[dict]:
    brief = getattr(decision, "brief", None)
    raw = brief.ops if brief is not None else decision.ops
    return list(raw)


def _op(ops: list[dict], name: str) -> dict:
    found = [op for op in ops if op.get("op") == name]
    assert found, f"missing {name}"
    return found[0]


def _presets(op: dict) -> list[str]:
    raw = op.get("presets", op.get("preset"))
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    return [str(item) for item in raw]


def _filled(value: object) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) > 0
    return value is not None


def test_silence_line_follows_the_recording_loudness() -> None:
    assert silence_threshold_db(-18.0) == -34.0, "typical speech keeps the tuned -34 dBFS"
    assert silence_threshold_db(None) == -34.0 and silence_threshold_db(float("nan")) == -34.0
    assert silence_threshold_db(-32.0) == -48.0, "a quiet phone recording is not read as all silence"
    assert silence_threshold_db(-9.0) == -26.0, "a loud, noisy room still has pauses"
    assert silence_threshold_db(-70.0) == -50.0


def test_a_quiet_recording_cuts_silence_at_its_own_level() -> None:
    shot, intent = _talk_short()
    from dataclasses import replace

    quiet = replace(shot, loudness_i=-31.0)
    ops = _ops(decide(intent, Inventory(shots=(quiet,), primary="main", notes=())))
    assert _op(ops, "cut_silence")["threshold_db"] == -47.0
