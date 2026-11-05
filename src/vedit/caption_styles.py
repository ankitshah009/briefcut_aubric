"""Caption styles: finished cues become a burn-ready ASS file.

Every id in ``edit_spec.CAPTION_STYLES`` is a row in ``LOOKS``. Sizes come from the
canvas, so a style reads the same at any export size. Lines are laid out here,
from the font's real glyph widths, instead of by libass: a line never leaves the
safe area, a page never holds more than the style allows, and a highlight that
moves from word to word cannot reflow the text.

A punch word (``edit.captions.emphasis``) is drawn in the accent color, a little
larger, and bold in a light style. It is measured at that size, so it never
pushes a line out of the safe area, and it keeps its size while the spoken-word
highlight passes over it, so the page never reflows.
"""

from __future__ import annotations

import mmap
import os
import re
import shutil
import struct
import unicodedata
from bisect import bisect_right
from collections.abc import Iterable
from dataclasses import dataclass, fields, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

from vedit.captions import (
    PHRASE_ENDS,
    SENTENCE_ENDS,
    Cue,
    Word,
    cue_key,
    edge_stripped,
    format_ts,
    interpolate_words,
    is_tight,
    join_words,
    split_words,
)
from vedit.edit_spec import COLOR, EMPHASIS_COLOR
from vedit.errors import BinaryMissing
from vedit.media import resolve_font

MIN_SCALE = 0.62  # a page shrinks this far for one long word before the word is broken
BOLD_WIDTH = 1.07  # bold faces run wider than the regular file the widths are read from
POP_SCALE = 112
POP_MS = 90
BOX_ALPHA = "48"
FALLBACK_FAMILY = "Arial"
# libass cannot draw color glyphs, so emoji would burn as empty boxes. A whole emoji goes: its skin tone,
# variation selector, keycap and flag tags, and the joiners of a family. A joiner between letters
# (Devanagari half forms) is not an emoji and stays; a keycap's digit stays as the digit.
_EMOJI_BASE = "\U0001f000-\U0001faff\u2600-\u27bf\u231a\u231b\u23e9-\u23f3\u23f8-\u23fa\u25fd\u25fe\u2b05-\u2b07\u2b1b\u2b1c\u2b50\u2b55\u3030\u303d\u3297\u3299"
_EMOJI_TAIL = "\ufe0e\ufe0f\u20e3\U000e0020-\U000e007f"
_EMOJI = re.compile(f"[{_EMOJI_BASE}][{_EMOJI_TAIL}]*(?:\u200d[{_EMOJI_BASE}][{_EMOJI_TAIL}]*)*|[{_EMOJI_TAIL}]")
_LONE_TAIL = re.compile(f"[{_EMOJI_TAIL}]+")
_PHRASE_END = PHRASE_ENDS
_SENTENCE_END = SENTENCE_ENDS
LRM, RLM = "\u200e", "\u200f"


@dataclass(frozen=True)
class Look:
    """One caption style. ``size``, ``outline``, ``shadow`` and ``padding`` scale with the canvas or the font."""

    size: float  # font size, as a share of the canvas's short side
    bold: bool = True
    outline: float = 0.0  # share of the font size
    shadow: float = 0.0  # share of the font size
    padding: float = 0.0  # box padding, share of the font size; 0 means no box
    blur: float = 0.0
    lines: int = 2
    words: int = 0  # most words on screen at once; 0 means as many as fit
    highlight: bool = False
    pop: bool = False
    punch: int = 115  # a punch word's size, in percent


LOOKS = {
    "boxed": Look(size=0.05, padding=0.3),
    "minimal": Look(
        size=0.042, bold=False, outline=0.05, shadow=0.05, blur=1.8
    ),  # the outline takes the blur, so letters stay sharp
    "bold": Look(size=0.072, outline=0.11, blur=0.5),
    "karaoke": Look(size=0.062, outline=0.09, blur=0.5, highlight=True),
    "pop": Look(
        size=0.09, outline=0.12, blur=0.5, words=3, highlight=True, pop=True, punch=108
    ),  # already large, three words a page
}


