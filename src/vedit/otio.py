"""OTIO-lite interchange. No OpenTimelineIO package required.

Writes a Timeline.1 JSON document other NLEs and agents can read.
This is an interchange sidecar, not a second source of truth — the brief is.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _rational(value: float, rate: float) -> dict[str, Any]:
    return {"OTIO_SCHEMA": "RationalTime.1", "value": float(value), "rate": float(rate)}


def pipeline_to_otio(
    *,
    name: str,
    work_dir: str,
    steps: list[str],
    meta: dict[str, Any],
) -> dict[str, Any]:
    canvas = meta.get("canvas")
    if not isinstance(canvas, dict):
        canvas = {}
    rate = float(canvas.get("fps") or 30.0)
    sources = meta.get("sources")
    if not isinstance(sources, dict):
        sources = {}
    ops = meta.get("ops")
    if not isinstance(ops, list):
        ops = []
    clips: list[dict[str, Any]] = []
    for src_name, path in sources.items():
        clips.append(
            {
                "OTIO_SCHEMA": "Clip.1",
                "name": str(src_name),
                "source_range": None,
                "media_reference": {
                    "OTIO_SCHEMA": "ExternalReference.1",
                    "target_url": str(path),
                    "available_range": None,
                },
                "metadata": {"vedit": {"source": str(src_name)}},
            }
        )
    if not clips:
        clips.append(
            {
                "OTIO_SCHEMA": "Clip.1",
                "name": name,
                "source_range": None,
                "media_reference": {"OTIO_SCHEMA": "MissingReference.1"},
            }
        )
    return {
        "OTIO_SCHEMA": "Timeline.1",
        "name": name,
        "global_start_time": _rational(0.0, rate),
        "metadata": {
            "vedit": {
                "canvas": canvas,
                "ops": ops,
                "steps": list(steps),
                "finals": meta.get("finals") or [],
                "work_dir": work_dir,
            }
        },
        "tracks": {
            "OTIO_SCHEMA": "Stack.1",
            "name": "tracks",
            "children": [
                {"OTIO_SCHEMA": "Track.1", "name": "V1", "kind": "Video", "children": clips},
                {"OTIO_SCHEMA": "Track.1", "name": "A1", "kind": "Audio", "children": list(clips)},
            ],
        },
    }


def write_otio(
    *,
    dest: Path,
    name: str,
    work_dir: str,
    steps: list[str],
    meta: dict[str, Any],
) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps(pipeline_to_otio(name=name, work_dir=work_dir, steps=steps, meta=meta), indent=2) + "\n",
        encoding="utf-8",
    )
    return dest
