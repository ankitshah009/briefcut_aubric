#!/usr/bin/env zsh
# Baseline on real footage: plan, render, probe, read the receipts.
# Briefcut runs on Meta models only, so every clip is uploaded to Meta and spends
# Meta credits. Only list clips you may send to an API.
#
#   zsh scripts/real_baseline.sh <work-dir>
#
# BRIEFCUT_SAMPLES is the folder of public sample clips (default below; missing clips are skipped).
# Add clips by listing them in BRIEFCUT_CLIPS, one "name|path|goal|target" per line.
set -u
work=${1:?usage: real_baseline.sh <work-dir>}
repo=${0:A:h:h}
samples=${BRIEFCUT_SAMPLES:-$HOME/GitHub/verified-video-search/data/previews}
if [[ -z ${VEDIT_FFMPEG:-} && -x /opt/homebrew/opt/ffmpeg-full/bin/ffmpeg ]]; then
  export VEDIT_FFMPEG=/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg
fi
cd $repo

run() {
  local name=$1 clip=$2 goal=$3 target=$4
  [[ -f $clip ]] || { echo "=== $name: not found, skipped"; return; }
  local dir=$work/$name
  rm -rf $dir; mkdir -p $dir/ev
  echo "=== $name"
  uv run python scripts/demo_run.py --clip "$clip" --goal "$goal" --target $target --workspace $dir --evidence-dir $dir/ev 2>&1 | grep -E "Plan .*Render|failed" | tail -2
  uv run python - $dir/ev <<'PY'
import glob, json, sys
records = sorted(glob.glob(sys.argv[1] + "/[0-9]*T*Z.json"))  # demo_run stamps: YYYYMMDDTHHMMSSZ.json
if not records:
    print("NO RECORD")
    raise SystemExit
data = json.load(open(records[-1]))
result = data.get("result") or {}
proof = result.get("proof") or {}
print("checks:", result.get("passed"), "/", result.get("total"), [c["name"] for c in result.get("checks") or [] if not c["passed"]])
print("proof:", {key: proof.get(key) for key in ("duration", "width", "height", "has_audio")})
print("warnings:", result.get("warnings"))
PY
}

run wanrong "$samples/wanrong-walk.mp4" "Make a short vertical walking recap with the best moments." 10
run whitehouse "$samples/whitehouse-walk.mp4" "Make a 4-second clip of the people walking." 4
run flower "$samples/flower.mp4" "Make a short vertical clip of the flower." 5
for line in ${(f)${BRIEFCUT_CLIPS:-}}; do
  run ${(s:|:)line}
done
