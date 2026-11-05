"""Caption styles: layout from the canvas, valid ASS, and what a real burn puts on the frame."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from vedit.caption_styles import (
    LOOKS,
    MIN_SCALE,
    Stage,
    ass_color,
    ass_text,
    caption_font,
    font_face,
    paginate,
    render_ass,
    safe_area,
    write_styled_ass,
)
from vedit.captions import Cue, Word, burn_filter, parse_ts
from vedit.edit_spec import CAPTION_STYLES
from vedit.media import has_filter, resolve_ffmpeg, resolve_font

STYLES = [row[0] for row in CAPTION_STYLES]
TALL, WIDE, SQUARE = (1080, 1920), (1920, 1080), (1080, 1080)
HIGH = "&H0AD6FF&"
LINE = re.compile(r"^Dialogue: 0,(\d:\d{2}:\d{2}\.\d{2}),(\d:\d{2}:\d{2}\.\d{2}),Default,,0,0,0,,(.*)$")


def timed(text: str, start: float = 1.0, each: float = 0.5) -> Cue:
    words = tuple(
        Word(start + index * each, start + (index + 1) * each, word) for index, word in enumerate(text.split())
    )
    return Cue(words[0].start, words[-1].end, text, words)


def events(ass: str) -> list[tuple[float, float, str]]:
    rows = []
    for line in ass.splitlines():
        if line.startswith("Dialogue:"):
            match = LINE.match(line)
            assert match, line
            rows.append((parse_ts(match[1]), parse_ts(match[2]), match[3]))
    return rows


def spoken(text: str) -> str:
    """What a viewer reads: override blocks gone, line breaks as spaces."""
    return " ".join(re.sub(r"(?<!\\)\{[^}]*\}", "", text).replace(r"\N", " ").split())


def style_fields(ass: str) -> dict[str, str]:
    names = next(line for line in ass.splitlines() if line.startswith("Format: Name"))[8:].split(", ")
    values = next(line for line in ass.splitlines() if line.startswith("Style: "))[7:].split(",")
    assert len(names) == len(values) == 23
    return dict(zip(names, values))


def test_every_catalogued_style_has_a_look() -> None:
    assert set(STYLES) == set(LOOKS)


@pytest.mark.parametrize("style", STYLES)
@pytest.mark.parametrize("size", [TALL, WIDE, SQUARE])
def test_ass_is_valid_and_events_never_overlap(style: str, size: tuple[int, int]) -> None:
    cues = [
        timed("This is the finished oak bowl, oiled and ready for the table.", 0.4, 0.3),
        timed("It started as a rough blank.", 5.0, 0.4),
        Cue(8.0, 10.0, "A line with no word timing at all."),
        Cue(10.5, 12.0, r"Odd {text} with \N in it."),
    ]
    ass = render_ass(cues, width=size[0], height=size[1], settings={"style": style})
    assert f"PlayResX: {size[0]}\nPlayResY: {size[1]}\n" in ass and "WrapStyle: 2" in ass
    assert style_fields(ass)["Encoding"] == "-1", "automatic direction, or Arabic punctuation lands on the wrong side"
    rows = events(ass)
    assert rows
    for (start, end, text), following in zip(rows, rows[1:] + [(99.0, 99.0, "")]):
        assert start < end <= following[0], "two events at once would stack on screen"
        assert re.sub(r"\\[{}]", "", text).count("{") == re.sub(r"\\[{}]", "", text).count("}")


def test_timed_styles_show_every_word_of_a_long_cue() -> None:
    text = (
        "This is the finished oak bowl, oiled and ready for the table, and it took me about three whole days to make."
    )
    cue = timed(text, 0.0, 0.3)
    for style in STYLES:
        rows = events(render_ass([cue], width=TALL[0], height=TALL[1], settings={"style": style}))
        pages: list[str] = []
        for _start, _end, line in rows:
            page = spoken(line)
            if not pages or pages[-1] != page:
                pages.append(page)
        assert " ".join(pages) == text, style


def test_karaoke_moves_one_highlight_and_never_reflows() -> None:
    cue = timed("one two three four")
    rows = events(
        render_ass([cue], width=TALL[0], height=TALL[1], settings={"style": "karaoke", "highlight": "#FFD60A"})
    )
    assert [(start, end) for start, end, _ in rows] == [(1.0, 1.5), (1.5, 2.0), (2.0, 2.5), (2.5, 3.0)]
    assert {spoken(text) for _, _, text in rows} == {"one two three four"}, "only the color moves"
    assert [re.search(r"\{\\c" + HIGH + r"[^}]*\}([^{]*)\{", text)[1] for _, _, text in rows] == [
        "one",
        "two",
        "three",
        "four",
    ]


def test_highlight_holds_through_a_pause_inside_a_line() -> None:
    words = (Word(0.0, 0.3, "well"), Word(1.5, 1.8, "then"))
    rows = events(
        render_ass([Cue(0.0, 1.8, "well then", words)], width=TALL[0], height=TALL[1], settings={"style": "karaoke"})
    )
    assert [(start, end) for start, end, _ in rows] == [(0.0, 1.5), (1.5, 1.8)], "no blink between words"


def test_pop_shows_at_most_three_words_and_scales_the_spoken_one() -> None:
    cue = timed("one two three four five six seven")
    rows = events(render_ass([cue], width=TALL[0], height=TALL[1], settings={"style": "pop"}))
    assert len(rows) == 7
    assert all(len(spoken(text).split()) <= 3 for _, _, text in rows)
    assert all(r"\t(0,90,\fscx100\fscy100)" in text and HIGH in text for _, _, text in rows)
    assert spoken(rows[0][2]) == spoken(rows[2][2]) == "one two three"
    assert spoken(rows[3][2]) == "four five six"


def test_pop_ends_a_phrase_at_a_pause() -> None:
    rows = events(render_ass([timed("well, this works now")], width=TALL[0], height=TALL[1], settings={"style": "pop"}))
    assert spoken(rows[0][2]) == "well,"
    assert spoken(rows[1][2]) == "this works now"


def test_a_cue_without_word_times_is_highlighted_from_an_estimate() -> None:
    rows = events(
        render_ass([Cue(0.0, 2.0, "Hola a todos")], width=TALL[0], height=TALL[1], settings={"style": "karaoke"})
    )
    assert len(rows) == 3 and rows[0][0] == 0.0 and rows[-1][1] == 2.0


def test_size_comes_from_the_canvas_and_the_scale_setting() -> None:
    def size(width: int, height: int, scale: float = 1.0) -> int:
        return int(
            style_fields(
                render_ass([timed("hello")], width=width, height=height, settings={"style": "bold", "scale": scale})
            )["Fontsize"]
        )

    assert size(1080, 1920) == size(1920, 1080) == 2 * size(540, 960)
    assert size(1080, 1920, 2.0) == 2 * size(1080, 1920)


@pytest.mark.parametrize("position,alignment", [("bottom", "2"), ("middle", "5"), ("top", "8")])
def test_position_stays_inside_the_tall_safe_area(position: str, alignment: str) -> None:
    fields = style_fields(render_ass([timed("hello")], width=TALL[0], height=TALL[1], settings={"position": position}))
    side, top, bottom = safe_area(*TALL)
    assert fields["Alignment"] == alignment
    assert int(fields["MarginV"]) == {"bottom": bottom, "middle": 0, "top": top}[position]
    assert int(fields["MarginL"]) == int(fields["MarginR"]) == side
    assert bottom >= 0.2 * TALL[1] and top >= 0.12 * TALL[1] and side >= 0.1 * TALL[0], (
        "feeds cover the frame's edges with their own buttons"
    )


def test_colors_are_written_blue_green_red() -> None:
    assert ass_color("#FFD60A") == "&H000AD6FF"
    fields = style_fields(render_ass([timed("hello")], width=100, height=100, settings={"color": "#112233"}))
    assert fields["PrimaryColour"] == "&H00332211"


def test_text_cannot_act_as_an_override_or_a_line_break() -> None:
    assert ass_text(r"{\b1\c&H0000FF&}") == r"\{" + "\\\u2060b1\\\u2060c&H0000FF&" + r"\}"
    assert r"\N" not in ass_text(r"C:\New")
    assert ass_text("tab\there\x07") == "tabhere"


def test_emoji_would_burn_as_empty_boxes_so_they_leave() -> None:
    rows = events(render_ass([Cue(0, 1, "Ready 🎉 to go ✅")], width=TALL[0], height=TALL[1]))
    assert spoken(rows[0][2]) == "Ready to go"
    assert events(render_ass([Cue(0, 1, "🎉")], width=TALL[0], height=TALL[1])) == []


def stage(style: str, size: tuple[int, int] = TALL) -> Stage:
    side, _top, _bottom = safe_area(*size)
    look = LOOKS[style]
    pixels = round(min(size) * look.size)
    return Stage(
        look, pixels, size[0] - 2 * side - round(2 * pixels * max(look.outline, look.padding)), caption_font()[1]
    )


@pytest.mark.parametrize("style", STYLES)
def test_long_words_fit_the_line(style: str) -> None:
    fitted = stage(style)
    for page in paginate(Cue(0, 4, "Supercalifragilisticexpialidocious is a long word"), fitted):
        for line in page.lines:
            assert fitted.measure(" ".join(word.text for word in line), page.size) <= fitted.room + 1


@pytest.mark.parametrize("style", STYLES)
def test_a_word_longer_than_any_line_is_broken_and_nothing_is_lost(style: str) -> None:
    fitted = stage(style)
    text = "x" * 300
    pages = paginate(Cue(0, 6, text), fitted)
    assert "".join(word.text for page in pages for word in page.words) == text
    for page in pages:
        assert page.size >= int(fitted.size * MIN_SCALE) - 1
        for line in page.lines:
            assert fitted.measure("".join(word.text for word in line), page.size) <= fitted.room + 1
    assert pages[0].start == 0 and pages[-1].end == 6


@pytest.mark.parametrize("style", STYLES)
@pytest.mark.parametrize(
    "text",
    [
        "木のボウルは粗いオークの塊から始まります。旋盤の上で、長く安定した切削で形が現れます。",
        "这个木碗从一块粗糙的橡木开始,在车床上,形状在长而稳定的切削中显现出来。",
    ],
)
def test_text_without_spaces_wraps_inside_the_line(style: str, text: str) -> None:
    fitted = stage(style)
    pages = paginate(Cue(0, 6, text), fitted)
    assert "".join(word.text for page in pages for word in page.words) == text
    for page in pages:
        for line in page.lines:
            assert fitted.measure("".join(word.text for word in line), page.size) <= fitted.room + 1


@pytest.mark.parametrize("style", STYLES)
def test_right_to_left_text_survives(style: str) -> None:
    text = "يبدأ الوعاء الخشبي كقطعة خشنة من خشب البلوط"
    ass = render_ass([Cue(0, 3, text)], width=TALL[0], height=TALL[1], settings={"style": style})
    seen: list[str] = []
    for _start, _end, line in events(ass):
        page = spoken(line)
        if not seen or seen[-1] != page:
            seen.append(page)
    assert " ".join(seen) == text


def test_font_family_and_widths_come_from_the_file() -> None:
    try:
        path = resolve_font()
    except Exception:
        pytest.skip("no system font")
    face = font_face(str(path))
    assert face is not None and face.family
    wide, narrow = face.advance("W"), face.advance("i")
    assert wide is not None and narrow is not None and 0 < narrow < wide < 2
    assert face.advance("\U000e0001") is None


def test_a_broken_font_file_falls_back_to_estimates(tmp_path: Path) -> None:
    junk = tmp_path / "junk.ttf"
    junk.write_bytes(b"not a font at all")
    assert font_face(str(junk)) is None
    assert Stage(LOOKS["bold"], 80, 800).measure("hello") > 0


def test_a_chosen_font_travels_with_the_captions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        system = resolve_font()
    except Exception:
        pytest.skip("no system font")
    chosen = tmp_path / ("Chosen" + system.suffix)
    chosen.write_bytes(system.read_bytes())
    monkeypatch.setenv("VEDIT_FONT", str(chosen))
    ass = write_styled_ass([timed("hello there")], tmp_path / "captions.ass", width=540, height=960)
    assert (tmp_path / "fonts" / chosen.name).is_file()
    assert style_fields(ass.read_text(encoding="utf-8"))["Fontname"] == font_face(str(chosen)).family
    if has_filter("ass"):
        assert ":fontsdir=" in burn_filter(ass) and str(tmp_path / "fonts") in burn_filter(ass).replace("\\", "")


def test_the_system_font_needs_no_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VEDIT_FONT", raising=False)
    write_styled_ass([timed("hello there")], tmp_path / "captions.ass", width=540, height=960)
    assert not (tmp_path / "fonts").exists()


def gray_frames(ass: Path, width: int, height: int, seconds: float = 1.0) -> list[bytes]:
    """Burn onto black and return each frame's luma, so a test can look at the pixels."""
    ffmpeg = str(resolve_ffmpeg())
    out = subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=black:s={width}x{height}:r=30:d={seconds}",
            "-vf",
            burn_filter(ass) + ",format=gray",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "-",
        ],
        capture_output=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr.decode(errors="replace")[-400:]
    size = width * height
    return [out.stdout[index : index + size] for index in range(0, len(out.stdout), size)]