@dataclass(frozen=True)
class Punch(Word):
    """A word the edit emphasises. It is a ``Word`` in every other way."""


def word_key(text: str) -> str:
    """A word as compared for emphasis: no edge punctuation, no case."""
    return edge_stripped(text).casefold()


def _punched(word: Word) -> Punch:
    return Punch(**{field.name: getattr(word, field.name) for field in fields(Word)})


def emphasis_marks(settings: dict[str, Any]) -> dict[str, list[tuple[int, str]]]:
    """``edit.captions.emphasis`` by cue key, or nothing when emphasis is off. Malformed rows are skipped."""
    if settings.get("emphasize") is not True or not isinstance(settings.get("emphasis"), list):
        return {}
    marks: dict[str, list[tuple[int, str]]] = {}
    for item in settings["emphasis"]:
        if not isinstance(item, dict):
            continue
        key, index, word = item.get("cue_key"), item.get("word_index"), item.get("word")
        if isinstance(key, str) and isinstance(index, int) and not isinstance(index, bool) and isinstance(word, str):
            marks.setdefault(key, []).append((index, word))
    return marks


def emphasised(cue: Cue, wanted: Iterable[tuple[int, str]]) -> Cue:
    """The cue with each wanted word marked as a ``Punch``.

    ``wanted`` holds ``(word_index, word)`` pairs, indexed into ``split_words(cue.text)``.
    A pair counts only while the line still has that word at that place, so an
    edited or translated line keeps no stale emphasis. Heard words are matched by
    occurrence when they were split differently from the text.
    """
    tokens = split_words(cue.text)
    timed = cue if cue.words else interpolate_words(cue)
    words = list(timed.words)
    keys = [word_key(word.text) for word in words]
    marked = False
    for index, word in wanted:
        target = word_key(word)
        if not target or not 0 <= index < len(tokens) or word_key(tokens[index]) != target:
            continue
        if len(words) == len(tokens) and keys[index] == target:
            position = index
        else:
            nth = sum(1 for token in tokens[:index] if word_key(token) == target)
            hits = [at for at, key in enumerate(keys) if key == target]
            if nth >= len(hits):
                continue
            position = hits[nth]
        words[position] = _punched(words[position])
        marked = True
    return replace(timed, words=tuple(words)) if marked else cue


def strip_emoji(text: str) -> str:
    """Text with every emoji gone and the spaces they leave closed up."""
    if not _EMOJI.search(text):
        return text
    return " ".join(_EMOJI.sub("", text).split())


def emoji_in(texts: Iterable[str]) -> list[str]:
    """The emoji in some texts, in order, each once."""
    return list(
        dict.fromkeys(
            match.group(0)
            for text in texts
            for match in _EMOJI.finditer(text)
            if not _LONE_TAIL.fullmatch(match.group(0))
        )
    )


def caption_warnings(cues: Iterable[Cue]) -> list[str]:
    """What the burned captions leave out, said plainly. Today that is emoji."""
    cues = list(cues)
    found = emoji_in(cue.text for cue in cues)
    if not found:
        return []
    lines = sum(1 for cue in cues if _EMOJI.search(cue.text))
    sample = " ".join(found[:6]) + (" ..." if len(found) > 6 else "")
    return [
        (
            f"{lines} caption line{'s' if lines != 1 else ''} had emoji ({sample}). The caption renderer cannot draw color emoji, "
            "so they were left out of the burned captions. The words around them are unchanged, and captions.srt keeps the emoji."
        )
    ]


