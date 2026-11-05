"""Settings: where the key lives, who may read it, and that the environment still wins."""

from __future__ import annotations

import http.client
import json
import os
import stat
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from vedit import meta, settings
from vedit.campaign import CampaignManager
from vedit.studio.server import StudioHandler, _LoopbackServer

KEY = "live-test-key-1234abcd"
ORIGIN_ROUTES = [
    ("GET", "/api/settings", None),
    ("POST", "/api/settings", {"default_language": "es"}),
    ("POST", "/api/settings/test", {}),
]


def call(
    port: int, method: str, path: str, payload: object = None, headers: dict[str, str] | None = None
) -> tuple[int, dict]:
    outgoing = dict(headers or {})
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
        outgoing.setdefault("Content-Type", "application/json")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(method, path, body=body, headers=outgoing)
        response = connection.getresponse()
        raw = response.read()
        return response.status, json.loads(raw) if raw else {}
    finally:
        connection.close()


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("MODEL_API_KEY", "VEDIT_META_ENABLED", "VEDIT_META_MODE", "VEDIT_WORKSPACE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None) -> Iterator[int]:
    monkeypatch.chdir(tmp_path)
    httpd = _LoopbackServer(("127.0.0.1", 0), StudioHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(httpd.server_address[1])
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)


def fake_security(folder: Path, *, fails: bool = False) -> Path:
    """A `security` that keeps its one secret in a file and logs its arguments."""
    folder.mkdir(parents=True, exist_ok=True)
    tool = folder / "security"
    tool.write_text(
        f"""#!/bin/sh
store={folder}/secret
printf '%s\\n' "$*" >> {folder}/argv.log
{"exit 1" if fails else ""}
case "$1" in
  -i) read -r line
      printf '%s\\n' "$line" | sed -e 's/.* -w "//' -e 's/".*//' > "$store" ;;
  find-generic-password) cat "$store" 2>/dev/null || exit 44 ;;
  delete-generic-password) rm -f "$store" ;;
esac
""",
        encoding="utf-8",
    )
    tool.chmod(0o755)
    return folder


@pytest.fixture
def keychain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None) -> Path:
    folder = fake_security(tmp_path / "bin")
    monkeypatch.setenv("PATH", f"{folder}:{os.environ['PATH']}")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv("VEDIT_NO_KEYCHAIN", raising=False)
    settings._keychain_cache.clear()
    yield folder
    settings._keychain_cache.clear()


def test_file_fallback_is_private_and_the_key_is_never_returned(clean_env: None) -> None:
    state = settings.update({"meta_api_key": KEY})
    path = settings.config_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert KEY in path.read_text(encoding="utf-8")
    assert state["meta_key"] == {"configured": True, "last4": "abcd", "source": "file"}
    assert KEY not in json.dumps(state)
    assert not list(path.parent.glob(".config-*"))


def test_folder_is_created_private(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None) -> None:
    home = tmp_path / "new" / "home"
    monkeypatch.setenv("VEDIT_HOME", str(home))
    settings.update({"default_language": "es"})
    assert stat.S_IMODE(home.stat().st_mode) == 0o700


def test_a_widened_file_is_tightened_on_read(clean_env: None) -> None:
    settings.update({"meta_api_key": KEY})
    settings.config_path().chmod(0o644)
    assert settings.load()["meta_key_last4"] == "abcd"
    assert stat.S_IMODE(settings.config_path().stat().st_mode) == 0o600


def test_a_damaged_file_does_not_stop_anything(clean_env: None) -> None:
    settings.config_path().parent.mkdir(parents=True, exist_ok=True)
    settings.config_path().write_text("{not json", encoding="utf-8")
    assert settings.public()["meta_key"]["configured"] is False
    assert meta.capabilities()["available"] is False


