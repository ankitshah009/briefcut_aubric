"""Live proof of Briefcut's private mode: Muse Glimmer on this Mac through llama.cpp.

Run from the repository root, with the local server already started
(``uv run python -m vedit.open_weights serve --draft``):

    VEDIT_PRIVATE_MODEL=glimmer uv run python docs/evidence/open-weights/run_proof.py

It calls the real ``vedit.meta`` functions that Studio calls (``patch_plan``,
``translate_cues``, ``script_prompts``) on inputs taken from earlier evidence
and from a local whisper transcript of ``examples/demo/workshop.mp4``. The Meta
API is disabled for the whole run: ``meta._request`` raises, and no key is loaded.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
WORK = HERE / "work"

if os.environ.get("VEDIT_PRIVATE_MODEL", "").lower() != "glimmer":
    sys.exit("Set VEDIT_PRIVATE_MODEL=glimmer.")
if os.environ.get("MODEL_API_KEY"):
    sys.exit("Unset MODEL_API_KEY: this proof must run without any Meta key.")

from vedit import meta, open_weights  # noqa: E402
from vedit.lint import apply_patches  # noqa: E402


def forbidden(*args, **kwargs):
    raise AssertionError("private mode tried to call the Meta API")


meta._request = forbidden  # any Meta API call fails the proof

inputs = json.loads((HERE / "inputs.json").read_text(encoding="utf-8"))
if WORK.exists():
    shutil.rmtree(WORK)
WORK.mkdir(parents=True)

status = open_weights.status()
assert status["ready"] and status["gguf"]["sha256_matches_official"], status
outputs: dict = {
    "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    "machine": {"cpu": subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True).stdout.strip(),
                "memory_bytes": int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout.strip()),
                "os": f"{platform.system()} {platform.release()}"},
    "server": status, "runs": [],
}


def run(kind: str, call, **extra) -> dict:
    started = time.time()
    result = call()
    entry = {"kind": kind, "seconds": round(time.time() - started, 2), **extra, "result": result}
    outputs["runs"].append(entry)
    print(f"{kind}: {entry['seconds']} s", flush=True)
    return entry


for case in inputs["notes"]:
    plan = dict(case["plan"], source_duration=case["source_duration"])
    entry = run("patch_plan", lambda: meta.patch_plan(goal=case["goal"], note=case["note"], plan=plan, sentences=inputs["sentences"],
                                                      work_dir=WORK, budget_usd=0.25, language="en"),
                evidence=case["evidence"], note=case["note"], meta_api_applied_patches=case.get("meta_api_applied_patches"))
    new_plan, applied = apply_patches(plan, entry["result"]["patches"], duration=case["source_duration"])
    entry["applied"] = applied
    entry["segments_before"] = [[s["start"], s["end"], s.get("speed", 1.0)] for s in plan["segments"]]
    entry["segments_after"] = [[s["start"], s["end"], s.get("speed", 1.0)] for s in new_plan["segments"]]
    entry["edit_after"] = new_plan.get("edit")

for language in inputs["translate_to"]:
    run("translate_cues", lambda: meta.translate_cues(inputs["sentences"], language, work_dir=WORK, source_language="en"),
        language=language)

run("script_prompts", lambda: meta.script_prompts(inputs["script_scenes"], aspect="9:16", work_dir=WORK))

(HERE / "outputs.json").write_text(json.dumps(outputs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("wrote", HERE / "outputs.json")