def _drawable(cue: Cue) -> Cue:
    """The cue without emoji: a word that was only emoji goes, and its time stays with the word before it."""
    if not _EMOJI.search(cue.text) and not any(_EMOJI.search(word.text) for word in cue.words):
        return cue
    words: list[Word] = []
    for word in cue.words:
        text = strip_emoji(word.text)
        if text:
            words.append(replace(word, text=text))
        elif words:
            words[-1] = replace(words[-1], end=max(words[-1].end, word.end))
    text = join_words(word.text for word in words) if cue.words else strip_emoji(cue.text)
    return replace(cue, text=text, words=tuple(words))


def direction_mark(text: str) -> str:
    """The mark that holds a line to its cue's reading direction, or nothing when the cue has one direction only.

    libass picks each line's direction from its first strong letter. A line of an
    English cue that starts with an Arabic word would then run right to left, so
    every line of a mixed cue starts with LRM or RLM. The cue's direction is the
    one most of its letters read in.
    """
    rtl = ltr = 0
    for char in text:
        kind = unicodedata.bidirectional(char)
        if kind in ("R", "AL"):
            rtl += 1
        elif kind == "L":
            ltr += 1
    if not rtl or not ltr:
        return ""
    return RLM if rtl > ltr else LRM


def is_rtl(text: str) -> bool:
    """True when most of a text's letters read right to left."""
    return (
        sum(
            1 if unicodedata.bidirectional(char) in ("R", "AL") else -1 if unicodedata.bidirectional(char) == "L" else 0
            for char in text
        )
        > 0
    )


class Face:
    """The parts of an sfnt font the layout needs: the family name and glyph widths."""

    def __init__(self, path: Path) -> None:
        with path.open("rb") as handle:
            self.data = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)  # a CJK font can be 100 MB
        base = struct.unpack_from(">I", self.data, 12)[0] if self.data[:4] == b"ttcf" else 0
        self.tables: dict[bytes, int] = {}
        for index in range(struct.unpack_from(">H", self.data, base + 4)[0]):
            tag, _sum, offset, _size = struct.unpack_from(">4sIII", self.data, base + 12 + 16 * index)
            self.tables[tag] = offset
        self.units = struct.unpack_from(">H", self.data, self.tables[b"head"] + 18)[0] or 1000
        self.metrics = struct.unpack_from(">H", self.data, self.tables[b"hhea"] + 34)[0]
        self.family = self._family()
        self._starts, self._ranges = self._character_map()

    def _family(self) -> str:
        table = self.tables[b"name"]
        _format, count, strings = struct.unpack_from(">HHH", self.data, table)
        found, rank = "", 9
        for index in range(count):
            platform, _encoding, language, name, length, offset = struct.unpack_from(
                ">6H", self.data, table + 6 + 12 * index
            )
            if name != 1 or platform not in (0, 1, 3):
                continue
            order = {3: 0 if language == 0x409 else 1, 0: 2, 1: 3}[platform]
            if order < rank:
                raw = self.data[table + strings + offset : table + strings + offset + length]
                found, rank = raw.decode("mac_roman" if platform == 1 else "utf-16-be", "ignore"), order
        return found.replace(",", " ").strip()

    def _character_map(self) -> tuple[list[int], list[tuple[int, int, int, int]]]:
        """Sorted ranges of ``(first, last, delta, glyph table offset)``. Handles the two formats fonts use for Unicode."""
        table = self.tables[b"cmap"]
        best, kind = 0, 0
        for index in range(struct.unpack_from(">H", self.data, table + 2)[0]):
            platform, encoding, offset = struct.unpack_from(">HHI", self.data, table + 4 + 8 * index)
            layout = struct.unpack_from(">H", self.data, table + offset)[0]
            unicode_map = platform == 0 or (platform == 3 and encoding in (1, 10))
            if unicode_map and layout in (4, 12) and layout > kind:
                best, kind = table + offset, layout
        ranges: list[tuple[int, int, int, int]] = []
        if kind == 12:
            for index in range(struct.unpack_from(">I", self.data, best + 12)[0]):
                first, last, glyph = struct.unpack_from(">III", self.data, best + 16 + 12 * index)
                ranges.append((first, last, glyph - first, 0))
        elif kind == 4:
            count = struct.unpack_from(">H", self.data, best + 6)[0] // 2
            ends, starts = best + 14, best + 16 + 2 * count
            deltas, offsets = starts + 2 * count, starts + 4 * count
            for index in range(count):
                last = struct.unpack_from(">H", self.data, ends + 2 * index)[0]
                first = struct.unpack_from(">H", self.data, starts + 2 * index)[0]
                delta = struct.unpack_from(">H", self.data, deltas + 2 * index)[0]
                spread = struct.unpack_from(">H", self.data, offsets + 2 * index)[0]
                ranges.append((first, last, delta, offsets + 2 * index + spread if spread else 0))
        ranges.sort()
        return [item[0] for item in ranges], ranges

    def _glyph(self, code: int) -> int:
        index = bisect_right(self._starts, code) - 1
        if index < 0:
            return 0
        first, last, delta, table = self._ranges[index]
        if code > last:
            return 0
        if not table:
            return (code + delta) & 0xFFFF if delta <= 0xFFFF and last <= 0xFFFF else code + delta
        glyph = struct.unpack_from(">H", self.data, table + 2 * (code - first))[0]
        return (glyph + delta) & 0xFFFF if glyph else 0

    def advance(self, char: str) -> float | None:
        """The width of a character in ems, or None when this font does not have it."""
        glyph = self._glyph(ord(char))
        if not glyph:
            return None
        return (
            struct.unpack_from(">H", self.data, self.tables[b"hmtx"] + 4 * min(glyph, self.metrics - 1))[0] / self.units
        )


