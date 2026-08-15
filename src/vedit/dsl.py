"""Constrained English / flag DSL → Brief. No paid LLM required."""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any

from vedit.errors import BriefError
from vedit.ir import Brief, Canvas

_FILE = re.compile(r"(\S+\.(?:mp4|mov|mkv|webm|avi|wav|mp3|m4a|aac|flac|png|jpg|jpeg|webp))", re.I)


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


def _parse_flags(text: str, *, extra_sources: dict[str, str] | None) -> Brief:
    try:
        tokens = shlex.split(text)
    except ValueError as exc:
        raise BriefError(f"could not parse flags: {exc}") from exc
    return _parse_flag_tokens(tokens, extra_sources=extra_sources)


def _parse_flag_tokens(tokens: list[str], *, extra_sources: dict[str, str] | None) -> Brief:
    positional: list[str] = []
    flags: dict[str, Any] = {}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("--"):
            key = tok[2:].replace("-", "_")
            if i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
                flags[key] = tokens[i + 1]
                i += 2
            else:
                flags[key] = True
                i += 1
        else:
            positional.append(tok)
            i += 1
    sources: dict[str, str] = dict(extra_sources or {})
    ops: list[dict[str, Any]] = []
    main = positional[0] if positional else flags.get("in") or flags.get("input")
    if not main:
        raise BriefError("pass an input file (positional or --in)")
    sources["main"] = str(Path(str(main)).expanduser())
    name = Path(sources["main"]).stem
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
        ops.append({"op": "fade_in", "duration": 0.3})
        ops.append({"op": "fade_out", "duration": 0.5})
    presets: list[str] = []
    if flags.get("preset"):
        presets = [str(flags["preset"])]
    ops.append({"op": "export", "presets": presets or ["youtube_1080p"], "path": flags.get("out")})
    return Brief(name=name, sources=sources, ops=ops, canvas=Canvas(), presets=presets)


def _parse_english(text: str, *, extra_sources: dict[str, str] | None) -> Brief:
    clauses = [c.strip() for c in re.split(r"[;\n]+", text) if c.strip()]
    sources: dict[str, str] = dict(extra_sources or {})
    ops: list[dict[str, Any]] = []
    presets: list[str] = []
    name = "edit"
    for clause in clauses:
        lower = clause.lower()
        files = _FILE.findall(clause)
        if files and "main" not in sources:
            sources["main"] = str(Path(files[0]).expanduser())
            name = Path(files[0]).stem
        if re.search(r"cut\s+silence|remove\s+silence|jump\s*cut", lower):
            ops.append({"op": "cut_silence"})
        elif re.search(r"\btrim\b", lower):
            nums = re.findall(r"(\d+(?:\.\d+)?)", clause)
            start = float(nums[0]) if nums else 0.0
            end = float(nums[1]) if len(nums) > 1 else None
            ops.append({"op": "trim", "start": start, "end": end})
        elif re.search(r"caption|subtitl|transcrib", lower):
            ops.append({"op": "captions", "burn": True})
        elif re.search(r"\bduck\b|\bmusic\b|\bbed\b", lower):
            audio = next((f for f in files if Path(f).suffix.lower() in {".mp3", ".wav", ".m4a", ".aac", ".flac"}), None)
            if audio:
                sources["music"] = str(Path(audio).expanduser())
            if "music" not in sources:
                raise BriefError(f"music/duck clause needs an audio file: {clause}")
            ops.append({"op": "music", "source": "music", "duck": True})
        elif re.search(r"reframe|vertical|shorts|9:16|tiktok|reels", lower):
            ops.append({"op": "reframe", "aspect": "9:16"})
            presets.append("shorts_9x16")
        elif re.search(r"loudnorm|normalize|ebu|r128", lower):
            ops.append({"op": "loudnorm"})
        elif re.search(r"highpass|rumble", lower):
            ops.append({"op": "highpass", "hz": 80})
        elif re.search(r"denoise|noise\s+reduc", lower):
            ops.append({"op": "denoise"})
        elif re.search(r"fade", lower):
            ops.append({"op": "fade_in", "duration": 0.3})
            ops.append({"op": "fade_out", "duration": 0.5})
        elif re.search(r"title|lower.?third", lower):
            quoted = re.search(r"[\"'](.+?)[\"']", clause)
            ops.append({"op": "title", "text": quoted.group(1) if quoted else clause})
        elif re.search(r"concat|join|stitch", lower):
            ops.append({"op": "concat", "sources": files or list(sources.values())})
        elif re.search(r"slideshow|from images", lower):
            ops.append({"op": "slideshow", "images": files})
        elif re.search(r"export|render|youtube|preset", lower):
            if "short" in lower or "9:16" in lower:
                presets.append("shorts_9x16")
            elif "preview" in lower:
                presets.append("web_preview")
            elif "audio" in lower or "mp3" in lower:
                presets.append("podcast_audio")
            else:
                presets.append("youtube_1080p")
        elif files and "main" in sources and files[0] not in sources.values():
            # extra file mentioned without a verb — ignore rather than invent an op
            continue
        else:
            raise BriefError(f"could not parse clause: {clause!r}")
    if "main" not in sources and sources:
        first = next(iter(sources))
        sources["main"] = sources[first]
    if "main" not in sources:
        raise BriefError("no input file found in do-string")
    if not any(op.get("op") == "export" for op in ops):
        ops.append({"op": "export", "presets": presets or ["youtube_1080p"]})
    return Brief(name=name, sources=sources, ops=ops, canvas=Canvas(), presets=presets)