def test_http_round_trip_never_exposes_the_key(server: int) -> None:
    status, posted = call(
        server,
        "POST",
        "/api/settings",
        {
            "meta_api_key": KEY,
            "meta_enabled": True,
            "meta_mode": "contributor",
            "default_budget_usd": 1.5,
            "default_language": "es",
        },
    )
    assert status == 200
    status, read = call(server, "GET", "/api/settings")
    assert status == 200
    for body in (posted, read):
        assert KEY not in json.dumps(body)
        assert body["settings"]["meta_key"]["last4"] == "abcd"
        assert body["settings"]["meta_enabled"] is True
        assert body["settings"]["meta_mode"] == "contributor"
        assert body["settings"]["default_budget_usd"] == 1.5
        assert body["settings"]["default_language"] == "es"
    status, capabilities = call(server, "GET", "/api/capabilities")
    assert status == 200 and KEY not in json.dumps(capabilities)
    assert capabilities["meta"]["available"] is True
    assert capabilities["meta"]["mode"] == "contributor"


def test_a_rejected_key_is_not_repeated_back(server: int) -> None:
    bad = 'has "quotes" and spaces'
    status, body = call(server, "POST", "/api/settings", {"meta_api_key": bad})
    assert status == 400
    assert bad not in json.dumps(body)
    assert settings.load() == {}


@pytest.mark.parametrize(
    "payload",
    [
        {"meta_enabled": "yes"},
        {"meta_mode": "cheap"},
        {"default_budget_usd": 0},
        {"default_budget_usd": 6},
        {"default_budget_usd": True},
        {"default_budget_usd": float("inf")},
        {"default_language": "../etc"},
        {"workspace": "relative/folder"},
        {"workspace": "/dev/null/x"},
        {"unknown": 1},
        {"meta_api_key": KEY, "clear_meta_api_key": True},
    ],
)
def test_one_bad_field_saves_nothing(payload: dict, clean_env: None) -> None:
    with pytest.raises(settings.SettingsError):
        settings.update({"default_language": "es", **payload})
    assert settings.load() == {}


def test_clearing_removes_the_key(clean_env: None) -> None:
    settings.update({"meta_api_key": KEY})
    state = settings.update({"clear_meta_api_key": True})
    assert state["meta_key"]["configured"] is False
    assert KEY not in settings.config_path().read_text(encoding="utf-8")


def test_adapter_reads_settings_when_the_environment_is_silent(clean_env: None) -> None:
    assert meta.capabilities()["configured"] is False
    settings.update({"meta_api_key": KEY})
    assert meta.capabilities()["configured"] is True
    assert meta.capabilities()["available"] is False, "a key alone must not enable paid calls"
    settings.update({"meta_enabled": True, "meta_mode": "contributor"})
    status = meta.capabilities()
    assert status["available"] is True and status["model"] == "muse-spark-1.3-contributor"


