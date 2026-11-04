"""Live proof for long sources: a ~15-minute procedural source, planned in windows, a 60 s story rendered, 3 shorts planned.

Run from the repository root (the key comes from the Keychain through Briefcut's settings):
    export VEDIT_META_MODE=standard VEDIT_FFMPEG=/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg
    uv run python docs/evidence/long-form/procedural/reproduce.py WORK_DIR

The source is examples/demo/workshop.mp4 and talk.mp4 joined 21 times (procedural clips only).
The story project's budget is $0.60 and the shorts' is $0.10 each, so Meta can never be charged more than that.
Writes plan.json, shorts.json, meta-calls.json, summary.json and contact-sheet.jpg here.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
CLIPS = [ROOT / "examples" / "demo" / "workshop.mp4", ROOT / "examples" / "demo" / "talk.mp4"]
REPEATS = 21
BUDGET = 0.60
SHORTS_BUDGET = 0.10
GOAL = ("Cut a 60-second highlight of this long workshop recording: open on the strongest moment, "
        "show the making steps clearly, and end on the finished piece.")


def build_source(work: Path, ffmpeg: str) -> Path:
    """Both clips at one size, rate and sound format, then joined without re-encoding."""
    parts = []
    for index, clip in enumerate(CLIPS):
        out = work / f"part-{index}.mp4"
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(clip),
                        "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,fps=30,format=yuv420p",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-profile:v", "high", "-g", "60",
                        "-c:a", "aac", "-ar", "48000", "-ac", "1", "-b:a", "96k", str(out)], check=True)
        parts.append(out)
    listing = work / "parts.txt"
    listing.write_text("".join(f"file '{part}'\n" for _ in range(REPEATS) for part in parts), encoding="utf-8")
    inbox = work / "workspace" / "vedit-sessions" / "source-long" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    source = inbox / "workshop-long.mp4"
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
                    "-c", "copy", "-movflags", "+faststart", str(source)], check=True)
    return source


def wait(manager, ident: str) -> dict:
    manager.process(ident)
    return manager.get(ident)


def sheet(source: Path, picks: list[tuple[str, float]], ffmpeg: str) -> Path:
    """One labelled frame from the middle of every picked moment, in one image."""
    from vedit.media import filter_value, resolve_font

    font = filter_value(resolve_font())
    tiles = HERE / "tiles"
    shutil.rmtree(tiles, ignore_errors=True)
    tiles.mkdir()
    for index, (label, at) in enumerate(picks):
        text = filter_value(label).replace(":", "\\:")
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{at:.3f}", "-i", str(source), "-frames:v", "1",
                        "-vf", f"scale=384:216,drawtext=fontfile={font}:text={text}:x=8:y=h-th-8:fontsize=18:fontcolor=white:box=1:boxcolor=black@0.6:boxborderw=4",
                        str(tiles / f"tile-{index:02d}.jpg")], check=True)
    columns = 4
    rows = -(-len(picks) // columns)
    out = HERE / "contact-sheet.jpg"
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-framerate", "1", "-i", str(tiles / "tile-%02d.jpg"),
                    "-vf", f"tile={columns}x{rows}:padding=4:color=white", "-frames:v", "1", str(out)], check=True)
    shutil.rmtree(tiles, ignore_errors=True)
    return out


def clock(seconds: float) -> str:
    whole = int(round(seconds))
    return f"{whole // 60}:{whole % 60:02d}"


def main(work: Path) -> None:
    os.environ.setdefault("VEDIT_META_ENABLED", "1")
    os.environ["VEDIT_META_MODE"] = "standard"
    from vedit import meta
    from vedit.campaign import CampaignManager
    from vedit.media import probe, resolve_ffmpeg

    work = work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    ffmpeg = str(resolve_ffmpeg())
    access = meta.capabilities()
    if not access["available"]:
        sys.exit(f"Meta is not available: {access.get('reason')}")
    source = build_source(work, ffmpeg)
    info = probe(source)
    print(f"source {info.duration:.1f}s {info.width}x{info.height} audio={info.has_audio}", flush=True)
    manager = CampaignManager(work / "workspace", start_worker=False)
    started = time.monotonic()
    try:
        job = manager.create({"paths": [str(source)], "goal": GOAL, "target_duration": 60, "aspect": "16:9", "captions": True,
                              "provider": "meta", "caption_provider": "meta", "budget_usd": BUDGET})
        planned = wait(manager, job["id"])
        plan_seconds = time.monotonic() - started
        if planned["status"] != "planned":
            sys.exit(f"plan failed: {planned.get('error')}")
        print(f"planned in {plan_seconds:.0f}s: {[(s['start'], s['end']) for s in planned['plan']['segments']]}", flush=True)
        manager.render(job["id"], {})
        rendered = wait(manager, job["id"])
        render_seconds = time.monotonic() - started - plan_seconds
        print(f"render {rendered['status']} in {render_seconds:.0f}s {rendered.get('error') or ''}", flush=True)
        shorts_started = time.monotonic()
        jobs = manager.shorts(job["id"], {"count": 3, "target_duration": 30, "aspect": "9:16", "auto_render": False, "budget_usd": SHORTS_BUDGET})
        shorts = [wait(manager, item["id"]) for item in jobs]
        shorts_seconds = time.monotonic() - shorts_started
        print(f"shorts {[item['status'] for item in shorts]} in {shorts_seconds:.0f}s {[item.get('error') for item in shorts]}", flush=True)
        wall = time.monotonic() - started
        root = manager.root / job["id"]
        candidates_file = sorted((root / "long-form").glob("candidates-*.json"))
        candidates = json.loads(candidates_file[0].read_text(encoding="utf-8")) if candidates_file else {}
        summary_file = root / "long-form" / "plan-summary.json"
        long_form = json.loads(summary_file.read_text(encoding="utf-8")) if summary_file.is_file() else {}
        logs = {"story": meta.call_log(root / "meta", budget_usd=BUDGET)}
        for index, item in enumerate(shorts, 1):
            logs[f"short-{index}"] = meta.call_log(manager.root / item["id"] / "meta", budget_usd=SHORTS_BUDGET)
        ledgers = {"story": json.loads((root / "meta" / "meta-usage.json").read_text(encoding="utf-8"))["calls"]}
        for index, item in enumerate(shorts, 1):
            path = manager.root / item["id"] / "meta" / "meta-usage.json"
            if path.is_file():
                ledgers[f"short-{index}"] = json.loads(path.read_text(encoding="utf-8"))["calls"]
        spend = {name: round(sum(meta.held_usd(call) for call in calls), 6) for name, calls in ledgers.items()}
        # The editor call: counted, billed and reserved input tokens, and its share of the input ceiling.
        raw_editor = [dict(project=name, **call) for name, calls in ledgers.items() for call in calls if call.get("operation") == "edit"]
        transcript = planned["plan"]["analysis"].get("transcript") or []
        words = sum(len(cue["text"].split()) for cue in transcript)
        result = rendered.get("result") or {}
        report_path = None
        try:
            report_path = manager.artifact(job["id"], "report")
        except Exception:
            pass
        report = json.loads(report_path.read_text(encoding="utf-8")) if report_path and report_path.is_file() else {}
        video = None
        try:
            video = manager.artifact(job["id"], "video")
        except Exception:
            pass
        probed = probe(video) if video and video.is_file() else None
        (HERE / "plan.json").write_text(json.dumps(planned["plan"], indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        (HERE / "shorts.json").write_text(json.dumps([{"status": item["status"], "error": item.get("error"), "plan": item.get("plan")} for item in shorts],
                                                      indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        (HERE / "candidates.json").write_text(json.dumps(candidates, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        (HERE / "meta-calls.json").write_text(json.dumps({"logs": logs, "ledgers": ledgers}, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        picks = [(f"story {index} {clock(seg['start'])}", (seg["start"] + seg["end"]) / 2) for index, seg in enumerate(planned["plan"]["segments"], 1)]
        for number, item in enumerate(shorts, 1):
            for seg in (item.get("plan") or {}).get("segments") or []:
                picks.append((f"short {number} {clock(seg['start'])}", (seg["start"] + seg["end"]) / 2))
        sheet(source, picks, ffmpeg)
        summary = {
            "source": {"made_from": [str(clip.relative_to(ROOT)) for clip in CLIPS], "repeats": REPEATS, "duration": round(info.duration, 3),
                       "width": info.width, "height": info.height, "has_audio": info.has_audio},
            "mode": "standard", "budget_usd": {"story": BUDGET, "each_short": SHORTS_BUDGET},
            "wall_seconds": {"plan": round(plan_seconds, 1), "render": round(render_seconds, 1), "shorts": round(shorts_seconds, 1), "total": round(wall, 1)},
            "windows": long_form.get("windows"), "candidates": long_form.get("candidates"), "picked": long_form.get("picked"),
            "editor": long_form.get("editor"), "editor_model": long_form.get("editor_model"), "editor_calls": raw_editor,
            "input_ceiling_tokens": meta._MAX_INPUT_TOKENS, "transcript_words": words, "transcript_lines": len(transcript),
            "story": {"status": rendered["status"], "error": rendered.get("error"), "duration": planned["plan"]["duration"],
                      "segments": [[seg["start"], seg["end"]] for seg in planned["plan"]["segments"]],
                      "render": {"duration": probed.duration if probed else None, "width": probed.width if probed else None, "height": probed.height if probed else None,
                                 "has_audio": probed.has_audio if probed else None, "has_video": probed.has_video if probed else None},
                      "checks_passed": result.get("passed"), "checks_total": result.get("total")},
            "shorts": [{"status": item["status"], "error": item.get("error"), "title": (item.get("plan") or {}).get("title"),
                        "duration": (item.get("plan") or {}).get("duration"),
                        "segments": [[seg["start"], seg["end"]] for seg in (item.get("plan") or {}).get("segments") or []]} for item in shorts],
            "spend_usd": spend, "total_spend_usd": round(sum(spend.values()), 6),
            "report_keys": sorted(report)[:40],
        }
        (HERE / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps({"spend": spend, "total": summary["total_spend_usd"], "wall": summary["wall_seconds"], "render": summary["story"]["render"]}), flush=True)
    finally:
        manager.close()


if __name__ == "__main__":
    main(Path(sys.argv[1]))
