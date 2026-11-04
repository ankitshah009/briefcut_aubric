"""Private mode: text-only Muse Spark work on Muse Glimmer, served on this machine.

Muse Glimmer (``meta-models/Muse-Glimmer-30B``) is Meta's open-weight 30B model,
distilled from Muse Spark and released under Apache 2.0. In private mode the
director's note, caption translation and the script scene writer send their
prompt to a llama.cpp ``llama-server`` on the loopback interface instead of the
Meta Model API. Nothing leaves the machine, no Meta key is needed, and the cost
is $0.

Turn it on with ``VEDIT_PRIVATE_MODEL=glimmer`` (or ``"private_model": "glimmer"``
in ``~/.vedit/config.json``; the environment wins). Start the server with
``uv run python -m vedit.open_weights serve``. The callers in ``vedit.meta`` keep
their prompts, schemas, validation and caches; only the model call moves here.

Private mode fails closed: a bad setting, a server that is down, a server that is
not on this machine or one that serves a different model raises
``OpenWeightsError`` (a ``MetaError``). It never falls back to the Meta API.

Every call is logged to ``open-weights-calls.jsonl`` in the job's meta folder,
with the model id, GGUF file and sha256, llama.cpp build, tokens, latency and a
short output sample, and is entered in ``meta-usage.json`` beside the Meta API
calls with a cost of $0.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import secrets
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from vedit import meta, settings
from vedit.meta import MetaError

MODEL_ID = "meta-models/Muse-Glimmer-30B"
MODEL_NAME = "Muse Glimmer 30B"
GGUF_REPO = "meta-models/Muse-Glimmer-30B-GGUF"
LICENSE = "Apache-2.0"
SERVED_ALIAS = "muse-glimmer-30b"
DEFAULT_URL = "http://127.0.0.1:8091"
DEFAULT_GGUF = "Muse-Glimmer-30B-KQuant-Dynamic-Q4_K_XL.gguf"
DRAFT_GGUF = "dflash-Muse-Glimmer-30B-Q4_K_M.gguf"
# sha256 of the official files, from the Hugging Face LFS pointers of GGUF_REPO (2026-11-04).
OFFICIAL_SHA256 = {
    "Muse-Glimmer-30B-KQuant-17GB-Q4_K_M.gguf": "4cc57c0f51040a226e5a72cc47b7613f7772950e460a665f7083de89f183f60e",
    "Muse-Glimmer-30B-KQuant-Dynamic-Q4_K_XL.gguf": "ac7023d6a4c704eb9af54ab53e476a66b7f5b6c0ef2fc4a8dde5253c291a6c38",
    "dflash-Muse-Glimmer-30B-Q4_K_M.gguf": "b2e808bf656086fe86bd0d0bd990f01d33e377537a07c02d45371517c8b264ef",
}
# Model card sampling defaults: temperature 1.0, top_p 0.95, top_k 64.
SAMPLING = {"temperature": 1.0, "top_p": 0.95, "top_k": 64}
LOG_NAME = "open-weights-calls.jsonl"
_MIN_LLAMA_BUILD = 10353  # first llama.cpp release that registers the muse-glimmer architecture
_OUTPUT_TOKENS = 16384  # reasoning shares this budget, as with Muse Spark
_PROBE_TIMEOUT = 5
_MAX_RESPONSE = 16_000_000
_SAMPLE_CHARS = 600
_ON = {"glimmer", "muse-glimmer", "muse-glimmer-30b", "on", "1", "true", "yes"}
_OFF = {"", "off", "0", "false", "no", "none"}
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")
_LOG_LOCK = threading.Lock()
_HASHES: dict[tuple[str, int, int], str] = {}


class OpenWeightsError(MetaError):
    """Private mode could not run. Nothing was sent to the Meta API."""


def _setting() -> str:
    env = os.environ.get("VEDIT_PRIVATE_MODEL")
    if env is not None and env.strip():
        return env.strip().lower()
    saved = settings.load().get("private_model")
    return saved.strip().lower() if isinstance(saved, str) else ""


def enabled() -> bool:
    """True when private mode is on. An unrecognised value refuses rather than guessing."""
    value = _setting()
    if value in _OFF:
        return False
    if value in _ON:
        return True
    raise OpenWeightsError("VEDIT_PRIVATE_MODEL must be 'glimmer' (private mode on) or 'off'. Nothing was sent anywhere.")


def server_url() -> str:
    """The local server's base URL. Only loopback addresses are accepted, so private mode stays on this machine."""
    raw = os.environ.get("VEDIT_PRIVATE_MODEL_URL", "").strip() or DEFAULT_URL
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        raise OpenWeightsError("VEDIT_PRIVATE_MODEL_URL is not a valid URL.") from None
    if parts.scheme != "http" or (parts.hostname or "").lower() not in _LOOPBACK or port is None or parts.path not in ("", "/") or parts.query or parts.username:
        raise OpenWeightsError("VEDIT_PRIVATE_MODEL_URL must be http://127.0.0.1:<port>. Private mode only talks to a server on this machine.")
    host = parts.hostname if parts.hostname != "::1" else "[::1]"
    return f"http://{host}:{port}"


