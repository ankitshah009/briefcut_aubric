from __future__ import annotations

from pathlib import Path

import pytest

from vedit.compile import compile_brief
from vedit.edit import Edit
from vedit.errors import CompileError
from vedit.ir import Brief
from vedit.media import probe, resolve_ffmpeg, run_ffmpeg


def _have_ffmpeg() -> bool:
    try:
        resolve_ffmpeg()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg required")


def _make_talk(path: Path, *, seconds: float = 2.0) -> Path:
    run_ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            f"color=c=navy:s=640x360:r=30:d={seconds}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate=48000:duration={seconds}",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(path),
        ]
    )
    return path


def _make_bed(path: Path, *, seconds: float = 2.0) -> Path:
    run_ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=220:sample_rate=48000:duration={seconds}",
            "-filter:a",
            "volume=-20dB",
            "-c:a",
            "pcm_s16le",
            str(path),
        ]
    )
    return path


def test_music_duck_export(tmp_path: Path) -> None:
    talk = _make_talk(tmp_path / "talk.mp4")
    bed = _make_bed(tmp_path / "bed.wav")
    finals = Edit(talk).work_dir(tmp_path / "work").music(bed, duck=True, volume_db=-20).export("web_preview").render()
    assert finals
    out = finals[0]
    assert out.is_file()
    info = probe(out)
    talk_info = probe(talk)
    assert info.has_video
    assert info.has_audio
    assert abs(info.duration - talk_info.duration) < 0.5


def test_missing_music_raises_compile_error(tmp_path: Path) -> None:
    talk = _make_talk(tmp_path / "talk.mp4")
    brief = Brief(
        name="ghost-music",
        sources={"main": str(talk), "music": str(tmp_path / "missing-bed.wav")},
        ops=[
            {"op": "music", "source": "music", "duck": True, "volume_db": -20},
            {"op": "export", "preset": "web_preview"},
        ],
        work_dir=str(tmp_path / "work"),
    )
    with pytest.raises(CompileError, match="missing source"):
        compile_brief(brief)
