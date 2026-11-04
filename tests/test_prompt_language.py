"""Meta prompts name a real language, never a bare code or "auto", and carry no demo subject.

Each call is stopped at the free token count, so nothing is billed and no
answer is needed: the test only reads the prompt Briefcut would send.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from vedit import meta, open_weights


class _Stop(Exception):
    pass


@pytest.fixture
def prompts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.setenv("MODEL_API_KEY", "test-secret-never-write-to-disk")
    monkeypatch.setenv("VEDIT_META_ENABLED", "1")
    monkeypatch.setattr(open_weights, "enabled", lambda: False)
    seen: list[str] = []

    def fake(method: str, path: str, *, payload: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        if path == "/files":
            return {"id": "file-test"}
        if method == "DELETE":
            return {}
        if path == "/responses/input_tokens" and payload is not None:
            seen.append(payload["input"][0]["content"][0]["text"])
            raise _Stop
        pytest.fail(f"unexpected Meta call {method} {path}")

    monkeypatch.setattr(meta, "_request", fake)
    return seen


@pytest.fixture
def clip(tmp_path: Path) -> Path:
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"media bytes; the upload is faked")
    return path


PLAN = {"duration": 7.0, "source_duration": 40.0,
        "segments": [{"label": "Opener", "start": 0.5, "end": 4.5}, {"label": "Reveal", "start": 20.0, "end": 23.0}]}


def _calls(clip: Path, work: Path, language: str) -> dict[str, Any]:
    return {
        "story": lambda: meta.plan_story(clip, goal="Haz un video corto", duration=40.0, target_duration=20.0, work_dir=work, language=language),
        "find": lambda: meta.find_moments(clip, query="el perro", duration=40.0, work_dir=work, language=language),
        "shorts": lambda: meta.plan_shorts(clip, goal="Tres cortos", duration=40.0, count=2, target_duration=10.0, work_dir=work, language=language),
        "script": lambda: meta.script_prompts(["Una mañana en el mercado."], aspect="9:16", work_dir=work, language=language),
        "patch": lambda: meta.patch_plan(goal="g", note="Más corto", plan=PLAN, sentences=None, work_dir=work, language=language),
    }


@pytest.mark.parametrize("operation", ["story", "find", "shorts", "script", "patch"])
def test_auto_language_never_reaches_a_prompt_as_a_word(operation: str, prompts: list[str], clip: Path, tmp_path: Path) -> None:
    with pytest.raises(_Stop):
        _calls(clip, tmp_path / "work", "auto")[operation]()
    prompt = prompts[-1]
    assert " in auto" not in prompt and "(auto)" not in prompt
    assert "written in" in prompt, "auto names where the language comes from"


@pytest.mark.parametrize("operation", ["story", "find", "shorts", "script", "patch"])
def test_a_language_code_reaches_the_prompt_as_its_name(operation: str, prompts: list[str], clip: Path, tmp_path: Path) -> None:
    with pytest.raises(_Stop):
        _calls(clip, tmp_path / "work", "pt-BR")[operation]()
    prompt = prompts[-1]
    assert "Portuguese (pt-BR)" in prompt and " in pt-BR" not in prompt


def test_find_prompt_names_no_demo_subject(prompts: list[str], clip: Path, tmp_path: Path) -> None:
    with pytest.raises(_Stop):
        meta.find_moments(clip, query="the dog", duration=40.0, work_dir=tmp_path / "work")
    lowered = prompts[-1].lower()
    for demo in ("bowl", "wooden", "lathe", "oak", "workshop", "gouge"):
        assert demo not in lowered


def test_write_language() -> None:
    assert meta.write_language("auto", "the brief's language") == "the brief's language"
    assert meta.write_language("", "the brief's language") == "the brief's language"
    assert meta.write_language("en", "x") == "English"
    assert meta.write_language("pt-BR", "x") == "Portuguese (pt-BR)"
    assert meta.write_language("xx", "x") == "xx"
