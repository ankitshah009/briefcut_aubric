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

# Optional local settings (never committed): MODEL_API_KEY, VEDIT_META_ENABLED, VEDIT_META_MODE, and the
# optional VEDIT_* lines. The same rule as the Python loader (vedit.settings.load_env_file): a variable already
# set in the environment wins over the file, and an empty value in the file never overrides anything.
# The file is read as data, never run, so a line in it cannot execute a command.
load_env_local() {
  local line name value
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line#"${line%%[![:space:]]*}"}"
    [[ -z "$line" || "$line" == \#* ]] && continue
    line="${line#export }"
    [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || continue
    name="${BASH_REMATCH[1]}"
    value="${BASH_REMATCH[2]}"
    if [[ "$value" =~ ^\"([^\"]*)\"[[:space:]]*(#.*)?$ || "$value" =~ ^\'([^\']*)\'[[:space:]]*(#.*)?$ ]]; then
      value="${BASH_REMATCH[1]}"
    else
      # A # after a space starts a comment, as in a shell. A # inside a word is part of the value.
      value="${value%%[[:space:]]#*}"
      value="${value%"${value##*[![:space:]]}"}"
    fi
    [[ -z "$value" || -n "${!name:-}" ]] && continue
    export "$name=$value"
  done < "$1"
  return 0
}
if [[ -f "$project_dir/.env.local" ]]; then
  load_env_local "$project_dir/.env.local"
fi

# Prefer the ffmpeg-full build: it has drawtext and subtitles. Checked after .env.local, so a VEDIT_FFMPEG there is kept.
if [[ -z "${VEDIT_FFMPEG:-}" && -x /opt/homebrew/opt/ffmpeg-full/bin/ffmpeg ]]; then
  export VEDIT_FFMPEG=/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg
fi
# A missing Meta key fails the doctor, but Studio still opens: its setup panel shows what to add.
"$python_bin" -m vedit doctor || echo "Something is missing (see above). Studio's setup panel shows how to fix it." >&2
exec "$python_bin" -m vedit studio "$@"
