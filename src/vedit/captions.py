"""Captions: speech to timed lines, the one caption path, SRT/ASS I/O, burn-in.

Speech is heard by Meta Voice Transcribe (``transcribe``), the only speech
model Briefcut calls. It reports turns, not words, so word times are estimated.

There is one way a caption reaches a video. Speech is held on the source clock
(or, for a provider that hears only the kept audio, the concat clock), mapped
onto the output clock by ``timeline.map_cues``, polished, edited, then written
as a styled ASS file that the compiler burns. ``preview_cues`` runs the same
path, so Studio shows what will burn.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable

from vedit.analyze import Range, audible
from vedit.errors import CaptionError
from vedit.media import filter_value, has_filter, probe, resolve_font, run_ffmpeg
from vedit.timeline import Span, concat_spans, concat_to_source, map_cues, output_duration, plan_timeline, to_source

MAX_WORDS = 4000
MAX_WORD_CHARS = 64
CUE_CHARS = 42
HOLD_SECONDS = 0.25
MIN_CUE_SECONDS = 0.04
PHANTOM_WORDS = 2

# Scripts written without spaces between words.
_TIGHT_RANGES = (
    (0x0E00, 0x0EFF), (0x1000, 0x109F), (0x1780, 0x17FF), (0x3000, 0x30FF), (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF), (0xF900, 0xFAFF), (0xFF00, 0xFFEF), (0x20000, 0x2FFFF),
)
# Line splitting can cut a note in two, so an unclosed start or a closing tail counts as well.
_ANNOTATION = re.compile(r"\[[^\]]*(?:\]|$)|\([^)]*(?:\)|$)|^[a-z][a-z ]{0,30}[\])]|\*[^*]{1,40}\*|[♪♫♬]+")


@dataclass(frozen=True)
class Word:
    """One word. ``estimated`` means its time was spread from a longer cue, not heard."""

    start: float
    end: float
    text: str
    estimated: bool = False


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str
    words: tuple[Word, ...] = ()
    source_start: float | None = None  # where this cue begins on the source clock, once mapped
    segment: int = 0  # the story moment it plays in, once mapped; polish never joins two moments


def is_tight(char: str) -> bool:
    """True for a character of a script written without spaces."""
    code = ord(char)
    return any(low <= code <= high for low, high in _TIGHT_RANGES)


# Where sentences and phrases end, in every script the captions are likely to meet:
# Latin, CJK full-width, Devanagari danda, Arabic and Urdu, Ethiopic, Myanmar, Khmer, Armenian.
SENTENCE_ENDS = ".!?…。！？।॥؟۔።။។։‼⁇⁈⁉‽"
PHRASE_ENDS = SENTENCE_ENDS + ",;:、，；：،؛﹐﹑､"
CLOSERS = "\"'”’»›)]}」』）》〉】〕］｝"
_TERMINAL_END = re.compile(f"[{re.escape(SENTENCE_ENDS)}][{re.escape(CLOSERS)}]*$")

# Line-breaking rules for text with no spaces (Japanese kinsoku, Thai and Lao vowel order).
# These never begin a line: small kana, the long-vowel mark, iteration marks, and vowels written after their consonant.
_NO_START = frozenset("ぁぃぅぇぉっゃゅょゎゕゖァィゥェォッャュョヮヵヶㇰㇱㇲㇳㇴㇵㇶㇷㇸㇹㇺㇻㇼㇽㇾㇿーゝゞヽヾ々〻ゟ" "ะาำๅๆฯ" "ະາຳໆ")
# These never end a line: Thai and Lao vowels written before their consonant.
_NO_END = frozenset("เแโใไ" "ເແໂໃໄ")
_THAI_CLUSTER = (frozenset("กขคตปพผบด"), frozenset("รลว"))  # กร, ปล, คว... begin one syllable


def _phrase_spaced(char: str) -> bool:
    """True for a script with no spaces between words but a space between phrases: Thai, Lao, Khmer, Myanmar."""
    code = ord(char)
    return 0x0E00 <= code <= 0x0EFF or 0x1780 <= code <= 0x17FF or 0x1000 <= code <= 0x109F


def text_width(text: str) -> int:
    """How many columns a text fills: wide (CJK) characters count two, marks and format characters none."""
    width = 0
    for char in text:
        kind = unicodedata.category(char)
        if kind[0] == "M" or kind == "Cf":
            continue
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def edge_stripped(text: str) -> str:
    """A word without the punctuation and symbols around it. Marks stay: "हैं," keeps its vowel signs, unlike ``\\W`` stripping."""
    letters = [index for index, char in enumerate(text) if unicodedata.category(char)[0] in "LNM"]
    return text[letters[0]:letters[-1] + 1] if letters else ""


def _kind(char: str) -> str:
    code = ord(char)
    if 0x3040 <= code <= 0x309F:
        return "hira"
    if 0x30A0 <= code <= 0x30FF or 0x31F0 <= code <= 0x31FF or 0xFF66 <= code <= 0xFF9F:
        return "kata"
    if 0x3400 <= code <= 0x4DBF or 0x4E00 <= code <= 0x9FFF or 0xF900 <= code <= 0xFAFF or 0x20000 <= code <= 0x2FFFF or char in "々〆〇":
        return "han"
    if 0x0E00 <= code <= 0x0E7F:
        return "thai"
    return "punct" if unicodedata.category(char)[0] in "PZS" else "other"


def _joins_back(char: str) -> bool:
    """True for a character that belongs with the one before it."""
    kind = unicodedata.category(char)
    return kind[0] == "M" or kind in ("Pe", "Pf", "Po", "Cf") or char in _NO_START


def _joins_on(char: str) -> bool:
    """True for a character that belongs with the one after it."""
    return unicodedata.category(char) in ("Ps", "Pi", "Cf") or char in _NO_END


def _thai_break(text: str, index: int) -> bool:
    """A likely syllable start in Thai: before a leading vowel, after a closing vowel, or before a consonant that takes a vowel sign."""
    char, before = text[index], text[index - 1]
    if char in "เแโใไ" or before in "ะำๆฯ":
        return True
    following = text[index + 1] if index + 1 < len(text) else ""
    if not (0x0E01 <= ord(char) <= 0x0E2E and following and (unicodedata.category(following)[0] == "M" or following in "ะาำ")):
        return False
    return not (char in _THAI_CLUSTER[1] and before in _THAI_CLUSTER[0])


def _clusters(chunk: str) -> list[str]:
    """The smallest pieces a run with no spaces may be cut into, never splitting a mark, a syllable or a kinsoku pair."""
    pieces: list[str] = []
    for index, char in enumerate(chunk):
        before = chunk[index - 1] if index else ""
        cut = bool(pieces) and not _joins_back(char) and not _joins_on(before) and (is_tight(char) or is_tight(before))
        if cut and _kind(char) == "thai" and _kind(before) == "thai":
            cut = _thai_break(chunk, index)
        if cut:
            pieces.append(char)
        elif pieces:
            pieces[-1] += char
        else:
            pieces.append(char)
    return pieces


def _tight_words(chunk: str, run: int) -> list[str]:
    """A run with no spaces as word-sized pieces.

    Japanese starts a new piece where kana turns back into kanji or katakana,
    which is where a particle or verb ending stops. Other scripts take pieces of
    about ``run`` columns, a CJK character counting two. Pieces never break a
    kinsoku rule, a mark, or a Thai syllable.
    """
    japanese = any(_kind(char) in ("hira", "kata") for char in chunk)
    limit = run * 4 if japanese else run
    words: list[str] = []
    current = ""
    for piece in _clusters(chunk):
        if current:
            last = _kind(current[-1])
            first = next((_kind(char) for char in piece if _kind(char) != "punct"), "punct")
            opens = unicodedata.category(piece[0]) in ("Ps", "Pi")
            if japanese:
                turn = (last == "hira" and first in ("han", "kata", "other")) or {last, first} == {"han", "kata"} or last == "punct" or opens
                full = text_width(current + piece) > limit
            else:
                turn = (last == "punct" and first != "punct") or opens
                full = text_width(current) >= limit
            if turn or full:
                words.append(current)
                current = ""
        current += piece
    if current:
        words.append(current)
    return words


def split_words(text: str, *, run: int = 4) -> list[str]:
    """The words of a line. Text with no spaces is cut into word-sized pieces, keeping marks and closing punctuation attached."""
    words: list[str] = []
    for chunk in text.split():
        if any(is_tight(char) for char in chunk):
            words.extend(_tight_words(chunk, run))
        else:
            words.append(chunk)
    return words


def join_words(parts: Iterable[str]) -> str:
    """Words back into a line: a space between them, except inside scripts that use none."""
    line = ""
    for part in parts:
        part = part.strip()
        if part:
            line += part if not line or (is_tight(line[-1]) and is_tight(part[0])) else " " + part
    return line


def strip_annotations(text: str) -> str:
    """Remove what a recognizer writes about the audio rather than the speech: [BLANK_AUDIO], (upbeat music), ♪."""
    return " ".join(_ANNOTATION.sub(" ", text).split())


def clean_transcript(cues: list[Cue]) -> list[Cue]:
    """Keep only cues that hold speech."""
    kept: list[Cue] = []
    for cue in cues:
        text = strip_annotations(cue.text)
        if any(char.isalnum() for char in text):
            kept.append(replace(cue, text=text))
    return kept


def _as_word(value: Any) -> Word | None:
    if isinstance(value, Word):
        return value
    if not isinstance(value, dict):
        return None
    start, end = _seconds(value.get("start")), _seconds(value.get("end"))
    text = " ".join(str(value.get("text") or "").split())
    if start is None or end is None or end < start or not text:
        return None
    return Word(start, end, text, bool(value.get("estimated")))


def _as_cue(value: Any) -> Cue | None:
    if isinstance(value, Cue):
        return value
    if not isinstance(value, dict):
        return None
    start, end = _seconds(value.get("start")), _seconds(value.get("end"))
    text = " ".join(str(value.get("text") or "").split())
    if start is None or end is None or end <= start or not text:
        return None
    words = tuple(word for word in map(_as_word, value.get("words") or ()) if word is not None)
    return Cue(start, end, text, words, _seconds(value.get("source_start")))


def _seconds(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def word_record(word: Word) -> dict[str, Any]:
    record: dict[str, Any] = {"start": round(word.start, 3), "end": round(word.end, 3), "text": word.text}
    if word.estimated:
        record["estimated"] = True
    return record


def cue_record(cue: Cue) -> dict[str, Any]:
    record: dict[str, Any] = {"start": round(cue.start, 3), "end": round(cue.end, 3), "text": cue.text}
    if cue.words:
        record["words"] = [word_record(word) for word in cue.words]
    return record


def cue_words(cues: Iterable[Cue]) -> list[Word]:
    return [word for cue in cues for word in cue.words]


def word_records(words: Iterable[Any], duration: float = math.inf) -> list[dict[str, Any]]:
    """Words as bounded plain records, clamped to the source. This is ``analysis.words``."""
    records: list[dict[str, Any]] = []
    for value in words:
        word = _as_word(value)
        if word is None or word.start >= duration:
            continue
        text = "".join(char for char in word.text if unicodedata.category(char) not in ("Cc", "Cs"))[:MAX_WORD_CHARS]
        if text:
            start = max(0.0, word.start)
            # Some tokens report no length. A word must still have one, or a bound downstream drops it.
            records.append(word_record(replace(word, start=start, end=min(duration, max(word.end, start + 0.01)), text=text)))
            if len(records) >= MAX_WORDS:
                break
    return records


def interpolate_words(cue: Any) -> Any:
    """Spread a cue's words across its time by character length, marked as estimated.

    A provider that reports turns, not words, can still drive a word highlight
    this way. Takes and returns a ``Cue`` or a cue record.
    """
    timed = _as_cue(cue)
    if timed is None:
        return cue
    parts = split_words(timed.text)
    total = sum(len(part) for part in parts)
    words: list[Word] = []
    spent = 0
    for part in parts:
        begin = timed.start + (timed.end - timed.start) * spent / total
        spent += len(part)
        words.append(Word(round(begin, 3), round(timed.start + (timed.end - timed.start) * spent / total, 3), part, True))
    if isinstance(cue, Cue):
        return replace(timed, words=tuple(words))
    return {**cue, "words": [word_record(word) for word in words], "estimated": True}


def format_ts(seconds: float, *, srt: bool = True) -> str:
    """SRT uses HH:MM:SS,mmm. ASS uses H:MM:SS.cc (centiseconds): libass reads
    a three-digit fraction as centiseconds, so ".380" would mean 3.8 s."""
    total = max(0.0, float(seconds))
    if srt:
        ms = int(round(total * 1000))
        h, rem = divmod(ms, 3_600_000)
        m, rem = divmod(rem, 60_000)
        s, ms = divmod(rem, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    cs = int(round(total * 100))
    h, rem = divmod(cs, 360_000)
    m, rem = divmod(rem, 6_000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def parse_ts(stamp: str) -> float:
    stamp = stamp.strip().replace(",", ".")
    h, m, rest = stamp.split(":")
    return int(h) * 3600 + int(m) * 60 + float(rest)


def _word_key(word: str) -> str:
    return edge_stripped(word).casefold()


def _capitalize(text: str) -> str:
    for index, char in enumerate(text):
        if char.isalpha():
            return text[:index] + char.upper() + text[index + 1 :]
        if char.isdigit():
            return text
    return text


def _tokens(cue: Cue) -> list[Word | str]:
    return list(cue.words) if cue.words else cue.text.split()


def _token_text(token: Word | str) -> str:
    return token.text if isinstance(token, Word) else token


def _rebuilt(cue: Cue, tokens: list[Word | str]) -> Cue:
    if cue.words:
        words = tuple(token for token in tokens if isinstance(token, Word))
        return replace(cue, text=join_words(word.text for word in words), words=words)
    return replace(cue, text=" ".join(_token_text(token) for token in tokens))


def _absorb(kept: Word | str, doubled: Word | str, punctuation: bool) -> Word | str:
    """One word from a doubled pair; a timed word also takes the time of the copy that went."""
    text = _token_text(doubled) if punctuation else _token_text(kept)
    if isinstance(kept, Word):
        end = doubled.end if isinstance(doubled, Word) else kept.end
        return replace(kept, text=text, end=max(kept.end, end))
    return text


REPEAT_WORDS = 4  # the longest phrase that collapses when said twice in a row


def _cased_key(key: str) -> bool:
    """True for a word in a script with case, where a doubled word is a stutter rather than grammar."""
    return any(char.upper() != char.lower() for char in key)


def _collapse_phrases(cues: list[Cue], longest: int = REPEAT_WORDS) -> list[Cue]:
    """Drop the second copy of a phrase of 2 to ``longest`` words said twice in a row ("the bowl the bowl").

    It works across cue boundaries inside one story moment, as single words do.
    Only words in scripts with case count, and a copy that ends a phrase with
    punctuation ("the bowl. The bowl is oak.") is a deliberate repeat and stays.
    The word left at the end of the phrase takes the time and closing
    punctuation of the copy that went.
    """
    stream = [(index, token) for index, cue in enumerate(cues) for token in _tokens(cue)]
    keys = [_word_key(_token_text(token)) for _index, token in stream]
    texts = [_token_text(token) for _index, token in stream]
    tokens: dict[int, Word | str] = {}
    dropped: set[int] = set()
    kept: list[int] = []
    position = 0
    while position < len(stream):
        size = 0
        for n in range(min(longest, len(kept), len(stream) - position), 1, -1):
            first, second = kept[-n:], list(range(position, position + n))
            if (all(keys[a] and keys[a] == keys[b] and _cased_key(keys[a]) for a, b in zip(first, second))
                    and len({cues[stream[at][0]].segment for at in (*first, *second)}) == 1
                    and not any(texts[at][-1] in PHRASE_ENDS for at in (*first, *second[:-1]))):
                size = n
                break
        if not size:
            kept.append(position)
            position += 1
            continue
        last, gone = kept[-1], position + size - 1
        token = tokens.get(last, stream[last][1])
        merged = _absorb(token, stream[gone][1], False)  # the kept word, holding the time of the copy that went
        stripped = edge_stripped(texts[gone])
        tail = texts[gone][texts[gone].rfind(stripped) + len(stripped):] if stripped else ""
        if tail and _token_text(token) == edge_stripped(_token_text(token)):
            merged = replace(merged, text=merged.text + tail) if isinstance(merged, Word) else merged + tail
        tokens[last] = merged
        dropped.update(range(position, position + size))
        position += size
    if not dropped:
        return cues
    out: list[Cue] = []
    for index, cue in enumerate(cues):
        left = [tokens.get(at, token) for at, (owner, token) in enumerate(stream) if owner == index and at not in dropped]
        if left:
            out.append(_rebuilt(cue, left))
        elif out and out[-1].segment == cue.segment:
            out[-1] = replace(out[-1], end=max(out[-1].end, cue.end))
    return out


def _capitalized(cue: Cue) -> Cue:
    if not cue.words:
        return replace(cue, text=_capitalize(cue.text))
    words = (replace(cue.words[0], text=_capitalize(cue.words[0].text)), *cue.words[1:])
    return replace(cue, text=join_words(word.text for word in words), words=words)


def polish_cues(cues: list[Cue], *, max_chars: int = 84, max_gap: float = 0.35) -> list[Cue]:
    """Clean provider cues for burn-in without touching timing except by merging.

    - Immediately repeated words collapse case-insensitively ("the the" -> "the"),
      also across a cue boundary when the next cue opens with the previous cue's
      last word (a Meta Voice Transcribe habit at turn edges). Only words in a
      script with case collapse: Hebrew, Hindi, Korean or Chinese double words on purpose.
    - A cue that does not end a sentence merges with the next one when it starts
      within ``max_gap`` seconds and the joined text fits ``max_chars``; a very
      short trailing cue (<= 0.8 s or <= 2 words) merges into its predecessor
      the same way. Splitting turns at the line length produces both shapes.
    - Whitespace is normalized, a line that starts a sentence starts with a capital, empties are dropped.

    A cue that carries ``words`` keeps them aligned with its text through all of this.
    Cues marked with different story moments stay apart: on the source clock, speech
    either side of a cut comes from different sentences.
    """
    cleaned: list[Cue] = []
    for cue in cues:
        text = join_words(word.text for word in cue.words) if cue.words else " ".join(cue.text.split())
        if text:
            cleaned.append(replace(cue, start=float(cue.start), end=float(cue.end), text=text))

    # Collapse doubled words, looking back across the cue boundary.
    collapsed: list[Cue] = []
    for cue in cleaned:
        kept: list[Word | str] = []
        for token in _tokens(cue):
            word = _token_text(token)
            if kept:
                previous = kept[-1]
            elif collapsed and collapsed[-1].segment == cue.segment:
                previous = _tokens(collapsed[-1])[-1]
            else:
                previous = None
            key = _word_key(word)
            # Only in scripts with case: Hebrew "סוף סוף" (finally) or Hindi "धीरे धीरे" (slowly) double on purpose.
            if previous is not None and key and key[0].upper() != key[0].lower() and key == _word_key(_token_text(previous)):
                # Keep whichever copy carries the punctuation ("lathe lathe," -> "lathe,").
                punctuation = edge_stripped(word) != word and edge_stripped(_token_text(previous)) == _token_text(previous)
                if kept:
                    kept[-1] = _absorb(kept[-1], token, punctuation)
                else:
                    head = _tokens(collapsed[-1])
                    head[-1] = _absorb(head[-1], token, punctuation)
                    collapsed[-1] = _rebuilt(collapsed[-1], head)
                continue
            kept.append(token)
        if kept:
            collapsed.append(_rebuilt(cue, kept))
        elif collapsed and collapsed[-1].segment == cue.segment:
            # The whole cue repeated the previous word; its time still belongs to that word.
            collapsed[-1] = replace(collapsed[-1], end=max(collapsed[-1].end, cue.end))

    collapsed = _collapse_phrases(collapsed)

    # Merge fragments into whole sentences where the clock allows.
    merged: list[Cue] = []
    for cue in collapsed:
        if merged and merged[-1].segment == cue.segment:
            previous = merged[-1]
            gap = cue.start - previous.end
            # Thai, Lao, Khmer and Myanmar put a space between phrases, so two lines join with one.
            spaced = _phrase_spaced(previous.text[-1]) and _phrase_spaced(cue.text[0])
            joined = f"{previous.text} {cue.text}" if spaced and not (previous.words and cue.words) else join_words((previous.text, cue.text))
            fits = text_width(joined) <= max_chars and gap <= max_gap
            open_ended = not _TERMINAL_END.search(previous.text)
            short_trailer = (cue.end - cue.start) <= 0.8 or len(split_words(cue.text)) <= 2
            if fits and (open_ended or short_trailer):
                words = previous.words + cue.words if previous.words and cue.words else ()
                merged[-1] = replace(previous, end=max(previous.end, cue.end), text=joined, words=words)
                continue
        merged.append(cue)

    # A line takes a capital only where a sentence starts: the first line, the first line of a story moment, or
    # after a line that ended a sentence. "on the lathe." after "I put the blank" is the same sentence and stays as spoken.
    return [_capitalized(cue) if index == 0 or cue.segment != merged[index - 1].segment or _TERMINAL_END.search(merged[index - 1].text) else cue
            for index, cue in enumerate(merged)]


def wrap_cue_text(text: str, *, max_line: int = 42) -> list[str]:
    """Split long caption text into two balanced lines at a word boundary.

    Text at or under ``max_line`` columns stays on one line (a CJK character
    fills two). Longer text breaks once, choosing the word gap that keeps the
    halves closest in width, with a slight preference for breaking after
    punctuation. Text with no spaces breaks between its word-sized pieces. Text
    that already carries line breaks is left as laid out.
    """
    text = text.strip()
    if "\n" in text:
        return [line.strip() for line in text.split("\n") if line.strip()]
    if text_width(text) <= max_line:
        return [text]
    words = split_words(text)
    if len(words) < 2:
        return [text]
    # Each half is the text as written, cut where a piece starts, so Thai keeps the spaces between its phrases.
    starts: list[int] = []
    at = 0
    for word in words:
        starts.append(text.find(word, at))
        at = starts[-1] + len(word)
    best: tuple[float, list[str]] | None = None
    for index in range(1, len(words)):
        left, right = " ".join(text[:starts[index]].split()), " ".join(text[starts[index]:].split())
        score = abs(text_width(left) - text_width(right))
        if left[-1] in PHRASE_ENDS:
            score -= 1.5
        if best is None or score < best[0]:
            best = (score, [left, right])
    assert best is not None
    return best[1]


def write_srt(cues: list[Cue], path: Path) -> None:
    blocks = []
    for i, cue in enumerate(cues, start=1):
        blocks.append(
            f"{i}\n{format_ts(cue.start)} --> {format_ts(cue.end)}\n" + "\n".join(wrap_cue_text(cue.text)) + "\n"
        )
    if not blocks:
        # No speech was heard. FFmpeg's subtitles filter cannot probe an empty
        # file, so write one invisible 1 ms cue; the burn then renders nothing.
        blocks.append(f"1\n{format_ts(0)} --> {format_ts(0.001)}\n\u200b\n")
    path.write_text("\n".join(blocks) + "\n", encoding="utf-8")


def write_ass(cues: list[Cue], path: Path, *, canvas_width: int = 1920, canvas_height: int = 1080) -> None:
    font = "Arial"
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {canvas_width}
PlayResY: {canvas_height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},52,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,3,0,2,80,80,64,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    from vedit.caption_styles import ass_text  # caption_styles builds on this module, so it is read only here

    lines = [header]
    for cue in cues:
        # Heard text is data: a brace would open an override block (libass hides what is inside) and "\N" would break the line.
        text = r"\N".join(ass_text(line) for line in wrap_cue_text(cue.text))
        lines.append(
            f"Dialogue: 0,{format_ts(cue.start, srt=False)},{format_ts(cue.end, srt=False)},Default,,0,0,0,,{text}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


_SRT_BLOCK = re.compile(
    r"(\d+)\s+(\d{2}:\d{2}:\d{2}[,.]\d{3})\s+-->\s+(\d{2}:\d{2}:\d{2}[,.]\d{3})\s+(.*?)(?=\n\d+\s+\d{2}:|\Z)",
    re.S,
)


def read_srt(path: Path) -> list[Cue]:
    text = path.read_text(encoding="utf-8")
    cues: list[Cue] = []
    for match in _SRT_BLOCK.finditer(text.strip() + "\n"):
        body = re.sub(r"\s+", " ", match.group(4)).strip()
        if body:
            cues.append(Cue(parse_ts(match.group(2)), parse_ts(match.group(3)), body))
    return cues


def remap_cues(cues: list[Cue], keeps: list[Range]) -> list[Cue]:
    """Map original-timeline cues onto a silence-cut timeline.

    A cue that runs into a cut pause ends where the kept speech ends, so a line
    whose turn trails into the closing silence still shows. A cue heard wholly
    inside cut silence is dropped.
    """
    remapped: list[Cue] = []
    for cue in cues:
        new_start = _map_time(cue.start, keeps)
        new_end = _map_time(cue.end, keeps)
        if new_start is None or new_end is None or new_end <= new_start:
            continue
        remapped.append(Cue(new_start, new_end, cue.text))
    return remapped


def _map_time(t: float, keeps: list[Range]) -> float | None:
    elapsed = 0.0
    for keep in keeps:
        if keep.start <= t <= keep.end:
            return elapsed + (t - keep.start)
        if t < keep.start:
            return elapsed
        elapsed += keep.duration
    return elapsed if keeps else None


TRANSCRIBER = "meta-voice-transcribe"
MAX_PIECE_SECONDS = 590.0  # Meta takes up to 10 minutes per request; a longer source is heard in pieces
_PIECE_SEARCH_SECONDS = 60.0  # how far back from a piece's limit a pause is looked for
SPEECH_RATE = 16_000  # what Meta Voice Transcribe hears: mono 16-bit PCM at this rate
_QUIET_WINDOW = 0.03  # seconds of sound in one level reading when looking for a pause
_QUIET_MARGIN_DB = 10.0  # how far above the stretch's own quiet floor still counts as a pause
_SHORTEST_PIECE = 0.1  # seconds; nothing can be said in less, and Meta refuses an empty request


def find_whisper_model(*, allow_stub: bool = False) -> Path | None:
    """Retired: Briefcut runs no local speech model. Always None.

    Kept only so setup screens that still look for one import cleanly.
    """
    return None


# Meta Voice Transcribe sometimes joins a word to a copy of itself ("ThisThis").
# Only a capitalised half repeated exactly is mended, so "mama" or "tutu" stay. Any cased
# script counts ("ÜberÜber", "ЭтоЭто"); uncased scripts never match, so "谢谢" stays.
_JOINED_DOUBLE = re.compile(r"(?<![\w'’])([^\W\d_][\w'’]*?)\1(?![\w'’])")


def _mended(match: re.Match[str]) -> str:
    half = match.group(1)
    rest = half[1:]
    capitalised = half[0].isupper() and any(char.islower() for char in rest) and not any(char.isupper() for char in rest)
    return half if capitalised else match.group(0)


def mend_joined_doubles(text: str) -> str:
    """``"ThisThis is oak"`` becomes ``"This is oak"``. Separate doubled words are left to ``polish_cues``."""
    return _JOINED_DOUBLE.sub(_mended, text)


def heard_cues(records: Iterable[Any]) -> list[Cue]:
    """Meta's turns as transcript lines: split at sentence ends and line length, each word's time ``estimated``.

    Meta Voice Transcribe reports turn-level times, not word times. A turn's
    start and end are heard; inside a turn each word is spread by character
    count, so a sentence end inside a long turn is an estimate. These lines carry
    no words of their own, so nothing downstream cuts audio at a guessed word time.
    """
    lines: list[Cue] = []
    for cue in clean_transcript([cue for cue in map(_as_cue, records) if cue is not None]):
        cue = replace(cue, text=mend_joined_doubles(cue.text))
        spread = interpolate_words(cue)
        lines.extend(replace(line, words=()) for line in cues_from_words(list(spread.words), text=spread.text))
    return lines


def _pause_in(wav: Path, low: float, high: float) -> float:
    """The middle of the longest quiet stretch of ``wav`` between ``low`` and ``high`` seconds.

    Quiet is measured against this stretch's own floor, not a fixed level, so a
    whispered talk, a noisy room and speech over music each still find their
    breath. Ties go to the later stretch, which keeps pieces long.
    """
    from operator import mul

    from vedit.sound import decode_mono

    samples = decode_mono(wav, rate=SPEECH_RATE, start=low, seconds=high - low, track=0)
    size = max(1, round(_QUIET_WINDOW * SPEECH_RATE))
    levels = []
    for begin in range(0, len(samples) - size + 1, size):
        chunk = samples[begin:begin + size]
        levels.append(10 * math.log10(max(1.0, sum(map(mul, chunk, chunk)) / size)))
    if not levels:
        return high
    # The 2nd percentile, so one dropout cannot set the floor for a whole minute.
    quiet = sorted(levels)[len(levels) // 50] + _QUIET_MARGIN_DB
    best_length, best_stop, run = 0, len(levels), 0
    for index, level in enumerate(levels):
        run = run + 1 if level <= quiet else 0
        if run and run >= best_length:
            best_length, best_stop = run, index + 1
    return min(high, low + (best_stop - best_length / 2) * size / SPEECH_RATE)


def _pieces(wav: Path, seconds: float) -> list[tuple[float, float]]:
    """Cut a long recording into requests Meta accepts, each ending in the longest late pause.

    Times are rounded to the millisecond the cutter uses, so each piece's lines
    move back onto the source clock by exactly where that piece began.
    """
    if seconds <= MAX_PIECE_SECONDS:
        return [(0.0, seconds)]
    pieces: list[tuple[float, float]] = []
    start = 0.0
    while seconds - start > MAX_PIECE_SECONDS:
        limit = start + MAX_PIECE_SECONDS
        end = round(_pause_in(wav, limit - _PIECE_SEARCH_SECONDS, limit), 3)
        if end <= start:
            end = round(limit, 3)
        pieces.append((start, end))
        start = end
    pieces.append((start, seconds))
    return pieces


def _hear(wav: Path, work_dir: Path, *, language: str, budget_usd: float, ledger: Path) -> list[dict[str, Any]]:
    """Meta's turns for a 16 kHz mono WAV, on that WAV's clock. Silence is never sent, not even one piece of a long talk."""
    from vedit import meta

    if not audible(wav):
        return []
    seconds = probe(wav).duration
    pieces = _pieces(wav, seconds)
    if len(pieces) > 1:
        pieces = [(start, end) for start, end in pieces if end - start >= _SHORTEST_PIECE and audible(wav, start=start, seconds=end - start)]
    elif seconds < _SHORTEST_PIECE:
        return []
    # Said before anything is sent: a budget that cannot cover every piece would otherwise pay for a transcript it cannot finish.
    most = sum(math.ceil(end - start) for start, end in pieces) / 3600 * meta._TRANSCRIBE_PRICE_PER_HOUR
    if most > budget_usd + 1e-9:
        minutes = sum(end - start for start, end in pieces) / 60
        raise meta.MetaError(f"Transcribing {minutes:.0f} minutes of speech costs up to ${most:.2f}, more than this project's ${budget_usd:.2f} Meta budget. "
                             "Raise the budget, or shorten the source.")
    heard: list[dict[str, Any]] = []
    for index, (start, end) in enumerate(pieces):
        piece = wav
        if len(pieces) > 1 or (start, end) != (0.0, seconds):
            piece = work_dir / f"{wav.stem}-{index:02d}.wav"
            run_ffmpeg(["-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(wav), "-c:a", "pcm_s16le", "-ar", str(SPEECH_RATE), "-ac", "1", str(piece)])
        try:
            result = meta.transcribe_audio(piece, duration=probe(piece).duration, work_dir=ledger, language=language, budget_usd=budget_usd)
        finally:
            if piece is not wav:
                # Meta's cache is keyed by the audio itself, so a later run cuts the same bytes and pays nothing.
                piece.unlink(missing_ok=True)
        heard.extend({"start": float(cue["start"]) + start, "end": float(cue["end"]) + start, "text": cue["text"]} for cue in result.get("cues") or [])
    return heard


def transcribe(media: Path, work_dir: Path, *, language: str = "auto", budget_usd: float = .25, meta_dir: Path | None = None) -> list[Cue]:
    """Speech to timed lines with Meta Voice Transcribe, the only transcriber Briefcut calls.

    The audio track the editor plays (``MediaInfo.audio_map``) is heard as
    16 kHz mono PCM, whatever its rate or channel layout. Silence is never sent.
    A recording over Meta's 10-minute limit is heard in pieces cut at pauses,
    and each piece's lines are moved back onto the source clock. ``language``
    "auto" lets Meta decide. Raises ``MetaError`` when Meta is not connected or
    the request fails.
    """
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    info = probe(media)
    if not info.has_audio or info.duration < _SHORTEST_PIECE:
        return []
    wav = work_dir / "speech-16k.wav"
    try:
        run_ffmpeg(["-i", str(media), "-map", f"0:a:{info.audio_map}", "-vn", "-ac", "1", "-ar", str(SPEECH_RATE), "-c:a", "pcm_s16le", str(wav)])
        ledger = Path(meta_dir) if meta_dir is not None else work_dir / "meta"
        return heard_cues(_hear(wav, work_dir, language=language, budget_usd=budget_usd, ledger=ledger))
    finally:
        # An hour of speech is 115 MB at this rate. It is cut again, byte for byte, if it is needed.
        wav.unlink(missing_ok=True)


def transcribe_kept(source: Path, spans: list[Span], work_dir: Path, *, language: str = "auto", budget_usd: float = .25,
                    meta_dir: Path | None = None) -> list[Cue]:
    """Meta hears only the kept story audio, on the concat clock, for ``concat_cues``.

    The same rules as ``transcribe``: silence is never sent, a story over 10
    minutes is heard in pieces, and joined doubles are mended. Returns no lines
    when the kept audio is silent.
    """
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    if not spans or not probe(source).has_audio:
        return []
    wav = work_dir / "speech-16k.wav"
    try:
        extract_kept_audio(source, spans, wav)
        ledger = Path(meta_dir) if meta_dir is not None else work_dir / "meta"
        return heard_cues(_hear(wav, work_dir, language=language, budget_usd=budget_usd, ledger=ledger))
    finally:
        wav.unlink(missing_ok=True)


def attach_words(cues: list[Cue], words: list[Word]) -> list[Cue]:
    """Give each cue the words spoken in it. A word belongs to the cue that holds its midpoint."""
    if not words:
        return cues
    pending = sorted(words, key=lambda word: word.start)
    attached: list[Cue] = []
    index = 0
    for position, cue in enumerate(cues):
        limit = cues[position + 1].start if position + 1 < len(cues) else math.inf
        owned: list[Word] = []
        while index < len(pending) and (pending[index].start + pending[index].end) / 2 < limit:
            middle = (pending[index].start + pending[index].end) / 2
            if cue.start - 0.5 <= middle <= cue.end + 0.5:
                owned.append(pending[index])
            index += 1
        attached.append(replace(cue, text=join_words(word.text for word in owned), words=tuple(owned)) if owned else cue)
    return attached


def _word_spans(text: str, words: list[Word]) -> list[tuple[int, int]] | None:
    """Where each word sits in ``text``, in order, or None when the words are not that text's."""
    spans: list[tuple[int, int]] = []
    at = 0
    for word in words:
        found = text.find(word.text, at)
        if found < 0 or text[at:found].strip():
            return None
        spans.append((found, found + len(word.text)))
        at = found + len(word.text)
    return spans


