#!/usr/bin/env bash
# Briefcut installer for macOS and Linux. Read it before you run it.
#
# In order, it will:
#   1. Look at what is already here and print a plan. Nothing changes yet.
#   2. Ask before each install:
#        uv        Homebrew on a Mac when present, otherwise astral.sh's installer. No sudo.
#        FFmpeg    Homebrew's ffmpeg-full on a Mac, apt on Debian and Ubuntu (sudo).
#        Briefcut  `uv tool install`, under your home folder. No sudo.
#                  It includes the MCP server for coding agents (`briefcut mcp`).
#   3. Print the one command that opens Briefcut, the one that connects an agent,
#      and where to get the Meta API key Briefcut needs. It downloads no model:
#      every model Briefcut calls is a Meta model, reached with that key.
#
# It does not edit your shell profile and sends nothing anywhere. On a Mac without
# Homebrew, or on Linux without apt, it says what to install and leaves that to you.
#
#   bash scripts/install.sh --dry-run   show the plan and the exact commands; change nothing
#   bash scripts/install.sh             ask before each step
#   bash scripts/install.sh --yes       accept every step
set -euo pipefail

repository="https://github.com/ankitshah009/briefcut_aubric"
dry_run=0
assume_yes=0

usage() { sed -n '2,20p' "${BASH_SOURCE[0]:-$0}" | sed 's/^# \{0,1\}//'; }

for argument in "$@"; do
  case "$argument" in
    -n|--dry-run) dry_run=1 ;;
    -y|--yes) assume_yes=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $argument" >&2; usage >&2; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }

# Show every command before it runs, in a form you could paste: an argument
# with spaces or brackets, such as vedit[mcp,sam] @ PATH, is single-quoted.
shown() {
  local out="" arg quote="'\\''"
  for arg in "$@"; do
    if [[ $arg =~ ^[A-Za-z0-9_./:=@+,-]+$ ]]; then
      out+=" $arg"
    else
      out+=" '${arg//\'/$quote}'"
    fi
  done
  printf '%s' "${out# }"
}

run() {
  printf '   $ %s\n' "$(shown "$@")"
  if [[ $dry_run -eq 0 ]]; then "$@"; fi
}

confirm() {
  [[ $dry_run -eq 1 || $assume_yes -eq 1 ]] && return 0
  local reply=""
  if [[ -t 0 ]]; then
    read -r -p "   $1 [y/N] " reply
  elif (exec </dev/tty) 2>/dev/null; then
    read -r -p "   $1 [y/N] " reply </dev/tty
  else
    say "   Skipped: nobody here to answer. Run with --yes to accept."
    return 1
  fi
  [[ $reply == [Yy]* ]]
}

case "$(uname -s)" in
  Darwin) system="macOS" ;;
  Linux) system="Linux" ;;
  *) say "Briefcut supports macOS and Linux. This is $(uname -s)." >&2; exit 1 ;;
esac

brew_bin=""
for candidate in "$(command -v brew || true)" /opt/homebrew/bin/brew /usr/local/bin/brew; do
  if [[ -n "$candidate" && -x "$candidate" ]]; then brew_bin="$candidate"; break; fi
done
apt_bin="$(command -v apt-get || true)"

uv_bin="$(command -v uv || true)"
if [[ -z "$uv_bin" && -x "$HOME/.local/bin/uv" ]]; then uv_bin="$HOME/.local/bin/uv"; fi

# An FFmpeg counts only with the filters that draw titles and burn captions.
good_ffmpeg() {
  local tool filters
  for tool in "${VEDIT_FFMPEG:-}" /opt/homebrew/opt/ffmpeg-full/bin/ffmpeg /usr/local/opt/ffmpeg-full/bin/ffmpeg "$(command -v ffmpeg || true)"; do
    [[ -n "$tool" && -x "$tool" ]] || continue
    filters="$("$tool" -hide_banner -filters 2>/dev/null)" || continue
    if grep -q ' drawtext ' <<<"$filters" && grep -q ' subtitles ' <<<"$filters"; then return 0; fi
  done
  return 1
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || pwd)"
if [[ -f "$script_dir/../pyproject.toml" ]]; then
  source_spec="$(cd "$script_dir/.." && pwd)"
else
  source_spec="git+$repository"
fi
# The mcp extra makes `briefcut mcp` work for coding agents; sam reads Meta's SAM cutouts.
requirement="vedit[mcp,sam] @ $source_spec"

