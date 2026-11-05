"""Brief documents load from JSON or YAML, and the built-in YAML subset is what a machine without PyYAML gets."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from vedit.yamlio import dump_yaml, load_document, loads

BRIEF = """
# a comment line
name: demo
sources:
  main: talk.mp4
  music: "bed with spaces.mp3"
canvas:
  width: 1080
  height: 1920
  fps: 29.97
hw: auto
ops:
  - op: cut_silence
    threshold_db: -32
    pad: 0.1
  - op: captions
    burn: true
    language: null
  - op: export
    presets: [youtube_1080p, shorts_9x16]
    path: ~
"""


@pytest.fixture
def without_pyyaml(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``import yaml`` fail, the way it does on a machine that never installed PyYAML."""
    monkeypatch.setitem(sys.modules, "yaml", None)


def test_json_text_loads_whatever_parser_is_installed(without_pyyaml: None) -> None:
    data = loads('  {"name": "x", "ops": [{"op": "trim", "start": 1.5}]}')
    assert data["ops"][0]["start"] == 1.5
    assert loads("[1, 2]") == [1, 2]


def test_the_yaml_subset_reads_a_whole_brief(without_pyyaml: None) -> None:
    subset = loads(BRIEF)
    assert subset["name"] == "demo"
    assert subset["sources"] == {"main": "talk.mp4", "music": "bed with spaces.mp3"}
    assert subset["canvas"] == {"width": 1080, "height": 1920, "fps": 29.97}
    assert subset["hw"] == "auto"
    assert subset["ops"][0] == {"op": "cut_silence", "threshold_db": -32, "pad": 0.1}
    assert subset["ops"][1] == {"op": "captions", "burn": True, "language": None}
    assert subset["ops"][2] == {"op": "export", "presets": ["youtube_1080p", "shorts_9x16"], "path": None}


def test_the_real_parser_and_the_subset_agree_on_the_brief() -> None:
    yaml = pytest.importorskip("yaml")
    expected = yaml.safe_load(BRIEF)
    assert loads(BRIEF) == expected
    with pytest.MonkeyPatch.context() as patch:
        patch.setitem(sys.modules, "yaml", None)
        assert loads(BRIEF) == expected, "a brief must mean the same thing with or without PyYAML"


def test_the_subset_handles_scalars_lists_and_nested_blocks(without_pyyaml: None) -> None:
    data = loads(
        """
flags:
  on: yes
  off: no
  gone: none
  count: -3
  ratio: -0.5
  text: plain words here
  single: 'quoted: colon'
  double: "tab\\tend"
  empty_list: []
  inline_map: {'a': 1, 'b': true}
nested:
  - name: inner
    items:
      - x
      - y
  - 7
  - plain
"""
    )
    flags = data["flags"]
    assert flags["on"] is True and flags["off"] is False and flags["gone"] is None
    assert flags["count"] == -3 and flags["ratio"] == -0.5
    assert flags["text"] == "plain words here"
    assert flags["single"] == "quoted: colon"
    assert flags["double"] == "tab\tend"
    assert flags["empty_list"] == []
    assert flags["inline_map"] == {"a": 1, "b": True}
    assert data["nested"] == [{"name": "inner", "items": ["x", "y"]}, 7, "plain"]


def test_the_subset_keeps_a_key_with_no_value_as_null(without_pyyaml: None) -> None:
    assert loads("a:\nb: 1") == {"a": None, "b": 1}
    assert loads("- op:\n- 2") == [{"op": None}, 2]


def test_an_empty_document_is_refused_with_its_source_named(without_pyyaml: None, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"<string>: document is empty"):
        loads("# only a comment\n\n")
    brief = tmp_path / "empty.yaml"
    brief.write_text("\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"empty\.yaml: document is empty"):
        load_document(brief)


def test_an_empty_document_is_refused_with_pyyaml_too(tmp_path: Path) -> None:
    pytest.importorskip("yaml")
    with pytest.raises(ValueError, match="document is empty"):
        loads("")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("a: 1\n   b: 2", "indent error"),
        ("a:\n  - 1\n  x: 2", "indent error near: x: 2"),
        ("- 1\n  - 2", "indent error"),
        ("just words", "expected 'key:'"),
        ("a: 1\n- 2", "expected mapping entry, got list item"),
    ],
)
def test_the_subset_refuses_malformed_yaml_with_the_offending_line(
    without_pyyaml: None, text: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        loads(text)


def test_load_document_reads_utf8_from_disk(tmp_path: Path) -> None:
    brief = tmp_path / "brief.yaml"
    brief.write_text("name: Café\nsources:\n  main: 'clip ünd.mp4'\n", encoding="utf-8")
    data = load_document(brief)
    assert data == {"name": "Café", "sources": {"main": "clip ünd.mp4"}}


def test_dump_yaml_round_trips_a_brief_in_order_and_keeps_unicode() -> None:
    pytest.importorskip("yaml")
    data = {"name": "Café", "ops": [{"op": "title", "text": "héllo"}], "hw": None, "presets": []}
    text = dump_yaml(data)
    assert text.index("name:") < text.index("ops:") < text.index("hw:") < text.index("presets:"), (
        "keys keep the order they were written in, so a saved brief reads like the one typed"
    )
    assert "Café" in text and "héllo" in text and "\\u" not in text
    assert loads(text) == data


def test_dump_yaml_falls_back_to_json_without_pyyaml(without_pyyaml: None) -> None:
    data = {"name": "x", "ops": [{"op": "trim", "start": 0}]}
    text = dump_yaml(data)
    assert text.endswith("\n")
    assert json.loads(text) == data
    assert loads(text) == data, "the fallback writes a document loads() can read back"