def cues_from_words(words: list[Word], *, max_chars: int = CUE_CHARS, max_gap: float = 0.8, text: str | None = None) -> list[Cue]:
    """Lines from bare words, closing at sentence ends, long gaps, and the line length.

    With ``text``, the line the words were split from, each line keeps that
    text's own spacing, so Thai keeps the spaces between its phrases.
    """
    cues: list[Cue] = []
    current: list[Word] = []
    spans = _word_spans(text, words) if text is not None else None
    taken = 0

    def close() -> None:
        nonlocal taken
        if current:
            line = text[spans[taken][0]:spans[taken + len(current) - 1][1]] if spans and text is not None else join_words(word.text for word in current)
            cues.append(Cue(current[0].start, max(current[-1].end, current[0].start + 0.01), line, tuple(current)))
            taken += len(current)
            current.clear()

    for word in words:
        if current and (word.start - current[-1].end > max_gap or text_width(join_words(item.text for item in (*current, word))) > max_chars):
            close()
        current.append(word)
        if _TERMINAL_END.search(word.text):
            close()
    close()
    return cues


def extract_kept_audio(source: Path, spans: list[Span], wav: Path) -> None:
    """Write the kept pieces to one 16 kHz WAV, in order and at normal speed.

    This is the concat clock: a recognizer hears natural speech whatever the edit's
    speed, and ``concat_spans`` places what it hears. A piece the audio stream runs
    out on is padded so every later cue stays on time.
    """
    parts = []
    track = probe(source).audio_map
    for index, span in enumerate(spans):
        length = span.source_end - span.source_start
        parts.append(f"[0:a:{track}]atrim=start={span.source_start:.6f}:end={span.source_end:.6f},asetpts=PTS-STARTPTS,apad=whole_dur={length:.6f},atrim=end={length:.6f}[a{index}]")
    chain = "".join(f"[a{index}]" for index in range(len(spans)))
    graph = ";".join(parts) + f";{chain}concat=n={len(spans)}:v=0:a=1,aformat=sample_fmts=s16:channel_layouts=mono,aresample={SPEECH_RATE}[out]"
    run_ffmpeg(["-i", str(source), "-filter_complex", graph, "-map", "[out]", "-c:a", "pcm_s16le", "-ar", str(SPEECH_RATE), "-ac", "1", str(wav)])