def _timeout() -> float:
    raw = os.environ.get("VEDIT_PRIVATE_MODEL_TIMEOUT", "").strip()
    if not raw:
        return 900.0
    try:
        value = float(raw)
    except ValueError:
        value = -1
    if not 1 <= value <= 7200:
        raise OpenWeightsError("VEDIT_PRIVATE_MODEL_TIMEOUT must be between 1 and 7200 seconds.")
    return value


def _reasoning_strength() -> str:
    """Muse Glimmer's reasoning_strength template variable. Follows VEDIT_META_REASONING unless overridden."""
    raw = (os.environ.get("VEDIT_PRIVATE_REASONING") or os.environ.get("VEDIT_META_REASONING") or "medium").strip().lower()
    if raw not in {"low", "medium", "high", "xhigh"}:
        raise OpenWeightsError("VEDIT_PRIVATE_REASONING must be low, medium, high or xhigh.")
    return raw


def models_dir() -> Path:
    override = os.environ.get("VEDIT_MODELS_DIR", "").strip()
    return Path(override).expanduser() if override else Path.home() / ".vedit" / "models"


def default_gguf() -> Path:
    override = os.environ.get("VEDIT_PRIVATE_MODEL_GGUF", "").strip()
    return Path(override).expanduser() if override else models_dir() / "open-weights" / DEFAULT_GGUF


def key_path() -> Path:
    """The local server's API key file. Without a key, any web page open in a browser could use the server."""
    override = os.environ.get("VEDIT_PRIVATE_MODEL_KEY_FILE", "").strip()
    return Path(override).expanduser() if override else models_dir() / "open-weights" / "llama-server.key"


def _server_key() -> str:
    try:
        return key_path().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def ensure_server_key() -> Path:
    """Create the key file once, readable by this user only."""
    path = key_path()
    if not _server_key():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(secrets.token_urlsafe(32) + "\n")
    os.chmod(path, 0o600)
    return path


def _http(method: str, url: str, *, payload: dict[str, Any] | None = None, timeout: float) -> dict[str, Any]:
    data = json.dumps(payload, allow_nan=False).encode() if payload is not None else None
    headers = {"Accept": "application/json"}
    if key := _server_key():
        headers["Authorization"] = "Bearer " + key
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, method=method, headers=headers)
    # No proxy handler: a loopback request must never be routed through HTTP(S)_PROXY.
    with build_opener(ProxyHandler({})).open(request, timeout=timeout) as response:
        raw = response.read(_MAX_RESPONSE + 1)
    if len(raw) > _MAX_RESPONSE:
        raise OpenWeightsError("The local model's answer exceeded the safe size limit.")
    decoded = json.loads(raw)
    if not isinstance(decoded, dict):
        raise OpenWeightsError("The local model server returned an invalid response.")
    return decoded


def _down(url: str) -> OpenWeightsError:
    return OpenWeightsError(
        f"Private mode is on, but no local Muse Glimmer server answers at {url}. "
        "Start it with: uv run python -m vedit.open_weights serve (or set VEDIT_PRIVATE_MODEL=off to use the Meta API). "
        "Nothing was sent to Meta.")


def _build_number(build: str) -> int | None:
    match = re.search(r"b?(\d{4,6})", build or "")
    return int(match.group(1)) if match else None


