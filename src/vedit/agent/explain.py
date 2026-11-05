"""Plain-language decisions for one sentence, and the caption gate.

Whether Meta can transcribe lives in ``transcriber_ready``. ``explain_decisions``
only reads the intent, the brief, and the inventory.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from vedit.agent.model import Decision, Intent, Inventory
from vedit.agent.policy import contract_for
from vedit.ir import Brief


def explain_decisions(
    intent: Intent,
    decision: Decision,
    inventory: Inventory,
    *,
    captions_skipped: bool,
) -> dict[str, object]:
    """Decisions a noob can read and a pro can check, plus the ordered program."""
    contract = contract_for(intent.platform)
    ops: list[dict[str, Any]] = [dict(op) if isinstance(op, dict) else {"op": str(op)} for op in decision.brief.ops]
    pauses = "cut" if _has_op(ops, "cut_silence") else "kept"
    pauses_sentence = "Pauses will be cut." if pauses == "cut" else "Pauses will be kept."
    shape = _shape(contract.width, contract.height, contract.reframe)
    shape_sentence = _shape_sentence(shape, contract.width, contract.height, contract.name)
    style = _caption_style(intent, ops)
    captions, captions_sentence = _caption_decision(
        intent,
        style=style,
        captions_skipped=captions_skipped,
        has_caption_op=_has_op(ops, "captions"),
    )
    clip = _clip_name(inventory)
    clip_sentence = f"Clip: {clip}."
    sentences = [pauses_sentence, shape_sentence, captions_sentence, clip_sentence]
    return {
        "pauses": pauses,
        "pauses_sentence": pauses_sentence,
        "shape": shape,
        "shape_sentence": shape_sentence,
        "captions": captions,
        "captions_sentence": captions_sentence,
        "caption_style": style,
        "clip": clip,
        "clip_sentence": clip_sentence,
        "program": ops,
        "sentences": sentences,
    }


def drop_captions(brief: Brief) -> tuple[Brief, bool]:
    """Return a brief with the captions op removed. The original is unchanged."""
    kept = [op for op in brief.ops if not (isinstance(op, dict) and op.get("op") == "captions")]
    if len(kept) == len(brief.ops):
        return brief, False
    return replace(brief, ops=kept), True


def transcriber_ready() -> bool:
    """True when Meta Voice Transcribe can hear the clip: a Meta key is set and Meta calls are allowed."""
    from vedit import meta

    status = meta.capabilities()
    return bool(status.get("available") and (status.get("features") or {}).get("transcribe"))


def _has_op(ops: list[dict[str, Any]], name: str) -> bool:
    return any(op.get("op") == name for op in ops)


def _shape(width: int, height: int, reframe: str | None) -> str:
    if reframe in {"16:9", "9:16", "1:1", "4:5"}:
        return str(reframe)
    if width == height:
        return "1:1"
    if height > width:
        return "9:16"
    return "16:9"


def _shape_sentence(shape: str, width: int, height: int, platform: str) -> str:
    if shape == "9:16":
        return f"Vertical 9:16, {width}×{height}."
    if shape == "1:1":
        return f"Square 1:1, {width}×{height}."
    if shape == "4:5":
        return f"Portrait 4:5, {width}×{height}."
    audience = {
        "youtube": "YouTube",
        "linkedin": "LinkedIn",
        "podcast": "a podcast",
        "preview": "a preview",
    }.get(platform, platform)
    return f"Landscape 16:9, {width}×{height}, for {audience}."


def _caption_style(intent: Intent, ops: list[dict[str, Any]]) -> str:
    for op in ops:
        if op.get("op") != "captions":
            continue
        style = op.get("style")
        if isinstance(style, str) and style.strip():
            return style.strip()
    if intent.platform == "shorts":
        return "boxed"
    return "lower-third"


def _caption_decision(
    intent: Intent,
    *,
    style: str,
    captions_skipped: bool,
    has_caption_op: bool,
) -> tuple[str, str]:
    if captions_skipped or (intent.captions and not has_caption_op):
        if style == "boxed":
            sentence = "Captions were not burned. Boxed captions stay off this short."
        else:
            sentence = "Captions were not burned. Lower-third captions stay off this cut."
        return "skipped", sentence
    if not intent.captions:
        return "off", "Captions stay off."
    if style == "boxed":
        return "burned", "Captions will be burned. Boxed, on the short."
    return "burned", "Captions will be burned. Lower-third, on the picture."


def _clip_name(inventory: Inventory) -> str:
    shot = inventory.primary_shot
    if shot is None and inventory.shots:
        shot = inventory.shots[0]
    if shot is None:
        return "clip"
    name = Path(shot.path).name
    return name or shot.name or "clip"