def _view_spans(view: dict[str, Any]) -> list[Span]:
    fields = ("segment", "source_start", "source_end", "output_start", "output_end", "speed")
    return [span if isinstance(span, Span) else Span(**{name: span[name] for name in fields}) for span in view.get("spans") or []]


def _edit_of(plan: dict[str, Any]) -> dict[str, Any]:
    return plan["edit"] if isinstance(plan.get("edit"), dict) else {}


def _drop_phantoms(cues: list[Cue], speech: Any) -> list[Cue]:
    """A recognizer can invent a word or two on silence. A longer line stands even where the meter heard quiet speech as silence."""
    if not isinstance(speech, list):
        return cues
    bands = [(float(band["start"]), float(band["end"])) for band in speech if isinstance(band, dict) and _seconds(band.get("start")) is not None and _seconds(band.get("end")) is not None]
    return [cue for cue in cues if len(split_words(cue.text)) > PHANTOM_WORDS or any(min(high, cue.end) > max(low, cue.start) for low, high in bands)]


def source_cues(analysis: dict[str, Any]) -> list[Cue]:
    """The project's speech on the source clock, with each cue's words when they were heard."""
    lines = analysis.get("transcript") or analysis.get("sentences") or []
    cues = clean_transcript([cue for cue in map(_as_cue, lines) if cue is not None])
    words = [word for word in map(_as_word, analysis.get("words") or ()) if word is not None]
    return _drop_phantoms(attach_words(cues, words) if cues else cues_from_words(words), analysis.get("speech"))


