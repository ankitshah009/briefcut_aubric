"""Studio's offline fallbacks must match the limits the server advertises.

The page reads limits from /api/capabilities. When that call fails it falls
back to constants in the JavaScript, and those drifted once (200 MB shown while
the server took 4 GB). These tests pin the fallbacks to the Python source.
"""

from __future__ import annotations

import re
from pathlib import Path

from vedit import lint, project_media

STATIC = Path(__file__).resolve().parents[1] / "src" / "vedit" / "studio" / "static"


def _fallback_limits() -> dict[str, int]:
    source = (STATIC / "mediabin.js").read_text(encoding="utf-8")
    match = re.search(r"const FALLBACK_LIMITS = \{([^}]*)\};", source)
    assert match, "mediabin.js no longer defines FALLBACK_LIMITS"
    return {key: int(value) for key, value in re.findall(r"(\w+):\s*(\d+)", match.group(1))}


def test_studio_upload_fallback_matches_the_server() -> None:
    source = (STATIC / "studio.js").read_text(encoding="utf-8")
    match = re.search(r"const maxClipBytes = (\d+);", source)
    assert match, "studio.js no longer defines maxClipBytes"
    assert int(match.group(1)) == project_media.MAX_SOURCE_BYTES


def test_mediabin_fallback_limits_match_the_server() -> None:
    assert _fallback_limits() == {
        "upload_bytes": project_media.MAX_SOURCE_BYTES,
        "source_seconds": project_media.MAX_SOURCE_SECONDS,
        "segments": lint.MAX_MOMENTS,
        "clips": project_media.MAX_ITEMS,
    }
