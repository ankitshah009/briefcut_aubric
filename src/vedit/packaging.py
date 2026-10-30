"""Stage a double-clickable macOS app and a DMG."""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import vedit

_README = (
    "Briefcut by Aubric.ai opens a video editor window.\n"
    "Python 3.13+ and ffmpeg stay on the machine. The bundle does not vendor them.\n"
    "App Sandbox is off so the window can launch that Python and ffmpeg-full.\n"
    "Minimum system version is macOS 15. Bundle id ai.aubric.briefcut.\n"
    "Projects are stored in ~/Library/Application Support/Vedit/vedit-sessions.\n"
    "VEDIT_WORKSPACE overrides the desktop project folder; VEDIT_PYTHON selects Python.\n"
    "Campaign downloads open a Save dialog and reveal the saved file in Finder.\n"
    "Run the binary inside Contents/MacOS with arguments to use the vedit CLI.\n"
)

# Used only when this package is staged off macOS, where swiftc cannot build
# the window. The Mac product is the Swift binary. This script does not open
# a browser.
_FALLBACK_LAUNCHER = """\
#!/bin/sh
case "$0" in
  */*) _macos="${0%/*}" ;;
  *) _macos="." ;;
esac
RES="$(CDPATH= cd "$_macos/../Resources" && pwd)"
export PYTHONPATH="$RES/src${PYTHONPATH:+:$PYTHONPATH}"

if [ -x /opt/homebrew/opt/ffmpeg-full/bin/ffmpeg ]; then
  if [ -z "${VEDIT_FFMPEG:-}" ]; then
    export VEDIT_FFMPEG="/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg"
  fi
  if [ -z "${VEDIT_FFPROBE:-}" ]; then
    export VEDIT_FFPROBE="/opt/homebrew/opt/ffmpeg-full/bin/ffprobe"
  fi
fi

PY="${VEDIT_PYTHON:-}"
if [ -n "$PY" ]; then
  if ! "$PY" -I -S -c 'import sys; sys.exit(0 if sys.version_info >= (3,13) else 1)' >/dev/null 2>&1; then
    echo "Python 3.13+ is required. Check VEDIT_PYTHON." >&2
    exit 1
  fi
fi
for name in python3.14 python3.13 python3; do
  [ -n "$PY" ] && break
  if [ -x "/opt/homebrew/bin/$name" ]; then
    candidate="/opt/homebrew/bin/$name"
  elif command -v "$name" >/dev/null 2>&1; then
    candidate="$(command -v "$name")"
  else
    continue
  fi
  if "$candidate" -I -S -c 'import sys; sys.exit(0 if sys.version_info >= (3,13) else 1)' >/dev/null 2>&1; then
    PY="$candidate"
    break
  fi
done

if [ -z "$PY" ]; then
  echo "Python 3.13+ is required" >&2
  exit 1
fi

if [ "$#" -eq 0 ]; then
  workspace="${VEDIT_WORKSPACE:-$HOME/Library/Application Support/Vedit}"
  mkdir -p "$workspace" || exit 1
  cd "$workspace" || exit 1
  exec "$PY" -m vedit studio --host 127.0.0.1 --port 8765
fi
exec "$PY" -m vedit "$@"
"""

_PLIST = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>
  <string>Briefcut</string>
  <key>CFBundleDisplayName</key>
  <string>Briefcut</string>
  <key>CFBundleIdentifier</key>
  <string>ai.aubric.briefcut</string>
  <key>CFBundleExecutable</key>
  <string>vedit</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>@@VERSION@@</string>
  <key>CFBundleVersion</key>
  <string>@@VERSION@@</string>
  <key>LSMinimumSystemVersion</key>
  <string>15.0</string>
  <key>LSApplicationCategoryType</key>
  <string>public.app-category.video</string>
  <key>NSHighResolutionCapable</key>
  <true/>
  <key>NSAppTransportSecurity</key>
  <dict>
    <key>NSAllowsLocalNetworking</key>
    <true/>
  </dict>