def _placed(cues: list[Cue], spans: list[Span], *, on_source: Callable[[float], float] = float) -> list[Cue]:
    """Move cues onto the output clock, remembering where each piece began on the source clock."""
    placed: list[Cue] = []
    for cue in cues:
        estimated = any(word.estimated for word in cue.words)
        for index, piece in enumerate(map_cues([cue_record(cue)], spans)):
            moved = _as_cue(piece)
            if moved is None:
                continue
            words = tuple(replace(word, estimated=estimated) for word in moved.words)
            segment = int(piece["segment"])
            found = to_source(spans, moved.start, segment=segment)
            begin = cue.start if index == 0 or found is None else found[1]
            placed.append(replace(moved, words=words, segment=segment, source_start=on_source(begin)))
    placed.sort(key=lambda cue: (cue.start, cue.end))
    return placed


def cue_key(cue: Cue) -> str:
    """What ``edit.captions.edits`` is keyed by: the cue's source start in milliseconds."""
    return str(int(round((cue.start if cue.source_start is None else cue.source_start) * 1000)))


def _with_edits(cues: list[Cue], edits: Any) -> list[Cue]:
    """Replace edited lines. An edited line has no heard words, so its word times are estimated; an emptied line is gone."""
    if not isinstance(edits, dict) or not edits:
        return cues
    finished: list[Cue] = []
    for cue in cues:
        text = edits.get(cue_key(cue))
        if text is None:
            finished.append(cue)
        elif text.strip():
            finished.append(interpolate_words(replace(cue, text=" ".join(text.split()), words=())))
    return finished


