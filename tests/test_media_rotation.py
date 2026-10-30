from pathlib import Path

import pytest

from vedit.media import MediaInfo, StreamInfo, _rotation


@pytest.mark.parametrize("rotation,expected", [(0, (1920, 1080)), (90, (1080, 1920)), (180, (1920, 1080)), (270, (1080, 1920)), (-90, (1080, 1920))])
def test_display_dimensions_preserve_stored_dimensions(rotation, expected):
    video = StreamInfo(0, "video", "h264", width=1920, height=1080, rotation=rotation)
    info = MediaInfo(Path("phone.mov"), 10, "mov", 100, None, (video,))
    assert (info.display_width, info.display_height) == expected
    assert (info.width, info.height) == (1920, 1080)


def test_display_matrix_takes_precedence_over_legacy_rotation():
    assert _rotation({"tags": {"rotate": "180"}, "side_data_list": [{"rotation": -90}]}) == 270
    assert _rotation({"tags": {"rotate": "90"}}) == 90
    assert _rotation({"side_data_list": [{"rotation": float("nan")}], "tags": {"rotate": "90"}}) == 90
    assert _rotation({"tags": {"rotate": "unknown"}}) == 0