@lru_cache(maxsize=64)
def font_face(path: str) -> Face | None:
    try:
        return Face(Path(path))
    except (OSError, ValueError, KeyError, IndexError, struct.error):
        return None


@lru_cache(maxsize=256)
def _fallbacks(missing: str) -> tuple[Face, ...]:
    faces: list[Face] = []
    left = set(missing)
    while left and len(faces) < 4:
        try:
            face = font_face(str(resolve_font("".join(sorted(left)))))
        except BinaryMissing:
            break
        if face is None:
            break
        covered = {char for char in left if face.advance(char) is not None}
        if not covered:
            break
        faces.append(face)
        left -= covered
    return tuple(faces)


def fallback_faces(text: str, face: Face | None) -> tuple[Face, ...]:
    """The fonts that draw the letters ``face`` lacks, as libass's own fallback would, so their real widths are measured.

    With no ``face`` there is nothing to fall back from, and widths stay estimates.
    """
    if face is None:
        return ()
    missing = {char for char in text if unicodedata.category(char)[0] in "LN" and face.advance(char) is None}
    return _fallbacks("".join(sorted(missing))) if missing else ()


def caption_font() -> tuple[str, Face | None, Path | None]:
    """The font family to name, its metrics, and its file when it must travel with the captions.

    ``VEDIT_FONT`` wins and is copied beside the file, because libass finds a font
    outside the system's list only through a folder. A glyph the font lacks is
    drawn from a system font by libass.
    """
    try:
        path = resolve_font()
    except BinaryMissing:
        return FALLBACK_FAMILY, None, None
    face = font_face(str(path))
    family = (face.family if face else "") or FALLBACK_FAMILY
    chosen = os.environ.get("VEDIT_FONT")
    return family, face, path if chosen and Path(chosen) == path else None


def safe_area(width: int, height: int) -> tuple[int, int, int]:
    """Side, top, and bottom margins in pixels. Tall frames leave room for the feed's own buttons and caption."""
    tall = height / max(1, width)
    if tall >= 1.5:
        return round(width * 0.11), round(height * 0.13), round(height * 0.22)
    if tall <= 0.75:
        return round(width * 0.06), round(height * 0.07), round(height * 0.08)
    return round(width * 0.08), round(height * 0.09), round(height * 0.1)