def _settled(cues: list[Cue], duration: float) -> list[Cue]:
    """Fit cues to the output clock: inside the video, never two at once, short gaps held so lines do not blink."""
    settled: list[Cue] = []
    for index, cue in enumerate(cues):
        following = cues[index + 1].start if index + 1 < len(cues) else math.inf
        end = min(cue.end, following, duration)
        if following - end <= HOLD_SECONDS:
            end = min(following, duration)
        if end - cue.start < MIN_CUE_SECONDS:
            continue
        words = tuple(replace(word, start=min(word.start, end), end=min(word.end, end)) for word in cue.words)
        settled.append(replace(cue, start=round(cue.start, 3), end=round(end, 3), words=words))
    return settled


def _finished(cues: list[Cue], plan: dict[str, Any], view: dict[str, Any]) -> list[Cue]:
    settings = _edit_of(plan).get("captions")
    edits = settings.get("edits") if isinstance(settings, dict) else None
    duration = _seconds(view.get("duration")) or output_duration(_view_spans(view))
    return _settled(_with_edits(polish_cues(cues), edits), duration)


def story_cues(plan: dict[str, Any], view: dict[str, Any] | None = None) -> list[Cue]:
    """Finished cues from the transcript the plan already holds."""
    view = view if view is not None else plan_timeline(plan)
    analysis = plan["analysis"] if isinstance(plan.get("analysis"), dict) else {}
    return _finished(_placed(source_cues(analysis), _view_spans(view)), plan, view)


