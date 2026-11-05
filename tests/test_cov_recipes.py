"""Named recipes: each produces a brief whose ops exist, end in export, and compile without a render."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from vedit.compile import OPS, compile_brief
from vedit.errors import BriefError
from vedit.media import resolve_ffmpeg
from vedit.presets import PRESETS
from vedit.recipes import (
    RECIPES,
    apply_recipe,
    list_recipes,
    recipe_podcast,
    recipe_shorts,
    recipe_slideshow,
    recipe_talking_head,
    recipe_youtube,
)


def _have_ffmpeg() -> bool:
    try:
        resolve_ffmpeg()
        return True
    except Exception:
        return False


render = pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg required")


def _ffmpeg(*args: str) -> None:
    subprocess.run([resolve_ffmpeg(), "-hide_banner", "-v", "error", "-y", *args], check=True)


def _clip(path: Path, *, seconds: float = 1.0) -> Path:
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=s=320x180:r=30:d={seconds}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:sample_rate=48000:duration={seconds}",
        "-shortest",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        str(path),
    )
    return path


def _still(path: Path, color: str = "red") -> Path:
    _ffmpeg("-f", "lavfi", "-i", f"color=c={color}:s=320x180:d=0.1", "-frames:v", "1", str(path))
    return path


def _tone(path: Path, *, seconds: float = 1.0) -> Path:
    _ffmpeg("-f", "lavfi", "-i", f"sine=frequency=220:sample_rate=48000:duration={seconds}", str(path))
    return path


def test_every_listed_recipe_has_a_builder_and_a_description() -> None:
    listed = list_recipes()
    assert set(listed) == set(RECIPES)
    assert all(isinstance(text, str) and text.strip() for text in listed.values())


@pytest.mark.parametrize("name", sorted(RECIPES))
def test_every_recipe_uses_known_ops_and_presets_and_ends_with_export(name: str, tmp_path: Path) -> None:
    kwargs: dict[str, object] = {"images": ["a.png", "b.png"]} if name == "slideshow" else {"inp": "talk.mp4"}
    brief = apply_recipe(name, **kwargs)  # type: ignore[arg-type]
    assert brief.ops, "a recipe must do something"
    assert brief.ops[-1]["op"] == "export"
    unknown = [op["op"] for op in brief.ops if op["op"] not in OPS]
    assert unknown == [], f"recipe {name} uses ops the compiler does not have: {unknown}"
    for preset in brief.ops[-1]["presets"]:
        assert preset in PRESETS, f"recipe {name} exports to an unknown preset {preset}"
    for preset in brief.presets:
        assert preset in PRESETS
    for source in brief.sources.values():
        assert Path(source).is_absolute(), "recipe sources are resolved so the brief works from any directory"


def test_recipe_names_are_normalised_before_lookup() -> None:
    assert apply_recipe("Talking-Head", inp="a.mp4").ops == apply_recipe("talking_head", inp="a.mp4").ops
    assert apply_recipe("  shorts ", inp="a.mp4").name == "a_shorts"


def test_an_unknown_recipe_names_the_known_ones() -> None:
    with pytest.raises(BriefError, match=r"unknown recipe 'karaoke'") as exc:
        apply_recipe("karaoke", inp="a.mp4")
    for known in RECIPES:
        assert known in str(exc.value)


@pytest.mark.parametrize("name", ["podcast", "youtube", "shorts", "talking_head"])
def test_recipes_that_edit_a_tape_refuse_to_run_without_an_input(name: str) -> None:
    with pytest.raises(BriefError, match=f"recipe {name} requires --in"):
        apply_recipe(name)
    with pytest.raises(BriefError, match="requires --in"):
        apply_recipe(name, inp="", images=["a.png"])


def test_the_slideshow_recipe_refuses_to_run_without_images() -> None:
    with pytest.raises(BriefError, match="recipe slideshow requires --images"):
        apply_recipe("slideshow", inp="talk.mp4")
    with pytest.raises(BriefError, match="requires --images"):
        apply_recipe("slideshow", images=[])


def test_podcast_adds_a_ducked_bed_only_when_music_is_given() -> None:
    plain = recipe_podcast("talk.mp4")
    assert "music" not in plain.sources
    assert [op["op"] for op in plain.ops].count("music") == 0
    assert plain.presets == ["podcast_video"]
    assert plain.ops[-1]["presets"] == ["podcast_video", "podcast_audio"], "a podcast ships video and audio masters"

    with_bed = recipe_podcast("talk.mp4", music="~/bed.mp3", name="episode 1")
    assert with_bed.name == "episode 1"
    assert with_bed.sources["music"] == str(Path("~/bed.mp3").expanduser().resolve())
    music = next(op for op in with_bed.ops if op["op"] == "music")
    assert music["source"] == "music" and music["duck"] is True and music["volume_db"] < 0
    ops = [op["op"] for op in with_bed.ops]
    assert ops.index("music") < ops.index("loudnorm") < ops.index("export"), (
        "the bed is mixed before loudness is measured, so the master is normalised with the music in it"
    )


def test_youtube_puts_an_optional_title_first_and_names_the_brief_after_the_file() -> None:
    plain = recipe_youtube("/clips/My Talk.mp4")
    assert plain.name == "My Talk"
    assert plain.ops[0]["op"] == "cut_silence"
    assert "title" not in [op["op"] for op in plain.ops]

    titled = recipe_youtube("/clips/My Talk.mp4", title="Hello", name="custom")
    assert titled.name == "custom"
    assert titled.ops[0] == {"op": "title", "text": "Hello"}
    assert titled.ops[-1]["presets"] == ["youtube_1080p"]


def test_shorts_reframes_to_vertical_with_boxed_captions() -> None:
    brief = recipe_shorts("/clips/talk.mp4")
    assert brief.name == "talk_shorts"
    reframe = next(op for op in brief.ops if op["op"] == "reframe")
    assert reframe["aspect"] == "9:16" and reframe["mode"] == "fill"
    captions = next(op for op in brief.ops if op["op"] == "captions")
    assert captions["style"] == "boxed" and captions["burn"] is True
    ops = [op["op"] for op in brief.ops]
    assert ops.index("reframe") < ops.index("captions"), "captions are placed on the final frame, not cropped away"
    assert brief.ops[-1]["presets"] == ["shorts_9x16"]


def test_talking_head_adds_a_watermark_only_with_a_logo() -> None:
    plain = recipe_talking_head("talk.mp4")
    assert "logo" not in plain.sources and "watermark" not in [op["op"] for op in plain.ops]
    assert plain.ops[-1]["presets"] == ["youtube_1080p", "shorts_9x16"]

    marked = recipe_talking_head("talk.mp4", logo="brand.png")
    assert marked.sources["logo"].endswith("brand.png")
    watermark = next(op for op in marked.ops if op["op"] == "watermark")
    assert watermark["source"] == "logo" and 0 < watermark["opacity"] <= 1 and 0 < watermark["scale"] < 1


def test_slideshow_names_every_image_and_replaces_audio_only_with_music() -> None:
    plain = recipe_slideshow(["a.png", "b.jpg", "c.webp"], hold=2.5)
    assert set(plain.sources) == {"img0", "img1", "img2"}
    show = plain.ops[0]
    assert show["op"] == "slideshow" and show["images"] == ["img0", "img1", "img2"] and show["duration"] == 2.5
    assert [op["op"] for op in plain.ops] == ["slideshow", "export"]

    scored = recipe_slideshow(["a.png"], music="bed.wav", name="deck")
    assert scored.name == "deck"
    assert [op["op"] for op in scored.ops] == ["slideshow", "replace_audio", "loudnorm", "export"]
    assert scored.ops[1]["source"] == "music" and scored.sources["music"].endswith("bed.wav")


def test_apply_recipe_passes_only_the_options_each_recipe_takes() -> None:
    # A title given to podcast, or a logo given to youtube, is ignored rather than refused: the CLI
    # exposes one flag set for every recipe.
    podcast = apply_recipe("podcast", inp="talk.mp4", title="ignored", logo="ignored.png")
    assert "title" not in [op["op"] for op in podcast.ops] and "logo" not in podcast.sources
    youtube = apply_recipe("youtube", inp="talk.mp4", title="Kept", music="ignored.mp3")
    assert youtube.ops[0] == {"op": "title", "text": "Kept"} and "music" not in youtube.sources


@render
@pytest.mark.parametrize("name", ["podcast", "youtube", "shorts", "talking_head"])
def test_tape_recipes_compile_to_a_program_on_a_real_clip(name: str, tmp_path: Path) -> None:
    talk = _clip(tmp_path / "talk.mp4")
    extras: dict[str, object] = {}
    if name == "podcast":
        extras["music"] = str(_tone(tmp_path / "bed.wav"))
    if name == "talking_head":
        extras["logo"] = str(_still(tmp_path / "logo.png"))
    if name == "youtube":
        extras["title"] = "Hello"
    brief = apply_recipe(name, inp=str(talk), **extras)  # type: ignore[arg-type]
    compiler = compile_brief(brief, work_dir=tmp_path / "work")
    names = [step.name for step in compiler.pipeline.steps]
    assert names, "a recipe compiles to at least one step"
    assert compiler.finals, "a recipe delivers at least one file"
    for preset in brief.ops[-1]["presets"]:
        assert any(preset in str(final) or PRESETS[preset].container in str(final) for final in compiler.finals), (
            f"recipe {name} promises {preset} but no final carries it: {compiler.finals}"
        )


@render
def test_the_slideshow_recipe_compiles_on_real_stills_and_a_bed(tmp_path: Path) -> None:
    images = [str(_still(tmp_path / f"{color}.png", color)) for color in ("red", "green")]
    bed = _tone(tmp_path / "bed.wav")
    brief = apply_recipe("slideshow", images=images, music=str(bed))
    compiler = compile_brief(brief, work_dir=tmp_path / "work")
    assert compiler.finals and all(Path(final).suffix == ".mp4" for final in compiler.finals)
    assert any("slideshow" in step.name for step in compiler.pipeline.steps)
