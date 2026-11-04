"""Machine settings for people who never open a shell.

Stored in ``~/.vedit/config.json`` with mode 0600 (``VEDIT_HOME`` moves the
folder). The Meta key goes to the macOS Keychain through the ``security`` tool
when it can, and into that file otherwise. The environment always wins, so
``.env.local``, CI, and every existing launcher behave as before.

The key leaves this module in one place only: ``meta_api_key()``, read by the
Meta adapter when it builds a request. ``public()`` is what the browser sees:
whether a key is configured and its last four characters.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from contextlib import suppress
from pathlib import Path
from typing import Any

BUDGET_MIN, BUDGET_MAX = 0.01, 5.0
DEFAULT_BUDGET = 0.25
DEFAULT_LANGUAGE = "en"
_MODES = ("standard", "contributor")
_LANGUAGE = re.compile(r"^(?:auto|[a-z]{2,3}(?:-[A-Za-z]{2,8})?)$")
# Printable ASCII without quotes, backslash, or space: real keys never need them,
# and they would have to be escaped for the Keychain tool's command line.
_KEY = re.compile(r"^[!#-&(-\[\]-~]{8,512}$")
_KEYCHAIN_SERVICE = "ai.aubric.briefcut.meta-api-key"
_KEYCHAIN_ACCOUNT = "briefcut"
_KEYCHAIN_TIMEOUT = 10
_FIELDS = {"meta_api_key", "clear_meta_api_key", "meta_enabled", "meta_mode", "default_budget_usd", "default_language", "workspace"}
_ENV_LOCAL_KEYS = {"MODEL_API_KEY", "VEDIT_META_ENABLED", "VEDIT_META_MODE"}
_LOCK = threading.RLock()
_keychain_cache: dict[str, Any] = {}


class SettingsError(Exception):
    """A message safe to show a person. It never contains a submitted value."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def home_dir() -> Path:
    override = os.environ.get("VEDIT_HOME", "").strip()
    return Path(override).expanduser() if override else Path.home() / ".vedit"


def config_path() -> Path:
    return home_dir() / "config.json"


def load() -> dict[str, Any]:
    """The saved settings, or nothing. A damaged file must not stop Studio opening."""
    path = config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    # Someone may have copied the file with looser permissions; the key must not stay readable.
    with suppress(OSError):
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            path.chmod(0o600)
    return data if isinstance(data, dict) else {}


def _write(data: dict[str, Any]) -> None:
    path = config_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # mkstemp creates the file 0600, so the key is never briefly world-readable.
    descriptor, name = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(name)
        raise


def keychain_available() -> bool:
    return sys.platform == "darwin" and os.environ.get("VEDIT_NO_KEYCHAIN") != "1" and shutil.which("security") is not None


def _keychain_target() -> list[str]:
    # A test can name a throwaway keychain; a person never needs this.
    target = os.environ.get("VEDIT_KEYCHAIN", "").strip()
    return [target] if target else []


