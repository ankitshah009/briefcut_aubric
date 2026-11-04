"""Text and images on the finished picture, placed on the output clock.

Text is written as one ASS file. libass was chosen over ``drawtext`` by test: it
shapes Arabic right to left, draws CJK and Devanagari from a fallback font, and
takes text as data, so nothing a creator types can reach a filtergraph. The same
strings through ``drawtext`` came out reversed or as empty boxes. ``drawtext`` is
kept for machines without libass.

Lines are laid out here from the font's real glyph widths, as captions are,
because libass will not break a run of text with no spaces. Sizes come from the
canvas, so a style reads the same at any export size.

An image joins the filter chain through a ``movie`` source rather than a second
input, so any number of them share one encode with the rest of the look.
"""

from __future__ import annotations

import shutil
import struct
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from vedit.caption_styles import _EMOJI, Look, Stage, ass_color, ass_text, caption_font, font_face, safe_area
from vedit.captions import format_ts, join_words, split_words
from vedit.media import filter_value, has_filter, resolve_font

ACCENT = "#FFD60A"
BOX_ALPHA = "4D"  # 70% opaque
MIN_FIT = 0.6
DEFAULT_FADE = 0.2


@dataclass(frozen=True)
class TextStyle:
    size: float  # font size, as a share of the canvas's short side
    lines: int  # the most lines before the size shrinks to fit
    box: str = ""  # "dark" or "accent"
    padding: float = 0.0  # share of the font size
    left: bool = False


TEXT_STYLES = {
    "title": TextStyle(size=0.092, lines=4),
    "label": TextStyle(size=0.042, lines=3, box="dark", padding=0.42),
    "lower_third": TextStyle(size=0.046, lines=2, box="dark", padding=0.5, left=True),
    "callout": TextStyle(size=0.06, lines=3, box="accent", padding=0.42),
}


def _measure_stage(style: TextStyle, size: int, room: int, face: Any) -> Stage:
    return Stage(Look(size=style.size, bold=True), size, room, face)


def _greedy(words: list[str], stage: Stage, room: float) -> list[list[str]]:
    lines: list[list[str]] = []
    for word in words:
        if lines and stage.measure(join_words([*lines[-1], word])) <= room:
            lines[-1].append(word)
        else:
            lines.append([word])
    return lines


def _balanced(words: list[str], stage: Stage) -> list[list[str]]:
    """The narrowest wrap that keeps the same line count, so no line ends in one lonely word."""
    best = _greedy(words, stage, stage.room)
    low, high = 0.0, float(stage.room)
    for _ in range(12):
        middle = (low + high) / 2
        trial = _greedy(words, stage, middle)
        if len(trial) <= len(best) and max(stage.measure(join_words(line)) for line in trial) <= middle:
            best, high = trial, middle
        else:
            low = middle
    return best


def _pieces(word: str, stage: Stage) -> list[str]:
    """A word too wide for any line at the smallest size, cut where it fits."""
    limit = stage.room / MIN_FIT
    if stage.measure(word) <= limit:
        return [word]
    parts, current = [], ""
    for char in word:
        if current and stage.measure(current + char) > limit:
            parts.append(current)
            current = ""
        current += char
    return [*parts, current]