step "What is here"
say "   System:   $system"
if [[ -n "$brew_bin" ]]; then packages="Homebrew"; elif [[ -n "$apt_bin" ]]; then packages="apt"; else packages="none found"; fi
say "   Packages: $packages"
say "   uv:       ${uv_bin:-not installed}"
if good_ffmpeg; then have_ffmpeg=1; say "   FFmpeg:   ready, with titles and captions"; else have_ffmpeg=0; say "   FFmpeg:   not installed, or missing titles and captions"; fi
say "   Briefcut: from $source_spec"
if [[ $dry_run -eq 1 ]]; then say "   Dry run: nothing below will change."; fi

if [[ -z "$uv_bin" ]]; then
  step "Install uv, which runs Briefcut and provides Python 3.13"
  if [[ -n "$brew_bin" ]]; then
    if confirm "Install uv with Homebrew?"; then run "$brew_bin" install uv; fi
  else
    say "   Downloads astral.sh's installer, then runs it. It writes to ~/.local/bin and leaves your shell profile alone."
    if confirm "Install uv from astral.sh?"; then
      installer="${TMPDIR:-/tmp}/uv-install-$$.sh"
      if command -v curl >/dev/null; then
        run curl -fsSL https://astral.sh/uv/install.sh -o "$installer"
      elif command -v wget >/dev/null; then
        run wget -qO "$installer" https://astral.sh/uv/install.sh
      else
        say "   This step needs curl or wget. Install one, then run this script again." >&2
        exit 1
      fi
      run env UV_NO_MODIFY_PATH=1 sh "$installer"
      run rm -f "$installer"
    fi
  fi
  uv_bin="$(command -v uv || true)"
  for candidate in "$HOME/.local/bin/uv" "${brew_bin%/brew}/uv"; do
    if [[ -z "$uv_bin" && -x "$candidate" ]]; then uv_bin="$candidate"; fi
  done
fi

if [[ $have_ffmpeg -eq 0 ]]; then
  step "Install FFmpeg, which reads your footage and renders the video"
  if [[ $system == "macOS" && -n "$brew_bin" ]]; then
    say "   Homebrew's ffmpeg-full has titles and captions. It is a large install with many dependencies."
    if confirm "Install ffmpeg-full with Homebrew?"; then run "$brew_bin" install ffmpeg-full; fi
  elif [[ $system == "macOS" ]]; then
    say "   FFmpeg needs Homebrew, which this script does not install for you."
    say "   Install Homebrew from https://brew.sh, then run: brew install ffmpeg-full"
  elif [[ -n "$apt_bin" ]]; then
    elevate=()
    if [[ "$(id -u)" -ne 0 ]]; then elevate=(sudo); fi
    say "   apt installs ffmpeg and fonts for titles and for captions in Japanese, Chinese, Korean, Hindi and other scripts. It asks for your password."
    if confirm "Install ffmpeg with apt?"; then
      run "${elevate[@]}" "$apt_bin" update
      run "${elevate[@]}" "$apt_bin" install -y ffmpeg fonts-dejavu-core fonts-noto-core fonts-noto-cjk
    fi
  else
    say "   No apt here. Install FFmpeg with your package manager. It needs libx264, drawtext, and subtitles."
  fi
fi

step "Install Briefcut"
installed=0
if [[ -z "$uv_bin" && $dry_run -eq 0 ]]; then
  say "   uv is not installed, so Briefcut cannot be installed yet. Run this script again after installing uv."
else
  uv_shown="${uv_bin:-uv}"
  if confirm "Install the briefcut command with uv?"; then
    run "$uv_shown" tool install --force --reinstall --python 3.13 "$requirement"
    installed=1
  fi
fi

step "Next"
if [[ $installed -eq 1 ]]; then
  bin_dir="$HOME/.local/bin"
  if [[ -n "$uv_bin" && $dry_run -eq 0 ]]; then bin_dir="$("$uv_bin" tool dir --bin)"; fi
  case ":$PATH:" in
    *":$bin_dir:"*) say "   Open Briefcut:"; say "     briefcut" ;;
    *) say "   Open Briefcut:"; say "     $bin_dir/briefcut"
       say "   To type just \`briefcut\`, add this to your shell profile:"
       say "     export PATH=\"$bin_dir:\$PATH\"" ;;
  esac
  say "   Connect Claude Code (projects go in the folder Claude starts in, or in VEDIT_WORKSPACE):"
  say "     claude mcp add --scope user briefcut -- \"$bin_dir/briefcut\" mcp"
fi
say "   Add your Meta API key. Briefcut runs on Meta models and needs one to make a video."
say "     1. Create a key at https://dev.meta.ai"
say "     2. Open Briefcut, then Settings, and paste it. Or set MODEL_API_KEY and VEDIT_META_ENABLED=1."