@dataclass(frozen=True)
class Stage:
    """A style at one size on one canvas."""

    look: Look
    size: int
    room: int  # widest line, in pixels
    face: Face | None = None
    fallbacks: tuple[Face, ...] = ()  # fonts for the letters ``face`` lacks, in the order libass would reach for them

    def measure(self, text: str, size: int | None = None) -> float:
        ems = 0.0
        for char in text:
            kind = unicodedata.category(char)
            if kind[0] in "MC":
                continue
            advance = self.face.advance(char) if self.face else None
            for other in self.fallbacks if advance is None else ():
                advance = other.advance(char)
                if advance is not None:
                    break
            if advance is None:
                wide = unicodedata.east_asian_width(char) in ("W", "F")
                advance = 1.0 if wide else 0.3 if char == " " else 0.62
            ems += advance
        return ems * (size or self.size) * (BOLD_WIDTH if self.look.bold else 1.0)

    def grow(self, word: Word) -> float:
        """How much wider a word draws than ``measure`` says: a punch word is larger, and bold in a light style."""
        if not isinstance(word, Punch):
            return 1.0
        return self.look.punch / 100 * (1.0 if self.look.bold else BOLD_WIDTH)

    def width(self, words: Iterable[Word]) -> float:
        """A line's width as drawn, punch words included."""
        words = tuple(words)
        return self.measure(_line_text(words)) + sum(
            self.measure(word.text) * (self.grow(word) - 1) for word in words if isinstance(word, Punch)
        )


@dataclass(frozen=True)
class Page:
    """What is on screen at once."""

    start: float
    end: float
    lines: tuple[tuple[Word, ...], ...]
    size: int

    @property
    def words(self) -> tuple[Word, ...]:
        return tuple(word for line in self.lines for word in line)


def _line_text(words: Iterable[Word]) -> str:
    return join_words(word.text for word in words)


def _broken_word(word: Word, stage: Stage) -> list[Word]:
    """A word too long for any line at the smallest size, cut into pieces that fit, sharing its time by length."""
    limit = stage.room / MIN_SCALE / stage.grow(word)
    parts: list[str] = []
    current = ""
    for char in word.text:
        if current and unicodedata.category(char)[0] != "M" and stage.measure(current + char) > limit:
            parts.append(current)
            current = ""
        current += char
    parts.append(current)
    span = word.end - word.start
    total = sum(len(part) for part in parts)
    pieces: list[Word] = []
    spent = 0
    for part in parts:
        begin = word.start + span * spent / total
        spent += len(part)
        pieces.append(
            replace(
                word, start=round(begin, 3), end=round(word.start + span * spent / total, 3), text=part, estimated=True
            )
        )
    return pieces


def _fitted_words(cue: Cue, stage: Stage) -> list[Word]:
    timed = cue if cue.words else interpolate_words(cue)
    words: list[Word] = []
    for word in timed.words:
        if not word.text.strip():
            continue  # nothing to draw, and pagination reads each word's last letter
        words.extend(_broken_word(word, stage) if stage.width((word,)) * MIN_SCALE > stage.room else [word])
    return words


def _wrapped(words: list[Word], stage: Stage) -> list[list[Word]]:
    """Fill lines left to right. A word wider than a line stands alone, and the page shrinks to fit it."""
    lines: list[list[Word]] = []
    for word in words:
        if lines and stage.width((*lines[-1], word)) <= stage.room:
            lines[-1].append(word)
        else:
            lines.append([word])
    return lines