def ink_columns(frame: bytes, width: int, height: int) -> tuple[int, int] | None:
    columns = [column for column in range(width) if any(frame[column + row * width] > 40 for row in range(height))]
    return (columns[0], columns[-1]) if columns else None


@pytest.fixture
def burns() -> None:
    try:
        if not has_filter("ass"):
            pytest.skip("this ffmpeg has no ass filter")
    except Exception:
        pytest.skip("ffmpeg required")


@pytest.mark.render
@pytest.mark.parametrize("style", STYLES)
@pytest.mark.parametrize("size", [(270, 480), (480, 270)])
def test_a_real_burn_puts_text_inside_the_side_margins(
    style: str, size: tuple[int, int], tmp_path: Path, burns: None
) -> None:
    width, height = size
    cues = [timed("Supercalifragilisticexpialidocious pneumonoultramicroscopicsilicovolcanoconiosis", 0.0, 0.5)]
    ass = write_styled_ass(cues, tmp_path / "captions.ass", width=width, height=height, settings={"style": style})
    side = safe_area(width, height)[0]
    seen = 0
    for frame in gray_frames(ass, width, height):
        ink = ink_columns(frame, width, height)
        if ink:
            seen += 1
            assert ink[0] >= side * 0.6 and ink[1] <= width - side * 0.6, f"{style} left the frame edge margin: {ink}"
    assert seen > 20


