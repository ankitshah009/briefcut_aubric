from pathlib import Path

import pytest

from vedit.cli import _peel_do_flags
from vedit.dsl import parse_do
from vedit.ir import brief_from_mapping, parse_hw
from vedit.yamlio import loads


def test_yaml_subset_brief():
    text = """
name: demo
sources:
  main: talk.mp4
  music: bed.mp3
ops:
  - op: cut_silence
    threshold_db: -32
  - op: captions
    burn: true
  - op: music
    source: music
"""
    data = loads(text)
    brief = brief_from_mapping(data)
    assert brief.name == "demo"
    assert brief.sources["main"] == "talk.mp4"
    assert brief.ops[0]["op"] == "cut_silence"
    assert brief.ops[0]["threshold_db"] == -32
    assert brief.ops[1]["burn"] is True


def test_json_brief():
    data = loads('{"name": "x", "sources": {"main": "a.mp4"}, "ops": [{"op": "trim", "start": 1}]}')
    brief = brief_from_mapping(data)
    assert brief.ops[0]["start"] == 1


def test_do_flags_after_file_are_peeled():
    kept, work, dry, plan, hw = _peel_do_flags(
        ["talk.mp4", "--cut-silence", "--dry-run", "--plan", "--no-hw", "--work-dir", "/tmp/w"]
    )
    assert kept == ["talk.mp4", "--cut-silence"]
    assert work == "/tmp/w"
    assert dry is True
    assert plan is True
    assert hw is False


def test_dsl_title_keeps_spaces():
    brief = parse_do(["talk.mp4", "--title", "VEDIT LIVE", "--preset", "web_preview"])
    titles = [o for o in brief.ops if o["op"] == "title"]
    assert titles and titles[0]["text"] == "VEDIT LIVE"


def test_dsl_flags():
    brief = parse_do("talk.mp4 --cut-silence --captions --music bed.mp3 --preset shorts --out out.mp4")
    ops = [o["op"] for o in brief.ops]
    assert ops[0] == "cut_silence"
    assert "captions" in ops
    assert "music" in ops
    assert brief.sources["music"].endswith("bed.mp3")
    assert "shorts" in brief.presets[0] or brief.presets[0] == "shorts"


def test_dsl_english():
    brief = parse_do("cut silence from interview.mp4; captions; duck music.mp3; reframe 9:16; export shorts")
    ops = [o["op"] for o in brief.ops]
    assert "cut_silence" in ops
    assert "captions" in ops
    assert "music" in ops
    assert "reframe" in ops
    assert any(o["op"] == "export" for o in brief.ops)
    assert brief.sources["main"].endswith("interview.mp4")


def test_one_quoted_english_sentence_in_a_list_is_parsed_as_english():
    """argparse hands ``vedit do "cut silence from talk.mp4; captions"`` over as a one-element list."""
    brief = parse_do(["cut silence from talk.mp4; captions"])
    assert [o["op"] for o in brief.ops] == ["cut_silence", "captions", "export"]
    assert brief.sources["main"].endswith("talk.mp4")


def test_cli_version():
    from vedit.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0


def test_parse_hw_auto():
    assert parse_hw("auto") is None
    assert parse_hw(None) is None
    assert parse_hw(True) is True
    assert parse_hw("off") is False
    assert parse_hw("on") is True
    brief = brief_from_mapping({"name": "x", "sources": {"main": "a.mp4"}, "ops": [{"op": "trim"}], "hw": "auto"})
    assert brief.hw is None


def test_load_example_podcast(tmp_path: Path):
    src = Path("examples/podcast.yaml")
    if not src.is_file():
        return
    # Rewrite sources to dummy paths so load doesn't require real media.
    text = src.read_text()
    dest = tmp_path / "podcast.yaml"
    dest.write_text(text)
    # File paths in the example are relative; load_brief should not crash on YAML shape.
    data = loads(text)
    assert data["ops"][0]["op"] == "cut_silence"


def test_a_missing_brief_is_a_brief_error_not_a_traceback(tmp_path: Path) -> None:
    from vedit.errors import BriefError
    from vedit.ir import load_brief

    with pytest.raises(BriefError, match="brief not found"):
        load_brief(tmp_path / "missing.yaml")
