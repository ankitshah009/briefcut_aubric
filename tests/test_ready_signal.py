"""The Mac app's proof mode and the QA script wait for a status Studio really writes.

They waited for "Editor ready", which Studio stopped writing when it became Meta-only,
so both always timed out (found while launch-testing the DMG on 2026-11-04).
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STUDIO = (ROOT / "src/vedit/studio/static/studio.js").read_text()
WAITERS = {
    "src/vedit/macos/VeditApp.swift": (ROOT / "src/vedit/macos/VeditApp.swift").read_text(),
    "scripts/qa_studio.py": (ROOT / "scripts/qa_studio.py").read_text(),
}


def _written_statuses() -> set[str]:
    found: set[str] = set()
    for line in STUDIO.splitlines():
        if '$("connection-status").textContent =' in line:
            found.update(re.findall(r'"([^"]+)"', line.split("=", 1)[1]))
    return found


def test_studio_writes_the_loaded_states():
    assert {"Meta connected", "Setup needed"} <= _written_statuses()


def test_waiters_accept_only_states_studio_writes():
    written = _written_statuses()
    for name, text in WAITERS.items():
        for line in text.splitlines():
            if "connection-status" in line:
                assert "Editor ready" not in line, (name, line.strip())
        accepted = set(re.findall(r"\[([^\]]*Meta connected[^\]]*)\]", text))
        assert accepted, f"{name} no longer waits for a Studio status"
        for group in accepted:
            for status in re.findall(r"[\"']([^\"']+)[\"']", group):
                assert status in written, (name, status)
