"""Stdio JSON tools an external agent calls, one object per line."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, TextIO


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


_SPECS: list[dict[str, Any]] = [
    {
        "name": "doctor",
        "description": "Report whether ffmpeg, ffprobe, filters, and optional whisper are ready.",
        "parameters": _schema({}),
    },
    {
        "name": "inventory",
        "description": "Probe media paths into named shots with duration, size, and optional silence or loudness.",
        "parameters": _schema(
            {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Media files to probe and name.",
                },
                "analyze": {
                    "type": "boolean",
                    "default": False,
                    "description": "When true, measure silence ratio, scene count, and loudness.",
                },
            },
            ["paths"],
        ),
    },
    {
        "name": "parse_intent",
        "description": "Parse an English edit goal into a platform, duration cap, and feature flags.",
        "parameters": _schema(
            {
                "goal": {"type": "string", "description": "English description of the edit."},
                "sources": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "Optional source name to file path map.",
                },
            },
            ["goal"],
        ),
    },
    {
        "name": "decide",
        "description": "Inventory the paths, parse the goal, and return an editorial brief decision.",
        "parameters": _schema(
            {
                "goal": {"type": "string", "description": "English description of the edit."},
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Media files the decision may use.",
                },
                "analyze": {
                    "type": "boolean",
                    "default": False,
                    "description": "When true, measure silence, scenes, and loudness before deciding.",
                },
            },
            ["goal", "paths"],
        ),
    },
    {
        "name": "plan",
        "description": "Compile a session for the goal and paths and stop before any encode.",
        "parameters": _schema(
            {
                "goal": {"type": "string", "description": "English description of the edit."},
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Media files. Omit to take paths written in the goal.",
                },
                "root": {
                    "type": "string",
                    "description": "Session directory. Omit to create one under vedit-sessions.",
                },
                "analyze": {
                    "type": "boolean",
                    "default": False,
                    "description": "When true, measure silence, scenes, and loudness while planning.",
                },
            },
            ["goal"],
        ),
    },
    {
        "name": "render",
        "description": "Render the goal, critique the output, and revise the brief up to the given count.",
        "parameters": _schema(
            {
                "goal": {"type": "string", "description": "English description of the edit."},
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Media files. Omit to take paths written in the goal.",
                },
                "root": {
                    "type": "string",
                    "description": "Session directory. Omit to create one under vedit-sessions.",
                },
                "analyze": {
                    "type": "boolean",
                    "default": True,
                    "description": "When true, measure silence, scenes, and loudness before the render.",
                },
                "revise": {
                    "type": "integer",
                    "default": 1,
                    "description": "Maximum critique-driven brief revisions after the first encode.",
                },
            },
            ["goal"],
        ),
    },
    {
        "name": "critique",
        "description": "Judge a rendered file against the goal and return fail or warn findings.",
        "parameters": _schema(
            {
                "path": {"type": "string", "description": "Rendered media file to judge."},
                "goal": {"type": "string", "description": "English description the file was meant to satisfy."},
                "work_dir": {
                    "type": "string",
                    "description": "Work directory that may contain captions.srt.",
                },
                "measure": {
                    "type": "boolean",
                    "default": False,
                    "description": "When true, measure loudness instead of only probing the file.",
                },
            },
            ["path", "goal"],
        ),
    },
    {
        "name": "revise",
        "description": "Apply critique failures to a brief and return the updated brief plus notes.",
        "parameters": _schema(
            {
                "brief": {
                    "type": "object",
                    "description": "Brief mapping with name, sources, and ops.",
                },
                "critique": {
                    "type": "object",
                    "description": "Critique mapping with ok and findings.",
                },
                "goal": {"type": "string", "description": "English description used to parse intent."},
            },
            ["brief", "critique", "goal"],
        ),
    },
    {
        "name": "specs",
        "description": "Return the name, description, and parameter schema of every tool.",
        "parameters": _schema({}),
    },
]


def tool_specs() -> list[dict[str, Any]]:
    return [dict(item) for item in _SPECS]


def _error_text(exc: Exception) -> str:
    message = str(exc).strip()
    if not message:
        return exc.__class__.__name__
    return message


def _flag(args: dict[str, Any], key: str, default: bool) -> bool:
    if key not in args or args[key] is None:
        return default
    value = args[key]
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, int) and not isinstance(value, bool):
        return bool(value)
    raise ValueError(f"{key} must be a boolean")


def _int(args: dict[str, Any], key: str, default: int) -> int:
    if key not in args or args[key] is None:
        return default
    value = args[key]
    if isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    raise ValueError(f"{key} must be an integer")


def _goal(args: dict[str, Any]) -> str:
    value = args.get("goal")
    if not isinstance(value, str):
        raise ValueError("goal must be a string")
    return value


def _string_list(args: dict[str, Any], key: str, *, required: bool) -> list[str]:
    if key not in args or args[key] is None:
        if required:
            raise ValueError(f"{key} must be a list of strings")
        return []
    value = args[key]
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{key} must be a list of strings")
    return list(value)


def _optional_str(args: dict[str, Any], key: str) -> str | None:
    if key not in args or args[key] is None or args[key] == "":
        return None
    value = args[key]
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _optional_path(args: dict[str, Any], key: str) -> Path | None:
    text = _optional_str(args, key)
    if text is None:
        return None
    return Path(text).expanduser()


def _mapping(args: dict[str, Any], key: str) -> dict[str, Any]:
    value = args.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value


def _tool_doctor(_args: dict[str, Any]) -> dict[str, Any]:
    from vedit.doctor import doctor_dict

    return doctor_dict()


def _tool_inventory(args: dict[str, Any]) -> dict[str, Any]:
    from vedit.agent.inventory import inventory_paths

    inventory = inventory_paths(_string_list(args, "paths", required=True), analyze=_flag(args, "analyze", False))
    return inventory.to_dict()


def _tool_parse_intent(args: dict[str, Any]) -> dict[str, Any]:
    from vedit.agent.intent import parse_intent

    sources = args.get("sources")
    if sources is not None and not isinstance(sources, dict):
        raise ValueError("sources must be an object")
    return parse_intent(_goal(args), sources=sources).to_dict()


def _tool_decide(args: dict[str, Any]) -> dict[str, Any]:
    from vedit.agent.decide import decide
    from vedit.agent.intent import parse_intent
    from vedit.agent.inventory import inventory_paths

    inventory = inventory_paths(_string_list(args, "paths", required=True), analyze=_flag(args, "analyze", False))
    sources = {shot.name: shot.path for shot in inventory.shots}
    intent = parse_intent(_goal(args), sources=sources or None)
    return decide(intent, inventory).to_dict()


def _tool_plan(args: dict[str, Any]) -> dict[str, Any]:
    from vedit.agent.loop import run_session

    session = run_session(
        _goal(args),
        _string_list(args, "paths", required=False),
        root=_optional_path(args, "root"),
        render=False,
        analyze=_flag(args, "analyze", False),
    )
    return session.to_dict()


def _tool_render(args: dict[str, Any]) -> dict[str, Any]:
    from vedit.agent.loop import run_session

    session = run_session(
        _goal(args),
        _string_list(args, "paths", required=False),
        root=_optional_path(args, "root"),
        render=True,
        analyze=_flag(args, "analyze", True),
        max_revisions=_int(args, "revise", 1),
    )
    return session.to_dict()


def _tool_critique(args: dict[str, Any]) -> dict[str, Any]:
    from vedit.agent.critique import critique_output
    from vedit.agent.intent import parse_intent

    path = _optional_str(args, "path")
    if path is None:
        raise ValueError("path must be a string")
    intent = parse_intent(_goal(args))
    critique = critique_output(
        path,
        intent,
        work_dir=_optional_path(args, "work_dir"),
        measure=_flag(args, "measure", False),
    )
    return critique.to_dict()


def _tool_revise(args: dict[str, Any]) -> dict[str, Any]:
    from vedit.agent.intent import parse_intent
    from vedit.agent.model import Critique
    from vedit.agent.revise import revise_brief
    from vedit.ir import brief_from_mapping, brief_to_dict

    brief = brief_from_mapping(_mapping(args, "brief"))
    critique = Critique.from_dict(_mapping(args, "critique"))
    intent = parse_intent(_goal(args), sources=brief.sources or None)
    revised, notes = revise_brief(brief, critique, intent)
    return {"brief": brief_to_dict(revised), "notes": list(notes)}


def _tool_specs(_args: dict[str, Any]) -> list[dict[str, Any]]:
    return tool_specs()


_HANDLERS = {
    "doctor": _tool_doctor,
    "inventory": _tool_inventory,
    "parse_intent": _tool_parse_intent,
    "decide": _tool_decide,
    "plan": _tool_plan,
    "render": _tool_render,
    "critique": _tool_critique,
    "revise": _tool_revise,
    "specs": _tool_specs,
}


def dispatch(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(name, str) or not name.strip():
        return {"ok": False, "error": "tool name must be a string"}
    if arguments is None:
        arguments = {}
    elif not isinstance(arguments, dict):
        return {"ok": False, "error": "arguments must be an object"}
    handler = _HANDLERS.get(name.strip())
    if handler is None:
        return {"ok": False, "error": f"unknown tool: {name.strip()}"}
    try:
        return {"ok": True, "result": handler(arguments)}
    except Exception as exc:
        return {"ok": False, "error": _error_text(exc)}


def _emit(stdout: TextIO, payload: dict[str, Any]) -> None:
    try:
        line = json.dumps(payload)
    except (TypeError, ValueError) as exc:
        line = json.dumps({"ok": False, "error": f"result is not JSON: {exc}"})
    stdout.write(line + "\n")
    stdout.flush()


def _tool_name(payload: dict[str, Any]) -> str | None:
    for key in ("tool", "name"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def serve_stdio(stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    incoming = sys.stdin if stdin is None else stdin
    outgoing = sys.stdout if stdout is None else stdout
    for line in incoming:
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            _emit(outgoing, {"ok": False, "error": str(exc)})
            continue
        if not isinstance(payload, dict):
            _emit(outgoing, {"ok": False, "error": "expected a JSON object"})
            continue
        name = _tool_name(payload)
        if name is None:
            _emit(outgoing, {"ok": False, "error": "missing tool name"})
            continue
        if "arguments" in payload and payload["arguments"] is not None:
            arguments = payload["arguments"]
        else:
            arguments = {}
        if not isinstance(arguments, dict):
            _emit(outgoing, {"ok": False, "error": "arguments must be an object"})
            continue
        try:
            result = dispatch(name, arguments)
        except Exception as exc:
            result = {"ok": False, "error": _error_text(exc)}
        _emit(outgoing, result)
    return 0
