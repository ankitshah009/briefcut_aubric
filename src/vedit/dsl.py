"""Constrained English / flag DSL → Brief. No paid LLM required."""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable
from pathlib import Path
from typing import Any

from vedit.errors import BriefError
from vedit.ir import Brief, Canvas

_FILE = re.compile(r"(\S+\.(?:mp4|mov|mkv|webm|avi|wav|mp3|m4a|aac|flac|png|jpg|jpeg|webp))", re.IGNORECASE)
_AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".flac"}
_DEFAULT_PRESET = "youtube_1080p"
_FADE_OPS = ({"op": "fade_in", "duration": 0.3}, {"op": "fade_out", "duration": 0.5})


def parse_do(text: str | list[str], *, extra_sources: dict[str, str] | None = None) -> Brief:
    """
    Accepts either:
      talk.mp4 --cut-silence --captions --music bed.mp3 --preset shorts
    or:
      cut silence from talk.mp4; captions; duck bed.mp3; reframe 9:16; export shorts
    """
    if isinstance(text, list):
        tokens = [str(t) for t in text if str(t)]
        if tokens and tokens[0] == "--":
            tokens = tokens[1:]
        if not tokens:
            raise BriefError("empty do-string")
        if len(tokens) == 1:
            # One quoted sentence ("cut silence from talk.mp4; captions") is a string, not an argv.
            return parse_do(tokens[0], extra_sources=extra_sources)
        if any(t.startswith("--") for t in tokens) or Path(tokens[0]).suffix:
            return _parse_flag_tokens(tokens, extra_sources=extra_sources)
        return _parse_english(" ".join(tokens), extra_sources=extra_sources)
    raw = text.strip()
    if not raw:
        raise BriefError("empty do-string")
    if "--" in raw or (raw.split() and Path(raw.split()[0]).suffix):
        try:
            tokens = shlex.split(raw)
        except ValueError as exc:
            raise BriefError(f"could not parse flags: {exc}") from exc
        return _parse_flag_tokens(tokens, extra_sources=extra_sources)
    return _parse_english(raw, extra_sources=extra_sources)


def _split_flags(tokens: list[str]) -> tuple[list[str], dict[str, Any]]:
    """Positional words and ``--flag [value]`` pairs; a flag with no value reads as True."""
    positional: list[str] = []
    flags: dict[str, Any] = {}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if not tok.startswith("--"):
            positional.append(tok)
            i += 1
            continue
        key = tok[2:].replace("-", "_")
        if i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
            flags[key] = tokens[i + 1]
            i += 2
        else:
            flags[key] = True
            i += 1
    return positional, flags


def _flag_ops(flags: dict[str, Any], sources: dict[str, str]) -> list[dict[str, Any]]:
    """The ops the flags ask for, in render order. Files named by a flag are added to ``sources``."""
    ops: list[dict[str, Any]] = []
    if flags.get("cut_silence") or flags.get("silence"):
        ops.append({"op": "cut_silence"})
    if flags.get("highpass"):
        hz = 80 if flags["highpass"] is True else int(flags["highpass"])
        ops.append({"op": "highpass", "hz": hz})
    if flags.get("trim"):
        start, _, end = str(flags["trim"]).partition("-")
        ops.append({"op": "trim", "start": float(start), "end": float(end) if end else None})
    if flags.get("title"):
        ops.append({"op": "title", "text": str(flags["title"])})
    if flags.get("captions") or flags.get("subs"):
        ops.append({"op": "captions", "burn": True})
    if flags.get("watermark"):
        sources["logo"] = str(Path(str(flags["watermark"])).expanduser())
        ops.append({"op": "watermark", "source": "logo"})
    if flags.get("music"):
        sources["music"] = str(Path(str(flags["music"])).expanduser())
        ops.append({"op": "music", "source": "music", "duck": True})
    if flags.get("reframe"):
        ops.append({"op": "reframe", "aspect": str(flags["reframe"])})
    if flags.get("loudnorm") or flags.get("normalize"):
        ops.append({"op": "loudnorm"})
    if flags.get("denoise"):
        ops.append({"op": "denoise"})
    if flags.get("fade"):
        ops.extend(dict(op) for op in _FADE_OPS)
    return ops


def _parse_flag_tokens(tokens: list[str], *, extra_sources: dict[str, str] | None) -> Brief:
    positional, flags = _split_flags(tokens)
    sources: dict[str, str] = dict(extra_sources or {})
    main = positional[0] if positional else flags.get("in") or flags.get("input")
    if not main:
        raise BriefError("pass an input file (positional or --in)")
    sources["main"] = str(Path(str(main)).expanduser())
    name = Path(sources["main"]).stem
    ops = _flag_ops(flags, sources)
    presets: list[str] = [str(flags["preset"])] if flags.get("preset") else []
    ops.append({"op": "export", "presets": presets or [_DEFAULT_PRESET], "path": flags.get("out")})
    return Brief(name=name, sources=sources, ops=ops, canvas=Canvas(), presets=presets)


# An English clause handler: (clause, files named in it, sources so far, presets so far) -> ops.
# It may add to ``sources`` and ``presets``.
_ClauseOps = Callable[[str, list[str], dict[str, str], list[str]], list[dict[str, Any]]]