def _balanced(lines: list[list[Word]], stage: Stage) -> list[list[Word]]:
    """Two lines of similar length, preferring a break after punctuation."""
    if len(lines) != 2:
        return lines
    words = lines[0] + lines[1]
    best, best_score = lines, float("inf")
    for index in range(1, len(words)):
        left, right = stage.width(words[:index]), stage.width(words[index:])
        if max(left, right) > stage.room:
            continue
        score = abs(left - right) - (stage.size * 1.5 if words[index - 1].text[-1] in _PHRASE_END else 0)
        if score < best_score:
            best, best_score = [words[:index], words[index:]], score
    return best


def _grouped(words: list[Word], stage: Stage) -> list[list[Word]]:
    """Share a cue's words among pages. A full page ends at the end of a sentence; a short phrase ends at any pause."""
    look = stage.look
    pages: list[list[Word]] = []
    current: list[Word] = []
    capacity = stage.room * look.lines
    for word in words:
        trial = [*current, word]
        if current and ((look.words and len(trial) > look.words) or len(_wrapped(trial, stage)) > look.lines):
            pages.append(current)
            current, trial = [], [word]
        current = trial
        phrase_page = bool(look.words) and word.text[-1] in _PHRASE_END
        full_sentence = word.text[-1] in _SENTENCE_END and stage.width(current) >= capacity * 0.5
        if phrase_page or full_sentence:
            pages.append(current)
            current = []
    if current:
        pages.append(current)
    if not look.words:
        pages = _without_orphans(pages, stage)
    if not look.words and len(pages) >= 2 and len(pages[-1]) == 1 and len(pages[-2]) >= 3:
        # A lone last word reads as a mistake, so it takes one from the page before.
        pages[-1].insert(0, pages[-2].pop())
    return pages


ORPHAN_WORDS = 3  # a page this short, cut off mid-sentence, is an orphan


def _fits(words: list[Word], stage: Stage) -> bool:
    return len(_wrapped(words, stage)) <= stage.look.lines


def _without_orphans(pages: list[list[Word]], stage: Stage) -> list[list[Word]]:
    """A page of three words or fewer, cut off mid-sentence, rejoins the page before it, or the two share the words evenly.

    A line too long for one page (a translation often runs longer than the speech)
    would otherwise leave its last words alone, like "la mesa." for half a second.
    The new break prefers punctuation. A page that follows a finished sentence is
    left alone: it starts a sentence of its own.
    """
    for index in range(1, len(pages)):
        head, tail = pages[index - 1], pages[index]
        if not head or not tail or len(tail) > ORPHAN_WORDS or head[-1].text[-1] in _SENTENCE_END:
            continue
        words = head + tail
        if _fits(words, stage):
            pages[index - 1], pages[index] = words, []
            continue
        best: tuple[float, list[Word], list[Word]] | None = None
        for cut in range(1, len(words)):
            left, right = words[:cut], words[cut:]
            if len(right) <= len(tail) or not _fits(left, stage) or not _fits(right, stage):
                continue
            score = abs(stage.width(left) - stage.width(right)) - (
                stage.size * 3 if left[-1].text[-1] in _PHRASE_END else 0
            )
            if best is None or score < best[0]:
                best = (score, left, right)
        if best is not None:
            pages[index - 1], pages[index] = best[1], best[2]
    return [page for page in pages if page]


def paginate(cue: Cue, stage: Stage) -> list[Page]:
    """Split a cue into pages that fit the style, each timed from its words."""
    groups = _grouped(_fitted_words(cue, stage), stage)
    pages: list[Page] = []
    for index, group in enumerate(groups):
        lines = _balanced(_wrapped(group, stage), stage)
        widest = max(stage.width(line) for line in lines)
        size = stage.size if widest <= stage.room else max(1, int(stage.size * stage.room / widest))
        start = cue.start if index == 0 else group[0].start
        end = groups[index + 1][0].start if index + 1 < len(groups) else cue.end
        pages.append(Page(start, max(start, end), tuple(tuple(line) for line in lines), size))
    return pages


