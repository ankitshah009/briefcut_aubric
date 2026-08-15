from __future__ import annotations

import json
from pathlib import Path

from vedit.otio import pipeline_to_otio, write_otio
from vedit.presets import encode_args, get_preset, video_encoder


def test_youtube_presets_are_broadcast_tagged():
    for name in ("youtube_1080p", "youtube_720p", "youtube_4k", "shorts_9x16", "master_aac"):
        extra = get_preset(name).extra
        assert "bt709" in extra
        assert "yuv420p" in extra
        assert "+faststart" in extra
        assert "high" in extra


def test_web_preview_never_uses_videotoolbox():
    assert video_encoder(True, preset_name="web_preview") == "libx264"
    assert video_encoder(None, preset_name="web_preview") == "libx264"
    args = encode_args(get_preset("web_preview"), hw=True)
    assert "h264_videotoolbox" not in args
    assert args[args.index("-c:v") + 1] == "libx264"


def test_software_hw_flag_is_libx264():
    args = encode_args(get_preset("youtube_1080p"), hw=False)
    assert args[args.index("-c:v") + 1] == "libx264"
    assert "-crf" in args


def test_otio_lite_from_meta(tmp_path: Path):
    dest = tmp_path / "timeline.otio.json"
    write_otio(
        dest=dest,
        name="demo",
        work_dir=str(tmp_path),
        steps=["cut_silence", "export_youtube_1080p"],
        meta={
            "canvas": {"width": 1920, "height": 1080, "fps": 30},
            "sources": {"main": "/tmp/talk.mp4"},
            "ops": [{"op": "cut_silence"}],
            "finals": ["/tmp/out.mp4"],
        },
    )
    data = json.loads(dest.read_text())
    assert data["OTIO_SCHEMA"] == "Timeline.1"
    assert data["name"] == "demo"
    tracks = data["tracks"]["children"]
    assert {t["kind"] for t in tracks} == {"Video", "Audio"}
    clip = tracks[0]["children"][0]
    assert clip["media_reference"]["target_url"] == "/tmp/talk.mp4"
    assert data["metadata"]["vedit"]["steps"] == ["cut_silence", "export_youtube_1080p"]


def test_otio_empty_sources_still_valid():
    doc = pipeline_to_otio(name="empty", work_dir="/tmp", steps=[], meta={})
    assert doc["tracks"]["children"][0]["children"][0]["OTIO_SCHEMA"] == "Clip.1"
