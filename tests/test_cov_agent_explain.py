"""The plain-language decisions, the caption gate, and the inventory that names and probes local media."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from vedit.agent.explain import drop_captions, explain_decisions, transcriber_ready
from vedit.agent.inventory import inventory_paths
from vedit.agent.model import Decision, Intent, Inventory, SourceShot
from vedit.errors import ProbeError
from vedit.ir import Brief, Canvas
from vedit.media import resolve_ffmpeg

KEY = "test-meta-key-0123456789"


def _have_ffmpeg() -> bool:
    try:
        resolve_ffmpeg()
        return True
    except Exception:
        return False


render = pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg required")


def _ffmpeg(*args: str) -> None:
    subprocess.run([resolve_ffmpeg(), "-hide_banner", "-v", "error", "-y", *args], check=True)


def _video(path: Path, *, seconds: float = 1.0, audio: bool = True) -> Path:
    command = ["-f", "lavfi", "-i", "testsrc2=s=320x180:r=30"]
    if audio:
        command += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
    command += ["-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if audio:
        command += ["-c:a", "aac"]
    _ffmpeg(*command, str(path))
    return path


def _audio(path: Path) -> Path:
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=220:sample_rate=48000", "-t", "1", str(path))
    return path


def _image(path: Path) -> Path:
    _ffmpeg("-f", "lavfi", "-i", "color=c=red:s=320x180:d=0.1", "-frames:v", "1", str(path))
    return path


def _shot(name: str = "main", path: str = "/clips/Board Meeting.mp4", kind: str = "video") -> SourceShot:
    return SourceShot(
        name=name,
        path=path,
        kind=kind,
        duration=12.0,
        width=1920,
        height=1080,
        fps=30.0,
        has_video=kind != "audio",
        has_audio=True,
        size_bytes=1,
    )


def _decision(*ops: dict) -> Decision:
    brief = Brief(name="x", sources={"main": "/clips/a.mp4"}, ops=list(ops), canvas=Canvas())
    return Decision(rationale=("because",), risks=(), brief=brief)


def test_a_short_is_explained_as_vertical_with_boxed_captions() -> None:
    intent = Intent(platform="shorts", captions=True)
    decision = _decision({"op": "cut_silence"}, {"op": "captions", "burn": True}, {"op": "export"})
    out = explain_decisions(intent, decision, Inventory(shots=(_shot(),), primary="main"), captions_skipped=False)
    assert out["pauses"] == "cut" and out["pauses_sentence"] == "Pauses will be cut."
    assert out["shape"] == "9:16" and out["shape_sentence"] == "Vertical 9:16, 1080×1920."
    assert out["captions"] == "burned" and out["caption_style"] == "boxed"
    assert out["captions_sentence"] == "Captions will be burned. Boxed, on the short."
    assert out["clip"] == "Board Meeting.mp4" and out["clip_sentence"] == "Clip: Board Meeting.mp4."
    assert out["program"] == decision.brief.ops and out["program"] is not decision.brief.ops
    assert out["sentences"] == [
        out["pauses_sentence"],
        out["shape_sentence"],
        out["captions_sentence"],
        out["clip_sentence"],
    ]


@pytest.mark.parametrize(
    ("platform", "sentence"),
    [
        ("youtube", "Landscape 16:9, 1920×1080, for YouTube."),
        ("linkedin", "Landscape 16:9, 1920×1080, for LinkedIn."),
        ("podcast", "Landscape 16:9, 1920×1080, for a podcast."),
        ("preview", "Landscape 16:9, 960×540, for a preview."),
        ("square", "Square 1:1, 1080×1080."),
    ],
)
def test_every_platform_has_a_shape_sentence(platform: str, sentence: str) -> None:
    out = explain_decisions(
        Intent(platform=platform), _decision({"op": "export"}), Inventory(shots=(), primary=""), captions_skipped=False
    )
    assert out["shape_sentence"] == sentence
    assert out["pauses_sentence"] == "Pauses will be kept."
    assert out["clip"] == "clip", "with no shot there is still a sentence"


def test_the_caption_sentence_says_when_captions_were_wanted_but_not_burned() -> None:
    inventory = Inventory(shots=(_shot(),), primary="main")
    wanted = Intent(platform="youtube", captions=True)
    skipped = explain_decisions(wanted, _decision({"op": "export"}), inventory, captions_skipped=True)
    assert skipped["captions"] == "skipped"
    assert skipped["captions_sentence"] == "Captions were not burned. Lower-third captions stay off this cut."
    no_op = explain_decisions(wanted, _decision({"op": "export"}), inventory, captions_skipped=False)
    assert no_op["captions"] == "skipped", "wanted but absent from the program reads as skipped"
    short = explain_decisions(
        Intent(platform="shorts", captions=True), _decision({"op": "export"}), inventory, captions_skipped=True
    )
    assert short["captions_sentence"] == "Captions were not burned. Boxed captions stay off this short."
    off = explain_decisions(
        Intent(platform="youtube", captions=False), _decision({"op": "export"}), inventory, captions_skipped=False
    )
    assert off["captions"] == "off" and off["captions_sentence"] == "Captions stay off."
    styled = explain_decisions(
        wanted, _decision({"op": "captions", "style": " karaoke "}, {"op": "export"}), inventory, captions_skipped=False
    )
    assert (
        styled["caption_style"] == "karaoke"
        and styled["captions_sentence"] == "Captions will be burned. Lower-third, on the picture."
    )


def test_the_clip_sentence_falls_back_to_the_first_shot_then_its_name() -> None:
    first = Inventory(shots=(_shot("broll1", "/x/b.mp4"), _shot("music", "/x/bed.mp3", "audio")), primary="gone")
    assert explain_decisions(Intent(), _decision({"op": "export"}), first, captions_skipped=False)["clip"] == "b.mp4"
    nameless = Inventory(shots=(_shot("main", ""),), primary="main")
    assert explain_decisions(Intent(), _decision({"op": "export"}), nameless, captions_skipped=False)["clip"] == "main"


def test_drop_captions_returns_a_new_brief_and_says_whether_anything_changed() -> None:
    brief = Brief(name="x", sources={}, ops=[{"op": "trim"}, {"op": "captions"}, {"op": "export"}], canvas=Canvas())
    stripped, dropped = drop_captions(brief)
    assert dropped is True and [op["op"] for op in stripped.ops] == ["trim", "export"]
    assert [op["op"] for op in brief.ops] == ["trim", "captions", "export"], "the original is untouched"
    same, dropped = drop_captions(stripped)
    assert dropped is False and same is stripped


def test_transcriber_is_ready_only_with_a_key_and_calls_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.delenv("VEDIT_META_ENABLED", raising=False)
    assert transcriber_ready() is False
    monkeypatch.setenv("MODEL_API_KEY", KEY)
    monkeypatch.setenv("VEDIT_META_ENABLED", "0")
    assert transcriber_ready() is False
    monkeypatch.setenv("VEDIT_META_ENABLED", "1")
    assert transcriber_ready() is True


def test_an_empty_inventory_is_refused() -> None:
    with pytest.raises(ProbeError, match="no media paths"):
        inventory_paths([])


def test_a_missing_file_is_named(tmp_path: Path) -> None:
    gone = tmp_path / "gone.mov"
    with pytest.raises(ProbeError, match=f"file does not exist: {gone}"):
        inventory_paths([str(gone)])


@render
def test_shots_are_named_by_kind_and_the_first_video_is_primary(tmp_path: Path) -> None:
    bed = _audio(tmp_path / "bed.wav")
    logo = _image(tmp_path / "logo.png")
    talk = _video(tmp_path / "talk.mp4")
    broll = _video(tmp_path / "broll.mp4", audio=False)
    bed2 = _audio(tmp_path / "bed2.wav")
    inventory = inventory_paths([str(bed), str(logo), str(talk), str(broll), str(bed2)])
    assert [shot.name for shot in inventory.shots] == ["music", "image1", "main", "broll1", "music2"]
    assert [shot.kind for shot in inventory.shots] == ["audio", "image", "video", "video", "audio"]
    assert inventory.primary == "main" and inventory.primary_shot is not None
    assert inventory.primary_shot.path == str(talk.resolve())
    assert inventory.shot("broll1").has_audio is False and inventory.shot("image1").duration >= 0
    assert inventory.notes == ()
    assert all(shot.silence_ratio is None and shot.loudness_i is None for shot in inventory.shots)


@render
def test_without_a_video_the_first_file_is_main_whatever_it_is(tmp_path: Path) -> None:
    bed = _audio(tmp_path / "bed.wav")
    logo = _image(tmp_path / "logo.png")
    inventory = inventory_paths([str(bed), str(logo)])
    assert [shot.name for shot in inventory.shots] == ["main", "image1"] and inventory.primary == "main"
    assert inventory_paths([str(logo)]).shots[0].kind == "image"


@render
def test_analysis_measures_what_each_kind_can_offer(tmp_path: Path) -> None:
    talk = _video(tmp_path / "talk.mp4")
    logo = _image(tmp_path / "logo.png")
    mute = _video(tmp_path / "mute.mp4", audio=False)
    inventory = inventory_paths([str(talk), str(logo), str(mute)], analyze=True)
    main, image, broll = inventory.shots
    assert main.loudness_i is not None and main.loudness_i < 0
    assert main.silence_ratio == 0.0 and main.scene_count == 0
    assert image.scene_count is None and image.loudness_i is None, "a still has no scenes or sound to measure"
    assert broll.loudness_i is None and broll.silence_ratio is None and broll.scene_count == 0
    assert inventory.notes == ()


@render
def test_a_failed_measurement_becomes_a_short_note_not_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vedit.agent.inventory as inventory_module
    from vedit.errors import AnalysisError

    talk = _video(tmp_path / "talk.mp4")

    def broken(*args, **kwargs):
        raise AnalysisError("loudnorm   did not print\n a JSON measurement block " + "x" * 100)

    monkeypatch.setattr(inventory_module, "measure_loudness", broken)
    inventory = inventory_paths([str(talk)], analyze=True)
    shot = inventory.shots[0]
    assert shot.loudness_i is None
    assert shot.silence_ratio == 0.0, "the silence line falls back to the default when loudness is unknown"
    (note,) = inventory.notes
    assert note.startswith("main: loudness analysis failed: loudnorm did not print a JSON measurement block")
    assert note.endswith("...") and len(note) == len("main: loudness analysis failed: ") + 80
