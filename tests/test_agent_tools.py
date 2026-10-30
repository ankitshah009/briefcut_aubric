import io
import json

from vedit.agent.tools import dispatch, serve_stdio, tool_specs

_TOOL_NAMES = (
    "doctor",
    "inventory",
    "parse_intent",
    "decide",
    "plan",
    "render",
    "critique",
    "revise",
    "specs",
)


def test_tool_specs_include_agent_tools():
    specs = tool_specs()
    assert isinstance(specs, list)
    names = []
    for item in specs:
        assert isinstance(item, dict)
        assert isinstance(item["name"], str) and item["name"]
        assert isinstance(item["description"], str) and item["description"].strip()
        assert item.get("parameters") is not None
        names.append(item["name"])
    assert set(_TOOL_NAMES) <= set(names)


def test_dispatch_parse_intent_short_under_20s():
    out = dispatch("parse_intent", {"goal": "short under 20s of talk.mp4"})
    assert out["ok"] is True
    assert out["result"]["platform"] == "shorts"
    assert out["result"]["max_duration"] == 20


def test_dispatch_unknown_tool_does_not_raise():
    out = dispatch("nope", {})
    assert out["ok"] is False


def test_dispatch_revise_inserts_captions_before_export():
    out = dispatch(
        "revise",
        {
            "brief": {
                "name": "t",
                "sources": {"main": "/tmp/a.mp4"},
                "ops": [{"op": "export", "presets": ["youtube_1080p"]}],
            },
            "critique": {
                "ok": False,
                "findings": [
                    {
                        "code": "captions_missing",
                        "severity": "fail",
                        "message": "no srt",
                        "fix": "add captions",
                    }
                ],
            },
            "goal": "youtube talk",
        },
    )
    assert out["ok"] is True
    ops = out["result"]["brief"]["ops"]
    assert any(op["op"] == "captions" for op in ops)
    assert ops[-1]["op"] == "export"


def test_serve_stdio_two_json_lines():
    stdin = io.StringIO(
        '{"tool":"parse_intent","arguments":{"goal":"podcast episode, no captions"}}\n'
        '{"tool":"missing"}\n'
    )
    stdout = io.StringIO()
    code = serve_stdio(stdin, stdout)
    assert code == 0
    lines = stdout.getvalue().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first["ok"] is True
    assert first["result"]["platform"] == "podcast"
    assert first["result"]["captions"] is False
    assert second["ok"] is False
