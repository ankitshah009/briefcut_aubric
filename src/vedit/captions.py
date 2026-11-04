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


def split_words(text: str, *, run: int = 4) -> list[str]:
    """The words of a line. Text with no spaces is cut into short runs, keeping marks and closing punctuation attached."""
    words: list[str] = []
    for chunk in text.split():
        if not any(is_tight(char) for char in chunk):
            words.append(chunk)
            continue
        current = ""
        for char in chunk:
            kind = unicodedata.category(char)
            if len(current) >= run and kind[0] != "M" and kind not in ("Pe", "Pf", "Po"):
                words.append(current)
                current = ""
            current += char
        if current:
            words.append(current)
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


_TERMINAL_END = re.compile(r"""[.!?…]["'”’)\]]*$""")
_WORD_STRIP = re.compile(r"""^[^\w]+|[^\w]+$""")


def _word_key(word: str) -> str:
    return _WORD_STRIP.sub("", word).casefold()


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


def _capitalized(cue: Cue) -> Cue:
    if not cue.words:
        return replace(cue, text=_capitalize(cue.text))
    words = (replace(cue.words[0], text=_capitalize(cue.words[0].text)), *cue.words[1:])
    return replace(cue, text=join_words(word.text for word in words), words=words)


def polish_cues(cues: list[Cue], *, max_chars: int = 84, max_gap: float = 0.35) -> list[Cue]:
    """Clean provider cues for burn-in without touching timing except by merging.

    - Immediately repeated words collapse case-insensitively ("the the" -> "the"),
      also across a cue boundary when the next cue opens with the previous cue's
      last word (a Meta Voice Transcribe habit at turn edges).
    - A cue that does not end a sentence merges with the next one when it starts
      within ``max_gap`` seconds and the joined text fits ``max_chars``; a very
      short trailing cue (<= 0.8 s or <= 2 words) merges into its predecessor
      the same way. Splitting turns at the line length produces both shapes.
    - Whitespace is normalized, each cue starts with a capital, empties are dropped.

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
            if previous is not None and key and not is_tight(key[0]) and key == _word_key(_token_text(previous)):
                # Keep whichever copy carries the punctuation ("lathe lathe," -> "lathe,").
                punctuation = bool(_WORD_STRIP.search(word)) and not _WORD_STRIP.search(_token_text(previous))
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

    # Merge fragments into whole sentences where the clock allows.
    merged: list[Cue] = []
    for cue in collapsed:
        if merged and merged[-1].segment == cue.segment:
            previous = merged[-1]
            gap = cue.start - previous.end
            joined = join_words((previous.text, cue.text))
            fits = len(joined) <= max_chars and gap <= max_gap
            open_ended = not _TERMINAL_END.search(previous.text)
            short_trailer = (cue.end - cue.start) <= 0.8 or len(cue.text.split()) <= 2
            if fits and (open_ended or short_trailer):
                words = previous.words + cue.words if previous.words and cue.words else ()
                merged[-1] = replace(previous, end=max(previous.end, cue.end), text=joined, words=words)
                continue
        merged.append(cue)

    return [_capitalized(cue) for cue in merged]


def wrap_cue_text(text: str, *, max_line: int = 42) -> list[str]:
    """Split long caption text into two balanced lines at a word boundary.

    Text at or under ``max_line`` characters stays on one line. Longer text
    breaks once, choosing the word gap that keeps the halves closest in
    length, with a slight preference for breaking after punctuation. Text
    that already carries line breaks is left as laid out.
    """
    text = text.strip()
    if "\n" in text:
        return [line.strip() for line in text.split("\n") if line.strip()]
    if len(text) <= max_line:
        return [text]
    words = text.split()
    if len(words) < 2:
        return [text]
    best: tuple[float, list[str]] | None = None
    for index in range(1, len(words)):
        left, right = " ".join(words[:index]), " ".join(words[index:])
        score = abs(len(left) - len(right))
        if left[-1] in ",.;:!?":
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
    lines = [header]
    for cue in cues:
        text = r"\N".join(wrap_cue_text(cue.text))
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
    """Map original-timeline cues onto a silence-cut timeline."""
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
    return None


TRANSCRIBER = "meta-voice-transcribe"
MAX_PIECE_SECONDS = 590.0  # Meta takes up to 10 minutes per request; a longer source is heard in pieces
_PIECE_SEARCH_SECONDS = 60.0  # how far back from a piece's limit a pause is looked for


def find_whisper_model(*, allow_stub: bool = False) -> Path | None:
    """Retired: Briefcut runs no local speech model. Always None.

    Kept only so setup screens that still look for one import cleanly.
    """
    return None


def heard_cues(records: Iterable[Any]) -> list[Cue]:
    """Meta's turns as transcript lines: split at sentence ends and line length, each word's time ``estimated``.

    Meta Voice Transcribe reports turn-level times, not word times. A turn's
    start and end are heard; inside a turn each word is spread by character
    count, so a sentence end inside a long turn is an estimate. These lines carry
    no words of their own, so nothing downstream cuts audio at a guessed word time.
    """
    lines: list[Cue] = []
    for cue in clean_transcript([cue for cue in map(_as_cue, records) if cue is not None]):
        spread = interpolate_words(cue)
        lines.extend(replace(line, words=()) for line in cues_from_words(list(spread.words)))
    return lines


def _pieces(wav: Path, seconds: float) -> list[tuple[float, float]]:
    """Cut a long recording into requests Meta accepts, each ending in the longest late pause when there is one."""
    if seconds <= MAX_PIECE_SECONDS:
        return [(0.0, seconds)]
    from vedit.analyze import detect_silence

    pauses = detect_silence(wav, threshold_db=-35.0, min_silence=0.3)
    pieces: list[tuple[float, float]] = []
    start = 0.0
    while seconds - start > MAX_PIECE_SECONDS:
        limit = start + MAX_PIECE_SECONDS
        late = [pause for pause in pauses if limit - _PIECE_SEARCH_SECONDS <= (pause.start + pause.end) / 2 <= limit]
        widest = max(late, key=lambda pause: pause.end - pause.start, default=None)
        end = (widest.start + widest.end) / 2 if widest is not None else limit
        pieces.append((start, end))
        start = end
    pieces.append((start, seconds))
    return pieces


def transcribe(media: Path, work_dir: Path, *, language: str = "en", budget_usd: float = .25, meta_dir: Path | None = None) -> list[Cue]:
    """Speech to timed lines with Meta Voice Transcribe, the only transcriber Briefcut calls.

    The first audio track is heard as 16 kHz mono PCM. Silence is never sent.
    A recording over Meta's 10-minute limit is heard in pieces cut at pauses,
    and each piece's lines are moved back onto the source clock. Raises
    ``MetaError`` when Meta is not connected or the request fails.
    """
    from vedit import meta

    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    info = probe(media)
    if not info.has_audio:
        return []
    wav = work_dir / "speech-16k.wav"
    run_ffmpeg(["-i", str(media), "-map", f"0:a:{info.audio_map}", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)])
    if not audible(wav):
        return []
    seconds = probe(wav).duration
    ledger = Path(meta_dir) if meta_dir is not None else work_dir / "meta"
    heard: list[dict[str, Any]] = []
    for index, (start, end) in enumerate(_pieces(wav, seconds)):
        piece = wav
        if (start, end) != (0.0, seconds):
            piece = work_dir / f"speech-16k-{index:02d}.wav"
            run_ffmpeg(["-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(wav), "-c:a", "pcm_s16le", "-ar", "16000", "-ac", "1", str(piece)])
        result = meta.transcribe_audio(piece, duration=probe(piece).duration, work_dir=ledger, language=language, budget_usd=budget_usd)
        heard.extend({"start": float(cue["start"]) + start, "end": float(cue["end"]) + start, "text": cue["text"]} for cue in result.get("cues") or [])
    return heard_cues(heard)


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


def cues_from_words(words: list[Word], *, max_chars: int = CUE_CHARS, max_gap: float = 0.8) -> list[Cue]:
    """Lines from bare words, closing at sentence ends, long gaps, and the line length."""
    cues: list[Cue] = []
    current: list[Word] = []

    def close() -> None:
        if current:
            cues.append(Cue(current[0].start, max(current[-1].end, current[0].start + 0.01), join_words(word.text for word in current), tuple(current)))
            current.clear()

    for word in words:
        if current and (word.start - current[-1].end > max_gap or len(join_words(item.text for item in (*current, word))) > max_chars):
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
    graph = ";".join(parts) + f";{chain}concat=n={len(spans)}:v=0:a=1,aformat=sample_fmts=s16:channel_layouts=mono,aresample=16000[out]"
    run_ffmpeg(["-i", str(source), "-filter_complex", graph, "-map", "[out]", "-c:a", "pcm_s16le", "-ar", "16000", "-ac", "1", str(wav)])


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

    status = meta.capabilities()
    if not status["available"]:
        raise meta.MetaError(f"Translated captions use Meta, which is not available here. {status['reason']} Or clear the caption language to render without translating.")
    result = meta.translate_cues([cue_record(replace(cue, words=())) for cue in cues], language, work_dir=Path(ctx.work).parent / "meta", budget_usd=ctx.request.get("budget_usd", 0.25),
                                 source_language=str(ctx.request.get("language") or "en"))
    return [cue for cue in map(_as_cue, result["cues"]) if cue is not None]


def write_caption_files(cues: list[Cue], ctx: Any) -> Path:
    """Write ``captions.srt``, a translation when one was chosen, and the styled ``captions.ass`` that burns."""
    from vedit.caption_styles import write_styled_ass

    work = Path(ctx.work)
    settings = ctx.edit.get("captions") if isinstance(ctx.edit.get("captions"), dict) else {}
    write_srt(cues, work / "captions.srt")
    burned = cues
    language = settings.get("language")
    if language and str(language).lower() != str(ctx.request.get("language") or "").lower():
        burned = _translated(cues, str(language), ctx)
        write_srt(burned, work / f"captions.{language}.srt")
    ass = work / "captions.ass"
    write_styled_ass(burned, ass, width=ctx.canvas.width, height=ctx.canvas.height, settings=settings)
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
    font = filter_value(resolve_font())
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
    language: str = "en",
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
    parser.add_argument("--language", default="en")
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