def ass_color(value: str, alpha: str = "00") -> str:
    """``#RRGGBB`` as an ASS style color."""
    return f"&H{alpha}{value[5:7]}{value[3:5]}{value[1:3]}".upper()


def _tag_color(value: str) -> str:
    return f"&H{value[5:7]}{value[3:5]}{value[1:3]}&".upper()


def safe_color(value: Any, fallback: str = "#FFFFFF") -> str:
    """``value`` when it is ``#RRGGBB``, else ``fallback``.

    A colour is sliced into ASS tags and FFmpeg options, so anything else (a brace,
    a backslash, a colon or a comma) could open a tag or add a filter option.
    """
    return value.upper() if isinstance(value, str) and COLOR.fullmatch(value) else fallback


def ass_text(text: str) -> str:
    """Caption text as literal ASS: nothing in it can act as an override tag or a line break."""
    # Bidi embeddings, overrides and isolates (U+202A-U+202E, U+2066-U+2069) from model text could show a line reversed.
    plain = "".join(
        char
        for char in text
        if unicodedata.category(char) not in ("Cc", "Cs")
        and not ("\u202a" <= char <= "\u202e" or "\u2066" <= char <= "\u2069")
    )
    plain = _EMOJI.sub("", plain).replace("\\", "\\⁠")  # a joiner keeps "\N" from reading as a break
    return plain.replace("{", "\\{").replace("}", "\\}")


def _centiseconds(seconds: float) -> int:
    return round(seconds * 100)


def _dialogue(start: int, end: int, text: str) -> str:
    return f"Dialogue: 0,{format_ts(start / 100, srt=False)},{format_ts(end / 100, srt=False)},Default,,0,0,0,,{text}"


def _page_body(
    page: Page,
    *,
    active: int | None = None,
    base: str = "",
    high: str = "",
    pop: bool = False,
    accent: str = "",
    punch: int = 100,
    bold: bool = True,
    mark: str = "",
) -> str:
    """A page's text. The active word carries the highlight; scaling a word never moves another line.

    With an ``accent``, a punch word is drawn in it at ``punch`` percent, and bold when
    the style is not. When the spoken-word highlight reaches a punch word, the
    highlight color wins and the size stays, so reading position and emphasis both hold.
    """
    rendered: list[str] = []
    index = 0
    for line in page.lines:
        out = ""
        previous = ""
        for word in line:
            text = ass_text(word.text)
            marked = bool(accent) and isinstance(word, Punch)
            size = punch if marked else 100
            heavy, light = ("\\b1", "\\b0") if marked and not bold else ("", "")
            if index == active:
                if pop:
                    start = round(size * POP_SCALE / 100)
                    grow = f"\\fscx{start}\\fscy{start}\\t(0,{POP_MS},\\fscx{size}\\fscy{size})"
                else:
                    grow = f"\\fscx{size}\\fscy{size}" if marked else ""
                undo = "\\fscx100\\fscy100" if pop or marked else ""
                text = f"{{\\c{high}{grow}{heavy}}}{text}{{\\c{base}{undo}{light}}}"
            elif marked:
                text = f"{{\\c{accent}\\fscx{size}\\fscy{size}{heavy}}}{text}{{\\c{base}\\fscx100\\fscy100{light}}}"
            if out and word.text and not (is_tight(previous[-1:] or " ") and is_tight(word.text[0])):
                out += " "
            out += text
            previous = word.text
            index += 1
        rendered.append(mark + out if out else out)
    return "\\N".join(rendered)


