#!/usr/bin/env bash
set -euo pipefail

# Run from the checkout so uploads, jobs, and models have one predictable home.
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"
python_bin="$project_dir/.venv/bin/python"
if [[ ! -x "$python_bin" ]]; then
  echo 'Create the environment first: python3.13 -m venv .venv && .venv/bin/pip install -e ".[dev]"' >&2
  exit 1
fi
if [[ -z "${VEDIT_WHISPER_MODEL:-}" && -s "$project_dir/.vedit-models/ggml-base.bin" ]]; then
  export VEDIT_WHISPER_MODEL="$project_dir/.vedit-models/ggml-base.bin"
fi
"$python_bin" -m vedit doctor
exec "$python_bin" -m vedit studio "$@"