def layout_text(text: str, style: TextStyle, *, width: int, height: int, scale: float = 1.0, face: Any = None) -> tuple[list[str], int]:
    """Wrapped lines and a font size that fit the canvas's safe width. Pure."""
    side, _top, _bottom = safe_area(width, height)
    size = max(8, round(min(width, height) * style.size * scale))
    room = max(size, width - 2 * side - round(2 * size * style.padding))
    stage = _measure_stage(style, size, room, face)
    words = [piece for word in split_words(text, run=2) for piece in _pieces(word, stage)]
    fitted = max(8, round(size * MIN_FIT))
    chosen, lines = size, _greedy(words, stage, room)
    for trial in range(size, fitted - 1, -max(1, size // 32)):
        stage = replace(stage, size=trial)
        chosen, lines = trial, _balanced(words, stage)
        widest = max(stage.measure(join_words(line)) for line in lines)
        if len(lines) <= style.lines and widest <= room:
            break
    return [join_words(line) for line in lines], chosen


def _contrast(color: str) -> str:
    """Black or white, whichever reads on a background color."""
    r, g, b = (int(color[index:index + 2], 16) for index in (1, 3, 5))
    return "#101014" if 0.2126 * r + 0.7152 * g + 0.0722 * b > 140 else "#FFFFFF"


def _tag(color: str) -> str:
    return f"&H{color[5:7]}{color[3:5]}{color[1:3]}&".upper()


def _anchor(style: TextStyle, position: str, *, width: int, height: int, pad: int, reserve: tuple[int, int] = (0, 0)) -> tuple[int, int, int]:
    """ASS alignment and the point it refers to, inside the safe area and clear of a logo."""
    side, top, bottom = safe_area(width, height)
    top, bottom = max(top, reserve[0]), max(bottom, reserve[1])
    if style.left:
        # A lower third belongs low in the frame, whatever the default position says.
        return (7, side + pad, top + pad) if position == "top" else (1, side + pad, height - bottom - pad)
    if position == "top":
        return 8, width // 2, top + pad
    if position == "bottom":
        return 2, width // 2, height - bottom - pad
    return 5, width // 2, height // 2


def text_event(item: dict[str, Any], *, width: int, height: int, end_limit: float | None, face: Any, reserve: tuple[int, int] = (0, 0)) -> str | None:
    """One ``Dialogue`` line for an ``edit.texts`` item, or None when nothing is left to draw."""
    style = TEXT_STYLES.get(str(item.get("style", "label")), TEXT_STYLES["label"])
    lines, size = layout_text(str(item["text"]), style, width=width, height=height, scale=float(item.get("scale", 1.0)), face=face)
    body = "\\N".join(ass_text(line) for line in lines).strip()
    if not body.replace("\\N", "").strip():
        return None
    start = max(0.0, float(item.get("start", 0.0)))
    end = start + float(item.get("duration", 3.0))
    if end_limit is not None:
        end = min(end, end_limit)
    if end <= start:
        return None
    color = str(item.get("color", "#FFFFFF"))
    pad = round(size * style.padding)
    alignment, x, y = _anchor(style, str(item.get("position", "center")), width=width, height=height, pad=pad, reserve=reserve)
    fade = float(item.get("fade", DEFAULT_FADE))
    tags = [f"\\an{alignment}", f"\\pos({x},{y})", f"\\fs{size}"]
    if fade > 0:
        ramp = round(min(fade, (end - start) / 3) * 1000)
        tags.append(f"\\fad({ramp},{ramp})")
    if style.box == "accent":
        box = ACCENT if color.upper() == "#FFFFFF" else color
        tags += [f"\\1c{_tag(_contrast(box))}", f"\\4c{_tag(box)}", "\\4a&H00&", f"\\shad{pad}"]
    elif style.box == "dark":
        tags += [f"\\1c{_tag(color)}", "\\4c&H141010&", f"\\4a&H{BOX_ALPHA}&", f"\\shad{pad}"]
    else:
        # Soft-edged dark outline: readable on anything, with no hard stroke around the letters.
        outline = max(1, round(size * 0.05))
        tags += [f"\\1c{_tag(color)}", f"\\bord{outline}", f"\\blur{outline * 1.4:.1f}", "\\3c&H000000&", "\\3a&H60&", "\\shad0"]
    style_name = "Boxed" if style.box else "Plain"
    return (f"Dialogue: 5,{format_ts(start, srt=False)},{format_ts(end, srt=False)},{style_name},,0,0,0,,"
            f"{{{''.join(tags)}}}{body}")


def has_emoji(text: str) -> bool:
    return bool(_EMOJI.search(text))


def render_text_ass(items: list[dict[str, Any]], *, width: int, height: int, end_limit: float | None = None, reserve: tuple[int, int] = (0, 0)) -> str:
    """The complete ASS text for every text item on a canvas.

    ``reserve`` is how far from the top and bottom edges a logo reaches, so text
    in those bands sits clear of it.
    """
    family, face, _file = caption_font()
    # BorderStyle 4 draws one box behind a whole block, in the shadow's color.
    fields = "Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"
    common = f"{family},48,{ass_color('#FFFFFF')},&H000000FF,&H00000000,&H4D141010,-1,0,0,0,100,100,0,0"
    events = [event for event in (text_event(item, width=width, height=height, end_limit=end_limit, face=face, reserve=reserve) for item in items) if event]
    return "\n".join([
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}", f"PlayResY: {height}", "WrapStyle: 2",
        "ScaledBorderAndShadow: yes", "YCbCr Matrix: TV.709", "",
        "[V4+ Styles]", f"Format: Name, {fields}",
        f"Style: Plain,{common},1,0,0,5,0,0,0,-1", f"Style: Boxed,{common},4,0,0,5,0,0,0,-1", "",
        "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        *events, ""])


def write_text_ass(items: list[dict[str, Any]], path: Path, *, width: int, height: int, end_limit: float | None = None, reserve: tuple[int, int] = (0, 0)) -> Path:
    """Write the text file and, for a font chosen with ``VEDIT_FONT``, a folder holding that font."""
    path.write_text(render_text_ass(items, width=width, height=height, end_limit=end_limit, reserve=reserve), encoding="utf-8")
    _family, _face, chosen = caption_font()
    if chosen is not None:
        fonts = path.parent / "fonts"
        fonts.mkdir(exist_ok=True)
        shutil.copyfile(chosen, fonts / chosen.name)
    return path


def drawtext_filters(items: list[dict[str, Any]], *, work_dir: Path, width: int, height: int, end_limit: float | None = None) -> list[str]:
    """Plain text for a machine without libass.

    The text sits in a file and ``expansion=none`` turns off every special
    character, so no string can break the filter. This path has no font fallback
    and no right-to-left shaping.
    """
    font = filter_value(resolve_font())
    _family, face, _file = caption_font()
    filters: list[str] = []
    for index, item in enumerate(items, 1):
        style = TEXT_STYLES.get(str(item.get("style", "label")), TEXT_STYLES["label"])
        lines, size = layout_text(str(item["text"]), style, width=width, height=height, scale=float(item.get("scale", 1.0)), face=face)
        path = work_dir / f"text_{index:02d}.txt"
        path.write_text("\n".join(lines), encoding="utf-8")
        start = max(0.0, float(item.get("start", 0.0)))
        end = start + float(item.get("duration", 3.0))
        if end_limit is not None:
            end = min(end, end_limit)
        fade = min(float(item.get("fade", DEFAULT_FADE)), (end - start) / 3)
        alpha = f"min(1,min((t-{start:.3f})/{fade:.3f},({end:.3f}-t)/{fade:.3f}))" if fade > 0 else "1"
        pad = round(size * style.padding)
        _alignment, _x, y = _anchor(style, str(item.get("position", "center")), width=width, height=height, pad=pad)
        side, _top, _bottom = safe_area(width, height)
        where = f"x={side + pad}" if style.left else "x=(w-text_w)/2"
        vertical = {"top": f"y={y}", "bottom": f"y={y}-text_h"}.get(str(item.get("position", "center")), "y=(h-text_h)/2")
        if style.left and str(item.get("position")) != "top":
            vertical = f"y={y}-text_h"
        box = f":box=1:boxcolor=0x101014@0.7:boxborderw={pad}" if style.box else f":borderw={max(1, size // 20)}:bordercolor=black@0.6"
        filters.append(f"drawtext=fontfile={font}:textfile={filter_value(path)}:expansion=none:fontsize={size}"
                       f":fontcolor={str(item.get('color', '#FFFFFF')).replace('#', '0x')}:text_align=center:{where}:{vertical}"
                       f":alpha='max(0,{alpha})'{box}:enable='between(t,{start:.3f},{end:.3f})'")
    return filters


def logo_reserve(position: str, *, image_width: int, image_height: int, width: int, height: int, scale: float) -> tuple[int, int]:
    """How far a logo reaches in from the top and bottom edges, with a small gap. Pure."""
    if position == "center" or image_width <= 0:
        return 0, 0
    _side, top, bottom = safe_area(width, height)
    tall = round(width * scale * image_height / image_width)
    gap = round(min(width, height) * 0.02)
    return (top + tall + gap, 0) if position.startswith("top") else (0, bottom + tall + gap)


def corner(position: str, *, width: int, height: int) -> tuple[str, str]:
    """Overlay expressions for a catalogue position, inside the safe area."""
    side, top, bottom = safe_area(width, height)
    x = {"left": str(side), "right": f"W-overlay_w-{side}"}
    y = {"top": str(top), "bottom": f"H-overlay_h-{bottom}"}
    if position == "center":
        return "(W-overlay_w)/2", "(H-overlay_h)/2"
    vertical, horizontal = position.split("-")
    return x[horizontal], y[vertical]


def image_filter(path: str | Path, *, tag: str, x: str, y: str, width: int, opacity: float = 1.0,
                 start: float | None = None, end: float | None = None, fade: float = 0.0, fps: float = 30.0) -> str:
    """Fuse one image into a filter chain. The text joins the chain like any single filter."""
    chain = [f"movie={filter_value(path)}", "format=rgba"]
    if opacity < 1:
        chain.append(f"colorchannelmixer=aa={opacity:.3f}")
    # The overlay has to reach YUV through the same matrix the export declares.
    chain += [f"scale={width}:-2:flags=lanczos:out_color_matrix=bt709:out_range=limited", "format=yuva420p"]
    timed = start is not None and end is not None
    ending = "eof_action=repeat"
    if timed and fade > 0:
        # Fading needs a stream of frames, not the single frame a still image gives.
        chain += ["loop=loop=-1:size=1:start=0", f"fps={fps}", f"fade=t=in:st={start:.3f}:d={fade:.3f}:alpha=1",
                  f"fade=t=out:st={end - fade:.3f}:d={fade:.3f}:alpha=1"]
        # That stream never ends, and overlay would hold the picture for as long as it runs.
        ending = "shortest=1"
    enable = f":enable='between(t,{start:.3f},{end:.3f})'" if timed else ""
    return (f"null[{tag}m];{','.join(chain)}[{tag}o];"
            f"[{tag}m][{tag}o]overlay=x={x}:y={y}:format=yuv420:{ending}{enable}")


def text_warnings(items: list[dict[str, Any]]) -> list[str]:
    if any(has_emoji(str(item.get("text", ""))) for item in items):
        return ["Emoji in on-screen text cannot be drawn by the local renderer, so they were left out of the text."]
    return []


def text_engine() -> str:
    """Which text renderer this machine has: ``ass``, ``drawtext``, or an empty string."""
    return "ass" if has_filter("ass") else "drawtext" if has_filter("drawtext") else ""


# --- The hero word: one huge word behind the subject ---
#
# It is drawn on the background layer, and the cut-out subject is laid back on
# top of it, so the subject hides the part of the word it stands in front of.
# The layout here is pure: the compiler supplies the frame, the part of it that
# survives the reframe, and the font.

HERO_FONT_FILES = {
    "heavy": ("/System/Library/Fonts/Supplemental/Arial Black.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    "condensed": ("/System/Library/Fonts/Supplemental/Impact.ttf", "/System/Library/Fonts/Supplemental/DIN Condensed Bold.ttf",
                  "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf", "/usr/share/fonts/truetype/liberation/LiberationSansNarrow-Bold.ttf"),
    "rounded": ("/System/Library/Fonts/Supplemental/Arial Rounded Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
}
# Where the word's centre sits and how wide it may grow, as shares of the visible picture.
HERO_PLACES = {
    "center": (0.5, 0.5, 0.9),
    "top": (0.5, 0.3, 0.9),
    "left": (0.3, 0.46, 0.54),
    "right": (0.7, 0.46, 0.54),
}
HERO_MAX_HEIGHT = 0.42  # the word's em, as a share of the visible height, so a short word is not a wall
HERO_POP = 0.18  # seconds the word takes to grow to full size
HERO_FADE = 0.12


def hero_font(font: str) -> tuple[str, Any, Path | None]:
    """The family, metrics and file for a hero font, or the caption font when this machine lacks it."""
    for candidate in HERO_FONT_FILES.get(font, ()):
        path = Path(candidate)
        if path.is_file():
            face = font_face(str(path))
            if face is not None and face.family:
                return face.family, face, path
    family, face, _chosen = caption_font()
    try:
        fallback = resolve_font()
    except Exception:
        fallback = None
    return family, face, fallback


def _ass_em(face: Any) -> float:
    """How big an em draws at ASS size 1. libass sizes a font by its OS/2 Windows height, not its em."""
    try:
        table = face.tables[b"OS/2"]
        ascent, descent = struct.unpack_from(">HH", face.data, table + 74)
        return face.units / (ascent + descent) if ascent + descent else 0.8
    except (AttributeError, KeyError, struct.error):
        return 0.8


def _is_bold(face: Any) -> bool:
    """True when the face is already heavy, so asking libass for bold picks it rather than faking a bolder one."""
    try:
        return struct.unpack_from(">H", face.data, face.tables[b"OS/2"] + 4)[0] >= 600
    except (AttributeError, KeyError, struct.error):
        return True


def visible_rect(width: int, height: int, aspect: str | None, focus: tuple[float, float] | None = None) -> tuple[int, int, int, int]:
    """The part of a ``width`` x ``height`` frame a fill reframe to ``aspect`` keeps, centred on ``focus``. Pure."""
    shapes = {"9:16": 9 / 16, "16:9": 16 / 9, "1:1": 1.0, "4:5": 0.8, "21:9": 21 / 9}
    ratio = shapes.get(aspect or "")
    if ratio is None:
        return 0, 0, width, height
    keep_w, keep_h = min(width, round(height * ratio)), min(height, round(width / ratio))
    cx, cy = focus if focus else (0.5, 0.5)
    left = min(max(0, round(cx * width - keep_w / 2)), width - keep_w)
    top = min(max(0, round(cy * height - keep_h / 2)), height - keep_h)
    return left, top, keep_w, keep_h


def hero_layout(word: str, *, side: str, width: int, height: int, visible: tuple[int, int, int, int] | None = None,
                face: Any = None) -> dict[str, Any]:
    """Where the hero word goes and how big: as wide as its side allows, inside the visible picture. Pure."""
    left, top, keep_w, keep_h = visible or (0, 0, width, height)
    across, down, share = HERO_PLACES.get(side, HERO_PLACES["center"])
    text = word.upper()
    stage = Stage(Look(size=0.1, bold=False), 100, keep_w, face)
    ems = max(0.3, stage.measure(text, size=1))
    em = _ass_em(face) if face is not None else 0.8
    room = keep_w * share
    size = min(room / (ems * em), keep_h * HERO_MAX_HEIGHT / em)
    size = max(8, int(size))
    drawn = ems * em * size
    x = left + keep_w * across
    # Keep the whole word on the visible picture even when its side is narrow.
    x = min(max(x, left + drawn / 2 + keep_w * 0.02), left + keep_w - drawn / 2 - keep_w * 0.02) if drawn < keep_w * 0.96 else left + keep_w / 2
    return {"text": text, "size": size, "x": round(x), "y": round(top + keep_h * down), "width": round(drawn),
            "height": round(em * size), "visible": [left, top, keep_w, keep_h]}


def render_hero_ass(word: str, *, start: float, end: float, side: str, color: str, family: str, face: Any,
                    width: int, height: int, visible: tuple[int, int, int, int] | None = None) -> tuple[str, dict[str, Any]]:
    """The ASS file for one hero word, and the layout it used. Times are already on the output clock."""
    layout = hero_layout(word, side=side, width=width, height=height, visible=visible, face=face)
    size = layout["size"]
    shadow = max(2, round(size * 0.035))
    fade = round(min(HERO_FADE, (end - start) / 4) * 1000)
    pop = round(min(HERO_POP, (end - start) / 4) * 1000)
    bold = 1 if face is None or _is_bold(face) else 0
    tags = (f"\\an5\\pos({layout['x']},{layout['y']})\\fs{size}\\b{bold}\\bord0\\shad{shadow}\\1c{_tag(color)}"
            f"\\4c&H000000&\\4a&HA0&\\fad({fade},{fade})\\fscx86\\fscy86\\t(0,{pop},\\fscx100\\fscy100)")
    fields = "Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"
    text = "\n".join([
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}", f"PlayResY: {height}", "WrapStyle: 2",
        "ScaledBorderAndShadow: yes", "YCbCr Matrix: TV.709", "",
        "[V4+ Styles]", f"Format: Name, {fields}",
        f"Style: Hero,{family},{size},{ass_color(color)},&H000000FF,&H00000000,&H60000000,{-bold},0,0,0,100,100,0,0,1,0,0,5,0,0,0,1", "",
        "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        f"Dialogue: 0,{format_ts(start, srt=False)},{format_ts(end, srt=False)},Hero,,0,0,0,,{{{tags}}}{ass_text(layout['text'])}", ""])
    return text, layout
