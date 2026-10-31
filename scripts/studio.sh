#!/usr/bin/env bash
set -euo pipefail

# Run from the checkout so uploads, jobs, and models have one predictable home.
# uv owns the Python environment: it provisions CPython 3.13 and installs the package.
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

uv_bin="$(command -v uv || true)"
if [[ -z "$uv_bin" && -x "$HOME/.local/bin/uv" ]]; then
  uv_bin="$HOME/.local/bin/uv"
fi

python_bin="$project_dir/.venv/bin/python"
if [[ ! -x "$python_bin" ]]; then
  if [[ -z "$uv_bin" ]]; then
    echo 'uv is required. Install it (https://docs.astral.sh/uv/), then run:' >&2
    echo '  uv sync' >&2
    exit 1
  fi
  echo "Creating .venv with uv sync (CPython 3.13 from .python-version)..." >&2
  "$uv_bin" sync
fi

# Prefer the ffmpeg-full build: it has drawtext and subtitles.
if [[ -z "${VEDIT_FFMPEG:-}" && -x /opt/homebrew/opt/ffmpeg-full/bin/ffmpeg ]]; then
  export VEDIT_FFMPEG=/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg
fi

# Discover a Whisper model: the shared ~/.vedit/models/ first (usable across projects),
# then the checkout's .vedit-models/ (prefer base.en, then base, then any ggml).
if [[ -z "${VEDIT_WHISPER_MODEL:-}" ]]; then
  models_home="${VEDIT_MODELS_DIR:-$HOME/.vedit/models}"
  for candidate in \
    "$models_home/ggml-base.en.bin" \
    "$models_home/ggml-base.bin" \
    "$models_home"/ggml-*.bin \
    "$project_dir/.vedit-models/ggml-base.en.bin" \
    "$project_dir/.vedit-models/ggml-base.bin" \
    "$project_dir"/.vedit-models/ggml-*.bin; do
    if [[ -s "$candidate" ]]; then
      export VEDIT_WHISPER_MODEL="$candidate"
      break
    fi
  done
fi

# Optional local secrets (never committed): MODEL_API_KEY, VEDIT_META_ENABLED, VEDIT_META_MODE.
if [[ -f "$project_dir/.env.local" ]]; then
  set -a
  # shellcheck disable=SC1091
  . "$project_dir/.env.local"
  set +a
fi
"$python_bin" -m vedit doctor
exec "$python_bin" -m vedit studio "$@"
