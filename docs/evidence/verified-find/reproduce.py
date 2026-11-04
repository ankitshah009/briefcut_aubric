"""Live proof for verified find: Muse Spark finds, SAM 3.1 checks. Writes results.json, thumbs/ and contact-sheet.jpg here.

Run from the repository root with the key in the environment only:
    set -a; . ./.env.local; set +a; uv run python docs/evidence/verified-find/reproduce.py WORK_DIR
The whole run shares one $0.15 ledger cap, so Meta can never be charged more than that.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from vedit import meta
from vedit.media import filter_value, probe, resolve_font, run_ffmpeg
from vedit.verify_find import verify_moments

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SOURCE = ROOT / "examples" / "demo" / "workshop.mp4"
BUDGET = 0.15
QUERIES = ["the wooden bowl", "the gouge", "a dog"]


def relative(path: str | None) -> str | None:
    return str(Path(path).resolve().relative_to(HERE)) if path else None


def main(work: Path) -> None:
    work = work.resolve()
    thumbs = HERE / "thumbs"
    shutil.rmtree(thumbs, ignore_errors=True)
    duration = probe(SOURCE).duration
    runs = []
    for query in QUERIES:
        spark = meta.find_moments(SOURCE, query=query, duration=duration, work_dir=work, budget_usd=BUDGET)
        checked = verify_moments(spark["moments"], query, SOURCE, work, BUDGET, out_dir=thumbs)
        runs.append({"query": query, "spark": {"model": spark.get("model"), "usage": spark.get("usage"), "moments": spark["moments"]},
                     "verified": checked})
    # Muse Spark saw no dog, so SAM had nothing to reject. Hand SAM Muse Spark's first two bowl moments
    # under the dog query: a stand-in for a confident wrong answer, so the rejection path runs live.
    # SAM found no "gouge" where the gouge is plainly visible. One more call asks for the generic "tool" in the
    # same window, to tell a naming miss from a hallucination. Labelled as an override; not part of the default path.
    gouge = runs[1]["spark"]["moments"][:1]
    if gouge:
        checked = verify_moments(gouge, "the gouge", SOURCE, work, BUDGET, out_dir=thumbs, phrase="tool")
        runs.append({"query": "the gouge", "phrase_override": "tool", "spark": runs[1]["spark"], "verified": checked})
    bowl = runs[0]["spark"]["moments"][:2]
    if not runs[2]["spark"]["moments"] and bowl:
        injected = [dict(moment, reason="Injected for the proof: a bowl moment offered as 'a dog'.") for moment in bowl]
        checked = verify_moments(injected, "a dog", SOURCE, work, BUDGET, out_dir=thumbs)
        runs.append({"query": "a dog", "injected": True, "spark": {"model": None, "moments": injected}, "verified": checked})
    for run in runs:
        for moment in run["verified"]["moments"] + run["verified"]["dropped"]:
            moment["thumbnail"] = relative(moment.get("thumbnail"))
    log = meta.call_log(work, budget_usd=BUDGET)
    result = {"source": str(SOURCE.relative_to(ROOT)), "duration": round(duration, 3), "budget_usd": BUDGET,
              "models": {"find": runs[0]["spark"]["model"], "verify": "sam-3.1"}, "runs": runs, "calls": log["calls"], "totals": log["totals"]}
    (HERE / "results.json").write_text(json.dumps(result, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    sheet(runs)
    print(json.dumps(log["totals"]))


def sheet(runs: list[dict]) -> None:
    """Every thumbnail, labelled with its query, window, coverage and fate, in one image."""
    font = filter_value(resolve_font())
    tiles = HERE / "tiles"
    shutil.rmtree(tiles, ignore_errors=True)
    tiles.mkdir()
    count = 0
    for run in runs:
        verdict = run["verified"]
        for moment, fate in [(m, "kept") for m in verdict["moments"]] + [(m, "dropped") for m in verdict["dropped"]]:
            if not moment.get("thumbnail"):
                continue
            window = moment.get("evidence", {}).get("window", [moment["start"], moment["end"]])
            share = "n/a" if moment.get("coverage") is None else f"{moment['coverage']:.0%}"
            fate = "dropped" if fate == "dropped" else "verified" if moment.get("verified") else "flagged" if moment.get("flagged") else "kept"
            tag = " (injected)" if run.get("injected") else f" as '{run['phrase_override']}'" if run.get("phrase_override") else ""
            label = f"{run['query']}{tag} | {window[0]:.1f}-{window[1]:.1f}s | coverage {share} | {fate}"
            count += 1
            run_ffmpeg(["-loglevel", "error", "-i", str(HERE / moment["thumbnail"]), "-vf",
                        f"scale=640:360,pad=640:404:0:0:black,drawtext=fontfile={font}:expansion=none:text={filter_value(label)}:x=12:y=372:fontsize=19:fontcolor=white",
                        "-q:v", "3", str(tiles / f"tile-{count:02d}.jpg")])
    columns = 3
    rows = -(-count // columns)
    run_ffmpeg(["-loglevel", "error", "-framerate", "1", "-i", str(tiles / "tile-%02d.jpg"), "-vf", f"tile={columns}x{rows}:padding=6:color=0x222222",
                "-frames:v", "1", "-q:v", "3", str(HERE / "contact-sheet.jpg")])
    shutil.rmtree(tiles)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