def _events(pages: list[Page], stage: Stage, base: str, high: str, accent: str = "", mark: str = "") -> list[str]:
    look = stage.look
    ink = {"base": base, "accent": accent, "punch": look.punch, "bold": look.bold, "mark": mark}
    events: list[str] = []
    for page in pages:
        start, end = _centiseconds(page.start), _centiseconds(page.end)
        if end <= start:
            continue
        prefix = "".join(
            (f"\\fs{page.size}" if page.size != stage.size else "", f"\\blur{look.blur:g}" if look.blur else "")
        )
        prefix = f"{{{prefix}}}" if prefix else ""
        if not look.highlight:
            body = _page_body(page, **ink)
            if body.strip():
                events.append(_dialogue(start, end, prefix + body))
            continue
        # One event per word. The text is identical in each, so only the color moves.
        words = page.words
        marks = [start] + [max(start, min(end, _centiseconds(word.start))) for word in words[1:]] + [end]
        for index in range(len(words)):
            begin, finish = marks[index], max(marks[index], marks[index + 1])
            if index < len(words) - 1 and marks[index + 1] < begin:
                marks[index + 1] = begin
            body = _page_body(page, active=index, high=high, pop=look.pop, **ink)
            if finish > begin and body.strip():
                events.append(_dialogue(begin, finish, prefix + body))
    return events


def render_ass(cues: list[Cue], *, width: int, height: int, settings: dict[str, Any] | None = None) -> str:
    """The complete ASS text for finished cues on a canvas, styled by ``edit.captions``."""
    settings = settings or {}
    look = LOOKS.get(str(settings.get("style", "boxed")), LOOKS["boxed"])
    scale = float(settings.get("scale") or 1.0)
    position = settings.get("position", "bottom")
    color = safe_color(settings.get("color"), "#FFFFFF")
    highlight = safe_color(settings.get("highlight"), "#FFD60A")
    accent = safe_color(settings.get("emphasis_color"), EMPHASIS_COLOR)
    marks = emphasis_marks(settings)
    family, face, _file = caption_font()
    side, top, bottom = safe_area(width, height)
    size = max(8, round(min(width, height) * look.size * scale))
    outline = round(size * look.outline, 1)
    pad = round(size * look.padding)
    room = max(size, width - 2 * side - round(2 * max(outline, pad)))
    stage = Stage(look, size, room, face, fallback_faces("".join(cue.text for cue in cues), face))
    alignment, margin = {"top": (8, top), "middle": (5, 0)}.get(position, (2, bottom))
    if pad:
        border_style, shadow, outline_color, back_color = 4, pad, "&H00000000", f"&H{BOX_ALPHA}000000"
    else:
        border_style, shadow, outline_color, back_color = 1, round(size * look.shadow, 1), "&H00000000", "&H64000000"
    style = ",".join(
        str(item)
        for item in (
            "Default",
            family,
            size,
            ass_color(color),
            "&H000000FF",
            outline_color,
            back_color,
            -1 if look.bold else 0,
            0,
            0,
            0,
            100,
            100,
            0,
            0,
            border_style,
            outline,
            shadow,
            alignment,
            side,
            side,
            margin,
            -1,
        )
    )
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: {style}

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []
    for cue in cues:
        wanted = marks.get(cue_key(cue)) if marks else None
        # Emphasis goes on before the emoji come off, so a pick's place still counts them.
        drawn = _drawable(emphasised(cue, wanted) if wanted else cue)
        if not drawn.text:
            continue
        events.extend(
            _events(
                paginate(drawn, stage),
                stage,
                _tag_color(color),
                _tag_color(highlight),
                _tag_color(accent),
                direction_mark(drawn.text),
            )
        )
    return header + "\n".join(events) + "\n"


def write_styled_ass(
    cues: list[Cue], path: Path, *, width: int, height: int, settings: dict[str, Any] | None = None
) -> Path:
    """Write the styled captions and, for a font chosen with ``VEDIT_FONT``, a folder holding that font."""
    path.write_text(render_ass(cues, width=width, height=height, settings=settings), encoding="utf-8")
    _family, _face, chosen = caption_font()
    if chosen is not None:
        fonts = path.parent / "fonts"
        fonts.mkdir(exist_ok=True)
        shutil.copyfile(chosen, fonts / chosen.name)
    return path