def concat_cues(cues: list[Cue], plan: dict[str, Any], view: dict[str, Any]) -> list[Cue]:
    """Finished cues from a recognizer that heard ``extract_kept_audio``. Turn-level cues get estimated words.

    It heard the moments in playback order, so a sentence it reports across a cut
    is one sentence as the viewer hears it, and polish may keep it whole.
    """
    spans = _view_spans(view)
    timed = [cue if cue.words else interpolate_words(cue) for cue in clean_transcript(cues)]
    placed = _placed(timed, concat_spans(spans), on_source=lambda moment: concat_to_source(spans, moment))
    return _finished([replace(cue, segment=0) for cue in placed], plan, view)


def preview_cues(plan: dict[str, Any], view: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """What ``timeline.captions`` shows. ``key`` is what an edit of that line is stored under."""
    records: list[dict[str, Any]] = []
    for cue in story_cues(plan, view):
        record = cue_record(cue)
        record["key"] = cue_key(cue)
        record["source_start"] = round(cue.start if cue.source_start is None else cue.source_start, 3)
        if any(word.estimated for word in cue.words):
            record["estimated"] = True
        records.append(record)
    return records


def _translated(cues: list[Cue], language: str, ctx: Any) -> list[Cue]:
    from vedit import meta

    from vedit import open_weights

    status = meta.capabilities()
    # Private mode translates on Muse Glimmer on this computer, so it needs no Meta key.
    if not status["available"] and not open_weights.enabled():
        raise meta.MetaError(f"Translated captions use Meta, which is not available here. {status['reason']} Or clear the caption language to render without translating.")
    # The speech is in the language the request or the plan names; when neither does, the model reads it from the lines.
    plan = getattr(ctx, "plan", None)
    source = str(ctx.request.get("language") or (plan.get("language") if isinstance(plan, dict) else "") or "auto")
    result = meta.translate_cues([cue_record(replace(cue, words=())) for cue in cues], language, work_dir=Path(ctx.work).parent / "meta", budget_usd=ctx.request.get("budget_usd", 0.25),
                                 source_language=source)
    return [cue for cue in map(_as_cue, result["cues"]) if cue is not None]


# Very common words, enough to tell these languages apart in a transcript. Data, not a model.
_COMMON_WORDS = {
    "en": "the and is to of in that it you was for on are with this be at have not but they we what so",
    "es": "el la que de y en los es un por con una para las del se no lo como más pero sus",
    "fr": "le la les de et est un une des du que qui pour pas dans ce il elle vous nous sur avec",
    "de": "der die und das ist ich nicht ein eine zu den mit sich des auf für sie es wir auch",
    "pt": "o a os as de e que é um uma para com não do da em no na mas você isso",
    "it": "il la che di e è un una per non con sono del della gli lo questo mi ma anche",
    "nl": "de het een en van is dat niet ik je op te met voor zijn maar ook wat er",
}
_COMMON = {code: frozenset(words.split()) for code, words in _COMMON_WORDS.items()}
# A script that names one language on its own.
_SCRIPT_LANGUAGE = (("ja", ((0x3040, 0x30FF),)), ("ko", ((0xAC00, 0xD7AF), (0x1100, 0x11FF))), ("ar", ((0x0600, 0x06FF),)),
                    ("he", ((0x0590, 0x05FF),)), ("hi", ((0x0900, 0x097F),)), ("th", ((0x0E00, 0x0E7F),)), ("el", ((0x0370, 0x03FF),)),
                    ("zh", ((0x4E00, 0x9FFF),)))


def guess_language(texts: Iterable[str]) -> str | None:
    """The language a transcript is spoken in, from its letters and its most common words, or None when unsure.

    Meta Voice Transcribe does not report the language it heard, so with "Detect automatically" this keeps a
    caption language that matches the speech from starting a needless, paid translation. No model is called.
    """
    text = " ".join(str(item) for item in texts if item)[:6000]
    letters = [char for char in text if char.isalpha()]
    if len(letters) < 12:
        return None
    for code, ranges in _SCRIPT_LANGUAGE:  # kana before Han: Japanese writes both
        share = sum(1 for char in letters if any(low <= ord(char) <= high for low, high in ranges)) / len(letters)
        if share >= 0.3:
            return code
    words = re.findall(r"[^\W\d_]+(?:'[^\W\d_]+)?", text.lower())
    if len(words) < 6:
        return None
    scores = sorted(((sum(1 for word in words if word in common) / len(words), code) for code, common in _COMMON.items()), reverse=True)
    (best, code), (second, _) = scores[0], scores[1]
    return code if best >= 0.12 and best >= 1.5 * second else None


def _base_language(code: Any) -> str:
    return str(code or "").strip().lower().replace("_", "-").split("-", 1)[0]


def translation_target(settings: Any, request: Any, *, plan: Any = None, heard: Iterable[str] | None = None) -> str | None:
    """The language the burned captions are translated into, or None when they show the words as spoken.

    ``edit.captions.language`` names it. The project's own spoken language means no translation. With the
    spoken language on "auto", the transcript (``heard``, else the plan's) is read to see what was said, so
    English captions on English speech are not translated. When unsure, the captions are translated.
    """
    language = settings.get("language") if isinstance(settings, dict) else None
    if not language:
        return None
    spoken = request.get("language") if isinstance(request, dict) else None
    if _base_language(spoken) == "auto":
        if heard is None and isinstance(plan, dict):
            analysis = plan.get("analysis") if isinstance(plan.get("analysis"), dict) else {}
            heard = [item.get("text", "") for item in analysis.get("transcript") or [] if isinstance(item, dict)]
        spoken = guess_language(heard or [])
    if spoken and _base_language(language) == _base_language(spoken):
        return None
    return str(language)


TRANSLATED_PUNCH_WARNING = "Punch words are not shown on translated captions."


def write_caption_files(cues: list[Cue], ctx: Any) -> Path:
    """Write ``captions.srt``, a translation when one was chosen, and the styled ``captions.ass`` that burns.

    A translation has new words at new places, so punch words picked on the
    spoken line are not drawn on it, and the render says so.
    """
    from vedit.caption_styles import write_styled_ass

    work = Path(ctx.work)
    settings = ctx.edit.get("captions") if isinstance(ctx.edit.get("captions"), dict) else {}
    write_srt(cues, work / "captions.srt")
    burned, notes = cues, []
    language = translation_target(settings, ctx.request, heard=[cue.text for cue in cues])
    if language:
        burned = _translated(cues, language, ctx)
        write_srt(burned, work / f"captions.{language}.srt")
        if settings.get("emphasize") or settings.get("emphasis"):
            notes.append(TRANSLATED_PUNCH_WARNING)
            settings = dict(settings, emphasize=False)
        if ctx.edit.get("hero_word"):
            notes.append("The hero word is the spoken word, so it is not translated with the captions.")
    ass = work / "captions.ass"
    write_styled_ass(burned, ass, width=ctx.canvas.width, height=ctx.canvas.height, settings=settings)
    warnings = getattr(ctx, "warnings", None)
    if isinstance(warnings, list):
        from vedit.caption_styles import caption_warnings

        warnings.extend(message for message in (*notes, *caption_warnings(burned)) if message not in warnings)
    return ass


def build_caption_file(plan: dict[str, Any], view: dict[str, Any], ctx: Any) -> Path | None:
    """The ASS file to burn, on the output clock, or None when no speech lands in the story.

    Nothing here runs a speech model: the transcript from planning is enough. A plan
    with no transcript at all gets one from the caller, which hears the kept audio.
    """
    cues = story_cues(plan, view)
    return write_caption_files(cues, ctx) if cues else None


def burn_filter(subtitle_path: Path, *, style: str = "lower-third") -> str:
    """Return an ffmpeg -vf fragment that burns captions."""
    escaped = filter_value(subtitle_path)
    if subtitle_path.suffix.lower() == ".ass" and has_filter("ass"):
        # A font chosen with VEDIT_FONT travels in a folder beside the captions.
        fonts = subtitle_path.parent / "fonts"
        return f"ass={escaped}" + (f":fontsdir={filter_value(fonts)}" if fonts.is_dir() else "")
    if has_filter("subtitles"):
        force = (
            "FontName=Arial,FontSize=28,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
            "BorderStyle=1,Outline=2,Shadow=0,MarginV=64,Alignment=2"
        )
        if style == "boxed":
            force = (
                "FontName=Arial,FontSize=26,PrimaryColour=&H00FFFFFF,BackColour=&H80000000,"
                "BorderStyle=3,Outline=1,MarginV=56,Alignment=2"
            )
        return f"subtitles={escaped}:force_style='{force}'"
    raise CaptionError(
        "This ffmpeg build has no subtitles/ass filter. "
        "Use ffmpeg-full (`brew install ffmpeg-full`) or set VEDIT_FFMPEG."
    )


def title_filter(
    text: str,
    canvas_height: int,
    *,
    y_ratio: float = 0.78,
    size: int = 56,
    box: bool = False,
    start: float | None = None,
    end: float | None = None,
) -> str:
    """Centered drawtext. ``start``/``end`` limit it to a window on the clip clock; ``box`` adds a dark card."""
    if not has_filter("drawtext"):
        raise CaptionError("drawtext filter missing — point VEDIT_FFMPEG at ffmpeg-full")
    font = filter_value(resolve_font(text))  # drawtext has no fallback: one font must hold every letter
    _ = canvas_height  # kept for callers; position is expression-based so any frame size works
    # expansion=none makes every character literal, so a plain "100%" cannot fail the render.
    spec = (
        f"drawtext=fontfile={font}:text={filter_value(text)}:expansion=none:"
        f"fontsize={size}:fontcolor=white:borderw=3:bordercolor=black:"
        f"x=(w-text_w)/2:y=h*{y_ratio:.3f}-text_h/2"
    )
    if box:
        spec += ":box=1:boxcolor=black@0.55:boxborderw=22"
    if start is not None and end is not None:
        spec += f":enable='between(t,{start:.3f},{end:.3f})'"
    elif start is not None:
        spec += f":enable='gte(t,{start:.3f})'"
    elif end is not None:
        spec += f":enable='lt(t,{end:.3f})'"
    return spec


def ffmpeg_has_text() -> bool:
    return has_filter("drawtext") or has_filter("subtitles") or has_filter("ass")


def transcribe_to_files(
    media: Path,
    work_dir: Path,
    *,
    srt: Path,
    ass: Path,
    language: str = "auto",
    keeps_path: Path | None = None,
    width: int = 1920,
    height: int = 1080,
    budget_usd: float = .25,
) -> list[Cue]:
    """Hear the media with Meta Voice Transcribe and write SRT/ASS. The ``captions`` brief step (`python -m vedit.captions`)."""
    cues = transcribe(media, work_dir, language=language, budget_usd=budget_usd)
    if keeps_path is not None and keeps_path.is_file():
        data = json.loads(keeps_path.read_text(encoding="utf-8"))
        keeps = [Range(float(k["start"]), float(k["end"])) for k in data.get("keeps") or []]
        if keeps:
            cues = remap_cues(cues, keeps)
    cues = polish_cues(cues)
    write_srt(cues, srt)
    write_ass(cues, ass, canvas_width=width, canvas_height=height)
    return cues


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="vedit.captions", description="Caption step: Meta Voice Transcribe to SRT/ASS")
    parser.add_argument("--media", required=True)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--srt", required=True)
    parser.add_argument("--ass", required=True)
    parser.add_argument("--keeps")
    parser.add_argument("--language", default="auto", help='the spoken language, or "auto" to let Meta decide')
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--budget", type=float, default=.25, help="most this step may spend on Meta, in USD")
    args = parser.parse_args(argv)
    transcribe_to_files(
        Path(args.media),
        Path(args.work_dir),
        srt=Path(args.srt),
        ass=Path(args.ass),
        language=args.language,
        keeps_path=Path(args.keeps) if args.keeps else None,
        width=args.width,
        height=args.height,
        budget_usd=args.budget,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