def file_sha256(path: Path) -> str | None:
    """sha256 of a local GGUF, cached by path, size and modification time (a 20 GB file takes a few seconds)."""
    try:
        stat = path.stat()
    except OSError:
        return None
    key = (str(path), stat.st_size, int(stat.st_mtime))
    if key in _HASHES:
        return _HASHES[key]
    sidecar = path.with_name(path.name + ".sha256.json")
    try:
        saved = json.loads(sidecar.read_text(encoding="utf-8"))
        if saved.get("size") == stat.st_size and saved.get("mtime") == int(stat.st_mtime) and re.fullmatch(r"[0-9a-f]{64}", saved.get("sha256", "")):
            _HASHES[key] = saved["sha256"]
            return saved["sha256"]
    except (OSError, ValueError, AttributeError):
        pass
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(8 * 1024 * 1024):
                digest.update(chunk)
    except OSError:
        return None
    value = digest.hexdigest()
    _HASHES[key] = value
    try:
        sidecar.write_text(json.dumps({"file": path.name, "size": stat.st_size, "mtime": int(stat.st_mtime), "sha256": value}) + "\n", encoding="utf-8")
    except OSError:
        pass
    return value


def identity(url: str | None = None) -> dict[str, Any]:
    """What the local server is serving: model file, sha256, llama.cpp build. Refuses a server that is not Muse Glimmer."""
    base = url or server_url()
    try:
        props = _http("GET", base + "/props", timeout=_PROBE_TIMEOUT)
    except HTTPError as exc:
        exc.close()
        if exc.code == 401:
            raise OpenWeightsError(f"The local model server at {base} rejected Briefcut's key. Restart it with: uv run python -m vedit.open_weights serve") from None
        raise OpenWeightsError(f"The local model server at {base} answered HTTP {exc.code} to /props. Is it llama-server, and has it finished loading?") from None
    except (URLError, TimeoutError, ConnectionError, OSError):
        raise _down(base) from None
    except (ValueError, UnicodeError):
        raise OpenWeightsError(f"The server at {base} is not a llama.cpp server.") from None
    model_path = props.get("model_path") if isinstance(props.get("model_path"), str) else ""
    name = Path(model_path).name
    if "glimmer" not in name.lower():
        raise OpenWeightsError(f"The local server at {base} is serving {name or 'an unnamed model'}, not Muse Glimmer. Private mode only runs Meta's open-weight Muse Glimmer.")
    build = props.get("build_info") if isinstance(props.get("build_info"), str) else ""
    number = _build_number(build)
    path = Path(model_path)
    sha = file_sha256(path) if path.is_absolute() else None
    official = OFFICIAL_SHA256.get(name)
    return {
        "model": MODEL_ID, "model_name": MODEL_NAME, "license": LICENSE, "weights_repo": GGUF_REPO,
        "gguf": {"file": name, "path": model_path, "sha256": sha, "official_sha256": official,
                 "sha256_matches_official": bool(sha and official and sha == official),
                 "size_bytes": path.stat().st_size if sha else None},
        "runtime": {"name": "llama.cpp llama-server", "build": build or None, "build_number": number,
                    "supported": number is None or number >= _MIN_LLAMA_BUILD, "url": base},
        "served_as": props.get("model_alias") if isinstance(props.get("model_alias"), str) else None,
        "slots": props.get("total_slots") if isinstance(props.get("total_slots"), int) else None,
        "where": "this machine (loopback only)",
    }


def status() -> dict[str, Any]:
    """For a Settings panel or `doctor`: whether private mode is on and the local server is ready."""
    try:
        on = enabled()
        url = server_url()
    except OpenWeightsError as exc:
        return {"enabled": False, "ready": False, "reason": str(exc)}
    try:
        found = identity(url)
    except OpenWeightsError as exc:
        return {"enabled": on, "ready": False, "url": url, "model": MODEL_ID, "reason": str(exc)}
    return {"enabled": on, "ready": True, "url": url, **found,
            "reason": f"{MODEL_NAME} is running on this machine with llama.cpp {found['runtime']['build']}."}


def _parse_json(text: str) -> Any:
    cleaned = _FENCE.sub("", text.strip())
    try:
        return json.loads(cleaned)
    except ValueError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start >= 0 and end > start:
            return json.loads(cleaned[start:end + 1])
        raise


