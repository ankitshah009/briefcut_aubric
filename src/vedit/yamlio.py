"""Load briefs from JSON or YAML. PyYAML if installed; otherwise a strict subset."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def load_document(path: str | Path) -> Any:
    text = Path(path).read_text(encoding="utf-8")
    return loads(text, source=str(path))


def loads(text: str, *, source: str = "<string>") -> Any:
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        return json.loads(text)
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text)
    except ImportError:
        data = _simple_yaml(text)
    if data is None:
        raise ValueError(f"{source}: document is empty")
    return data


def dump_yaml(data: Any) -> str:
    try:
        import yaml  # type: ignore

        return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    except ImportError:
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _simple_yaml(text: str) -> Any:
    """Indent-based YAML subset: maps, lists, scalars, comments. Good enough for briefs."""
    lines: list[tuple[int, str]] = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        lines.append((indent, raw.strip()))
    if not lines:
        return None
    value, _ = _parse_block(lines, 0, lines[0][0])
    return value


def _parse_block(lines: list[tuple[int, str]], i: int, indent: int) -> tuple[Any, int]:
    if i >= len(lines):
        return None, i
    _, content = lines[i]
    if content.startswith("- "):
        return _parse_list(lines, i, indent)
    return _parse_map(lines, i, indent)


def _parse_map(lines: list[tuple[int, str]], i: int, indent: int) -> tuple[dict[str, Any], int]:
    out: dict[str, Any] = {}
    while i < len(lines):
        ind, content = lines[i]
        if ind < indent:
            break
        if ind > indent:
            raise ValueError(f"YAML indent error near: {content}")
        if content.startswith("- "):
            raise ValueError(f"YAML: expected mapping entry, got list item: {content}")
        if ":" not in content:
            raise ValueError(f"YAML: expected 'key:' near: {content}")
        key, rest = content.split(":", 1)
        key = key.strip()
        rest = rest.strip()
        i += 1
        if rest:
            out[key] = _parse_scalar(rest)
            continue
        if i < len(lines) and lines[i][0] > indent:
            child, i = _parse_block(lines, i, lines[i][0])
            out[key] = child
        else:
            out[key] = None
    return out, i


def _parse_list(lines: list[tuple[int, str]], i: int, indent: int) -> tuple[list[Any], int]:
    out: list[Any] = []
    while i < len(lines):
        ind, content = lines[i]
        if ind < indent:
            break
        if ind > indent:
            raise ValueError(f"YAML indent error near: {content}")
        if not content.startswith("- "):
            break
        rest = content[2:].strip()
        i += 1
        if rest:
            if ":" in rest and not rest.startswith(("'", '"')):
                # inline map start: "- op: trim"
                key, val = rest.split(":", 1)
                item: dict[str, Any] = {key.strip(): _parse_scalar(val.strip()) if val.strip() else None}
                if i < len(lines) and lines[i][0] > indent:
                    extra, i = _parse_map(lines, i, lines[i][0])
                    if item[key.strip()] is None and isinstance(extra, dict):
                        item[key.strip()] = extra
                    else:
                        item.update(extra)
                out.append(item)
            else:
                out.append(_parse_scalar(rest))
        elif i < len(lines) and lines[i][0] > indent:
            child, i = _parse_block(lines, i, lines[i][0])
            out.append(child)
        else:
            out.append(None)
    return out, i


_TRUE = {"true", "yes", "on"}
_FALSE = {"false", "no", "off"}
_NULL = {"null", "~", "none"}


def _parse_scalar(text: str) -> Any:
    if text.startswith("'") and text.endswith("'") and len(text) >= 2:
        return text[1:-1]
    if text.startswith('"') and text.endswith('"') and len(text) >= 2:
        return bytes(text[1:-1], "utf-8").decode("unicode_escape")
    low = text.lower()
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    if low in _NULL:
        return None
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.\d+", text):
        return float(text)
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(p.strip()) for p in inner.split(",")]
    if text.startswith("{") and text.endswith("}"):
        return json.loads(text.replace("'", '"'))
    return text
