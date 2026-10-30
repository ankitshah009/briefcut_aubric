"""Exercise the shipped launcher, independent of the development checkout."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest

from vedit.packaging import stage_app


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("swiftc") is None,
    reason="the native launcher requires macOS and Swift",
)


@pytest.fixture(scope="module")
def native_app(tmp_path_factory):
    return stage_app(tmp_path_factory.mktemp("native-campaign") / "Vedit native test.app", package_root=ROOT)


def _launch(app: Path, cwd: Path, python: Path, *args: str, input_text=None):
    env = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME", "MODEL_API_KEY", "META_API_KEY"):
        env.pop(key, None)
    env["VEDIT_PYTHON"] = str(python)
    env["VEDIT_META_ENABLED"] = "0"
    env["VEDIT_WORKSPACE"] = str(cwd / "workspace")
    return subprocess.run(
        [str(app / "Contents" / "MacOS" / "vedit"), *args],
        input=input_text, text=True, capture_output=True, env=env,
        cwd=cwd, timeout=30, check=False,
    )


def test_native_launcher_uses_explicit_python_for_real_module_execution(native_app, tmp_path):
    marker = tmp_path / "python-invocations.txt"
    wrapper = tmp_path / "chosen python"
    wrapper.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$1\" >> {shlex.quote(str(marker))}\n"
        f"exec {shlex.quote(sys.executable)} \"$@\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    result = _launch(native_app, tmp_path, wrapper, "--help")
    assert result.returncode == 0, result.stderr
    assert "studio" in result.stdout and "package" in result.stdout
    assert "-m" in marker.read_text().splitlines(), "override was probed but not used to run vedit"


def test_native_launcher_rejects_missing_explicit_python_without_fallback(native_app, tmp_path):
    result = _launch(native_app, tmp_path, tmp_path / "missing-python", "--help")
    assert result.returncode != 0
    assert "Python" in result.stderr and "3.13" in result.stderr
    assert "usage:" not in result.stdout


def test_native_launcher_rejects_an_installed_unsupported_python(native_app, tmp_path):
    legacy = Path("/usr/bin/python3")
    if not legacy.is_file():
        pytest.skip("no legacy system Python to exercise")
    version = subprocess.run(
        [str(legacy), "-I", "-S", "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    if version.returncode or tuple(map(int, version.stdout.strip().split("."))) >= (3, 13):
        pytest.skip("system Python is already supported")
    result = _launch(native_app, tmp_path, legacy, "--help")
    assert result.returncode != 0
    assert "3.13" in result.stderr
    assert "usage:" not in result.stdout


def test_packaged_agent_runs_away_from_checkout_and_includes_campaign(native_app, tmp_path):
    packaged = native_app / "Contents" / "Resources" / "src" / "vedit"
    for relative in ("campaign.py", "meta.py", "studio/static/index.html", "studio/static/studio.js", "studio/static/studio.css"):
        assert (packaged / relative).is_file(), f"bundle is missing {relative}"
    result = _launch(
        native_app, tmp_path, Path(sys.executable), "tools",
        input_text=json.dumps({"tool": "specs"}) + "\n",
    )
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert response["ok"] is True
    names = {tool["name"] for tool in response["result"]}
    assert {"plan", "render", "critique"}.issubset(names)