def _security(arguments: list[str], *, stdin: str | None = None) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(["security", *arguments], input=stdin, capture_output=True, text=True, timeout=_KEYCHAIN_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def _keychain_store(secret: str) -> bool:
    # `security -i` reads the command from stdin. On argv the key would show in `ps`.
    target = " ".join(f'"{item}"' for item in _keychain_target())
    command = f'add-generic-password -U -a "{_KEYCHAIN_ACCOUNT}" -s "{_KEYCHAIN_SERVICE}" -l "Briefcut Meta API key" -w "{secret}" {target}\n'
    result = _security(["-i"], stdin=command)
    if result is None or result.returncode != 0 or "error" in (result.stderr or "").lower():
        return False
    _keychain_cache.clear()
    return _keychain_read() == secret


def _keychain_read() -> str:
    cached = _keychain_cache.get("value")
    if cached:
        return cached
    result = _security(["find-generic-password", "-a", _KEYCHAIN_ACCOUNT, "-s", _KEYCHAIN_SERVICE, "-w", *_keychain_target()])
    if result is None or result.returncode != 0:
        return ""
    value = (result.stdout or "").strip()
    if value:
        _keychain_cache["value"] = value
    return value


def _keychain_delete() -> None:
    _keychain_cache.clear()
    _security(["delete-generic-password", "-a", _KEYCHAIN_ACCOUNT, "-s", _KEYCHAIN_SERVICE, *_keychain_target()])


def _last4(secret: str) -> str:
    # A very short test key would be disclosed whole by "the last four".
    return secret[-4:] if len(secret) >= 8 else ""


def meta_key_state() -> dict[str, Any]:
    """Whether a key exists and where from. Reads no secret out of the Keychain."""
    env = os.environ.get("MODEL_API_KEY", "").strip()
    if env:
        return {"configured": True, "last4": _last4(env), "source": "environment"}
    data = load()
    if data.get("meta_key_store") == "keychain" and data.get("meta_key_last4") and keychain_available():
        return {"configured": True, "last4": str(data["meta_key_last4"]), "source": "keychain"}
    stored = data.get("meta_api_key")
    if isinstance(stored, str) and stored.strip():
        return {"configured": True, "last4": _last4(stored.strip()), "source": "file"}
    return {"configured": False, "last4": "", "source": None}


def meta_api_key() -> str:
    """The key for a Meta request. Environment first, then Keychain, then the file."""
    env = os.environ.get("MODEL_API_KEY", "").strip()
    if env:
        return env
    data = load()
    if data.get("meta_key_store") == "keychain":
        return _keychain_read() if keychain_available() else ""
    stored = data.get("meta_api_key")
    return stored.strip() if isinstance(stored, str) else ""


def meta_enabled() -> bool:
    env = os.environ.get("VEDIT_META_ENABLED")
    if env:
        return env == "1"
    return load().get("meta_enabled") is True


def meta_mode() -> str:
    """The Meta mode. A bad environment value is returned so the adapter can refuse it."""
    env = os.environ.get("VEDIT_META_MODE", "").strip().lower()
    if env:
        return env
    saved = load().get("meta_mode")
    return saved if saved in _MODES else "standard"


def default_budget() -> float:
    saved = load().get("default_budget_usd")
    if isinstance(saved, (int, float)) and not isinstance(saved, bool) and math.isfinite(saved) and BUDGET_MIN <= saved <= BUDGET_MAX:
        return float(saved)
    return DEFAULT_BUDGET


def default_language() -> str:
    saved = load().get("default_language")
    return saved if isinstance(saved, str) and _LANGUAGE.fullmatch(saved) else DEFAULT_LANGUAGE


def default_workspace() -> Path:
    """Where a person's projects live when nothing else says.

    macOS shares the native app's folder, so the app and `briefcut` show the same projects.
    """
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Vedit"
    base = os.environ.get("XDG_DATA_HOME", "").strip()
    return (Path(base) if base else Path.home() / ".local" / "share") / "briefcut"


def configured_workspace() -> tuple[Path | None, str]:
    """The workspace someone asked for, and who asked: environment, then Settings."""
    env = os.environ.get("VEDIT_WORKSPACE", "").strip()
    if env:
        return Path(env).expanduser(), "environment"
    saved = load().get("workspace")
    if isinstance(saved, str) and saved.strip():
        return Path(saved), "settings"
    return None, "default"


def enter_workspace(default: Path | None = None) -> Path:
    """Move this process to the workspace. With nothing chosen and no default, stay put.

    A folder on a drive that is not mounted must not stop Studio from opening,
    so a failure is reported and the process keeps its current folder.
    """
    chosen, _source = configured_workspace()
    target = chosen or default
    if target is None:
        return Path.cwd()
    try:
        target.mkdir(parents=True, exist_ok=True)
        os.chdir(target)
    except OSError:
        print("Briefcut could not open its project folder. Check Settings or VEDIT_WORKSPACE. Staying in the current folder.", file=sys.stderr)
    return Path.cwd()


def load_env_file(path: Path) -> list[str]:
    """Apply `KEY=value` lines for the Meta settings. Existing environment wins. Returns names set."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    applied = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.removeprefix("export ").split("=", 1)
        name, value = name.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if name in _ENV_LOCAL_KEYS and value and not os.environ.get(name):
            os.environ[name] = value
            applied.append(name)
    return applied


def load_checkout_env() -> list[str]:
    """`.env.local` beside this checkout's pyproject.toml, as scripts/studio.sh sources it."""
    root = Path(__file__).resolve().parents[2]
    return load_env_file(root / ".env.local") if (root / "pyproject.toml").is_file() else []


def _display_path(path: Path) -> str:
    text = str(path)
    home = str(Path.home())
    return "~" + text[len(home):] if text == home or text.startswith(home + "/") else text


def public() -> dict[str, Any]:
    """Everything the browser may know. Never contains the key."""
    data = load()
    chosen, workspace_source = configured_workspace()
    current = Path.cwd()
    env_enabled = bool(os.environ.get("VEDIT_META_ENABLED"))
    env_mode = bool(os.environ.get("VEDIT_META_MODE", "").strip())
    saved_mode = data.get("meta_mode") if data.get("meta_mode") in _MODES else "standard"
    return {
        "meta_key": meta_key_state(),
        "meta_enabled": meta_enabled(),
        "meta_enabled_source": "environment" if env_enabled else "settings",
        "meta_mode": meta_mode() if env_mode else saved_mode,
        "meta_mode_source": "environment" if env_mode else "settings",
        "default_budget_usd": default_budget(),
        "default_language": default_language(),
        "workspace": {
            "current": str(current),
            "saved": data.get("workspace") if isinstance(data.get("workspace"), str) else "",
            "source": workspace_source,
            "restart_required": chosen is not None and _resolved(chosen) != _resolved(current),
        },
        "keychain_available": keychain_available(),
        "config_path": _display_path(config_path()),
        "limits": {"budget_min": BUDGET_MIN, "budget_max": BUDGET_MAX},
    }


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


def _checked_key(value: Any) -> str:
    if not isinstance(value, str) or not _KEY.fullmatch(value.strip()):
        raise SettingsError("That does not look like an API key. Paste the whole key, with no spaces or quotes.")
    return value.strip()


def _checked_budget(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SettingsError("The default budget must be a number.")
    try:
        number = float(value)
    except OverflowError as exc:
        raise SettingsError("The default budget must be a finite number.") from exc
    if not math.isfinite(number) or not BUDGET_MIN <= number <= BUDGET_MAX:
        raise SettingsError(f"The default budget must be between ${BUDGET_MIN:g} and ${BUDGET_MAX:g}.")
    return round(number, 2)


def _checked_workspace(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 1024 or any(ord(character) < 32 for character in value):
        raise SettingsError("Choose a folder by its full path.")
    text = value.strip()
    if not text:
        return ""
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise SettingsError("Choose a folder by its full path, for example ~/Movies/Briefcut.")
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SettingsError("Briefcut could not create that folder. Choose one you can write to.") from exc
    if not path.is_dir() or not os.access(path, os.W_OK | os.X_OK):
        raise SettingsError("Briefcut cannot write to that folder. Choose one you can write to.")
    return str(path.resolve())


def update(payload: Any) -> dict[str, Any]:
    """Validate everything first, then save, so one bad field changes nothing."""
    if not isinstance(payload, dict):
        raise SettingsError("Send a JSON object.")
    if set(payload) - _FIELDS:
        raise SettingsError("One of those settings is not recognised.")
    changes: dict[str, Any] = {}
    key = _checked_key(payload["meta_api_key"]) if payload.get("meta_api_key") else None
    clear = payload.get("clear_meta_api_key", False)
    if not isinstance(clear, bool):
        raise SettingsError("clear_meta_api_key must be true or false.")
    if key and clear:
        raise SettingsError("Choose either to save a key or to remove it.")
    if "meta_enabled" in payload:
        if not isinstance(payload["meta_enabled"], bool):
            raise SettingsError("meta_enabled must be true or false.")
        changes["meta_enabled"] = payload["meta_enabled"]
    if "meta_mode" in payload:
        if payload["meta_mode"] not in _MODES:
            raise SettingsError("Choose standard or contributor mode.")
        changes["meta_mode"] = payload["meta_mode"]
    if "default_budget_usd" in payload:
        changes["default_budget_usd"] = _checked_budget(payload["default_budget_usd"])
    if "default_language" in payload:
        language = payload["default_language"]
        if not isinstance(language, str) or not _LANGUAGE.fullmatch(language):
            raise SettingsError("Choose a caption language code, such as en, es, or auto.")
        changes["default_language"] = language
    if "workspace" in payload:
        changes["workspace"] = _checked_workspace(payload["workspace"])
    with _LOCK:
        data = load()
        if clear:
            if data.get("meta_key_store") == "keychain" and keychain_available():
                _keychain_delete()
            for name in ("meta_api_key", "meta_key_store", "meta_key_last4"):
                data.pop(name, None)
        if key:
            if keychain_available() and _keychain_store(key):
                data.pop("meta_api_key", None)
                data["meta_key_store"] = "keychain"
            else:
                data["meta_api_key"] = key
                data["meta_key_store"] = "file"
            data["meta_key_last4"] = _last4(key)
        data.update(changes)
        if data.get("workspace") == "":
            data.pop("workspace")
        _write(data)
    return public()
