"""Dev scripts work on any clip and any machine, not only the demo clip on the author's Mac."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import demo_run  # noqa: E402

DEMO_WORDS = ("bowl", "oak", "lathe", "workshop", "wood")


def test_demo_clip_keeps_its_own_brief() -> None:
    goal, note = demo_run.default_brief(demo_run.WORKSHOP, 25)
    assert goal.startswith("Make a 25-second") and "bowl" in goal and "bowl" in note


def test_any_other_clip_gets_a_brief_that_names_no_demo_subject(tmp_path: Path) -> None:
    goal, note = demo_run.default_brief(tmp_path / "my-holiday.mp4", 40)
    assert goal.startswith("Make a 40-second")
    for word in DEMO_WORDS:
        assert word not in goal.lower() and word not in note.lower()


def test_scripts_name_no_private_or_mac_only_path() -> None:
    baseline = (ROOT / "scripts" / "real_baseline.sh").read_text()
    assert "Downloads" not in baseline, "user clips in ~/Downloads must never be sent to an API"
    assert "--provider local" not in baseline, "demo_run is Meta-only and rejects --provider local"
    assert '"/2026' not in baseline, "evidence files are found by their stamp, not by the year"
    assert "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg\"\n" not in (ROOT / "scripts" / "make_music_beds.py").read_text()
    assert "google-chrome" in (ROOT / "scripts" / "shoot_studio.py").read_text()
