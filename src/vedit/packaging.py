"""Stage a double-clickable macOS app and a DMG."""

from __future__ import annotations

import platform
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import vedit

_README = (
    "Briefcut by Aubric, Inc. opens a video editor window.\n"
    "\n"
    "1. Drag Briefcut to Applications and open it.\n"
    "2. This build is not notarized. If macOS says it cannot verify the app, run this once,\n"
    "   then open it again:\n"
    "     xattr -dr com.apple.quarantine /Applications/Briefcut.app\n"
    "3. FFmpeg is not in the app. Briefcut shows the exact command when it opens:\n"
    "     brew install ffmpeg-full\n"
    "\n"
    "Apple Silicon only. Python comes with the app unless it was built with --no-python;\n"
    "then Python 3.13+ must be on the machine.\n"
    "App Sandbox is off so the window can launch that Python and ffmpeg-full.\n"
    "Minimum system version is macOS 15. Bundle id ai.aubric.briefcut.\n"
    "Projects are stored in ~/Library/Application Support/Vedit/vedit-sessions.\n"
    "VEDIT_WORKSPACE overrides the desktop project folder; VEDIT_PYTHON selects Python.\n"
    "Campaign downloads open a Save dialog and reveal the saved file in Finder.\n"
    "Run the binary inside Contents/MacOS with arguments to use the vedit CLI.\n"
)

# Parts of a CPython tree that a video editor never loads.
_PYTHON_LEAVES_OUT = re.compile(
    r"test|tests|idlelib|tkinter|turtledemo|ensurepip|pydoc_data|lib2to3|site-packages|include|share|pkgconfig|BUILD"
    r"|config-3\.13-.*|tcl\d.*|tk\d.*|itcl.*|thread\d.*|libtcl.*|libtk.*|_tkinter\..*\.so"
    r"|idle3.*|pip3?(\.\d+)?|pydoc3.*|python3(\.\d+)?-config"
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


def relocatable_python() -> Path | None:
    """A CPython 3.13 tree that still works after being copied, or None.

    uv's managed builds (python-build-standalone) find their library relative to
    themselves. Homebrew's and python.org's are tied to where they were installed.
    """
    candidates = [Path(sys.base_prefix)]
    uv = shutil.which("uv")
    if uv is not None:
        found = subprocess.run([uv, "python", "find", "--managed-python", "3.13"], capture_output=True, text=True, check=False)
        if found.returncode == 0 and found.stdout.strip():
            candidates.append(Path(found.stdout.strip()).parent.parent)
    for candidate in candidates:
        prefix = candidate.resolve()
        if "/uv/python/cpython-3.13" in prefix.as_posix() and (prefix / "bin" / "python3.13").is_file():
            return prefix
    return None


def _bundle_python(resources: Path, source: Path) -> None:
    """Copy a slimmed interpreter into the app, with PyYAML beside the package."""
    target = resources / "python"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target, symlinks=True, ignore=_ignore_python)
    vendored = resources / "src" / "yaml"
    if vendored.exists():
        shutil.rmtree(vendored)
    import yaml

    # The pure-Python package. PyYAML falls back to it without its C extension.
    shutil.copytree(Path(yaml.__file__).parent, vendored, ignore=shutil.ignore_patterns("__pycache__", "*.so"))


def _ignore_python(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if _PYTHON_LEAVES_OUT.fullmatch(name)}


def stage_app(
    dest: Path,
    *,
    package_root: Path | None = None,
    version: str | None = None,
    bundle_python: bool = False,
) -> Path:
    dest = Path(dest)
    if version is None:
        version = vedit.__version__

    # An installed package has no src/ folder; use wherever this module lives.
    pkg = Path(__file__).resolve().parent if package_root is None else Path(package_root) / "src" / "vedit"
    if not pkg.is_dir():
        raise FileNotFoundError(pkg)

    macos = dest / "Contents" / "MacOS"
    resources = dest / "Contents" / "Resources"
    macos.mkdir(parents=True, exist_ok=True)
    resources.mkdir(parents=True, exist_ok=True)

    _write_executable(macos / "vedit", pkg)
    (dest / "Contents" / "Info.plist").write_text(_info_plist(version), encoding="utf-8")
    (resources / "README.txt").write_text(_README, encoding="utf-8")

    bundled = resources / "src" / "vedit"
    if bundled.exists():
        shutil.rmtree(bundled)
    shutil.copytree(pkg, bundled, ignore=_ignore_packaged)
    if bundle_python:
        python = relocatable_python() if sys.platform == "darwin" else None
        if python is not None:
            _bundle_python(resources, python)
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


def _write_executable(launcher: Path, pkg: Path) -> None:
    swift = pkg / "macos" / "VeditApp.swift"
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
