#!/bin/sh
# Starts Briefcut's MCP server for the Claude Code plugin.
#
# It finds uv, installs Briefcut's runtime packages into this plugin's own
# .venv (the mcp and sam extras, no development tools), then runs `vedit mcp`.
# The agent's working folder is kept, so projects land in
# $VEDIT_WORKSPACE when it is set, otherwise in ./vedit-sessions/ there.
# stdout is the MCP channel: every message here goes to stderr.
set -eu

root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)

uv_bin=$(command -v uv 2>/dev/null || true)
if [ -z "$uv_bin" ]; then
  # Apps started from the Dock often have a short PATH. Look where uv installs.
  for candidate in "${HOME:-}/.local/bin/uv" "${HOME:-}/.cargo/bin/uv" /opt/homebrew/bin/uv /usr/local/bin/uv; do
    if [ -x "$candidate" ]; then uv_bin=$candidate; break; fi
  done
fi
if [ -z "$uv_bin" ]; then
  echo "Briefcut plugin: uv was not found. Install it (brew install uv, or see https://docs.astral.sh/uv/getting-started/installation/), then reconnect briefcut in /mcp." >&2
  exit 127
fi

if ! "$uv_bin" sync --project "$root" --no-dev --extra mcp --extra sam --inexact --quiet; then
  echo "Briefcut plugin: could not install its Python packages into $root/.venv. The first start needs network access to download them. Fix the error above, then reconnect briefcut in /mcp." >&2
  exit 1
fi

exec "$uv_bin" run --project "$root" --no-sync vedit mcp
