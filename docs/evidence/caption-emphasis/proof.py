"""Live proof for punch-word captions: one Muse Spark call, then a real burn and a contact sheet.

Usage (from the repo, with the key loaded only inside the command):
    set -a; . ./.env.local; set +a; uv run python proof.py JOB_DIR OUT_DIR EVIDENCE_DIR
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from vedit.caption_styles import write_styled_ass
from vedit.captions import burn_filter, cue_key, read_srt, story_cues
from vedit.edit_spec import merge_edit, validate_edit
from vedit.emphasis import emphasis_lines, emphasis_prompt, emphasis_limit, suggest_emphasis
from vedit.media import probe, resolve_ffmpeg

def facts(info) -> dict:
    video = next(stream for stream in info.streams if stream.codec_type == "video")
    return {"duration": info.duration, "width": video.width, "height": video.height, "has_video": True,
            "has_audio": any(stream.codec_type == "audio" for stream in info.streams), "pix_fmt": video.pix_fmt}


GOAL = "The oak bowl: a short that shows a raw oak blank becoming a finished bowl on the lathe, for people who like handmade things."


def main(job: Path, out: Path, evidence: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    evidence.mkdir(parents=True, exist_ok=True)
    plan = json.loads((job / "plan.json").read_text(encoding="utf-8"))
    cues = story_cues(plan)
    burned = read_srt(job / "render-1" / "captions.srt")
    assert [(round(c.start, 2), c.text) for c in cues] == [(round(c.start, 2), c.text) for c in burned], "cues match the job's captions.srt"

    result = suggest_emphasis(cues, GOAL, "en", work_dir=out / "meta", budget_usd=0.05)
    ledger = json.loads((out / "meta" / "meta-usage.json").read_text(encoding="utf-8"))["calls"]
    lines = emphasis_lines(cues)

    edit = validate_edit(merge_edit(plan["edit"], {"captions": result["edit"]}), duration=86_400.0)
    canvas = facts(probe(job / "render-1" / "01_select_ranges.mp4"))
    width, height = canvas["width"], canvas["height"]
    sheets = {}
    for style in ("boxed", "karaoke"):
        settings = {**edit["captions"], "style": style}
        ass = write_styled_ass(cues, out / f"captions-{style}.ass", width=width, height=height, settings=settings)
        video = out / f"emphasis-{style}.mp4"
        run = subprocess.run([str(resolve_ffmpeg()), "-hide_banner", "-v", "error", "-y", "-i", str(job / "render-1" / "01_select_ranges.mp4"),
                              "-vf", burn_filter(ass), "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "copy", str(video)],
                             capture_output=True, text=True)
        assert run.returncode == 0 and not run.stderr.strip(), run.stderr[-800:]
        info = facts(probe(video))
        # Mid-word moments of the emphasised lines, up to three.
        picked = {item["cue_key"]: item for item in result["emphasis"]}
        moments = []
        for cue in cues:
            item = picked.get(cue_key(cue))
            if item is None:
                continue
            words = cue.words
            index = min(item["word_index"], len(words) - 1)
            spoken = words[index]
            # boxed: while the word is said; karaoke: just after, so the highlight is on the next word and the punch stays.
            at = (spoken.start + spoken.end) / 2 if style == "boxed" else min(cue.end - 0.05, spoken.end + 0.12)
            moments.append(round(at, 3))
        while len(moments) < 3:
            moments.append(round(cues[len(moments)].start + 0.6, 3))
        moments = moments[:3]
        frames = []
        for number, at in enumerate(moments):
            frame = out / f"{style}-{number}.png"
            subprocess.run([str(resolve_ffmpeg()), "-hide_banner", "-v", "error", "-y", "-ss", f"{at:.3f}", "-i", str(video), "-frames:v", "1", str(frame)], check=True)
            frames.append(frame)
        sheet = evidence / f"emphasis-{style}-sheet.jpg"
        inputs = [arg for frame in frames for arg in ("-i", str(frame))]
        graph = ";".join(f"[{i}:v]scale=480:-2[f{i}]" for i in range(3)) + ";" + "".join(f"[f{i}]" for i in range(3)) + "hstack=inputs=3[v]"
        subprocess.run([str(resolve_ffmpeg()), "-hide_banner", "-v", "error", "-y", *inputs, "-filter_complex", graph, "-map", "[v]", "-q:v", "3", str(sheet)], check=True)
        sheets[style] = {"sheet": sheet.name, "frames_at_seconds": moments, "burned_video": str(video),
                         "probe": info,
                         "dialogue_lines": sum(1 for line in ass.read_text(encoding="utf-8").splitlines() if line.startswith("Dialogue:"))}

    record = {
        "feature": "punch-word caption emphasis",
        "job": str(job),
        "base_video": "render-1/01_select_ranges.mp4 (the job's assembled cut before its captions were burned)",
        "goal": GOAL,
        "language": "en",
        "model": result["model"],
        "provider": result["provider"],
        "usage": result["usage"],
        "ledger": [{key: call.get(key) for key in ("operation", "model", "status", "reserved_usd", "budget_usd", "request_id")} | {"cost_usd": (call.get("usage") or {}).get("cost_usd")}
                   for call in ledger],
        "limit": {"lines": result["lines"], "max_emphasised_lines": result["limit"], "share": 0.4, "per_line": 1},
        "cues": [{"key": line["key"], "text": line["text"]} for line in lines],
        "emphasis": result["emphasis"],
        "edit_captions_patch": result["edit"],
        "warnings": result["warnings"],
        "prompt": emphasis_prompt(lines, GOAL, "en", limit=emphasis_limit(len(lines))),
        "renders": sheets,
    }
    (evidence / "emphasis-live.json").write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({key: record[key] for key in ("model", "usage", "emphasis", "warnings", "renders")}, indent=2))


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
