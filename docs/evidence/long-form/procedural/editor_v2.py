"""The editor call again, on the same candidates, after its prompt was changed to forbid repeated beats.

The first live run (reproduce.py) rendered a story whose last three moments repeat beats it had already
shown: the procedural source loops the same 43 s, and the first prompt asked the editor to aim close to
the target. The prompt now says the length is a ceiling and two moments may not show or say the same beat.
This asks Meta once more, text only, on the same project ledger and its $0.60 budget, and writes editor-v2.json.

    export VEDIT_META_MODE=standard VEDIT_META_ENABLED=1
    uv run python docs/evidence/long-form/procedural/editor_v2.py WORK_DIR
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUDGET = 0.60


def main(work: Path) -> None:
    from vedit import meta

    campaigns = work.resolve() / "workspace" / "vedit-sessions" / "campaigns"
    plan = json.loads((HERE / "plan.json").read_text(encoding="utf-8"))
    summary = json.loads((HERE / "summary.json").read_text(encoding="utf-8"))
    [saved] = list(campaigns.glob("*/long-form/candidates-*.json"))  # only the story project watched the windows
    root = saved.parent.parent
    candidates = json.loads(saved.read_text(encoding="utf-8"))["candidates"]
    target = 60.0
    fitted = [dict(item, end=round(min(item["end"], item["start"] + target), 3)) for item in candidates]
    story = meta.edit_long_story(goal=GOAL,
                                 duration=summary["source"]["duration"], target_duration=target, candidates=fitted, work_dir=root / "meta",
                                 language="auto", budget_usd=BUDGET, transcript=plan["analysis"]["transcript"])
    calls = json.loads((root / "meta" / "meta-usage.json").read_text(encoding="utf-8"))["calls"]
    edits = [call for call in calls if call.get("operation") == "edit"]
    out = {"prompt": "v2: the length is a ceiling; no two moments may show or say the same beat",
           "picked": [segment["candidate"] for segment in story["segments"]],
           "segments": [[segment["start"], segment["end"], segment["label"]] for segment in story["segments"]],
           "duration": round(sum(segment["end"] - segment["start"] for segment in story["segments"]), 3),
           "title": story["title"], "model": story.get("model"), "usage": story.get("usage"), "warnings": story.get("warnings"),
           "ledger_edit_calls": edits, "project_held_usd": round(sum(meta.held_usd(call) for call in calls), 6)}
    (HERE / "editor-v2.json").write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({key: out[key] for key in ("picked", "duration", "project_held_usd")}))


GOAL = ("Cut a 60-second highlight of this long workshop recording: open on the strongest moment, "
        "show the making steps clearly, and end on the finished piece.")

if __name__ == "__main__":
    main(Path(sys.argv[1]))