@pytest.mark.render
@pytest.mark.parametrize("folder", ["John's Videos", "a,b;c", "[brackets]", "colon:name", "with space"])
def test_a_real_burn_takes_any_folder_name(
    folder: str, tmp_path: Path, burns: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    try:
        system = resolve_font()
    except Exception:
        pytest.skip("no system font")
    chosen = tmp_path / ("Chosen" + system.suffix)
    chosen.write_bytes(system.read_bytes())
    monkeypatch.setenv("VEDIT_FONT", str(chosen))
    work = tmp_path / folder
    work.mkdir()
    ass = write_styled_ass([timed("hello there", 0.0, 0.5)], work / "captions.ass", width=320, height=240)
    assert any(ink_columns(frame, 320, 240) for frame in gray_frames(ass, 320, 240))


@pytest.mark.render
def test_pop_visibly_settles_after_each_word_starts(tmp_path: Path, burns: None) -> None:
    ass = write_styled_ass(
        [timed("wonderful", 0.0, 1.0)], tmp_path / "captions.ass", width=540, height=960, settings={"style": "pop"}
    )
    frames = gray_frames(ass, 540, 960)
    first, settled = ink_columns(frames[0], 540, 960), ink_columns(frames[15], 540, 960)
    assert first and settled
    assert first[1] - first[0] > settled[1] - settled[0], "the word starts larger and scales back"


@pytest.mark.render
@pytest.mark.parametrize("text", ["日本語のキャプション", "مرحبا بالعالم، كيف حالك؟", "שלום עולם", "हिन्दी कैप्शन"])
def test_a_real_burn_of_other_scripts_does_not_fail(text: str, tmp_path: Path, burns: None) -> None:
    ass = write_styled_ass(
        [Cue(0.0, 1.0, text)], tmp_path / "captions.ass", width=540, height=960, settings={"style": "karaoke"}
    )
    assert any(ink_columns(frame, 540, 960) for frame in gray_frames(ass, 540, 960))
