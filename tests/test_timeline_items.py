from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "src" / "vedit" / "studio" / "static" / "timeline_items.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js to exercise the Studio timeline helper")
def test_end_aligned_callout_tracks_story_growth_and_shrink() -> None:
    page = (ROOT / "src" / "vedit" / "studio" / "static" / "index.html").read_text(encoding="utf-8")
    timeline = (ROOT / "src" / "vedit" / "studio" / "static" / "timeline.js").read_text(encoding="utf-8")
    assert page.index('src="timeline_items.js"') < page.index('src="timeline.js"')
    assert "BriefcutTimelineItems.anchorEndCallout(item, beforeTotal, total)" in timeline
    script = f"""
const {{ anchorEndCallout }} = require({str(HELPER)!r});
const card = {{ style: "callout", start: 7, duration: 3 }};
if (!anchorEndCallout(card, 10, 15) || card.start !== 12 || card.duration !== 3) process.exit(1);
if (!anchorEndCallout(card, 15, 8) || card.start !== 5 || card.duration !== 3) process.exit(2);
const short = {{ style: "callout", start: 7, duration: 3 }};
if (!anchorEndCallout(short, 10, 2) || short.start !== 0 || short.duration !== 2) process.exit(3);
const ordinary = {{ style: "title", start: 1, duration: 2 }};
if (anchorEndCallout(ordinary, 10, 15) || ordinary.start !== 1 || ordinary.duration !== 2) process.exit(4);
"""
    subprocess.run([shutil.which("node") or "node", "-e", script], cwd=ROOT, check=True)