def _append_log(work_dir: Path, record: dict[str, Any]) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n"
    with _LOG_LOCK:
        path = work_dir / LOG_NAME
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(line)


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def structured(work_dir: Path, operation: str, *, name: str, schema: dict[str, Any], prompt: str, budget: float,
               check: Callable[[Any], dict[str, Any]], purpose: str = "") -> dict[str, Any]:
    """One schema-constrained JSON answer from Muse Glimmer on this machine. Mirrors ``meta._structured``.

    ``check`` turns the parsed JSON into the result or raises ``MetaError``. The
    call goes through the same ledger and call log as a Meta API call, at $0,
    and its full record (GGUF, sha256, llama.cpp build, timings, a short output
    sample) is appended to ``open-weights-calls.jsonl``.
    """
    work_dir = Path(work_dir).resolve()
    base = server_url()
    found = identity(base)
    if not found["runtime"]["supported"]:
        raise OpenWeightsError(f"llama.cpp {found['runtime']['build']} predates Muse Glimmer support (b{_MIN_LLAMA_BUILD}). Update llama.cpp.")
    strength = _reasoning_strength()
    request = {
        "model": found.get("served_as") or SERVED_ALIAS,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}},
        "chat_template_kwargs": {"reasoning_strength": strength},
        "max_tokens": _OUTPUT_TOKENS, "stream": False, **SAMPLING,
    }
    default = getattr(meta, "_PURPOSES", {}).get(operation, "Ask Muse")
    started = time.time()
    # A $0 reservation: the shared ledger and call log show the local call beside the Meta API calls.
    ident = meta._reserve(work_dir, operation, MODEL_ID, 0.0, budget, purpose=f"{purpose or default} (private: {MODEL_NAME} on this machine)")
    meta._ledger(work_dir, {"id": ident, "runtime": "llama.cpp", "local": True})
    record: dict[str, Any] = {
        "at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"), "id": ident, "operation": operation,
        "provider": "meta-open-weights", "model": MODEL_ID, "model_name": MODEL_NAME, "license": LICENSE,
        "gguf": found["gguf"], "llama_cpp": found["runtime"]["build"], "server": base, "where": found["where"],
        "reasoning_strength": strength, "sampling": SAMPLING, "schema": name, "prompt_chars": len(prompt),
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(), "cost_usd": 0.0,
    }
    response: dict[str, Any] = {}
    try:
        try:
            response = _http("POST", base + "/v1/chat/completions", payload=request, timeout=_timeout())
        except HTTPError as exc:
            exc.close()
            raise OpenWeightsError(f"The local Muse Glimmer server refused the request (HTTP {exc.code}). Check its log; nothing was sent to Meta.") from None
        except (URLError, ConnectionError) as exc:
            if isinstance(getattr(exc, "reason", None), TimeoutError):
                raise OpenWeightsError("The local Muse Glimmer server did not finish within VEDIT_PRIVATE_MODEL_TIMEOUT.") from None
            raise _down(base) from None
        except (TimeoutError, OSError):
            raise OpenWeightsError("The local Muse Glimmer server did not finish within VEDIT_PRIVATE_MODEL_TIMEOUT.") from None
        except (ValueError, UnicodeError):
            raise OpenWeightsError("The local Muse Glimmer server returned an unreadable response.") from None
        latency = time.time() - started
        raw_usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
        timings = response.get("timings") if isinstance(response.get("timings"), dict) else {}
        usage: dict[str, Any] = {"estimated_max_usd": 0.0, "cached": False, "cost_usd": 0.0, "local": True}
        for key, source in (("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens"), ("total_tokens", "total_tokens")):
            if (value := _count(raw_usage.get(source))) is not None:
                usage[key] = value
        choices = response.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        content = message.get("content") if isinstance(message.get("content"), str) else ""
        reasoning = message.get("reasoning_content") if isinstance(message.get("reasoning_content"), str) else ""
        record.update(
            latency_seconds=round(latency, 3), finish_reason=choice.get("finish_reason"), served_model=response.get("model"),
            system_fingerprint=response.get("system_fingerprint"), tokens={k: usage[k] for k in ("input_tokens", "output_tokens", "total_tokens") if k in usage},
            reasoning_chars=len(reasoning), output_chars=len(content), output_sample=content[:_SAMPLE_CHARS],
            timings={k: timings[k] for k in ("prompt_n", "prompt_ms", "prompt_per_second", "predicted_n", "predicted_ms", "predicted_per_second", "draft_n", "draft_n_accepted") if k in timings},
        )
        meta._ledger(work_dir, {"id": ident, "status": "received", "usage": usage})
        if choice.get("finish_reason") == "length":
            raise OpenWeightsError("Muse Glimmer ran out of output tokens before finishing (reasoning shares the budget). Retry, or set VEDIT_PRIVATE_REASONING=low.")
        if not content.strip():
            raise OpenWeightsError("Muse Glimmer returned no answer. Retry the request.")
        try:
            parsed = _parse_json(content)
        except (ValueError, UnicodeError):
            raise OpenWeightsError(f"Muse Glimmer returned malformed JSON for {operation}. Nothing was changed.") from None
        result = check(parsed)
        result.update(provider="meta", model=MODEL_ID, usage=usage, runtime="llama.cpp", private=True)
        result.setdefault("warnings", [])
        meta._ledger(work_dir, {"id": ident, "status": "completed"})
        record["status"] = "completed"
        return result
    except Exception as exc:
        meta._ledger(work_dir, {"id": ident, "status": "failed_or_incomplete"})
        record.setdefault("latency_seconds", round(time.time() - started, 3))
        record.update(status="failed", error=str(exc)[:300] if isinstance(exc, MetaError) else type(exc).__name__)
        raise
    finally:
        _append_log(work_dir, record)


