"""Bugs found in the live end-to-end run of 2026-11-04 (docs/evidence/live-integration/README.md).

Each test failed before its fix. No test calls Meta: every answer is stubbed at
the engine's own seam.

- A. The hero word asked SAM for a guessed "the speaker" when no subject was given.
- B. The motion card was drawn over the captions, so speech went unreadable.
- C. Translated captions left a short orphan page, dropped punch words without a
  word, and the motion card was titled in the spoken language.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from vedit import blender_cards, emphasis, hero_word, meta, render_plan
from vedit.campaign import CampaignError, CampaignManager
from vedit.caption_styles import LOOKS, render_ass
from vedit.captions import Cue, write_caption_files
from vedit.compile import compile_brief
from vedit.edit_spec import default_edit, merge_edit, validate_edit
from vedit.ir import Brief, Canvas
from vedit.media import probe, resolve_ffmpeg
from vedit.pipeline import execute
from vedit.render_plan import build_ops, context

ROOT = Path(__file__).resolve().parents[1]


def _wiring() -> Any:
    """The manager fixtures from ``test_feature_wiring.py``, loaded once rather than copied."""
    spec = importlib.util.spec_from_file_location("live_bugs_wiring", Path(__file__).with_name("test_feature_wiring.py"))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wiring = _wiring()
offline = wiring.offline
enabled = wiring.enabled
clip = wiring.clip
manager = wiring.manager
planned = wiring.planned


def _with_captions(manager: CampaignManager, ident: str, **captions: Any) -> None:
    """Set ``edit.captions`` fields on the saved plan, as an approved edit would."""
    job = manager.get(ident)
    edit = dict(job["plan"]["edit"], captions=dict(job["plan"]["edit"]["captions"], **captions))
    manager._update(ident, plan=dict(job["plan"], edit=edit))


# --- A. The hero word never sends SAM a guessed subject -------------------------------


def test_a_hero_word_with_no_subject_is_refused_before_any_meta_call(manager: CampaignManager, clip: Path, enabled: None,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hero_word, "suggest_hero_word", lambda *a, **k: pytest.fail("no subject was named, so nothing may be asked"))
    ident = planned(manager, clip, provider="meta", budget_usd=0.3)
    assert not manager.get(ident)["request"].get("subject")
    for subject in (None, "", "   "):
        with pytest.raises(CampaignError) as refused:
            manager.suggest_hero_word(ident, subject=subject)
        assert refused.value.status == 400
        assert str(refused.value) == "Name what the word sits behind, for example the person in the red jacket."
    assert not (manager.root / ident / "meta" / "meta-calls.jsonl").exists()


def test_a_hero_word_uses_the_given_subject_then_the_projects(manager: CampaignManager, clip: Path, enabled: None,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def suggest(plan: dict[str, Any], transcript: Any = None, **kwargs: Any) -> dict[str, Any]:
        seen.append(kwargs["subject"])
        return {"word": "oak", "start": 0.2, "end": 1.0, "side": "center", "reason": "", "spoken": [0.26, 0.5], "proposal": {}, "adjusted": [],
                "usage": {}, "model": "muse-spark-1.3"}

    monkeypatch.setattr(hero_word, "suggest_hero_word", suggest)
    ident = planned(manager, clip, provider="meta", budget_usd=0.3, subject="the wooden bowl")
    assert manager.suggest_hero_word(ident)["hero_word"]["subject"] == "the wooden bowl"
    assert manager.suggest_hero_word(ident, subject="the lathe")["hero_word"]["subject"] == "the lathe"
    assert seen == ["the wooden bowl", "the lathe"]
    assert "speaker" not in " ".join(seen)


def test_the_mcp_hero_word_tool_says_a_subject_is_needed() -> None:
    source = (ROOT / "src" / "vedit" / "mcp_server.py").read_text(encoding="utf-8")
    start = source.index("def briefcut_hero_word(")
    doc = source[start:source.index('"""', source.index('"""', start) + 3)]
    assert "the speaker" not in doc
    assert "needed when the project has none" in " ".join(doc.split())


# --- B. Captions stay readable over the motion card -----------------------------------


CARD = {"kind": "intro_title", "text": "From Log to Bowl", "subtitle": "", "style": "warm_wood", "palette": "oak", "camera": "dolly_in", "duration": 1.5}


def test_captions_are_drawn_after_the_motion_card(tmp_path: Path) -> None:
    names = [stage.__name__ for stage in render_plan.STAGES]
    assert names.index("overlays") < names.index("motion_card") < names.index("captions") < names.index("fades")
    plan = {"title": "Bowl", "segments": [{"start": 0.0, "end": 4.0}], "analysis": {},
            "edit": validate_edit(merge_edit(default_edit(), {"motion_card": CARD}), duration=4.0, assets=None)}
    ctx = context(plan, request={"language": "en"}, aspect="16:9", canvas=Canvas(width=320, height=180), work=tmp_path,
                  has_audio=True, subtitle=tmp_path / "c.ass")
    ops = [op["op"] for op in build_ops(ctx)]
    assert ops.index("motion_card") < ops.index("captions")


def _ffmpeg(*args: str) -> None:
    subprocess.run([resolve_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


@pytest.mark.render
def test_burned_captions_show_through_the_motion_card_window(tmp_path: Path) -> None:
    """The card here is an opaque red plate over the whole frame, like a Muse Image backdrop."""
    source = tmp_path / "clip.mp4"
    _ffmpeg("-f", "lavfi", "-i", "color=c=0x203040:s=320x180:r=30:d=2", "-f", "lavfi", "-i", "sine=frequency=330:duration=2",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source))
    card = tmp_path / "card.mov"
    _ffmpeg("-f", "lavfi", "-i", "color=c=red:s=320x180:r=30:d=1,format=rgba", "-vf", "format=yuva444p10le",
            "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le", str(card))
    ass = tmp_path / "captions.ass"
    ass.write_text(render_ass([Cue(0.0, 2.0, "Wide white words")], width=320, height=180, settings={"style": "boxed"}), encoding="utf-8")
    plan = {"title": "Bowl", "segments": [{"start": 0.0, "end": 2.0}], "analysis": {},
            "edit": validate_edit(merge_edit(default_edit(), {"motion_card": CARD}), duration=2.0, assets=None)}
    ctx = context(plan, request={"language": "en"}, aspect="16:9", canvas=Canvas(width=320, height=180, fps=30), work=tmp_path,
                  has_audio=True, subtitle=ass)
    # The real stage order; only the card is swapped for a file so no Blender is needed.
    layered = [op if op["op"] != "motion_card" else {"op": "motion_card", "file": str(card), "start": 0.5}
               for op in build_ops(ctx) if op["op"] in {"captions", "motion_card"}]
    assert [op["op"] for op in layered] == ["motion_card", "captions"]
    out = tmp_path / "out.mp4"
    brief = Brief(name="card", sources={"footage": str(source)}, canvas=Canvas(width=320, height=180, fps=30), hw=False, work_dir=str(tmp_path / "render"),
                  ops=[{"op": "select_ranges", "ranges": [{"start": 0.0, "end": 2.0}]}, *layered, {"op": "export", "preset": "web_preview", "path": str(out)}])
    reports = execute(compile_brief(brief).pipeline)
    assert all(report.ok for report in reports), [report.stderr_tail for report in reports if not report.ok]
    info = probe(out)
    assert info.has_video and info.has_audio and abs(info.duration - 2.0) < 0.06

    def pixels(seconds: float, crop: str) -> bytes:
        return subprocess.run([resolve_ffmpeg(), "-hide_banner", "-loglevel", "error", "-ss", f"{seconds}", "-i", str(out), "-frames:v", "1",
                               "-vf", f"crop={crop},format=rgb24", "-f", "rawvideo", "-"], check=True, capture_output=True).stdout

    def white(raw: bytes) -> int:
        return sum(1 for index in range(0, len(raw), 3) if min(raw[index], raw[index + 1], raw[index + 2]) > 180)

    top = pixels(1.0, "iw:ih/5:0:0")
    assert sum(top[0::3]) // (len(top) // 3) > 200 and sum(top[1::3]) // (len(top) // 3) < 60, "the card's red plate covers the frame"
    assert white(pixels(0.2, "iw:ih/2:0:ih/2")) > 40, "the captions before the card"
    assert white(pixels(1.0, "iw:ih/2:0:ih/2")) > 40, "the captions stay readable inside the card's window"


# --- C. Translated captions --------------------------------------------------------------


SPANISH = Cue(0.027, 4.507, "Este es el cuenco de roble terminado, aceitado y listo para la mesa.")
FRENCH = Cue(10.127, 13.802, "Le ponçage passe du grossier au fin avec le tour qui tourne lentement.")


def _pages(cue: Cue, style: str = "boxed") -> list[tuple[float, float, list[str]]]:
    """What ``render_ass`` burns for one cue on the 9:16 canvas of render-7: (start, end, lines) per page."""
    def seconds(stamp: str) -> float:
        hours, minutes, rest = stamp.split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(rest)

    pages = []
    for line in render_ass([cue], width=1080, height=1920, settings={"style": style}).splitlines():
        if line.startswith("Dialogue:"):
            fields = line.split(",", 9)
            pages.append((seconds(fields[1]), seconds(fields[2]), re.sub(r"\{[^}]*\}", "", fields[9]).split(r"\N")))
    return pages


@pytest.mark.parametrize("cue", [SPANISH, FRENCH], ids=["es", "fr"])
def test_a_translated_line_leaves_no_short_orphan_page(cue: Cue) -> None:
    pages = _pages(cue)
    texts = [" ".join(lines) for _start, _end, lines in pages]
    assert " ".join(texts) == cue.text, "no word is lost or reordered"
    assert all(len(lines) <= LOOKS["boxed"].lines for _start, _end, lines in pages), texts
    assert all(len(text.split()) > 3 for text in texts), f"orphan page in {texts}"
    assert abs(pages[0][0] - cue.start) < 0.01 and abs(pages[-1][1] - cue.end) < 0.01
    assert all(abs(pages[index][1] - pages[index + 1][0]) < 0.01 for index in range(len(pages) - 1)), "pages hand over without a gap"


def test_the_spanish_line_breaks_after_its_comma() -> None:
    texts = [" ".join(lines) for _start, _end, lines in _pages(SPANISH)]
    assert texts == ["Este es el cuenco de roble terminado,", "aceitado y listo para la mesa."]


def test_a_line_that_fits_keeps_one_page_and_a_sentence_break_stays(arial_width_captions: Path) -> None:
    english = Cue(0.027, 4.507, "This is the finished oak bowl, oiled and ready for the table.")
    assert len(_pages(english)) == 1
    # A short page after a finished sentence starts a sentence of its own, and is left alone.
    told = Cue(0.0, 6.0, "On the lathe the shape appears in long steady cuts. It shines.")
    assert [" ".join(lines) for _start, _end, lines in _pages(told)] == ["On the lathe the shape appears in long steady cuts.", "It shines."]
    # The pop style shows three words a page by design, so it keeps its short pages.
    assert all(len(" ".join(lines).split()) <= 3 for _start, _end, lines in _pages(SPANISH, "pop"))


def _caption_ctx(tmp_path: Path, **captions: Any) -> SimpleNamespace:
    edit = dict(default_edit(), captions=dict(default_edit()["captions"], **captions))
    return SimpleNamespace(work=tmp_path, edit=edit, request={"language": "en", "budget_usd": 0.25}, canvas=Canvas(width=1080, height=1920),
                           warnings=[], plan={})


def test_translated_captions_say_punch_words_are_not_shown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vedit import captions

    monkeypatch.setattr(captions, "_translated", lambda cues, language, ctx: [SPANISH])
    english = [Cue(0.027, 4.507, "This is the finished oak bowl, oiled and ready for the table.")]
    ctx = _caption_ctx(tmp_path, language="es", emphasize=True, emphasis=[{"cue_key": "27", "word_index": 5, "word": "bowl,"}])
    write_caption_files(english, ctx)
    assert "Punch words are not shown on translated captions." in ctx.warnings
    assert (tmp_path / "captions.es.srt").is_file()
    plain = _caption_ctx(tmp_path, language="es")
    write_caption_files(english, plain)
    assert not any("Punch words" in warning for warning in plain.warnings)
    spoken = _caption_ctx(tmp_path, emphasize=True)
    write_caption_files(english, spoken)
    assert not any("Punch words" in warning for warning in spoken.warnings)


def test_translated_captions_ask_for_no_punch_words_and_report_none(manager: CampaignManager, clip: Path, enabled: None,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(emphasis, "suggest_emphasis", lambda *a, **k: pytest.fail("punch words are not drawn on a translation, so none are bought"))
    ident = planned(manager, clip, provider="meta", budget_usd=0.3, captions=True, language="en")
    _with_captions(manager, ident, language="es", emphasize=True)
    job = manager._load(manager._connect(), ident)
    plan = job["plan"]
    warnings: list[str] = []
    assert manager._emphasis(ident, job, plan, warnings) is None
    picks = [{"cue_key": "200", "word_index": 0, "word": "oak"}]
    drawn = dict(plan, edit=dict(plan["edit"], captions=dict(plan["edit"]["captions"], emphasis=picks)))
    assert CampaignManager._emphasis_result(drawn, picks, job["request"]) == [], "the receipt names no punch word the video does not draw"
    spoken = dict(drawn, edit=dict(drawn["edit"], captions=dict(drawn["edit"]["captions"], language=None)))
    assert CampaignManager._emphasis_result(spoken, picks, job["request"])[0]["word"] == "oak"


def test_the_motion_card_is_written_in_the_caption_language(manager: CampaignManager, clip: Path, enabled: None,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def suggest(plan: dict[str, Any], transcript: Any = None, **kwargs: Any) -> dict[str, Any]:
        seen.append(kwargs["language"])
        return {"card": wiring._card(aspect="9:16"), "backdrop_prompt": "", "notes": [], "proposal": {}, "usage": {}, "model": "muse-spark-1.3"}

    monkeypatch.setattr(blender_cards, "blender_available", lambda: True)
    monkeypatch.setattr(blender_cards, "suggest_card", suggest)
    monkeypatch.setattr(meta, "plan_story", lambda *a, **k: {"title": "Bowl", "summary": "s", "caption": "c", "cover_prompt": "p", "hashtags": [],
                                                             "segments": [{"start": 0.0, "end": 1.6, "label": "A", "reason": "r"}], "usage": {}, "warnings": []})
    ident = planned(manager, clip, provider="meta", budget_usd=0.3, target_duration=1.6, captions=True, language="en")
    manager.suggest_motion_card(ident, backdrop=False)
    _with_captions(manager, ident, language="es")
    result = manager.suggest_motion_card(ident, backdrop=False)
    assert seen == ["en", "es"]
    assert any("Spanish" in note for note in result["warnings"]), result["warnings"]


def test_the_hero_word_stays_spoken_and_says_so(manager: CampaignManager, clip: Path, enabled: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hero_word, "suggest_hero_word", lambda *a, **k: {"word": "oak", "start": 0.2, "end": 1.0, "side": "center", "reason": "",
                                                                         "spoken": [0.26, 0.5], "proposal": {}, "adjusted": [], "usage": {}})
    ident = planned(manager, clip, provider="meta", budget_usd=0.3, captions=True, language="en", subject="the bowl")
    assert not any("spoken word" in warning for warning in manager.suggest_hero_word(ident)["warnings"])
    _with_captions(manager, ident, language="es")
    result = manager.suggest_hero_word(ident)
    assert result["hero_word"]["word"] == "oak"
    assert any("spoken word" in warning and "Spanish" in warning for warning in result["warnings"]), result["warnings"]
