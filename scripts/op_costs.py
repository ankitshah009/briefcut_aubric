#!/usr/bin/env python3
"""Measure what each finishing op adds to a render.

    uv run python scripts/op_costs.py <clip> <work-dir>

Use a 20 s 1080x1920 clip. Each case runs twice and the faster time is kept.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from vedit.compile import compile_brief
from vedit.ir import Brief, Canvas
from vedit.media import probe
from vedit.pipeline import execute
from vedit.sound import bundled_music

REPO = Path(__file__).resolve().parents[1]


def main() -> int:
    clip, work = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
    length = probe(clip).duration
    logo = str(REPO / "examples" / "demo" / "logo.png")
    bed = next(item["path"] for item in bundled_music() if item["id"] == "calm_keys")
    step = length / 4
    moves = [{"start": i * step, "end": (i + 1) * step, "from": {"scale": 1, "x": .5, "y": .5}, "to": {"scale": 1.09, "x": .5, "y": .5}} for i in range(4)]
    text = [{"op": "text", "text": "Day one", "style": "title", "position": "top", "start": 1, "duration": 4},
            {"op": "text", "text": "The organizer", "style": "lower_third", "start": 6, "duration": 4},
            {"op": "text", "text": "Link in bio", "style": "callout", "position": "bottom", "start": 12, "duration": 4}]
    cases = {
        "baseline (export only)": [],
        "look: cinematic": [{"op": "look", "preset": "cinematic"}],
        "look: auto": [{"op": "look", "preset": "auto"}],
        "look: film": [{"op": "look", "preset": "film"}],
        "zoom: 4 slow pushes": [{"op": "zoom", "moves": moves}],
        "stabilize": [{"op": "stabilize"}],
        "voice: denoise and enhance": [{"op": "voice"}],
        "text: 3 items": text,
        "logo": [{"op": "logo", "source": "logo"}],
        "overlay: timed": [{"op": "overlay", "source": "logo", "start": 4, "duration": 4, "position": "center", "scale": .3}],
        "music: ducked": [{"op": "music", "source": "bed", "volume_db": -16}],
        "everything": [{"op": "stabilize"}, {"op": "zoom", "moves": moves}, {"op": "look", "preset": "cinematic"}, {"op": "voice"}, *text,
                       {"op": "logo", "source": "logo"}, {"op": "music", "source": "bed"}, {"op": "loudnorm", "i": -16}],
    }
    base = None
    for index, (name, ops) in enumerate(cases.items()):
        best = float("inf")
        for attempt in range(2):
            folder = work / f"{index}_{attempt}"
            brief = Brief(name="cost", sources={"main": str(clip), "logo": logo, "bed": bed},
                          ops=[*ops, {"op": "export", "preset": "shorts_9x16", "path": str(folder / "out.mp4")}],
                          canvas=Canvas(width=1080, height=1920, fps=30.0), work_dir=str(folder))
            compiler = compile_brief(brief, work_dir=folder)
            started = time.perf_counter()
            execute(compiler.pipeline, progress=False)
            best = min(best, time.perf_counter() - started)
        base = best if base is None else base
        info = probe(folder / "out.mp4")
        print(f"{name:30s} {best:7.2f} s  +{best - base:6.2f} s  {length / best:5.1f}x realtime  {info.size_bytes / 1e6:6.1f} MB  {info.video.color_space}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
