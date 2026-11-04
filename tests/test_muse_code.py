"""Muse Code setup stays in step with the repository's own MCP config and skill."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_muse_code_finds_the_same_skill_the_plugin_ships() -> None:
    linked = ROOT / ".agents" / "skills" / "briefcut" / "SKILL.md"
    canonical = ROOT / "skills" / "briefcut" / "SKILL.md"
    assert linked.is_file(), "Muse Code reads project skills from .agents/skills/<id>/SKILL.md"
    assert linked.resolve() == canonical.resolve(), "one skill, not a copy that can drift"


def test_the_documented_server_runs_the_same_command_as_mcp_json() -> None:
    doc = (ROOT / "docs" / "MUSE_CODE.md").read_text(encoding="utf-8")
    block = re.search(r"```json\n(.*?)\n```", doc, re.S)
    assert block, "docs/MUSE_CODE.md lost its settings block"
    settings = json.loads(block.group(1))
    assert settings["schema_version"] == 1, "Muse Code refuses a settings file without schema_version 1"
    server = settings["mcpServers"]["briefcut"]  # the spelling Muse Code writes back
    assert server["transport"] == "stdio"
    args = list(server["args"])
    at = args.index("--project")
    del args[at : at + 2]  # Muse Code starts outside the checkout, so the docs name it
    project = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["briefcut"]
    assert [server["command"], *args] == [project["command"], *project["args"]]
    assert "MODEL_API_KEY" not in json.dumps(settings), "the key never goes in settings.json"