</dict>
</plist>
"""


def stage_app(
    dest: Path,
    *,
    package_root: Path | None = None,
    version: str | None = None,
) -> Path:
    dest = Path(dest)
    if package_root is None:
        package_root = Path(__file__).resolve().parents[2]
    else:
        package_root = Path(package_root)
    if version is None:
        version = vedit.__version__

    pkg = package_root / "src" / "vedit"
    if not pkg.is_dir():
        raise FileNotFoundError(pkg)

    macos = dest / "Contents" / "MacOS"
    resources = dest / "Contents" / "Resources"
    macos.mkdir(parents=True, exist_ok=True)
    resources.mkdir(parents=True, exist_ok=True)

    _write_executable(macos / "vedit", package_root)
    (dest / "Contents" / "Info.plist").write_text(_info_plist(version), encoding="utf-8")
    (resources / "README.txt").write_text(_README, encoding="utf-8")

    bundled = resources / "src" / "vedit"
    if bundled.exists():
        shutil.rmtree(bundled)
    shutil.copytree(pkg, bundled, ignore=_ignore_packaged)
    if sys.platform == "darwin" and shutil.which("codesign"):
        subprocess.run(["codesign", "--force", "--sign", "-", str(dest)], check=True)
    return dest


def create_dmg(
    app_dir: Path,
    dest: Path,
    *,
    volname: str = "Briefcut",
    run: Callable[[list[str]], Any] | None = None,
) -> Path:
    app_dir = Path(app_dir)
    dest = Path(dest)
    staging = Path(tempfile.mkdtemp(prefix="vedit-dmg-"))
    try:
        shutil.copytree(app_dir, staging / app_dir.name, symlinks=True)
        (staging / "Applications").symlink_to("/Applications")
        (staging / "README.txt").write_text(_README, encoding="utf-8")
        dest.parent.mkdir(parents=True, exist_ok=True)
        argv = [
            "hdiutil",
            "create",
            "-volname",
            volname,
            "-srcfolder",
            str(staging),
            "-ov",
            "-format",
            "UDZO",
            str(dest),
        ]
        if run is None:
            subprocess.run(argv, check=True)
        else:
            run(argv)
            if not dest.exists():
                dest.write_bytes(b"")
        return dest
    finally:
        link = staging / "Applications"
        if link.is_symlink():
            link.unlink()
        shutil.rmtree(staging)


def _write_executable(launcher: Path, package_root: Path) -> None:
    swift = package_root / "src" / "vedit" / "macos" / "VeditApp.swift"
    if sys.platform == "darwin":
        compiler = shutil.which("swiftc")
        if compiler is None:
            raise RuntimeError("swiftc is required to stage the Mac app")
        if not swift.is_file():
            raise FileNotFoundError(swift)
        machine = platform.machine()
        module_cache = Path(tempfile.gettempdir()) / "vedit-swift-module-cache"
        module_cache.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                compiler,
                "-parse-as-library",
                "-O",
                "-module-cache-path",
                str(module_cache),
                "-target",
                f"{machine}-apple-macosx15.0",
                "-o",
                str(launcher),
                str(swift),
            ],
            check=True,
        )
        launcher.chmod(0o755)
        if shutil.which("codesign"):
            subprocess.run(
                ["codesign", "--force", "--sign", "-", str(launcher)],
                check=True,
            )
        return
    launcher.write_text(_FALLBACK_LAUNCHER, encoding="utf-8")
    launcher.chmod(0o755)


def _info_plist(version: str) -> str:
    return _PLIST.replace("@@VERSION@@", _xml_escape(version))


def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _ignore_packaged(_directory: str, names: list[str]) -> set[str]:
    return {
        name
        for name in names
        if name == "__pycache__" or name.endswith(".pyc") or "egg-info" in name
    }
