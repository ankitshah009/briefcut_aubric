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
        '{"tool":"parse_intent","arguments":{"goal":"podcast episode, no captions"}}\n{"tool":"missing"}\n'
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


def _failed_session(**fields):
    return {
        "status": "failed",
        "root": "/x",
        "revision": 0,
        "critique": {
            "ok": False,
            "findings": [
                {
                    "code": "render_failed",
                    "severity": "fail",
                    "message": "step 'export' failed (exit 1)",
                    "fix": "read project.json and the step stderr",
                },
                {"code": "soft", "severity": "warn", "message": "the hook is late", "fix": "trim the open"},
            ],
        },
        **fields,
    }


def test_a_failed_render_or_plan_is_not_ok(monkeypatch):
    from vedit.agent import tools

    monkeypatch.setitem(tools._HANDLERS, "render", lambda args: _failed_session())
    monkeypatch.setitem(tools._HANDLERS, "plan", lambda args: dict(_failed_session(), status="planned"))
    out = dispatch("render", {"goal": "youtube talk", "paths": ["/x.mp4"]})
    assert out["ok"] is False
    assert out["result"]["status"] == "failed", "the session still comes back for inspection"
    assert out["error"] == "step 'export' failed (exit 1) (read project.json and the step stderr)", (
        "only failing findings make the error"
    )
    assert dispatch("plan", {"goal": "youtube talk"})["ok"] is True


def test_a_failed_session_without_a_critique_still_says_why(monkeypatch):
    from vedit.agent import tools

    monkeypatch.setitem(tools._HANDLERS, "render", lambda args: {"status": "failed", "critique": None})
    out = dispatch("render", {"goal": "youtube talk"})
    assert out["ok"] is False and out["error"] == "The session failed and left no critique."


def test_revise_is_clamped_to_zero_through_three(monkeypatch):
    import vedit.agent.loop as loop
    from vedit.agent import tools

    seen = []

    class _Session:
        def to_dict(self):
            return {"status": "verified"}

    def fake_run_session(goal, paths, **kwargs):
        seen.append(kwargs["max_revisions"])
        return _Session()

    monkeypatch.setattr(loop, "run_session", fake_run_session)
    for asked, expected in [(None, 1), (0, 0), (2, 2), (3, 3), (50, tools.MAX_REVISIONS), (-4, 0), ("7", 3)]:
        args = {"goal": "youtube talk", "paths": ["/x.mp4"]}
        if asked is not None:
            args["revise"] = asked
        assert dispatch("render", args)["ok"] is True
    assert seen == [1, 0, 2, 3, 3, 0, 3]