def _fixed(*ops: dict[str, Any]) -> _ClauseOps:
    """A handler that returns fresh copies of the same ops every time."""
    return lambda _clause, _files, _sources, _presets: [dict(op) for op in ops]


def _trim_ops(clause: str, _files: list[str], _sources: dict[str, str], _presets: list[str]) -> list[dict[str, Any]]:
    nums = re.findall(r"(\d+(?:\.\d+)?)", clause)
    start = float(nums[0]) if nums else 0.0
    end = float(nums[1]) if len(nums) > 1 else None
    return [{"op": "trim", "start": start, "end": end}]


def _music_ops(clause: str, files: list[str], sources: dict[str, str], _presets: list[str]) -> list[dict[str, Any]]:
    audio = next((f for f in files if Path(f).suffix.lower() in _AUDIO_SUFFIXES), None)
    if audio:
        sources["music"] = str(Path(audio).expanduser())
    if "music" not in sources:
        raise BriefError(f"music/duck clause needs an audio file: {clause}")
    return [{"op": "music", "source": "music", "duck": True}]


def _reframe_ops(_clause: str, _files: list[str], _sources: dict[str, str], presets: list[str]) -> list[dict[str, Any]]:
    presets.append("shorts_9x16")
    return [{"op": "reframe", "aspect": "9:16"}]


def _title_ops(clause: str, _files: list[str], _sources: dict[str, str], _presets: list[str]) -> list[dict[str, Any]]:
    quoted = re.search(r"[\"'](.+?)[\"']", clause)
    return [{"op": "title", "text": quoted.group(1) if quoted else clause}]


def _concat_ops(_clause: str, files: list[str], sources: dict[str, str], _presets: list[str]) -> list[dict[str, Any]]:
    return [{"op": "concat", "sources": files or list(sources.values())}]


def _slideshow_ops(
    _clause: str, files: list[str], _sources: dict[str, str], _presets: list[str]
) -> list[dict[str, Any]]:
    return [{"op": "slideshow", "images": files}]


def _export_preset(lower: str) -> str:
    if "short" in lower or "9:16" in lower:
        return "shorts_9x16"
    if "preview" in lower:
        return "web_preview"
    if "audio" in lower or "mp3" in lower:
        return "podcast_audio"
    return _DEFAULT_PRESET


def _export_ops(clause: str, _files: list[str], _sources: dict[str, str], presets: list[str]) -> list[dict[str, Any]]:
    presets.append(_export_preset(clause.lower()))
    return []


# In priority order: the first pattern that matches a clause decides what it means.
_CLAUSES: tuple[tuple[re.Pattern[str], _ClauseOps], ...] = (
    (re.compile(r"cut\s+silence|remove\s+silence|jump\s*cut"), _fixed({"op": "cut_silence"})),
    (re.compile(r"\btrim\b"), _trim_ops),
    (re.compile(r"caption|subtitl|transcrib"), _fixed({"op": "captions", "burn": True})),
    (re.compile(r"\bduck\b|\bmusic\b|\bbed\b"), _music_ops),
    (re.compile(r"reframe|vertical|shorts|9:16|tiktok|reels"), _reframe_ops),
    (re.compile(r"loudnorm|normalize|ebu|r128"), _fixed({"op": "loudnorm"})),
    (re.compile(r"highpass|rumble"), _fixed({"op": "highpass", "hz": 80})),
    (re.compile(r"denoise|noise\s+reduc"), _fixed({"op": "denoise"})),
    (re.compile(r"fade"), _fixed(*_FADE_OPS)),
    (re.compile(r"title|lower.?third"), _title_ops),
    (re.compile(r"concat|join|stitch"), _concat_ops),
    (re.compile(r"slideshow|from images"), _slideshow_ops),
    (re.compile(r"export|render|youtube|preset"), _export_ops),
)


def _clause_ops(clause: str, files: list[str], sources: dict[str, str], presets: list[str]) -> list[dict[str, Any]]:
    lower = clause.lower()
    for pattern, handler in _CLAUSES:
        if pattern.search(lower):
            return handler(clause, files, sources, presets)
    if files and "main" in sources and files[0] not in sources.values():
        return []  # an extra file named without a verb: ignored rather than inventing an op
    raise BriefError(f"could not parse clause: {clause!r}")


def _parse_english(text: str, *, extra_sources: dict[str, str] | None) -> Brief:
    clauses = [c.strip() for c in re.split(r"[;\n]+", text) if c.strip()]
    sources: dict[str, str] = dict(extra_sources or {})
    ops: list[dict[str, Any]] = []
    presets: list[str] = []
    name = "edit"
    for clause in clauses:
        files = _FILE.findall(clause)
        if files and "main" not in sources:
            sources["main"] = str(Path(files[0]).expanduser())
            name = Path(files[0]).stem
        ops.extend(_clause_ops(clause, files, sources, presets))
    if "main" not in sources and sources:
        first = next(iter(sources))
        sources["main"] = sources[first]
    if "main" not in sources:
        raise BriefError("no input file found in do-string")
    if not any(op.get("op") == "export" for op in ops):
        ops.append({"op": "export", "presets": presets or [_DEFAULT_PRESET]})
    return Brief(name=name, sources=sources, ops=ops, canvas=Canvas(), presets=presets)