def llama_server() -> str:
    found = os.environ.get("VEDIT_LLAMA_SERVER", "").strip() or shutil.which("llama-server") or "/opt/homebrew/bin/llama-server"
    return found


def serve_command(*, model: Path | None = None, port: int = 8091, draft: Path | None = None,
                  context: int = 131072, slots: int = 2, key_file: Path | None = None) -> list[str]:
    """The llama-server command line from the model card, bound to loopback with every layer on the GPU.

    With ``key_file`` the server demands that key, so a web page in a browser on
    this machine cannot use it. ``--no-slots`` hides the slot monitor, which can
    echo cached prompts, and ``--no-webui`` serves no chat page on the port.
    """
    command = [llama_server(), "-m", str(model or default_gguf()), "-a", SERVED_ALIAS,
               "-ngl", "99", "-c", str(context), "-np", str(slots),
               "--host", "127.0.0.1", "--port", str(port), "--jinja",
               "--temp", str(SAMPLING["temperature"]), "--top-p", str(SAMPLING["top_p"]), "--top-k", str(SAMPLING["top_k"]),
               "--no-slots", "--no-webui"]
    if key_file is not None:
        command += ["--api-key-file", str(key_file)]
    if draft is not None:
        command += ["-md", str(draft), "-ngld", "99"]
    return command


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vedit.open_weights", description="Run Muse Glimmer on this machine for Briefcut's private mode.")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="Start llama-server with the official Muse Glimmer GGUF on 127.0.0.1.")
    serve.add_argument("--model", type=Path, default=None)
    serve.add_argument("--port", type=int, default=8091)
    serve.add_argument("--draft", action="store_true", help="Add the official DFlash drafter for speculative decoding.")
    serve.add_argument("--print", action="store_true", help="Print the command instead of running it.")
    sub.add_parser("status", help="Show what the local server is serving.")
    args = parser.parse_args(argv)
    if args.command == "status":
        print(json.dumps(status(), indent=2))
        return 0
    model = args.model or default_gguf()
    draft = model.with_name(DRAFT_GGUF) if args.draft else None
    command = serve_command(model=model, port=args.port, draft=draft, key_file=key_path())
    print(" ".join(command), flush=True)
    if args.print:
        return 0
    ensure_server_key()
    if not model.is_file():
        print(f"Missing {model}. Download it from https://huggingface.co/{GGUF_REPO}.", file=sys.stderr)
        return 1
    if (sha := file_sha256(model)) and OFFICIAL_SHA256.get(model.name) not in (None, sha):
        print(f"{model.name} does not match the official sha256; re-download it.", file=sys.stderr)
        return 1
    os.execv(command[0], command)
    return 0  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
