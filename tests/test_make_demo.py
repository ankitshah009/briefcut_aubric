"""Fast checks on the procedural workshop textures in scripts/make_demo.py.

The full ``write_workshop`` render needs macOS ``say`` and ~25 s, so these
tests only render the stills every scene is assembled from and check that the
alpha-carrying ones are actually opaque where they should be. ffmpeg's
``random(idx)`` keeps its state in expression variable ``idx``; a texture
whose prelude used slot 0 for geometry once rendered fully transparent.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import make_demo  # noqa: E402

from vedit.media import resolve_ffmpeg  # noqa: E402


def _have_ffmpeg() -> bool:
    try:
        resolve_ffmpeg()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg required")


def _plane(path: Path, pix_fmt: str, stride: int, offset: int) -> bytes:
    raw = subprocess.run(
        [
            resolve_ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-f",
            "rawvideo",
            "-pix_fmt",
            pix_fmt,
            "-frames:v",
            "1",
            "-",
        ],
        capture_output=True,
        check=True,
    ).stdout
    return raw[offset::stride]


@pytest.fixture(scope="module")
def textures(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    return make_demo._write_textures(tmp_path_factory.mktemp("workshop-tex"))


def test_scene_table_has_no_baked_titles() -> None:
    assert len(make_demo.WORKSHOP_SCENES) == 5
    for label, line in make_demo.WORKSHOP_SCENES:
        assert label and "[[slnc 1000]]" in line
    # Nothing in the picture pipeline draws text.
    import inspect

    for fn in (make_demo._bowl_scene, make_demo._blank_scene, make_demo._write_textures):
        assert "drawtext" not in inspect.getsource(fn)


@pytest.mark.parametrize("name", ["disc", "blank", "shade_dark", "shade_light", "shadow", "tool", "band"])
def test_alpha_textures_are_not_blank(textures: dict[str, Path], name: str) -> None:
    alpha = _plane(textures[name], "rgba", 4, 3)
    # Shade, shadow and gloss layers are deliberately soft (peak alpha well
    # under 255), so only require a clearly opaque and a clearly clear region.
    assert max(alpha) >= 80, f"{name}.png has no opaque pixels"
    assert min(alpha) <= 8, f"{name}.png has no transparent pixels"


def test_bowl_mask_is_a_filled_ellipse(textures: dict[str, Path]) -> None:
    lum = _plane(textures["mask"], "gray", 1, 0)
    w, h = make_demo.BOWL_W, make_demo.BOWL_H
    assert lum[(h // 2) * w + w // 2] == 255
    assert lum[0] == 0 and lum[-1] == 0
    # An ellipse covers ~pi/4 of its bounding box.
    coverage = sum(1 for v in lum if v > 128) / len(lum)
    assert 0.7 < coverage < 0.85


def test_blank_has_bark_edge_and_lighter_core(textures: dict[str, Path]) -> None:
    rgba = subprocess.run(
        [
            resolve_ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(textures["blank"]),
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgba",
            "-frames:v",
            "1",
            "-",
        ],
        capture_output=True,
        check=True,
    ).stdout
    w = 880

    def red(x: int, y: int) -> int:
        return rgba[(y * w + x) * 4]

    core = sum(red(440 + dx, 280) for dx in range(-40, 40, 8)) / 10
    edge = sum(red(20 + dx, 280) for dx in range(0, 24, 4)) / 6
    assert core > edge + 30, (core, edge)