def test_environment_wins_over_settings(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    settings.update({"meta_api_key": KEY, "meta_enabled": True, "meta_mode": "contributor"})
    monkeypatch.setenv("MODEL_API_KEY", "from-the-environment")
    monkeypatch.setenv("VEDIT_META_ENABLED", "0")
    monkeypatch.setenv("VEDIT_META_MODE", "standard")
    assert settings.meta_api_key() == "from-the-environment"
    status = meta.capabilities()
    assert status["enabled"] is False and status["mode"] == "standard"
    state = settings.public()
    assert state["meta_key"] == {"configured": True, "last4": "ment", "source": "environment"}
    assert state["meta_enabled_source"] == "environment" and state["meta_mode_source"] == "environment"


def test_bad_environment_mode_is_still_refused(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VEDIT_META_MODE", "cheap")
    assert "VEDIT_META_MODE" in meta.capabilities()["reason"]


def test_the_request_carries_the_saved_key(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    settings.update({"meta_api_key": KEY, "meta_enabled": True})
    seen: dict[str, str] = {}

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, _limit):
            return b'{"data": []}'

    class Opener:
        def open(self, request, timeout):
            seen.update(request.header_items())
            return Reply()

    monkeypatch.setattr(meta, "build_opener", lambda *_: Opener())
    assert meta.check_access()["ok"] is True
    assert seen["Authorization"] == f"Bearer {KEY}"


def test_a_missing_key_stops_the_request_before_the_network(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    settings.update({"meta_api_key": KEY, "meta_enabled": True})
    monkeypatch.setattr(settings, "meta_api_key", lambda: "")
    monkeypatch.setattr(meta, "build_opener", lambda *_: pytest.fail("no network call without a key"))
    assert "could not be read" in meta.check_access()["reason"]


def test_test_connection_is_the_same_free_check(server: int, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(
        meta,
        "check_access",
        lambda: calls.append(1) or {"ok": True, "models": ["muse-spark-1.3"], "reason": "Meta credentials work."},
    )
    status, body = call(server, "POST", "/api/settings/test", {})
    assert status == 200 and calls == [1]
    assert body["access"]["reason"] == "Meta credentials work."


def test_test_connection_says_why_when_meta_is_off(server: int) -> None:
    status, body = call(server, "POST", "/api/settings/test", {})
    assert status == 200
    assert body["access"]["ok"] is False and "Settings" in body["access"]["reason"]


@pytest.mark.parametrize(("method", "path", "payload"), ORIGIN_ROUTES)
@pytest.mark.parametrize(
    "headers", [{"Host": "evil.example"}, {"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}]
)
def test_new_routes_refuse_other_origins(
    server: int, method: str, path: str, payload: object, headers: dict[str, str]
) -> None:
    status, body = call(server, method, path, payload, headers)
    assert status == 403, body
    assert settings.load() == {}


def test_settings_post_needs_a_json_content_type(server: int) -> None:
    status, _body = call(server, "POST", "/api/settings", {"default_language": "es"}, {"Content-Type": "text/plain"})
    assert status == 415
    assert settings.load() == {}


def test_keychain_is_preferred_and_the_key_stays_off_argv_and_disk(keychain: Path) -> None:
    state = settings.update({"meta_api_key": KEY})
    assert state["meta_key"]["source"] == "keychain" and state["meta_key"]["last4"] == "abcd"
    assert KEY not in settings.config_path().read_text(encoding="utf-8")
    assert KEY not in (keychain / "argv.log").read_text(encoding="utf-8")
    settings._keychain_cache.clear()
    assert settings.meta_api_key() == KEY


def test_keychain_is_not_touched_until_a_key_is_needed(keychain: Path) -> None:
    settings.update({"default_language": "es"})
    assert meta.capabilities()["configured"] is False
    assert not (keychain / "argv.log").exists()


def test_clearing_removes_the_keychain_item(keychain: Path) -> None:
    settings.update({"meta_api_key": KEY})
    settings.update({"clear_meta_api_key": True})
    assert not (keychain / "secret").exists()
    assert settings.meta_api_key() == ""


def test_keychain_failure_falls_back_to_the_private_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
) -> None:
    folder = fake_security(tmp_path / "broken", fails=True)
    monkeypatch.setenv("PATH", f"{folder}:{os.environ['PATH']}")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv("VEDIT_NO_KEYCHAIN", raising=False)
    state = settings.update({"meta_api_key": KEY})
    assert state["meta_key"]["source"] == "file"
    assert stat.S_IMODE(settings.config_path().stat().st_mode) == 0o600


def test_no_keychain_off_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("VEDIT_NO_KEYCHAIN", raising=False)
    assert settings.keychain_available() is False


@pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get("VEDIT_TEST_KEYCHAIN") != "1",
    reason="real macOS Keychain; run with VEDIT_TEST_KEYCHAIN=1",
)
def test_real_keychain_round_trip_in_a_throwaway_keychain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
) -> None:
    path = tmp_path / "throwaway.keychain-db"
    subprocess.run(["security", "create-keychain", "-p", "test", str(path)], check=True)
    try:
        subprocess.run(["security", "unlock-keychain", "-p", "test", str(path)], check=True)
        monkeypatch.setenv("VEDIT_KEYCHAIN", str(path))
        monkeypatch.delenv("VEDIT_NO_KEYCHAIN", raising=False)
        settings._keychain_cache.clear()
        assert settings.update({"meta_api_key": KEY})["meta_key"]["source"] == "keychain"
        settings._keychain_cache.clear()
        assert settings.meta_api_key() == KEY
        settings.update({"clear_meta_api_key": True})
        settings._keychain_cache.clear()
        assert settings._keychain_read() == ""
    finally:
        subprocess.run(["security", "delete-keychain", str(path)], check=False)


def test_workspace_order_is_environment_then_settings_then_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
) -> None:
    assert settings.configured_workspace() == (None, "default")
    chosen = tmp_path / "projects"
    state = settings.update({"workspace": str(chosen)})
    assert chosen.is_dir()
    assert state["workspace"]["source"] == "settings" and state["workspace"]["restart_required"] is True
    assert settings.configured_workspace() == (chosen.resolve(), "settings")
    monkeypatch.setenv("VEDIT_WORKSPACE", str(tmp_path / "from-env"))
    assert settings.configured_workspace() == (tmp_path / "from-env", "environment")


def test_enter_workspace_moves_there_and_survives_a_missing_drive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert settings.enter_workspace() == tmp_path.resolve(), "nothing chosen and no default stays put"
    target = tmp_path / "default"
    assert settings.enter_workspace(target) == target.resolve()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VEDIT_WORKSPACE", "/dev/null/gone")
    assert settings.enter_workspace(target) == tmp_path.resolve()
    assert "project folder" in capsys.readouterr().err


def test_a_workspace_can_be_cleared(tmp_path: Path, clean_env: None) -> None:
    settings.update({"workspace": str(tmp_path / "a")})
    assert settings.update({"workspace": ""})["workspace"]["source"] == "default"
    assert "workspace" not in settings.load()


def test_default_workspace_follows_the_platform(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert settings.default_workspace() == tmp_path / "Library" / "Application Support" / "Vedit"
    monkeypatch.setattr(sys, "platform", "linux")
    assert settings.default_workspace() == tmp_path / ".local" / "share" / "briefcut"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert settings.default_workspace() == tmp_path / "data" / "briefcut"


def test_env_local_is_applied_without_overriding_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
) -> None:
    env = tmp_path / ".env.local"
    env.write_text(
        "# secrets\nexport MODEL_API_KEY='from-file'\nVEDIT_META_ENABLED=1\nVEDIT_META_MODE=contributor\nPATH=/evil\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VEDIT_META_MODE", "standard")
    before = os.environ["PATH"]
    applied = settings.load_env_file(env)
    assert set(applied) == {"MODEL_API_KEY", "VEDIT_META_ENABLED"}
    assert settings.meta_api_key() == "from-file" and settings.meta_mode() == "standard"
    assert os.environ["PATH"] == before, "only the Meta settings are read"


def test_saved_defaults_fill_a_request_that_omits_them(tmp_path: Path, clean_env: None) -> None:
    source = tmp_path / "vedit-sessions" / "source" / "inbox" / "clip.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"not read here")
    settings.update({"default_budget_usd": 2, "default_language": "es"})
    manager = CampaignManager(tmp_path, start_worker=False)
    try:
        request = manager._request({"paths": [str(source)], "goal": "A story"})
        assert request["budget_usd"] == 2 and request["language"] == "es"
        request = manager._request({"paths": [str(source)], "goal": "A story", "budget_usd": 1, "language": "fr"})
        assert request["budget_usd"] == 1 and request["language"] == "fr"
    finally:
        manager.close()


def test_no_meta_is_refused_with_a_settings_hint(tmp_path: Path, clean_env: None) -> None:
    from vedit.campaign import CampaignError

    source = tmp_path / "vedit-sessions" / "source" / "inbox" / "clip.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"x")
    manager = CampaignManager(tmp_path, start_worker=False)
    try:
        with pytest.raises(CampaignError, match="Settings"):
            manager._request({"paths": [str(source)], "goal": "A story", "provider": "meta"})
    finally:
        manager.close()
